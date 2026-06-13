import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.stattools import adfuller


def _is_stationary(s: np.ndarray) -> bool:
    try:
        return adfuller(s, autolag="AIC")[1] < 0.05
    except Exception:
        return False


def _best_order(s: np.ndarray) -> tuple:
    best_aic, best_order = np.inf, (1, 1, 1)
    d = 0 if _is_stationary(s) else 1
    for p in range(0, 4):
        for q in range(0, 4):
            try:
                r = ARIMA(s, order=(p, d, q)).fit()
                if r.aic < best_aic:
                    best_aic, best_order = r.aic, (p, d, q)
            except Exception:
                continue
    return best_order


def _dates(last: pd.Timestamp, steps: int, freq: str) -> list[str]:
    return [
        d.strftime("%Y-%m-%d")
        for d in pd.date_range(
            start=last + pd.tseries.frequencies.to_offset(freq),
            periods=steps,
            freq=freq,
        )
    ]


def _build_result(label, model_name, aic, history_df, fc_mean, ci, dates, currency, ci_level):
    hist = [
        {"date": d.strftime("%Y-%m-%d"), "price": round(float(v), 2)}
        for d, v in history_df.items()
    ]
    fc = [
        {
            "date":     dates[i],
            "forecast": round(float(fc_mean[i]), 2),
            "lower":    round(float(ci.iloc[i, 0]), 2),
            "upper":    round(float(ci.iloc[i, 1]), 2),
        }
        for i in range(len(dates))
    ]
    last_price = history_df.iloc[-1]
    end_price  = fc_mean[-1]
    pct_change = (end_price - last_price) / last_price * 100

    return {
        "label":        label,
        "model":        model_name,
        "aic":          round(aic, 1),
        "ci_level":     ci_level,
        "currency":     currency,
        "last_price":   round(float(last_price), 2),
        "end_forecast": round(float(end_price), 2),
        "pct_change":   round(float(pct_change), 2),
        "history":      hist,
        "forecast":     fc,
    }


# ── Court terme : ARIMA — 30 jours ───────────────────────────
def forecast_short(symbol: str, currency: str = "USD") -> dict:
    from backend.data.market import get_ohlcv
    try:
        df   = get_ohlcv(symbol, period="1y", interval="1d")
        if df.empty or len(df) < 60:
            return {}
        log  = np.log(df["Close"].dropna().values)
        order = _best_order(log)
        fit   = ARIMA(log, order=order).fit()
        steps = 30
        fc_obj = fit.get_forecast(steps)
        fc_mean = np.exp(fc_obj.predicted_mean)
        ci      = np.exp(fc_obj.conf_int(alpha=0.20))
        dates   = _dates(df.index[-1], steps, "B")
        return _build_result(
            "Court terme — 30 jours ouvrés", f"ARIMA{order}", fit.aic,
            df["Close"].tail(90), fc_mean, ci, dates, currency, "80%"
        )
    except Exception as e:
        print(f"[short] {e}")
        return {}


# ── Moyen terme : SARIMA hebdo — 3 mois ──────────────────────
def forecast_mid(symbol: str, currency: str = "USD") -> dict:
    from backend.data.market import get_ohlcv
    try:
        df   = get_ohlcv(symbol, period="3y", interval="1d")
        if df.empty or len(df) < 120:
            return {}
        log  = np.log(df["Close"].dropna().values)
        model = SARIMAX(log, order=(1, 1, 1), seasonal_order=(1, 0, 1, 5),
                        enforce_stationarity=False, enforce_invertibility=False)
        fit   = model.fit(disp=False, maxiter=100)
        steps = 90
        fc_obj  = fit.get_forecast(steps)
        fc_mean = np.exp(fc_obj.predicted_mean)
        ci      = np.exp(fc_obj.conf_int(alpha=0.20))
        dates   = _dates(df.index[-1], steps, "B")

        # Agréger en semaines pour le graphique
        fc_df = pd.DataFrame({
            "date":     pd.to_datetime(dates),
            "forecast": fc_mean.values,
            "lower":    ci.iloc[:, 0].values,
            "upper":    ci.iloc[:, 1].values,
        }).set_index("date").resample("W").mean()

        history = df["Close"].resample("W").last().tail(52)
        hist = [{"date": d.strftime("%Y-%m-%d"), "price": round(float(v), 2)} for d, v in history.items()]

        fc_list = [
            {"date": d.strftime("%Y-%m-%d"),
             "forecast": round(float(r["forecast"]), 2),
             "lower":    round(float(r["lower"]),    2),
             "upper":    round(float(r["upper"]),    2)}
            for d, r in fc_df.iterrows()
        ]

        last_price = history.iloc[-1]
        end_price  = fc_df["forecast"].iloc[-1]
        return {
            "label":        "Moyen terme — 3 mois",
            "model":        "SARIMA(1,1,1)(1,0,1,5)",
            "aic":          round(fit.aic, 1),
            "ci_level":     "80%",
            "currency":     currency,
            "last_price":   round(float(last_price), 2),
            "end_forecast": round(float(end_price), 2),
            "pct_change":   round((float(end_price) - float(last_price)) / float(last_price) * 100, 2),
            "history":      hist,
            "forecast":     fc_list,
        }
    except Exception as e:
        print(f"[mid] {e}")
        return {}


# ── Long terme : SARIMA annuel — 52 semaines ─────────────────
def forecast_long(symbol: str, currency: str = "USD") -> dict:
    from backend.data.market import get_ohlcv
    try:
        df   = get_ohlcv(symbol, period="5y", interval="1wk")
        if df.empty or len(df) < 60:
            return {}
        log  = np.log(df["Close"].dropna().values)
        try:
            model = SARIMAX(log, order=(1, 1, 1), seasonal_order=(1, 1, 0, 52),
                            enforce_stationarity=False, enforce_invertibility=False)
            fit   = model.fit(disp=False, maxiter=150)
        except Exception:
            order = _best_order(log)
            fit   = ARIMA(log, order=order).fit()

        steps   = 52
        fc_obj  = fit.get_forecast(steps)
        fc_mean = np.exp(fc_obj.predicted_mean)
        ci      = np.exp(fc_obj.conf_int(alpha=0.10))
        dates   = _dates(df.index[-1], steps, "W")
        return _build_result(
            "Long terme — 1 an", "SARIMA(1,1,1)(1,1,0,52)", fit.aic,
            df["Close"], fc_mean, ci, dates, currency, "90%"
        )
    except Exception as e:
        print(f"[long] {e}")
        return {}
