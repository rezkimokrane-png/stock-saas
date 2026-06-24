import os, json, time, random
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ── Headers pour contourner le blocage Yahoo Finance ──────────
# Yahoo bloque les IPs de datacenter cloud. On simule un vrai navigateur.
import requests
from requests import Session

def _make_session():
    s = Session()
    s.headers.update({
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    return s

# ── Cache Redis optionnel ────────────────────────────────────
try:
    import redis
    _redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    _redis.ping()
    REDIS_OK = True
except Exception:
    _redis = None
    REDIS_OK = False

CACHE_TTL = 300
_mem_cache: dict = {}

def _cache_get(key: str):
    if REDIS_OK:
        try:
            v = _redis.get(key)
            return json.loads(v) if v else None
        except Exception:
            pass
    entry = _mem_cache.get(key)
    if entry:
        value, expires_at = entry
        if time.time() < expires_at:
            return value
        del _mem_cache[key]
    return None

def _cache_set(key: str, value, ttl=CACHE_TTL):
    if REDIS_OK:
        try:
            _redis.setex(key, ttl, json.dumps(value))
            return
        except Exception:
            pass
    _mem_cache[key] = (value, time.time() + ttl)

def _retry(fn, attempts=3, delay=2.0):
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            time.sleep(delay * (i + 1))
    raise last_err

_FAILED = "__FAILED__"

# ── Données OHLCV ────────────────────────────────────────────
def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}:{interval}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return pd.DataFrame()
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df

    try:
        session = _make_session()
        ticker  = yf.Ticker(symbol, session=session)
        df      = _retry(lambda: ticker.history(period=period, interval=interval))
    except Exception as e:
        print(f"[get_ohlcv] {symbol} failed: {e}")
        _cache_set(key, _FAILED, ttl=20)
        return pd.DataFrame()

    if df.empty:
        _cache_set(key, _FAILED, ttl=20)
        return df

    _cache_set(key, df.reset_index().assign(
        Date=lambda x: x["Date"].dt.strftime("%Y-%m-%d")
    ).set_index("Date").to_dict(), ttl=120)
    return df

# ── Infos fondamentales ───────────────────────────────────────
def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached

    try:
        session = _make_session()
        ticker  = yf.Ticker(symbol, session=session)
        info    = _retry(lambda: ticker.info)
    except Exception as e:
        print(f"[get_info] {symbol} failed: {e}")
        _cache_set(key, _FAILED, ttl=20)
        return {}

    if not info or not isinstance(info, dict):
        _cache_set(key, _FAILED, ttl=20)
        return {}

    _cache_set(key, info, ttl=3600)
    return info

# ── Prix temps réel ───────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
