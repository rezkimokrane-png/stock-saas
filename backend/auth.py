import os
import datetime
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from backend.db import get_db
from backend import models

SECRET_KEY   = os.getenv("SECRET_KEY", "change-me-in-production-please")
ALGORITHM    = "HS256"
TOKEN_EXPIRE = 7  # jours

pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer   = HTTPBearer(auto_error=False)


# ── Passwords ────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


# ── Tokens ───────────────────────────────────────────────────
def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_EXPIRE),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")


# ── Dépendances FastAPI ───────────────────────────────────────
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> models.User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Non authentifié")
    user_id = decode_token(credentials.credentials)
    user    = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Utilisateur introuvable")
    return user

def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
):
    if not credentials:
        return None
    try:
        user_id = decode_token(credentials.credentials)
        return db.query(models.User).filter(models.User.id == user_id).first()
    except Exception:
        return None


# ── Rate limiting par plan ────────────────────────────────────
PLAN_LIMITS = {"free": 3, "pro": 50, "premium": 999}

def check_rate_limit(user: models.User, db: Session):
    from datetime import date
    today = str(date.today())
    if user.analyses_date != today:
        user.analyses_date  = today
        user.analyses_today = 0
    limit = PLAN_LIMITS.get(user.plan, 3)
    if user.analyses_today >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Limite atteinte ({limit}/jour sur le plan {user.plan}). Upgradez pour continuer."
        )
    user.analyses_today += 1
    db.commit()
