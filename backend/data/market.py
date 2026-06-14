import os, json, time
import requests
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ── Session avec en-têtes navigateur (réduit le blocage Yahoo) ──
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

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


def _retry(fn, attempts=3, delay=1.5):
    """Réessaie en cas d'erreur transitoire (rate limit Yahoo, JSON cassé)."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            time.sleep(delay * (i + 1))
    raise last_err


# ── Données OHLCV ────────────────────────────────────────────
def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}:{interval}"
    cached = _cache_get(key)
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df

    try:
        df = _retry(lambda: yf.Ticker(symbol, session=_session).history(period=period, interval=interval))
    except Exception as e:
        print(f"[get_ohlcv] {symbol} failed: {e}")
        return pd.DataFrame()

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

    try:
        info = _retry(lambda: yf.Ticker(symbol, session=_session).info)
    except Exception as e:
        print(f"[get_info] {symbol} failed: {e}")
        return {}

    if not info or not isinstance(info, dict):
        return {}

    _cache_set(key, info, ttl=3600)
    return info


# ── Prix temps réel ───────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
