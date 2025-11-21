from indicators_2 import get_rsi_series,  debug_nw_point, nw_score_from_df
import os, math, json, asyncio, time, ssl, certifi
from collections import deque, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from dotenv import load_dotenv
import pandas as pd
import math # Dosyanın en başına ekle (math.ceil için)

load_dotenv()  # .env dosyasını okur

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_GROUP_ID")
MAX_MSG_PER_SEC    = float(os.getenv("MAX_MSG_PER_SEC", "25"))
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Istanbul")
FAPI_HTTP          = "https://fapi.binance.com"
# WEBSOCKET sürekli data geliyo
WS_URL_SECONDS     = "wss://fstream.binance.com/stream?streams=!miniTicker@arr"
INTERVALS = ['1m']
KLINE_LIMIT = 500
BATCH_SIZE = 50           # Tek seferde kaç coin için istek göndereceğiz? (50 iyi bir başlangıç)
SLEEP_BETWEEN_BATCHES = 15 # Her grup arasında kaç saniye bekleyeceğiz? (Saniye)
WS_URL_BASE = "wss://fstream.binance.com/stream?streams="
KLINE_COLUMNS = [
    'Open time', 'Open', 'High', 'Low', 'Close', 'Volume',
    'Close time', 'Quote asset volume', 'Number of trades',
    'Taker buy base asset volume', 'Taker buy quote asset volume', 'Ignore'
]


# BURADAKİ DF O SEMBOLÜN DFSİ OLUYO VE YAPTIGIN DEGİSİKLİK KALICI OLUYO GÜZEL
def calculate_indicators(df):
    try:
        # ...
        # 'rsi_from_closes' yerine 'get_rsi_series' kullanın
        df['RSI'] = get_rsi_series(df['Close'], length=14)
        # ...

        #debug_nw_point(df, -1, price_source='hl2')  # veya 'hlc3' ya da 'close'

        # tüm DF için score üretme
        mid, upper, lower, score = nw_score_from_df(df, price_source='hl2')
        df['NW_mid'] = mid
        df['NW_upper'] = upper
        df['NW_lower'] = lower
        df['NW_score'] = score


        # 'Stoch_K' ve 'Stoch_D' hesaplamalarınız...
        # (Not: stoch_rsi_from_closes fonksiyonunuzun da
        # tüm seriyi döndürdüğünden emin olun)

    except Exception as e:
        print(f"İndikatör hesaplama hatası: {e}")



async def get_all_futures_symbols(session):
    url = f"{FAPI_HTTP}/fapi/v1/exchangeInfo"
    try:
        # Hatalı satır kaldırıldı. 'session' artık 'main'den geleni kullanıyor.
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()

                symbols = [
                    s['symbol']
                    for s in data['symbols']
                    if s['status'] == 'TRADING'
                ]
                return symbols
            else:
                print(f"Sembol listesi alınırken hata oluştu. Status Code: {response.status}")
                print(await response.text())
                return None

    except Exception as e:
        print(f"get_all_futures_symbols fonksiyonunda hata: {e}")
        return None

async def fetch_klines(session, symbol, interval, limit):
    """
    Belirli bir sembol için kline verilerini çeker ve bir DataFrame'e dönüştürür.
    """
    url = f"{FAPI_HTTP}/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }

    try:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if not data:
                    return None

                df = pd.DataFrame(data, columns=KLINE_COLUMNS)
                df = df[['Open time', 'Open', 'High', 'Low', 'Close', 'Volume']].copy()
                df['Open time'] = pd.to_datetime(df['Open time'], unit='ms')

                numeric_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
                df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')

                df.set_index('Open time', inplace=True)

                return df
            else:
                return None
    except Exception as e:
        return None


def handle_kline_message(all_data, data):
    """
    Gelen kline mesajını işler, ilgili DataFrame'i günceller.
    (Düzeltilmiş FIFO / Güncelleme mantığı ile)
    """

    if 'k' in data and data['k']['x']:  # 'x': true (mum kapandı)
        kline = data['k']
        symbol = kline['s']

        if symbol in all_data:

            open_time = pd.to_datetime(kline['t'], unit='ms')

            new_data = {
                'Open': pd.to_numeric(kline['o']),
                'High': pd.to_numeric(kline['h']),
                'Low': pd.to_numeric(kline['l']),
                'Close': pd.to_numeric(kline['c']),
                'Volume': pd.to_numeric(kline['v'])
            }

            new_row_df = pd.DataFrame([new_data], index=[open_time])
            new_row_df.index.name = 'Open time'

            df = all_data[symbol]

            # -----------------------------------------------------------------
            # <<< YENİ GÜNCELLENMİŞ MANTIK (FIFO DÜZELTMESİ) >>>
            # -----------------------------------------------------------------

            # Gelen mumun zamanı (index) DataFrame'de zaten var mı?
            if open_time in df.index:

                # EVET VARSA: Bu, HTTP'den gelen son 'açık' mumun 'kapalı' halidir.
                # FIFO (baştan silme) YAPMA! Sadece son satırı güncelle.
                # (En güvenli yol: son satırı sil ve yenisini ekle)

                df = df.drop(df.index[-1])  # HTTP'den gelen son (açık) satırı sil
                df = pd.concat([df, new_row_df])  # WS'ten gelen (kapalı) satırı ekle

                # print(f"GÜNCELLEME (HTTP Sync): {symbol} son mumu kapandı: {open_time}")

            else:
                # HAYIR YOKSA: Bu tamamen yeni bir mumdur (örn: 10:20, 10:21...)
                # Normal FIFO (sona ekle, baştan sil) mantığını uygula.

                # 6. YENİ MUMU EKLE (sona)
                df = pd.concat([df, new_row_df])

                # 7. ESKİ MUMU SİL (en baştan)
                df = df.iloc[1:]

                # print(f"GÜNCELLEME (FIFO): {symbol} DataFrame güncellendi. Yeni mum: {open_time}")

            # -----------------------------------------------------------------
            # <<< GÜNCELLEME SONU >>>
            # -----------------------------------------------------------------

            # 8. Güncellenmiş (veya FIFO uygulanmış) DataFrame'i sözlüğe geri koy
            all_data[symbol] = df

            # 9. İndikatörleri yeniden hesapla
            #calculate_indicators(all_data[symbol])

            # KANIT: (Bu satırı açarak indikatörlerin son halini görebilirsiniz)
            # print(all_Ddata[symbol][['Close', 'RSI']].tail())

            # NOT: Artık bu print'i dışarıya (calculate_indicators sonrasına) taşıdığımız
            # için bir önceki cevaptaki print satırını silebilirsiniz.
            calculate_indicators(all_data[symbol])

            print(f"GÜNCELLEME: {symbol} DataFrame güncellendi. Yeni mum: {open_time}")
            print(all_data[symbol].tail(5))

async def websocket_listener(all_data, symbol_list):
    """
    (SONSUZ DÖNGÜ) Tüm sembollerin kline_1m akışına bağlanır ve dinler.
    """

    # 1. Tüm semboller için ('btcusdt@kline_1m/ethusdt@kline_1m/...') URL'i oluştur
    streams = [f"{symbol.lower()}@kline_{INTERVALS[0]}" for symbol in symbol_list]
    ws_url = WS_URL_BASE + "/".join(streams)
    print(f"\n{len(streams)} adet WebSocket akışına bağlanılıyor...")

    # 2. aiohttp session'ı tekrar kullan
    async with aiohttp.ClientSession() as session:

        # 3. Bu Dış 'while True' döngüsü, bağlantı koptuğunda yeniden bağlanmayı sağlar
        while True:
            print("WebSocket bağlantısı kuruluyor...")
            try:
                async with session.ws_connect(ws_url, max_msg_size=0) as ws:
                    print("WebSocket bağlantısı başarılı. Canlı veriler dinleniyor...")

                    # 4. Bu İç 'async for' döngüsü, mesajları dinleyen asıl döngüdür
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)

                            # 'stream' anahtarı varsa, bu toplu bir mesajdır
                            if 'stream' in data and 'data' in data:
                                # Gelen mesajı DataFrame'i güncellemesi için fonksiyona yolla
                                handle_kline_message(all_data, data['data'])

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            print(f"WebSocket hatası: {ws.exception()}")
                            break  # Hata durumunda iç döngüden çık (dış döngü tekrar bağlanır)

            except aiohttp.ClientConnectorError as e:
                print(f"Bağlantı hatası: {e}. 10 saniye sonra tekrar denenecek...")
                await asyncio.sleep(10)
            except Exception as e:
                print(f"Beklenmedik WebSocket hatası: {e}. 10 saniye sonra tekrar denenecek...")
                await asyncio.sleep(10)

async def main():
    start_time = time.time()

    # --- 1. AŞAMA: TARİХSEL VERİYİ YÜKLEME (GÜVENLİK ÖNLEMLİ) ---

    all_data = {}
    symbol_list = []
    valid_symbols_for_ws = []  # WS için geçerli semboller

    async with aiohttp.ClientSession() as session:
        print("Semboller çekiliyor...")
        symbol_list = await get_all_futures_symbols(session)

        if not symbol_list:
            print("Semboller çekilemedi, bot durduruluyor.")
            return

        print(f"Toplam {len(symbol_list)} sembol çekildi.")
        print(f"Veri çekme işlemi {BATCH_SIZE} coinlik gruplar halinde yapılacak...")

        # Tüm sonuçları toplamak için geçici bir sözlük
        all_results = {}

        # --- TÜM SEMBOLLERİ GRUPLARA AYIRARAK İSTEK GÖNDER ---
        total_batches = math.ceil(len(symbol_list) / BATCH_SIZE)  # Toplam grup sayısını hesapla
        for i in range(0, len(symbol_list), BATCH_SIZE):
            batch_symbols = symbol_list[i: i + BATCH_SIZE]
            current_batch_num = (i // BATCH_SIZE) + 1
            print(f"\nİşleniyor: Grup {current_batch_num} / {total_batches} ({len(batch_symbols)} sembol)")

            # --- Grup için Kline Verisini Paralel Çek ---
            interval = INTERVALS[0]  # Sadece 1m çektiğimizi varsayıyorum
            tasks = [asyncio.ensure_future(fetch_klines(session, s, interval, KLINE_LIMIT)) for s in batch_symbols]
            print(f"  -> {interval} ({KLINE_LIMIT} mum) verileri çekiliyor...")
            results_batch = await asyncio.gather(*tasks, return_exceptions=True)

            # --- Sonuçları Geçici Sözlüğe Ekle ---
            for j, result in enumerate(results_batch):
                symbol = batch_symbols[j]
                all_results[symbol] = result  # Başarılı veya Hata, fark etmez

            # --- GRUPLAR ARASINDA BEKLE (API LİMİTİ İÇİN) ---
            if current_batch_num < total_batches:  # Son gruptan sonra bekleme
                print(f"  Grup tamamlandı. {SLEEP_BETWEEN_BATCHES} saniye bekleniyor...")
                await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
            else:
                print("  Son grup tamamlandı.")



        # --- TÜM ÇEKİLEN VERİLERİ İŞLE ---
        print("\nTüm veriler çekildi, DataFrame'ler oluşturuluyor ve işleniyor...")
        success_count = 0
        for symbol in symbol_list:  # Orijinal listedeki sırayla işleyelim
            result = all_results.get(symbol)  # Çekilen sonucu al

            if isinstance(result, pd.DataFrame) and not result.empty:
                calculate_indicators(result)
                all_data[symbol] = result
                valid_symbols_for_ws.append(symbol)
                success_count += 1
            elif isinstance(result, Exception):
                print(f"Hata ({interval} - {symbol}): {result}")
                # else: # result = None ise (API'den boş döndüyse)
                #     print(f"Uyarı ({interval} - {symbol}): Veri alınamadı (boş yanıt).")
    end_time = time.time()
    print("\n--- 1. AŞAMA (Tarihsel Yükleme) Tamamlandı ---")
    print(f"Geçen süre: {end_time - start_time:.2f} saniye")
    print(f"Başarıyla alınan DataFrame: {success_count} / {len(symbol_list)}")

    if 'BTCUSDT' in all_data:
        print("\n--- ÖRNEK YÜKLENEN VERİ: all_data['BTCUSDT'] (SON 5) ---")
        print(all_data['BTCUSDT'].tail())

    # --- 2. AŞAMA: CANLI GÜNCELLEME (SONSUZ DÖNGÜ) ---

    if all_data:  # Eğer en az 1 adet bile DataFrame yüklendiyse
        # Doldurulmuş 'all_data' ve geçerli 'symbol_list' ile
        # sonsuz dinleme döngüsünü başlat.
        # Bu fonksiyon (websocket_listener) siz botu durdurana kadar asla bitmeyecek.
        await websocket_listener(all_data, valid_symbols_for_ws)
    else:
        print("Hiçbir sembol için veri çekilemedi. Bot başlatılamıyor.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot kullanıcı tarafından durduruldu.")