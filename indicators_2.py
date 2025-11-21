import pandas as pd
import numpy as np
from typing import List  # Artık 'List'e gerek yok ama kalsın


def get_rsi_series(closes: pd.Series, length: int = 14) -> pd.Series:
    """
    Bir pandas Serisi (Close sütunu) alır ve tüm RSI serisini (yeni bir sütun)
    TradingView tarzı (Wilder's RMA) olarak hesaplar.
    """
    if len(closes) < length + 1:
        # Yeterli veri yoksa, tamamı NaN olan bir seri döndür
        return pd.Series([float("nan")] * len(closes), index=closes.index)

    # 1. 'closes' zaten bir pd.Series, tekrar oluşturmaya gerek yok.
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # 2. 'min_periods=length' yerine 0 kullanmak, ilk periyotları
    #    NaN yapmak yerine hesaplamaya daha erken başlamasını sağlar (RMA doğası)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=0).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=0).mean()

    # 3. Sıfıra bölme hatasını önle (tüm sütun için)
    rs = avg_gain / avg_loss.replace(0, 1e-9)  # Çok küçük bir sayıya böl

    rsi = 100 - (100 / (1 + rs))

    # İlk 'length' periyodu NaN olarak ayarla, çünkü o noktada
    # yeterli veri yok (isteğe bağlı ama doğru bir yaklaşımdır)
    rsi.iloc[0:length] = float("nan")

    # 4. Sadece son değeri (.iloc[-1]) değil, TÜM SERİ'yi döndür
    return rsi


def _price_source(df, source: str):
    """
    source: 'close' | 'hl2' | 'hlc3' | 'typical'
    df must contain 'High','Low','Close'
    """
    source = source.lower()
    if source == 'close':
        return df['Close']
    if source == 'hl2':
        return (df['High'] + df['Low']) / 2
    if source == 'hlc3' or source == 'typical':
        return (df['High'] + df['Low'] + df['Close']) / 3
    raise ValueError("Unknown price source")

def nadaraya_watson(df: pd.DataFrame, price_source: str = 'close', bandwidth: float = 20.0, vol_period: int = 20, vol_mult: float = 2.0):
    """
    df: DataFrame with High, Low, Close
    price_source: 'close' (default), 'hl2', 'hlc3'
    returns mid, upper, lower (pd.Series aligned with df.index)
    """
    price = _price_source(df, price_source).astype(float)
    x = np.arange(len(price))
    y = price.values

    def kernel(dist):
        return np.exp(-(dist ** 2) / (2 * (bandwidth ** 2)))

    mid = np.zeros_like(y)
    for i in range(len(y)):
        w = kernel(x - x[i])
        s = np.sum(w)
        if s == 0:
            mid[i] = y[i]
        else:
            mid[i] = np.sum((w / s) * y)

    mid_series = pd.Series(mid, index=price.index)

    # volatilite kafası: varsayılan rolling std; TV farklı ise vol_mult/period ile ayarla
    vol = price.rolling(vol_period).std().bfill()
    upper = mid_series + vol_mult * vol
    lower = mid_series - vol_mult * vol

    return mid_series, upper, lower

def nw_score_from_df(df: pd.DataFrame, price_source: str = 'close', bandwidth: float = 20.0, vol_period: int = 20, vol_mult: float = 2.0):
    """
    Kolay kullanım: df ver -> NW mid/upper/lower + score döner
    """
    mid, upper, lower = nadaraya_watson(df, price_source=price_source, bandwidth=bandwidth, vol_period=vol_period, vol_mult=vol_mult)
    price = _price_source(df, price_source)
    score = nw_score(price, mid, upper, lower)
    return mid, upper, lower, score

def nw_score(price: pd.Series, mid: pd.Series, upper: pd.Series, lower: pd.Series):
    score_list = []
    for c, m, u, l in zip(price.values, mid.values, upper.values, lower.values):
        if any(pd.isna(x) for x in (c, m, u, l)):
            score_list.append(np.nan)
            continue

        up_range = u - m
        down_range = m - l

        # mid == upper veya mid == lower ise overflow/underflow direkt 1/-1 yerine 1+ veya -1- küçük değer verelim
        if up_range == 0:
            up_range = 1e-6
        if down_range == 0:
            down_range = 1e-6

        if c > u:
            overflow = 1.0 + (c - u) / up_range
            score_list.append(overflow)
        elif c < l:
            underflow = -1.0 - (l - c) / down_range
            score_list.append(underflow)
        else:
            if c >= m:
                val = (c - m) / up_range
                score_list.append(val)
            else:
                val = (c - m) / down_range
                score_list.append(val)

    return pd.Series(score_list, index=price.index)

# -------------------------
# DEBUG HELP: belirli bir indexi incele
# -------------------------
def debug_nw_point(df: pd.DataFrame, idx, price_source: str = 'close', bandwidth: float = 20.0, vol_period: int = 20, vol_mult: float = 2.0):
    """
    idx: index label (datetime) veya integer pozisyon
    Yazdirir: price, mid, upper, lower, score — ayni mumda ne hesaplandigini gorebilirsin.
    """
    mid, upper, lower = nadaraya_watson(df, price_source=price_source, bandwidth=bandwidth, vol_period=vol_period, vol_mult=vol_mult)
    # idx olabilir integer veya index label
    if isinstance(idx, int):
        pos = idx
        label = df.index[pos]
    else:
        label = idx
        pos = df.index.get_loc(idx)

    price = _price_source(df, price_source).iloc[pos]
    m = mid.iloc[pos]
    u = upper.iloc[pos]
    l = lower.iloc[pos]
    s = nw_score(pd.Series([price], index=[label]), pd.Series([m], index=[label]), pd.Series([u], index=[label]), pd.Series([l], index=[label])).iloc[0]
    """
    print(f"Index: {label}")
    print(f"Price ({price_source}): {price}")
    print(f"Mid: {m}")
    print(f"Upper: {u}")
    print(f"Lower: {l}")
    print(f"Score: {s}")
    # Ekstra: candle gövdesinin ortası
    hl2 = (df['High'].iloc[pos] + df['Low'].iloc[pos]) / 2
    print(f"HL2 (candle ortası): {hl2}")
    print(f"Close: {df['Close'].iloc[pos]}")
    """

from datetime import datetime
ts_ms = 1762668538249
dt = datetime.utcfromtimestamp(ts_ms / 1000)
print(dt)
