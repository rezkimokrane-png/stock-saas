import os
import json
import time
import requests
import pandas as pd
import numpy as np
import warnings

warnings.filterwarnings("ignore")

AV_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")

# ── Cache mémoire / Redis ───────────────────────────────────────
try:
    import redis

    _redis = redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True
    )
    _redis.ping()
    REDIS_OK = True
except Exception:
    _redis = None
    REDIS_OK = False

CACHE_TTL = 1800  # 30 minutes
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


# ── Alpha Vantage — OHLCV ─────────────────────────────────────
def _fetch_av_ohlcv(symbol: str, period: str = "1y") -> pd.DataFrame:
    if not AV_KEY:
        return pd.DataFrame()
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=TIME_SERIES_DAILY_ADJUSTED"
            f"&symbol={symbol}&outputsize=full&apikey={AV_KEY}"
        )
        r = requests.get(url, timeout=15)
        data = r.json()
        ts = data.get("Time Series (Daily)", {})
        if not ts:
            note = data.get("Note") or data.get("Information") or "no data"
            print(f"[av_ohlcv] {symbol}: {note}")
            return pd.DataFrame()

        records = []
        for date_str, vals in ts.items():
            records.append(
                {
                    "Date": date_str,
                    "Open": float(vals["1. open"]),
                    "High": float(vals["2. high"]),
                    "Low": float(vals["3. low"]),
                    "Close": float(vals["5. adjusted close"]),
                    "Volume": int(vals["6. volume"]),
                }
            )

        df = pd.DataFrame(records)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").set_index("Date")

        periods = {"1y": 365, "2y": 730, "3y": 1095, "5y": 1825}
        days = periods.get(period, 365)
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
        df = df[df.index >= cutoff]

        print(f"[av_ohlcv] {symbol}: {len(df)} jours récupérés")
        return df

    except Exception as e:
        print(f"[av_ohlcv] {symbol} failed: {e}")
        return pd.DataFrame()


# ── Alpha Vantage — Fondamentaux ──────────────────────────────
def _fetch_av_info(symbol: str) -> dict:
    if not AV_KEY:
        return {}
    try:
        url = f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={symbol}&apikey={AV_KEY}"
        r = requests.get(url, timeout=15)
        data = r.json()
        if not data or "Symbol" not in data:
            return {}

        def safe_float(v):
            try:
                return float(v) if v and v != "None" else None
            except Exception:
                return None

        price_url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={AV_KEY}"
        pr = requests.get(price_url, timeout=10).json()
        quote = pr.get("Global Quote", {})
        price = safe_float(quote.get("05. price"))

        return {
            "regularMarketPrice": price,
            "currentPrice": price,
            "longName": data.get("Name", symbol),
            "sector": data.get("Sector", "—"),
            "industry": data.get("Industry", "—"),
            "exchange": data.get("Exchange", "—"),
            "currency": data.get("Currency", "USD"),
            "marketCap": safe_float(data.get("MarketCapitalization")),
            "trailingPE": safe_float(data.get("TrailingPE")),
            "forwardPE": safe_float(data.get("ForwardPE")),
            "priceToBook": safe_float(data.get("PriceToBookRatio")),
            "pegRatio": safe_float(data.get("PEGRatio")),
            "returnOnEquity": safe_float(data.get("ReturnOnEquityTTM")),
            "returnOnAssets": safe_float(data.get("ReturnOnAssetsTTM")),
            "profitMargins": safe_float(data.get("ProfitMargin")),
            "grossMargins": safe_float(data.get("GrossProfitTTM")),
            "revenueGrowth": safe_float(data.get("QuarterlyRevenueGrowthYOY")),
            "earningsGrowth": safe_float(data.get("QuarterlyEarningsGrowthYOY")),
            "debtToEquity": safe_float(data.get("DebtToEquityRatio")),
            "currentRatio": safe_float(data.get("CurrentRatio")),
            "dividendYield": safe_float(data.get("DividendYield")),
            "payoutRatio": safe_float(data.get("PayoutRatio")),
            "fiftyTwoWeekHigh": safe_float(data.get("52WeekHigh")),
            "fiftyTwoWeekLow": safe_float(data.get("52WeekLow")),
            "targetMeanPrice": safe_float(data.get("AnalystTargetPrice")),
            "numberOfAnalystOpinions": safe_float(
                data.get("AnalystRatingStrongBuy")
            ),
            "recommendationKey": data.get("AnalystRatingStrongBuy", "—"),
            "longBusinessSummary": data.get("Description", ""),
            "enterpriseToEbitda": safe_float(data.get("EVToEBITDA")),
            "freeCashflow": safe_float(data.get("OperatingCashflowTTM")),
            "sharesOutstanding": safe_float(data.get("SharesOutstanding")),
            "heldPercentInsiders": safe_float(data.get("PercentInsiders")),
        }
    except Exception as e:
        print(f"[av_info] {symbol} failed: {e}")
        return {}


# ── Fallback yfinance ─────────────────────────────────────────
def _fetch_yf_info(symbol: str) -> dict:
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.info
        if (
            info
            and isinstance(info, dict)
            and (
                info.get("regularMarketPrice") or info.get("currentPrice")
            )
        ):
            return info
        fi = ticker.fast_info
        last_p = getattr(fi, "last_price", None)
        return {
            "regularMarketPrice": last_p,
            "currentPrice": last_p,
            "currency": getattr(fi, "currency", "USD"),
            "longName": symbol,
        }
    except Exception as e:
        print(f"[yf_info] {symbol} failed: {e}")
        return {}


def _fetch_yf_ohlcv(symbol: str, period: str = "1y") -> pd.DataFrame:
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period, interval="1d")
        if not df.empty and "Close" in df.columns:
            df.index = pd.to_datetime(df.index)
            return df
        return pd.DataFrame()
    except Exception as e:
        print(f"[yf_ohlcv] {symbol} failed: {e}")
        return pd.DataFrame()


# ── API publique — OHLCV, Info, Price ─────────────────────────
def get_ohlcv(
    symbol: str, period: str = "1y", interval: str = "1d"
) -> pd.DataFrame:
    key = f"ohlcv:{symbol}:{period}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return pd.DataFrame()
    if cached:
        try:
            df = pd.DataFrame(cached)
            df["Date"] = pd.to_datetime(df["Date"])
            return df.set_index("Date")
        except Exception:
            pass

    df = _fetch_av_ohlcv(symbol, period) if AV_KEY else pd.DataFrame()
    if df.empty:
        print(f"[get_ohlcv] AV échec/absent, fallback yfinance pour {symbol}")
        df = _fetch_yf_ohlcv(symbol, period)

    if df.empty:
        _cache_set(key, _FAILED, ttl=60)
        return df

    df_serialized = df.reset_index()
    df_serialized["Date"] = df_serialized["Date"].dt.strftime("%Y-%m-%d")
    _cache_set(key, df_serialized.to_dict(orient="records"), ttl=CACHE_TTL)
    return df


def get_info(symbol: str) -> dict:
    key = f"info:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached

    info = _fetch_av_info(symbol) if AV_KEY else {}
    if not info or not (
        info.get("regularMarketPrice") or info.get("currentPrice")
    ):
        print(f"[get_info] AV échec/absent, fallback yfinance pour {symbol}")
        info = _fetch_yf_info(symbol)

    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if not info or price is None:
        _cache_set(key, _FAILED, ttl=60)
        return {}

    _cache_set(key, info, ttl=CACHE_TTL)
    return info


def get_price(symbol: str) -> float | None:
    info = get_info(symbol)
    return info.get("regularMarketPrice") or info.get("currentPrice")


# ── Aperçu marché (Batch Download avec données complètes frontend) ──
_OVERVIEW_CACHE_KEY = "market_overview_data"


def get_market_overview(tickers_dict: dict) -> list[dict]:
    cached = _cache_get(_OVERVIEW_CACHE_KEY)
    if cached:
        return cached

    try:
        import yfinance as yf

        symbols = list(tickers_dict.values())
        data = yf.download(
            symbols,
            period="1mo",
            interval="1d",
            group_by="ticker",
            progress=False,
        )

        result = []
        for name, symbol in tickers_dict.items():
            try:
                df = data[symbol] if len(symbols) > 1 else data
                df = df.dropna(subset=["Close"])
                if not df.empty and len(df) >= 2:
                    price = float(df["Close"].iloc[-1])
                    prev_price = float(df["Close"].iloc[-2])
                    change = round(((price - prev_price) / prev_price) * 100, 2)

                    # 30 derniers jours pour la mini-courbe (sparkline)
                    sparkline = [
                        round(float(p), 2)
                        for p in df["Close"].tail(30).tolist()
                    ]

                    # RSI simplifié
                    delta = df["Close"].diff()
                    gain = (
                        (delta.where(delta > 0, 0)).rolling(window=14).mean()
                    )
                    loss = (
                        (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    )
                    rs = gain / loss
                    rsi_val = 100 - (100 / (1 + rs))
                    rsi = (
                        round(float(rsi_val.iloc[-1]), 1)
                        if not rsi_val.empty and not np.isnan(rsi_val.iloc[-1])
                        else 50.0
                    )

                    signal = (
                        "ACHAT"
                        if rsi < 45
                        else ("VENTE" if rsi > 70 else "NEUTRE")
                    )

                    result.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "price": round(price, 2),
                            "change": change,
                            "rsi": rsi,
                            "signal": signal,
                            "model": "ARIMA-ML",
                            "forecast": round(
                                price * (1 + (0.01 if rsi < 50 else -0.005)), 2
                            ),
                            "sparkline": sparkline,
                        }
                    )
            except Exception as e:
                print(f"[market_overview] Error for {symbol}: {e}")
                continue

        if result:
            _cache_set(_OVERVIEW_CACHE_KEY, result, ttl=300)
            return result

    except Exception as e:
        print(f"[market_overview] Error: {e}")

    return []


# ── Financials ────────────────────────────────────────────────
def get_financials(symbol: str) -> dict:
    key = f"financials:{symbol}"
    cached = _cache_get(key)
    if cached == _FAILED:
        return {}
    if cached:
        return cached

    financials = {
        "income_statement": [],
        "balance_sheet": [],
        "cash_flow": [],
    }

    if AV_KEY:
        try:
            url = f"https://www.alphavantage.co/query?function=INCOME_STATEMENT&symbol={symbol}&apikey={AV_KEY}"
            r = requests.get(url, timeout=15)
            data = r.json()
            if "annualReports" in data:
                financials["income_statement"] = data["annualReports"]
        except Exception as e:
            print(f"[av_financials] {symbol} failed: {e}")

    if not any(financials.values()):
        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)

            def df_to_list(df):
                if df is None or df.empty:
                    return []
                df_reset = df.reset_index()
                # Convertir les dates/colonnes en chaînes de caractères pour éviter l'échec de la sérialisation JSON
                df_reset.columns = [str(c) for c in df_reset.columns]
                return df_reset.astype(str).to_dict(orient="records")

            financials = {
                "income_statement": df_to_list(
                    getattr(ticker, "financials", None)
                ),
                "balance_sheet": df_to_list(
                    getattr(ticker, "balance_sheet", None)
                ),
                "cash_flow": df_to_list(getattr(ticker, "cashflow", None)),
            }
        except Exception as e:
            print(f"[yf_financials] {symbol} failed: {e}")

    if not any(financials.values()):
        _cache_set(key, _FAILED, ttl=60)
        return {}

    _cache_set(key, financials, ttl=CACHE_TTL)
    return financials
