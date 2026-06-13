import os
import stripe
from fastapi import HTTPException

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

PLANS = {
    "pro":     os.getenv("STRIPE_PRICE_PRO",     "price_pro_placeholder"),
    "premium": os.getenv("STRIPE_PRICE_PREMIUM", "price_premium_placeholder"),
}

APP_URL = os.getenv("APP_URL", "http://localhost:8000")


def create_checkout_session(email: str, plan: str, user_id: int) -> str:
    if plan not in PLANS:
        raise HTTPException(400, "Plan inconnu")
    if not stripe.api_key:
        raise HTTPException(503, "Stripe non configuré (STRIPE_SECRET_KEY manquant)")
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer_email=email,
        client_reference_id=str(user_id),
        line_items=[{"price": PLANS[plan], "quantity": 1}],
        success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/pricing",
    )
    return session.url


def cancel_subscription(stripe_sub_id: str):
    if not stripe_sub_id or not stripe.api_key:
        return
    stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True)


def handle_webhook(payload: bytes, sig: str) -> dict:
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as e:
        raise HTTPException(400, str(e))
    return event
