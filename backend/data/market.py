import os, json, time
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

try:
    import redis
    _redis = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    _redis.ping()
    REDIS_OK = True
except Exception:
    _redis = None
    REDIS_OK = False

CACHE_TTL = 1800
_mem_cache: dict = {}
_FAILED = "__FAILED__"

def _cache_get(key: str):
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

def _cache_set(key: str, value, ttl=CACHE_TTL):
    if REDIS_OK:
        try:
            _redis.setex(key, ttl, json.dumps(value))
            return
        except Exception:
            pass
    _mem_cache[key] = (value, time.time() + ttl)

def _stooq_symbol(symbol: str) -> str:
    s = symbol.upper()
    mapping = {".PA": ".fr", ".AS": ".nl", ".L": ".uk", ".DE": ".de", ".MI": ".it", ".MC": ".es"}
    for yf_suffix, stooq_suffix in mapping.items():
        if s.endswith(yf_suffix):
            return s.replace(yf_suffix, stooq_suffix)
    if "." not in s:
        s = s + ".US"
    return s

def _fetch_stooq(symbol: str, period: str = "1y") -> pd.DataFrame:
    import pandas_datareader.data as web
    from datetime import date, timedelta
    periods = {"1y": 365, "2y": 730, "3y": 1095, "5y": 1825}
    days = periods.get(period, 365)
    end = date.today()
    start = end - timedelta(days=days)
    stooq_sym = _stooq_symbol(symbol)
    try:
        df = web.DataReader(stooq_sym, "stooq", start, end)
        df = df.sort_index()
        df.columns = [c.title() for c in df.columns]
        return df
    except Exception as e:
        print(f"[stooq] {symbol} ({stooq_sym}) failed: {e}")
        return pd.DataFrame()

def _fetch_yf_info(symbol: str) -> dict:
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.info
        if info and isinstance(info, dict) and info.get("regularMarketPrice"):
            return info
        fi = ticker.fast_info
        return {"regularMarketPrice": getattr(fi, "last_price", None), "currency": getattr(fi, "currency", "USD"), "longName": symbol}
    except Exception as e:
        print(f"[yf_info] {symbol} failed: {e}")
        return {}

def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return pd.DataFrame()
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df
    df = _fetch_stooq(symbol, period)
    if df.empty:
        _cache_set(key, _FAILED, ttl=60)
        return df
    _cache_set(key, df.reset_index().assign(Date=lambda x: x["Date"].dt.strftime("%Y-%m-%d")).set_index("Date").to_dict(), ttl=CACHE_TTL)
    return df

def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached
    info = _fetch_yf_info(symbol)
    if not info:
        df = get_ohlcv(symbol, "1y")
        if not df.empty and "Close" in df.columns:
            price = float(df["Close"].iloc[-1])
            info = {"regularMarketPrice": price, "currentPrice": price, "longName": symbol, "currency": "USD"}
            print(f"[fallback info] Prix Stooq pour {symbol}: {price}")
    if not info:
        _cache_set(key, _FAILED, ttl=60)
        return {}
    _cache_set(key, info, ttl=CACHE_TTL)
    return info

def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")
