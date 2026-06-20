import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from store.bot_targets import (
    TelegramTarget,
    get_primary_customer_telegram_target,
    get_vpn_client_telegram_targets,
)
from store.models import BotEventLog, BotUser, Customer, Order, VPNClient
from store.revenue_engine.guards import (
    can_send_revenue_offer,
    record_revenue_offer_attempt,
    resolve_revenue_context,
    sanitize_revenue_metadata,
)
from store.revenue_engine.optimization.tracker import (
    OfferTracker,
    experiment_payload,
    offer_type_from_decision,
    variant_from_decision,
)
from store.telegram_bot.notifications import send_to_config


logger = logging.getLogger(__name__)

OFFER_COOLDOWN_SECONDS = 24 * 60 * 60
REVENUE_EVENT_TYPE = BotEventLog.EventType.WEBHOOK


def _target_key(target):
    return str(target.telegram_user_id or target.chat_id or "").strip()


def _client_store(vpn_client, context=None):
    context = context or {}
    store = context.get("store")
    if store:
        return store
    if getattr(vpn_client, "store_id", None):
        return vpn_client.store
    order = getattr(vpn_client, "order", None)
    if order and getattr(order, "store_id", None):
        return order.store
    plan = getattr(vpn_client, "plan", None)
    if plan and getattr(plan, "store_id", None):
        return plan.store
    return None


def resolve_target(user, context=None):
    context = context or {}
    bot_user = context.get("bot_user")
    if isinstance(bot_user, BotUser) and bot_user.chat_id and bot_user.bot_config:
        return TelegramTarget(
            chat_id=str(bot_user.chat_id),
            telegram_user_id=str(bot_user.provider_user_id or bot_user.chat_id),
            bot_config=bot_user.bot_config,
            source="context.bot_user",
        )

    if isinstance(user, BotUser) and user.chat_id and user.bot_config:
        return TelegramTarget(
            chat_id=str(user.chat_id),
            telegram_user_id=str(user.provider_user_id or user.chat_id),
            bot_config=user.bot_config,
            source="bot_user",
        )

    if isinstance(user, VPNClient):
        targets = get_vpn_client_telegram_targets(user, store=_client_store(user, context))
        return targets[0] if targets else None

    if isinstance(user, Order):
        customer = user.customer if user.customer_id else None
        return get_primary_customer_telegram_target(customer, store=user.store) if customer else None

    if isinstance(user, Customer):
        return get_primary_customer_telegram_target(user, store=context.get("store"))

    return None


def _cooldown_key(target_key):
    return f"revenue_engine:offer:{target_key}"


def _upsell_active_key(target_key):
    return f"revenue_engine:upsell_active:{target_key}"


def _sent_recently(target_key, now=None):
    if not target_key:
        return True
    if cache.get(_cooldown_key(target_key)):
        return True
    now = now or timezone.now()
    return BotEventLog.objects.filter(
        event_type=REVENUE_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        created_at__gte=now - timedelta(seconds=OFFER_COOLDOWN_SECONDS),
        raw_payload__revenue_engine=True,
        raw_payload__target_key=target_key,
    ).exists()


def _remember_sent(target, user, decision, context=None, now=None):
    now = now or timezone.now()
    target_key = _target_key(target)
    cache.set(_cooldown_key(target_key), now.isoformat(), OFFER_COOLDOWN_SECONDS)
    order = getattr(user, "order", None) if isinstance(user, VPNClient) else (user if isinstance(user, Order) else None)
    log = BotEventLog.objects.create(
        bot_config=target.bot_config,
        order=order,
        event_type=REVENUE_EVENT_TYPE,
        status=BotEventLog.Status.SENT,
        message="revenue_engine_offer_sent",
        raw_payload={
            "revenue_engine": True,
            "target_key": target_key,
            "chat_id": target.chat_id,
            "decision_type": decision.get("type"),
            "discount": decision.get("discount"),
            "experiment": experiment_payload(decision),
            "vpn_client_id": getattr(user, "pk", None) if isinstance(user, VPNClient) else None,
            "order_id": getattr(getattr(user, "order", None), "pk", None) if isinstance(user, VPNClient) else getattr(user, "pk", None) if isinstance(user, Order) else None,
            "context": {
                "event_type": (context or {}).get("event_type"),
                "usage_percent": str((context or {}).get("usage_percent", "")),
            },
        },
    )
    try:
        OfferTracker().user_received_offer(
            target_key,
            offer_type_from_decision(decision, "renewal"),
            variant_from_decision(decision),
            bot_config=target.bot_config,
            order=order,
            metadata={
                "engine": "renewal",
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
        logger.warning("Offer impression tracking skipped target=%s: %s", target_key, exc)
    return log


def execute(user, decision, context=None):
    context = context or {}
    if not decision:
        return {"sent": False, "skipped": True, "reason": "no_decision"}

    target = resolve_target(user, context)
    resolved = resolve_revenue_context(user, context)
    event_type = context.get("event_type")
    guard = can_send_revenue_offer(
        resolved["customer"],
        "renewal",
        event_type,
        store=resolved["store"],
        bot_user=resolved["bot_user"],
        target=target,
        decision=decision,
        force_dry_run=bool(context.get("revenue_dry_run") or context.get("dry_run")),
    )
    guard_metadata = {
        "engine": "renewal",
        "decision_type": decision.get("type"),
        "event_type": event_type,
        "target_key": _target_key(target) if target else "",
        "context": {
            "usage_percent": str(context.get("usage_percent", "")),
            "source": context.get("source", ""),
        },
    }
    target_key = _target_key(target) if target else ""
    if target_key and cache.get(_upsell_active_key(target_key)) and guard.reason in {
        "cooldown_active",
        "daily_user_cap",
        "weekly_user_cap",
        "daily_total_cap",
    }:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="renewal",
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="upsell_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "upsell_active", "revenue_offer_log_id": log.pk}
    if not guard.allowed:
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="renewal",
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
    if cache.get(_upsell_active_key(target_key)):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="renewal",
            event_type=event_type,
            decision=decision,
            status="suppressed",
            skip_reason="upsell_active",
            store=resolved["store"],
            metadata=guard_metadata,
        )
        return {"sent": False, "skipped": True, "reason": "upsell_active"}
    if _sent_recently(target_key, now=now):
        record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="renewal",
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
            event_type=REVENUE_EVENT_TYPE,
            order=getattr(user, "order", None) if isinstance(user, VPNClient) else (user if isinstance(user, Order) else None),
            chat_id=target.chat_id,
        )
    except Exception as exc:
        logger.warning(
            "Revenue engine Telegram delivery failed user=%s decision=%s: %s",
            getattr(user, "pk", None),
            decision.get("type"),
            exc,
        )
        log = record_revenue_offer_attempt(
            user=user,
            context=context,
            engine_type="renewal",
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
            engine_type="renewal",
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
        engine_type="renewal",
        event_type=event_type,
        decision=decision,
        status="sent",
        sent_at=now,
        store=resolved["store"],
        metadata=sanitize_revenue_metadata(guard_metadata),
    )
    _remember_sent(target, user, decision, context=context, now=now)
    return {"sent": True, "skipped": False, "target": target.chat_id, "revenue_offer_log_id": log.pk}
