import os, json, time
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ── curl_cffi ────────────────────────────────────────────────
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

# ── Fallback Stooq (données OHLCV uniquement) ─────────────────
def _stooq_symbol(symbol: str) -> str:
    """Convertit le ticker Yahoo vers format Stooq."""
    sym = symbol.upper()
    # Actions françaises : MC.PA → MC.FR
    if sym.endswith(".PA"):
        return sym.replace(".PA", ".FR")
    # Actions US : pas de suffixe
    return sym

def _get_ohlcv_stooq(symbol: str) -> pd.DataFrame:
    """Fallback Stooq — pas de blocage IP, données OHLCV fiables."""
    try:
        stooq_sym = _stooq_symbol(symbol)
        df = pd.read_csv(
            f"https://stooq.com/q/d/l/?s={stooq_sym.lower()}&i=d",
            parse_dates=["Date"],
            index_col="Date"
        )
        if df.empty or len(df) < 10:
            return pd.DataFrame()
        # Renomme les colonnes au format yfinance
        df.columns = [c.title() for c in df.columns]
        df = df.sort_index()
        # Garde seulement la dernière année
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=1)
        df = df[df.index >= cutoff]
        print(f"[stooq] {symbol} OK — {len(df)} séances")
        return df
    except Exception as e:
        print(f"[stooq] {symbol} failed: {e}")
        return pd.DataFrame()

# ── Fallback info via Stooq (prix seulement) ─────────────────
def _get_price_stooq(symbol: str) -> float | None:
    """Récupère le prix actuel via Stooq."""
    try:
        stooq_sym = _stooq_symbol(symbol)
        df = pd.read_csv(
            f"https://stooq.com/q/d/l/?s={stooq_sym.lower()}&i=d",
            parse_dates=["Date"],
            index_col="Date"
        )
        if df.empty:
            return None
        df.columns = [c.title() for c in df.columns]
        return float(df["Close"].iloc[-1])
    except Exception:
        return None

# ── OHLCV principal ───────────────────────────────────────────
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

    # Tentative 1 : yfinance + curl_cffi
    try:
        t = _ticker(symbol)
        df = _retry(lambda: t.history(period=period, interval=interval))
        if not df.empty:
            print(f"[yfinance] {symbol} OK")
    except Exception as e:
        print(f"[yfinance] {symbol} failed: {e}")

    # Tentative 2 : Stooq si yfinance échoue
    if df.empty:
        print(f"[fallback] Tentative Stooq pour {symbol}")
        df = _get_ohlcv_stooq(symbol)

    if df.empty:
        _cache_set(key, _FAILED, ttl=20)
        return df

    _cache_set(key, df.reset_index().assign(
        Date=lambda x: x.iloc[:, 0].astype(str) if "Date" not in df.reset_index().columns
        else x["Date"].astype(str) if x["Date"].dtype == "object"
        else x["Date"].dt.strftime("%Y-%m-%d")
    ).set_index("Date").to_dict(), ttl=120)
    return df

# ── Info fondamentale ─────────────────────────────────────────
def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached

    info = {}

    # Tentative 1 : yfinance + curl_cffi
    try:
        t = _ticker(symbol)
        info = _retry(lambda: t.info)
        if info and isinstance(info, dict) and info.get("regularMarketPrice"):
            print(f"[yfinance info] {symbol} OK")
            _cache_set(key, info, ttl=3600)
            return info
    except Exception as e:
        print(f"[yfinance info] {symbol} failed: {e}")

    # Tentative 2 : construire un info minimal via Stooq
    print(f"[fallback info] Construction info minimal pour {symbol}")
    price = _get_price_stooq(symbol)
    if price:
        info = {
            "symbol": symbol,
            "regularMarketPrice": price,
            "currentPrice": price,
            "longName": symbol,
            "shortName": symbol,
            "currency": "USD",
            "exchange": "Unknown",
        }
        _cache_set(key, info, ttl=300)
        return info

    _cache_set(key, _FAILED, ttl=20)
    return {}

# ── Prix temps réel ───────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
