#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Optuna ile Ultimate Reversal Engine parametre optimizasyonu

- ccxt ile Binance USDT-M futures geçmiş veri (1m) çekilir.
- TÜM USDT-M futures sembolleri için strateji backtest edilir.
- Hedef: Toplam PnL / Drawdown oranını MAKSİMİZE etmek.
"""

import asyncio
from typing import Dict, List, Tuple

import optuna
import numpy as np
import ccxt.async_support as ccxt_async


# =========================
# 1) STRATEJİ MATEMATİĞİ
# =========================

class UltimateMath:
    @staticmethod
    def ema(prices: np.ndarray, alpha: float) -> np.ndarray:
        ema = np.zeros_like(prices)
        ema[0] = prices[0]
        for i in range(1, len(prices)):
            ema[i] = alpha * prices[i] + (1 - alpha) * ema[i-1]
        return ema

    @staticmethod
    def rolling_vwap(prices: np.ndarray, volumes: np.ndarray) -> float:
        pv = np.sum(prices * volumes)
        vv = np.sum(volumes)
        if vv == 0:
            return float(prices[-1])
        return float(pv / vv)

    @staticmethod
    def z_score(window: np.ndarray, current: float):
        mean = float(window.mean())
        std = float(window.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        z = (current - mean) / std
        return z, mean, std

    @staticmethod
    def get_sigma(window: np.ndarray):
        mean = float(window.mean())
        std = float(window.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        return mean, std

    @staticmethod
    def poly_curve_signal(
        prices_tail: np.ndarray,
        side: str,
        poly_window: int,
        min_curvature: float
    ):
        if len(prices_tail) < poly_window:
            return False, 0.0, 0.0, 0.0
        y = prices_tail[-poly_window:]
        x = np.arange(len(y))
        try:
            a, b, c = np.polyfit(x, y, 2)
        except Exception:
            return False, 0.0, 0.0, 0.0
        curvature = a
        slope = b
        current_slope = 2 * a * (len(x) - 1) + b

        if side == "SHORT":
            broken = (curvature < -min_curvature) and (current_slope < 0)
        else:
            broken = (curvature > min_curvature) and (current_slope > 0)

        return broken, curvature, slope, current_slope


# =========================
# 2) BASİT BACKTEST
# =========================

REVERSAL_CONFIRM_PCT_MIN = 0.3   # çok küçük hareketlerde bile en az %0.3 reversal istiyoruz

def get_dynamic_rev(move_pct: float) -> float:
    abs_move = abs(move_pct)
    for limit, thr in [
        (15.0, 0.5),
        (7.0, 0.8),
        (3.0, 1.2),
        (1.5, 1.8),
    ]:
        if abs_move >= limit:
            return max(thr, REVERSAL_CONFIRM_PCT_MIN)
    return 999.0


def backtest_symbol(
    closes: np.ndarray,
    volumes: np.ndarray,
    params: Dict,
    fee_rate: float = 0.0004,  # takas+komisyon ~0.04%
    tp_pct: float = 0.003,     # %0.3 TP
    sl_pct: float = 0.004,     # %0.4 SL
) -> Dict:
    """
    Tek bir sembol için basit long/short backtest.
    1x notional, sadece yön önemli.
    """
    CONTEXT_WINDOW = params["CONTEXT_WINDOW"]
    VWAP_WINDOW    = params["VWAP_WINDOW"]
    SIGMA_WINDOW   = params["SIGMA_WINDOW"]
    POLY_WINDOW    = params["POLY_WINDOW"]

    BASE_Z             = params["BASE_Z_SCORE_THRESHOLD"]
    VWAP_DEV           = params["VWAP_STRETCH_THRESHOLD"]
    SIGMA_MOVE_STRICT  = params["SIGMA_MOVE_STRICT"]
    ENTRY_STRICTNESS   = params["ENTRY_STRICTNESS"]
    Z_EXIT_BAND        = params["Z_EXIT_BAND"]
    CURVE_ACCELERATOR  = params["CURVE_ACCELERATOR"]
    MIN_CURV           = params["MIN_CURVATURE"]
    EMA_ALPHA          = params["EMA_ALPHA"]
    MIN_RANGE_PCT      = params["MIN_RANGE_PCT"]

    equity = 1.0
    peak_equity = 1.0
    dd_max = 0.0
    trades = 0
    wins = 0
    losses = 0

    in_position = False
    pos_side = None   # "LONG" / "SHORT"
    entry_price = 0.0

    sigma_avg = 0.0
    z_peak = 0.0

    for i in range(CONTEXT_WINDOW, len(closes)):
        window = closes[i-CONTEXT_WINDOW:i]
        price = closes[i]

        # ölü market filtresi
        tail = closes[i-SIGMA_WINDOW:i]
        r = (tail.max() - tail.min()) / price * 100
        if r < MIN_RANGE_PCT:
            if in_position:
                # basit: bu barı geç
                pass
            continue

        z, ctx_mean, ctx_sigma = UltimateMath.z_score(window, price)
        _, sigma_short = UltimateMath.get_sigma(closes[i-SIGMA_WINDOW:i])

        # coin volatilite karakteri
        sigma_avg = sigma_short if sigma_avg == 0 else 0.98*sigma_avg + 0.02*sigma_short
        abs_z = abs(z)
        z_peak = abs_z if z_peak == 0 else 0.98*z_peak + 0.02*abs_z

        # dinamik Z threshold
        dyn_z_th = 0.7 * z_peak + 0.3 * BASE_Z
        dyn_z_th = max(1.8, min(4.0, dyn_z_th))

        vwap = UltimateMath.rolling_vwap(
            closes[i-VWAP_WINDOW:i], volumes[i-VWAP_WINDOW:i]
        )

        smooth = UltimateMath.ema(closes[i-CONTEXT_WINDOW:i], EMA_ALPHA)
        v = np.diff(smooth)
        a_arr = np.diff(v)
        a = a_arr[-1] if len(a_arr) else 0.0

        vwap_dev_pct = (price - vwap) / vwap * 100

        # trade açıkken önce TP/SL kontrolü
        if in_position:
            high = price
            low  = price

            if pos_side == "LONG":
                tp = entry_price * (1 + tp_pct)
                sl = entry_price * (1 - sl_pct)
                exit_price = None
                if low <= sl:
                    exit_price = sl
                elif high >= tp:
                    exit_price = tp

                if exit_price:
                    pnl = (exit_price / entry_price - 1) - fee_rate*2
                    equity *= (1 + pnl)
                    in_position = False
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1

            elif pos_side == "SHORT":
                tp = entry_price * (1 - tp_pct)
                sl = entry_price * (1 + sl_pct)
                exit_price = None
                if high >= sl:
                    exit_price = sl
                elif low <= tp:
                    exit_price = tp

                if exit_price:
                    pnl = (entry_price / exit_price - 1) - fee_rate*2
                    equity *= (1 + pnl)
                    in_position = False
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1

            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity
            dd_max = max(dd_max, dd)

            # pozisyon açıksa yeni sinyal aramıyoruz
            continue

        # pozisyon yoksa sinyal arıyoruz

        # vol ratio → çok volatilse daha sert reversal iste
        vol_ratio = sigma_short / sigma_avg if sigma_avg > 0 else 1.0
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        # PUMP (SHORT)
        if z >= dyn_z_th and vwap_dev_pct >= VWAP_DEV:
            apex = price
            nadir = window.min()
            pump_move = apex - nadir
            pump_pct = (pump_move / nadir) * 100 if nadir > 0 else 0.0

            base_rev = get_dynamic_rev(pump_pct)
            curve_broken, curv, slope, cur_slope = UltimateMath.poly_curve_signal(
                closes[i-CONTEXT_WINDOW:i], "SHORT", POLY_WINDOW, MIN_CURV
            )
            final_rev = base_rev * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_rev *= ENTRY_STRICTNESS * vol_factor

            if i+1 >= len(closes):
                break
            future_price = closes[i+1]
            drop_pct = (apex - future_price) / apex * 100
            drop_sigma = (apex - future_price) / max(sigma_short, 1e-6)

            z_next, *_ = UltimateMath.z_score(window, future_price)
            z_back = z_next <= dyn_z_th * Z_EXIT_BAND

            if (
                drop_pct >= final_rev and
                drop_sigma >= SIGMA_MOVE_STRICT and
                a < 0 and
                z_back
            ):
                in_position = True
                pos_side = "SHORT"
                entry_price = future_price
                equity *= (1 - fee_rate)

        # DUMP (LONG)
        elif z <= -dyn_z_th and vwap_dev_pct <= -VWAP_DEV:
            nadir = price
            apex = window.max()
            dump_move = apex - nadir
            dump_pct = (dump_move / apex) * 100 if apex > 0 else 0.0

            base_bounce = get_dynamic_rev(dump_pct)
            curve_broken, curv, slope, cur_slope = UltimateMath.poly_curve_signal(
                closes[i-CONTEXT_WINDOW:i], "LONG", POLY_WINDOW, MIN_CURV
            )
            final_bounce = base_bounce * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_bounce *= ENTRY_STRICTNESS * vol_factor

            if i+1 >= len(closes):
                break
            future_price = closes[i+1]
            bounce_pct = (future_price - nadir) / nadir * 100
            bounce_sigma = (future_price - nadir) / max(sigma_short, 1e-6)

            z_next, *_ = UltimateMath.z_score(window, future_price)
            z_back = z_next >= -dyn_z_th * Z_EXIT_BAND

            if (
                bounce_pct >= final_bounce and
                bounce_sigma >= SIGMA_MOVE_STRICT and
                a > 0 and
                z_back
            ):
                in_position = True
                pos_side = "LONG"
                entry_price = future_price
                equity *= (1 - fee_rate)

        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity
        dd_max = max(dd_max, dd)

    return {
        "equity": equity,
        "dd_max": dd_max,
        "trades": trades,
        "wins": wins,
        "losses": losses,
    }


# =========================
# 3) TÜM FUTURES SEMBOLLERİ + DATA ÇEKME
# =========================

async def pick_all_futures_symbols() -> List[str]:
    """
    Binance USDM'den TÜM USDT-M perpetual kontrat sembollerini döndür.
    """
    ex = ccxt_async.binanceusdm({"enableRateLimit": True})
    markets = await ex.load_markets()
    symbols: List[str] = []

    for sym, m in markets.items():
        # sadece USDT-M perpetual (quote=USDT ve contract=True)
        if m.get("quote") == "USDT" and m.get("contract", False):
            symbols.append(sym)

    await ex.close()

    symbols = sorted(symbols)  # sırala, sadece düzen olsun
    print(f"Toplam USDT-M futures sayısı: {len(symbols)}")
    print(symbols)
    return symbols


async def fetch_data_for_symbols(
    symbols: List[str],
    timeframe: str = "1m",
    per_symbol_limit: int = 10_000,   # her coin için hedef bar sayısı (10000 = ~1 hafta 1m)
) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """
    Binance USDM'den her sembol için ~per_symbol_limit kadar 1m mum çeker.
    fetch_ohlcv limit kısıtlarını aşmak için pagination yapar.
    DİKKAT: Tüm coinler + çok büyük per_symbol_limit çok ağır olur.
    """
    ex = ccxt_async.binanceusdm({"enableRateLimit": True})
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}

    tf_ms = 60_000   # 1m
    max_chunk = 1000
    print(f"{len(symbols)} sembol için yaklaşık {per_symbol_limit} bar çekilecek...")

    for idx, sym in enumerate(symbols, start=1):
        try:
            print(f"[{idx}/{len(symbols)}] [DATA] {sym} için veri çekiliyor...")
            all_ohlcv: List[List[float]] = []

            now = ex.milliseconds()
            since = now - per_symbol_limit * tf_ms

            while True:
                ohlcv = await ex.fetch_ohlcv(
                    sym,
                    timeframe,
                    since=since,
                    limit=max_chunk
                )
                if not ohlcv:
                    break

                all_ohlcv.extend(ohlcv)

                last_ts = ohlcv[-1][0]
                since = last_ts + tf_ms

                if len(all_ohlcv) >= per_symbol_limit:
                    break

                await asyncio.sleep(0.05)

            if len(all_ohlcv) == 0:
                print(f"[DATA] {sym}: veri alınamadı, atlanıyor.")
                continue

            all_ohlcv = all_ohlcv[-per_symbol_limit:]

            closes = np.array([x[4] for x in all_ohlcv], dtype=float)
            vols   = np.array([x[5] for x in all_ohlcv], dtype=float)

            # Çok az bar varsa (ör: yeni açılmış kontrat), çöpe at
            if len(closes) < 200:
                print(f"[DATA] {sym}: yeterli veri yok ({len(closes)} bar), atlanıyor.")
                continue

            out[sym] = (closes, vols)
            print(f"[DATA] {sym}: {len(closes)} bar alındı.")

        except Exception as e:
            print(f"[DATA ERR] {sym}: {e}")
            continue

    await ex.close()
    print("Veri çekme bitti.")
    return out


# =========================
# 4) OPTUNA OBJECTIVE
# =========================

HISTORY_CACHE: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}  # global cache: symbol -> (closes, vols)


def create_params_from_trial(trial: optuna.Trial) -> Dict:
    return {
        "CONTEXT_WINDOW": 200,
        "VWAP_WINDOW": 60,
        "SIGMA_WINDOW": 30,
        "POLY_WINDOW": 20,

        "BASE_Z_SCORE_THRESHOLD": trial.suggest_float("BASE_Z", 2.0, 4.0),
        "VWAP_STRETCH_THRESHOLD": trial.suggest_float("VWAP_DEV", 0.3, 2.0),
        "SIGMA_MOVE_STRICT":      trial.suggest_float("SIGMA_MOVE", 0.8, 2.0),
        "ENTRY_STRICTNESS":       trial.suggest_float("ENTRY_STRICT", 1.0, 1.6),
        "Z_EXIT_BAND":            trial.suggest_float("Z_EXIT_BAND", 0.3, 0.7),
        "CURVE_ACCELERATOR":      trial.suggest_float("CURVE_ACCEL", 0.4, 0.9),
        "MIN_CURVATURE":          trial.suggest_float("MIN_CURV", 0.00002, 0.0002),
        "EMA_ALPHA":              trial.suggest_float("EMA_ALPHA", 0.1, 0.4),
        "MIN_RANGE_PCT":          trial.suggest_float("MIN_RANGE", 0.1, 0.5),
    }


def objective(trial: optuna.Trial) -> float:
    params = create_params_from_trial(trial)

    symbols = list(HISTORY_CACHE.keys())
    if not symbols:
        return 0.0

    total_equity = 0.0
    total_dd = 0.0
    total_trades = 0

    for sym in symbols:
        closes, vols = HISTORY_CACHE[sym]
        res = backtest_symbol(closes, vols, params)
        total_equity += res["equity"]
        total_dd += res["dd_max"]
        total_trades += res["trades"]

    if total_trades == 0:
        return 0.0

    avg_equity = total_equity / len(symbols)
    avg_dd = total_dd / max(len(symbols), 1)

    # skor: (getiri / drawdown), trade azsa cezalandır
    reward = (avg_equity - 1.0) / max(avg_dd, 0.01)
    reward *= min(1.0, total_trades / 100)  # çok az trade ise etkisini azalt

    return reward


# =========================
# 5) ANA ÇALIŞTIRICI
# =========================

async def main():
    global HISTORY_CACHE

    print("TÜM USDT-M futures semboller çekiliyor...")
    symbols = await pick_all_futures_symbols()

    print("Veri çekiliyor...")
    HISTORY_CACHE = await fetch_data_for_symbols(
        symbols,
        timeframe="1m",
        per_symbol_limit=10_000,   # istersen burayı sonra yukarı çekersin
    )
    print("Toplanan sembol sayısı (yeterli datası olan):", len(HISTORY_CACHE))

    def obj(trial: optuna.Trial) -> float:
        return objective(trial)

    study = optuna.create_study(direction="maximize")
    study.optimize(obj, n_trials=50)   # burayı da sonra artırırsın (100, 200, ...)

    print("En iyi değerler:")
    print(study.best_params)
    print("En iyi skor:", study.best_value)


if __name__ == "__main__":
    asyncio.run(main())
