#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ULTIMATE REVERSAL ENGINE V3 (Final Sniper Edition)
--------------------------------------------------
- Optimizasyon: Apple Silicon M3 Pro (Joblib/Optuna)
- Parametre Seti: 21.11.2025 Tarihli Nihai Set
- Strateji: Z-Score + VWAP + Kalman + Hurst + RSI + Volume Z
- Mod: Sniper (1.5s Teyit)
"""

import asyncio
import json
import time
from collections import defaultdict, deque

import numpy as np
import ccxt.async_support as ccxt_async
from telegram import Bot
import websockets

# ==========================================
# 1) AYARLAR (NİHAİ OPTUNA PARAMETRELERİ)
# ==========================================

# 🔐 TELEGRAM
TG_BOT_TOKEN = "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ"
TG_CHAT_ID   = 1222350744

# 🔐 BINANCE (Veri akışı için gereklidir)
BINANCE_API_KEY    = "NQAXu0lgpVinMKr5pAiANMFqLunbiZ5eYMZbm6Zbinr83cEfgextjsalzS87YATQ"
BINANCE_API_SECRET = "DHdJl7vTnitQvO8GznnXIa66eqho4FOI58gXZN9kJpuWFiyvsVynmGaWOr5oioNe"

# ⚙️ SABİT PENCERELER
CONTEXT_WINDOW     = 300   # 5 dk
VWAP_WINDOW        = 120   # 2 dk
SIGMA_WINDOW       = 60    # 1 dk
POLY_WINDOW        = 30    # 30 sn

# 🏆 OPTUNA KAZANAN DEĞERLER
BASE_Z_SCORE_THRESHOLD   = 2.791587479580907
VWAP_STRETCH_THRESHOLD   = 1.381176029649929

SIGMA_MOVE_STRICT        = 2.656513515181195
ENTRY_STRICTNESS         = 2.308280296901772

Z_EXIT_BAND              = 0.4224101144101774
CURVE_ACCELERATOR        = 1.3568283979305238

MIN_CURVATURE            = 1.4401219014166845e-05
EMA_ALPHA                = 0.2823625114062088
MIN_RANGE_PCT            = 1.3248439814703656

# Hareket büyüklüğüne göre minimum tepki
REVERSAL_CONFIRM_PCT_MIN = 0.6352632135729053

# 🧠 GELİŞMİŞ FİLTRE AYARLARI
RSI_OB      = 77.04377791188023      # Aşırı Alım
RSI_OS      = 29.220580349604695     # Aşırı Satım
HURST_LIM   = 0.5379695549418683     # Rejim Sınırı
VOL_Z_TH    = 0.43233344191657264    # Hacim Onayı

# 📊 DİNAMİK EŞİKLER (DT1, DT2, DT3, DT4)
DYNAMIC_THRESHOLDS = [
    (15.0, 0.6374820726832433), # DT1
    (7.0,  0.6745895678529111), # DT2
    (3.0,  1.5318824242235567), # DT3
    (1.5,  1.8950224253668406), # DT4
]

# Sabit Teknik Değerler
RSI_PERIOD    = 14
HURST_WINDOW  = 120
KALMAN_WINDOW = 120
COOLDOWN_SEC  = 90
WS_URL        = "wss://fstream.binance.com/stream?streams=!miniTicker@arr"

def log(*args):
    print(*args, flush=True)

# ==========================================
# 2) MATEMATİK MOTORU (TEMEL)
# ==========================================

class UltimateMath:
    @staticmethod
    def ema(prices, alpha):
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
        if vv == 0: return float(p[-1])
        return float(pv / vv)

    @staticmethod
    def z_score(window_prices, current_price):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std  = float(arr.std())
        if std == 0: std = max(abs(mean) * 1e-4, 1e-6)
        z = (current_price - mean) / std
        return z, mean, std

    @staticmethod
    def get_sigma(window_prices):
        arr = np.array(window_prices, dtype=float)
        mean = float(arr.mean())
        std  = float(arr.std())
        if std == 0: std = max(abs(mean) * 1e-4, 1e-6)
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

# ==========================================
# 3) GELİŞMİŞ MATEMATİK (ADVANCED ENGINE)
# ==========================================

class AdvancedMath:
    @staticmethod
    def calculate_rsi(prices, period=14):
        """Hızlı RSI Hesaplama"""
        prices = np.array(prices, dtype=float)
        if len(prices) < period + 1: return 50.0

        deltas = np.diff(prices)
        seed = deltas[:period+1]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        if down == 0: return 100.0
        rs = up / down
        rsi = 100.0 - (100.0 / (1.0 + rs))

        for i in range(period + 1, len(deltas)):
            delta = deltas[i]
            if delta > 0:
                upval, downval = delta, 0.
            else:
                upval, downval = 0., -delta
            
            up = (up * (period - 1) + upval) / period
            down = (down * (period - 1) + downval) / period
            if down == 0: rs = 0
            else: rs = up / down
            rsi = 100.0 - (100.0 / (1.0 + rs))
            
        return float(rsi)

    @staticmethod
    def hurst_exponent(time_series):
        """Piyasa Rejimi Belirleyici (Trend vs Mean Reversion)"""
        try:
            ts = np.array(time_series, dtype=float)
            if len(ts) < 20: return 0.5
            lags = range(2, 20)
            tau = [np.sqrt(np.std(np.subtract(ts[lag:], ts[:-lag]))) for lag in lags]
            poly = np.polyfit(np.log(lags), np.log(tau), 1)
            return float(poly[0] * 2.0)
        except:
            return 0.5

    @staticmethod
    def kalman_filter(prices, process_variance=1e-5, measurement_variance=0.1):
        """Fiyatı gürültüden temizleyerek 'Gerçek' değeri bulur"""
        prices = np.array(prices, dtype=float)
        n_iter = len(prices)
        if n_iter < 2: return float(prices[-1]) if n_iter>0 else 0.0

        xhat = prices[0]
        P = 1.0

        for k in range(1, n_iter):
            xhatminus = xhat
            Pminus = P + process_variance
            K = Pminus / (Pminus + measurement_variance)
            xhat = xhatminus + K * (prices[k] - xhatminus)
            P = (1 - K) * Pminus
            
        return float(xhat)

# ==========================================
# 4) SNIPER TEYİT (1.5 Saniye)
# ==========================================

async def metalabel_confirmation(engine, sym, side):
    """
    Optimizasyon sonuçlarına uygun 'Hızlı' teyit mekanizması.
    """
    if sym not in engine.price_buffers or len(engine.price_buffers[sym]) == 0:
        return False

    base = float(engine.price_buffers[sym][-1])
    
    # 3 kere 0.5 sn bekle (Toplam 1.5 sn)
    for _ in range(3):
        await asyncio.sleep(0.5)
        if sym not in engine.price_buffers: return False
        cur = float(engine.price_buffers[sym][-1])
        
        # Fiyat tersine patlarsa anında iptal (Stop Avı Koruması)
        if side == "SHORT" and cur > base * 1.001: return False
        if side == "LONG" and cur < base * 0.999: return False

    return True

# ==========================================
# 5) ANA MOTOR
# ==========================================

class UltimateReversalWS:
    def __init__(self):
        log("[INIT] Ultimate Reversal V3 (Sniper Edition) başlatılıyor...")
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
                "sigma_avg": 0.0,
                "z_peak": 0.0,
                "last_rsi": 50.0
            }
        log(f"[MARKET] {len(self.valid_ids)} sembol takip ediliyor.")

    # --- WEBSOCKET ---
    async def websocket_loop(self):
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    log("[WS] Bağlandı.")
                    async for msg in ws:
                        data = json.loads(msg).get("data")
                        if not data: continue
                        for item in data:
                            sym = item["s"]
                            if sym not in self.valid_ids: continue
                            self.latest_price[sym] = float(item["c"])
                            self.latest_vol24[sym] = float(item["v"])
            except Exception as e:
                log("[WS ERR]", e)
                await asyncio.sleep(5)

    # --- AGGREGATION ---
    async def aggregation_loop(self):
        while True:
            start = time.time()
            for sym in list(self.valid_ids):
                price = self.latest_price.get(sym)
                if price is None: continue
                
                vol24 = self.latest_vol24.get(sym, 0.0)
                prev = self.last_vol24_snapshot.get(sym, vol24)
                self.last_vol24_snapshot[sym] = vol24
                delta_vol = max(vol24 - prev, 0.0)

                self.price_buffers[sym].append(price)
                self.volume_buffers[sym].append(delta_vol)

                if len(self.price_buffers[sym]) >= CONTEXT_WINDOW:
                    await self.process_series(
                        sym, 
                        np.array(self.price_buffers[sym]), 
                        np.array(self.volume_buffers[sym])
                    )
            
            elapsed = time.time() - start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    # --- LOGIC CORE ---
    async def process_series(self, sym, prices, vols):
        st = self.state[sym]
        now = time.time()
        if now < st["cooldown_until"]: return

        current_price = float(prices[-1])

        # 1. Kalman Filtresi (Signal Price)
        kalman_w = min(len(prices), KALMAN_WINDOW)
        kalman_price = AdvancedMath.kalman_filter(prices[-kalman_w:])
        
        if abs(current_price - kalman_price) / current_price > 0.01:
            signal_price = current_price
        else:
            signal_price = kalman_price

        # 2. Ölü Piyasa Filtresi
        tail = prices[-SIGMA_WINDOW:]
        if (tail.max() - tail.min()) / current_price * 100 < MIN_RANGE_PCT:
            return

        # 3. Temel Hesaplamalar
        ctx = prices[-CONTEXT_WINDOW:]
        z, mean, sigma_ctx = UltimateMath.z_score(ctx, signal_price)
        _, sigma_short = UltimateMath.get_sigma(tail)

        if st["sigma_avg"] == 0: st["sigma_avg"] = sigma_short
        else: st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short
        
        abs_z = abs(z)
        if st["z_peak"] == 0: st["z_peak"] = abs_z
        else: st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        # 4. Gelişmiş İndikatörler
        rsi_val = AdvancedMath.calculate_rsi(ctx, RSI_PERIOD)
        prev_rsi = st.get("last_rsi", 50.0)
        st["last_rsi"] = rsi_val

        # Optimizasyon: Hurst sadece Z-Score yüksekken hesaplansın (CPU tasarrufu)
        hurst_val = 0.5
        if abs_z > 2.0:
            hurst_val = AdvancedMath.hurst_exponent(ctx[-120:])
        
        vol_tail = vols[-SIGMA_WINDOW:]
        v_mean = np.mean(vol_tail)
        v_std = np.std(vol_tail)
        if v_std == 0: v_std = 1e-9
        vol_z = (vols[-1] - v_mean) / v_std

        # 5. VWAP & Kinematik
        vw_prices = prices[-VWAP_WINDOW:]
        vw_vols = vols[-VWAP_WINDOW:]
        vwap = UltimateMath.rolling_vwap(vw_prices, vw_vols)
        vwap_dev = (signal_price - vwap) / vwap * 100

        sm = UltimateMath.ema(prices, EMA_ALPHA)
        v, a = UltimateMath.get_kinematics(sm)

        vol_ratio = sigma_short / max(st["sigma_avg"], 1e-8)
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        dyn_z = 0.7 * st["z_peak"] + 0.3 * BASE_Z_SCORE_THRESHOLD
        dyn_z = max(1.8, min(4.0, dyn_z))

        mode = st["mode"]

        # --- FILTRELER (OPTUNA) ---
        mean_revert_ok = (hurst_val <= HURST_LIM)
        
        rsi_short_ready = (rsi_val >= RSI_OB or (prev_rsi >= RSI_OB and rsi_val < prev_rsi))
        rsi_long_ready  = (rsi_val <= RSI_OS or (prev_rsi <= RSI_OS and rsi_val > prev_rsi))

        # --- SHORT LOGIC ---
        if mode == "IDLE":
            if (mean_revert_ok and 
                rsi_short_ready and 
                z >= dyn_z and 
                vwap_dev >= VWAP_STRETCH_THRESHOLD):
                
                st["mode"] = "WATCHING_SHORT"
                st["apex"] = current_price
                st["locked_sigma"] = sigma_short
                st["nadir"] = float(ctx.min())

        elif mode == "WATCHING_SHORT":
            if current_price > st["apex"]: st["apex"] = current_price
            
            pump = ((st["apex"] - st["nadir"]) / st["nadir"] * 100) if st["nadir"] > 0 else 0
            base_req = UltimateMath.get_dynamic_reversal_threshold(pump)
            
            c_ok, curv, sl, c_sl = UltimateMath.poly_curve_signal(prices, "SHORT")
            final_req = base_req * (CURVE_ACCELERATOR if c_ok else 1.0)
            final_req *= ENTRY_STRICTNESS * vol_factor

            drop_pct = (st["apex"] - signal_price) / st["apex"] * 100
            drop_sigma = (st["apex"] - signal_price) / max(st["locked_sigma"], 1e-6)
            
            z_ok = z <= dyn_z * Z_EXIT_BAND
            vol_ok = vol_z >= VOL_Z_TH
            rsi_conf = rsi_val < prev_rsi

            if (drop_pct >= final_req and 
                drop_sigma >= SIGMA_MOVE_STRICT and 
                a < 0 and 
                z_ok and 
                vol_ok and 
                rsi_conf):
                
                if await metalabel_confirmation(self, sym, "SHORT"):
                    await self.send_signal(sym, "SHORT", current_price, z, vwap_dev, pump, drop_pct, drop_sigma, c_ok, rsi_val, hurst_val, vol_z)
                    st["mode"] = "COOLDOWN"
                    st["cooldown_until"] = now + COOLDOWN_SEC
                else:
                    st["mode"] = "IDLE"

        # --- LONG LOGIC ---
        if mode == "IDLE":
            if (mean_revert_ok and 
                rsi_long_ready and 
                z <= -dyn_z and 
                vwap_dev <= -VWAP_STRETCH_THRESHOLD):
                
                st["mode"] = "WATCH_LONG"
                st["nadir"] = current_price
                st["apex"] = float(ctx.max())
                st["locked_sigma"] = sigma_short

        elif mode == "WATCH_LONG":
            if current_price < st["nadir"]: st["nadir"] = current_price
            
            dump = ((st["apex"] - st["nadir"]) / st["apex"] * 100) if st["apex"] > 0 else 0
            base_req = UltimateMath.get_dynamic_reversal_threshold(dump)
            
            c_ok, curv, sl, c_sl = UltimateMath.poly_curve_signal(prices, "LONG")
            final_req = base_req * (CURVE_ACCELERATOR if c_ok else 1.0)
            final_req *= ENTRY_STRICTNESS * vol_factor

            bounce_pct = (signal_price - st["nadir"]) / st["nadir"] * 100
            bounce_sigma = (signal_price - st["nadir"]) / max(st["locked_sigma"], 1e-6)
            
            z_ok = z >= -dyn_z * Z_EXIT_BAND
            vol_ok = vol_z >= VOL_Z_TH
            rsi_conf = rsi_val > prev_rsi

            if (bounce_pct >= final_req and 
                bounce_sigma >= SIGMA_MOVE_STRICT and 
                a > 0 and 
                z_ok and 
                vol_ok and 
                rsi_conf):
                
                if await metalabel_confirmation(self, sym, "LONG"):
                    await self.send_signal(sym, "LONG", current_price, z, vwap_dev, dump, bounce_pct, bounce_sigma, c_ok, rsi_val, hurst_val, vol_z)
                    st["mode"] = "COOLDOWN"
                    st["cooldown_until"] = now + COOLDOWN_SEC
                else:
                    st["mode"] = "IDLE"

        if st["mode"] == "COOLDOWN" and now >= st["cooldown_until"] and abs(vwap_dev) < 0.5:
            st["mode"] = "IDLE"

    async def send_signal(self, sym, side, price, z, vwap, move, rev, sigma, curve, rsi, hurst, volz):
        if not self.bot: return
        emoji = "🔴 SHORT" if side == "SHORT" else "🟢 LONG"
        
        # Kalite Skoru (Parametrelere göre)
        score = 0
        if sigma > 3.0: score += 2
        if abs(volz) > 1.0: score += 1
        if curve: score += 1
        if hurst < 0.4: score += 1
        
        qual = "⭐⭐⭐" if score >= 4 else "⭐⭐" if score >= 2 else "⭐"
        
        msg = (
            f"<b>{emoji} | {sym}</b>\n"
            f"Güç: <b>{qual}</b>\n"
            f"Fiyat: <code>{price:.5f}</code>\n\n"
            f"🧠 <b>Analiz:</b>\n"
            f"• Z-Score: <code>{z:.2f}</code>\n"
            f"• RSI: <code>{rsi:.1f}</code>\n"
            f"• Hurst: <code>{hurst:.2f}</code>\n"
            f"• Vol-Z: <code>{volz:.2f}</code>\n\n"
            f"📉 <b>Hareket:</b>\n"
            f"• Ana Move: <b>%{move:.2f}</b>\n"
            f"• Reversal: <b>%{rev:.2f}</b> ({sigma:.2f}σ)\n"
            f"• Curve: {'🏎 EVET' if curve else 'Hayır'}\n"
            f"<a href='https://www.binance.com/en/futures/{sym}'>Binance</a>"
        )
        try:
            await self.bot.send_message(TG_CHAT_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
            log(f"[SİNYAL] {sym} {side} | {qual}")
        except: pass

    async def run(self):
        await self.load_futures_list()
        await asyncio.gather(self.websocket_loop(), self.aggregation_loop())

if __name__ == "__main__":
    engine = UltimateReversalWS()
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        log("Durduruldu.")