# 🚀 Ultimate Reversal Engine (High-Frequency Crypto Bot)

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Binance](https://img.shields.io/badge/Exchange-Binance%20Futures-yellow)
![Asyncio](https://img.shields.io/badge/Concurrency-Asyncio%20%2F%20Uvloop-green)
![Status](https://img.shields.io/badge/Status-Production%20Ready-red)

**Ultimate Reversal Engine**, Binance USDT-M Futures piyasasında çalışan, matematiksel modellere dayalı asenkron bir algoritmik ticaret botudur. Standart indikatörler (RSI, MACD vb.) yerine; **istatistiksel sapmalar (Z-Score), fiyat kinematiği (hız/ivme) ve piyasa mikro yapısı (Order Book/Wick analizi)** üzerine kuruludur.

---

## 🎯 Projenin Amacı ve Felsefesi

Kripto para piyasalarında volatilite yüksektir ve standart teknik analiz araçları genellikle gecikmeli (lagging) sinyaller üretir. Bu projenin temel amacı:
1.  **Gürültüyü Filtrelemek:** Piyasadaki rastgele hareketleri (Noise) matematiksel olarak elemek.
2.  **Karşıt Yatırım (Mean Reversion):** Fiyatın ortalamadan (VWAP) aşırı saptığı "Pump" ve "Dump" anlarını yakalamak.
3.  **Sniper Yaklaşımı:** Sürekli işlem açmak yerine, sadece olasılığı en yüksek (%80+) kurulumları beklemek.

> *"Piyasa her zaman haklıdır, ancak fiyat her zaman doğru değildir. Biz fiyatın doğru olmadığı o kısa anları (anomalileri) avlıyoruz."*

---

## ⚙️ Teknik Mimari ve Kullanılan Teknolojiler

Bu proje, performans ve hız odaklı modern bir Python yığını üzerine inşa edilmiştir:

* **Core:** `Python 3.11+`
* **Concurrency:** `asyncio` ve `uvloop` (Standart döngüden 2-3 kat daha hızlı event loop).
* **Data Stream:** `websockets` ile Binance `!miniTicker` ve `kline` verisi (Canlı/Real-time).
* **Math Engine:** `NumPy` ile vektörel hesaplamalar (Polinom regresyonu, Standart Sapma, EMA).
* **Notification:** Telegram Bot API entegrasyonu.

---

## 🧠 Algoritmik Mantık: "HİBRİT PUANLAMA SİSTEMİ"

Geleneksel "If-Else" mantığı yerine, piyasa koşullarına dinamik uyum sağlayan bir **Puanlama (Scoring) Sistemi** geliştirilmiştir. Bir işlemin açılması için algoritmanın **100 üzerinden en az 70 puan** toplaması gerekir.

### Puanlama Kriterleri:

1.  **📊 VWAP Sapması (+30 Puan):** Fiyatın, hacim ağırlıklı ortalamadan (VWAP) istatistiksel olarak kopması (Stretch).
2.  **📈 Dinamik Z-Score (+20 Puan):** Fiyatın, kendi standart sapma geçmişine göre "Extreme" bölgesinde (2.5σ+) olması.
3.  **🕯️ Wick (Fitil) Teyidi (+25 Puan):** Fiyatın tepe veya dipte reddedildiğini gösteren uzun fitiller (Rejection Candle).
4.  **📉 Hacim Uyumsuzluğu (+25 Puan):** Fiyat yükselirken hacmin düşmesi (Momentum kaybı / Exhaustion).
5.  **🏎️ Kinematik Kırılım (+15 Puan):** 2. Dereceden Polinom Regresyonu ile hesaplanan fiyat eğrisinin (Curvature) kırılması.

### Ek Filtreler (Safety Layers):
* **Trend Filtresi:** 1H ve 5m EMA karşılaştırması ile "Trendin tersine işlem açma" kuralı.
* **Debounce:** Sinyalin anlık bir "iğne" değil, kararlı bir hareket olduğunun teyidi.
* **Likidite Kontrolü:** Düşük hacimli "çöp" coinlerin filtrelenmesi.

---

## 🚀 Kurulum ve Çalıştırma

Projeyi yerel ortamınızda çalıştırmak için:

1.  **Repoyu Klonlayın:**
    ```bash
    git clone [https://github.com/kullaniciadi/ultimate-reversal-engine.git](https://github.com/kullaniciadi/ultimate-reversal-engine.git)
    cd ultimate-reversal-engine
    ```

2.  **Gereksinimleri Yükleyin:**
    ```bash
    pip install -r requirements.txt
    # M3 Mac veya Linux için performans artışı:
    pip install uvloop
    ```

3.  **Environment Değişkenlerini Ayarlayın:**
    `.env` dosyası oluşturun veya kod içerisindeki API anahtarlarını güncelleyin (Güvenlik için .env önerilir).

4.  **Başlatın:**
    ```bash
    python main.py
    ```

---

## 📊 Geliştirme Süreci (DevLog)

Bu proje 3 ana evrim geçirdi:
* **V1 (Basic Reversal):** Sadece Z-Score kullanan basit yapı. Çok fazla hatalı sinyal (Fakeout) üretiyordu.
* **V2 (Strict Sniper):** Çok katı kurallar eklendi. Güvenliydi ancak sinyal sayısı çok azdı (Günde 1-2).
* **V3 (Hybrid Scoring - Current):** "Katı Kurallar" yerine "Puanlama" sistemine geçildi. Hacim uyumsuzluğu (Volume Divergence) ve Wick analizi eklenerek esneklik ve kalite dengelendi.

---

## ⚠️ Yasal Uyarı

Bu yazılım eğitim ve araştırma amaçlı geliştirilmiştir. Kripto para piyasaları yüksek risk içerir. Bu botun ürettiği sinyaller yatırım tavsiyesi değildir. Oluşabilecek finansal kayıplardan geliştirici sorumlu tutulamaz.

---

**Geliştirici:** Edip  
*Yazılım Mühendisliği Öğrencisi & Algoritmik Trader*