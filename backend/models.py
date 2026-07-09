from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.sql import func
from .db import Base


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name     = Column(String, default="")
    plan          = Column(String, default="free")          # free | pro | premium
    stripe_customer_id = Column(String, nullable=True)
    stripe_sub_id      = Column(String, nullable=True, index=True)
    analyses_today     = Column(Integer, default=0)
    analyses_date      = Column(String, default="")         # YYYY-MM-DD
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class Watchlist(Base):
    __tablename__ = "watchlist"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    symbol     = Column(String, nullable=False)
    added_at   = Column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, index=True, nullable=False)
    symbol     = Column(String, nullable=False)
    condition  = Column(String, nullable=False)   # "above" | "below"
    target     = Column(Float, nullable=False)
    triggered  = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class NewsletterSubscriber(Base):
    __tablename__ = "newsletter_subscribers"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String, unique=True, index=True, nullable=False)
    subscribed_at = Column(DateTime(timezone=True), server_default=func.now())
