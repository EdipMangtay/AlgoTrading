#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ULTIMATE REVERSAL ENGINE v2 (Advanced Math Edition)
---------------------------------------------------
Özellikler:
- VWAP + Z-Score (Ana Omurga)
- Kalman Filtresi (Gürültü temizleme & Hızlı Fiyat)
- RSI (Momentum Teyidi)
- Hurst Exponent (Trend/Mean Reversion Rejim Filtresi)
- Hacim Z-Score (Fake hareket koruması)
"""

import asyncio
import json
import time
from collections import defaultdict, deque

import numpy as np
import ccxt.async_support as ccxt_async
from telegram import Bot
import websockets

# ==============================
# 1) AYARLAR (Optuna Sonuçları GÖMÜLDÜ)
# ==============================

# 🔐 TELEGRAM
TG_BOT_TOKEN = "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ"
TG_CHAT_ID   = 1222350744

# 🔐 BINANCE
BINANCE_API_KEY    = "NQAXu0lgpVinMKr5pAiANMFqLunbiZ5eYMZbm6Zbinr83cEfgextjsalzS87YATQ"
BINANCE_API_SECRET = "DHdJl7vTnitQvO8GznnXIa66eqho4FOI58gXZN9kJpuWFiyvsVynmGaWOr5oioNe"

# Pencereler
CONTEXT_WINDOW     = 300   # 5 dk
VWAP_WINDOW        = 120   # 2 dk
SIGMA_WINDOW       = 60    # 1 dk
POLY_WINDOW        = 30    # son 30 sn

# 🔧 TEMEL PARAMETRELER (Optuna’dan gelenler)
BASE_Z_SCORE_THRESHOLD   = 2.791587479580907      # BASE_Z
VWAP_STRETCH_THRESHOLD   = 1.381176029649929      # VWAP_STR

SIGMA_MOVE_STRICT        = 2.656513515181195      # SIGMA_STR
ENTRY_STRICTNESS         = 2.308280296901772      # ENTRY_STR

Z_EXIT_BAND              = 0.4224101144101774     # Z_EXIT
CURVE_ACCELERATOR        = 1.3568283979305238     # CURV_ACC

MIN_CURVATURE            = 1.4401219014166845e-05 # MIN_CURV
EMA_ALPHA                = 0.2823625114062088     # EMA_AL
MIN_RANGE_PCT            = 1.3248439814703656     # MIN_RNG

REVERSAL_CONFIRM_PCT_MIN = 0.6352632135729053     # REV_MIN

COOLDOWN_SEC       = 60

WS_URL = "wss://fstream.binance.com/stream?streams=!miniTicker@arr"

# -----------------
# v2 GELİŞMİŞ FİLTRELER (Optuna bunları optimize etti)
# -----------------
RSI_PERIOD          = 14
RSI_OVERBOUGHT      = 77.04377791188023          # RSI_OB
RSI_OVERSOLD        = 29.220580349604695         # RSI_OS

HURST_WINDOW        = 120
HURST_TREND_LIMIT   = 0.5379695549418683         # HURST_LIM (0.5 altı mean reversion, üstü trend)

KALMAN_WINDOW       = 120
VOL_Z_THRESHOLD     = 0.43233344191657264        # VOL_Z_TH

# Dinamik reversal eşikleri (Optuna DT1–DT4)
DT1 = 0.6374820726832433
DT2 = 0.6745895678529111
DT3 = 1.5318824242235567
DT4 = 1.8950224253668406

DYNAMIC_THRESHOLDS = [
    (15.0, DT1),
    (7.0,  DT2),
    (3.0,  DT3),
    (1.5,  DT4),
]


def log(*args):
    print(*args, flush=True)


# ==============================
# 2) MATEMATİK MOTORU (ADVANCED)
# ==============================

class UltimateMath:
    @staticmethod
    def ema(prices, alpha=EMA_ALPHA):
        prices = np.array(prices, dtype=float)
        ema = np.zeros_like(prices)
        ema[0] = prices[0]
        for i in range(1, len(prices)):
            ema[i] = alpha * prices[i] + (1 - alpha) * ema[i-1]
        return ema

    @staticmethod
    def get_kinematics(prices):
        prices = np.array(prices, dtype=float)
        v = np.diff(prices)
        a = np.diff(v)
        v_curr = float(v[-1]) if len(v) > 0 else 0.0
        a_curr = float(a[-1]) if len(a) > 0 else 0.0
        return v_curr, a_curr

    @staticmethod
    def rolling_vwap(prices, volumes):
        p = np.array(prices, dtype=float)
        v = np.array(volumes, dtype=float)
        pv = np.sum(p * v)
        vv = np.sum(v)
        if vv == 0:
            return float(p[-1])
        return float(pv / vv)

    @staticmethod
    def z_score(window_prices, current_price):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std  = float(arr.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        z = (current_price - mean) / std
        return z, mean, std

    @staticmethod
    def get_sigma(window_prices):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std  = float(arr.std())
        if std == 0:
            std = max(abs(mean) * 1e-4, 1e-6)
        return mean, std

    @staticmethod
    def get_dynamic_reversal_threshold(move_pct):
        abs_move = abs(move_pct)
        for limit, thr in DYNAMIC_THRESHOLDS:
            if abs_move >= limit:
                return max(thr, REVERSAL_CONFIRM_PCT_MIN)
        return 999.0

    @staticmethod
    def poly_curve_signal(prices_tail, side):
        if len(prices_tail) < POLY_WINDOW:
            return False, 0.0, 0.0, 0.0

        y = np.array(prices_tail[-POLY_WINDOW:], dtype=float)
        x = np.arange(len(y))
        try:
            coeffs = np.polyfit(x, y, 2)
        except Exception:
            return False, 0.0, 0.0, 0.0

        a, b, c = coeffs
        curvature = a
        slope = b
        current_slope = 2 * a * (len(x) - 1) + b

        if side == "SHORT":
            broken = (curvature < -MIN_CURVATURE) and (current_slope < 0)
        else:
            broken = (curvature > MIN_CURVATURE) and (current_slope > 0)

        return broken, curvature, slope, current_slope


class AdvancedMath:
    @staticmethod
    def calculate_rsi(prices, period=RSI_PERIOD):
        """Numpy ile hızlı RSI."""
        prices = np.array(prices, dtype=float)
        if len(prices) < period + 1:
            return 50.0

        deltas = np.diff(prices)
        seed = deltas[:period+1]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        if down == 0:
            return 100.0
        rs = up / down
        rsi = 100 - (100 / (1 + rs))

        for i in range(period + 1, len(deltas)):
            delta = deltas[i]
            u = delta if delta > 0 else 0
            d = -delta if delta < 0 else 0
            up = (up * (period - 1) + u) / period
            down = (down * (period - 1) + d) / period
            if down == 0:
                return 100.0
            rs = up / down
            rsi = 100 - (100 / (1 + rs))

        return float(rsi)

    @staticmethod
    def hurst_exponent(time_series):
        """Hurst Exponent: 0.5=Random, <0.5=Mean Reverting, >0.5=Trend."""
        try:
            ts = np.array(time_series, dtype=float)
            if len(ts) < 20:
                return 0.5
            lags = range(2, 20)
            tau = [np.sqrt(np.std(np.subtract(ts[lag:], ts[:-lag]))) for lag in lags]
            poly = np.polyfit(np.log(lags), np.log(tau), 1)
            return float(poly[0] * 2.0)
        except:
            return 0.5

    @staticmethod
    def kalman_filter(prices, process_variance=1e-5, measurement_variance=0.1):
        """1D Kalman Filtresi (Gürültü Azaltıcı)."""
        prices = np.array(prices, dtype=float)
        n_iter = len(prices)
        if n_iter == 0:
            return 0.0

        xhat = np.zeros(n_iter)
        P = np.zeros(n_iter)
        xhatminus = np.zeros(n_iter)
        Pminus = np.zeros(n_iter)
        K = np.zeros(n_iter)

        xhat[0] = prices[0]
        P[0] = 1.0

        for k in range(1, n_iter):
            xhatminus[k] = xhat[k-1]
            Pminus[k] = P[k-1] + process_variance
            K[k] = Pminus[k] / (Pminus[k] + measurement_variance)
            xhat[k] = xhatminus[k] + K[k] * (prices[k] - xhatminus[k])
            P[k] = (1 - K[k]) * Pminus[k]

        return float(xhat[-1])


# ==============================
# 3) WEBSOCKET + ENGINE
# ==============================

class UltimateReversalWS:
    def __init__(self):
        log("[INIT] Ultimate Reversal v2 (Advanced) başlatılıyor...")
        self.ex = ccxt_async.binanceusdm({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
        })
        self.bot = Bot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

        self.valid_ids = set()
        self.latest_price = {}
        self.latest_vol24 = {}
        self.last_vol24_snapshot = {}

        self.price_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
        self.volume_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))

        # state
        self.state = {}

    async def load_futures_list(self):
        markets = await self.ex.load_markets()
        for m in markets.values():
            if m.get("quote") == "USDT" and m.get("active", True):
                self.valid_ids.add(m["id"])

        for sym in self.valid_ids:
            self.state[sym] = {
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
                "last_rsi": 50.0,   # v2
                "last_hurst": 0.5,  # v2
            }
        log(f"[MARKETS] {len(self.valid_ids)} sembol izleniyor.")

    async def websocket_loop(self):
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    log("[WS] Bağlandı.")
                    async for message in ws:
                        msg = json.loads(message)
                        data = msg.get("data")
                        if not data:
                            continue
                        for item in data:
                            sym_id = item.get("s")
                            if sym_id not in self.valid_ids:
                                continue
                            self.latest_price[sym_id] = float(item["c"])
                            self.latest_vol24[sym_id] = float(item["v"])
            except Exception as e:
                log("[WS ERR]", e)
                await asyncio.sleep(5)

    async def aggregation_loop(self):
        while True:
            start = time.time()
            active_count = 0
            for sym_id in list(self.valid_ids):
                price = self.latest_price.get(sym_id)
                if price is None:
                    continue

                vol24 = self.latest_vol24.get(sym_id, 0.0)
                prev24 = self.last_vol24_snapshot.get(sym_id, vol24)
                delta_vol = max(vol24 - prev24, 0.0)
                self.last_vol24_snapshot[sym_id] = vol24

                pb = self.price_buffers[sym_id]
                vb = self.volume_buffers[sym_id]
                pb.append(price)
                vb.append(delta_vol)

                if len(pb) >= CONTEXT_WINDOW:
                    active_count += 1
                    await self.process_series(sym_id, np.array(pb), np.array(vb))

            elapsed = time.time() - start
            log(f"[AGG] {active_count} analiz edildi.")
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    async def process_series(self, sym_id, prices, volumes):
        st = self.state[sym_id]
        now = time.time()

        if now < st["cooldown_until"]:
            return

        current_price = float(prices[-1])

        # --- v2: KALMAN FILTER (Signal Price) ---
        kalman_window = min(len(prices), KALMAN_WINDOW)
        kalman_price = AdvancedMath.kalman_filter(prices[-kalman_window:])
        signal_price = kalman_price if kalman_window >= 5 else current_price

        # Ölü Piyasa Filtresi
        tail = prices[-SIGMA_WINDOW:]
        rng = (tail.max() - tail.min()) / current_price * 100
        if rng < MIN_RANGE_PCT:
            return

        # Bağlam
        context_window = prices[-CONTEXT_WINDOW:]
        z, ctx_mean, ctx_sigma = UltimateMath.z_score(context_window, signal_price)
        st["context_mean"] = ctx_mean
        st["context_sigma"] = ctx_sigma

        _, sigma_short = UltimateMath.get_sigma(prices[-SIGMA_WINDOW:])

        if st["sigma_avg"] == 0.0:
            st["sigma_avg"] = sigma_short
        else:
            st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short

        abs_z = abs(z)
        if st["z_peak"] == 0.0:
            st["z_peak"] = abs_z
        else:
            st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        dyn_z_th = 0.7 * st["z_peak"] + 0.3 * BASE_Z_SCORE_THRESHOLD
        dyn_z_th = max(1.8, min(4.0, dyn_z_th))

        vwap_prices = prices[-VWAP_WINDOW:]
        vwap_vols   = volumes[-VWAP_WINDOW:]
        vwap = UltimateMath.rolling_vwap(vwap_prices, vwap_vols)

        smooth = UltimateMath.ema(prices)
        v, a = UltimateMath.get_kinematics(smooth)
        vwap_dev_pct = (signal_price - vwap) / vwap * 100

        mode = st["mode"]
        if st["sigma_avg"] > 0:
            vol_ratio = sigma_short / st["sigma_avg"]
        else:
            vol_ratio = 1.0
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        # --- v2: RSI & HURST & VOL_Z ---
        prev_rsi = st.get("last_rsi", 50.0)
        rsi_val = AdvancedMath.calculate_rsi(context_window, period=RSI_PERIOD)
        st["last_rsi"] = rsi_val

        hurst_win = min(len(context_window), HURST_WINDOW)
        hurst_val = AdvancedMath.hurst_exponent(context_window[-hurst_win:])
        st["last_hurst"] = hurst_val

        vol_tail = volumes[-SIGMA_WINDOW:]
        vol_mean = float(vol_tail.mean())
        vol_std = float(vol_tail.std())
        vol_z = (float(vol_tail[-1]) - vol_mean) / (vol_std + 1e-9)

        # FİLTRELER
        mean_revert_ok = (hurst_val <= HURST_TREND_LIMIT)

        rsi_short_ready = (
            rsi_val >= RSI_OVERBOUGHT or
            (prev_rsi >= RSI_OVERBOUGHT and rsi_val < prev_rsi)
        )
        rsi_long_ready  = (
            rsi_val <= RSI_OVERSOLD or
            (prev_rsi <= RSI_OVERSOLD and rsi_val > prev_rsi)
        )

        # --- SHORT ---
        if mode == "IDLE":
            if (
                mean_revert_ok and
                rsi_short_ready and
                z >= dyn_z_th and
                vwap_dev_pct >= VWAP_STRETCH_THRESHOLD
            ):
                st["mode"] = "WATCHING_SHORT"
                st["apex"] = current_price
                st["locked_sigma"] = sigma_short
                st["nadir"] = float(context_window.min())

        elif mode == "WATCHING_SHORT":
            if current_price > st["apex"]:
                st["apex"] = current_price

            pump_move = st["apex"] - st["nadir"]
            pump_pct = (pump_move / st["nadir"] * 100) if st["nadir"] > 0 else 0.0

            base_rev = UltimateMath.get_dynamic_reversal_threshold(pump_pct)
            curve_broken, curv, slope, cur_slope = UltimateMath.poly_curve_signal(prices, "SHORT")

            final_rev = (
                base_rev *
                (CURVE_ACCELERATOR if curve_broken else 1.0) *
                ENTRY_STRICTNESS *
                vol_factor
            )

            drop_pct = (st["apex"] - signal_price) / st["apex"] * 100
            drop_sigma = (st["apex"] - signal_price) / max(st["locked_sigma"], 1e-6)

            z_back = abs(z) <= dyn_z_th * Z_EXIT_BAND
            rsi_conf = rsi_val < prev_rsi
            vol_conf = vol_z >= VOL_Z_THRESHOLD

            if (
                drop_pct >= final_rev and
                drop_sigma >= SIGMA_MOVE_STRICT and
                a < 0 and
                z_back and
                rsi_conf and
                vol_conf
            ):
                await self.send_signal(
                    sym_id, "SHORT", current_price, z, vwap_dev_pct,
                    pump_pct, drop_pct, drop_sigma,
                    curve_broken, curv, cur_slope,
                    rsi_val, hurst_val
                )
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now + COOLDOWN_SEC

        # --- LONG ---
        if mode == "IDLE":
            if (
                mean_revert_ok and
                rsi_long_ready and
                z <= -dyn_z_th and
                vwap_dev_pct <= -VWAP_STRETCH_THRESHOLD
            ):
                st["mode"] = "WATCHING_LONG"
                st["nadir"] = current_price
                st["apex"] = float(context_window.max())
                st["locked_sigma"] = sigma_short

        elif mode == "WATCHING_LONG":
            if current_price < st["nadir"]:
                st["nadir"] = current_price

            dump_move = st["apex"] - st["nadir"]
            dump_pct = (dump_move / st["apex"] * 100) if st["apex"] > 0 else 0.0

            base_rev = UltimateMath.get_dynamic_reversal_threshold(dump_pct)
            curve_broken, curv, slope, cur_slope = UltimateMath.poly_curve_signal(prices, "LONG")

            final_rev = (
                base_rev *
                (CURVE_ACCELERATOR if curve_broken else 1.0) *
                ENTRY_STRICTNESS *
                vol_factor
            )

            bounce_pct = (signal_price - st["nadir"]) / st["nadir"] * 100
            bounce_sigma = (signal_price - st["nadir"]) / max(st["locked_sigma"], 1e-6)

            z_back = abs(z) <= dyn_z_th * Z_EXIT_BAND
            rsi_conf = rsi_val > prev_rsi
            vol_conf = vol_z >= VOL_Z_THRESHOLD

            if (
                bounce_pct >= final_rev and
                bounce_sigma >= SIGMA_MOVE_STRICT and
                a > 0 and
                z_back and
                rsi_conf and
                vol_conf
            ):
                await self.send_signal(
                    sym_id, "LONG", current_price, z, vwap_dev_pct,
                    dump_pct, bounce_pct, bounce_sigma,
                    curve_broken, curv, cur_slope,
                    rsi_val, hurst_val
                )
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now + COOLDOWN_SEC

        # Cooldown reset
        if st["mode"] == "COOLDOWN" and now >= st["cooldown_until"] and abs(vwap_dev_pct) < 0.5:
            st["mode"] = "IDLE"

    async def send_signal(self, sym, side, price, z, vwap, move, rev, sigma, curv_ok, curv, slope, rsi, hurst):
        emoji = "🔴 SHORT" if side == "SHORT" else "🟢 LONG"
        accel = "EVET 🏎" if curv_ok else "HAYIR"

        msg = (
            f"<b>{emoji} | {sym}</b>\n"
            f"Fiyat: <code>{price:.6f}</code>\n"
            f"🧠 <b>Math:</b>\n"
            f"• Z-Score: <code>{z:.2f}</code>\n"
            f"• VWAP Dev: <b>%{vwap:.2f}</b>\n"
            f"• RSI: <code>{rsi:.1f}</code> | Hurst: <code>{hurst:.2f}</code>\n\n"
            f"📈 <b>Hareket:</b>\n"
            f"• Ana Move: <b>%{move:.2f}</b>\n"
            f"• Reversal: <b>%{rev:.2f}</b> ({sigma:.2f}σ)\n"
            f"• Poly Accel: <b>{accel}</b>\n"
        )
        if self.bot:
            try:
                await self.bot.send_message(TG_CHAT_ID, msg, parse_mode="HTML")
            except:
                pass
        log(f"SİNYAL: {sym} {side} | Rev: {rev:.2f}% | RSI: {rsi:.1f}")

    async def run(self):
        await self.load_futures_list()
        await asyncio.gather(self.websocket_loop(), self.aggregation_loop())


if __name__ == "__main__":
    engine = UltimateReversalWS()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        log("Durduruldu.")
