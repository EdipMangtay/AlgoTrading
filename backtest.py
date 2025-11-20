#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SECOND-LEVEL MULTI-SYMBOL BACKTEST
==================================
- Bu script, hammer_pro.py içindeki engine’in mantığını temel alır
  ve saniyelik 1s barlar üzerinden multi-symbol backtest yapar.
- 400 USDT-M coin’e kadar:
    • Son 5000 saniyelik (yaklaşık 1 saat 23 dk) trade datasını çeker
    • 1 saniyelik OHLCV barları üretir
    • Engine’dekine çok yakın, ama BİRAZ gevşetilmiş kurallarla sinyal üretir
    • Sinyal geldiğinde:
        - LONG/SHORT trade açar
    • Çıkış kuralları:
        - |Z| < 0.3 → mean reversion tamamladı
        - veya trend tersine döndü
- Her kapanan trade:
    • backtest_trades_seconds.csv   → tüm işlemler
    • backtest_summary_seconds.txt  → özet rapor
"""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt_async

# hammer_pro içindeki parametre ve matematiği aynen kullan
from hammer_pro import (
    UltimateMath,
    CONTEXT_WINDOW,
    VWAP_WINDOW,
    SIGMA_WINDOW,
    POLY_WINDOW,
    BASE_Z_SCORE_THRESHOLD,
    VWAP_STRETCH_THRESHOLD,
    SIGMA_MOVE_STRICT,
    ENTRY_STRICTNESS,
    Z_EXIT_BAND,
    CURVE_ACCELERATOR,
    MIN_CURVATURE,
    EMA_ALPHA,
    MIN_RANGE_PCT,
    REVERSAL_CONFIRM_PCT_MIN,
    DYNAMIC_THRESHOLDS,
    LOW_SIGMA_TH,
    HIGH_SIGMA_TH,
    Z_DEBOUNCE_LEN,
    Z_DEBOUNCE_COUNT,
    MIN_24H_VOL_USDT,
)


# ==========================
# GENEL AYARLAR
# ==========================

SECONDS_HISTORY = 5000      # her coin için 5000 saniye
MAX_SYMBOLS     = 400       # en fazla 400 coin
TRADES_LIMIT    = 20000     # güvenlik için max trade sayısı

TRADES_CSV_PATH  = "backtest_trades_seconds.csv"
SUMMARY_TXT_PATH = "backtest_summary_seconds.txt"

# ==========================
# BACKTEST İÇİN HAFİF GEVŞETME PARAMETRELERİ
# (Sadece bu dosyada kullanılıyor, live bot değişmiyor)
# ==========================

BT_RANGE_MULT = 0.6                     # ölü piyasa filtresi -> %40 daha esnek
BT_Z_DEBOUNCE_COUNT = max(1, Z_DEBOUNCE_COUNT - 1)
BT_SIGMA_MOVE_STRICT = max(0.8, SIGMA_MOVE_STRICT * 0.7)
BT_REV_REQ_MULT = 0.85                  # reversal yüzdesi %15 azaltıldı


# ==========================
# YILDIZ SKORU (engine ile aynı mantık)
# ==========================

def score_signal(side, z, dyn_z_th, reversal_sigma, vwap_dev,
                 curve_broken, wick_ok, trend, nw_extreme):
    score = 0

    # Z-score aşırılığı
    if abs(z) >= dyn_z_th:
        score += 1
    if abs(z) >= dyn_z_th * 1.3:
        score += 1

    # Reversal sigma
    if reversal_sigma >= 1.0:
        score += 1
    if reversal_sigma >= 2.0:
        score += 1

    # VWAP sapması
    if abs(vwap_dev) >= VWAP_STRETCH_THRESHOLD * 0.9:
        score += 1

    # Poly eğri kırılımı
    if curve_broken:
        score += 1

    # Wick teyidi
    if wick_ok:
        score += 1

    # Nadaraya overshoot
    if nw_extreme:
        score += 1

    # Trend uyumu (UP → SHORT, DOWN → LONG)
    if side == "SHORT" and trend == "UP":
        score += 1
    if side == "LONG" and trend == "DOWN":
        score += 1

    if score >= 6:
        stars = 3
    elif score >= 3:
        stars = 2
    else:
        stars = 1

    return stars


# ==========================
# RAPORLAYICI
# ==========================

class LiveReporter:
    def __init__(self, trades_csv_path, summary_txt_path):
        self.trades_csv_path = trades_csv_path
        self.summary_txt_path = summary_txt_path
        self.trades = []

    def on_trade_closed(self, trade_dict):
        # LOG: trade kapandığında terminale yaz
        print(
            f"[TRADE CLOSED] {trade_dict['symbol']} "
            f"{trade_dict['side']} "
            f"{trade_dict['pnl_pct']:.3f}% "
            f"({trade_dict['entry_time_str']} -> {trade_dict['exit_time_str']})"
        )

        self.trades.append(trade_dict)
        self._write_trades_csv()
        self._write_summary_txt()

    def _write_trades_csv(self):
        if not self.trades:
            return
        df = pd.DataFrame(self.trades)
        df.to_csv(self.trades_csv_path, index=False)

    def _write_summary_txt(self):
        if not self.trades:
            return

        df = pd.DataFrame(self.trades)

        total_trades = len(df)
        wins = (df["pnl_pct"] > 0).sum()
        losses = (df["pnl_pct"] <= 0).sum()
        winrate = wins / total_trades * 100.0 if total_trades > 0 else 0.0

        avg_pnl = df["pnl_pct"].mean()
        cum_pnl = df["pnl_pct"].sum()
        max_win = df["pnl_pct"].max()
        max_loss = df["pnl_pct"].min()

        by_sym = df.groupby("symbol")["pnl_pct"].agg(
            ["count", "mean", "sum"]
        ).sort_values("sum", ascending=False)

        lines = []
        lines.append("===== SECOND-LEVEL MULTI-SYMBOL BACKTEST SUMMARY =====")
        lines.append(f"Toplam işlem sayısı : {total_trades}")
        lines.append(f"Kazanan işlem sayısı: {wins}")
        lines.append(f"Kaybeden işlem sayısı: {losses}")
        lines.append(f"Winrate           : {winrate:.2f}%")
        lines.append("")
        lines.append(f"Ortalama PnL (trade başına) : {avg_pnl:.3f}%")
        lines.append(f"Kümülatif PnL (toplam)      : {cum_pnl:.3f}%")
        lines.append(f"Maksimum kazanç (PnL %)     : {max_win:.3f}%")
        lines.append(f"Maksimum kayıp (PnL %)      : {max_loss:.3f}%")
        lines.append("")
        lines.append("— Sembol Bazlı Performans —")
        if not by_sym.empty:
            lines.append(by_sym.to_string())
        else:
            lines.append("Veri yok.")

        with open(self.summary_txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))


# ==========================
# 1s OHLC BAR ÜRETİCİ (trades → 1s bar)
# ==========================

async def fetch_1s_bars_for_symbol(ex, symbol, seconds=SECONDS_HISTORY):
    """
    Binance USDT-M futures'dan son 'seconds' saniyelik trade verisini alır,
    1 saniyelik OHLCV barlarına çevirir.
    """
    print(f"[{symbol}] 1s barlar için trades çekiliyor...")

    now_ms = ex.milliseconds()
    start_ms = now_ms - seconds * 1000

    all_trades = []
    since = start_ms
    while True:
        batch = await ex.fetch_trades(symbol, since=since, limit=1000)
        if not batch:
            break
        all_trades.extend(batch)
        last_ts = batch[-1]["timestamp"]
        if last_ts >= now_ms or len(batch) < 1000:
            break
        since = last_ts + 1
        if len(all_trades) >= TRADES_LIMIT:
            break

    if not all_trades:
        print(f"[{symbol}] Hiç trade yok, atlanıyor.")
        return None

    # trade → 1s bucket
    buckets = {}
    last_price = float(all_trades[0]["price"])
    for tr in all_trades:
        ts = tr["timestamp"]
        sec = ts // 1000
        price = float(tr["price"])
        amount = float(tr["amount"])
        quote_vol = price * amount

        if sec not in buckets:
            buckets[sec] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": quote_vol,
            }
        else:
            b = buckets[sec]
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += quote_vol

        last_price = price

    start_sec = start_ms // 1000
    end_sec = now_ms // 1000

    secs = []
    opens = []
    highs = []
    lows = []
    closes = []
    vols = []

    last_o = last_h = last_l = last_c = last_price
    for sec in range(int(start_sec), int(end_sec) + 1):
        bar = buckets.get(sec)
        if bar:
            o = bar["open"]
            h = bar["high"]
            l = bar["low"]
            c = bar["close"]
            v = bar["volume"]
            last_o, last_h, last_l, last_c = o, h, l, c
        else:
            # o saniye hiç trade olmamış → flat bar
            o = last_c
            h = last_c
            l = last_c
            c = last_c
            v = 0.0

        secs.append(sec)
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        vols.append(v)

    # sadece son 'seconds' kadarını al
    if len(secs) > seconds:
        secs = secs[-seconds:]
        opens = opens[-seconds:]
        highs = highs[-seconds:]
        lows = lows[-seconds:]
        closes = closes[-seconds:]
        vols = vols[-seconds:]

    return (
        np.array(secs, dtype=np.int64),
        np.array(opens, dtype=float),
        np.array(highs, dtype=float),
        np.array(lows, dtype=float),
        np.array(closes, dtype=float),
        np.array(vols, dtype=float),
    )


# ==========================
# WICK FONKSİYONU (engine'deki 3s pseudo mum mantığı)
# → Biraz gevşetildi (body*1.5 ve range*0.3)
# ==========================

def check_wick_from_1s_history(ohlc_history, side, window_sec=3):
    """
    engine'deki _check_wick ile aynı mantık;
    ancak burada 1s OHLC history listesi üzerinden çalışıyor.
    Son window_sec bar'ı alıp kompozit mum üretiyor.
    """
    if len(ohlc_history) < window_sec:
        return False

    recent = list(ohlc_history)[-window_sec:]
    open_ = recent[0]["open"]
    close_ = recent[-1]["close"]
    high = max(b["high"] for b in recent)
    low = min(b["low"] for b in recent)

    body = abs(close_ - open_)
    rng = max(high - low, 1e-12)

    upper_wick = high - max(open_, close_)
    lower_wick = min(open_, close_) - low

    if side == "SHORT":
        if upper_wick > body * 1.5 and upper_wick > rng * 0.3:
            return True
    else:
        if lower_wick > body * 1.5 and lower_wick > rng * 0.3:
            return True

    return False


# ==========================
# BACKTEST ENGINE
# ==========================

class SecondBacktester:
    def __init__(self, reporter: LiveReporter):
        self.reporter = reporter

        # engine'deki state yapıları
        self.price_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
        self.volume_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
        self.state = defaultdict(lambda: {
            "mode": "IDLE",
            "apex": 0.0,
            "nadir": 0.0,
            "cooldown_until": 0.0,
            "context_mean": 0.0,
            "context_sigma": 0.0,
            "locked_sigma": 0.0,
            "sigma_avg": 0.0,
            "z_peak": 0.0,
            "last_signal_ts": 0.0,
            "last_z": 0.0,
            "last_trend": "RANGE",
        })
        self.z_history = defaultdict(lambda: deque(maxlen=Z_DEBOUNCE_LEN))
        self.ohlc_1s_history = defaultdict(lambda: deque(maxlen=60))

        # trade state
        self.open_positions = {}  # sym_id -> dict

    def _update_ohlc_1s(self, sym_id, ts_sec, price, high, low, open_, close_):
        """
        engine'deki _update_ohlc_1s ile aynı mantık;
        ancak websocket yerine hazır 1s bar verisi kullanıyoruz.
        """
        bar = {
            "ts": ts_sec,
            "open": open_,
            "high": high,
            "low": low,
            "close": close_,
        }
        self.ohlc_1s_history[sym_id].append(bar)

    def process_point(self, sym_id, idx, ts_sec, o, h, l, c, vol):
        """
        1 saniyelik bar üzerinden engine’deki process_series mantığını
        backtest için hafif gevşetilmiş haliyle uygular.
        """
        st = self.state[sym_id]

        # OHLC history güncelle
        self._update_ohlc_1s(sym_id, ts_sec, c, h, l, o, c)

        # buffer’lara ekle
        pb = self.price_buffers[sym_id]
        vb = self.volume_buffers[sym_id]
        pb.append(c)
        vb.append(vol)

        if len(pb) < CONTEXT_WINDOW:
            return  # yeterli context yok

        prices = np.array(pb, dtype=float)
        volumes = np.array(vb, dtype=float)
        current_price = float(c)
        now = float(ts_sec)  # saniye bazlı "şimdiki zaman"

        # Ölü piyasa filtresi (biraz gevşetilmiş)
        tail = prices[-SIGMA_WINDOW:]
        price_range_pct = (tail.max() - tail.min()) / max(current_price, 1e-12) * 100
        if price_range_pct < MIN_RANGE_PCT * BT_RANGE_MULT:
            return

        context_window = prices[-CONTEXT_WINDOW:]

        # Z-score + sigma
        z, ctx_mean, ctx_sigma = UltimateMath.z_score(context_window, current_price)
        st["context_mean"] = ctx_mean
        st["context_sigma"] = ctx_sigma

        _, sigma_short = UltimateMath.get_sigma(prices[-SIGMA_WINDOW:])

        # sigma_avg profili
        if st["sigma_avg"] == 0.0:
            st["sigma_avg"] = sigma_short
        else:
            st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short

        if st["sigma_avg"] < LOW_SIGMA_TH:
            return

        abs_z = abs(z)
        if st["z_peak"] == 0.0:
            st["z_peak"] = abs_z
        else:
            st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        dyn_z_th = 0.7 * st["z_peak"] + 0.3 * BASE_Z_SCORE_THRESHOLD
        dyn_z_th = max(1.8, min(4.0, dyn_z_th))

        self.z_history[sym_id].append(z)
        over_th_count = sum(1 for zz in self.z_history[sym_id] if zz >= dyn_z_th)
        under_th_count = sum(1 for zz in self.z_history[sym_id] if zz <= -dyn_z_th)

        # VWAP
        vwap_prices = prices[-VWAP_WINDOW:]
        vwap_vols = volumes[-VWAP_WINDOW:]
        vwap = UltimateMath.rolling_vwap(vwap_prices, vwap_vols)
        vwap_dev_pct = (current_price - vwap) / max(vwap, 1e-12) * 100

        # EMA kinematics
        smooth = UltimateMath.ema(prices)
        v, a = UltimateMath.get_kinematics(smooth)

        # Trend filtresi
        alpha_fast = 2.0 / (60 + 1)
        alpha_slow = 2.0 / (180 + 1)
        ema_fast = UltimateMath.ema(context_window, alpha=alpha_fast)[-1]
        ema_slow = UltimateMath.ema(context_window, alpha=alpha_slow)[-1]

        if ema_fast > ema_slow * 1.001:
            trend = "UP"
        elif ema_fast < ema_slow * 0.999:
            trend = "DOWN"
        else:
            trend = "RANGE"

        st["last_z"] = z
        st["last_trend"] = trend

        # Volatilite faktörleri
        if st["sigma_avg"] > 0:
            vol_ratio = sigma_short / st["sigma_avg"]
        else:
            vol_ratio = 1.0
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))
        vol_regime_mult = 1.3 if st["sigma_avg"] > HIGH_SIGMA_TH else 1.0

        # Nadaraya-Watson bandı
        nw_out, nw_mae, nw_upper, nw_lower = UltimateMath.nadaraya_watson(
            context_window, h=8.0, mult=3.0, max_len=200
        )
        nw_extreme_short = current_price > nw_upper
        nw_extreme_long = current_price < nw_lower

        mode = st["mode"]

        # ==========================
        # SHORT ENTRY (PUMP)
        # ==========================
        if mode == "IDLE":
            if (
                trend == "UP"
                and over_th_count >= BT_Z_DEBOUNCE_COUNT
                and z >= dyn_z_th
                and vwap_dev_pct >= VWAP_STRETCH_THRESHOLD * 0.95
            ):
                st["mode"] = "WATCHING_SHORT"
                st["apex"] = current_price
                st["locked_sigma"] = sigma_short
                st["nadir"] = float(context_window.min())

        elif mode == "WATCHING_SHORT":
            if current_price > st["apex"]:
                st["apex"] = current_price

            pump_move = st["apex"] - st["nadir"]
            pump_pct = (pump_move / max(st["nadir"], 1e-12)) * 100

            base_rev_req = UltimateMath.get_dynamic_reversal_threshold(pump_pct)
            curve_broken, curvature, slope, cur_slope = UltimateMath.poly_curve_signal(
                prices, side="SHORT"
            )

            ENTRY_STRICTNESS_LOCAL = ENTRY_STRICTNESS * vol_factor * vol_regime_mult
            final_rev_req = base_rev_req * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_rev_req *= ENTRY_STRICTNESS_LOCAL
            final_rev_req *= BT_REV_REQ_MULT  # backtest: biraz gevşet

            drop_pct = (st["apex"] - current_price) / max(st["apex"], 1e-12) * 100
            drop_sigma = (st["apex"] - current_price) / max(st["locked_sigma"], 1e-6)
            z_back_to_mean = z <= dyn_z_th * Z_EXIT_BAND

            wick_ok = check_wick_from_1s_history(self.ohlc_1s_history[sym_id], side="SHORT")

            if (
                drop_pct >= final_rev_req
                and drop_sigma >= BT_SIGMA_MOVE_STRICT
                and a < 0
                and z_back_to_mean
                and wick_ok
            ):
                stars = score_signal(
                    side="SHORT",
                    z=z,
                    dyn_z_th=dyn_z_th,
                    reversal_sigma=drop_sigma,
                    vwap_dev=vwap_dev_pct,
                    curve_broken=curve_broken,
                    wick_ok=wick_ok,
                    trend=trend,
                    nw_extreme=nw_extreme_short,
                )

                self._on_signal(sym_id, "SHORT", current_price, ts_sec, stars)
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now
                st["last_signal_ts"] = now

        # ==========================
        # LONG ENTRY (DUMP)
        # ==========================
        if mode == "IDLE":
            if (
                trend == "DOWN"
                and under_th_count >= BT_Z_DEBOUNCE_COUNT
                and z <= -dyn_z_th
                and vwap_dev_pct <= -VWAP_STRETCH_THRESHOLD * 0.95
            ):
                st["mode"] = "WATCHING_LONG"
                st["nadir"] = current_price
                st["apex"] = float(context_window.max())
                st["locked_sigma"] = sigma_short

        elif mode == "WATCHING_LONG":
            if current_price < st["nadir"]:
                st["nadir"] = current_price

            dump_move = st["apex"] - st["nadir"]
            dump_pct = (dump_move / max(st["apex"], 1e-12)) * 100

            base_bounce_req = UltimateMath.get_dynamic_reversal_threshold(dump_pct)
            curve_broken, curvature, slope, cur_slope = UltimateMath.poly_curve_signal(
                prices, side="LONG"
            )

            ENTRY_STRICTNESS_LOCAL = ENTRY_STRICTNESS * vol_factor * vol_regime_mult
            final_bounce_req = base_bounce_req * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_bounce_req *= ENTRY_STRICTNESS_LOCAL
            final_bounce_req *= BT_REV_REQ_MULT

            bounce_pct = (current_price - st["nadir"]) / max(st["nadir"], 1e-12) * 100
            bounce_sigma = (current_price - st["nadir"]) / max(st["locked_sigma"], 1e-6)
            z_back_to_mean = z >= -dyn_z_th * Z_EXIT_BAND

            wick_ok = check_wick_from_1s_history(self.ohlc_1s_history[sym_id], side="LONG")

            if (
                bounce_pct >= final_bounce_req
                and bounce_sigma >= BT_SIGMA_MOVE_STRICT
                and a > 0
                and z_back_to_mean
                and wick_ok
            ):
                stars = score_signal(
                    side="LONG",
                    z=z,
                    dyn_z_th=dyn_z_th,
                    reversal_sigma=bounce_sigma,
                    vwap_dev=vwap_dev_pct,
                    curve_broken=curve_broken,
                    wick_ok=wick_ok,
                    trend=trend,
                    nw_extreme=nw_extreme_long,
                )

                self._on_signal(sym_id, "LONG", current_price, ts_sec, stars)
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now
                st["last_signal_ts"] = now

        # Cooldown'dan çıkış: fiyat VWAP civarına geldiğinde
        if (
            st["mode"] == "COOLDOWN"
            and abs(vwap_dev_pct) < 0.5
        ):
            st["mode"] = "IDLE"

        # Pozisyon varsa exit koşullarını kontrol et
        self._check_exit(sym_id, current_price, ts_sec)

    # -----------------------
    # SİNYALDE POZİSYON AÇ
    # -----------------------
    def _on_signal(self, sym_id, side, price, ts_sec, stars):
        pos = self.open_positions.get(sym_id)
        if pos is not None:
            # zaten pozisyon varsa şimdilik yeni açma
            return

        print(
            f"[SIGNAL] {sym_id} {side} "
            f"entry={price:.6f} stars={stars} "
            f"time={datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        self.open_positions[sym_id] = {
            "symbol": sym_id,
            "side": side,
            "entry_price": price,
            "entry_time": ts_sec,
            "stars": stars,
        }

    # -----------------------
    # EXIT KURALI
    # -----------------------
    def _check_exit(self, sym_id, price, ts_sec):
        pos = self.open_positions.get(sym_id)
        if pos is None:
            return

        st = self.state[sym_id]
        z = st["last_z"]
        trend = st["last_trend"]

        exit_reason = None

        # 1) Z-score mean'e döndüyse
        if abs(z) < 0.3:
            exit_reason = "Z_TO_ZERO"

        # 2) Trend tersine döndüyse
        if pos["side"] == "LONG" and trend == "DOWN":
            exit_reason = exit_reason or "TREND_FLIP"
        if pos["side"] == "SHORT" and trend == "UP":
            exit_reason = exit_reason or "TREND_FLIP"

        if exit_reason is None:
            return

        entry_price = pos["entry_price"]
        side = pos["side"]

        if side == "LONG":
            pnl_pct = (price - entry_price) / entry_price * 100.0
        else:
            pnl_pct = (entry_price - price) / entry_price * 100.0

        trade = {
            "symbol": sym_id,
            "side": side,
            "stars": pos["stars"],
            "entry_time": int(pos["entry_time"]),
            "entry_time_str": datetime.fromtimestamp(
                pos["entry_time"], tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": float(entry_price),
            "exit_time": int(ts_sec),
            "exit_time_str": datetime.fromtimestamp(
                ts_sec, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": float(price),
            "pnl_pct": float(pnl_pct),
            "exit_reason": exit_reason,
        }

        self.reporter.on_trade_closed(trade)
        del self.open_positions[sym_id]


# ==========================
# TEK SEMBOL BACKTEST
# ==========================

async def backtest_symbol_seconds(ex, symbol, reporter: LiveReporter):
    data = await fetch_1s_bars_for_symbol(ex, symbol, seconds=SECONDS_HISTORY)
    if data is None:
        return

    secs, opens, highs, lows, closes, vols = data

    if len(secs) < CONTEXT_WINDOW + 10:
        print(f"[{symbol}] Yetersiz 1s bar sayısı, atlanıyor.")
        return

    bt = SecondBacktester(reporter)

    for i in range(len(secs)):
        bt.process_point(
            sym_id=symbol,
            idx=i,
            ts_sec=int(secs[i]),
            o=float(opens[i]),
            h=float(highs[i]),
            l=float(lows[i]),
            c=float(closes[i]),
            vol=float(vols[i]),
        )

    # kalan açık pozisyonu son fiyattan kapat
    if symbol in bt.open_positions:
        pos = bt.open_positions[symbol]
        price = float(closes[-1])
        ts_sec = int(secs[-1])
        side = pos["side"]
        entry_price = pos["entry_price"]

        if side == "LONG":
            pnl_pct = (price - entry_price) / entry_price * 100.0
        else:
            pnl_pct = (entry_price - price) / entry_price * 100.0

        trade = {
            "symbol": symbol,
            "side": side,
            "stars": pos["stars"],
            "entry_time": int(pos["entry_time"]),
            "entry_time_str": datetime.fromtimestamp(
                pos["entry_time"], tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": float(entry_price),
            "exit_time": int(ts_sec),
            "exit_time_str": datetime.fromtimestamp(
                ts_sec, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": float(price),
            "pnl_pct": float(pnl_pct),
            "exit_reason": "FORCE_CLOSE_END",
        }
        reporter.on_trade_closed(trade)


# ==========================
# ANA FONKSİYON
# ==========================

async def main():
    # Binance USDT-M futures (sadece public endpoint, API key gerekmiyor)
    ex = ccxt_async.binanceusdm({
        "enableRateLimit": True,
    })

    reporter = LiveReporter(TRADES_CSV_PATH, SUMMARY_TXT_PATH)

    print("[INFO] Piyasalar yükleniyor...")
    markets = await ex.load_markets()
    print(f"[INFO] Toplam market sayısı: {len(markets)}")

    # Sadece USDT-quoted swap sözleşmeler (USDT-M perpetual)
    all_symbols = [
        s for s, m in markets.items()
        if m.get("quote") == "USDT" and m.get("swap", False)
    ]
    print(f"[INFO] USDT-M sembol sayısı (ham): {len(all_symbols)}")

    # 24h hacimleri tek seferde çek
    print("[INFO] 24h hacim verileri toplu çekiliyor...")
    tickers = await ex.fetch_tickers(all_symbols)

    filtered_symbols = []
    for s in all_symbols:
        t = tickers.get(s, {})
        vol_quote = t.get("quoteVolume") or t.get("baseVolume") or 0
        if vol_quote and vol_quote >= MIN_24H_VOL_USDT:
            filtered_symbols.append(s)

    symbols = sorted(filtered_symbols)[:MAX_SYMBOLS]

    print(f"[INFO] Backtest yapılacak sembol sayısı: {len(symbols)}")
    print(symbols)

    for i, symbol in enumerate(symbols, start=1):
        print(f"\n========== [{i}/{len(symbols)}] {symbol} (1s) backtest başlıyor ==========")
        try:
            await backtest_symbol_seconds(ex, symbol, reporter)
        except Exception as e:
            print(f"[{symbol}] Hata: {e}")

    await ex.close()
    print("\n[INFO] Tüm semboller için second-level backtest tamamlandı.")
    print(f"[INFO] Trades CSV : {TRADES_CSV_PATH}")
    print(f"[INFO] Summary TXT: {SUMMARY_TXT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
