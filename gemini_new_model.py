

import time
import asyncio
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import numpy as np
import ccxt.async_support as ccxt_async
from telegram import Bot

# ==============================
# 1) AYARLAR (HASSAS QUANT AYARLARI)
# ==============================

TG_BOT_TOKEN = "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ"
TG_CHAT_ID   = 1222350744

BINANCE_API_KEY    = "NQAXu0lgpVinMKr5pAiANMFqLunbiZ5eYMZbm6Zbinr83cEfgextjsalzS87YATQ"
BINANCE_API_SECRET = "DHdJl7vTnitQvO8GznnXIa66eqho4FOI58gXZN9kJpuWFiyvsVynmGaWOr5oioNe"

SCAN_INTERVAL_SEC      = 3         # Çok hızlı tarama
QUOTE                  = "USDT"
MAX_CONCURRENT_SYMBOLS = 10        # Order Book çektiğimiz için sayıyı düşürdük

# EŞİKLER
MIN_OBI_SCORE          = -0.3      # Satış baskısı > %30 (Short için)
MAX_HURST              = 0.45      # 0.5 altı "Mean Reverting" (Dönüşe meyilli) demektir.
MIN_QUANT_SCORE        = 80.0      # Nihai Skor Eşiği

# ==============================
# 2) İLERİ MATEMATİK MOTORU
# ==============================

def log(*args):
    print(*args, flush=True)

@dataclass
class QuantData:
    real_price: float
    kalman_diff: float
    hurst: float
    obi: float
    velocity: float
    acceleration: float
    total_score: float
    signal: str

class AdvancedQuantMath:
    
    @staticmethod
    def kalman_filter(data: np.ndarray, n_iter: int = 5) -> np.ndarray:
        """
        Basitleştirilmiş 1D Kalman Filtresi.
        Fiyat serisindeki gürültüyü (noise) temizler, gerçek trendi (state) bulur.
        """
        sz = (len(data),)
        # Başlangıç tahminleri
        xhat = np.zeros(sz)      # Tahmin (Posterior)
        P = np.zeros(sz)         # Hata kovaryansı
        xhatminus = np.zeros(sz) # Önsel tahmin (Prior)
        Pminus = np.zeros(sz)    # Önsel hata kovaryansı
        K = np.zeros(sz)         # Kalman Kazancı

        Q = 1e-5 # Süreç gürültü varyansı
        R = 0.01 # Ölçüm gürültü varyansı

        xhat[0] = data[0]
        P[0] = 1.0

        for k in range(1, len(data)):
            # Zaman güncellemesi
            xhatminus[k] = xhat[k-1]
            Pminus[k] = P[k-1] + Q

            # Ölçüm güncellemesi
            K[k] = Pminus[k] / (Pminus[k] + R)
            xhat[k] = xhatminus[k] + K[k] * (data[k] - xhatminus[k])
            P[k] = (1 - K[k]) * Pminus[k]

        return xhat

    @staticmethod
    def calculate_hurst(series: np.ndarray) -> float:
        """
        Hurst Üssü (Hurst Exponent) Hesabı.
        H < 0.5: Mean Reverting (Fiyat ortalamaya dönecek -> SHORT/LONG fırsatı)
        H > 0.5: Trending (Fiyat gitmeye devam edecek -> DOKUNMA)
        H = 0.5: Random Walk (Rastgele)
        """
        lags = range(2, 10)
        tau = [np.sqrt(np.std(np.subtract(series[lag:], series[:-lag]))) for lag in lags]
        
        # Hata önleme
        if len(tau) < 2 or np.any(np.isnan(tau)) or np.any(np.array(tau) == 0):
            return 0.5
            
        # Log-Log düzleminde eğim hesabı
        try:
            m = np.polyfit(np.log(lags), np.log(tau), 1)
            hurst = m[0] * 2.0
            return float(hurst)
        except:
            return 0.5

    @staticmethod
    def calculate_obi(bids, asks, depth=10) -> float:
        """
        Order Book Imbalance (OBI).
        Tahtanın ilk 'depth' seviyesindeki hacim dengesi.
        Pozitif: Alıcı baskın.
        Negatif: Satıcı baskın.
        """
        bid_vol = sum([b[1] for b in bids[:depth]])
        ask_vol = sum([a[1] for a in asks[:depth]])
        
        if (bid_vol + ask_vol) == 0: return 0.0
        
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        return imbalance

    @staticmethod
    def analyze_quantum(prices: np.ndarray, bids: list, asks: list) -> Optional[QuantData]:
        """
        Tüm ileri matematiği birleştirir.
        """
        if len(prices) < 30: return None

        # 1. KALMAN FİLTRESİ
        kalman_series = AdvancedQuantMath.kalman_filter(prices)
        kalman_price = kalman_series[-1]
        current_price = prices[-1]
        
        # Kalman Farkı: Fiyat, filtrelenmiş "gerçek" değerden ne kadar saptı?
        # Eğer fiyat Kalman'ın çok üstündeyse, "köpük"tür.
        kalman_diff_pct = (current_price - kalman_price) / kalman_price * 100

        # 2. KİNEMATİK (Hız ve İvme - Kalman verisi üzerinden)
        velocity = kalman_series[-1] - kalman_series[-2]
        acceleration = velocity - (kalman_series[-2] - kalman_series[-3])

        # 3. HURST EXPONENT (Kaos Analizi)
        # Son 50 mumluk fractal yapı
        hurst = AdvancedQuantMath.calculate_hurst(prices)

        # 4. ORDER BOOK IMBALANCE
        obi = AdvancedQuantMath.calculate_obi(bids, asks)

        # --- PUANLAMA MOTORU ---
        score = 0.0
        signal = None

        # Short Sinyali Mantığı (Pump Sonrası Dönüş)
        # 1. Fiyat Kalman'ın üzerinde (Köpük var)
        # 2. İvme negatif (Gaz kesildi)
        # 3. Hurst < 0.5 (Piyasa ortalamaya dönmek istiyor, trend değil)
        # 4. OBI Negatif (Satıcılar tahtaya yığıldı)
        
        if kalman_diff_pct > 0.3: # %0.3 köpük (1dk için yüksektir)
            base = 40
            
            if acceleration < 0: base += 15
            if velocity < 0: base += 10
            
            # Hurst Kritik Filtre: Trend varsa girme!
            if hurst < 0.45: 
                base += 20
            elif hurst > 0.60:
                base -= 50 # Ceza puanı (Güçlü trend, short açma!)
            
            # Tahta Analizi
            if obi < MIN_OBI_SCORE: base += 15 # Güçlü satıcı baskısı
            
            if base >= MIN_QUANT_SCORE:
                score = base
                signal = "SHORT"

        # Long mantığı simetrik olarak kurulabilir, şimdilik Short odaklı (Pump avcısı)
        elif kalman_diff_pct < -0.3:
            base = 40
            if acceleration > 0: base += 15
            if hurst < 0.45: base += 20
            if obi > abs(MIN_OBI_SCORE): base += 15
            
            if base >= MIN_QUANT_SCORE:
                score = base
                signal = "LONG"

        if signal:
            return QuantData(kalman_price, kalman_diff_pct, hurst, obi, velocity, acceleration, score, signal)
        
        return None

# ==============================
# 3) UYGULAMA
# ==============================

class QuantumScanner:
    def __init__(self):
        log("[INIT] Quantum Sniper (Kalman/Hurst/OBI) Başlatılıyor...")
        self.ex = ccxt_async.binanceusdm({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
        })
        self.symbols = []
        self.bot = Bot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

    async def refresh_symbols(self):
        try:
            m = await self.ex.load_markets(reload=True)
            # Hacmi yüksek ilk 50 coini al (Order book maliyetli olduğu için)
            all_syms = [d for d in m.values() if d.get("quote") == QUOTE and d.get("active")]
            # Hacme göre sırala
            sorted_syms = sorted(all_syms, key=lambda x: float(x['info'].get('volume', 0)), reverse=True)
            self.symbols = [x['symbol'] for x in sorted_syms[:50]] # Sadece top 50
            log(f"[REFRESH] Top {len(self.symbols)} yüksek hacimli coin izleniyor.")
        except Exception as e:
            log(f"[ERR] Refresh: {e}")

    async def process_symbol(self, symbol: str):
        try:
            # 1. MUM VERİLERİ (OHLCV)
            ohlcv = await self.ex.fetch_ohlcv(symbol, "1m", limit=60)
            if not ohlcv: return
            closes = np.array([x[4] for x in ohlcv], dtype=float)
            
            # Hızlı Ön Eleme (API Tasarrufu):
            # Eğer fiyat son 3 dakikada %0.5 oynamadıysa order book çekme.
            if (max(closes[-3:]) - min(closes[-3:])) / closes[-1] < 0.005:
                return

            # 2. ORDER BOOK (Sadece potansiyel varsa çek)
            ob = await self.ex.fetch_order_book(symbol, limit=20)
            bids = ob['bids']
            asks = ob['asks']

            # 3. QUANT ANALİZ
            q = AdvancedQuantMath.analyze_quantum(closes, bids, asks)

            if q and q.total_score >= MIN_QUANT_SCORE:
                # EMOJI VE RENKLER
                arrow = "🔴 SNIPER SHORT" if q.signal == "SHORT" else "🟢 SNIPER LONG"
                
                clean_sym = symbol.replace("/", "")
                
                msg = (
                    f"<b>{arrow} #{clean_sym}</b>\n"
                    f"Quant Score: <b>{q.total_score:.0f}/100</b>\n"
                    f"Current Price: <code>{closes[-1]:.4f}</code>\n"
                    f"Kalman ('True') Price: <code>{q.real_price:.4f}</code>\n\n"
                    f"<b>🔬 LAB VERİLERİ:</b>\n"
                    f"• <b>Hurst Exp:</b> <code>{q.hurst:.2f}</code>\n"
                    f"  <i>(0.5 altı=Dönüş İhtimali, Üstü=Trend)</i>\n"
                    f"• <b>Kalman Diff:</b> <code>%{q.kalman_diff:.2f}</code>\n"
                    f"• <b>OrderBook Imbalance:</b> <code>{q.obi:.2f}</code>\n"
                    f"  <i>(Negatif=Satıcı, Pozitif=Alıcı Baskın)</i>\n"
                    f"• <b>Acceleration:</b> <code>{q.acceleration:.5f}</code>\n\n"
                    f"⚠️ <i>Yapay zeka destekli istatistiksel arbitraj sinyali.</i>"
                )
                
                await self.send_telegram(msg)
                log(f"[SİNYAL] {symbol} Skor: {q.total_score}")

        except Exception as e:
            pass # Hataları yut (Speed run)

    async def send_telegram(self, text: str):
        if self.bot:
            try:
                await self.bot.send_message(TG_CHAT_ID, text, parse_mode="HTML")
            except: pass

    async def run(self):
        await self.refresh_symbols()
        log("Quantum Scanner Aktif.")
        while True:
            for i in range(0, len(self.symbols), MAX_CONCURRENT_SYMBOLS):
                batch = self.symbols[i : i + MAX_CONCURRENT_SYMBOLS]
                tasks = [self.process_symbol(s) for s in batch]
                await asyncio.gather(*tasks)
                await asyncio.sleep(1) # Rate limit koruması
            
            await asyncio.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    s = QuantumScanner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(s.run())