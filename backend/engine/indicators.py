import pandas as pd
import numpy as np


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]

    # ── Moyennes mobiles ─────────────────────────────────────
    df["SMA20"]  = close.rolling(20).mean()
    df["SMA50"]  = close.rolling(50).mean()
    df["SMA200"] = close.rolling(200).mean()
    df["EMA12"]  = close.ewm(span=12, adjust=False).mean()
    df["EMA26"]  = close.ewm(span=26, adjust=False).mean()

    # ── MACD ─────────────────────────────────────────────────
    df["MACD"]        = df["EMA12"] - df["EMA26"]
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"]   = df["MACD"] - df["MACD_signal"]

    # ── RSI ──────────────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    # ── Bollinger Bands ──────────────────────────────────────
    sma20      = close.rolling(20).mean()
    std20      = close.rolling(20).std()
    df["BB_upper"] = sma20 + 2 * std20
    df["BB_lower"] = sma20 - 2 * std20
    df["BB_mid"]   = sma20

    # ── ATR (volatilité) ─────────────────────────────────────
    h, l, c = df["High"], df["Low"], close
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    # ── Stochastic ───────────────────────────────────────────
    low14  = l.rolling(14).min()
    high14 = h.rolling(14).max()
    df["Stoch_K"] = 100 * (close - low14) / (high14 - low14).replace(0, np.nan)
    df["Stoch_D"] = df["Stoch_K"].rolling(3).mean()

    return df


def get_signals(df: pd.DataFrame) -> dict:
    """Résumé des signaux techniques sur la dernière bougie."""
    last = df.iloc[-1]
    prev = df.iloc[-2]

    signals = {}

    # Tendance MA
    if last["SMA20"] > last["SMA50"] > last["SMA200"]:
        signals["trend"] = {"value": "Haussière forte", "bullish": True}
    elif last["SMA20"] < last["SMA50"]:
        signals["trend"] = {"value": "Baissière", "bullish": False}
    else:
        signals["trend"] = {"value": "Neutre", "bullish": None}

    # RSI
    rsi = round(last["RSI"], 1)
    if rsi > 70:
        signals["rsi"] = {"value": f"{rsi} (suracheté)", "bullish": False}
    elif rsi < 30:
        signals["rsi"] = {"value": f"{rsi} (survendu)", "bullish": True}
    else:
        signals["rsi"] = {"value": str(rsi), "bullish": None}

    # MACD croisement
    if last["MACD"] > last["MACD_signal"] and prev["MACD"] <= prev["MACD_signal"]:
        signals["macd"] = {"value": "Croisement haussier", "bullish": True}
    elif last["MACD"] < last["MACD_signal"] and prev["MACD"] >= prev["MACD_signal"]:
        signals["macd"] = {"value": "Croisement baissier", "bullish": False}
    else:
        sign = "+" if last["MACD_hist"] > 0 else "-"
        signals["macd"] = {"value": f"Histogramme {sign}", "bullish": bool(last["MACD_hist"] > 0)}

    # Bollinger
    close = last["Close"]
    if close > last["BB_upper"]:
        signals["bollinger"] = {"value": "Au-dessus bande haute", "bullish": False}
    elif close < last["BB_lower"]:
        signals["bollinger"] = {"value": "En-dessous bande basse", "bullish": True}
    else:
        pct = round((close - last["BB_lower"]) / (last["BB_upper"] - last["BB_lower"]) * 100, 0)
        signals["bollinger"] = {"value": f"Dans les bandes ({int(pct)}%)", "bullish": None}

    # Stochastique
    k = round(last["Stoch_K"], 1)
    if k > 80:
        signals["stochastic"] = {"value": f"{k} (suracheté)", "bullish": False}
    elif k < 20:
        signals["stochastic"] = {"value": f"{k} (survendu)", "bullish": True}
    else:
        signals["stochastic"] = {"value": str(k), "bullish": None}

    return signals


def serialize_ohlcv(df: pd.DataFrame, n: int = 200) -> list:
    cols = ["Open", "High", "Low", "Close", "Volume"]
    subset = df[cols].tail(n).dropna()
    result = []
    for dt, row in subset.iterrows():
        result.append({
            "date":   dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),
            "open":   round(float(row["Open"]),  2),
            "high":   round(float(row["High"]),  2),
            "low":    round(float(row["Low"]),   2),
            "close":  round(float(row["Close"]), 2),
            "volume": int(row["Volume"]),
        })
    return result
