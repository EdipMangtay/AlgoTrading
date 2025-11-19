#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hammer Pro+ (DYNAMIC REALTIME FORMULA) – SIKI FİLTRELİ
- Boost formülü: Daha önce birlikte çıkardığımız 5 adımlı matematikle birebir.
- Formüle dokunmuyoruz, sadece SİNYAL FİLTRESİNİ sıkılaştırıyoruz:
  - Yüksek Boost şart (MIN_BOOST_ALERT)
  - REV (dönüş miktarı) belirli eşiğin üstü
  - Gate G (kaç TF aşırı bölgede) yeterli
  - 1m RSI/SRSI gerçekten uçlarda (hafif değil, sert aşırılık)
"""

import time
import math
import asyncio
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import numpy as np
import pandas as pd
import ccxt.async_support as ccxt_async
from telegram import Bot

# ==============================
# 1) KULLANICI AYARLARI
# ==============================


#deneme
# ==============================
# GENEL AYARLAR
# ==============================

SCAN_INTERVAL_SEC        = 8         # tarama aralığı
REFRESH_SYMBOLS_SEC      = 300
COOLDOWN_SEC             = 90        # aynı sembol için minimum süre (daha az spam)
QUOTE                     = "USDT"
MAX_CONCURRENT_SYMBOLS    = 15

# *** SİNYAL KALİTESİ EŞİKLERİ ***
MIN_BOOST_ALERT          = 3.0       # Boost bu değerin ALTINDA ise sinyal YOK (önce 2.0/2.5/3.0 deneyebilirsin)
MIN_REV_FILTER           = 0.30      # REV >= 0.30 → en az %0.3'lik dönüş (0.5 dersen %0.5)
MIN_GATE_FILTER          = 2.0/3.0   # G >= 2 TF aşırı bölgede (yaklaşık 0.66)
# RSI uç bölge filtreleri (1m için):
RSI_SHORT_MIN            = 70.0
SRSI_SHORT_MIN           = 80.0
RSI_LONG_MAX             = 30.0
SRSI_LONG_MAX            = 20.0

# --- FORMÜL PARAMETRELERİ ---
SCALE_S = 39.0           # Kalibrasyon ölçeği
TH_E    = 0.30           # Gate E eşiği
MTF_W   = {"1m": 0.40, "5m": 0.40, "1h": 0.20}
VOL_BINS = [
    (0.24, 0.24),
    (0.45, 0.40),
    (0.55, 0.50),
    (9.99, 0.85),
]
FUND_CAP = 0.50

# ==============================
# MATEMATİK MOTORU
# ==============================

def log(*args):
    print(*args, flush=True)

def clamp(x, lo=0.0, hi=1.0) -> float:
    if math.isnan(x):
        return lo
    return float(max(lo, min(hi, x)))

def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1/length, adjust=False).mean()

def rsi_tradingview(close_array: np.ndarray, length: int = 14) -> float:
    if len(close_array) < length + 1:
        return 50.0
    series = pd.Series(close_array)
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = rma(up, length)
    ma_down = rma(down, length)
    curr_up = ma_up.iloc[-1]
    curr_down = ma_down.iloc[-1]
    if curr_down == 0:
        return 100.0
    if curr_up == 0:
        return 0.0
    rs = curr_up / curr_down
    return float(100 - (100 / (1 + rs)))

def stoch_rsi_tradingview(close_array: np.ndarray, length_rsi=14, length_stoch=14, smooth_k=3) -> float:
    if len(close_array) < length_rsi + length_stoch + smooth_k:
        return 50.0
    series = pd.Series(close_array)
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = rma(up, length_rsi)
    ma_down = rma(down, length_rsi)
    rs = ma_up / ma_down
    rsi_series = 100 - (100 / (1 + rs))
    rsi_series = rsi_series.fillna(50.0)
    rsi_window = rsi_series.rolling(window=length_stoch)
    min_rsi = rsi_window.min()
    max_rsi = rsi_window.max()
    denominator = max_rsi - min_rsi
    stoch = (rsi_series - min_rsi) / denominator.replace(0, 0.000001) * 100
    stoch = stoch.fillna(50.0)
    k_series = stoch.rolling(window=smooth_k).mean()
    val = k_series.iloc[-1]
    return float(val) if not math.isnan(val) else 50.0

def atr_percent_1m(ohlcv_1m: np.ndarray, n: int = 14) -> float:
    if len(ohlcv_1m) < n + 1:
        return 0.1
    h, l, c = ohlcv_1m[:, 2], ohlcv_1m[:, 3], ohlcv_1m[:, 4]
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1/n, adjust=False).mean().iloc[-1]
    if c[-1] == 0:
        return 0.0
    return float(atr / c[-1] * 100.0)

def get_v_star(percent_vol: float) -> float:
    if math.isnan(percent_vol):
        return 0.24
    for thr, val in VOL_BINS:
        if percent_vol <= thr:
            return val
    return VOL_BINS[-1][1]

# ==============================
# BOOST FORMÜLÜ (BİREBİR)
# ==============================

def et_calc(rsi, srsi, direction):
    """Adım 1: Aşırılık (MTF)"""
    if direction == "short":
        term1 = 0.6 * max(0.0, (rsi - 70.0) / 20.0)
        term2 = 0.4 * max(0.0, (srsi - 80.0) / 20.0)
    else:  # long
        term1 = 0.6 * max(0.0, (30.0 - rsi) / 20.0)
        term2 = 0.4 * max(0.0, (20.0 - srsi) / 20.0)
    return term1 + term2

def compute_boost_dynamic(
    direction: str,
    tf_vals: Dict[str, Tuple[float, float]],
    prev: float,
    curr: float,
    funding: float,
    vstar: float,
    decay: float,
) -> Tuple[float, Dict[str, float]]:
    """
    Formül birebir:
      - MTF (E_T)
      - REV (dönüş)
      - G (gate)
      - Funding bonus
      - V★ ve decay çarpanı
    Boost hesaplamasının kendisine DOKUNMUYORUZ.
    Ek olarak bileşenleri dict olarak geri döndürüyoruz (filtre için).
    """

    # --- YÖN KONTROLÜ ---
    if direction == "short":
        if curr >= prev:
            return 0.0, {"mtf": 0.0, "rev": 0.0, "g": 0.0, "raw": 0.0}
        price_diff = prev - curr
    else:  # long
        if curr <= prev:
            return 0.0, {"mtf": 0.0, "rev": 0.0, "g": 0.0, "raw": 0.0}
        price_diff = curr - prev

    # --- ADIM 1: MTF ---
    E: Dict[str, float] = {}
    cnt = 0
    for tf in ["1m", "5m", "1h"]:
        r, s = tf_vals[tf]
        val = et_calc(r, s, direction)
        E[tf] = val
        if val >= TH_E:
            cnt += 1

    mtf_score = (
        MTF_W["1m"] * E["1m"] +
        MTF_W["5m"] * E["5m"] +
        MTF_W["1h"] * E["1h"]
    )

    # --- ADIM 2: REV ---
    raw_rev = price_diff / (0.01 * prev)
    rev = clamp(raw_rev, 0.0, 1.0)

    # --- ADIM 3: GATE G ---
    g = 0.0 if cnt == 0 else cnt / 3.0

    # --- ADIM 4: ÇEKİRDEK ---
    rev_eff = rev * mtf_score
    fund_val = clamp((-funding if direction == "short" else funding), 0.0, FUND_CAP)
    raw = 0.60 * mtf_score + 0.35 * rev_eff + 0.05 * fund_val

    # --- ADIM 5: SCALE ---
    boost = SCALE_S * raw * g * vstar * decay

    components = {
        "mtf": mtf_score,
        "rev": rev,
        "g": g,
        "raw": raw,
    }
    return float(boost), components

def star_for_boost(b: float) -> str:
    if b >= 4.0:
        return "⭐⭐⭐"
    if b >= 3.0:
        return "⭐⭐"
    if b >= 2.0:
        return "⭐"
    return ""

def fmt(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    return str(int(round(v)))

# ==============================
# ANA İŞLEM DÖNGÜSÜ
# ==============================

@dataclass
class SymbolState:
    last_alert_ts: float = 0.0

class Scanner:
    def __init__(self):
        log("[INIT] Hammer Pro+ (DYNAMIC) Başlatılıyor...")
        self.ex = ccxt_async.binanceusdm({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self.symbols = []
        self.state: Dict[str, SymbolState] = {}
        self.bot = Bot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

    async def close(self):
        await self.ex.close()

    async def refresh_symbols(self):
        try:
            m = await self.ex.load_markets(reload=True)
            self.symbols = [
                d["symbol"] for d in m.values()
                if d.get("quote") == QUOTE and d.get("active")
            ]
            for s in self.symbols:
                if s not in self.state:
                    self.state[s] = SymbolState()
            log(f"[REFRESH] {len(self.symbols)} sembol aktif.")
        except Exception as e:
            log(f"[ERR] Refresh: {e}")

    async def process_symbol(self, symbol: str, fr_cache: Dict[str, float]) -> Optional[str]:
        st = self.state[symbol]
        now = time.time()
        if now - st.last_alert_ts < COOLDOWN_SEC:
            return None

        try:
            o1 = np.array(await self.ex.fetch_ohlcv(symbol, "1m", limit=100), dtype=float)
            if len(o1) < 20:
                return None
            o5 = np.array(await self.ex.fetch_ohlcv(symbol, "5m", limit=50), dtype=float)
            oH = np.array(await self.ex.fetch_ohlcv(symbol, "1h", limit=50), dtype=float)
        except Exception:
            return None

        c1 = o1[:, 4]
        c5 = o5[:, 4] if len(o5) > 10 else np.array([])
        cH = oH[:, 4] if len(oH) > 10 else np.array([])

        curr = float(c1[-1])
        prev = float(c1[-2])

        r1 = rsi_tradingview(c1)
        s1 = stoch_rsi_tradingview(c1)
        r5 = rsi_tradingview(c5) if len(c5) > 0 else 50.0
        s5 = stoch_rsi_tradingview(c5) if len(c5) > 0 else 50.0
        rH = rsi_tradingview(cH) if len(cH) > 0 else 50.0
        sH = stoch_rsi_tradingview(cH) if len(cH) > 0 else 50.0

        tf_vals = {
            "1m": (r1, s1),
            "5m": (r5, s5),
            "1h": (rH, sH),
        }

        vol_pct = atr_percent_1m(o1)
        vstar   = get_v_star(vol_pct)
        fr      = fr_cache.get(symbol, 0.0)
        decay   = 1.0  # şimdilik sabit, istersek ileride ek azaltıcı koyarız

        best_boost = 0.0
        best_msg   = None

        for d in ["short", "long"]:
            boost, comp = compute_boost_dynamic(
                d, tf_vals, prev, curr, fr, vstar, decay
            )
            mtf_score = comp["mtf"]
            rev_val   = comp["rev"]
            gate_g    = comp["g"]

            # === KALİTE FİLTRELERİ ===
            # 1) Boost eşiği
            if boost < MIN_BOOST_ALERT:
                continue

            # 2) REV filtresi (fiyatın gerçekten dönmesi gerekiyor)
            if rev_val < MIN_REV_FILTER:
                continue

            # 3) Gate G filtresi (en az 2 TF aşırı bölgede)
            if gate_g < MIN_GATE_FILTER:
                continue

            # 4) 1m aşırılık filtresi (hafif değil, ciddi aşırılık)
            if d == "short":
                if not (r1 >= RSI_SHORT_MIN or s1 >= SRSI_SHORT_MIN):
                    continue
            else:  # long
                if not (r1 <= RSI_LONG_MAX or s1 <= SRSI_LONG_MAX):
                    continue

            # buraya kadar geldiyse, sinyal gerçekten sert bir durumda
            if boost > best_boost:
                best_boost = boost
                stars = star_for_boost(boost)
                arrow = "🔴" if d == "short" else "🟢"

                clean_sym = symbol.replace("/", "").split(":")[0]
                bn_link = f"https://www.binance.com/en/futures/{clean_sym}"
                tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{clean_sym}.P"

                chg_percent = ((curr - prev) / prev) * 100.0

                msg = (
                    f"<b>{arrow} #{clean_sym} {stars}</b>\n"
                    f"Boost: <b>+{boost:.2f}%</b>\n"
                    f"Price: <code>{curr:.5g}</code>\n"
                    f"Instant Chg: <b>{chg_percent:+.2f}%</b>\n"
                    f"RSI: 1m {fmt(r1)} | 5m {fmt(r5)} | 1h {fmt(rH)}\n"
                    f"SRSI: 1m {fmt(s1)} | 5m {fmt(s5)}\n"
                    f"MTF: {mtf_score:.2f} | REV: {rev_val:.2f} | G: {gate_g:.2f}\n"
                    f"V*: {vstar:.2f}\n"
                    f"<a href='{bn_link}'>Binance</a> | <a href='{tv_link}'>TradingView</a>"
                )
                best_msg = msg

        if best_msg:
            st.last_alert_ts = now
            return best_msg

        return None

    async def send_telegram(self, text: str):
        if not self.bot:
            log(text)
            return
        try:
            await self.bot.send_message(
                chat_id=TG_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            log(f"[TG ERR] {e}")

    async def run(self):
        await self.refresh_symbols()
        log("Hammer Pro+ (Dynamic) Başladı.")
        await self.send_telegram("Hammer Pro+ Başladı. Sıkı filtreler aktif (daha az ama daha kaliteli sinyal).")

        while True:
            try:
                fr_cache: Dict[str, float] = {}  # funding istersen burayı doldururuz
                for i in range(0, len(self.symbols), MAX_CONCURRENT_SYMBOLS):
                    batch = self.symbols[i : i + MAX_CONCURRENT_SYMBOLS]
                    batch_tasks = [self.process_symbol(s, fr_cache) for s in batch]
                    results = await asyncio.gather(*batch_tasks)

                    for res in results:
                        if res:
                            log(f"[SİNYAL] {res.splitlines()[0]}")
                            await self.send_telegram(res)

                    await asyncio.sleep(0.5)

                await asyncio.sleep(SCAN_INTERVAL_SEC)

            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"[ERR] {e}")
                traceback.print_exc()
                await asyncio.sleep(5)

if __name__ == "__main__":
    s = Scanner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(s.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(s.close())
