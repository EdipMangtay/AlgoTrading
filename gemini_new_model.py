#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ULTIMATE REVERSAL ENGINE (WebSocket Edition, Meta-Label + Regime Detection)
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
# 1) AYARLAR
# ==============================

# 🔐 TELEGRAM
TG_BOT_TOKEN = "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ"
TG_CHAT_ID   = 1222350744

# 🔐 BINANCE
BINANCE_API_KEY    = "NQAXu0lgpVinMKr5pAiANMFqLunbiZ5eYMZbm6Zbinr83cEfgextjsalzS87YATQ"
BINANCE_API_SECRET = "DHdJl7vTnitQvO8GznnXIa66eqho4FOI58gXZN9kJpuWFiyvsVynmGaWOr5oioNe"

# 1 saniyelik internal bar ayarları
CONTEXT_WINDOW     = 300   # 5 dk
VWAP_WINDOW        = 120   # 2 dk
SIGMA_WINDOW       = 60    # 1 dk
POLY_WINDOW        = 30    # son 30 sn

# 🔧 OPTUNA SONUÇLARI GÖMÜLDÜ
BASE_Z_SCORE_THRESHOLD   = 2.50
VWAP_STRETCH_THRESHOLD   = 1.98

SIGMA_MOVE_STRICT        = 1.99
ENTRY_STRICTNESS         = 1.56

Z_EXIT_BAND              = 0.39
CURVE_ACCELERATOR        = 0.47

MIN_CURVATURE            = 4.9e-05
EMA_ALPHA                = 0.12
MIN_RANGE_PCT            = 0.35

# Hareket büyüklüğüne göre adaptif reversal yüzdeleri
REVERSAL_CONFIRM_PCT_MIN = 0.3
DYNAMIC_THRESHOLDS = [
    (15.0, 0.5),
    (7.0,  0.8),
    (3.0,  1.2),
    (1.5,  1.8),
]

COOLDOWN_SEC = 90
WS_URL = "wss://fstream.binance.com/stream?streams=!miniTicker@arr"


def log(*args):
    print(*args, flush=True)


# ==============================
# 2) MATEMATİK MOTORU
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


# ==============================
# 3) REJİM TESPİTİ (TREND + VOLATILITE)
# ==============================

def detect_regime(sigma_short, sigma_avg, v, a):
    """
    3 rejim:
    - HIGH VOL CHOP
    - TREND (UP/DOWN)
    - NORMAL
    """
    if sigma_avg <= 0:
        return "NORMAL"

    vol_ratio = sigma_short / sigma_avg

    if vol_ratio > 1.8:
        return "HIGH_CHOP"

    if a > 0 and v > 0:
        return "UP_TREND"

    if a < 0 and v < 0:
        return "DOWN_TREND"

    return "NORMAL"


# ==============================
# 4) META-LABEL FİLTRESİ (HL / LH TESTİ)
# ==============================

async def metalabel_confirmation(engine, sym, side, window=6):
    """
    Sinyal sonrası 6 saniyelik mikro test:
    - SHORT için: fiyat en az birkaç tik aşağı (LH) yapmalı
    - LONG için: fiyat en az birkaç tik yukarı (HL) yapmalı
    engine.price_buffers[sym] canlı olarak güncelleniyor.
    """
    if sym not in engine.price_buffers or len(engine.price_buffers[sym]) == 0:
        return False

    base = float(engine.price_buffers[sym][-1])

    for _ in range(window):
        await asyncio.sleep(1.0)

        if sym not in engine.price_buffers or len(engine.price_buffers[sym]) == 0:
            continue

        cur = float(engine.price_buffers[sym][-1])

        if side == "SHORT":
            # aşağı yönlü mikro teyit
            if cur < base:
                return True

        elif side == "LONG":
            # yukarı yönlü mikro teyit
            if cur > base:
                return True

    return False


# ==============================
# 5) WEBSOCKET + ENGINE
# ==============================

class UltimateReversalWS:
    def __init__(self):
        log("[INIT] Ultimate Reversal Engine (Meta-Label + Regime) başlatılıyor...")
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

        # sinyal state
        self.state = {}

    async def load_futures_list(self):
        """USDT-M futures sembollerini çek."""
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
            }

        log(f"[MARKET] {len(self.valid_ids)} sembol bulundu.")

    # ==============================
    # WEBSOCKET: !miniTicker@arr
    # ==============================
    async def websocket_loop(self):
        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=20
                ) as ws:
                    log("[WS] Bağlandı.")
                    async for msg in ws:
                        data = json.loads(msg).get("data")
                        if not data:
                            continue

                        for item in data:
                            sym = item["s"]
                            if sym not in self.valid_ids:
                                continue

                            price = float(item["c"])
                            vol24 = float(item["v"])

                            self.latest_price[sym] = price
                            self.latest_vol24[sym] = vol24

            except Exception as e:
                log("[WS ERR]", e)
                await asyncio.sleep(5)

    # ==============================
    # AGGREGATION LOOP (1 saniye)
    # ==============================
    async def aggregation_loop(self):
        while True:
            start = time.time()
            count = 0

            for sym in list(self.valid_ids):
                price = self.latest_price.get(sym)
                if price is None:
                    continue

                # volume delta
                vol24 = self.latest_vol24.get(sym, 0.0)
                prev = self.last_vol24_snapshot.get(sym, vol24)
                self.last_vol24_snapshot[sym] = vol24
                delta_vol = max(vol24 - prev, 0.0)

                # append
                self.price_buffers[sym].append(price)
                self.volume_buffers[sym].append(delta_vol)

                if len(self.price_buffers[sym]) >= CONTEXT_WINDOW:
                    count += 1
                    await self.process_series(
                        sym,
                        np.array(self.price_buffers[sym]),
                        np.array(self.volume_buffers[sym])
                    )

            log(f"[AGG] {count} sembol işlendi.")
            elapsed = time.time() - start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    # ==============================
    # ANA SİNYAL MOTORU
    # ==============================
    async def process_series(self, sym, prices, vols):
        st = self.state[sym]
        now = time.time()

        if now < st["cooldown_until"]:
            return

        price = float(prices[-1])

        # -----------------------------
        # ÖLÜ PİYASA FİLTRESİ
        # -----------------------------
        tail = prices[-SIGMA_WINDOW:]
        rng = (tail.max() - tail.min()) / price * 100
        if rng < MIN_RANGE_PCT:
            return

        # -----------------------------
        # Z + Sigma bağlamı
        # -----------------------------
        ctx = prices[-CONTEXT_WINDOW:]
        z, mean, sigma_ctx = UltimateMath.z_score(ctx, price)

        st["context_mean"] = mean
        st["context_sigma"] = sigma_ctx

        _, sigma_short = UltimateMath.get_sigma(prices[-SIGMA_WINDOW:])

        # sigma_avg güncelle
        if st["sigma_avg"] == 0:
            st["sigma_avg"] = sigma_short
        else:
            st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short

        # z_peak
        abs_z = abs(z)
        if st["z_peak"] == 0:
            st["z_peak"] = abs_z
        else:
            st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        # -----------------------------
        # REJİM TESPİTİ (UP / DOWN / CHOP)
        # -----------------------------
        sm = UltimateMath.ema(prices)
        v, a = UltimateMath.get_kinematics(sm)
        regime = detect_regime(sigma_short, st["sigma_avg"], v, a)

        # Rejime göre strictness ayarı
        entry_strict = ENTRY_STRICTNESS
        sig_strict = SIGMA_MOVE_STRICT

        if regime == "HIGH_CHOP":  # çok gürültülü
            entry_strict *= 1.25
            sig_strict *= 1.25

        elif regime == "UP_TREND" and a > 0:
            # LONG için gevşet
            entry_strict *= 0.85

        elif regime == "DOWN_TREND" and a < 0:
            # SHORT için gevşet
            entry_strict *= 0.85

        # -----------------------------
        # VWAP – eğri – z
        # -----------------------------
        vw_prices = prices[-VWAP_WINDOW:]
        vw_vols = vols[-VWAP_WINDOW:]
        vwap = UltimateMath.rolling_vwap(vw_prices, vw_vols)
        vwap_dev = (price - vwap) / vwap * 100

        mode = st["mode"]

        # volatility factor
        vol_ratio = sigma_short / max(st["sigma_avg"], 1e-8)
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        # ================
        # 1) SHORT START
        # ================
        dyn_z = 0.7 * st["z_peak"] + 0.3 * BASE_Z_SCORE_THRESHOLD
        dyn_z = max(1.8, min(4.0, dyn_z))

        if mode == "IDLE" and z >= dyn_z and vwap_dev >= VWAP_STRETCH_THRESHOLD:
            st["mode"] = "WATCHING_SHORT"
            st["apex"] = price
            st["locked_sigma"] = sigma_short
            st["nadir"] = float(ctx.min())

        # ================
        # 1.b SHORT CONFIRM
        # ================
        elif mode == "WATCHING_SHORT":

            if price > st["apex"]:
                st["apex"] = price

            pump_move = st["apex"] - st["nadir"]
            pump_pct = (pump_move / st["nadir"]) * 100 if st["nadir"] > 0 else 0

            base_req = UltimateMath.get_dynamic_reversal_threshold(pump_pct)

            curve_ok, curv, sl, curr_sl = UltimateMath.poly_curve_signal(
                prices, "SHORT"
            )

            final_req = base_req * (CURVE_ACCELERATOR if curve_ok else 1.0)
            final_req *= entry_strict * vol_factor

            drop_pct = (st["apex"] - price) / st["apex"] * 100
            drop_sigma = (st["apex"] - price) / max(st["locked_sigma"], 1e-6)

            z_mean_ok = z <= dyn_z * Z_EXIT_BAND

            # -------------------------------
            # META-LABEL TEST + SİNYAL
            # -------------------------------
            if (
                drop_pct >= final_req and
                drop_sigma >= sig_strict and
                a < 0 and
                z_mean_ok
            ):
                ok = await metalabel_confirmation(self, sym, "SHORT")
                if ok:
                    await self.send_signal(
                        sym, "SHORT", price,
                        z, vwap_dev,
                        pump_pct, drop_pct, drop_sigma,
                        curve_ok, curv, curr_sl,
                        mean, sigma_ctx, sigma_short
                    )
                    st["mode"] = "COOLDOWN"
                    st["cooldown_until"] = now + COOLDOWN_SEC
                else:
                    st["mode"] = "IDLE"  # test başarısız → sinyal iptal

        # ================
        # 2) LONG START
        # ================
        if mode == "IDLE" and z <= -dyn_z and vwap_dev <= -VWAP_STRETCH_THRESHOLD:
            st["mode"] = "WATCHING_LONG"
            st["nadir"] = price
            st["apex"] = float(ctx.max())
            st["locked_sigma"] = sigma_short

        # ================
        # 2.b LONG CONFIRM
        # ================
        elif mode == "WATCHING_LONG":

            if price < st["nadir"]:
                st["nadir"] = price

            dump_move = st["apex"] - st["nadir"]
            dump_pct = (dump_move / st["apex"]) * 100 if st["apex"] > 0 else 0

            base_req = UltimateMath.get_dynamic_reversal_threshold(dump_pct)

            curve_ok, curv, sl, curr_sl = UltimateMath.poly_curve_signal(
                prices, "LONG"
            )

            final_req = base_req * (CURVE_ACCELERATOR if curve_ok else 1.0)
            final_req *= entry_strict * vol_factor

            bounce_pct = (price - st["nadir"]) / st["nadir"] * 100
            bounce_sigma = (price - st["nadir"]) / max(st["locked_sigma"], 1e-6)

            z_mean_ok = z >= -dyn_z * Z_EXIT_BAND

            # -------------------------------
            # META-LABEL TEST + SİNYAL
            # -------------------------------
            if (
                bounce_pct >= final_req and
                bounce_sigma >= sig_strict and
                a > 0 and
                z_mean_ok
            ):
                ok = await metalabel_confirmation(self, sym, "LONG")
                if ok:
                    await self.send_signal(
                        sym, "LONG", price,
                        z, vwap_dev,
                        dump_pct, bounce_pct, bounce_sigma,
                        curve_ok, curv, curr_sl,
                        mean, sigma_ctx, sigma_short
                    )
                    st["mode"] = "COOLDOWN"
                    st["cooldown_until"] = now + COOLDOWN_SEC
                else:
                    st["mode"] = "IDLE"

        # ====================
        # COOLDOWN'DAN ÇIKIŞ
        # ====================
        if (
            st["mode"] == "COOLDOWN" and
            now >= st["cooldown_until"] and
            abs(vwap_dev) < 0.5
        ):
            st["mode"] = "IDLE"

    # ==============================
    # 6) SİNYAL GÖNDERME
    # ==============================
    async def send_signal(
        self, sym, side, price,
        z, vwap_dev,
        move_pct, rev_pct, rev_sigma,
        curve_ok, curvature, curr_sl,
        mean, sigma_ctx, sigma_short
    ):
        if not self.bot:
            return

        star = "⭐" if abs(z) < 2.5 else "⭐⭐" if abs(z) < 3.5 else "⭐⭐⭐"

        text = (
            f"📌 <b>M1 Hammer</b>\n"
            f"{'🔴 SHORT' if side=='SHORT' else '🟢 LONG'} {star}\n"
            f"• Coin: <b>#{sym}</b>\n"
            f"• Fiyat: <b>{price:.6f}</b>\n"
            f"• Z-score: <b>{z:.2f}</b>\n"
            f"• VWAP Sapma: <b>{vwap_dev:.2f}%</b>\n"
            f"• Hareket %: <b>{move_pct:.2f}%</b>\n"
            f"• Dönüş %: <b>{rev_pct:.2f}%</b>\n"
            f"• Sigma Move: <b>{rev_sigma:.2f}</b>\n"
            f"• Eğri Hızlandırıcı (Poly): <b>{'EVET 🏎️' if curve_ok else 'Hayır'}</b>\n"
            f"• Curvature: <b>{curvature:.5f}</b>\n"
            f"• Son slope: <b>{curr_sl:.5f}</b>\n"
            f"• Ort. Mean: <b>{mean:.6f}</b>\n"
            f"• Sigma Context: <b>{sigma_ctx:.6f}</b>\n"
            f"• Sigma Short: <b>{sigma_short:.6f}</b>"
        )

        try:
            await self.bot.send_message(
                TG_CHAT_ID, text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            log(f"[SIGNAL] {sym} {side} gönderildi.")
        except Exception as e:
            log("[TG ERR]", e)

    # ==============================
    # 7) RUN
    # ==============================
    async def run(self):
        await self.load_futures_list()
        await asyncio.gather(
            self.websocket_loop(),
            self.aggregation_loop()
        )


# ==============================
# 9) MAIN
# ==============================
if __name__ == "__main__":
    engine = UltimateReversalWS()
    asyncio.run(engine.run())
