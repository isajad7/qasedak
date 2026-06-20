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
from store.telegram_bot.notifications import send_to_config


logger = logging.getLogger(__name__)

UPSELL_COOLDOWN_SECONDS = 24 * 60 * 60
UPSELL_SKIP_SECONDS = 48 * 60 * 60
UPSELL_ACTIVE_SECONDS = 2 * 60 * 60
UPSELL_EVENT_TYPE = BotEventLog.EventType.WEBHOOK


def upsell_cooldown_key(target_key):
    return f"revenue_engine:upsell_offer:{target_key}"


def upsell_skip_key(target_key):
    return f"revenue_engine:upsell_skip:{target_key}"


def upsell_active_key(target_key):
    return f"revenue_engine:upsell_active:{target_key}"


def _sent_recently(target_key, now=None):
    if not target_key:
        return True
    if cache.get(upsell_cooldown_key(target_key)):
        return True
    now = now or timezone.now()
    return BotEventLog.objects.filter(
        event_type=UPSELL_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        created_at__gte=now - timedelta(seconds=UPSELL_COOLDOWN_SECONDS),
        raw_payload__upsell_engine=True,
        raw_payload__target_key=target_key,
    ).exists()


def _build_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "همین پلن فعلی کافی است", "callback_data": "user:upsell_skip"}],
            [{"text": "مشاهده پلن‌ها", "callback_data": "user:buy"}],
        ]
    }


def _remember_sent(target, decision, context=None, now=None):
    now = now or timezone.now()
    target_key = _target_key(target)
    cache.set(upsell_cooldown_key(target_key), now.isoformat(), UPSELL_COOLDOWN_SECONDS)
    cache.set(upsell_active_key(target_key), now.isoformat(), UPSELL_ACTIVE_SECONDS)
    log = BotEventLog.objects.create(
        bot_config=target.bot_config,
        event_type=UPSELL_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        message="upsell_engine_offer_sent",
        raw_payload={
            "upsell_engine": True,
            "target_key": target_key,
            "chat_id": target.chat_id,
            "decision_type": decision.get("type"),
            "title": decision.get("title"),
            "add_on": decision.get("add_on", ""),
            "experiment": experiment_payload(decision),
            "upgrade_plan_id": getattr(decision.get("upgrade_plan"), "pk", None),
            "context": {
                "event_type": (context or {}).get("event_type"),
                "plan_id": getattr((context or {}).get("selected_plan") or (context or {}).get("plan"), "pk", None),
            },
        },
    )
    try:
        OfferTracker().user_received_offer(
            target_key,
            offer_type_from_decision(decision, "upsell"),
            variant_from_decision(decision),
            bot_config=target.bot_config,
            metadata={
                "engine": "upsell",
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
        logger.warning("Upsell impression tracking skipped target=%s: %s", target_key, exc)
    return log


def user_skipped_recently(target_key):
    return bool(target_key and cache.get(upsell_skip_key(target_key)))


def mark_upsell_skipped(user, context=None):
    target = resolve_target(user, context or {})
    if not target:
        return {"skipped": False, "reason": "no_personal_telegram_target"}
    target_key = _target_key(target)
    cache.set(upsell_skip_key(target_key), timezone.now().isoformat(), UPSELL_SKIP_SECONDS)
    BotEventLog.objects.create(
        bot_config=target.bot_config,
        event_type=UPSELL_EVENT_TYPE,
        status=BotEventLog.Status.SKIPPED,
        message="upsell_engine_user_skipped",
        raw_payload={"upsell_engine": True, "target_key": target_key, "chat_id": target.chat_id},
    )
    return {"skipped": True, "target": target.chat_id}


def execute(user, decision, context=None):
    context = context or {}
    if not decision:
        return {"sent": False, "skipped": True, "reason": "no_decision"}

    target = resolve_target(user, context)
    resolved = resolve_revenue_context(user, context)
    event_type = context.get("event_type")
    guard = can_send_revenue_offer(
        resolved["customer"],
        "upsell",
        event_type,
        store=resolved["store"],
        bot_user=resolved["bot_user"],
        target=target,
        decision=decision,
        force_dry_run=bool(context.get("revenue_dry_run") or context.get("dry_run")),
    )
    guard_metadata = {
        "engine": "upsell",
        "decision_type": decision.get("type"),
        "event_type": event_type,
        "target_key": _target_key(target) if target else "",
        "upgrade_plan_id": getattr(decision.get("upgrade_plan"), "pk", None),
        "add_on": decision.get("add_on", ""),
    }
    if not guard.allowed:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="upsell",
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

    target_key = _target_key(target)
    if user_skipped_recently(target_key):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="upsell",
            event_type=event_type,
            decision=decision,
            status="skipped",
            skip_reason="user_skipped_recently",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "user_skipped_recently"}
    if _sent_recently(target_key):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="upsell",
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
            text=decision.get("message") or "",
            event_type=UPSELL_EVENT_TYPE,
            chat_id=target.chat_id,
            reply_markup=_build_keyboard(),
        )
    except Exception as exc:
        logger.warning(
            "Upsell delivery failed user=%s decision=%s: %s",
            getattr(user, "pk", None),
            decision.get("title"),
            exc,
        )
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="upsell",
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
            engine_type="upsell",
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
        engine_type="upsell",
        event_type=event_type,
        decision=decision,
        status="sent",
        sent_at=timezone.now(),
        store=resolved["store"],
        metadata=guard_metadata,
    )
    _remember_sent(target, decision, context=context)
    return {"sent": True, "skipped": False, "target": target.chat_id, "revenue_offer_log_id": log.pk}
