#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ULTIMATE REVERSAL ENGINE (WebSocket Edition, Dynamic Per-Coin, Enhanced)
------------------------------------------------------------------------
- Veri: Binance USDT-M Futures WebSocket (!miniTicker@arr)
- Zaman çözünürlüğü: 1s custom close serisi + 1s OHLC mumları
- Strateji: VWAP + Z-Score + Sigma + Polynomial Curve + EMA Kinematics
- SHORT: Pump sonrası tepeden dönüş
- LONG : Dump sonrası dipten tepki

Ek Özellikler:
- Her coin için ayrı:
  - Z-score eşiği (z_peak'e göre adaptif)
  - Volatilite profili (sigma_avg) → reversal yüzdesi buna göre sertleşir/yumuşar
- Likidite filtresi (24h volume)
- Trend filtresi (context EMA fast / EMA slow)
- Z-score debounce (en az birkaç saniye eşik üstü/altı)
- Wick teyidi (3s pseudo mum, üst/alt fitil yapısı)
"""

import asyncio
import json
import os
import time
from collections import defaultdict, deque

import numpy as np
import ccxt.async_support as ccxt_async
from telegram import Bot
import websockets

# ==============================
# 1) AYARLAR
# ==============================

# 🔐 TELEGRAM (ENV VAR)
TG_BOT_TOKEN = "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ"
TG_CHAT_ID   = 1222350744

BINANCE_API_KEY    = "NQAXu0lgpVinMKr5pAiANMFqLunbiZ5eYMZbm6Zbinr83cEfgextjsalzS87YATQ"
BINANCE_API_SECRET = "DHdJl7vTnitQvO8GznnXIa66eqho4FOI58gXZN9kJpuWFiyvsVynmGaWOr5oioNe"
# 1 saniyelik internal bar ayarları
CONTEXT_WINDOW     = 300   # 5 dk = 300 sn
VWAP_WINDOW        = 120   # 2 dk
SIGMA_WINDOW       = 60    # 1 dk
POLY_WINDOW        = 30    # son 30 sn

# 🔧 OPTUNA SONUÇLARI GÖMÜLDÜ
BASE_Z_SCORE_THRESHOLD   = 2.506125147502857
VWAP_STRETCH_THRESHOLD   = 1.9855807644923995

SIGMA_MOVE_STRICT        = 1.9940935349673694
ENTRY_STRICTNESS         = 1.5640220883287945

Z_EXIT_BAND              = 0.39694508157160496
CURVE_ACCELERATOR        = 0.4785662272373655

MIN_CURVATURE            = 4.9376511621030375e-05
EMA_ALPHA                = 0.12065578244594607
MIN_RANGE_PCT            = 0.3574704654934885

# Hareket büyüklüğüne göre adaptif reversal yüzdeleri
REVERSAL_CONFIRM_PCT_MIN = 0.3
DYNAMIC_THRESHOLDS = [
    (15.0, 0.5),
    (7.0,  0.8),
    (3.0,  1.2),
    (1.5,  1.8),
]

COOLDOWN_SEC       = 90      # sinyal sonrası aynı sembolde bekleme
WS_URL             = "wss://fstream.binance.com/stream?streams=!miniTicker@arr"

# Likidite filtresi (24h volume - USDT)
MIN_24H_VOL_USDT   = 10_000_000

# Volatilite rejimi eşikleri
LOW_SIGMA_TH       = 0.0005   # çok sakin market → sinyal üretme
HIGH_SIGMA_TH      = 0.02     # aşırı manyak market → daha sıkı giriş

# Z-score debounce ayarları
Z_DEBOUNCE_LEN     = 5        # son 5 saniye
Z_DEBOUNCE_COUNT   = 3        # en az 3 tanesi eşik üstü/altı olmalı


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
        # küçük hareketlerde işlem yapma
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
# 3) WEBSOCKET + ENGINE
# ==============================

class UltimateReversalWS:
    def __init__(self):
        log("[INIT] Ultimate Reversal Engine (WebSocket) başlatılıyor...")
        self.ex = ccxt_async.binanceusdm({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
        })
        self.bot = Bot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

        self.valid_ids = set()      # "BTCUSDT", "ETHUSDT", ...

        self.latest_price = {}      # id -> last price
        self.latest_vol24 = {}      # id -> 24h volume
        self.last_vol24_snapshot = {}  # id -> önceki 24h volume

        self.price_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))
        self.volume_buffers = defaultdict(lambda: deque(maxlen=CONTEXT_WINDOW))

        # state: coin karakteri + sinyal durumu
        # mode, apex, nadir, cooldown, context_mean/sigma, locked_sigma, sigma_avg, z_peak, last_signal_ts
        self.state = {}

        # Z-score debounce için
        self.z_history = defaultdict(lambda: deque(maxlen=Z_DEBOUNCE_LEN))

        # 1s OHLC üretimi için
        self.ohlc_1s_current = {}   # sym_id -> {"ts", "open", "high", "low", "close"}
        self.ohlc_1s_history = defaultdict(lambda: deque(maxlen=60))  # son 60 saniye

    async def load_futures_list(self):
        """Tüm USDT-M futures id’lerini yükle."""
        markets = await self.ex.load_markets()
        for m in markets.values():
            if m.get("quote") == "USDT" and m.get("active", True):
                sym_id = m["id"]  # örn "BTCUSDT"
                self.valid_ids.add(sym_id)

        for sym_id in self.valid_ids:
            self.state[sym_id] = {
                "mode": "IDLE",
                "apex": 0.0,
                "nadir": 0.0,
                "cooldown_until": 0.0,
                "context_mean": 0.0,
                "context_sigma": 0.0,
                "locked_sigma": 0.0,
                "sigma_avg": 0.0,   # coin'in tipik 60sn sigma ortalaması
                "z_peak": 0.0,      # coin'in tipik max |z|
                "last_signal_ts": 0.0,
            }

        log(f"[MARKETS] USDT-M futures sayısı: {len(self.valid_ids)}")

    # ---------- 1s OHLC HELPER ----------

    def _update_ohlc_1s(self, sym_id, price):
        now_sec = int(time.time())
        bar = self.ohlc_1s_current.get(sym_id)

        if bar is None or bar["ts"] != now_sec:
            # eski bar'ı history'e it
            if bar is not None:
                self.ohlc_1s_history[sym_id].append(bar)
            # yeni bar başlat
            self.ohlc_1s_current[sym_id] = {
                "ts": now_sec,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            # aynı saniye içindeyiz, high/low/close güncelle
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price

    def _check_wick(self, sym_id, side, window_sec=3):
        """Son window_sec 1s mumundan pseudo 3s mum üret ve wick teyidi yap."""
        bars = self.ohlc_1s_history[sym_id]
        if len(bars) < window_sec:
            return False

        recent = list(bars)[-window_sec:]
        open_ = recent[0]["open"]
        close_ = recent[-1]["close"]
        high = max(b["high"] for b in recent)
        low = min(b["low"] for b in recent)

        body = abs(close_ - open_)
        range_ = max(high - low, 1e-12)

        upper_wick = high - max(open_, close_)
        lower_wick = min(open_, close_) - low

        # fitil gövdenin en az 2 katı ve total range'in anlamlı kısmı olsun
        if side == "SHORT":
            if upper_wick > body * 2 and upper_wick > range_ * 0.4:
                return True
        else:  # LONG
            if lower_wick > body * 2 and lower_wick > range_ * 0.4:
                return True

        return False

    # ---------- WEBSOCKET LOOP ----------

    async def websocket_loop(self):
        """!miniTicker@arr stream’inden sürekli fiyat/volume güncelle."""
        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    log("[WS] Bağlandı: !miniTicker@arr")
                    async for message in ws:
                        msg = json.loads(message)
                        data = msg.get("data")
                        if not data:
                            continue
                        for item in data:
                            sym_id = item.get("s")  # "BTCUSDT"
                            if sym_id not in self.valid_ids:
                                continue
                            price = float(item["c"])
                            vol24 = float(item["v"])
                            self.latest_price[sym_id] = price
                            self.latest_vol24[sym_id] = vol24

                            # 1s OHLC güncelle
                            self._update_ohlc_1s(sym_id, price)
            except Exception as e:
                log("[WS ERR]", e)
                log("[WS] 5 sn sonra yeniden bağlanmayı deneyecek...")
                await asyncio.sleep(5)

    # ---------- 1s AGGREGATION + ANALYSIS ----------

    async def aggregation_loop(self):
        """Her 1 saniyede bir, 1s bar oluştur ve analiz et."""
        while True:
            start = time.time()
            active_count = 0

            for sym_id in list(self.valid_ids):
                price = self.latest_price.get(sym_id)
                if price is None:
                    continue

                # Likidite filtresi (24h volume)
                vol24 = self.latest_vol24.get(sym_id, 0.0)
                if vol24 < MIN_24H_VOL_USDT:
                    continue

                prev24 = self.last_vol24_snapshot.get(sym_id, vol24)
                delta_vol = max(vol24 - prev24, 0.0)
                self.last_vol24_snapshot[sym_id] = vol24

                pb = self.price_buffers[sym_id]
                vb = self.volume_buffers[sym_id]
                pb.append(price)
                vb.append(delta_vol if delta_vol > 0 else 0.0)

                if len(pb) >= CONTEXT_WINDOW:
                    active_count += 1
                    await self.process_series(
                        sym_id,
                        np.array(pb, dtype=float),
                        np.array(vb, dtype=float),
                    )

            elapsed = time.time() - start
            log(f"[AGG] Analiz edilen sembol sayısı: {active_count}")
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    async def process_series(self, sym_id, prices, volumes):
        st = self.state[sym_id]
        now = time.time()

        if now < st["cooldown_until"]:
            return

        current_price = float(prices[-1])

        # Ölü piyasa filtresi (son 60 saniye range)
        tail = prices[-SIGMA_WINDOW:]
        price_range_pct = (tail.max() - tail.min()) / current_price * 100
        if price_range_pct < MIN_RANGE_PCT:
            return

        # Bağlam penceresi
        context_window = prices[-CONTEXT_WINDOW:]

        # Z-score + context sigma
        z, ctx_mean, ctx_sigma = UltimateMath.z_score(context_window, current_price)
        st["context_mean"] = ctx_mean
        st["context_sigma"] = ctx_sigma

        # Local sigma
        _, sigma_short = UltimateMath.get_sigma(prices[-SIGMA_WINDOW:])

        # Coin'in volatilite profili (running EMA)
        if st["sigma_avg"] == 0.0:
            st["sigma_avg"] = sigma_short
        else:
            st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short

        # Aşırı sakin market → sinyal üretme
        if st["sigma_avg"] < LOW_SIGMA_TH:
            return

        # Coin'in tipik z_peak profili (running EMA)
        abs_z = abs(z)
        if st["z_peak"] == 0.0:
            st["z_peak"] = abs_z
        else:
            st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        # Dinamik Z-score eşiği (1.8–4.0 bandı)
        dyn_z_th = 0.7 * st["z_peak"] + 0.3 * BASE_Z_SCORE_THRESHOLD
        dyn_z_th = max(1.8, min(4.0, dyn_z_th))

        # Z-score debounce
        self.z_history[sym_id].append(z)
        over_th_count = sum(1 for zz in self.z_history[sym_id] if zz >= dyn_z_th)
        under_th_count = sum(1 for zz in self.z_history[sym_id] if zz <= -dyn_z_th)

        # VWAP
        vwap_prices = prices[-VWAP_WINDOW:]
        vwap_vols   = volumes[-VWAP_WINDOW:]
        vwap = UltimateMath.rolling_vwap(vwap_prices, vwap_vols)

        # EMA kinematics
        smooth = UltimateMath.ema(prices)
        v, a = UltimateMath.get_kinematics(smooth)

        vwap_dev_pct = (current_price - vwap) / vwap * 100
        mode = st["mode"]

        # Trend filtresi (context içi fast/slow EMA)
        # Fast ~ 60s, Slow ~ 180s
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

        # Volatiliteye göre reversal yüzdesini dinamik çarpan
        if st["sigma_avg"] > 0:
            vol_ratio = sigma_short / st["sigma_avg"]
        else:
            vol_ratio = 1.0
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        # Aşırı yüksek volatilite rejimi → giriş daha sıkı
        vol_regime_mult = 1.3 if st["sigma_avg"] > HIGH_SIGMA_TH else 1.0

        # ===================================
        # ============ SHORT (PUMP) =========
        # ===================================

        if mode == "IDLE":
            # Trend UP, Z-score uzun süredir yüksek, VWAP'tan anlamlı uzak
            if (
                trend == "UP" and
                over_th_count >= Z_DEBOUNCE_COUNT and
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
            pump_pct  = (pump_move / st["nadir"]) * 100 if st["nadir"] > 0 else 0.0

            base_rev_req = UltimateMath.get_dynamic_reversal_threshold(pump_pct)
            curve_broken, curvature, slope, cur_slope = UltimateMath.poly_curve_signal(
                prices, side="SHORT"
            )

            ENTRY_STRICTNESS_LOCAL = ENTRY_STRICTNESS * vol_factor * vol_regime_mult

            final_rev_req = base_rev_req * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_rev_req *= ENTRY_STRICTNESS_LOCAL  # daha geç teyit

            drop_pct   = (st["apex"] - current_price) / st["apex"] * 100
            drop_sigma = (st["apex"] - current_price) / max(st["locked_sigma"], 1e-6)

            # Ek şart: Z-score artık ortalamaya yaklaşmış olmalı
            z_back_to_mean = z <= dyn_z_th * Z_EXIT_BAND

            # Wick teyidi (üst fitil)
            wick_ok = self._check_wick(sym_id, side="SHORT")

            if (
                drop_pct >= final_rev_req and
                drop_sigma >= SIGMA_MOVE_STRICT and
                a < 0 and
                z_back_to_mean and
                wick_ok
            ):
                await self.send_signal(
                    symbol_id=sym_id,
                    side="SHORT",
                    price=current_price,
                    z=z,
                    vwap_dev=vwap_dev_pct,
                    move_pct=pump_pct,
                    reversal_pct=drop_pct,
                    reversal_sigma=drop_sigma,
                    curve_broken=curve_broken,
                    curvature=curvature,
                    slope=cur_slope,
                    ctx_mean=ctx_mean,
                    ctx_sigma=ctx_sigma,
                    sigma_short=sigma_short
                )
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now + COOLDOWN_SEC
                st["last_signal_ts"]  = now

        # ===================================
        # ============ LONG (DUMP) ==========
        # ===================================

        if mode == "IDLE":
            if (
                trend == "DOWN" and
                under_th_count >= Z_DEBOUNCE_COUNT and
                z <= -dyn_z_th and
                vwap_dev_pct <= -VWAP_STRETCH_THRESHOLD
            ):
                st["mode"] = "WATCHING_LONG"
                st["nadir"] = current_price
                st["apex"]  = float(context_window.max())
                st["locked_sigma"] = sigma_short

        elif mode == "WATCHING_LONG":
            if current_price < st["nadir"]:
                st["nadir"] = current_price

            dump_move = st["apex"] - st["nadir"]
            dump_pct  = (dump_move / st["apex"]) * 100 if st["apex"] > 0 else 0.0

            base_bounce_req = UltimateMath.get_dynamic_reversal_threshold(dump_pct)
            curve_broken, curvature, slope, cur_slope = UltimateMath.poly_curve_signal(
                prices, side="LONG"
            )

            ENTRY_STRICTNESS_LOCAL = ENTRY_STRICTNESS * vol_factor * vol_regime_mult

            final_bounce_req = base_bounce_req * (CURVE_ACCELERATOR if curve_broken else 1.0)
            final_bounce_req *= ENTRY_STRICTNESS_LOCAL

            bounce_pct   = (current_price - st["nadir"]) / st["nadir"] * 100
            bounce_sigma = (current_price - st["nadir"]) / max(st["locked_sigma"], 1e-6)

            # Ek şart: Z-score artık aşırı negatif değil, toparlamış olmalı
            z_back_to_mean = z >= -dyn_z_th * Z_EXIT_BAND

            # Wick teyidi (alt fitil)
            wick_ok = self._check_wick(sym_id, side="LONG")

            if (
                bounce_pct >= final_bounce_req and
                bounce_sigma >= SIGMA_MOVE_STRICT and
                a > 0 and
                z_back_to_mean and
                wick_ok
            ):
                await self.send_signal(
                    symbol_id=sym_id,
                    side="LONG",
                    price=current_price,
                    z=z,
                    vwap_dev=vwap_dev_pct,
                    move_pct=dump_pct,
                    reversal_pct=bounce_pct,
                    reversal_sigma=bounce_sigma,
                    curve_broken=curve_broken,
                    curvature=curvature,
                    slope=cur_slope,
                    ctx_mean=ctx_mean,
                    ctx_sigma=ctx_sigma,
                    sigma_short=sigma_short
                )
                st["mode"] = "COOLDOWN"
                st["cooldown_until"] = now + COOLDOWN_SEC
                st["last_signal_ts"]  = now

        # Cooldown'dan çıkış: fiyat tekrar VWAP civarına gelmiş olsun
        if (
            st["mode"] == "COOLDOWN" and
            now >= st["cooldown_until"] and
            abs(vwap_dev_pct) < 0.5
        ):
            st["mode"] = "IDLE"

    async def send_signal(
        self, symbol_id, side, price,
        z, vwap_dev, move_pct,
        reversal_pct, reversal_sigma,
        curve_broken, curvature, slope,
        ctx_mean, ctx_sigma, sigma_short
    ):
        emoji_side = "🔴 SHORT" if side == "SHORT" else "🟢 LONG"
        title = "PUMP REVERSAL" if side == "SHORT" else "DUMP BOUNCE"
        accel_text = "EVET 🏎️" if curve_broken else "HAYIR"

        # Kalite metriği (sigma'ya göre)
        if reversal_sigma >= 3:
            quality = "🚨 AŞIRI GÜÇLÜ"
        elif reversal_sigma >= 2:
            quality = "🔥 YÜKSEK"
        elif reversal_sigma >= 1.5:
            quality = "✅ ORTA"
        else:
            quality = "⚠️ ZAYIF (dikkat)"

        binance_url = f"https://www.binance.com/en/futures/{symbol_id}"
        tv_url      = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{symbol_id}"

        msg = (
            f"<b>{emoji_side} | {symbol_id}</b>\n"
            f"<b>Özet:</b> {title} | Kalite: <b>{quality}</b>\n"
            f"Giriş Fiyatı: <code>{price:.6f}</code>\n\n"
            f"🧠 <b>Bağlam:</b>\n"
            f"• Z-Score: <code>{z:.2f}</code>\n"
            f"• VWAP Sapması: <b>%{vwap_dev:.2f}</b>\n"
            f"• 5dk Mean / Sigma: <code>{ctx_mean:.6f}</code> / <code>{ctx_sigma:.6f}</code>\n"
            f"• 60sn Sigma: <code>{sigma_short:.6f}</code>\n\n"
            f"📈 <b>Hareket:</b>\n"
            f"• Ana Move (dump/pump): <b>%{move_pct:.2f}</b>\n"
            f"• Reversal: <b>%{reversal_pct:.2f}</b> (%), <b>{reversal_sigma:.2f}σ</b>\n"
            f"• Eğri Hızlandırıcı (Poly): <b>{accel_text}</b>\n\n"
            f"<i>(VWAP, dinamik Z-score, sigma, trend filtresi ve polinom eğri ile geç teyitli dönüş sinyali.)</i>\n"
            f"<a href='{binance_url}'>Binance</a> | "
            f"<a href='{tv_url}'>TradingView</a>"
        )

        if self.bot and TG_CHAT_ID != 0:
            try:
                await self.bot.send_message(
                    chat_id=TG_CHAT_ID,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as e:
                log("[TG ERR]", e)

        log(
            f"SİNYAL: {symbol_id} | {side} | "
            f"move={move_pct:.2f}% | rev={reversal_pct:.2f}% | "
            f"{reversal_sigma:.2f}σ | z={z:.2f}"
        )

    async def run(self):
        await self.load_futures_list()
        await asyncio.gather(
            self.websocket_loop(),
            self.aggregation_loop()
        )


if __name__ == "__main__":
    engine = UltimateReversalWS()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        log("Durduruldu.")
