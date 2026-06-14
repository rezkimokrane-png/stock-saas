import numpy as np
import pandas as pd


def compute_score(df: pd.DataFrame, info: dict, signals: dict) -> dict:
    """
    Score global sur 100 avec 5 dimensions.
    Retourne le score, la note, et les détails par dimension.
    """
    scores = {}

    # ── 1. Valorisation (20 pts) ─────────────────────────────
    pe  = info.get("trailingPE")   or 0
    pb  = info.get("priceToBook")  or 0
    peg = info.get("pegRatio")     or 0

    val = 10.0
    if pe   > 0: val += max(0, 10 - pe  / 4)
    if pb   > 0: val += max(0,  5 - pb  / 2)
    if peg  > 0: val += 5 if peg < 1 else (3 if peg < 2 else 0)
    scores["valuation"] = min(round(val, 1), 20.0)

    # ── 2. Croissance (20 pts) ───────────────────────────────
    rev_g = (info.get("revenueGrowth")  or 0) * 100
    eps_g = (info.get("earningsGrowth") or 0) * 100
    roe   = (info.get("returnOnEquity") or 0) * 100

    g = 0.0
    g += min(rev_g / 2, 8)   # max 8 pts si +16% CA
    g += min(eps_g / 3, 8)   # max 8 pts si +24% BPA
    g += 4 if roe > 20 else (2 if roe > 10 else 0)
    scores["growth"] = min(round(max(g, 0), 1), 20.0)

    # ── 3. Santé financière (20 pts) ─────────────────────────
    de  = info.get("debtToEquity")  or 0
    cr  = info.get("currentRatio")  or 0
    qr  = info.get("quickRatio")    or 0
    fcf = info.get("freeCashflow")  or 0
    cap = info.get("marketCap")     or 1

    h = 0.0
    h += 8 if de < 50 else (5 if de < 100 else (2 if de < 200 else 0))
    h += 4 if cr > 2  else (2 if cr > 1   else 0)
    h += 4 if qr > 1  else (2 if qr > 0.5 else 0)
    h += 4 if fcf / cap > 0.05 else (2 if fcf > 0 else 0)
    scores["health"] = min(round(h, 1), 20.0)

    # ── 4. Momentum technique (20 pts) ───────────────────────
    bull_count = sum(1 for s in signals.values() if s.get("bullish") is True)
    bear_count = sum(1 for s in signals.values() if s.get("bullish") is False)
    total_sig  = len(signals) or 1

    m = (bull_count / total_sig) * 20
    scores["momentum"] = round(min(m, 20.0), 1)

    # ── 5. Dividendes (20 pts) ───────────────────────────────
    # Yahoo retourne désormais dividendYield directement en % (ex: 0.93 = 0.93%),
    # et non plus comme une fraction (ex: 0.0093). Pas de *100 ici.
    dy = info.get("dividendYield") or 0
    pr = info.get("payoutRatio") or 1

    d = 0.0
    if dy > 4 and pr < 0.7:   d = 18.0
    elif dy > 3 and pr < 0.8: d = 14.0
    elif dy > 1:               d = 8.0
    elif dy > 0:               d = 4.0
    else:
        # Pas de dividende → on regarde le rachat d'actions
        byr = info.get("buybackYield") or 0
        d   = min(byr * 200, 10.0)
    scores["dividends"] = round(d, 1)

    # ── Score global ─────────────────────────────────────────
    total = sum(scores.values())

    if total >= 75:
        rating, color = "STRONG BUY",  "#2ecc8a"
    elif total >= 60:
        rating, color = "BUY",         "#4f8ef7"
    elif total >= 45:
        rating, color = "HOLD",        "#f2a03f"
    elif total >= 30:
        rating, color = "UNDERPERFORM","#f2a03f"
    else:
        rating, color = "SELL",        "#f25b5b"

    return {
        "total":    round(total, 1),
        "rating":   rating,
        "color":    color,
        "breakdown": scores,
    }


def get_fundamentals_summary(info: dict) -> dict:
    def pct(v):  return f"{round((v or 0) * 100, 1)}%"
    def val(v):  return round(v, 2) if v else None
    def bn(v):   return f"{round(v/1e9, 2)}B" if v else "—"

    return {
        "price":          info.get("regularMarketPrice") or info.get("currentPrice"),
        "currency":       info.get("currency", "USD"),
        "name":           info.get("longName", ""),
        "sector":         info.get("sector", "—"),
        "industry":       info.get("industry", "—"),
        "exchange":       info.get("exchange", "—"),
        "market_cap":     bn(info.get("marketCap")),
        "description":    (info.get("longBusinessSummary") or "")[:400],
        "pe":             val(info.get("trailingPE")),
        "fwd_pe":         val(info.get("forwardPE")),
        "pb":             val(info.get("priceToBook")),
        "peg":            val(info.get("pegRatio")),
        "ev_ebitda":      val(info.get("enterpriseToEbitda")),
        "roe":            pct(info.get("returnOnEquity")),
        "roa":            pct(info.get("returnOnAssets")),
        "margins":        pct(info.get("profitMargins")),
        "gross_margins":  pct(info.get("grossMargins")),
        "rev_growth":     pct(info.get("revenueGrowth")),
        "eps_growth":     pct(info.get("earningsGrowth")),
        "debt_eq":        val(info.get("debtToEquity")),
        "current_ratio":  val(info.get("currentRatio")),
        "div_yield":      f"{round(info.get('dividendYield') or 0, 2)}%",
        "payout":         pct(info.get("payoutRatio")),
        "52w_high":       val(info.get("fiftyTwoWeekHigh")),
        "52w_low":        val(info.get("fiftyTwoWeekLow")),
        "target":         val(info.get("targetMeanPrice")),
        "analysts":       info.get("numberOfAnalystOpinions"),
        "recommendation": info.get("recommendationKey", "—"),
    }
