import logging


logger = logging.getLogger(__name__)

USER_EXPIRED = "USER_EXPIRED"
USER_NEAR_EXPIRY = "USER_NEAR_EXPIRY"
HIGH_USAGE_USER = "HIGH_USAGE_USER"

USER_ACTIVE = "USER_ACTIVE"
USER_PURCHASE = "USER_PURCHASE"

HIGH_USAGE = HIGH_USAGE_USER


def emit_event(event_type, user, context=None):
    from .engine import RevenueEngine

    return RevenueEngine().handle(event_type, user, context or {})


def safe_emit_event(event_type, user, context=None):
    try:
        return emit_event(event_type, user, context or {})
    except Exception as exc:
        logger.warning(
            "Revenue event skipped event=%s user=%s: %s",
            event_type,
            getattr(user, "pk", None),
            exc,
        )
        return None
