import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .jalali import format_jalali_datetime, persian_digits
from .bot_targets import get_primary_customer_telegram_target, get_vpn_client_telegram_targets
from .models import (
    FreeTrialRequest,
    Order,
    Store,
    VPNClient,
    VPNClientReminderLog,
)
from .xui_api import has_usage_stats, sync_vpn_client_stats

logger = logging.getLogger(__name__)

BYTES_PER_GB = Decimal(1024 ** 3)


@dataclass(frozen=True)
class ReminderSettings:
    store: Store | None
    renewal_reminders_enabled: bool
    reminder_days_before_expiry: tuple[int, ...]
    reminder_days_after_expiry: tuple[int, ...]
    low_traffic_reminders_enabled: bool
    low_traffic_percent_threshold: int
    low_traffic_gb_threshold: Decimal
    reminder_cooldown_hours: int
    reminder_max_per_client_per_day: int
    renewal_reminders_start_at: object = None


@dataclass(frozen=True)
class ReminderDecision:
    reminder_type: str
    trigger_key: str


@dataclass(frozen=True)
class ReminderSendResult:
    status: str
    reminder_type: str
    trigger_key: str
    vpn_client_id: int
    customer_id: int | None = None
    telegram_id: str = ""
    message: str = ""
    error: str = ""
    log_id: int | None = None


def normalize_reminder_days(value, *, default=()):
    if value in (None, ""):
        value = default
    if isinstance(value, str):
        raw_items = (
            value.replace("[", "")
            .replace("]", "")
            .replace("،", ",")
            .replace(";", ",")
            .split(",")
        )
        value = [item.strip() for item in raw_items if item.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError("Reminder days must be a list of integers.")

    days = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("Reminder days must be integers.")
        try:
            day = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("Reminder days must be integers.") from exc
        if day < 0:
            raise ValueError("Reminder days cannot be negative.")
        if day not in days:
            days.append(day)
    return tuple(sorted(days, reverse=True))


def _positive_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _decimal(value, default):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return number if number > 0 else default


def get_reminder_settings(store=None):
    store = store or Store.objects.filter(is_active=True).order_by("id").first() or Store.objects.order_by("id").first()
    try:
        before_days = normalize_reminder_days(
            getattr(store, "reminder_days_before_expiry", None),
            default=(3, 1, 0),
        )
    except ValueError:
        logger.warning("Invalid reminder_days_before_expiry for store=%s; using defaults.", getattr(store, "pk", None))
        before_days = (3, 1, 0)
    try:
        after_days = normalize_reminder_days(
            getattr(store, "reminder_days_after_expiry", None),
            default=(1, 3),
        )
    except ValueError:
        logger.warning("Invalid reminder_days_after_expiry for store=%s; using defaults.", getattr(store, "pk", None))
        after_days = (1, 3)

    percent_threshold = max(min(_positive_int(getattr(store, "low_traffic_percent_threshold", None), 20), 100), 1)
    return ReminderSettings(
        store=store,
        renewal_reminders_enabled=bool(getattr(store, "renewal_reminders_enabled", True)),
        reminder_days_before_expiry=before_days,
        reminder_days_after_expiry=after_days,
        low_traffic_reminders_enabled=bool(getattr(store, "low_traffic_reminders_enabled", True)),
        low_traffic_percent_threshold=percent_threshold,
        low_traffic_gb_threshold=_decimal(getattr(store, "low_traffic_gb_threshold", None), Decimal("2")),
        reminder_cooldown_hours=_positive_int(getattr(store, "reminder_cooldown_hours", None), 24),
        reminder_max_per_client_per_day=_positive_int(getattr(store, "reminder_max_per_client_per_day", None), 1),
        renewal_reminders_start_at=getattr(store, "renewal_reminders_start_at", None),
    )


def get_active_clients_for_reminders(*, store=None, customer_id=None, client_id=None):
    queryset = (
        VPNClient.objects.select_related(
            "store",
            "plan",
            "order",
            "order__customer",
            "inbound",
            "inbound__panel",
        )
        .filter(
            Q(order__customer__isnull=False) | Q(free_trial_requests__customer__isnull=False),
            inbound__isnull=False,
            inbound__is_active=True,
            inbound__panel__isnull=False,
            inbound__panel__is_active=True,
            status__in=[
                VPNClient.Status.CREATED,
                VPNClient.Status.INACTIVE,
                VPNClient.Status.ACTIVE,
                VPNClient.Status.EXPIRED,
            ],
        )
        .exclude(order__status__in=[Order.Status.CANCELLED, Order.Status.REJECTED])
        .distinct()
        .order_by("expires_at", "pk")
    )
    if store:
        queryset = queryset.filter(Q(store=store) | Q(order__store=store) | Q(plan__store=store))
    if customer_id:
        queryset = queryset.filter(Q(order__customer_id=customer_id) | Q(free_trial_requests__customer_id=customer_id))
    if client_id:
        queryset = queryset.filter(pk=client_id)
    return queryset


def _usage_from_stats(vpn_client, stats):
    stats = stats or {}
    raw = stats.get("raw") if isinstance(stats.get("raw"), dict) else {}
    raw_traffic = raw.get("traffic") if isinstance(raw.get("traffic"), dict) else {}
    raw_client = raw.get("client") if isinstance(raw.get("client"), dict) else {}
    panel_available = bool(stats.get("panel_available", True))
    raw_usage_known = has_usage_stats(raw_traffic) or has_usage_stats(raw_client)
    total = stats.get("total_traffic_bytes")
    used = stats.get("used_traffic_bytes")
    remaining = stats.get("remaining_traffic_bytes")
    usage_known = bool(panel_available and raw_usage_known and total and remaining is not None and used is not None)

    return {
        "total_traffic_bytes": int(total or 0),
        "used_upload_bytes": int(stats.get("used_upload_bytes") or 0),
        "used_download_bytes": int(stats.get("used_download_bytes") or 0),
        "used_traffic_bytes": int(used or 0),
        "remaining_traffic_bytes": int(remaining or 0) if remaining is not None else None,
        "expiry_at": stats.get("expiry_at") or vpn_client.expires_at,
        "is_enabled": stats.get("is_enabled"),
        "panel_available": panel_available,
        "usage_known": usage_known,
        "source": "xui" if usage_known else "unknown",
        "error": stats.get("error") or "",
        "raw": raw,
    }


def _local_usage(vpn_client, *, error=""):
    local_known = bool(vpn_client.traffic_limit_bytes and (vpn_client.last_synced_at or vpn_client.used_traffic_bytes))
    return {
        "total_traffic_bytes": int(vpn_client.traffic_limit_bytes or 0),
        "used_upload_bytes": int(vpn_client.used_upload_bytes or 0),
        "used_download_bytes": int(vpn_client.used_download_bytes or 0),
        "used_traffic_bytes": int(vpn_client.used_traffic_bytes or 0),
        "remaining_traffic_bytes": int(vpn_client.remaining_traffic_bytes) if local_known else None,
        "expiry_at": vpn_client.expires_at,
        "is_enabled": vpn_client.status == VPNClient.Status.ACTIVE,
        "panel_available": False,
        "usage_known": local_known,
        "source": "local" if local_known else "unknown",
        "error": error,
        "raw": {},
    }


def get_live_client_usage(vpn_client):
    local_fallback = _local_usage(vpn_client)
    try:
        stats = sync_vpn_client_stats(vpn_client, force=True, create_snapshot=False)
    except Exception as exc:
        logger.warning("Could not sync usage for reminder vpn_client=%s: %s", vpn_client.pk, exc)
        return _local_usage(vpn_client, error=str(exc))

    usage = _usage_from_stats(vpn_client, stats)
    if usage["usage_known"]:
        return usage

    if local_fallback["usage_known"]:
        local_fallback["error"] = usage.get("error", "")
        return local_fallback

    return usage


def _expiry_at(vpn_client, live_usage=None):
    if live_usage and live_usage.get("expiry_at"):
        return live_usage.get("expiry_at")
    return vpn_client.expires_at


def _days_until(expiry_at, now):
    if not expiry_at:
        return None
    return (timezone.localtime(expiry_at).date() - timezone.localtime(now).date()).days


def should_send_expiry_reminder(vpn_client, settings, now, live_usage=None):
    if not settings.renewal_reminders_enabled:
        return []

    expiry_at = _expiry_at(vpn_client, live_usage)
    days = _days_until(expiry_at, now)
    if days is None:
        return []
    if days > 0 and days in settings.reminder_days_before_expiry:
        return [ReminderDecision(VPNClientReminderLog.ReminderType.EXPIRY_BEFORE, f"before_{days}d")]
    if days == 0 and 0 in settings.reminder_days_before_expiry:
        return [ReminderDecision(VPNClientReminderLog.ReminderType.EXPIRY_TODAY, "today")]
    if days < 0:
        after_days = abs(days)
        if after_days in settings.reminder_days_after_expiry:
            return [ReminderDecision(VPNClientReminderLog.ReminderType.EXPIRY_AFTER, f"after_{after_days}d")]
    return []


def should_send_low_traffic_reminder(vpn_client, settings, live_usage, now):
    if not settings.low_traffic_reminders_enabled:
        return None
    expiry_at = _expiry_at(vpn_client, live_usage)
    if expiry_at and expiry_at <= now:
        return None
    if not live_usage or not live_usage.get("usage_known"):
        return None

    total = int(live_usage.get("total_traffic_bytes") or 0)
    remaining = live_usage.get("remaining_traffic_bytes")
    if total <= 0 or remaining is None:
        return None
    remaining = max(int(remaining), 0)
    remaining_percent = (Decimal(remaining) * Decimal("100")) / Decimal(total)
    if remaining_percent <= Decimal(settings.low_traffic_percent_threshold):
        return ReminderDecision(
            VPNClientReminderLog.ReminderType.LOW_TRAFFIC,
            f"low_traffic_{settings.low_traffic_percent_threshold}pct",
        )

    gb_threshold_bytes = int(settings.low_traffic_gb_threshold * BYTES_PER_GB)
    if remaining <= gb_threshold_bytes:
        gb_label = _decimal_label(settings.low_traffic_gb_threshold)
        return ReminderDecision(VPNClientReminderLog.ReminderType.LOW_TRAFFIC, f"low_traffic_{gb_label}gb")
    return None


def calculate_client_reminder_status(vpn_client, live_usage=None, *, settings=None, now=None):
    now = now or timezone.now()
    settings = settings or get_reminder_settings(vpn_client.store or getattr(vpn_client.order, "store", None))
    live_usage = live_usage if live_usage is not None else get_live_client_usage(vpn_client)
    decisions = list(should_send_expiry_reminder(vpn_client, settings, now, live_usage))
    traffic_decision = should_send_low_traffic_reminder(vpn_client, settings, live_usage, now)
    if traffic_decision:
        decisions.append(traffic_decision)
    return decisions


def _customer_for_client(vpn_client):
    order = getattr(vpn_client, "order", None)
    if order and getattr(order, "customer", None):
        return order.customer
    if not getattr(vpn_client, "pk", None):
        return None
    trial_request = (
        FreeTrialRequest.objects.select_related("customer")
        .filter(vpn_client=vpn_client, customer__isnull=False)
        .order_by("-created_at", "-pk")
        .first()
    )
    return getattr(trial_request, "customer", None)


def _client_store(vpn_client, settings=None):
    if settings and settings.store:
        return settings.store
    if vpn_client.store_id:
        return vpn_client.store
    if getattr(vpn_client, "order_id", None) and vpn_client.order.store_id:
        return vpn_client.order.store
    if vpn_client.plan_id and vpn_client.plan.store_id:
        return vpn_client.plan.store
    return None


def resolve_customer_telegram_bot_user(customer, *, store=None):
    return get_primary_customer_telegram_target(customer, store=store)


def _client_created_at_for_reminder(vpn_client):
    return getattr(vpn_client, "created_at", None) or getattr(getattr(vpn_client, "order", None), "created_at", None)


def _client_is_before_reminder_start(vpn_client, settings):
    start_at = getattr(settings, "renewal_reminders_start_at", None)
    created_at = _client_created_at_for_reminder(vpn_client)
    return bool(start_at and created_at and created_at < start_at)


def _decimal_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    label = format(number.normalize(), "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def _format_gb_from_bytes(value):
    if value is None:
        return "نامشخص"
    number = (Decimal(max(int(value), 0)) / BYTES_PER_GB).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return persian_digits(_decimal_label(number))


def _service_name(vpn_client):
    return vpn_client.xui_email or vpn_client.username or f"#{vpn_client.pk}"


def _remaining_time_label(vpn_client, live_usage=None):
    expiry_at = _expiry_at(vpn_client, live_usage)
    if not expiry_at:
        return "نامحدود"
    now = timezone.now()
    days = _days_until(expiry_at, now)
    if days is None:
        return "نامشخص"
    if days < 0:
        return f"{persian_digits(abs(days))} روز گذشته"
    if days == 0:
        return "امروز"
    return f"{persian_digits(days)} روز"


def build_reminder_message(vpn_client, reminder_type, trigger_key, live_usage=None):
    live_usage = live_usage or {}
    name = _service_name(vpn_client)
    expiry_at = _expiry_at(vpn_client, live_usage)
    remaining = live_usage.get("remaining_traffic_bytes") if live_usage.get("usage_known") else None

    if reminder_type == VPNClientReminderLog.ReminderType.LOW_TRAFFIC:
        total = live_usage.get("total_traffic_bytes")
        used = live_usage.get("used_traffic_bytes")
        return "\n".join(
            [
                "⚠️ حجم سرویس شما رو به پایان است",
                "",
                f"نام سرویس: {name}",
                f"حجم کل: {_format_gb_from_bytes(total)} گیگ",
                f"مصرف‌شده: {_format_gb_from_bytes(used)} گیگ",
                f"باقی‌مانده: {_format_gb_from_bytes(remaining)} گیگ",
                "",
                "برای ادامه استفاده، سرویس را تمدید کنید.",
            ]
        )

    if reminder_type == VPNClientReminderLog.ReminderType.EXPIRY_AFTER:
        return "\n".join(
            [
                "❌ سرویس شما منقضی شده است",
                "",
                f"نام سرویس: {name}",
                f"زمان انقضا: {format_jalali_datetime(expiry_at, default='نامشخص')}",
                "",
                "برای فعال‌سازی دوباره می‌توانید تمدید کنید.",
            ]
        )

    title = "⏰ سرویس شما امروز منقضی می‌شود" if reminder_type == VPNClientReminderLog.ReminderType.EXPIRY_TODAY else "⏰ سرویس شما در حال اتمام است"
    lines = [
        title,
        "",
        f"نام سرویس: {name}",
        f"زمان باقی‌مانده: {_remaining_time_label(vpn_client, live_usage)}",
    ]
    if remaining is not None:
        lines.append(f"حجم باقی‌مانده: {_format_gb_from_bytes(remaining)} گیگ")
    lines.extend(["", "برای جلوگیری از قطع شدن، می‌توانید همین حالا تمدید کنید."])
    return "\n".join(lines)


def build_reminder_keyboard(vpn_client):
    public_id = str(vpn_client.public_id)
    return {
        "inline_keyboard": [
            [{"text": "تمدید همین سرویس 🔄", "callback_data": f"user:client_renew:{public_id}"}],
            [{"text": "مشاهده وضعیت 📊", "callback_data": f"user:client_usage:{public_id}"}],
            [{"text": "خرید سرویس جدید 🛒", "callback_data": "user:buy"}],
        ]
    }


def _today(now=None):
    return timezone.localtime(now or timezone.now()).date()


def _sent_duplicate_exists(vpn_client, reminder_type, trigger_key, trigger_date):
    queryset = VPNClientReminderLog.objects.filter(
        vpn_client=vpn_client,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        status=VPNClientReminderLog.Status.SENT,
    )
    if reminder_type == VPNClientReminderLog.ReminderType.LOW_TRAFFIC:
        queryset = queryset.filter(trigger_date=trigger_date)
    return queryset.exists()


def _latest_attempt(vpn_client, reminder_type, trigger_key):
    queryset = VPNClientReminderLog.objects.filter(vpn_client=vpn_client)
    if reminder_type == VPNClientReminderLog.ReminderType.LOW_TRAFFIC:
        queryset = queryset.filter(reminder_type=VPNClientReminderLog.ReminderType.LOW_TRAFFIC)
    else:
        queryset = queryset.filter(reminder_type=reminder_type, trigger_key=trigger_key)
    return queryset.order_by("-updated_at", "-created_at").first()


def _cooldown_active(vpn_client, reminder_type, trigger_key, settings, now):
    latest = _latest_attempt(vpn_client, reminder_type, trigger_key)
    if not latest:
        return False
    if latest.status == VPNClientReminderLog.Status.SENT and reminder_type != VPNClientReminderLog.ReminderType.LOW_TRAFFIC:
        return False
    last_attempt_at = latest.sent_at or latest.updated_at or latest.created_at
    return last_attempt_at and last_attempt_at > now - timedelta(hours=settings.reminder_cooldown_hours)


def _daily_sent_count(vpn_client, trigger_date):
    return VPNClientReminderLog.objects.filter(
        vpn_client=vpn_client,
        trigger_date=trigger_date,
        status=VPNClientReminderLog.Status.SENT,
    ).count()


def _record_reminder_log(
    *,
    vpn_client,
    customer,
    reminder_type,
    trigger_key,
    trigger_date,
    status,
    message_text="",
    telegram_id="",
    error_message="",
    sent_at=None,
):
    log, _created = VPNClientReminderLog.objects.get_or_create(
        vpn_client=vpn_client,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        trigger_date=trigger_date,
        defaults={"customer": customer, "status": status},
    )
    log.customer = customer
    log.status = status
    log.message_text = message_text or log.message_text
    log.sent_to_telegram_id = telegram_id or log.sent_to_telegram_id
    log.error_message = error_message
    if sent_at is not None:
        log.sent_at = sent_at
    log.save(
        update_fields=[
            "customer",
            "status",
            "message_text",
            "sent_to_telegram_id",
            "error_message",
            "sent_at",
            "updated_at",
        ]
    )
    return log


def _skip_result(vpn_client, customer, reminder_type, trigger_key, reason, *, settings, live_usage=None, now=None):
    now = now or timezone.now()
    message_text = build_reminder_message(vpn_client, reminder_type, trigger_key, live_usage)
    log = _record_reminder_log(
        vpn_client=vpn_client,
        customer=customer,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        trigger_date=_today(now),
        status=VPNClientReminderLog.Status.SKIPPED,
        message_text=message_text,
        error_message=reason,
    )
    return ReminderSendResult(
        status=VPNClientReminderLog.Status.SKIPPED,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        vpn_client_id=vpn_client.pk,
        customer_id=getattr(customer, "pk", None),
        message=reason,
        log_id=log.pk,
    )


def send_reminder_to_customer(vpn_client, reminder_type, trigger_key, live_usage=None, *, settings=None, dry_run=False, now=None):
    from .telegram_bot.client import BotClient, BotDeliveryError

    now = now or timezone.now()
    settings = settings or get_reminder_settings(_client_store(vpn_client))
    customer = _customer_for_client(vpn_client)
    trigger_date = _today(now)

    if _sent_duplicate_exists(vpn_client, reminder_type, trigger_key, trigger_date):
        return ReminderSendResult(
            status=VPNClientReminderLog.Status.SKIPPED,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
            vpn_client_id=vpn_client.pk,
            customer_id=getattr(customer, "pk", None),
            message="Reminder was already sent for this trigger.",
        )

    if _cooldown_active(vpn_client, reminder_type, trigger_key, settings, now):
        return ReminderSendResult(
            status=VPNClientReminderLog.Status.SKIPPED,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
            vpn_client_id=vpn_client.pk,
            customer_id=getattr(customer, "pk", None),
            message="Reminder cooldown is active.",
        )

    if _daily_sent_count(vpn_client, trigger_date) >= settings.reminder_max_per_client_per_day:
        return _skip_result(
            vpn_client,
            customer,
            reminder_type,
            trigger_key,
            "Daily reminder limit reached for this VPN client.",
            settings=settings,
            live_usage=live_usage,
            now=now,
        )

    store = _client_store(vpn_client, settings)
    targets = get_vpn_client_telegram_targets(vpn_client, store=store)
    target = targets[0] if targets else None
    message_text = build_reminder_message(vpn_client, reminder_type, trigger_key, live_usage)
    if not target or not target.bot_config:
        if dry_run:
            return ReminderSendResult(
                status=VPNClientReminderLog.Status.SKIPPED,
                reminder_type=reminder_type,
                trigger_key=trigger_key,
                vpn_client_id=vpn_client.pk,
                customer_id=getattr(customer, "pk", None),
                message="No active Telegram target was found for this VPN client.",
            )
        return _skip_result(
            vpn_client,
            customer,
            reminder_type,
            trigger_key,
            "No active Telegram target was found for this VPN client.",
            settings=settings,
            live_usage=live_usage,
            now=now,
        )
    if dry_run:
        return ReminderSendResult(
            status="would_send",
            reminder_type=reminder_type,
            trigger_key=trigger_key,
            vpn_client_id=vpn_client.pk,
            customer_id=getattr(customer, "pk", None),
            telegram_id=target.chat_id,
            message=message_text,
        )

    try:
        with transaction.atomic():
            log = _record_reminder_log(
                vpn_client=vpn_client,
                customer=customer,
                reminder_type=reminder_type,
                trigger_key=trigger_key,
                trigger_date=trigger_date,
                status=VPNClientReminderLog.Status.SKIPPED,
                message_text=message_text,
                telegram_id=target.chat_id,
            )
        BotClient(target.bot_config).send_message(
            message_text,
            chat_id=target.chat_id,
            reply_markup=build_reminder_keyboard(vpn_client),
            parse_mode=None,
        )
    except BotDeliveryError as exc:
        error = str(exc)
        log = _record_reminder_log(
            vpn_client=vpn_client,
            customer=customer,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
                trigger_date=trigger_date,
                status=VPNClientReminderLog.Status.FAILED,
                message_text=message_text,
                telegram_id=getattr(target, "chat_id", ""),
                error_message=error,
            )
        logger.warning("Renewal reminder delivery failed vpn_client=%s chat_id=%s: %s", vpn_client.pk, target.chat_id, error)
        return ReminderSendResult(
            status=VPNClientReminderLog.Status.FAILED,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
            vpn_client_id=vpn_client.pk,
            customer_id=getattr(customer, "pk", None),
            telegram_id=target.chat_id,
            error=error,
            log_id=log.pk,
        )
    except Exception as exc:
        error = str(exc)
        log = _record_reminder_log(
            vpn_client=vpn_client,
            customer=customer,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
                trigger_date=trigger_date,
                status=VPNClientReminderLog.Status.FAILED,
                message_text=message_text,
                telegram_id=getattr(target, "chat_id", ""),
                error_message=error,
            )
        logger.exception("Renewal reminder failed vpn_client=%s", vpn_client.pk)
        return ReminderSendResult(
            status=VPNClientReminderLog.Status.FAILED,
            reminder_type=reminder_type,
            trigger_key=trigger_key,
            vpn_client_id=vpn_client.pk,
            customer_id=getattr(customer, "pk", None),
            telegram_id=getattr(target, "chat_id", ""),
            error=error,
            log_id=log.pk,
        )

    sent_at = timezone.now()
    log = _record_reminder_log(
        vpn_client=vpn_client,
        customer=customer,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        trigger_date=trigger_date,
        status=VPNClientReminderLog.Status.SENT,
        message_text=message_text,
        telegram_id=target.chat_id,
        sent_at=sent_at,
    )
    return ReminderSendResult(
        status=VPNClientReminderLog.Status.SENT,
        reminder_type=reminder_type,
        trigger_key=trigger_key,
        vpn_client_id=vpn_client.pk,
        customer_id=getattr(customer, "pk", None),
        telegram_id=target.chat_id,
        message=message_text,
        log_id=log.pk,
    )


def _decision_matches(decision, reminder_type_filter):
    reminder_type_filter = reminder_type_filter or "all"
    if reminder_type_filter == "all":
        return True
    if reminder_type_filter == "expiry":
        return decision.reminder_type in {
            VPNClientReminderLog.ReminderType.EXPIRY_BEFORE,
            VPNClientReminderLog.ReminderType.EXPIRY_TODAY,
            VPNClientReminderLog.ReminderType.EXPIRY_AFTER,
        }
    if reminder_type_filter == "traffic":
        return decision.reminder_type == VPNClientReminderLog.ReminderType.LOW_TRAFFIC
    return False


def run_renewal_reminders(
    dry_run=False,
    limit=None,
    *,
    customer_id=None,
    client_id=None,
    reminder_type="all",
    now=None,
):
    now = now or timezone.now()
    queryset = get_active_clients_for_reminders(customer_id=customer_id, client_id=client_id)
    limit_value = _positive_int(limit, 0) if limit else 0

    summary = {
        "total_clients_seen": 0,
        "ignored_before_start_at": 0,
        "old_skipped": 0,
        "candidates": 0,
        "due": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "would_send": 0,
        "dry_run": bool(dry_run),
        "results": [],
    }

    for vpn_client in queryset:
        settings = get_reminder_settings(_client_store(vpn_client))
        if _client_is_before_reminder_start(vpn_client, settings):
            summary["total_clients_seen"] += 1
            summary["ignored_before_start_at"] += 1
            summary["old_skipped"] += 1
            continue
        if limit_value and summary["candidates"] >= limit_value:
            break

        summary["total_clients_seen"] += 1
        summary["candidates"] += 1
        live_usage = get_live_client_usage(vpn_client)
        decisions = calculate_client_reminder_status(vpn_client, live_usage, settings=settings, now=now)
        decisions = [decision for decision in decisions if _decision_matches(decision, reminder_type)]
        summary["due"] += len(decisions)

        for decision in decisions:
            result = send_reminder_to_customer(
                vpn_client,
                decision.reminder_type,
                decision.trigger_key,
                live_usage,
                settings=settings,
                dry_run=dry_run,
                now=now,
            )
            if result.status == VPNClientReminderLog.Status.SENT:
                summary["sent"] += 1
            elif result.status == VPNClientReminderLog.Status.FAILED:
                summary["failed"] += 1
            elif result.status == "would_send":
                summary["would_send"] += 1
            else:
                summary["skipped"] += 1
            summary["results"].append(result)
    return summary
