#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ULTIMATE REVERSAL OPTIMIZER - JOBLIB EDITION (STABLE M3 PRO)
------------------------------------------------------------
- Yöntem: Joblib 'Loky' Backend
- Farkı: Kilitlenmeyi önlemek için 'Trial Parallelism' yerine 
         'Data Parallelism' kullanır.
- Performans: %100 CPU Kullanımı (Deadlock-Free)
"""

import asyncio
import time
import sys
import numpy as np
import ccxt.async_support as ccxt_async
import ccxt
import optuna
from tqdm import tqdm
from joblib import Parallel, delayed

# ============================================================
# 1) CONFIGURATION
# ============================================================

BINANCE_API_KEY    = "" 
BINANCE_API_SECRET = ""

# M3 Pro Gücü İçin 50-75 arası idealdir.
MAX_SYMBOLS        = 50
TIMEFRAME          = "1m"
LIMIT_BARS         = 1440 

CONTEXT_WINDOW     = 300
VWAP_WINDOW        = 120
SIGMA_WINDOW       = 60
POLY_WINDOW        = 30
LOOKAHEAD_SEC      = 60

TOTAL_TRIALS       = 300

MARKET_DATA = {}

# ============================================================
# 2) MATH ENGINE
# ============================================================

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
        mean = np.mean(window_prices)
        std  = np.std(window_prices)
        if std < 1e-9: std = 1e-9
        z = (current_price - mean) / std
        return z, mean, std

    @staticmethod
    def get_sigma(window_prices):
        mean = np.mean(window_prices)
        std  = np.std(window_prices)
        return mean, std

# ============================================================
# 3) LOGIC & DATA GEN
# ============================================================

def detect_regime(sigma_short, sigma_avg, v, a):
    if sigma_avg <= 0: return "NORMAL"
    vol_ratio = sigma_short / sigma_avg
    if vol_ratio > 1.8: return "HIGH_CHOP"
    if a > 0 and v > 0: return "UP_TREND"
    if a < 0 and v < 0: return "DOWN_TREND"
    return "NORMAL"

def generate_synthetic_1s(ohlcv, noise_level=0.0002):
    ohlcv = np.array(ohlcv, dtype=float)
    opens, closes, vols = ohlcv[:, 1], ohlcv[:, 4], ohlcv[:, 5]
    
    price_chunks, vol_chunks = [], []
    
    for o, c, v in zip(opens, closes, vols):
        base = np.linspace(o, c, 60)
        ref = max(abs(o), 1.0)
        noise = np.random.normal(0, noise_level * ref, 60)
        p_sec = np.maximum(base + noise, 1e-8)
        v_sec = (v / 60.0) * np.random.uniform(0.8, 1.2, 60)
        
        price_chunks.append(p_sec)
        vol_chunks.append(v_sec)
        
    return np.concatenate(price_chunks), np.concatenate(vol_chunks)

async def fetch_data():
    global MARKET_DATA
    ex = ccxt_async.binanceusdm({'enableRateLimit': True})
    markets = await ex.load_markets()
    
    symbols = [m["id"] for m in markets.values() if m.get("quote") == "USDT" and m.get("swap", False)]
    symbols.sort()
    symbols = symbols[:MAX_SYMBOLS]
    
    print(f"[DATA] {len(symbols)} Sembol indiriliyor...")
    
    pbar = tqdm(total=len(symbols), desc="İndirme")
    for sym in symbols:
        try:
            ohlcv = await ex.fetch_ohlcv(sym, TIMEFRAME, limit=LIMIT_BARS)
            if ohlcv and len(ohlcv) > 100:
                p, v = generate_synthetic_1s(ohlcv)
                if len(p) > CONTEXT_WINDOW + LOOKAHEAD_SEC:
                    MARKET_DATA[sym] = {"p": p, "v": v}
        except: pass
        pbar.update(1)
    
    await ex.close()
    pbar.close()

# ============================================================
# 4) SIMULATION (WORKER)
# ============================================================

def simulate_single_symbol(prices, volumes, params):
    """
    Bu fonksiyon Joblib tarafından paralel çalıştırılacak.
    """
    # Parametre Unpack
    BASE_Z      = params["BASE_Z"]
    VWAP_STR    = params["VWAP_STR"]
    SIGMA_STR   = params["SIGMA_STR"]
    ENTRY_STR   = params["ENTRY_STR"]
    Z_EXIT      = params["Z_EXIT"]
    CURV_ACC    = params["CURV_ACC"]
    MIN_CURV    = params["MIN_CURV"]
    EMA_AL      = params["EMA_AL"]
    MIN_RNG     = params["MIN_RNG"]
    REV_MIN     = params["REV_MIN"]
    DYN_TH      = params["DYN_TH"]

    def get_thresh(mpct):
        am = abs(mpct)
        for l, t in DYN_TH:
            if am >= l: return max(t, REV_MIN)
        return 999.0

    n = len(prices)
    # Hızlandırma: Numpy array slicing ile erişim
    # State
    sigma_avg = 0.0
    z_peak = 0.0
    mode = 0 # 0:IDLE, 1:SHORT, 2:LONG
    apex = 0.0
    nadir = 0.0
    locked_sigma = 0.0
    cooldown = 0
    
    signals = 0
    edges = []

    # Döngü
    for i in range(CONTEXT_WINDOW, n - LOOKAHEAD_SEC):
        if cooldown > 0:
            cooldown -= 1
            continue

        p_curr = prices[i]
        
        # 1. Range Filtresi
        tail = prices[i-SIGMA_WINDOW:i]
        if (tail.max() - tail.min()) / p_curr * 100 < MIN_RNG:
            continue

        # 2. Context
        ctx = prices[i-CONTEXT_WINDOW:i]
        z, _, _ = UltimateMath.z_score(ctx, p_curr)
        _, sigma_s = UltimateMath.get_sigma(tail)

        # State update
        if sigma_avg == 0: sigma_avg = sigma_s
        else: sigma_avg = 0.98*sigma_avg + 0.02*sigma_s
        
        abz = abs(z)
        if z_peak == 0: z_peak = abz
        else: z_peak = 0.98*z_peak + 0.02*abz

        # 3. Regime
        sm = UltimateMath.ema(ctx, EMA_AL)
        v, a = UltimateMath.get_kinematics(sm)
        regime = detect_regime(sigma_s, sigma_avg, v, a)
        
        estr = ENTRY_STR
        sstr = SIGMA_STR
        if regime == "HIGH_CHOP": estr *= 1.25; sstr *= 1.25
        elif regime == "UP_TREND" and a > 0: estr *= 0.85
        elif regime == "DOWN_TREND" and a < 0: estr *= 0.85

        # 4. VWAP
        vp = prices[i-VWAP_WINDOW:i]
        vv = volumes[i-VWAP_WINDOW:i]
        vwap = UltimateMath.rolling_vwap(vp, vv)
        vdev = (p_curr - vwap)/vwap*100

        # Factors
        vfact = np.clip(sigma_s/max(sigma_avg, 1e-9), 0.7, 1.5)
        dyn_z = max(1.8, min(4.0, 0.7*z_peak + 0.3*BASE_Z))

        # --- LOGIC ---
        # SHORT
        if mode == 0 and z >= dyn_z and vdev >= VWAP_STR:
            mode = 1
            apex = p_curr
            locked_sigma = sigma_s
            nadir = ctx.min()
        
        elif mode == 1: # Watch Short
            if p_curr > apex: apex = p_curr
            
            pump = ((apex-nadir)/nadir*100) if nadir>0 else 0
            req = get_thresh(pump)
            
            # Curve check
            cok = False
            if i >= POLY_WINDOW:
                try:
                    y = prices[i-POLY_WINDOW:i]
                    x = np.arange(len(y))
                    a2, b2, _ = np.polyfit(x, y, 2)
                    sl = 2*a2*(len(x)-1)+b2
                    if a2 < -MIN_CURV and sl < 0: cok = True
                except: pass
            
            freq = req * (CURV_ACC if cok else 1.0) * estr * vfact
            drop = (apex - p_curr)/apex*100
            d_sig = (apex - p_curr)/max(locked_sigma, 1e-9)
            
            if drop >= freq and d_sig >= sstr and a < 0 and z <= dyn_z*Z_EXIT:
                # Meta-label check (Offline)
                fut = prices[i+1:i+1+6]
                if len(fut)>0 and fut.min() < p_curr: # Success
                    future_deep = prices[i+1:i+1+LOOKAHEAD_SEC]
                    edge = (apex - future_deep.min())/apex*100
                    edges.append(edge)
                    signals += 1
                    mode = 0
                    cooldown = 30
                else:
                    mode = 0

        # LONG
        if mode == 0 and z <= -dyn_z and vdev <= -VWAP_STR:
            mode = 2
            nadir = p_curr
            apex = ctx.max()
            locked_sigma = sigma_s
            
        elif mode == 2: # Watch Long
            if p_curr < nadir: nadir = p_curr
            dump = ((apex-nadir)/apex*100) if apex>0 else 0
            req = get_thresh(dump)
            
            cok = False
            if i >= POLY_WINDOW:
                try:
                    y = prices[i-POLY_WINDOW:i]
                    x = np.arange(len(y))
                    a2, b2, _ = np.polyfit(x, y, 2)
                    sl = 2*a2*(len(x)-1)+b2
                    if a2 > MIN_CURV and sl > 0: cok = True
                except: pass

            freq = req * (CURV_ACC if cok else 1.0) * estr * vfact
            bounce = (p_curr - nadir)/nadir*100
            b_sig = (p_curr - nadir)/max(locked_sigma, 1e-9)
            
            if bounce >= freq and b_sig >= sstr and a > 0 and z >= -dyn_z*Z_EXIT:
                fut = prices[i+1:i+1+6]
                if len(fut)>0 and fut.max() > p_curr:
                    future_deep = prices[i+1:i+1+LOOKAHEAD_SEC]
                    edge = (future_deep.max() - nadir)/nadir*100
                    edges.append(edge)
                    signals += 1
                    mode = 0
                    cooldown = 30
                else:
                    mode = 0

    return signals, edges

# ============================================================
# 5) OPTUNA OBJECTIVE (JOBLIB WRAPPER)
# ============================================================

def objective(trial):
    # Parametreleri hazırla
    params = {
        "BASE_Z": trial.suggest_float("BASE_Z", 1.8, 3.5),
        "VWAP_STR": trial.suggest_float("VWAP_STR", 0.8, 3.0),
        "SIGMA_STR": trial.suggest_float("SIGMA_STR", 1.0, 3.0),
        "ENTRY_STR": trial.suggest_float("ENTRY_STR", 1.0, 2.2),
        "Z_EXIT": trial.suggest_float("Z_EXIT", 0.1, 0.7),
        "CURV_ACC": trial.suggest_float("CURV_ACC", 0.3, 1.5),
        "MIN_CURV": trial.suggest_float("MIN_CURV", 1e-5, 1e-3, log=True),
        "EMA_AL": trial.suggest_float("EMA_AL", 0.05, 0.4),
        "MIN_RNG": trial.suggest_float("MIN_RNG", 0.1, 1.5),
        "REV_MIN": trial.suggest_float("REV_MIN", 0.1, 1.0),
    }
    t1 = trial.suggest_float("DT1", 0.3, 1.0)
    t2 = trial.suggest_float("DT2", 0.5, 1.5)
    t3 = trial.suggest_float("DT3", 0.8, 2.0)
    t4 = trial.suggest_float("DT4", 1.0, 2.5)
    params["DYN_TH"] = [(15.0, t1),(7.0, t2),(3.0, t3),(1.5, t4)]
    
    if not MARKET_DATA: return -9999.0

    # --- PARALLEL EXECUTION ---
    # Optuna'nın kendisi yerine, hesaplamayı Joblib ile dağıtıyoruz.
    # n_jobs=-1 : Tüm çekirdekleri kullan
    # backend='loky': En kararlı backend (Deadlock proof)
    
    results = Parallel(n_jobs=-1, backend="loky")(
        delayed(simulate_single_symbol)(
            MARKET_DATA[sym]["p"], 
            MARKET_DATA[sym]["v"], 
            params
        ) for sym in MARKET_DATA
    )
    
    # Sonuçları topla
    total_signals = 0
    all_edges = []
    
    for sigs, edges in results:
        total_signals += sigs
        all_edges.extend(edges)
        
    if total_signals == 0 or not all_edges:
        return -1.0
        
    avg_edge = np.mean(all_edges)
    score = avg_edge * np.log(1 + total_signals)
    if avg_edge < 0: score -= 50.0
    
    return score

# ============================================================
# 6) MAIN
# ============================================================

if __name__ == "__main__":
    print("[MAIN] Veri hazırlanıyor...")
    try:
        loop = asyncio.get_running_loop()
    except:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    loop.run_until_complete(fetch_data())
    
    if not MARKET_DATA:
        print("Veri Yok!")
        sys.exit(1)
        
    print(f"[MAIN] Optuna Başlıyor (Joblib Backend ile)...")
    print(f"[INFO] M3 Pro'nun tüm çekirdekleri hesaplama anında aktif olacak.")
    
    study = optuna.create_study(direction="maximize")
    
    # DİKKAT: Optuna'ya n_jobs=1 diyoruz, çünkü paralelleştirmeyi içeride Joblib yapıyor.
    # Bu yöntem kilitlenmeyi %100 çözer.
    study.optimize(objective, n_trials=TOTAL_TRIALS, n_jobs=1, show_progress_bar=True)
    
    print("\n" + "="*60)
    print("🏆 SONUÇLAR")
    print(f"Skor: {study.best_value}")
    print(study.best_params)