import os
from fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import models
from db import engine, get_db
import auth
import stripe_payments
from data.market import get_ohlcv, get_info, get_price
from engine.indicators import add_indicators, get_signals, serialize_ohlcv
from engine.scoring import compute_score, get_fundamentals_summary
from engine.forecasting import forecast_short, forecast_mid, forecast_long

# ── Init DB ──────────────────────────────────────────────────
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="StockSaaS API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Servir le frontend ───────────────────────────────────────
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/", include_in_schema=False)
async def root():
    idx = os.path.join(FRONTEND, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return {"status": "StockSaaS API running"}


# ════════════════════════════════════════════════════════════
#  AUTH
# ════════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    email:     str
    password:  str
    full_name: str = ""

class LoginBody(BaseModel):
    email:    str
    password: str


@app.post("/api/auth/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(400, "Email déjà utilisé")
    user = models.User(
        email=body.email,
        password_hash=auth.hash_password(body.password),
        full_name=body.full_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": auth.create_token(user.id), "plan": user.plan, "email": user.email}


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


# ════════════════════════════════════════════════════════════
#  ANALYSE PRINCIPALE
# ════════════════════════════════════════════════════════════

@app.get("/api/analysis/{symbol}")
def analysis(
    symbol: str,
    db:     Session = Depends(get_db),
    user:   models.User = Depends(auth.get_current_user_optional),
):
    # Rate limiting pour les utilisateurs connectés
    if user:
        auth.check_rate_limit(user, db)

    sym = symbol.upper()
    info = get_info(sym)
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if not price:
        raise HTTPException(404, f"Ticker {sym} introuvable")

    df = get_ohlcv(sym)
    if df.empty:
        raise HTTPException(404, "Données historiques indisponibles")

    df      = add_indicators(df)
    signals = get_signals(df)
    score   = compute_score(df, info, signals)
    fundas  = get_fundamentals_summary(info)
    ohlcv   = serialize_ohlcv(df)

    # Prévisions limitées selon le plan
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
        "forecast": {
            "short": short_fc,
            "mid":   mid_fc,
            "long":  long_fc,
        },
    }


# ════════════════════════════════════════════════════════════
#  WATCHLIST
# ════════════════════════════════════════════════════════════

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
        raise HTTPException(400, "Déjà dans la watchlist")
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


# ════════════════════════════════════════════════════════════
#  STRIPE
# ════════════════════════════════════════════════════════════

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
        user_id = int(session.get("client_reference_id", 0))
        sub_id  = session.get("subscription")
        # Déterminer le plan par le price_id
        line_items = session.get("display_items") or []
        plan = "pro"  # Défaut — à affiner avec les price_id réels
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            user.plan            = plan
            user.stripe_sub_id   = sub_id
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
    stripe_payments.cancel_subscription(user.stripe_sub_id)
    return {"ok": True, "message": "Abonnement annulé en fin de période"}


# ════════════════════════════════════════════════════════════
#  HEALTH
# ════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}
