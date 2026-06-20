import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from store.models import BotEventLog
from store.revenue_engine.actions import _target_key, resolve_target
from store.revenue_engine.guards import (
    can_send_revenue_offer,
    record_revenue_offer_attempt,
    resolve_revenue_context,
)
from store.revenue_engine.optimization.tracker import (
    OfferTracker,
    experiment_payload,
    offer_type_from_decision,
    variant_from_decision,
)
from store.revenue_engine.upsell.actions import upsell_active_key, upsell_cooldown_key
from store.telegram_bot.notifications import send_to_config


logger = logging.getLogger(__name__)

RETENTION_COOLDOWN_SECONDS = 48 * 60 * 60
RETENTION_DISCOUNT_COOLDOWN_SECONDS = 7 * 24 * 60 * 60
RETENTION_IGNORED_SECONDS = 72 * 60 * 60
SILENT_ALERT_COOLDOWN_SECONDS = 72 * 60 * 60
RETENTION_EVENT_TYPE = BotEventLog.EventType.WEBHOOK

SILENT_SUPPORT_MESSAGE = """⚠️ شما هنوز سرویس فعال دارید اما استفاده‌ای ثبت نشده

اگر مشکلی دارید:
- اتصال
- تنظیمات
- یا نیاز به تغییر سرور

پشتیبانی در دسترس است"""


def retention_cooldown_key(target_key):
    return f"revenue_engine:retention_offer:{target_key}"


def retention_discount_key(target_key):
    return f"revenue_engine:retention_discount:{target_key}"


def retention_ignored_key(target_key):
    return f"revenue_engine:retention_ignored:{target_key}"


def silent_alert_key(target_key):
    return f"revenue_engine:silent_active:{target_key}"


def renewal_active_key(target_key):
    return f"revenue_engine:offer:{target_key}"


def _recent_payload_exists(payload_key, target_key, seconds, *, status=BotEventLog.Status.SENT, now=None):
    if not target_key:
        return True
    now = now or timezone.now()
    return BotEventLog.objects.filter(
        event_type=RETENTION_EVENT_TYPE,
        status=status,
        created_at__gte=now - timedelta(seconds=seconds),
        raw_payload__target_key=target_key,
        **{f"raw_payload__{payload_key}": True},
    ).exists()


def _retention_sent_recently(target_key, now=None):
    if cache.get(retention_cooldown_key(target_key)):
        return True
    return _recent_payload_exists("retention_engine", target_key, RETENTION_COOLDOWN_SECONDS, now=now)


def _discount_recently(target_key, now=None):
    if cache.get(retention_discount_key(target_key)):
        return True
    now = now or timezone.now()
    logs = BotEventLog.objects.filter(
        event_type=RETENTION_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        created_at__gte=now - timedelta(seconds=RETENTION_DISCOUNT_COOLDOWN_SECONDS),
        raw_payload__retention_engine=True,
        raw_payload__target_key=target_key,
    )
    for log in logs:
        try:
            if int((log.raw_payload or {}).get("discount") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _ignored_recently(target_key, now=None):
    if cache.get(retention_ignored_key(target_key)):
        return True
    return _recent_payload_exists(
        "retention_engine",
        target_key,
        RETENTION_IGNORED_SECONDS,
        status=BotEventLog.Status.SKIPPED,
        now=now,
    )


def _silent_event(context):
    return (context or {}).get("event_type") == "silent_active_user"


def _silent_alert_recently(target_key, now=None):
    if cache.get(silent_alert_key(target_key)):
        return True
    return _recent_payload_exists("silent_active_user", target_key, SILENT_ALERT_COOLDOWN_SECONDS, now=now)


def _recently_connected(context, now=None):
    context = context or {}
    last_connection = context.get("last_connection") or context.get("last_online_at")
    if not last_connection:
        return False
    now = now or timezone.now()
    return last_connection > now - timedelta(hours=48)


def _priority_block_reason(target_key, now=None):
    if cache.get(upsell_active_key(target_key)) or cache.get(upsell_cooldown_key(target_key)):
        return "upsell_active"
    if _recent_payload_exists("upsell_engine", target_key, 24 * 60 * 60, now=now):
        return "upsell_active"
    if cache.get(renewal_active_key(target_key)):
        return "renewal_active"
    if _recent_payload_exists("revenue_engine", target_key, 24 * 60 * 60, now=now):
        return "renewal_active"
    return ""


def _build_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "فعلاً نه", "callback_data": "user:retention_ignore"}],
            [{"text": "مشاهده پلن‌ها", "callback_data": "user:buy"}],
        ]
    }


def _build_silent_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "ارتباط با پشتیبانی", "callback_data": "user:support"}],
            [{"text": "فعلاً نه", "callback_data": "user:retention_ignore"}],
        ]
    }


def _message_for_decision(decision, context=None):
    if decision.get("type") == "support_check_in" or _silent_event(context):
        return SILENT_SUPPORT_MESSAGE
    return decision.get("message") or ""


def _remember_sent(target, decision, context=None, now=None):
    now = now or timezone.now()
    target_key = _target_key(target)
    discount = int(decision.get("discount") or 0)
    if _silent_event(context) or decision.get("type") == "support_check_in":
        cache.set(silent_alert_key(target_key), now.isoformat(), SILENT_ALERT_COOLDOWN_SECONDS)
    else:
        cache.set(retention_cooldown_key(target_key), now.isoformat(), RETENTION_COOLDOWN_SECONDS)
    if discount:
        cache.set(retention_discount_key(target_key), now.isoformat(), RETENTION_DISCOUNT_COOLDOWN_SECONDS)
    log = BotEventLog.objects.create(
        bot_config=target.bot_config,
        event_type=RETENTION_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        message="retention_engine_offer_sent",
        raw_payload={
            "retention_engine": True,
            "silent_active_user": bool(_silent_event(context) or decision.get("type") == "support_check_in"),
            "target_key": target_key,
            "chat_id": target.chat_id,
            "decision_type": decision.get("type"),
            "discount": discount,
            "bonus_gb": decision.get("bonus_gb"),
            "experiment": experiment_payload(decision),
            "context": {
                "event_type": (context or {}).get("event_type"),
                "customer_id": getattr((context or {}).get("customer"), "pk", None),
            },
        },
    )
    try:
        OfferTracker().user_received_offer(
            target_key,
            offer_type_from_decision(decision, "retention"),
            variant_from_decision(decision),
            bot_config=target.bot_config,
            metadata={
                "engine": "retention",
                "decision_type": decision.get("type"),
                "event_type": (context or {}).get("event_type"),
                "ai_generated": bool(decision.get("ai_generated")),
                "ai_strategy": decision.get("ai_strategy", ""),
                "ai_confidence": decision.get("ai_confidence"),
                "ai_prediction": decision.get("ai_prediction"),
                "ai_fallback_reason": decision.get("ai_fallback_reason", ""),
                "selection_reason": decision.get("selection_reason", ""),
            },
        )
    except Exception as exc:
        logger.warning("Retention impression tracking skipped target=%s: %s", target_key, exc)
    return log


def mark_retention_ignored(user, context=None):
    target = resolve_target(user, context or {})
    if not target:
        return {"ignored": False, "reason": "no_personal_telegram_target"}
    target_key = _target_key(target)
    cache.set(retention_ignored_key(target_key), timezone.now().isoformat(), RETENTION_IGNORED_SECONDS)
    BotEventLog.objects.create(
        bot_config=target.bot_config,
        event_type=RETENTION_EVENT_TYPE,
        status=BotEventLog.Status.SKIPPED,
        message="retention_engine_user_ignored",
        raw_payload={"retention_engine": True, "target_key": target_key, "chat_id": target.chat_id},
    )
    return {"ignored": True, "target": target.chat_id}


def execute(user, decision, context=None):
    context = context or {}
    if not decision:
        return {"sent": False, "skipped": True, "reason": "no_decision"}

    target = resolve_target(user, context)
    resolved = resolve_revenue_context(user, context)
    event_type = context.get("event_type")
    is_silent = _silent_event(context) or decision.get("type") == "support_check_in"
    guard_engine_type = "silent_active" if is_silent else "retention"
    controlled_real_send = bool(context.get("revenue_canary_send") or context.get("revenue_limited_batch_send"))
    guard = can_send_revenue_offer(
        resolved["customer"],
        guard_engine_type,
        event_type,
        store=resolved["store"],
        bot_user=resolved["bot_user"],
        target=target,
        decision=decision,
        force_dry_run=bool(context.get("revenue_dry_run") or context.get("dry_run")),
        allow_canary_send=controlled_real_send,
    )
    guard_metadata = {
        "engine": guard_engine_type,
        "decision_type": decision.get("type"),
        "event_type": event_type,
        "target_key": _target_key(target) if target else "",
        "silent_active_user": bool(is_silent),
    }
    if context.get("revenue_canary_send"):
        guard_metadata.update(
            {
                "canary": True,
                "source_dry_run_log_id": context.get("source_dry_run_log_id"),
                "canary_command": "send_revenue_canary",
                "canary_command_version": context.get("canary_command_version", ""),
            }
        )
    if context.get("revenue_limited_batch_send"):
        guard_metadata.update(
            {
                "limited_batch": True,
                "batch_id": context.get("limited_batch_id", ""),
                "source_dry_run_log_id": context.get("source_dry_run_log_id"),
                "batch_command_version": context.get("batch_command_version", ""),
            }
        )
    target_key = _target_key(target) if target else ""
    priority_reason = _priority_block_reason(target_key) if target_key else ""
    if priority_reason and guard.reason in {
        "cooldown_active",
        "daily_user_cap",
        "weekly_user_cap",
        "daily_total_cap",
    }:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason=priority_reason,
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": priority_reason, "revenue_offer_log_id": log.pk}
    if is_silent and target_key and _silent_alert_recently(target_key) and guard.reason in {
        "cooldown_active",
        "daily_user_cap",
        "weekly_user_cap",
        "daily_total_cap",
    }:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="silent_cooldown_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "silent_cooldown_active", "revenue_offer_log_id": log.pk}
    if not guard.allowed:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status=guard.status,
            skip_reason=guard.reason,
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {
            "sent": False,
            "skipped": guard.status != "dry_run",
            "dry_run": guard.dry_run,
            "reason": guard.reason,
            "revenue_offer_log_id": log.pk,
        }
    if not target or not target.bot_config or not target.chat_id:
        return {"sent": False, "skipped": True, "reason": "no_personal_telegram_target"}

    now = timezone.now()
    priority_reason = _priority_block_reason(target_key, now=now)
    if priority_reason:
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason=priority_reason,
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": priority_reason}
    if is_silent and _recently_connected(context, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="recently_connected",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "recently_connected"}
    if is_silent and _silent_alert_recently(target_key, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="silent_cooldown_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "silent_cooldown_active"}
    if _discount_recently(target_key, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="discount_cooldown_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "discount_cooldown_active"}
    if _ignored_recently(target_key, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="ignored_cooldown_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "ignored_cooldown_active"}
    if _retention_sent_recently(target_key, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="cooldown_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "cooldown_active"}

    try:
        sent = send_to_config(
            target.bot_config,
            text=_message_for_decision(decision, context),
            event_type=RETENTION_EVENT_TYPE,
            chat_id=target.chat_id,
            reply_markup=_build_silent_keyboard() if is_silent else _build_keyboard(),
        )
    except Exception as exc:
        logger.warning(
            "Retention delivery failed user=%s decision=%s: %s",
            getattr(user, "pk", None),
            decision.get("type"),
            exc,
        )
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="failed",
            error_message=str(exc),
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "failed": True, "reason": str(exc), "revenue_offer_log_id": log.pk}

    if not sent:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type=guard_engine_type,
            event_type=event_type,
            decision=decision,
            status="failed",
            error_message="send_failed",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "failed": True, "reason": "send_failed", "revenue_offer_log_id": log.pk}

    log = record_revenue_offer_attempt(
        user=user,
        context=context,
        engine_type=guard_engine_type,
        event_type=event_type,
        decision=decision,
        status="sent",
        sent_at=now,
        store=resolved["store"],
        metadata=guard_metadata,
    )
    _remember_sent(target, decision, context=context, now=now)
    return {"sent": True, "skipped": False, "target": target.chat_id, "revenue_offer_log_id": log.pk}
