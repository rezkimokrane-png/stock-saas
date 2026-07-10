import os, io, json, time as _time
import requests
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════════════
#  SOURCE DE DONNÉES : Financial Modeling Prep (remplace Yahoo
#  Finance / yfinance). Yahoo Finance bloquait/throttlait
#  systématiquement les IP partagées de Render (confirmé : endpoint
#  /api/market-overview renvoyait {} après un timeout complet sur
#  TOUS les tickers). FMP est une vraie API avec clé, sans ce
#  problème de blocage IP.
# ═══════════════════════════════════════════════════════════════
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE    = "https://financialmodelingprep.com/api/v3"


def _fmp_get(path: str, params: dict | None = None, timeout: int = 10):
    """Appel générique à l'API FMP. Retourne None (jamais d'exception)
    si la clé est absente, le réseau échoue, ou la réponse n'est pas
    du JSON exploitable — chaque appelant gère le cas None."""
    if not FMP_API_KEY:
        return None
    p = dict(params or {})
    p["apikey"] = FMP_API_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{path}", params=p, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


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


# ── Données OHLCV (historique quotidien, agrégé en hebdo si besoin) ──
_PERIOD_DAYS = {"3mo": 95, "1y": 380, "3y": 1130, "5y": 1900}

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

    days = _PERIOD_DAYS.get(period, 380)
    data = _fmp_get(f"historical-price-full/{symbol}", {"timeseries": days})
    hist = (data or {}).get("historical") if isinstance(data, dict) else None
    if not hist:
        return pd.DataFrame()

    df = pd.DataFrame(hist)
    df["Date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                             "close": "Close", "volume": "Volume"})
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date")
    df = df.set_index("Date")

    if interval == "1wk":
        df = df.resample("W").agg({"Open": "first", "High": "max", "Low": "min",
                                    "Close": "last", "Volume": "sum"}).dropna()

    if df.empty:
        return df

    try:
        payload = df.reset_index().rename(columns={df.index.name or "index": "Date"}).to_json(
            orient="records", date_format="iso"
        )
        cache_set_raw(key, payload, ttl=900)
    except Exception:
        pass
    return df


# ── Infos fondamentales (assemble un dict au format proche de
#    l'ancien yfinance `.info`, pour ne rien casser dans scoring.py) ──
# NOUVEAU : cache mémoire de secours (fonctionne même si Redis n'est
# pas provisionné sur Render — c'est le cas par défaut si aucune
# instance Redis n'a été ajoutée). Sans lui, chaque appel refait 3
# requêtes FMP à chaque fois, ce qui épuise très vite le quota gratuit
# de 250 requêtes/jour.
_info_cache: dict[str, tuple[float, dict]] = {}
INFO_TTL = 6 * 3600  # 6h : les fondamentaux changent lentement (trimestriel)

def get_info(symbol: str) -> dict:
    now = _time.time()
    mem = _info_cache.get(symbol)
    if mem and now - mem[0] < INFO_TTL:
        return mem[1]

    key = f"info:{symbol}"
    cached = cache_get_json(key)
    if cached is not None:
        _info_cache[symbol] = (now, cached)
        return cached

    # 3 appels au lieu de 4 : on se passe de financial-growth (revenue/
    # earnings growth) pour économiser le quota — dégradation
    # acceptable, ces champs retombent sur None/0 partout où ils sont
    # utilisés (scoring, filtrage thématique).
    profile = _fmp_get(f"profile/{symbol}")
    quote   = _fmp_get(f"quote/{symbol}")
    ratios  = _fmp_get(f"ratios-ttm/{symbol}")

    p = (profile or [{}])[0] if isinstance(profile, list) and profile else {}
    q = (quote or [{}])[0] if isinstance(quote, list) and quote else {}
    r = (ratios or [{}])[0] if isinstance(ratios, list) and ratios else {}

    info = {
        "regularMarketPrice":   q.get("price"),
        "currentPrice":         q.get("price"),
        "currency":             p.get("currency", "USD"),
        "longName":             p.get("companyName"),
        "sector":               p.get("sector"),
        "industry":             p.get("industry"),
        "exchange":             p.get("exchangeShortName"),
        "longBusinessSummary":  p.get("description", ""),
        "marketCap":            q.get("marketCap") or p.get("mktCap"),
        "trailingPE":           q.get("pe") or r.get("peRatioTTM"),
        "forwardPE":            None,
        "priceToBook":          r.get("priceToBookRatioTTM"),
        "pegRatio":             r.get("priceEarningsToGrowthRatioTTM"),
        "enterpriseToEbitda":   r.get("enterpriseValueMultipleTTM"),
        "returnOnEquity":       r.get("returnOnEquityTTM"),
        "returnOnAssets":       r.get("returnOnAssetsTTM"),
        "profitMargins":        r.get("netProfitMarginTTM"),
        "grossMargins":         r.get("grossProfitMarginTTM"),
        "revenueGrowth":        None,
        "earningsGrowth":       None,
        "debtToEquity":         r.get("debtEquityRatioTTM"),
        "currentRatio":         r.get("currentRatioTTM"),
        "quickRatio":           r.get("quickRatioTTM"),
        "freeCashflow":         None,
        "dividendYield":        r.get("dividendYielTTM") or r.get("dividendYieldTTM"),
        "payoutRatio":          r.get("payoutRatioTTM"),
        "fiftyTwoWeekHigh":     q.get("yearHigh"),
        "fiftyTwoWeekLow":      q.get("yearLow"),
        "targetMeanPrice":      None,
        "numberOfAnalystOpinions": None,
        "recommendationKey":   "-",
        "buybackYield":        None,
    }
    # Ne met en cache que si on a au moins un prix (évite de figer un
    # échec temporaire pendant toute la durée du TTL).
    if info.get("regularMarketPrice") is not None:
        cache_set_json(key, info, ttl=INFO_TTL)
        _info_cache[symbol] = (now, info)
    return info


def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")


# ── Aperçu marché léger (page d'accueil) ──────────────────────
from ..engine.indicators import add_indicators, get_signals as _get_signals

_overview_cache = {"data": None, "ts": 0.0}
# NOUVEAU : 30 minutes (au lieu de 5) — le tier gratuit FMP a un quota
# journalier limité ; avec ~28 tickers par rafraîchissement, ceci reste
# largement dans le budget tout en respectant la mention "délai 15-20
# min" déjà affichée dans l'interface.
# NOUVEAU : 1 heure — avec ~28 tickers suivis, chaque rafraîchissement
# consomme ~28 requêtes FMP. Sur le tier gratuit (250 requêtes/jour),
# ceci laisse une marge pour les clics individuels sur des actions
# (/api/analysis, /api/financials). Ajustez cette valeur à la baisse
# une fois passé sur un plan FMP payant.
OVERVIEW_TTL = 3600


def get_quick_quote(symbol: str, with_fundamentals: bool = False) -> dict | None:
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
    result = {
        "price":       round(price, 4),
        "change_pct":  round(change_pct, 2),
        "rsi":         rsi,
        "signal":      sig,
        "points":      points,
    }

    if with_fundamentals:
        try:
            info = get_info(symbol)
            result["sector"]       = info.get("sector")
            result["industry"]     = info.get("industry")
            result["pe"]           = info.get("trailingPE")
            result["peg"]          = info.get("pegRatio")
            result["div_yield"]    = round((info.get("dividendYield") or 0) * 100, 2)
            result["roe"]          = round((info.get("returnOnEquity") or 0) * 100, 2)
            result["rev_growth"]   = round((info.get("revenueGrowth") or 0) * 100, 2)
            result["debt_eq"]      = info.get("debtToEquity")
            result["market_cap"]   = info.get("marketCap")
            result["payout_ratio"] = info.get("payoutRatio")
        except Exception:
            pass

    return result


def get_market_overview(tickers: dict, fundamentals_for: set[str] | None = None) -> dict:
    """tickers: {nom_affiché: symbole}. Cache PARTAGÉ (mémoire + Redis),
    tickers interrogés EN PARALLÈLE avec timeout strict par ticker,
    pour ne jamais bloquer tout l'endpoint sur un seul appel lent."""
    fundamentals_for = fundamentals_for or set()
    now = _time.time()
    if _overview_cache["data"] is not None and now - _overview_cache["ts"] < OVERVIEW_TTL:
        return _overview_cache["data"]

    cached = cache_get_json("market_overview:v3")
    if cached is not None:
        _overview_cache["data"], _overview_cache["ts"] = cached, now
        return cached

    import concurrent.futures
    TICKER_TIMEOUT = 8
    MAX_WORKERS    = 10

    result = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {
        pool.submit(get_quick_quote, sym, name in fundamentals_for): name
        for name, sym in tickers.items()
    }
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=TICKER_TIMEOUT * 3):
            name = futures[fut]
            try:
                q = fut.result()
                if q:
                    result[name] = q
            except Exception:
                continue
    except concurrent.futures.TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    cache_set_json("market_overview:v3", result, ttl=OVERVIEW_TTL)
    _overview_cache["data"], _overview_cache["ts"] = result, now
    return result


# ── États financiers complets : bilan, résultats, trésorerie ──
BALANCE_MAP = {
    "Total Assets":                            "totalAssets",
    "Current Assets":                          "totalCurrentAssets",
    "Cash And Cash Equivalents":               "cashAndCashEquivalents",
    "Total Liabilities Net Minority Interest": "totalLiabilities",
    "Current Liabilities":                     "totalCurrentLiabilities",
    "Total Debt":                              "totalDebt",
    "Total Equity Gross Minority Interest":    "totalStockholdersEquity",
}
INCOME_MAP = {
    "Total Revenue":    "revenue",
    "Gross Profit":     "grossProfit",
    "Operating Income": "operatingIncome",
    "EBITDA":           "ebitda",
    "Net Income":        "netIncome",
    "Diluted EPS":       "epsdiluted",
}
CASHFLOW_MAP = {
    "Operating Cash Flow": "operatingCashFlow",
    "Investing Cash Flow": "netCashUsedForInvestingActivites",
    "Financing Cash Flow": "netCashUsedProvidedByFinancingActivities",
    "Capital Expenditure": "capitalExpenditure",
    "Free Cash Flow":      "freeCashFlow",
}


def _fmp_statement_to_json(entries: list, label_map: dict, scale_eps_labels=frozenset({"Diluted EPS"})):
    """entries: liste FMP (une entrée = un exercice, la plus récente en
    premier). Retourne {years, items} au même format que la version
    précédente basée sur yfinance, pour ne rien changer côté frontend."""
    if not entries:
        return {"years": [], "items": []}

    entries = entries[:4]
    years = [str(e.get("calendarYear") or (e.get("date", "")[:4])) for e in entries]

    items = []
    for label, fmp_key in label_map.items():
        values = []
        has_any = False
        for e in entries:
            v = e.get(fmp_key)
            if v is None:
                values.append(None)
                continue
            has_any = True
            if label in scale_eps_labels:
                values.append(round(float(v), 2))
            else:
                values.append(round(float(v) / 1e9, 3))
        if has_any:
            items.append({"label": label, "values": values})

    return {"years": years, "items": items}


def get_financials(symbol: str) -> dict:
    key = f"financials:{symbol}"
    cached = cache_get_json(key)
    if cached is not None:
        return cached

    bs  = _fmp_get(f"balance-sheet-statement/{symbol}", {"limit": 4})
    inc = _fmp_get(f"income-statement/{symbol}", {"limit": 4})
    cf  = _fmp_get(f"cash-flow-statement/{symbol}", {"limit": 4})

    result = {
        "symbol":           symbol,
        "balance_sheet":    _fmp_statement_to_json(bs  if isinstance(bs,  list) else [], BALANCE_MAP),
        "income_statement": _fmp_statement_to_json(inc if isinstance(inc, list) else [], INCOME_MAP),
        "cash_flow":        _fmp_statement_to_json(cf  if isinstance(cf,  list) else [], CASHFLOW_MAP),
    }

    has_any = any(result[k]["items"] for k in ("balance_sheet", "income_statement", "cash_flow"))
    if has_any:
        cache_set_json(key, result, ttl=6 * 3600)
    return result
