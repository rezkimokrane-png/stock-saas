import os, json, time
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ── curl_cffi session (imite TLS fingerprint Chrome) ─────────
# C'est la seule méthode fiable pour contourner le blocage
# Yahoo Finance sur les IPs de datacenter (Render, AWS, etc.)
try:
    from curl_cffi.requests import Session as CurlSession
    CURL_OK = True
except ImportError:
    CURL_OK = False

def _make_curl_session():
    """Session curl_cffi qui imite Chrome 120 — passe le blocage TLS Yahoo."""
    if not CURL_OK:
        return None
    s = CurlSession(impersonate="chrome120")
    return s

# ── yfinance avec session curl_cffi ──────────────────────────
import yfinance as yf

def _ticker(symbol: str):
    """Crée un Ticker yfinance avec session curl_cffi si disponible."""
    session = _make_curl_session()
    if session:
        return yf.Ticker(symbol, session=session)
    return yf.Ticker(symbol)

# ── Cache Redis optionnel ─────────────────────────────────────
try:
    import redis
    _redis = redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379"),
        decode_responses=True
    )
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

# ── Données OHLCV ─────────────────────────────────────────────
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
        t = _ticker(symbol)
        df = _retry(lambda: t.history(period=period, interval=interval))
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
        t = _ticker(symbol)
        info = _retry(lambda: t.info)
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
