import os, json, hashlib
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ── Cache Redis optionnel (dégradé gracieusement si absent) ──
try:
    import redis
    _redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    _redis.ping()
    REDIS_OK = True
except Exception:
    _redis   = None
    REDIS_OK = False

CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str):
    if not REDIS_OK:
        return None
    try:
        v = _redis.get(key)
        return json.loads(v) if v else None
    except Exception:
        return None


def _cache_set(key: str, value, ttl=CACHE_TTL):
    if not REDIS_OK:
        return
    try:
        _redis.setex(key, ttl, json.dumps(value))
    except Exception:
        pass


# ── Données OHLCV ────────────────────────────────────────────
def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}:{interval}"
    cached = _cache_get(key)
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df
    df = yf.Ticker(symbol).history(period=period, interval=interval)
    if df.empty:
        return df
    _cache_set(key, df.reset_index().assign(
        Date=lambda x: x["Date"].dt.strftime("%Y-%m-%d")
    ).set_index("Date").to_dict(), ttl=120)
    return df


# ── Infos fondamentales ───────────────────────────────────────
def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached:
        return cached
    info = yf.Ticker(symbol).info
    _cache_set(key, info, ttl=3600)
    return info


# ── Prix temps réel ───────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
