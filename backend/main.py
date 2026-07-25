import os
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from . import models
from .db import engine, get_db
from . import auth
from . import stripe_payments
from .data.market import get_ohlcv, get_info, get_price, get_financials, get_market_overview
from .engine.indicators import add_indicators, get_signals, serialize_ohlcv
from .engine.scoring import compute_score, get_fundamentals_summary
from .engine.forecasting import forecast_short, forecast_mid, forecast_long

# â”€â”€ Init DB â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="StockSaaS API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# â”€â”€ Servir le frontend â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/", include_in_schema=False)
async def root():
    idx = os.path.join(FRONTEND, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return {"status": "StockSaaS API running"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  APERÃ‡U MARCHÃ‰ (page d'accueil â€” public, non rate-limitÃ©)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# NOUVEAU : sert les grilles d'indices/actions de la home page.
# Volontairement public et mutualisÃ© (voir get_market_overview) pour
# ne pas re-dÃ©clencher le rate-limiting Yahoo Finance cÃ´tÃ© Render.

MARKET_TICKERS = {
    # Indices
    "CAC 40": "^FCHI", "DAX 40": "^GDAXI", "FTSE 100": "^FTSE", "SMI": "^SSMI",
    "IBEX 35": "^IBEX", "S&P 500": "^GSPC", "Nasdaq 100": "^NDX", "Dow Jones": "^DJI",
    "Nikkei 225": "^N225", "Hang Seng": "^HSI", "Sensex": "^BSESN", "ASX 200": "^AXJO",
    "Gold (XAU/USD)": "GC=F", "Brent Crude": "BZ=F", "EUR/USD": "EURUSD=X",
    # Actions (mÃªmes tickers que les cartes de la home page)
    "LVMH": "MC.PA", "TotalEnergies": "TTE.PA", "Sanofi": "SAN.PA",
    "BNP Paribas": "BNP.PA", "Airbus": "AIR.PA",
    "Apple": "AAPL", "Microsoft": "MSFT", "Tesla": "TSLA",
    "Nvidia": "NVDA", "Amazon": "AMZN", "Meta": "META",
    "NestlÃ©": "NESN.SW", "Samsung": "005930.KS",
}

@app.get("/api/market-overview")
def market_overview():
    return get_market_overview(MARKET_TICKERS)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  AUTH
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class RegisterBody(BaseModel):
    email:     EmailStr
    password:  str
    full_name: str = ""

class LoginBody(BaseModel):
    email:    EmailStr
    password: str


@app.post("/api/auth/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    if len(body.password) < 8:
        raise HTTPException(400, "Le mot de passe doit faire au moins 8 caractÃ¨res")
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(400, "Email dÃ©jÃ  utilisÃ©")
    user = models.User(
        email=body.email,
        password_hash=auth.hash_password(body.password),
        full_name=body.full_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": auth.create_token(user.id), "plan": user.plan, "email": user.email,
            "full_name": user.full_name}


@app.post("/api/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == body.email).first()
    if not user or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Identifiants incorrects")
    return {"token": auth.create_token(user.id), "plan": user.plan, "email": user.email,
            "full_name": user.full_name}


@app.get("/api/auth/me")
def me(user: models.User = Depends(auth.get_current_user)):
    return {
        "id": user.id, "email": user.email, "full_name": user.full_name,
        "plan": user.plan, "analyses_today": user.analyses_today,
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  ANALYSE PRINCIPALE (prix, indicateurs, score, prÃ©visions)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/api/analysis/{symbol}")
def analysis(
    symbol:  str,
    request: Request,
    db:      Session = Depends(get_db),
    user:    models.User = Depends(auth.get_current_user_optional),
):
    if user:
        auth.check_rate_limit(user, db)
    else:
        auth.check_anon_rate_limit(request)

    sym = symbol.upper()
    info = get_info(sym)
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if not price:
        raise HTTPException(404, f"Ticker {sym} introuvable")

    df = get_ohlcv(sym)
    if df.empty:
        raise HTTPException(404, "DonnÃ©es historiques indisponibles")

    df      = add_indicators(df)
    signals = get_signals(df)
    score   = compute_score(df, info, signals)
    fundas  = get_fundamentals_summary(info)
    ohlcv   = serialize_ohlcv(df)

    plan     = user.plan if user else "free"
    short_fc = forecast_short(sym, fundas["currency"])
    mid_fc   = forecast_mid(sym, fundas["currency"])  if plan in ("pro", "premium") else {}
    long_fc  = forecast_long(sym, fundas["currency"]) if plan == "premium"          else {}

    return {
        "symbol":       sym,
        "plan":         plan,
        "score":        score,
        "fundamentals": fundas,
        "signals":      signals,
        "ohlcv":        ohlcv,
        "sentiment": sentiment,
        "forecast": {
            "short": short_fc,
            "mid":   mid_fc,
            "long":  long_fc,
        },
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Ã‰TATS FINANCIERS COMPLETS (bilan, rÃ©sultats, trÃ©sorerie)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# NOUVEAU : rÃ©servÃ© aux plans Pro/Premium, cohÃ©rent avec le lock dÃ©jÃ 
# prÃ©sent cÃ´tÃ© frontend sur l'onglet "Financials".

@app.get("/api/financials/{symbol}")
def financials(
    symbol: str,
    db:     Session = Depends(get_db),
    user:   models.User = Depends(auth.get_current_user),
):
    if user.plan == "free":
        raise HTTPException(
            403,
            "Les Ã©tats financiers complets (bilan, rÃ©sultats, trÃ©sorerie) sont rÃ©servÃ©s aux plans Pro et Premium."
        )
    sym = symbol.upper()
    data = get_financials(sym)
    if not any(data[k]["items"] for k in ("balance_sheet", "income_statement", "cash_flow")):
        raise HTTPException(404, f"Ã‰tats financiers indisponibles pour {sym}")
    return data


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  WATCHLIST (sert aussi de "favoris" cÃ´tÃ© frontend)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/api/watchlist")
def get_watchlist(
    db:   Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    items = db.query(models.Watchlist).filter(models.Watchlist.user_id == user.id).all()
    result = []
    for item in items:
        price = get_price(item.symbol)
        result.append({"id": item.id, "symbol": item.symbol, "price": price})
    return result


@app.post("/api/watchlist/{symbol}")
def add_to_watchlist(
    symbol: str,
    db:     Session = Depends(get_db),
    user:   models.User = Depends(auth.get_current_user),
):
    exists = db.query(models.Watchlist).filter(
        models.Watchlist.user_id == user.id,
        models.Watchlist.symbol  == symbol.upper(),
    ).first()
    if exists:
        raise HTTPException(400, "DÃ©jÃ  dans la watchlist")
    if user.plan == "free":
        count = db.query(models.Watchlist).filter(models.Watchlist.user_id == user.id).count()
        if count >= 5:
            raise HTTPException(403, "Limite watchlist atteinte (5 sur plan Free). Passez Pro.")
    db.add(models.Watchlist(user_id=user.id, symbol=symbol.upper()))
    db.commit()
    return {"ok": True}


@app.delete("/api/watchlist/{symbol}")
def remove_from_watchlist(
    symbol: str,
    db:     Session = Depends(get_db),
    user:   models.User = Depends(auth.get_current_user),
):
    db.query(models.Watchlist).filter(
        models.Watchlist.user_id == user.id,
        models.Watchlist.symbol  == symbol.upper(),
    ).delete()
    db.commit()
    return {"ok": True}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  STRIPE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/api/billing/checkout/{plan}")
def checkout(
    plan: str,
    user: models.User = Depends(auth.get_current_user),
):
    url = stripe_payments.create_checkout_session(user.email, plan, user.id)
    return {"url": url}


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    event   = stripe_payments.handle_webhook(payload, sig)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = int(session.get("client_reference_id") or 0)
        sub_id  = session.get("subscription")
        plan = stripe_payments.resolve_plan_from_session(session)
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            user.plan               = plan
            user.stripe_sub_id      = sub_id
            user.stripe_customer_id = session.get("customer")
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        user = db.query(models.User).filter(
            models.User.stripe_sub_id == sub["id"]
        ).first()
        if user:
            user.plan = "free"
            db.commit()

    return {"ok": True}


@app.post("/api/billing/cancel")
def cancel_subscription(
    db:   Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    if not user.stripe_sub_id:
        raise HTTPException(400, "Aucun abonnement actif")
    stripe_payments.cancel_subscription(user.stripe_sub_id)
    return {"ok": True, "message": "Abonnement annulÃ© en fin de pÃ©riode"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  NEWSLETTER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# NOUVEAU : endpoint minimal pour remplacer l'appel Supabase
# (sb.from('newsletter_subscribers').insert) qui n'existe plus.

@app.post("/api/newsletter")
def newsletter_subscribe(body: dict, db: Session = Depends(get_db)):
    email = (body or {}).get("email", "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Email invalide")
    exists = db.query(models.NewsletterSubscriber).filter(
        models.NewsletterSubscriber.email == email
    ).first()
    if not exists:
        db.add(models.NewsletterSubscriber(email=email))
        db.commit()
    return {"ok": True}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  HEALTH
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.1.0"}



