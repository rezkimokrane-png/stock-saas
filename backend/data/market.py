import os, json, time
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

try:
    from curl_cffi.requests import Session as CurlSession
    CURL_OK = True
except ImportError:
    CURL_OK = False

def _make_curl_session():
    if not CURL_OK:
        return None
    return CurlSession(impersonate="chrome120")

import yfinance as yf

def _ticker(symbol: str):
    session = _make_curl_session()
    if session:
        return yf.Ticker(symbol, session=session)
    return yf.Ticker(symbol)

# ── Cache ─────────────────────────────────────────────────────
try:
    import redis
    _redis = redis.from_url(os.getenv("REDIS_URL","redis://localhost:6379"), decode_responses=True)
    _redis.ping()
    REDIS_OK = True
except Exception:
    _redis = None
    REDIS_OK = False

CACHE_TTL = 300
_mem_cache: dict = {}

def _cache_get(key):
    if REDIS_OK:
        try:
            v = _redis.get(key)
            return json.loads(v) if v else None
        except Exception:
            pass
    entry = _mem_cache.get(key)
    if entry:
        value, exp = entry
        if time.time() < exp:
            return value
        del _mem_cache[key]
    return None

def _cache_set(key, value, ttl=CACHE_TTL):
    if REDIS_OK:
        try:
            _redis.setex(key, ttl, json.dumps(value))
            return
        except Exception:
            pass
    _mem_cache[key] = (value, time.time() + ttl)

_FAILED = "__FAILED__"

def _retry(fn, attempts=3, delay=2.0):
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            time.sleep(delay * (i + 1))
    raise last_err

# ── Stooq symbol ─────────────────────────────────────────────
def _stooq_sym(symbol: str) -> str:
    sym = symbol.upper()
    if sym.endswith(".PA"): return sym.replace(".PA", ".FR")
    if sym.endswith(".L"):  return sym.replace(".L", ".UK")
    return sym

# ── OHLCV via Stooq ──────────────────────────────────────────
def _ohlcv_stooq(symbol: str) -> pd.DataFrame:
    try:
        s = _stooq_sym(symbol)
        url = f"https://stooq.com/q/d/l/?s={s.lower()}&i=d"
        df = pd.read_csv(url, parse_dates=["Date"], index_col="Date")
        if df.empty or len(df) < 5:
            return pd.DataFrame()
        df.columns = [c.title() for c in df.columns]
        df = df.sort_index()
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=1)
        df = df[df.index >= cutoff]
        print(f"[stooq ohlcv] {symbol} OK — {len(df)} rows")
        return df
    except Exception as e:
        print(f"[stooq ohlcv] {symbol} error: {e}")
        return pd.DataFrame()

# ── Info via Stooq (reconstruit depuis OHLCV) ─────────────────
def _info_stooq(symbol: str) -> dict:
    """Construit un dict info minimal depuis les données Stooq."""
    try:
        s = _stooq_sym(symbol)
        url = f"https://stooq.com/q/d/l/?s={s.lower()}&i=d"
        df = pd.read_csv(url, parse_dates=["Date"], index_col="Date")
        if df.empty:
            return {}
        df.columns = [c.title() for c in df.columns]
        df = df.sort_index()

        price   = float(df["Close"].iloc[-1])
        prev    = float(df["Close"].iloc[-2]) if len(df) > 1 else price
        chg_pct = round((price - prev) / prev * 100, 2) if prev else 0
        high52  = round(float(df["Close"].tail(252).max()), 2)
        low52   = round(float(df["Close"].tail(252).min()), 2)

        # Devise selon suffixe
        currency = "USD"
        if symbol.endswith(".PA"): currency = "EUR"
        elif symbol.endswith(".L"): currency = "GBP"

        print(f"[stooq info] {symbol} price={price} chg={chg_pct}%")
        return {
            "symbol":             symbol,
            "longName":           symbol,
            "shortName":          symbol,
            "regularMarketPrice": price,
            "currentPrice":       price,
            "previousClose":      prev,
            "regularMarketChangePercent": chg_pct,
            "currency":           currency,
            "fiftyTwoWeekHigh":   high52,
            "fiftyTwoWeekLow":    low52,
            "exchange":           "Stooq",
        }
    except Exception as e:
        print(f"[stooq info] {symbol} error: {e}")
        return {}

# ── OHLCV public ─────────────────────────────────────────────
def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}:{interval}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return pd.DataFrame()
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df

    df = pd.DataFrame()

    # 1. yfinance + curl_cffi
    try:
        t  = _ticker(symbol)
        df = _retry(lambda: t.history(period=period, interval=interval))
        if not df.empty:
            print(f"[yfinance ohlcv] {symbol} OK")
    except Exception as e:
        print(f"[yfinance ohlcv] {symbol} failed: {e}")

    # 2. Stooq fallback
    if df.empty:
        df = _ohlcv_stooq(symbol)

    if df.empty:
        _cache_set(key, _FAILED, ttl=20)
        return df

    try:
        idx = df.reset_index()
        date_col = idx.columns[0]
        serialized = idx.assign(
            **{date_col: idx[date_col].astype(str)}
        ).set_index(date_col).to_dict()
        _cache_set(key, serialized, ttl=120)
    except Exception as e:
        print(f"[cache ohlcv] serialize error: {e}")

    return df

# ── Info public ──────────────────────────────────────────────
def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached

    info = {}

    # 1. yfinance + curl_cffi
    try:
        t    = _ticker(symbol)
        info = _retry(lambda: t.info)
        if info and isinstance(info, dict) and (
            info.get("regularMarketPrice") or info.get("currentPrice")
        ):
            print(f"[yfinance info] {symbol} OK")
            _cache_set(key, info, ttl=3600)
            return info
    except Exception as e:
        print(f"[yfinance info] {symbol} failed: {e}")

    # 2. Stooq fallback
    info = _info_stooq(symbol)
    if info:
        _cache_set(key, info, ttl=300)
        return info

    _cache_set(key, _FAILED, ttl=20)
    return {}

# ── Prix public ───────────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
