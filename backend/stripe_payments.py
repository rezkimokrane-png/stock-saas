import os
import stripe
from fastapi import HTTPException

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

PLANS = {
    "pro":     os.getenv("STRIPE_PRICE_PRO",     "price_pro_placeholder"),
    "premium": os.getenv("STRIPE_PRICE_PREMIUM", "price_premium_placeholder"),
}
# Inverse pour retrouver le plan à partir du price_id envoyé par Stripe
PRICE_TO_PLAN = {v: k for k, v in PLANS.items()}

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
        # on stocke aussi le plan visé dans les metadata : c'est la source
        # la plus fiable pour le webhook, indépendante des price_id.
        metadata={"plan": plan, "user_id": str(user_id)},
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


def resolve_plan_from_session(session: dict) -> str:
    """
    Détermine le plan réellement acheté.

    BUG CORRIGÉ : le code original mettait `plan = "pro"` en dur pour
    TOUT le monde, y compris les abonnés Premium. On utilise maintenant
    d'abord les metadata posées à la création du checkout (fiable), puis
    en repli les line items Stripe pour retrouver le price_id acheté.
    """
    plan = (session.get("metadata") or {}).get("plan")
    if plan in PLANS:
        return plan

    try:
        line_items = stripe.checkout.Session.list_line_items(session["id"], limit=5)
        for item in line_items.get("data", []):
            price_id = item.get("price", {}).get("id")
            if price_id in PRICE_TO_PLAN:
                return PRICE_TO_PLAN[price_id]
    except Exception as e:
        print(f"[stripe] impossible de résoudre le plan pour {session.get('id')}: {e}")

    return "pro"  # dernier repli si vraiment rien ne matche
