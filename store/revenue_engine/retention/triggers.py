import logging


logger = logging.getLogger(__name__)

USER_INACTIVE_24H = "user_inactive_24h"
USER_INACTIVE_72H = "user_inactive_72h"
USER_CANCELLED_SUBSCRIPTION = "user_cancelled_subscription"
USER_EXPIRED_NO_RENEW = "user_expired_no_renew"
USER_RETURNED_AFTER_ABSENCE = "user_returned_after_absence"
SILENT_ACTIVE_USER = "silent_active_user"


def emit_event(event_type, user, context=None):
    from .engine import RetentionEngine

    return RetentionEngine().handle(event_type, user, context or {})


def safe_emit_event(event_type, user, context=None):
    try:
        return emit_event(event_type, user, context or {})
    except Exception as exc:
        logger.warning(
            "Retention event skipped event=%s user=%s: %s",
            event_type,
            getattr(user, "pk", None),
            exc,
        )
        return None
