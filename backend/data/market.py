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


# ── Aperçu marché léger (page d'accueil) ──────────────────────
# NOUVEAU : endpoint public utilisé pour peupler les grilles
# indices/actions de la page d'accueil. Volontairement séparé de
# /api/analysis (qui reste rate-limité par utilisateur/IP et fait un
# vrai fit ARIMA). Ici on ne calcule qu'un quote léger (prix, variation,
# RSI, signal), et surtout on le fait AU PLUS UNE FOIS TOUTES LES 5 MIN
# pour TOUS les visiteurs confondus (cache mémoire + Redis), pas par
# visiteur. Sans ce découplage, chaque chargement de page déclencherait
# ~25 appels yfinance par visiteur, ce qui recréerait exactement le
# problème de rate-limiting Yahoo Finance / IP partagée Render déjà
# rencontré sur ce projet.
from ..engine.indicators import add_indicators, get_signals as _get_signals
import time as _time

_overview_cache = {"data": None, "ts": 0.0}
OVERVIEW_TTL = 300  # 5 minutes, partagé entre tous les visiteurs


def get_quick_quote(symbol: str) -> dict | None:
    df = get_ohlcv(symbol, period="3mo", interval="1d")
    if df.empty or len(df) < 15:
        return None
    df = add_indicators(df)
    signals = _get_signals(df)
    last, prev = df.iloc[-1], df.iloc[-2]
    price = float(last["Close"])
    prev_price = float(prev["Close"])
    if prev_price == 0:
        return None
    change_pct = (price - prev_price) / prev_price * 100
    rsi_val = last.get("RSI")
    rsi = round(float(rsi_val), 1) if pd.notna(rsi_val) else None
    bull = sum(1 for s in signals.values() if s.get("bullish") is True)
    bear = sum(1 for s in signals.values() if s.get("bullish") is False)
    sig = "buy" if bull > bear else ("sell" if bear > bull else "hold")
    points = [round(float(v), 4) for v in df["Close"].tail(11).tolist()]
    return {
        "price":       round(price, 4),
        "change_pct":  round(change_pct, 2),
        "rsi":         rsi,
        "signal":      sig,
        "points":      points,
    }


def get_market_overview(tickers: dict) -> dict:
    """tickers: {nom_affiché: symbole_yfinance}. Retourne un quote léger
    par instrument, avec cache PARTAGÉ (mémoire + Redis) de 5 minutes."""
    now = _time.time()
    if _overview_cache["data"] is not None and now - _overview_cache["ts"] < OVERVIEW_TTL:
        return _overview_cache["data"]

    cached = cache_get_json("market_overview:v1")
    if cached is not None:
        _overview_cache["data"], _overview_cache["ts"] = cached, now
        return cached

    result = {}
    for name, sym in tickers.items():
        try:
            q = get_quick_quote(sym)
            if q:
                result[name] = q
        except Exception:
            continue

    cache_set_json("market_overview:v1", result, ttl=OVERVIEW_TTL)
    _overview_cache["data"], _overview_cache["ts"] = result, now
    return result


# ── États financiers complets : bilan, résultats, trésorerie ──
# NOUVEAU : expose le détail annuel (jusqu'à 4 exercices) pour chaque
# grand poste comptable, en plus du résumé de ratios déjà fourni par
# get_fundamentals_summary(). Toutes les valeurs sont en milliards
# d'unités de la devise native du titre, pour un affichage direct.
BALANCE_ROWS = [
    "Total Assets",
    "Current Assets",
    "Cash And Cash Equivalents",
    "Total Liabilities Net Minority Interest",
    "Current Liabilities",
    "Total Debt",
    "Total Equity Gross Minority Interest",
]

INCOME_ROWS = [
    "Total Revenue",
    "Gross Profit",
    "Operating Income",
    "EBITDA",
    "Net Income",
    "Diluted EPS",
]

CASHFLOW_ROWS = [
    "Operating Cash Flow",
    "Investing Cash Flow",
    "Financing Cash Flow",
    "Capital Expenditure",
    "Free Cash Flow",
]


def _statement_to_json(df: pd.DataFrame, rows: list[str], scale_eps_rows: set[str] = frozenset({"Diluted EPS"})):
    """Convertit un DataFrame yfinance (lignes = postes, colonnes = exercices)
    en structure JSON {years:[...], items:[{label, values:[...]}]}.
    Valeurs en milliards, sauf les postes par action (EPS) laissés tels quels.
    Années les plus récentes en premier, limitées à 4 exercices."""
    if df is None or df.empty:
        return {"years": [], "items": []}

    cols = list(df.columns)[:4]
    years = [c.strftime("%Y") if hasattr(c, "strftime") else str(c) for c in cols]

    items = []
    for row_name in rows:
        if row_name not in df.index:
            continue
        values = []
        for c in cols:
            v = df.loc[row_name, c]
            if pd.isna(v):
                values.append(None)
            elif row_name in scale_eps_rows:
                values.append(round(float(v), 2))
            else:
                values.append(round(float(v) / 1e9, 3))
        items.append({"label": row_name, "values": values})

    return {"years": years, "items": items}


def get_financials(symbol: str) -> dict:
    key = f"financials:{symbol}"
    cached = cache_get_json(key)
    if cached is not None:
        return cached

    t = yf.Ticker(symbol)
    try:
        bs = t.balance_sheet
    except Exception:
        bs = None
    try:
        inc = t.financials
    except Exception:
        inc = None
    try:
        cf = t.cashflow
    except Exception:
        cf = None

    result = {
        "symbol":           symbol,
        "balance_sheet":    _statement_to_json(bs,  BALANCE_ROWS),
        "income_statement": _statement_to_json(inc, INCOME_ROWS),
        "cash_flow":        _statement_to_json(cf,  CASHFLOW_ROWS),
    }

    # Si absolument aucune donnée n'a pu être récupérée, on ne met pas
    # en cache (peut être un problème temporaire côté Yahoo Finance),
    # pour retenter à la prochaine requête plutôt que de servir du vide
    # pendant toute la durée du TTL.
    has_any = any(result[k]["items"] for k in ("balance_sheet", "income_statement", "cash_flow"))
    if has_any:
        cache_set_json(key, result, ttl=6 * 3600)
    return result
