import os, io, json
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


def _json_default(o):
    """Convertit les types numpy/pandas non sérialisables en JSON natif."""
    if hasattr(o, "item"):   # numpy scalar (int64, float64, bool_...)
        return o.item()
    return str(o)


def cache_get_raw(key: str):
    """Lit une chaîne brute dans le cache (pas de json.loads)."""
    if not REDIS_OK:
        return None
    try:
        return _redis.get(key)
    except Exception:
        return None


def cache_set_raw(key: str, value: str, ttl: int = CACHE_TTL):
    if not REDIS_OK:
        return
    try:
        _redis.setex(key, ttl, value)
    except Exception:
        pass


def cache_get_json(key: str):
    raw = cache_get_raw(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def cache_set_json(key: str, value, ttl: int = CACHE_TTL):
    try:
        payload = json.dumps(value, default=_json_default)
    except Exception:
        return
    cache_set_raw(key, payload, ttl)


# ── Données OHLCV ────────────────────────────────────────────
# BUG CORRIGÉ : l'ancienne version sérialisait le DataFrame via
# `.to_dict()` puis `json.dumps()`. Les colonnes OHLCV contiennent des
# types numpy (int64/float64) que `json.dumps` ne sait pas encoder ; ça
# levait une exception à CHAQUE écriture, silencieusement avalée par le
# try/except du cache. Résultat : le cache Redis pour l'historique de
# prix ne fonctionnait jamais, et chaque analyse retapait yfinance.
# On utilise maintenant `DataFrame.to_json()` / `pd.read_json()`, qui
# gèrent nativement les types numpy et les dates.
def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}:{interval}"
    cached = cache_get_raw(key)
    if cached:
        try:
            df = pd.read_json(io.StringIO(cached), orient="records", convert_dates=["Date"])
            if not df.empty:
                return df.set_index("Date")
        except Exception:
            pass

    df = yf.Ticker(symbol).history(period=period, interval=interval)
    if df.empty:
        return df

    try:
        payload = df.reset_index().rename(columns={df.index.name or "index": "Date"}).to_json(
            orient="records", date_format="iso"
        )
        cache_set_raw(key, payload, ttl=120)
    except Exception:
        pass
    return df


# ── Infos fondamentales ───────────────────────────────────────
def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = cache_get_json(key)
    if cached is not None:
        return cached
    info = yf.Ticker(symbol).info or {}
    cache_set_json(key, info, ttl=3600)
    return info


# ── Prix temps réel ───────────────────────────────────────────
def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
