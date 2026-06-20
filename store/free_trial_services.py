import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from types import SimpleNamespace

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .jalali import format_jalali_datetime, persian_digits
from .models import Customer, FreeTrialRequest, Inbound, VPNClient
from .naming import build_trial_client_name
from .order_services import get_current_store
from .xui_api import bytes_from_gb, create_trial_client_details, delete_client


logger = logging.getLogger(__name__)

FREE_TRIAL_COOLDOWN_STATUSES = (
    FreeTrialRequest.Status.CREATED,
    FreeTrialRequest.Status.DELIVERED,
)
FREE_TRIAL_LOCK_SECONDS = 120
CONFIG_LINK_LOG_RE = re.compile(r"\b(?:(?:vless|vmess|trojan|ss)://\S+|https?://[^\s<>'\"]*/sub/[^\s<>'\"]*)", re.IGNORECASE)


@dataclass
class FreeTrialResult:
    success: bool
    message: str
    request: FreeTrialRequest | None = None
    vpn_client: VPNClient | None = None
    next_available_at: object = None


def get_free_trial_settings(store=None, bot_config=None):
    store = store or getattr(bot_config, "store", None) or get_current_store()
    if not store:
        return {
            "store": None,
            "enabled": False,
            "panel": None,
            "inbound": None,
            "traffic_gb": Decimal("0"),
            "duration_hours": 0,
            "cooldown_days": 30,
        }
    return {
        "store": store,
        "enabled": bool(store.free_trial_enabled),
        "panel": store.free_trial_panel,
        "inbound": store.free_trial_inbound,
        "traffic_gb": Decimal(store.free_trial_traffic_gb or 0),
        "duration_hours": int(store.free_trial_duration_hours or 0),
        "cooldown_days": int(store.free_trial_cooldown_days or 30),
    }


def validate_free_trial_settings(store=None, bot_config=None):
    settings = get_free_trial_settings(store=store, bot_config=bot_config)
    if not settings["store"] or not settings["enabled"]:
        raise ValidationError("در حال حاضر تست رایگان فعال نیست.")

    errors = []
    panel = settings["panel"]
    inbound = settings["inbound"]
    if not panel:
        errors.append("پنل تست رایگان تنظیم نشده است.")
    elif not panel.is_active:
        errors.append("پنل تست رایگان غیرفعال است.")
    elif panel.store_id and settings["store"] and panel.store_id != settings["store"].pk:
        errors.append("پنل تست رایگان به این فروشگاه تعلق ندارد.")

    if not inbound:
        errors.append("اینباند تست رایگان تنظیم نشده است.")
    elif not inbound.is_active:
        errors.append("اینباند تست رایگان غیرفعال است.")
    elif not getattr(inbound, "available_for_new_orders", True):
        errors.append("اینباند تست رایگان برای ساخت کانفیگ جدید مجاز نیست.")
    elif panel and inbound.panel_id != panel.pk:
        errors.append("اینباند تست رایگان باید متعلق به پنل انتخاب‌شده باشد.")

    if settings["traffic_gb"] <= 0:
        errors.append("حجم تست رایگان باید مثبت باشد.")
    if settings["duration_hours"] <= 0:
        errors.append("مدت تست رایگان باید مثبت باشد.")
    if settings["cooldown_days"] <= 0:
        errors.append("فاصله مجاز دریافت تست رایگان باید مثبت باشد.")

    if errors:
        raise ValidationError(errors)
    return settings


def _trial_identity_filter(customer, telegram_user_id=None):
    query = Q()
    if customer:
        query |= Q(customer=customer)
    telegram_user_id = str(telegram_user_id or "").strip()
    if telegram_user_id:
        query |= Q(telegram_user_id=telegram_user_id)
    return query


def _latest_counting_request(customer, telegram_user_id=None):
    identity_query = _trial_identity_filter(customer, telegram_user_id)
    if not identity_query:
        return None
    return (
        FreeTrialRequest.objects.filter(identity_query, status__in=FREE_TRIAL_COOLDOWN_STATUSES)
        .order_by("-created_at", "-pk")
        .first()
    )


def get_next_free_trial_available_at(customer, telegram_user_id=None, store=None, bot_config=None):
    settings = get_free_trial_settings(store=store, bot_config=bot_config)
    latest_request = _latest_counting_request(customer, telegram_user_id)
    if not latest_request:
        return None
    available_at = latest_request.created_at + timedelta(days=settings["cooldown_days"])
    return available_at if available_at > timezone.now() else None


def can_customer_request_free_trial(customer, telegram_user_id=None, store=None, bot_config=None):
    return get_next_free_trial_available_at(
        customer,
        telegram_user_id=telegram_user_id,
        store=store,
        bot_config=bot_config,
    ) is None


def build_free_trial_client_payload(customer, telegram_user_id=None, *, now=None, settings=None):
    settings = settings or get_free_trial_settings()
    return {
        "email_prefix": build_trial_client_name(customer, telegram_user_id=telegram_user_id),
        "total_gb": settings["traffic_gb"],
        "duration_hours": settings["duration_hours"],
        "limit_ip": 1,
    }


def _trial_lock_key(customer, telegram_user_id=None):
    if telegram_user_id:
        return f"free-trial:create:telegram:{telegram_user_id}"
    if customer:
        return f"free-trial:create:customer:{customer.pk}"
    return ""


def sanitize_free_trial_log_value(value):
    return CONFIG_LINK_LOG_RE.sub("<config-link-redacted>", str(value or ""))


def cleanup_panel_trial_client(client_result, inbound, *, trial_request=None):
    if not client_result or not client_result.get("uuid") or not inbound:
        return False
    target = SimpleNamespace(inbound=inbound, uuid=client_result.get("uuid"))
    deleted = delete_client(target)
    log_context = {
        "request_id": getattr(trial_request, "pk", None),
        "uuid": client_result.get("uuid"),
        "email": client_result.get("email") or "",
        "inbound_id": getattr(inbound, "pk", None),
    }
    if deleted:
        logger.warning(
            "Free trial panel client cleaned up after local persistence failure request_id=%s uuid=%s email=%s inbound_pk=%s",
            log_context["request_id"],
            log_context["uuid"],
            log_context["email"],
            log_context["inbound_id"],
        )
        return True

    logger.warning(
        "Free trial panel client cleanup failed after local persistence failure request_id=%s uuid=%s email=%s inbound_pk=%s",
        log_context["request_id"],
        log_context["uuid"],
        log_context["email"],
        log_context["inbound_id"],
    )
    return False


def create_free_trial_for_customer(customer, telegram_user_id=None, *, store=None, bot_config=None):
    telegram_user_id = str(telegram_user_id or "").strip()
    if not customer and not telegram_user_id:
        return FreeTrialResult(False, "حساب کاربری برای دریافت تست رایگان پیدا نشد.")

    try:
        settings = validate_free_trial_settings(store=store, bot_config=bot_config)
    except ValidationError as exc:
        return FreeTrialResult(False, exc.messages[0] if exc.messages else "در حال حاضر تست رایگان فعال نیست.")

    next_available_at = get_next_free_trial_available_at(
        customer,
        telegram_user_id=telegram_user_id,
        store=settings["store"],
    )
    if next_available_at:
        return FreeTrialResult(
            False,
            f"شما قبلاً تست رایگان دریافت کرده‌اید. امکان دریافت بعدی از تاریخ {format_jalali_datetime(next_available_at)} وجود دارد.",
            next_available_at=next_available_at,
        )

    lock_key = _trial_lock_key(customer, telegram_user_id)
    if lock_key and not cache.add(lock_key, "1", timeout=FREE_TRIAL_LOCK_SECONDS):
        return FreeTrialResult(False, "در حال ساخت تست رایگان قبلی هستیم. چند لحظه بعد دوباره امتحان کنید.")

    trial_request = None
    client_result = None
    vpn_client = None
    try:
        now = timezone.now()
        expires_at = now + timedelta(hours=settings["duration_hours"])
        with transaction.atomic():
            if customer:
                Customer.objects.select_for_update().filter(pk=customer.pk).first()
            next_available_at = get_next_free_trial_available_at(
                customer,
                telegram_user_id=telegram_user_id,
                store=settings["store"],
            )
            if next_available_at:
                return FreeTrialResult(
                    False,
                    f"شما قبلاً تست رایگان دریافت کرده‌اید. امکان دریافت بعدی از تاریخ {format_jalali_datetime(next_available_at)} وجود دارد.",
                    next_available_at=next_available_at,
                )
            trial_request = FreeTrialRequest.objects.create(
                customer=customer,
                telegram_user_id=telegram_user_id,
                panel=settings["panel"],
                inbound=settings["inbound"],
                status=FreeTrialRequest.Status.CREATED,
                traffic_gb=settings["traffic_gb"],
                duration_hours=settings["duration_hours"],
                expires_at=expires_at,
            )

        payload = build_free_trial_client_payload(
            customer,
            telegram_user_id,
            now=now,
            settings=settings,
        )
        client_result = create_trial_client_details(
            panel=settings["panel"],
            inbound=settings["inbound"],
            email_prefix=payload["email_prefix"],
            total_gb=payload["total_gb"],
            duration_hours=payload["duration_hours"],
            limit_ip=payload["limit_ip"],
        )
        if not client_result:
            message = "ساخت تست رایگان روی پنل انجام نشد. کمی بعد دوباره تلاش کنید."
            trial_request.status = FreeTrialRequest.Status.FAILED
            trial_request.error_message = message
            trial_request.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning(
                "Free trial X-UI creation failed request_id=%s customer=%s telegram_user_id=%s panel=%s inbound=%s",
                trial_request.pk,
                getattr(customer, "pk", None),
                telegram_user_id,
                settings["panel"].pk,
                settings["inbound"].pk,
            )
            return FreeTrialResult(False, message, request=trial_request)

        config_link = client_result.get("direct_link") or client_result.get("sub_link") or ""
        expires_at = client_result.get("expires_at") or expires_at
        try:
            with transaction.atomic():
                vpn_client = VPNClient.objects.create(
                    store=settings["store"],
                    order=None,
                    plan=None,
                    inbound=settings["inbound"],
                    username=client_result["email"],
                    xui_email=client_result["email"],
                    uuid=client_result["uuid"],
                    sub_id=client_result.get("sub_id", ""),
                    sub_link=client_result.get("sub_link") or "",
                    direct_link=client_result.get("direct_link") or "",
                    status=VPNClient.Status.ACTIVE,
                    traffic_limit_bytes=bytes_from_gb(settings["traffic_gb"]),
                    duration_days=0,
                    device_limit=payload["limit_ip"],
                    activated_at=now,
                    expires_at=expires_at,
                    xui_raw=client_result.get("raw", {}),
                )
                trial_request.vpn_client = vpn_client
                trial_request.status = FreeTrialRequest.Status.DELIVERED
                trial_request.config_link = config_link
                trial_request.delivered_at = timezone.now()
                trial_request.expires_at = expires_at
                trial_request.save(
                    update_fields=[
                        "vpn_client",
                        "status",
                        "config_link",
                        "delivered_at",
                        "expires_at",
                        "updated_at",
                    ]
                )
                Inbound.objects.filter(pk=settings["inbound"].pk).update(
                    current_users=F("current_users") + 1,
                    updated_at=timezone.now(),
                )
        except Exception as exc:
            cleanup_panel_trial_client(client_result, settings["inbound"], trial_request=trial_request)
            safe_error = sanitize_free_trial_log_value(exc)
            if trial_request:
                trial_request.status = FreeTrialRequest.Status.FAILED
                trial_request.error_message = safe_error
                trial_request.save(update_fields=["status", "error_message", "updated_at"])
            logger.warning(
                "Free trial local persistence failed after X-UI creation request_id=%s customer=%s telegram_user_id=%s uuid=%s email=%s error=%s",
                getattr(trial_request, "pk", None),
                getattr(customer, "pk", None),
                telegram_user_id,
                client_result.get("uuid"),
                client_result.get("email") or "",
                safe_error,
            )
            return FreeTrialResult(
                False,
                "ساخت تست رایگان انجام شد اما ثبت محلی آن ناموفق بود. موضوع برای پشتیبانی ثبت شد.",
                request=trial_request,
            )

        return FreeTrialResult(
            True,
            "تست رایگان شما آماده شد.",
            request=trial_request,
            vpn_client=vpn_client,
        )
    except Exception as exc:
        safe_error = sanitize_free_trial_log_value(exc)
        logger.warning(
            "Free trial creation failed customer=%s telegram_user_id=%s error=%s",
            getattr(customer, "pk", None),
            telegram_user_id,
            safe_error,
        )
        if trial_request:
            trial_request.status = FreeTrialRequest.Status.FAILED
            trial_request.error_message = safe_error
            trial_request.save(update_fields=["status", "error_message", "updated_at"])
        return FreeTrialResult(False, "ساخت تست رایگان انجام نشد. لطفاً کمی بعد دوباره تلاش کنید.", request=trial_request)
    finally:
        if lock_key:
            cache.delete(lock_key)


def _decimal_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value or 0)
    label = format(number.normalize(), "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def format_free_trial_preview(settings):
    inbound = settings.get("inbound")
    panel = settings.get("panel")
    server_label = ""
    if inbound:
        server_label = inbound.remark or f"Inbound {inbound.inbound_id}"
    elif panel:
        server_label = panel.name

    lines = [
        "🎁 دریافت تست رایگان",
        "━━━━━━━━━━━━━━",
        f"حجم تست: {persian_digits(_decimal_label(settings['traffic_gb']))} گیگابایت",
        f"مدت اعتبار: {persian_digits(settings['duration_hours'])} ساعت",
    ]
    if server_label:
        lines.append(f"سرور: {server_label}")
    lines.extend(
        [
            "",
            "با تایید، کانفیگ تست رایگان شما ساخته و همین‌جا ارسال می‌شود.",
        ]
    )
    return "\n".join(lines)


def format_free_trial_result(result):
    if not result.success:
        return result.message
    trial_request = result.request
    config_link = (trial_request.config_link if trial_request else "") or getattr(result.vpn_client, "direct_link", "") or ""
    traffic_gb = trial_request.traffic_gb if trial_request else Decimal("0")
    duration_hours = trial_request.duration_hours if trial_request else 0
    return (
        "🎁 تست رایگان شما آماده شد\n\n"
        f"حجم: {persian_digits(_decimal_label(traffic_gb))} گیگابایت\n"
        f"مدت اعتبار: {persian_digits(duration_hours)} ساعت\n\n"
        "لینک کانفیگ:\n"
        f"<pre>{escape(config_link)}</pre>\n\n"
        "برای کپی، روی متن کانفیگ بزنید یا نگه دارید.\n\n"
        "اگر خواستید سرویس اصلی تهیه کنید، از گزینه «خرید سرویس 🛒» استفاده کنید."
    )
