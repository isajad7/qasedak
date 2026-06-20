import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone

from store.models import BotUser, Customer, Order, RevenueOfferLog, Store, VPNClient


SAFE_METADATA_MAX_LENGTH = 240
CONVERSION_WINDOW_DAYS = 7

TOKEN_KEYS = ("token", "secret", "key", "password", "link", "url", "uuid", "phone", "email")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{24,}\b")
URL_RE = re.compile(r"\b(?:https?://|vless://|vmess://|trojan://|ss://)\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[^@\s]{2,}@[^@\s]+\.[^@\s]+\b")
PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s\-()]{8,}\d)\b")


@dataclass(frozen=True)
class RevenueGuardDecision:
    allowed: bool
    status: str
    reason: str = ""
    dry_run: bool = False
    log: RevenueOfferLog | None = None


def get_revenue_settings(store=None):
    return store or Store.objects.filter(is_active=True).order_by("pk").first()


def is_revenue_engine_enabled(store=None):
    settings = get_revenue_settings(store)
    return bool(getattr(settings, "revenue_engine_enabled", True))


def is_engine_enabled(engine_type, store=None):
    settings = get_revenue_settings(store)
    if not is_revenue_engine_enabled(settings):
        return False
    engine_type = str(engine_type or "").strip()
    field_map = {
        RevenueOfferLog.EngineType.RENEWAL: "renewal_engine_enabled",
        RevenueOfferLog.EngineType.UPSELL: "upsell_engine_enabled",
        RevenueOfferLog.EngineType.RETENTION: "retention_engine_enabled",
        RevenueOfferLog.EngineType.SILENT_ACTIVE: "retention_engine_enabled",
        RevenueOfferLog.EngineType.AI_OPTIMIZER: "ai_revenue_optimizer_enabled",
    }
    field_name = field_map.get(engine_type)
    return bool(getattr(settings, field_name, True)) if field_name else True


def is_quiet_hours_now(store=None, now=None):
    settings = get_revenue_settings(store)
    if not settings or not getattr(settings, "revenue_engine_quiet_hours_enabled", False):
        return False
    start = getattr(settings, "revenue_engine_quiet_hours_start", None)
    end = getattr(settings, "revenue_engine_quiet_hours_end", None)
    if not start or not end:
        return False
    timezone_name = getattr(settings, "revenue_engine_timezone", "") or "Asia/Tehran"
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Tehran")
    local_time = timezone.localtime(now or timezone.now(), tz).time()
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _period_start(now, days):
    return now - timedelta(days=days)


def _countable_statuses():
    return [RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED]


def get_user_offer_counts(customer, period, *, bot_user=None, now=None):
    now = now or timezone.now()
    if period == "day":
        since = _period_start(now, 1)
    elif period == "week":
        since = _period_start(now, 7)
    else:
        since = _period_start(now, int(period or 1))
    qs = RevenueOfferLog.objects.filter(created_at__gte=since, status__in=_countable_statuses())
    if customer:
        qs = qs.filter(customer=customer)
    elif bot_user:
        qs = qs.filter(bot_user=bot_user)
    else:
        return 0
    return qs.count()


def should_suppress_due_to_active_offer(customer, engine_type, store=None, *, bot_user=None, now=None):
    settings = get_revenue_settings(store)
    now = now or timezone.now()
    cooldown_hours = getattr(settings, "retention_offer_cooldown_hours", 72) if engine_type in {
        RevenueOfferLog.EngineType.RETENTION,
        RevenueOfferLog.EngineType.SILENT_ACTIVE,
    } else getattr(settings, "revenue_offer_cooldown_hours", 24)
    since = now - timedelta(hours=max(int(cooldown_hours or 24), 1))
    qs = RevenueOfferLog.objects.filter(created_at__gte=since, status__in=_countable_statuses())
    if customer:
        qs = qs.filter(customer=customer)
    elif bot_user:
        qs = qs.filter(bot_user=bot_user)
    else:
        return False
    return qs.exists()


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return max(min(value, Decimal("1")), Decimal("0"))


def decision_source(decision):
    decision = decision or {}
    if decision.get("ai_generated"):
        return RevenueOfferLog.DecisionSource.AI
    if decision.get("ai_fallback_reason"):
        return RevenueOfferLog.DecisionSource.FALLBACK
    if decision.get("experiment_variant") or decision.get("selection_reason"):
        return RevenueOfferLog.DecisionSource.OPTIMIZATION
    return RevenueOfferLog.DecisionSource.RULE


def engine_type_from_context(engine_type, decision=None, context=None):
    decision = decision or {}
    context = context or {}
    if engine_type == RevenueOfferLog.EngineType.RETENTION and (
        decision.get("type") == "support_check_in" or context.get("event_type") == "silent_active_user"
    ):
        return RevenueOfferLog.EngineType.SILENT_ACTIVE
    return engine_type


def offer_type_from_decision(decision):
    decision = decision or {}
    return str(decision.get("optimization_offer_type") or decision.get("type") or "offer")


def variant_from_decision(decision):
    decision = decision or {}
    return str(decision.get("experiment_variant") or decision.get("variant") or "")


def resolve_revenue_context(user=None, context=None, store=None):
    context = context or {}
    bot_user = context.get("bot_user") if isinstance(context.get("bot_user"), BotUser) else None
    customer = context.get("customer") if isinstance(context.get("customer"), Customer) else None
    vpn_client = context.get("vpn_client") if isinstance(context.get("vpn_client"), VPNClient) else None
    order = user if isinstance(user, Order) else context.get("order") if isinstance(context.get("order"), Order) else None

    if isinstance(user, BotUser):
        bot_user = bot_user or user
    elif isinstance(user, Customer):
        customer = customer or user
    elif isinstance(user, VPNClient):
        vpn_client = vpn_client or user
        order = order or user.order

    if bot_user and bot_user.customer_id:
        customer = customer or bot_user.customer
    if order and order.customer_id:
        customer = customer or order.customer
    if vpn_client and vpn_client.order_id and vpn_client.order and vpn_client.order.customer_id:
        customer = customer or vpn_client.order.customer

    resolved_store = store or context.get("store")
    if not resolved_store:
        resolved_store = getattr(bot_user, "bot_config", None).store if bot_user and bot_user.bot_config_id else None
    if not resolved_store and order:
        resolved_store = order.store
    if not resolved_store and vpn_client:
        resolved_store = vpn_client.store or (vpn_client.order.store if vpn_client.order_id and vpn_client.order else None)
    if not resolved_store:
        resolved_store = get_revenue_settings(None)

    return {
        "store": resolved_store,
        "customer": customer,
        "bot_user": bot_user,
        "vpn_client": vpn_client,
        "order": order,
    }


def can_send_revenue_offer(
    customer,
    engine_type,
    event_type,
    store=None,
    now=None,
    *,
    bot_user=None,
    target=None,
    decision=None,
    force_dry_run=False,
    allow_canary_send=False,
):
    settings = get_revenue_settings(store)
    now = now or timezone.now()
    if not is_revenue_engine_enabled(settings):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "revenue_engine_disabled")
    if not is_engine_enabled(engine_type, settings):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "engine_disabled")
    if not target:
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SKIPPED, "no_personal_telegram_target")
    if is_quiet_hours_now(settings, now=now):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "quiet_hours")

    ai_confidence = _decimal_or_none((decision or {}).get("ai_confidence"))
    min_confidence = _decimal_or_none(getattr(settings, "revenue_min_ai_confidence", Decimal("0.50"))) or Decimal("0.50")
    if (decision or {}).get("ai_generated") and ai_confidence is not None and ai_confidence < min_confidence:
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "ai_confidence_below_threshold")

    if should_suppress_due_to_active_offer(customer, engine_type, settings, bot_user=bot_user, now=now):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "cooldown_active")
    daily_count = get_user_offer_counts(customer, "day", bot_user=bot_user, now=now)
    if daily_count >= int(getattr(settings, "revenue_max_offers_per_user_per_day", 1) or 1):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "daily_user_cap")
    weekly_count = get_user_offer_counts(customer, "week", bot_user=bot_user, now=now)
    if weekly_count >= int(getattr(settings, "revenue_max_offers_per_user_per_week", 3) or 3):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "weekly_user_cap")
    day_start = now - timedelta(days=1)
    total_today = RevenueOfferLog.objects.filter(created_at__gte=day_start, status__in=_countable_statuses())
    if settings:
        total_today = total_today.filter(Q(store=settings) | Q(store__isnull=True))
    if total_today.count() >= int(getattr(settings, "revenue_max_total_offers_per_day", 500) or 500):
        return RevenueGuardDecision(False, RevenueOfferLog.Status.SUPPRESSED, "daily_total_cap")
    if force_dry_run:
        return RevenueGuardDecision(False, RevenueOfferLog.Status.DRY_RUN, "dry_run", dry_run=True)
    if bool(getattr(settings, "revenue_engine_dry_run", False)) and not allow_canary_send:
        return RevenueGuardDecision(False, RevenueOfferLog.Status.DRY_RUN, "dry_run", dry_run=True)
    return RevenueGuardDecision(True, RevenueOfferLog.Status.SENT)


def _mask_sensitive_value(value):
    text = str(value or "")
    text = URL_RE.sub("<redacted-link>", text)
    text = UUID_RE.sub("<redacted-uuid>", text)
    text = LONG_HEX_RE.sub("<redacted-token>", text)
    text = EMAIL_RE.sub("<redacted-email>", text)
    text = PHONE_RE.sub("<redacted-phone>", text)
    if len(text) > SAFE_METADATA_MAX_LENGTH:
        text = f"{text[:SAFE_METADATA_MAX_LENGTH]}..."
    return text


def sanitize_revenue_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    safe = {}
    for key, value in metadata.items():
        key_text = str(key or "")
        lower_key = key_text.lower()
        if any(token in lower_key for token in TOKEN_KEYS):
            safe[key_text] = "<redacted>"
        elif isinstance(value, dict):
            safe[key_text] = sanitize_revenue_metadata(value)
        elif isinstance(value, (list, tuple)):
            safe[key_text] = [
                sanitize_revenue_metadata(item) if isinstance(item, dict) else _mask_sensitive_value(item)
                for item in value[:20]
            ]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key_text] = value
        else:
            safe[key_text] = _mask_sensitive_value(value)
    return safe


def record_revenue_offer_attempt(
    *,
    user=None,
    context=None,
    engine_type,
    event_type,
    decision=None,
    status,
    skip_reason="",
    error_message="",
    store=None,
    metadata=None,
    sent_at=None,
):
    resolved = resolve_revenue_context(user, context, store)
    decision = decision or {}
    engine_type = engine_type_from_context(engine_type, decision, context)
    return RevenueOfferLog.objects.create(
        store=resolved["store"],
        customer=resolved["customer"],
        bot_user=resolved["bot_user"],
        vpn_client=resolved["vpn_client"],
        engine_type=engine_type,
        event_type=str(event_type or (context or {}).get("event_type") or ""),
        offer_type=offer_type_from_decision(decision),
        variant=variant_from_decision(decision),
        decision_source=decision_source(decision),
        status=status,
        skip_reason=str(skip_reason or "")[:120],
        error_message=str(error_message or ""),
        ai_confidence=_decimal_or_none(decision.get("ai_confidence")),
        predicted_purchase_probability=_decimal_or_none(decision.get("ai_prediction")),
        sent_at=sent_at,
        metadata=sanitize_revenue_metadata(metadata or {}),
    )


def mark_latest_revenue_offer_converted(customer=None, *, bot_user=None, order=None, store=None, metadata=None, now=None):
    now = now or timezone.now()
    if customer is None and bot_user and bot_user.customer_id:
        customer = bot_user.customer
    if customer is None and order and order.customer_id:
        customer = order.customer
    if customer is None:
        return None
    since = now - timedelta(days=CONVERSION_WINDOW_DAYS)
    qs = RevenueOfferLog.objects.filter(
        customer=customer,
        created_at__gte=since,
        status=RevenueOfferLog.Status.SENT,
    )
    if store is not None:
        qs = qs.filter(Q(store=store) | Q(store__isnull=True))
    log = qs.order_by("-created_at", "-pk").first()
    if not log:
        return None
    log.status = RevenueOfferLog.Status.CONVERTED
    log.converted_at = now
    merged_metadata = dict(log.metadata or {})
    merged_metadata.update(sanitize_revenue_metadata(metadata or {}))
    if order is not None:
        merged_metadata["conversion_order_id"] = getattr(order, "pk", None)
    log.metadata = merged_metadata
    log.save(update_fields=["status", "converted_at", "metadata"])
    return log
