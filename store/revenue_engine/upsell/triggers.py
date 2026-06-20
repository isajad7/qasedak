import logging


logger = logging.getLogger(__name__)

USER_PLAN_SELECTED = "user_plan_selected"
CHECKOUT_STARTED = "checkout_started"
PAYMENT_SCREEN_OPENED = "payment_screen_opened"
LOW_PRICE_PLAN_SELECTED = "low_price_plan_selected"


def emit_event(event_type, user, context=None):
    from .engine import UpsellEngine

    return UpsellEngine().handle(event_type, user, context or {})


def safe_emit_event(event_type, user, context=None):
    try:
        return emit_event(event_type, user, context or {})
    except Exception as exc:
        logger.warning(
            "Upsell event skipped event=%s user=%s: %s",
            event_type,
            getattr(user, "pk", None),
            exc,
        )
        return None
