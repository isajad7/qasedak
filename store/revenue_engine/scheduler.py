import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from store.models import BotConfiguration, BotUser, Order, VPNClient

from .triggers import HIGH_USAGE_USER, USER_EXPIRED, USER_NEAR_EXPIRY, emit_event
from .retention.triggers import (
    SILENT_ACTIVE_USER,
    USER_EXPIRED_NO_RENEW,
    USER_INACTIVE_24H,
    USER_INACTIVE_72H,
    emit_event as emit_retention_event,
)


logger = logging.getLogger(__name__)


def run_revenue_learning_loop(offer_types=None):
    from .ai.predictor import PurchasePredictor
    from .ai.strategy import RevenueStrategyEngine
    from .optimization.scoring import ScoringEngine

    scores = ScoringEngine().update_all_scores(offer_types=offer_types)
    RevenueStrategyEngine().update_strategy_weights()
    PurchasePredictor().update_accuracy()
    return scores


def _usage_percent(vpn_client):
    total = getattr(vpn_client, "traffic_limit_bytes", 0) or 0
    used = getattr(vpn_client, "used_traffic_bytes", 0) or 0
    if total <= 0:
        return Decimal("0")
    try:
        return (Decimal(int(used)) * Decimal("100")) / Decimal(int(total))
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return Decimal("0")


def _expiry_time(vpn_client):
    return getattr(vpn_client, "expiry_time", None) or getattr(vpn_client, "expires_at", None)


def _last_activity(vpn_client):
    return (
        getattr(vpn_client, "last_activity", None)
        or getattr(vpn_client, "last_online_at", None)
        or getattr(vpn_client, "last_synced_at", None)
    )


def _last_connection(vpn_client):
    return (
        getattr(vpn_client, "last_connection", None)
        or getattr(vpn_client, "last_online_at", None)
        or getattr(vpn_client, "last_connected_at", None)
    )


def _client_is_active(vpn_client, now):
    is_active_attr = getattr(vpn_client, "is_active", None)
    if isinstance(is_active_attr, bool):
        active_status = is_active_attr
    elif callable(is_active_attr):
        try:
            active_status = bool(is_active_attr())
        except TypeError:
            active_status = False
    else:
        active_status = vpn_client.status == VPNClient.Status.ACTIVE
    expiry_time = _expiry_time(vpn_client)
    return bool(active_status and (not expiry_time or expiry_time > now))


def _context_for(vpn_client, now):
    expiry_time = _expiry_time(vpn_client)
    usage_percent = _usage_percent(vpn_client)
    last_connection = _last_connection(vpn_client)
    expiry_seconds = (expiry_time - now).total_seconds() if expiry_time else None
    return {
        "expiry_time": expiry_time,
        "expires_at": expiry_time,
        "expiry_seconds": expiry_seconds,
        "usage_percent": usage_percent,
        "last_activity": _last_activity(vpn_client),
        "last_connection": last_connection,
        "last_online_at": last_connection,
    }


def _events_for_context(context):
    events = []
    expiry_seconds = context.get("expiry_seconds")
    if expiry_seconds is not None:
        if expiry_seconds < 0:
            events.append(USER_EXPIRED)
        elif expiry_seconds <= 24 * 60 * 60:
            events.append(USER_NEAR_EXPIRY)

    if context.get("usage_percent", Decimal("0")) > Decimal("80"):
        events.append(HIGH_USAGE_USER)
    return events


def _is_silent_active_user(vpn_client, context, now):
    last_connection = context.get("last_connection")
    if not last_connection:
        return False
    return (
        _client_is_active(vpn_client, now)
        and context.get("usage_percent", Decimal("0")) < Decimal("10")
        and last_connection <= now - timedelta(hours=48)
    )


def _revenue_summary():
    return {
        "scanned": 0,
        "candidates": 0,
        "events": 0,
        "handled": 0,
        "sent": 0,
        "dry_run": 0,
        "suppressed": 0,
        "skipped": 0,
        "skipped_no_target": 0,
        "failed": 0,
        "errors": 0,
        "converted_recent": 0,
        "per_engine": {},
    }


def _record_engine_result(summary, result, engine_type):
    engine_counts = summary["per_engine"].setdefault(
        engine_type,
        {"sent": 0, "dry_run": 0, "suppressed": 0, "skipped": 0, "failed": 0},
    )
    if result and result.get("handled"):
        summary["handled"] += 1
    action = (result or {}).get("action") or {}
    if action.get("sent"):
        summary["sent"] += 1
        engine_counts["sent"] += 1
    elif action.get("dry_run"):
        summary["dry_run"] += 1
        engine_counts["dry_run"] += 1
    elif action.get("failed"):
        summary["failed"] += 1
        engine_counts["failed"] += 1
    elif action.get("skipped"):
        reason = action.get("reason", "")
        if reason == "no_personal_telegram_target":
            summary["skipped_no_target"] += 1
        if reason in {
            "revenue_engine_disabled",
            "engine_disabled",
            "quiet_hours",
            "daily_user_cap",
            "weekly_user_cap",
            "daily_total_cap",
            "cooldown_active",
            "ai_confidence_below_threshold",
            "upsell_active",
            "renewal_active",
        }:
            summary["suppressed"] += 1
            summary["skipped"] += 1
            engine_counts["suppressed"] += 1
        else:
            summary["skipped"] += 1
            engine_counts["skipped"] += 1


def run_revenue_scan(*, dry_run=False, customer_id=None, client_id=None, limit=None):
    now = timezone.now()
    queryset = (
        VPNClient.objects.select_related("store", "order", "order__customer", "order__store", "plan", "inbound", "inbound__panel")
        .filter(status=VPNClient.Status.ACTIVE)
        .order_by("pk")
    )
    if customer_id:
        queryset = queryset.filter(order__customer_id=customer_id)
    if client_id:
        queryset = queryset.filter(pk=client_id)
    if limit:
        queryset = queryset[: int(limit)]
    summary = _revenue_summary()

    for vpn_client in queryset.iterator():
        summary["scanned"] += 1
        context = _context_for(vpn_client, now)
        if dry_run:
            context["revenue_dry_run"] = True
        for event_type in _events_for_context(context):
            summary["candidates"] += 1
            summary["events"] += 1
            try:
                result = emit_event(event_type, vpn_client, context)
            except Exception as exc:
                summary["errors"] += 1
                logger.exception(
                    "Revenue scan event failed event=%s vpn_client=%s: %s",
                    event_type,
                    vpn_client.pk,
                    exc,
                )
                continue

            _record_engine_result(summary, result, "renewal")

        if _is_silent_active_user(vpn_client, context, now):
            _emit_retention(
                summary,
                SILENT_ACTIVE_USER,
                vpn_client,
                {
                    **context,
                    "customer": getattr(getattr(vpn_client, "order", None), "customer", None),
                    "store": vpn_client.store or getattr(getattr(vpn_client, "order", None), "store", None),
                    "vpn_client": vpn_client,
                    "subscription_active": True,
                    "revenue_dry_run": bool(dry_run),
                },
            )

    return summary


def _last_purchase_for_customer(customer):
    if not customer:
        return None
    return (
        Order.objects.filter(
            customer=customer,
            status=Order.Status.COMPLETED,
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def _active_subscription_exists(customer, now):
    if not customer:
        return False
    return VPNClient.objects.filter(
        order__customer=customer,
        expires_at__gt=now,
        status__in=[VPNClient.Status.CREATED, VPNClient.Status.INACTIVE, VPNClient.Status.ACTIVE],
    ).exists()


def _renewed_after(customer, expiry_time):
    if not customer or not expiry_time:
        return False
    return Order.objects.filter(
        customer=customer,
        status=Order.Status.COMPLETED,
        created_at__gt=expiry_time,
    ).exists()


def _retention_result(summary, result):
    _record_engine_result(summary, result, "retention")


def _retention_bot_users():
    return (
        BotUser.objects.select_related("customer", "bot_config", "bot_config__store")
        .filter(
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(chat_id="")
        .exclude(bot_config__bot_token="")
        .order_by("pk")
    )


def _emit_retention(summary, event_type, user, context):
    summary["events"] += 1
    try:
        result = emit_retention_event(event_type, user, context)
    except Exception as exc:
        summary["errors"] += 1
        logger.exception(
            "Retention scan event failed event=%s user=%s: %s",
            event_type,
            getattr(user, "pk", None),
            exc,
        )
        return
    _retention_result(summary, result)


def run_retention_scan(*, dry_run=False, customer_id=None, limit=None):
    now = timezone.now()
    summary = _revenue_summary()

    bot_users = _retention_bot_users()
    if customer_id:
        bot_users = bot_users.filter(customer_id=customer_id)
    if limit:
        bot_users = bot_users[: int(limit)]
    for bot_user in bot_users.iterator():
        summary["scanned"] += 1
        customer = bot_user.customer
        last_active_at = (
            getattr(bot_user, "last_active_at", None)
            or bot_user.last_seen_at
            or getattr(customer, "last_seen_at", None)
        )
        if not last_active_at:
            continue
        event_type = None
        if last_active_at <= now - timedelta(hours=72):
            event_type = USER_INACTIVE_72H
        elif last_active_at <= now - timedelta(hours=24):
            event_type = USER_INACTIVE_24H
        if not event_type:
            continue
        _emit_retention(
            summary,
            event_type,
            bot_user,
            {
                "bot_user": bot_user,
                "customer": customer,
                "store": bot_user.bot_config.store,
                "last_active_at": last_active_at,
                "last_purchase": _last_purchase_for_customer(customer),
                "subscription_active": _active_subscription_exists(customer, now),
                "revenue_dry_run": bool(dry_run),
            },
        )

    seen_customers = set()
    expired_clients = (
        VPNClient.objects.select_related("order", "order__customer", "store", "order__store")
        .filter(order__customer__isnull=False, expires_at__lt=now)
        .exclude(status=VPNClient.Status.DELETED)
        .order_by("order__customer_id", "-expires_at", "-pk")
    )
    if customer_id:
        expired_clients = expired_clients.filter(order__customer_id=customer_id)
    if limit:
        expired_clients = expired_clients[: int(limit)]
    for vpn_client in expired_clients.iterator():
        customer = vpn_client.order.customer
        if not customer or customer.pk in seen_customers:
            continue
        seen_customers.add(customer.pk)
        if _active_subscription_exists(customer, now) or _renewed_after(customer, vpn_client.expires_at):
            continue
        bot_user = _retention_bot_users().filter(customer=customer).first()
        user = bot_user or customer
        _emit_retention(
            summary,
            USER_EXPIRED_NO_RENEW,
            user,
            {
                "bot_user": bot_user,
                "customer": customer,
                "store": vpn_client.store or vpn_client.order.store,
                "vpn_client": vpn_client,
                "expiry_time": vpn_client.expires_at,
                "last_purchase": _last_purchase_for_customer(customer),
                "subscription_active": False,
                "revenue_dry_run": bool(dry_run),
            },
        )

    return summary
