import json
import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import PurePosixPath

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .jalali import format_jalali_datetime, persian_digits
from .bot_proxy import bot_request_kwargs, capture_telegram_webhook_response
from .models import (
    BotConfiguration,
    BotAdminOrderMessage,
    BotEventLog,
    BotPendingAction,
    BotUser,
    BroadcastMessage,
    Customer,
    DiscountCode,
    Order,
    PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES,
    PAYMENT_RECEIPT_ALLOWED_EXTENSIONS,
    Panel,
    SupportConversation,
    SupportMessage,
    VPNClient,
    validate_payment_receipt_image,
)
from .order_services import (
    CUSTOM_VOLUME_DURATION_DAYS,
    CUSTOM_VOLUME_MAX_GB,
    CUSTOM_VOLUME_MIN_GB,
    MAX_ORDER_QUANTITY,
    OPERATOR_INVALID_MESSAGE,
    OPERATOR_NO_PLANS_MESSAGE,
    OPERATOR_REQUIRED_MESSAGE,
    create_manual_payment_order,
    custom_volume_is_available,
    create_renewal_payment_order,
    get_active_operator,
    get_active_operators,
    get_current_store,
    get_custom_volume_currency,
    get_or_create_custom_volume_plan,
    get_store_plans,
    get_customer_wholesale_discount_percent,
    normalize_custom_volume_gb,
    operator_has_available_plans,
    preview_discount,
    sales_mode_requires_operator,
    store_custom_volume_price_per_gb,
    validate_order_quantity,
    calculate_percentage_discount,
)
from .referral_services import (
    apply_referral_code,
    get_available_referral_gb,
    get_active_referral_configs,
    get_referral_summary,
    redeem_referral_rewards,
)
from .free_trial_services import (
    create_free_trial_for_customer,
    format_free_trial_preview,
    format_free_trial_result,
    get_next_free_trial_available_at,
    validate_free_trial_settings,
)
from .telegram_membership import CHECK_MEMBERSHIP_CALLBACK, ensure_telegram_membership
from .customer_analytics import (
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_7_DAYS,
    PERIOD_TODAY,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_REFERRER,
    analytics_enabled,
    get_customers_by_segment,
    get_loyal_customers,
    get_period_range,
    get_top_customers,
    get_top_referrers,
    top_customers_limit,
)
from .config_lookup import (
    ConfigIdentifierMissing,
    InvalidConfigLink,
    check_config_usage,
    config_link_fingerprint,
    mask_identifier,
)
from .xui_api import build_config_link_for_identifier, refresh_vpn_client_links, sync_vpn_client_stats

logger = logging.getLogger(__name__)

BOT_TIMEOUT_SECONDS = 12
BOT_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
BOT_STATE_CONFIG_LOOKUP_WAIT_LINK = "config_lookup_wait_link"
CONFIG_LOOKUP_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CACHE_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX = "user:config_lookup_update:"
CONFIG_LOOKUP_RATE_FALLBACK = {}
CONFIG_LOOKUP_UPDATE_RATE_FALLBACK = {}
CONFIG_LOOKUP_LINK_LOG_RE = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
CONFIG_LOOKUP_UUID_LOG_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class BotDeliveryError(Exception):
    pass


def bot_api_timeout():
    return (
        getattr(settings, "BOT_API_CONNECT_TIMEOUT_SECONDS", 3),
        getattr(settings, "BOT_API_READ_TIMEOUT_SECONDS", 8),
    )


class BotClient:
    BASE_URLS = {
        BotConfiguration.Provider.BALE: "https://tapi.bale.ai/bot{token}",
        BotConfiguration.Provider.TELEGRAM: "https://api.telegram.org/bot{token}",
    }
    FILE_BASE_URLS = {
        BotConfiguration.Provider.BALE: "https://tapi.bale.ai/file/bot{token}",
        BotConfiguration.Provider.TELEGRAM: "https://api.telegram.org/file/bot{token}",
    }

    def __init__(self, config):
        self.config = config
        self.base_url = self.BASE_URLS[config.provider].format(token=config.bot_token).rstrip("/")

    def sanitized_error(self, exc):
        message = str(exc)
        token = str(self.config.bot_token or "")
        if token:
            message = message.replace(token, "<redacted-token>")
        return message

    def call(self, method, payload, *, timeout=None):
        if capture_telegram_webhook_response(self.config.provider, method, payload):
            return {"ok": True, "result": {}}

        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=timeout or bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                data = {}
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None

        if getattr(response, "ok", True) is False:
            raise BotDeliveryError(
                data.get("description") or data.get("message") or getattr(response, "reason", "") or "Bot API request failed."
            )
        if data.get("ok") is False:
            raise BotDeliveryError(data.get("description") or data.get("message") or "Bot API rejected request.")
        return data

    def call_multipart(self, method, *, data, files):
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                data=data,
                files=files,
                timeout=bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            try:
                payload = response.json()
            except ValueError:
                response.raise_for_status()
                payload = {}
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None

        if getattr(response, "ok", True) is False:
            raise BotDeliveryError(
                payload.get("description") or payload.get("message") or getattr(response, "reason", "") or "Bot API request failed."
            )
        if payload.get("ok") is False:
            raise BotDeliveryError(payload.get("description") or payload.get("message") or "Bot API rejected request.")
        return payload

    def send_message(self, text, *, reply_markup=None, chat_id=None, parse_mode="HTML"):
        target_chat_id = str(chat_id or self.config.admin_user_id)
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode and self.config.is_admin_user(target_chat_id):
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def send_photo(self, photo_file, *, caption="", reply_markup=None, chat_id=None):
        data = {
            "chat_id": str(chat_id or self.config.admin_user_id),
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        filename = PurePosixPath(getattr(photo_file, "name", "") or "receipt.jpg").name
        files = {"photo": (filename, photo_file)}
        return self.call_multipart("sendPhoto", data=data, files=files)

    def edit_message(self, *, chat_id, message_id, text, reply_markup=None):
        if not chat_id or not message_id:
            return None

        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        methods = ["editMessageText"]
        if self.config.provider == BotConfiguration.Provider.BALE:
            methods.append("editMessage")

        last_error = None
        for method in methods:
            try:
                return self.call(method, payload)
            except BotDeliveryError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return None

    def edit_caption(self, *, chat_id, message_id, caption, reply_markup=None):
        if not chat_id or not message_id:
            return None

        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        methods = ["editMessageCaption"]
        if self.config.provider == BotConfiguration.Provider.BALE:
            methods.append("editMessage")

        last_error = None
        for method in methods:
            try:
                return self.call(method, payload)
            except BotDeliveryError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return None

    def forward_message(self, *, from_chat_id, message_id, chat_id=None):
        if not from_chat_id or not message_id:
            return None
        return self.call(
            "forwardMessage",
            {
                "chat_id": str(chat_id or self.config.admin_user_id),
                "from_chat_id": str(from_chat_id),
                "message_id": message_id,
            },
        )

    def delete_message(self, *, chat_id, message_id):
        if not chat_id or not message_id:
            return None
        return self.call(
            "deleteMessage",
            {
                "chat_id": str(chat_id),
                "message_id": message_id,
            },
        )

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path):
        if not file_path:
            return None
        file_base = self.FILE_BASE_URLS[self.config.provider].format(token=self.config.bot_token).rstrip("/")
        file_url = f"{file_base}/{file_path.lstrip('/')}"
        try:
            response = requests.get(
                file_url,
                timeout=bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            response.raise_for_status()
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None
        return response.content

    def answer_callback(self, callback_query_id, text=""):
        if not callback_query_id:
            return None
        try:
            return self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": False,
                },
            )
        except BotDeliveryError:
            return None

    def get_me(self):
        return self.call("getMe", {})

    def delete_webhook(self, *, drop_pending_updates=False):
        return self.call("deleteWebhook", {"drop_pending_updates": bool(drop_pending_updates)})

    def get_updates(self, *, offset=None, timeout=20, limit=100, allowed_updates=None):
        payload = {
            "timeout": int(timeout),
            "limit": int(limit),
            "allowed_updates": allowed_updates or ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        connect_timeout, read_timeout = bot_api_timeout()
        polling_timeout = (connect_timeout, max(read_timeout, int(timeout) + 5))
        return self.call("getUpdates", payload, timeout=polling_timeout)


def active_bot_configs(order=None, *, store=None, reports=False):
    configs = BotConfiguration.objects.filter(is_active=True).exclude(bot_token="").exclude(admin_user_id="")
    store = store or (order.store if order and order.store_id else None)
    if order and order.store_id:
        configs = configs.filter(Q(store=order.store) | Q(store__isnull=True))
    elif store:
        configs = configs.filter(Q(store=store) | Q(store__isnull=True))
    if reports:
        configs = configs.filter(send_sales_reports=True)
    return configs


def is_sales_report_due(config, *, now=None):
    if not config.send_sales_reports:
        return False
    now = now or timezone.now()
    if not config.last_report_sent_at:
        return True
    return config.last_report_sent_at + timedelta(hours=config.report_interval_hours) <= now


def maybe_send_due_sales_report(config):
    if not is_sales_report_due(config):
        return False

    lock_key = f"bot:sales-report-lock:{config.pk}"
    if not cache.add(lock_key, "1", timeout=10 * 60):
        return False

    try:
        config.refresh_from_db(fields=["send_sales_reports", "report_interval_hours", "last_report_sent_at"])
        if not is_sales_report_due(config):
            return False
        report, sent_at = build_sales_report(config)
        if send_to_config(
            config,
            text=report,
            event_type=BotEventLog.EventType.SALES_REPORT,
        ):
            config.last_report_sent_at = sent_at
            config.save(update_fields=["last_report_sent_at", "updated_at"])
            return True
    except Exception as exc:
        logger.warning("Due sales report failed for bot_config=%s: %s", config.pk, exc)
        log_event(
            config,
            event_type=BotEventLog.EventType.ERROR,
            status=BotEventLog.Status.FAILED,
            message=f"Due sales report failed: {exc}",
        )
    finally:
        cache.delete(lock_key)
    return False


def money(value, currency="TOMAN"):
    try:
        return f"{int(value):,} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}"


def bot_money(value, currency="TOMAN"):
    labels = {
        "TOMAN": "تومان",
        "IRR": "ریال",
        "USD": "دلار",
    }
    try:
        amount = f"{int(value):,}"
    except (TypeError, ValueError):
        amount = str(value or 0)
    return f"{persian_digits(amount)} {labels.get(currency, currency)}"


def bot_gb_from_bytes(value):
    try:
        number = round((int(value or 0)) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        number = 0
    label = f"{number:.2f}".rstrip("0").rstrip(".")
    return persian_digits(label)


def clean_decimal_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value or 0)
    label = format(number.normalize(), "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def bot_volume_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return f"{persian_digits(value)} گیگابایت"

    if number < Decimal("1"):
        mb_value = number * Decimal("1000")
        return f"{persian_digits(clean_decimal_label(mb_value))} مگابایت"
    return f"{persian_digits(clean_decimal_label(number))} گیگابایت"


def bot_datetime(value):
    return format_jalali_datetime(value, default="ثبت نشده") if value else "ثبت نشده"


BOT_ORDER_STATUS_LABELS = {
    Order.Status.PENDING_PAYMENT: "در انتظار پرداخت",
    Order.Status.PENDING_VERIFICATION: "در انتظار بررسی",
    Order.Status.CONFIRMED: "تایید شده",
    Order.Status.COMPLETED: "فعال شده",
    Order.Status.REJECTED: "رد شده",
    Order.Status.CANCELLED: "لغو شده",
}

BOT_VERIFICATION_STATUS_LABELS = {
    Order.VerificationStatus.PENDING: "در انتظار بررسی",
    Order.VerificationStatus.VERIFIED: "تایید شده",
    Order.VerificationStatus.REJECTED: "رد شده",
}

BOT_CLIENT_STATUS_LABELS = {
    VPNClient.Status.CREATED: "ساخته شده",
    VPNClient.Status.INACTIVE: "غیرفعال",
    VPNClient.Status.ACTIVE: "فعال",
    VPNClient.Status.SUSPENDED: "متوقف شده",
    VPNClient.Status.EXPIRED: "تمام شده",
    VPNClient.Status.ERROR: "خطا",
}


def bot_order_status(order):
    return BOT_ORDER_STATUS_LABELS.get(order.status, order.get_status_display())


def bot_verification_status(order):
    return BOT_VERIFICATION_STATUS_LABELS.get(order.verification_status, order.get_verification_status_display())


def bot_client_status(client):
    return BOT_CLIENT_STATUS_LABELS.get(client.status, client.get_status_display())


def admin_order_type_label(order):
    metadata = order.metadata or {}
    if metadata.get("renewal") or metadata.get("renewal_client_pk"):
        return "تمدید"
    return "خرید جدید"


def admin_customer_label(order):
    if order.customer_id:
        return (
            order.customer.display_name
            or order.customer.username
            or order.customer.phone_number
            or f"Customer {str(order.customer.public_id)[:8]}"
        )
    return order.sender_card_name or "-"


def admin_customer_phone(order):
    if order.customer_id and order.customer.phone_number:
        return order.customer.phone_number
    return "-"


def admin_customer_telegram_id(order):
    metadata = order.metadata or {}
    bot_metadata = metadata.get("bot") or {}
    provider_user_id = bot_metadata.get("provider_user_id") or bot_metadata.get("chat_id")
    if provider_user_id:
        return str(provider_user_id)
    if not order.customer_id:
        return "-"
    bot_user = (
        BotUser.objects.filter(customer_id=order.customer_id, bot_config__provider=BotConfiguration.Provider.TELEGRAM)
        .order_by("-last_seen_at", "-updated_at")
        .first()
    )
    if not bot_user:
        return "-"
    return bot_user.provider_user_id or bot_user.chat_id or "-"


def admin_receipt_label(order):
    metadata = order.metadata or {}
    if order.payment_receipt_image:
        return "عکس رسید پیوست شده است."
    if (metadata.get("receipt_text") or "").strip():
        return "متن رسید ثبت شده است."
    if metadata.get("receipt"):
        return "رسید در پیام ربات دریافت شده است."
    if order.payment_submitted_at:
        return "اطلاعات پرداخت ثبت شده است."
    return "ثبت نشده"


def order_admin_keyboard(order):
    tracking_code = order.order_tracking_code
    return {
        "inline_keyboard": [
            [
                {"text": "تایید سفارش ✅", "callback_data": f"approve:{tracking_code}"},
                {"text": "رد سفارش ❌", "callback_data": f"reject:{tracking_code}"},
            ],
            [{"text": "مشاهده جزئیات 📦", "callback_data": f"order:detail:{tracking_code}"}],
        ]
    }


def empty_inline_keyboard():
    return {"inline_keyboard": []}


def format_order_message(order, *, title="سفارش جدید VPN"):
    metadata = order.metadata or {}
    receipt_analysis = metadata.get("receipt_analysis") or {}
    receipt_text = (metadata.get("receipt_text") or "").strip()
    duplicate_warning = metadata.get("duplicate_warning") or {}
    source = metadata.get("source") or "-"
    created_at = timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M") if order.created_at else "-"
    paid_at = (
        f"{order.payment_date or '-'} {order.payment_time.strftime('%H:%M') if order.payment_time else ''}".strip()
        if order.payment_date or order.payment_time
        else "-"
    )
    lines = [
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━",
        f"<b>شماره سفارش:</b> <code>#{order.pk}</code>",
        f"<b>کد پیگیری:</b> <code>{order.order_tracking_code}</code>",
        f"<b>نوع:</b> {admin_order_type_label(order)}",
        f"<b>مشتری:</b> {escape(admin_customer_label(order))}",
        f"<b>شماره موبایل:</b> <code>{escape(admin_customer_phone(order))}</code>",
        f"<b>تلگرام:</b> <code>{escape(admin_customer_telegram_id(order))}</code>",
        f"<b>پلن:</b> {escape(order.plan.name) if order.plan_id else '-'}",
        f"<b>اپراتور:</b> {escape(order.operator.name) if order.operator_id else '-'}",
        f"<b>حجم/مدت:</b> {order.plan.volume_gb if order.plan_id else '-'} GB / {order.plan.duration_days if order.plan_id else '-'} روز",
        f"<b>تعداد:</b> {getattr(order, 'quantity', 1) or 1}",
        f"<b>مبلغ نهایی:</b> <code>{money(order.amount, order.currency)}</code>",
        "",
        f"<b>وضعیت:</b> {bot_order_status(order)}",
        f"<b>تایید:</b> {bot_verification_status(order)}",
        f"<b>رسید:</b> {admin_receipt_label(order)}",
        f"<b>زمان ثبت:</b> <code>{created_at}</code>",
        f"<b>زمان پرداخت:</b> <code>{paid_at}</code>",
        "",
        f"<b>پرداخت‌کننده:</b> {escape(order.sender_card_name or '-')}",
        f"<b>۴ رقم کارت:</b> <code>{order.sender_card_last4 or '-'}</code>",
        f"<b>کانفیگ:</b> <code>{order.username}</code>",
        f"<b>منبع:</b> <code>{escape(str(source))}</code>",
    ]
    if order.bank_tracking_code:
        lines.append(f"<b>کد پیگیری بانکی:</b> <code>{escape(order.bank_tracking_code)}</code>")
    if duplicate_warning.get("detected"):
        lines.extend(
            [
                "",
                "<b>هشدار درخواست تکراری</b>",
                f"<b>تعداد تلاش مشابه:</b> <code>{duplicate_warning.get('attempt_count') or 1}</code>",
                f"<b>بازه بررسی:</b> <code>{int((duplicate_warning.get('window_seconds') or 600) / 60)} دقیقه</code>",
                "سفارش جدید ساخته نشد و این درخواست به همین سفارش وصل شد. قبل از تایید، رسید و اطلاعات پرداخت را دقیق‌تر بررسی کن.",
            ]
        )
    if receipt_analysis:
        status = receipt_analysis.get("status") or "-"
        lines.extend(["", f"<b>بررسی رسید:</b> {escape(str(status))}"])
        expected = receipt_analysis.get("expected_amount_irr")
        detected = receipt_analysis.get("matched_amount_irr")
        if expected is not None:
            lines.append(f"<b>مبلغ مورد انتظار:</b> <code>{money(expected, 'IRR')}</code>")
        if detected is not None:
            lines.append(f"<b>مبلغ پیدا شده:</b> <code>{money(detected, 'IRR')}</code>")
        if receipt_analysis.get("requires_admin_review"):
            lines.append("<b>هشدار:</b> رسید نیاز به بررسی دستی دارد.")
        if receipt_analysis.get("warning"):
            lines.append(f"<b>هشدار رسید:</b> {escape(str(receipt_analysis['warning']))}")
    if receipt_text:
        excerpt = receipt_text[:350] + ("..." if len(receipt_text) > 350 else "")
        lines.extend(["", f"<b>متن رسید:</b>\n{escape(excerpt)}"])
    if order.rejection_reason:
        lines.extend(["", f"<b>دلیل رد:</b> {escape(order.rejection_reason)}"])
    return "\n".join(lines)


def log_event(config, *, event_type, status, order=None, message="", raw_payload=None):
    return BotEventLog.objects.create(
        bot_config=config,
        order=order,
        event_type=event_type,
        status=status,
        message=sanitize_bot_event_log_value(message),
        raw_payload=sanitize_bot_event_log_value(raw_payload or {}),
    )


def log_callback(config, *, status, message="", order=None, raw_payload=None):
    logger.info("Bot callback: %s", message)
    return log_event(
        config,
        event_type=BotEventLog.EventType.CALLBACK,
        status=status,
        order=order,
        message=message,
        raw_payload=raw_payload or {},
    )


def send_to_config(config, *, text, event_type, order=None, reply_markup=None, chat_id=None):
    client = BotClient(config)
    target_chat_ids = [str(chat_id)] if chat_id else config.get_admin_user_ids()
    sent = 0
    last_error = ""

    for target_chat_id in target_chat_ids:
        try:
            client.send_message(text, reply_markup=reply_markup, chat_id=target_chat_id)
        except BotDeliveryError as exc:
            last_error = str(exc)
            log_event(
                config,
                event_type=event_type,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=str(exc),
                raw_payload={"chat_id": target_chat_id},
            )
            logger.warning("Bot notification failed for %s chat_id=%s: %s", config, target_chat_id, exc)
            continue

        sent += 1
        log_event(
            config,
            event_type=event_type,
            status=BotEventLog.Status.SENT,
            order=order,
            message=text,
            raw_payload={"chat_id": target_chat_id},
        )

    if sent and config.last_error:
        config.last_error = ""
        config.save(update_fields=["last_error", "updated_at"])
    elif last_error:
        config.last_error = last_error
        config.save(update_fields=["last_error", "updated_at"])
    return sent > 0


def fit_photo_caption(text, *, limit=1000):
    if len(text) <= limit:
        return text
    lines = []
    current_length = 0
    for line in text.splitlines():
        next_length = current_length + len(line) + 1
        if next_length > limit - 45:
            break
        lines.append(line)
        current_length = next_length
    lines.append("جزئیات کامل در پیام بعدی ارسال شد.")
    return "\n".join(lines)


def extract_sent_message_id(payload):
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result") or payload
    if isinstance(result, list):
        result = result[0] if result else {}
    if not isinstance(result, dict):
        return ""
    return str(first_present(result, "message_id", "messageId", "id") or "")


def remember_admin_order_message(config, order, *, admin_user_id, chat_id, message_id, message_kind, metadata=None):
    if not message_id:
        return None
    admin_user_id = str(admin_user_id or chat_id)
    chat_id = str(chat_id or admin_user_id)
    message_id = str(message_id)
    existing = BotAdminOrderMessage.objects.filter(
        bot_config=config,
        order=order,
        admin_user_id=admin_user_id,
        chat_id=chat_id,
        message_id=message_id,
    ).first()
    if existing:
        return existing
    return BotAdminOrderMessage.objects.create(
        bot_config=config,
        order=order,
        admin_user_id=admin_user_id,
        chat_id=chat_id,
        message_id=message_id,
        message_kind=message_kind,
        metadata=metadata or {},
    )


def edit_admin_order_message(client, message_ref, text, *, reply_markup=None):
    if message_ref.message_kind == BotAdminOrderMessage.MessageKind.PHOTO:
        try:
            client.edit_caption(
                chat_id=message_ref.chat_id,
                message_id=message_ref.message_id,
                caption=fit_photo_caption(text),
                reply_markup=reply_markup,
            )
            return True
        except BotDeliveryError:
            pass

    client.edit_message(
        chat_id=message_ref.chat_id,
        message_id=message_ref.message_id,
        text=text,
        reply_markup=reply_markup,
    )
    return True


def sync_admin_order_messages(order, *, title, event_type, prefix_message="", respect_notify=True, configs=None):
    text = format_order_message(order, title=title)
    if prefix_message:
        text = f"{prefix_message}\n\n{text}"

    sent_or_edited = 0
    for config in (configs or active_bot_configs(order)):
        if (
            respect_notify
            and event_type in {BotEventLog.EventType.ORDER_APPROVED, BotEventLog.EventType.ORDER_REJECTED}
            and not config.notify_order_updates
        ):
            continue

        client = BotClient(config)
        refs = list(
            BotAdminOrderMessage.objects.filter(
                bot_config=config,
                order=order,
            ).order_by("created_at", "pk")
        )
        refs_by_admin = {}
        for ref in refs:
            refs_by_admin.setdefault(ref.admin_user_id, []).append(ref)

        for admin_user_id in config.get_admin_user_ids():
            admin_refs = refs_by_admin.get(admin_user_id) or []
            if not admin_refs:
                if send_to_config(
                    config,
                    text=text,
                    event_type=event_type,
                    order=order,
                    chat_id=admin_user_id,
                ):
                    sent_or_edited += 1
                continue

            for ref in admin_refs:
                try:
                    edit_admin_order_message(client, ref, text, reply_markup=empty_inline_keyboard())
                except BotDeliveryError as exc:
                    log_event(
                        config,
                        event_type=event_type,
                        status=BotEventLog.Status.FAILED,
                        order=order,
                        message=f"Could not edit admin order message: {exc}",
                        raw_payload={
                            "admin_user_id": ref.admin_user_id,
                            "chat_id": ref.chat_id,
                            "message_id": ref.message_id,
                            "message_kind": ref.message_kind,
                        },
                    )
                    continue
                sent_or_edited += 1
                log_event(
                    config,
                    event_type=event_type,
                    status=BotEventLog.Status.SENT,
                    order=order,
                    message="Admin order message updated.",
                    raw_payload={
                        "admin_user_id": ref.admin_user_id,
                        "chat_id": ref.chat_id,
                        "message_id": ref.message_id,
                        "message_kind": ref.message_kind,
                    },
                )
    return sent_or_edited


def send_new_order_to_config(config, order, *, title="سفارش جدید VPN"):
    text = format_order_message(order, title=title)
    reply_markup = order_admin_keyboard(order)
    client = BotClient(config)
    sent = 0

    for admin_user_id in config.get_admin_user_ids():
        if order.payment_receipt_image:
            try:
                order.payment_receipt_image.open("rb")
                try:
                    payload = client.send_photo(
                        order.payment_receipt_image.file,
                        caption=fit_photo_caption(text),
                        reply_markup=reply_markup,
                        chat_id=admin_user_id,
                    )
                finally:
                    order.payment_receipt_image.close()
            except Exception as exc:
                config.last_error = str(exc)
                config.save(update_fields=["last_error", "updated_at"])
                log_event(
                    config,
                    event_type=BotEventLog.EventType.NEW_ORDER,
                    status=BotEventLog.Status.FAILED,
                    order=order,
                    message=f"Receipt photo notification failed: {exc}",
                    raw_payload={"admin_user_id": admin_user_id},
                )
                logger.warning("Bot receipt notification failed for %s order=%s admin=%s: %s", config, order.pk, admin_user_id, exc)
                if send_to_config(
                    config,
                    text=text,
                    event_type=BotEventLog.EventType.NEW_ORDER,
                    order=order,
                    reply_markup=reply_markup,
                    chat_id=admin_user_id,
                ):
                    sent += 1
                continue

            message_id = extract_sent_message_id(payload)
            remember_admin_order_message(
                config,
                order,
                admin_user_id=admin_user_id,
                chat_id=admin_user_id,
                message_id=message_id,
                message_kind=BotAdminOrderMessage.MessageKind.PHOTO,
                metadata={"receipt_image": order.payment_receipt_image.name},
            )
            sent += 1
            log_event(
                config,
                event_type=BotEventLog.EventType.NEW_ORDER,
                status=BotEventLog.Status.SENT,
                order=order,
                message="New order notification sent with receipt photo.",
                raw_payload={
                    "receipt_image": order.payment_receipt_image.name,
                    "admin_user_id": admin_user_id,
                    "message_id": message_id,
                },
            )
            if len(text) > 1000:
                try:
                    client.send_message(text, chat_id=admin_user_id)
                except BotDeliveryError:
                    pass
            continue

        try:
            payload = client.send_message(text, reply_markup=reply_markup, chat_id=admin_user_id)
        except BotDeliveryError as exc:
            config.last_error = str(exc)
            config.save(update_fields=["last_error", "updated_at"])
            log_event(
                config,
                event_type=BotEventLog.EventType.NEW_ORDER,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=str(exc),
                raw_payload={"admin_user_id": admin_user_id},
            )
            continue

        message_id = extract_sent_message_id(payload)
        remember_admin_order_message(
            config,
            order,
            admin_user_id=admin_user_id,
            chat_id=admin_user_id,
            message_id=message_id,
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )
        sent += 1
        log_event(
            config,
            event_type=BotEventLog.EventType.NEW_ORDER,
            status=BotEventLog.Status.SENT,
            order=order,
            message=text,
            raw_payload={"admin_user_id": admin_user_id, "message_id": message_id},
        )

    if sent and config.last_error:
        config.last_error = ""
        config.save(update_fields=["last_error", "updated_at"])
    return sent > 0


def notify_new_order(order):
    from .admin_notifications import notify_admins_new_order

    return notify_admins_new_order(order.pk)


def notify_duplicate_order_attempt(order):
    for config in active_bot_configs(order):
        if not config.notify_new_orders:
            continue
        send_new_order_to_config(config, order, title="هشدار سفارش تکراری")


def notify_order_event(order, *, event_type):
    title_by_event = {
        "approved": "Order approved",
        "rejected": "Order rejected",
    }
    log_type_by_event = {
        "approved": BotEventLog.EventType.ORDER_APPROVED,
        "rejected": BotEventLog.EventType.ORDER_REJECTED,
    }
    if not (order.metadata or {}).get("suppress_admin_order_updates"):
        sync_admin_order_messages(
            order,
            title=title_by_event.get(event_type, "Order updated"),
            event_type=log_type_by_event.get(event_type, BotEventLog.EventType.WEBHOOK),
        )
    notify_customer_order_event(order, event_type=event_type)


def format_customer_order_event(order, *, event_type):
    if event_type == "approved":
        clients = list(order.get_vpn_clients())
        lines = [
            "اشتراک شما فعال شد",
            "━━━━━━━━━━━━━━",
            "",
            f"کد پیگیری: {order.order_tracking_code}",
            f"پلن: {order.plan.name if order.plan_id else '-'}",
            f"تعداد کانفیگ: {persian_digits(order.quantity or 1)}",
        ]
        if order.operator_id:
            lines.append(f"اپراتور: {order.operator.name}")
        if clients:
            lines.extend(["", "کانفیگ‌ها"])
            for index, vpn_client in enumerate(clients, start=1):
                title = f"کانفیگ {persian_digits(index)}" if len(clients) > 1 else "کانفیگ"
                lines.extend(["", title, f"نام: {bot_client_label(vpn_client)}"])
                if vpn_client.sub_link:
                    lines.extend(["لینک اشتراک", vpn_client.sub_link])
                if vpn_client.direct_link:
                    lines.extend(["لینک مستقیم", vpn_client.direct_link])
        else:
            if order.sub_link:
                lines.extend(["", "لینک اشتراک", order.sub_link])
            if order.direct_link:
                lines.extend(["", "لینک مستقیم", order.direct_link])
        return "\n".join(lines)

    if event_type == "rejected":
        lines = [
            "پرداخت شما تایید نشد",
            "━━━━━━━━━━━━━━",
            "",
            f"کد پیگیری: {order.order_tracking_code}",
        ]
        if order.rejection_reason:
            lines.append(f"دلیل: {order.rejection_reason}")
        lines.append("برای پیگیری با پشتیبانی در ارتباط باشید.")
        return "\n".join(lines)

    return format_order_message(order, title="Order updated")


def notify_customer_order_event(order, *, event_type):
    if not order.customer_id or (order.metadata or {}).get("suppress_customer_notification"):
        return 0

    base_bot_users = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=order.customer,
            is_active=True,
            bot_config__is_active=True,
        )
        .exclude(chat_id="")
    )
    bot_users = base_bot_users
    order_bot = (order.metadata or {}).get("bot") or {}
    bot_user_id = order_bot.get("bot_user_id")
    bot_config_id = order_bot.get("bot_config_id")
    if bot_user_id:
        targeted_bot_user = base_bot_users.filter(pk=bot_user_id).first()
        if targeted_bot_user:
            bot_users = [targeted_bot_user]
        elif bot_config_id:
            bot_users = base_bot_users.filter(bot_config_id=bot_config_id)
    elif bot_config_id:
        bot_users = base_bot_users.filter(bot_config_id=bot_config_id)
    elif order.store_id:
        bot_users = base_bot_users.filter(Q(bot_config__store=order.store) | Q(bot_config__store__isnull=True))

    sent = 0
    text = format_customer_order_event(order, event_type=event_type)
    for bot_user in bot_users:
        try:
            BotClient(bot_user.bot_config).send_message(text, chat_id=bot_user.chat_id)
        except BotDeliveryError as exc:
            log_event(
                bot_user.bot_config,
                event_type=BotEventLog.EventType.ERROR,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Could not notify customer {bot_user.pk}: {exc}",
            )
            continue
        sent += 1
    return sent


def support_admin_keyboard(conversation):
    return {
        "inline_keyboard": [
            [
                {"text": "پاسخ", "callback_data": f"support:reply:{conversation.pk}"},
                {"text": "بستن", "callback_data": f"support:close:{conversation.pk}"},
            ]
        ]
    }


def format_support_message(conversation, message=None, *, title="پیام پشتیبانی"):
    created_at = timezone.localtime(conversation.created_at).strftime("%Y-%m-%d %H:%M") if conversation.created_at else "-"
    updated_at = timezone.localtime(conversation.updated_at).strftime("%Y-%m-%d %H:%M") if conversation.updated_at else "-"
    customer = conversation.customer
    customer_label = escape(str(customer)) if customer else "-"
    contact_value = escape(conversation.contact_value or "-")
    lines = [
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━",
        f"<b>شناسه گفتگو:</b> <code>{conversation.pk}</code>",
        f"<b>وضعیت:</b> {conversation.get_status_display()}",
        f"<b>فروشگاه:</b> {escape(conversation.store.name) if conversation.store_id else '-'}",
        f"<b>مشتری:</b> {customer_label}",
        f"<b>شماره یا تلگرام:</b> <code>{contact_value}</code>",
        f"<b>شروع:</b> <code>{created_at}</code>",
        f"<b>آخرین بروزرسانی:</b> <code>{updated_at}</code>",
    ]
    if customer:
        if customer.phone_number:
            lines.append(f"<b>شماره حساب:</b> <code>{escape(customer.phone_number)}</code>")
        if customer.username:
            lines.append(f"<b>نام کاربری حساب:</b> <code>{escape(customer.username)}</code>")
    if message:
        body = (message.body or "").strip()
        excerpt = body[:1200] + ("..." if len(body) > 1200 else "")
        lines.extend(
            [
                "",
                f"<b>متن پیام:</b>\n{escape(excerpt)}",
            ]
        )
    return "\n".join(lines)


def notify_support_message(conversation, message):
    sent = 0
    for config in active_bot_configs(store=conversation.store):
        if send_to_config(
            config,
            text=format_support_message(conversation, message, title="پیام جدید پشتیبانی"),
            event_type=BotEventLog.EventType.SUPPORT_MESSAGE,
            reply_markup=support_admin_keyboard(conversation),
        ):
            sent += 1
    return sent


def first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_id(value):
    if isinstance(value, dict):
        value = first_present(value, "id", "user_id", "chat_id")
    return str(value or "")


def get_callback_update(update):
    return first_present(update, "callback_query", "callbackQuery", "callback")


def get_message_update(update):
    return first_present(update, "message", "edited_message", "editedMessage")


def get_callback_id(callback_query):
    return first_present(callback_query, "id", "callback_id", "callbackQueryId", "callback_query_id")


def get_callback_data(callback_query):
    return first_present(callback_query, "data", "callback_data", "callbackData", "payload", "value") or ""


def get_sender_object(payload):
    return first_present(payload, "from", "from_user", "fromUser", "user", "sender", "author") or {}


def get_chat_object(payload):
    return first_present(payload, "chat", "peer") or {}


def get_message_id(message):
    return first_present(message, "message_id", "messageId", "id")


def get_message_text(message):
    return (first_present(message, "text", "caption") or "").strip()


def sanitize_bot_text_for_logging(value):
    if not isinstance(value, str):
        return value
    sanitized = CONFIG_LOOKUP_LINK_LOG_RE.sub("<config-link-redacted>", value)
    return CONFIG_LOOKUP_UUID_LOG_RE.sub(
        lambda match: f"<identifier:{mask_identifier(match.group(0))}>",
        sanitized,
    )


def sanitize_bot_update_for_logging(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in {"text", "caption"}:
                sanitized[key] = sanitize_bot_text_for_logging(item)
            else:
                sanitized[key] = sanitize_bot_update_for_logging(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_bot_update_for_logging(item) for item in value]
    return value


def sanitize_bot_event_log_value(value):
    if isinstance(value, str):
        return sanitize_bot_text_for_logging(value)
    if isinstance(value, dict):
        return {key: sanitize_bot_event_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_bot_event_log_value(item) for item in value]
    return value


def extract_user_id(update):
    callback_query = get_callback_update(update)
    if callback_query:
        return normalize_id(get_sender_object(callback_query))
    message = get_message_update(update)
    if message:
        return normalize_id(get_sender_object(message)) or normalize_id(get_chat_object(message))
    return ""


def extract_chat_id(update):
    callback_query = get_callback_update(update)
    if callback_query:
        message = first_present(callback_query, "message") or {}
        return (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or extract_user_id(update)
        )
    message = get_message_update(update)
    if message:
        return (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or extract_user_id(update)
        )
    return ""


def extract_callback_message_reference(callback_query, *, fallback_chat_id=""):
    message = first_present(callback_query, "message") or {}
    return {
        "chat_id": (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or fallback_chat_id
        ),
        "message_id": get_message_id(message),
    }


def delete_callback_message(client, callback_query, *, fallback_chat_id=""):
    message_ref = extract_callback_message_reference(callback_query or {}, fallback_chat_id=fallback_chat_id)
    try:
        return client.delete_message(
            chat_id=message_ref["chat_id"],
            message_id=message_ref["message_id"],
        )
    except BotDeliveryError as exc:
        logger.info(
            "Could not delete callback message provider=%s chat_id=%s message_id=%s: %s",
            client.config.provider,
            message_ref["chat_id"],
            message_ref["message_id"],
            exc,
        )
    return None


def is_admin_callback_data(data):
    action, tracking_code = parse_callback_data(data)
    return bool(action and tracking_code)


def parse_support_callback_data(data):
    data = (data or "").strip()
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "support" and parts[2].isdigit():
        action = parts[1]
        if action == "replay":
            action = "reply"
        if action in {"reply", "close"}:
            return action, int(parts[2])
    return "", None


def is_support_callback_data(data):
    action, conversation_id = parse_support_callback_data(data)
    return bool(action and conversation_id)


def sender_display_name(sender):
    first_name = first_present(sender, "first_name", "firstName") or ""
    last_name = first_present(sender, "last_name", "lastName") or ""
    username = first_present(sender, "username") or ""
    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if not display_name and username:
        display_name = f"@{username}"
    return display_name


def normalize_bot_username(username):
    return str(username or "").strip().lstrip("@")[:80]


def customer_username_is_available(customer, username):
    username = normalize_bot_username(username)
    if not username:
        return False
    qs = Customer.objects.filter(username=username)
    if customer and customer.pk:
        qs = qs.exclude(pk=customer.pk)
    return not qs.exists()


def update_customer_identity_from_bot(customer, *, display_name="", username=""):
    if not customer:
        return None

    changed_fields = []
    display_name = str(display_name or "").strip()[:120]
    username = normalize_bot_username(username)
    if display_name and (
        not customer.display_name
        or customer.display_name.startswith("Customer ")
        or customer.display_name == customer.phone_number
    ):
        customer.display_name = display_name
        changed_fields.append("display_name")
    if username and not customer.username and customer_username_is_available(customer, username):
        customer.username = username
        changed_fields.append("username")
    if changed_fields:
        customer.save(update_fields=[*changed_fields, "updated_at"])
    return customer


def get_or_create_bot_customer(*, display_name="", username=""):
    username = normalize_bot_username(username)
    if username:
        customer = Customer.objects.filter(username=username).first()
        if customer:
            return update_customer_identity_from_bot(customer, display_name=display_name, username=username)

    defaults = {"display_name": (display_name or (f"@{username}" if username else "") or "کاربر ربات")[:120]}
    if username:
        defaults["username"] = username
    return Customer.objects.create(**defaults)


def update_bot_user_from_update(config, update, *, chat_id, user_id):
    callback_query = get_callback_update(update)
    payload = callback_query or get_message_update(update) or {}
    sender = get_sender_object(payload)
    username = (first_present(sender, "username") or "").strip()
    first_name = (first_present(sender, "first_name", "firstName") or "").strip()
    last_name = (first_present(sender, "last_name", "lastName") or "").strip()
    display_name = sender_display_name(sender) or f"{config.get_provider_display()} user {user_id}"
    now = timezone.now()

    bot_user = BotUser.objects.filter(bot_config=config, provider_user_id=str(user_id)).select_related("customer").first()
    if bot_user:
        bot_user.chat_id = str(chat_id or bot_user.chat_id or user_id)
        bot_user.username = username
        bot_user.first_name = first_name
        bot_user.last_name = last_name
        bot_user.display_name = display_name
        bot_user.last_seen_at = now
        bot_user.is_active = True
        if not bot_user.customer_id:
            bot_user.customer = get_or_create_bot_customer(display_name=display_name, username=username)
        else:
            update_customer_identity_from_bot(bot_user.customer, display_name=display_name, username=username)
        bot_user.save(
            update_fields=[
                "chat_id",
                "username",
                "first_name",
                "last_name",
                "display_name",
                "last_seen_at",
                "is_active",
                "customer",
                "updated_at",
            ]
        )
        return bot_user

    customer = get_or_create_bot_customer(display_name=display_name, username=username)
    return BotUser.objects.create(
        bot_config=config,
        customer=customer,
        provider_user_id=str(user_id),
        chat_id=str(chat_id or user_id),
        username=username,
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        last_seen_at=now,
    )


def main_menu_keyboard(*, is_admin=False):
    rows = [
        [
            {"text": "خرید سرویس 🛒", "callback_data": "user:buy"},
            {"text": "سرویس‌های من 🔐", "callback_data": "user:subs"},
        ],
        [
            {"text": "🎁 دریافت تست رایگان", "callback_data": "user:free_trial"},
        ],
        [
            {"text": "تمدید سرویس 🔄", "callback_data": "user:renew"},
            {"text": "سفارش‌های من 📦", "callback_data": "user:orders"},
        ],
        [
            {"text": "مشاهده باقی‌مانده کانفیگ 📊", "callback_data": "user:config_lookup"},
        ],
        [
            {"text": "دعوت دوستان 🎁", "callback_data": "user:referrals"},
            {"text": "پشتیبانی 💬", "callback_data": "user:support"},
        ],
        [
            {"text": "راهنما ❓", "callback_data": "user:help"},
            {"text": "پروفایل من", "callback_data": "user:profile"},
        ],
    ]
    if is_admin:
        rows.extend(
            [
                [
                    {"text": "سفارش‌های pending", "callback_data": "admin:orders:pending"},
                    {"text": "گزارش فروش", "callback_data": "admin:sales_report"},
                ],
                [
                    {"text": "گزارش مشتریان 📊", "callback_data": "admin:ca:menu"},
                    {"text": "ارسال پیام 📣", "callback_data": "admin:bc:menu"},
                ],
                [{"text": "تنظیمات سریع", "callback_data": "admin:quick_settings"}],
            ]
        )
    return {"inline_keyboard": rows}


CUSTOMER_ANALYTICS_CALLBACK_PREFIX = "admin:ca:"
BROADCAST_CALLBACK_PREFIX = "admin:bc:"
BOT_STATE_BUY_WAIT_DISCOUNT = "buy_wait_discount"
BOT_STATE_SUPPORT_WAIT_MESSAGE = "support_wait_message"
BOT_SUPPORT_CATEGORIES = {
    "payment": "مشکل پرداخت",
    "connection": "مشکل اتصال",
    "renewal": "تمدید",
    "other": "سایر",
}


CUSTOMER_ANALYTICS_REPORTS = {
    "top_today": {
        "title": "۱۰ خریدار برتر امروز",
        "period": PERIOD_TODAY,
        "type": "top_buyers",
    },
    "top_7d": {
        "title": "۱۰ خریدار برتر ۷ روز اخیر",
        "period": PERIOD_LAST_7_DAYS,
        "type": "top_buyers",
    },
    "top_30d": {
        "title": "۱۰ خریدار برتر ۳۰ روز اخیر",
        "period": PERIOD_LAST_30_DAYS,
        "type": "top_buyers",
    },
    "loyal": {
        "title": "مشتریان ثابت",
        "type": "loyal",
    },
    "good": {
        "title": "مشتریان خوب",
        "type": "good",
    },
    "top_referrers": {
        "title": "معرف‌های برتر",
        "type": "top_referrers",
    },
    "no_order": {
        "title": "کاربران بدون خرید",
        "type": "segment",
        "segment": SEGMENT_NO_ORDER,
    },
    "inactive": {
        "title": "کاربران غیرفعال",
        "type": "segment",
        "segment": SEGMENT_INACTIVE,
    },
}


def is_customer_analytics_callback_data(data):
    return str(data or "").startswith(CUSTOMER_ANALYTICS_CALLBACK_PREFIX)


def customer_analytics_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "برترین‌های امروز", "callback_data": "admin:ca:top_today"}],
            [{"text": "برترین‌های ۷ روز اخیر", "callback_data": "admin:ca:top_7d"}],
            [{"text": "برترین‌های ۳۰ روز اخیر", "callback_data": "admin:ca:top_30d"}],
            [
                {"text": "مشتریان ثابت", "callback_data": "admin:ca:loyal"},
                {"text": "مشتریان خوب", "callback_data": "admin:ca:good"},
            ],
            [{"text": "معرف‌های برتر", "callback_data": "admin:ca:top_referrers"}],
            [
                {"text": "کاربران بدون خرید", "callback_data": "admin:ca:no_order"},
                {"text": "کاربران غیرفعال", "callback_data": "admin:ca:inactive"},
            ],
            [{"text": "منوی اصلی", "callback_data": "user:menu"}],
        ]
    }


def customer_analytics_menu_text():
    return "گزارش مشتریان 📊\nیکی از گزارش‌ها را انتخاب کنید."


def customer_analytics_customer_name(customer):
    return escape(customer.display_name or customer.username or customer.phone_number or f"Customer {str(customer.public_id)[:8]}")


def customer_analytics_contact(customer):
    value = customer.phone_number or customer.username or ""
    return f" - <code>{escape(value)}</code>" if value else ""


def customer_analytics_empty_message(title):
    return f"<b>{escape(title)}</b>\n\nداده‌ای برای این گزارش پیدا نشد."


def format_customer_analytics_report(report_key, *, config=None):
    report = CUSTOMER_ANALYTICS_REPORTS.get(report_key)
    if not report:
        return "این گزارش پیدا نشد."
    if not analytics_enabled(config.store if config else None):
        return "گزارش مشتریان در تنظیمات فروشگاه غیرفعال است."

    limit = top_customers_limit(config.store if config else None)
    title = report["title"]
    date_from = date_to = None
    if report.get("period"):
        date_from, date_to = get_period_range(report["period"])

    report_type = report["type"]
    if report_type == "top_buyers":
        customers = list(get_top_customers(metric="amount", date_from=date_from, date_to=date_to, limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} خرید - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_orders_count", 0) or 0),
                    bot_money(getattr(customer, "analytics_total_paid_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "loyal":
        customers = list(get_loyal_customers(limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} خرید موفق - {} تمدید".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_orders_count", 0) or 0),
                    persian_digits(getattr(customer, "analytics_renewal_orders_count", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "good":
        customers = list(get_customers_by_segment(SEGMENT_GOOD, limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    bot_money(getattr(customer, "analytics_total_paid_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "top_referrers":
        customers = list(get_top_referrers(limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} زیرمجموعه موفق - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_referrals_count", 0) or 0),
                    bot_money(getattr(customer, "analytics_successful_referral_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    customers = list(get_customers_by_segment(report["segment"], limit=limit))
    if not customers:
        return customer_analytics_empty_message(title)
    lines = [f"<b>{escape(title)}</b>"]
    for index, customer in enumerate(customers, start=1):
        if report["segment"] == SEGMENT_INACTIVE:
            last_purchase = bot_datetime(getattr(customer, "analytics_last_purchase_at", None))
            suffix = f" - آخرین خرید: {last_purchase}"
        else:
            suffix = f" - عضویت: {bot_datetime(customer.created_at)}"
        lines.append(f"{persian_digits(index)}. {customer_analytics_customer_name(customer)}{customer_analytics_contact(customer)}{suffix}")
    return "\n".join(lines)


BROADCAST_AUDIENCE_LABELS = {
    BroadcastMessage.AudienceType.ALL: "همه کاربران",
    BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS: "مشتریان دارای خرید موفق",
    BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG: "دارای کانفیگ فعال",
    BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER: "بدون سفارش",
    BroadcastMessage.AudienceType.LOYAL: "مشتریان وفادار",
    BroadcastMessage.AudienceType.GOOD: "مشتریان خوب",
    BroadcastMessage.AudienceType.TOP_BUYER: "خریداران برتر",
    BroadcastMessage.AudienceType.TOP_REFERRER: "معرف‌های برتر",
    BroadcastMessage.AudienceType.INACTIVE: "مشتریان غیرفعال",
    BroadcastMessage.AudienceType.NO_ORDER: "بدون خرید موفق",
}

BROADCAST_CHANNEL_LABELS = {
    BroadcastMessage.Channel.TELEGRAM: "تلگرام",
    BroadcastMessage.Channel.BALE: "بله",
    BroadcastMessage.Channel.ALL_AVAILABLE: "همه کانال‌های موجود",
}


def is_broadcast_callback_data(data):
    return str(data or "").startswith(BROADCAST_CALLBACK_PREFIX)


def broadcast_audience_keyboard():
    audience_rows = [
        (BroadcastMessage.AudienceType.ALL, BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS),
        (BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG, BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER),
        (BroadcastMessage.AudienceType.LOYAL, BroadcastMessage.AudienceType.GOOD),
        (BroadcastMessage.AudienceType.TOP_BUYER, BroadcastMessage.AudienceType.TOP_REFERRER),
        (BroadcastMessage.AudienceType.INACTIVE, BroadcastMessage.AudienceType.NO_ORDER),
    ]
    rows = []
    for left, right in audience_rows:
        rows.append(
            [
                {"text": BROADCAST_AUDIENCE_LABELS[left], "callback_data": f"admin:bc:aud:{left}"},
                {"text": BROADCAST_AUDIENCE_LABELS[right], "callback_data": f"admin:bc:aud:{right}"},
            ]
        )
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def broadcast_confirm_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "تایید و ارسال", "callback_data": "admin:bc:send"},
                {"text": "لغو", "callback_data": "admin:bc:cancel"},
            ],
            [{"text": "انتخاب مخاطب", "callback_data": "admin:bc:menu"}],
        ]
    }


def broadcast_menu_text(config):
    channel = BROADCAST_CHANNEL_LABELS.get(config.provider, config.get_provider_display())
    return (
        "ارسال پیام 📣\n"
        "گروه مخاطبان را انتخاب کنید.\n\n"
        f"کانال ارسال این ربات: {channel}"
    )


def broadcast_campaign_preview(config, *, audience_type, message_text, channel=None):
    from .broadcast_services import resolve_campaign_recipients

    campaign = BroadcastMessage(
        store=config.store,
        title="Broadcast preview",
        message_text=message_text,
        audience_type=audience_type,
        channel=channel or config.provider,
    )
    rows = resolve_campaign_recipients(campaign, bot_config=config)
    total = len(rows)
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    return {
        "total": total,
        "pending": total - skipped,
        "skipped": skipped,
    }


def format_broadcast_preview(*, audience_type, channel, message_text, counts):
    excerpt = message_text[:1200] + ("..." if len(message_text) > 1200 else "")
    audience_label = BROADCAST_AUDIENCE_LABELS.get(audience_type, audience_type)
    channel_label = BROADCAST_CHANNEL_LABELS.get(channel, channel)
    return "\n".join(
        [
            "پیش‌نمایش ارسال پیام 📣",
            "━━━━━━━━━━━━━━",
            f"مخاطب: {audience_label}",
            f"کانال: {channel_label}",
            "",
            f"{persian_digits(counts['total'])} مخاطب پیدا شد.",
            f"قابل ارسال: {persian_digits(counts['pending'])}",
            f"Skipped: {persian_digits(counts['skipped'])}",
            "",
            "متن پیام:",
            escape(excerpt),
        ]
    )


def format_broadcast_result(campaign, counts):
    skipped = counts.get("skipped")
    if skipped is None:
        skipped = campaign.recipients.filter(status="skipped").count()
    return "\n".join(
        [
            "گزارش ارسال پیام 📣",
            "━━━━━━━━━━━━━━",
            f"عنوان: {escape(campaign.title)}",
            f"مخاطب کل: {persian_digits(counts.get('total', campaign.total_recipients))}",
            f"موفق: {persian_digits(counts.get('success', campaign.success_count))}",
            f"ناموفق: {persian_digits(counts.get('failed', campaign.failed_count))}",
            f"Skipped: {persian_digits(skipped)}",
        ]
    )


def start_broadcast_flow(client, config, bot_user, *, chat_id):
    store = config.store or get_current_store()
    if store and not getattr(store, "broadcast_enabled", True):
        client.send_message("ارسال پیام در تنظیمات فروشگاه غیرفعال است.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": False, "broadcast_disabled": True}
    bot_user.reset_state()
    client.send_message(broadcast_menu_text(config), chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
    return {"ok": True, "broadcast_menu": True}


def select_broadcast_audience(client, config, bot_user, audience_type, *, chat_id):
    if audience_type not in BROADCAST_AUDIENCE_LABELS:
        client.send_message("این گروه مخاطب پیدا نشد.", chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
        return {"ok": True, "success": False}
    channel = config.provider
    bot_user.state = BotUser.State.BROADCAST_WAIT_TEXT
    bot_user.state_data = {"audience_type": audience_type, "channel": channel}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        (
            f"مخاطب انتخاب شد: {BROADCAST_AUDIENCE_LABELS[audience_type]}\n"
            "متن پیام را ارسال کنید."
        ),
        chat_id=chat_id,
        reply_markup=cancel_keyboard(),
    )
    return {"ok": True, "waiting_for_broadcast_text": True}


def handle_broadcast_text(config, bot_user, message_text, *, chat_id):
    client = BotClient(config)
    if not is_admin_bot_user(config, bot_user):
        return {"ok": True, "ignored": True}
    message_text = (message_text or "").strip()
    if not message_text:
        client.send_message("متن خالی پذیرفته نمی‌شود. لطفا متن پیام را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False}

    data = bot_user.state_data or {}
    audience_type = data.get("audience_type") or BroadcastMessage.AudienceType.ALL
    channel = data.get("channel") or config.provider
    counts = broadcast_campaign_preview(
        config,
        audience_type=audience_type,
        message_text=message_text,
        channel=channel,
    )
    bot_user.state = BotUser.State.BROADCAST_CONFIRM
    bot_user.state_data = {
        "audience_type": audience_type,
        "channel": channel,
        "message_text": message_text,
        "preview": counts,
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        format_broadcast_preview(
            audience_type=audience_type,
            channel=channel,
            message_text=message_text,
            counts=counts,
        ),
        chat_id=chat_id,
        reply_markup=broadcast_confirm_keyboard(),
    )
    return {"ok": True, "broadcast_preview": True, **counts}


def send_confirmed_broadcast(client, config, bot_user, *, chat_id):
    from .broadcast_services import send_campaign

    if bot_user.state != BotUser.State.BROADCAST_CONFIRM:
        client.send_message("کمپین آماده ارسال پیدا نشد.", chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
        return {"ok": True, "success": False}
    data = bot_user.state_data or {}
    message_text = (data.get("message_text") or "").strip()
    if not message_text:
        bot_user.reset_state()
        client.send_message("متن پیام خالی بود و ارسال انجام نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": False}

    now_label = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
    campaign = BroadcastMessage.objects.create(
        store=config.store,
        title=f"Bot broadcast {now_label}",
        message_text=message_text,
        audience_type=data.get("audience_type") or BroadcastMessage.AudienceType.ALL,
        channel=data.get("channel") or config.provider,
        status=BroadcastMessage.Status.QUEUED,
        metadata={
            "source": "admin_bot",
            "provider": config.provider,
            "bot_config_id": config.pk,
            "admin_user_id": bot_user.provider_user_id,
            "preview": data.get("preview") or {},
        },
    )
    counts = send_campaign(campaign, bot_config=config)
    bot_user.reset_state()
    client.send_message(format_broadcast_result(campaign, counts), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
    return {"ok": True, "success": True, "campaign_id": campaign.pk, **counts}


def handle_broadcast_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    user_id = normalize_id(get_sender_object(callback_query))
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")
    delete_callback_message(client, callback_query, fallback_chat_id=chat_id)

    if not (config.is_admin_user(user_id) or config.is_admin_user(chat_id)):
        client.send_message("شما اجازه ارسال پیام انبوه را ندارید.", chat_id=chat_id)
        return {"ok": True, "success": False, "permission_denied": True}

    bot_user = update_bot_user_from_update(
        config,
        {"callback_query": callback_query},
        chat_id=chat_id,
        user_id=user_id or chat_id,
    )
    if data == "admin:bc:menu":
        return start_broadcast_flow(client, config, bot_user, chat_id=chat_id)
    if data.startswith("admin:bc:aud:"):
        audience_type = data.rsplit(":", 1)[-1]
        return select_broadcast_audience(client, config, bot_user, audience_type, chat_id=chat_id)
    if data == "admin:bc:cancel":
        bot_user.reset_state()
        client.send_message("ارسال پیام لغو شد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "cancelled": True}
    if data == "admin:bc:send":
        return send_confirmed_broadcast(client, config, bot_user, chat_id=chat_id)
    return {"ok": True, "ignored": True}


ADMIN_MENU_CALLBACKS = {"admin:orders:pending", "admin:sales_report", "admin:quick_settings"}


def is_admin_menu_callback_data(data):
    return str(data or "") in ADMIN_MENU_CALLBACKS


def admin_pending_orders_keyboard(orders):
    rows = []
    for order in orders:
        tracking = order.order_tracking_code
        label = f"{order.plan.name if order.plan_id else '-'} | {tracking}"[:60]
        rows.append([{"text": label, "callback_data": f"order:approve:{tracking}"}])
        rows.append(
            [
                {"text": "تایید", "callback_data": f"approve:{tracking}"},
                {"text": "رد", "callback_data": f"reject:{tracking}"},
            ]
        )
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def admin_pending_orders_text(config):
    orders = Order.objects.select_related("plan", "operator", "customer").filter(
        status=Order.Status.PENDING_VERIFICATION,
        verification_status=Order.VerificationStatus.PENDING,
    )
    if config.store_id:
        orders = orders.filter(store=config.store)
    orders = list(orders.order_by("created_at")[:10])
    if not orders:
        return "سفارش pending برای بررسی وجود ندارد.", admin_pending_orders_keyboard([])

    lines = ["سفارش‌های pending", "━━━━━━━━━━━━━━"]
    for order in orders:
        customer_label = order.customer.display_name if order.customer_id else order.sender_card_name or "-"
        lines.extend(
            [
                f"شماره سفارش: {order.order_tracking_code}",
                f"پلن: {order.plan.name if order.plan_id else '-'}",
                *([f"اپراتور: {order.operator.name}"] if order.operator_id else []),
                f"مشتری: {customer_label}",
                f"مبلغ: {bot_money(order.amount, order.currency)}",
                f"ثبت: {bot_datetime(order.created_at)}",
                "",
            ]
        )
    return "\n".join(lines).strip(), admin_pending_orders_keyboard(orders)


def admin_sales_report_text(config):
    now = timezone.now()
    start = now - timedelta(days=1)
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=now)
    if config.store_id:
        orders = orders.filter(store=config.store)
    completed = orders.filter(status=Order.Status.COMPLETED)
    pending = orders.filter(status=Order.Status.PENDING_VERIFICATION)
    rejected = orders.filter(status=Order.Status.REJECTED)
    revenue = completed.aggregate(total=Sum("amount"))["total"] or 0
    lines = [
        "گزارش فروش",
        "━━━━━━━━━━━━━━",
        f"بازه: ۲۴ ساعت اخیر تا {bot_datetime(now)}",
        f"سفارش فعال شده: {persian_digits(completed.count())}",
        f"در انتظار بررسی: {persian_digits(pending.count())}",
        f"رد شده: {persian_digits(rejected.count())}",
        f"فروش تاییدشده: {bot_money(revenue)}",
    ]
    return "\n".join(lines)


def admin_quick_settings_text(config):
    store = config.store or get_current_store()
    force_join = "فعال" if config.force_telegram_channel_join else "غیرفعال"
    analytics = "فعال" if analytics_enabled(store) else "غیرفعال"
    broadcast = "فعال" if getattr(store, "broadcast_enabled", True) else "غیرفعال"
    return (
        "تنظیمات سریع\n"
        "━━━━━━━━━━━━━━\n"
        f"Force Join: {force_join}\n"
        f"گزارش مشتریان: {analytics}\n"
        f"ارسال پیام: {broadcast}\n"
        f"فروشگاه: {store.name if store else '-'}"
    )


def handle_admin_menu_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    admin_user_id = normalize_id(get_sender_object(callback_query))
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")
    if not (config.is_admin_user(admin_user_id) or config.is_admin_user(chat_id)):
        client.send_message("شما اجازه دسترسی به منوی ادمین را ندارید.", chat_id=chat_id)
        return {"ok": True, "success": False, "permission_denied": True}

    if data == "admin:orders:pending":
        text, reply_markup = admin_pending_orders_text(config)
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return {"ok": True, "handled": True}
    if data == "admin:sales_report":
        client.send_message(admin_sales_report_text(config), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "handled": True}
    if data == "admin:quick_settings":
        client.send_message(admin_quick_settings_text(config), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "handled": True}
    return {"ok": True, "ignored": True}


def referral_code_from_start_text(text):
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0].lower() != "/start":
        return ""
    payload = parts[1].strip()
    for prefix in ("ref_", "ref-", "ref:"):
        if payload.lower().startswith(prefix):
            payload = payload[len(prefix):]
            break
    return payload.strip()


def referral_keyboard(bot_user, config=None):
    rows = []
    active_configs = list(get_active_referral_configs(bot_user.customer)) if bot_user.customer_id else []
    if bot_user.customer_id:
        callback_data = "user:referral_redeem"
        if len(active_configs) > 1:
            callback_data = "user:referral_choose"
        rows.append([{"text": "دریافت جایزه 🎁", "callback_data": callback_data}])
        rows.append([{"text": "متن آماده دعوت 📨", "callback_data": "user:referral_invite_text"}])
        try:
            summary = get_referral_summary(
                bot_user.customer,
                store=(config.store if config else None) or get_current_store(),
                bot_config=config,
            )
        except Exception:
            summary = {}
        if summary.get("telegram_share_url"):
            rows.append([{"text": "اشتراک‌گذاری لینک", "url": summary["telegram_share_url"]}])
    rows.append([{"text": "راهنمای دعوت ❓", "callback_data": "user:referral_help"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def free_trial_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "دریافت تست ✅", "callback_data": "user:free_trial_confirm"}],
            [{"text": "لغو", "callback_data": "user:free_trial_cancel"}],
        ]
    }


def referral_config_keyboard(bot_user):
    rows = []
    active_configs = list(get_active_referral_configs(bot_user.customer)) if bot_user.customer_id else []
    for index, vpn_config in enumerate(active_configs, start=1):
        label = f"{persian_digits(index)}. {bot_client_label(vpn_config)}"
        rows.append([{"text": label[:60], "callback_data": f"user:referral_redeem:{vpn_config.public_id}"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:referrals"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_referral_panel(bot_user, config):
    customer = bot_user.customer
    if not customer:
        return "برای استفاده از دعوت دوستان، ابتدا از منوی اصلی پروفایل خود را بسازید."

    summary = get_referral_summary(customer, store=config.store or get_current_store(), bot_config=config)
    telegram_link = summary["telegram_link"] or summary["telegram_link_missing_message"]
    lines = [
        "🎁 دعوت دوستان",
        "━━━━━━━━━━━━━━",
        "کد دعوت شما:",
        summary["code"],
        "",
        "لینک اختصاصی شما:",
        telegram_link,
        "",
        "آمار:",
        f"• دوستان دعوت‌شده: {persian_digits(summary['invited_count'])}",
        f"• خریدهای موفق: {persian_digits(summary['successful_referrals_count'])}",
        f"• بسته‌های آماده دریافت: {persian_digits(summary['available_reward_count'])}",
        f"• حجم آماده دریافت: {bot_volume_label(summary['available_gb'])}",
        f"• مدت آماده دریافت: {persian_digits(summary['available_duration_days'])} روز",
        f"• حجم دریافت‌شده: {bot_volume_label(summary['redeemed_gb'])}",
    ]
    if summary["telegram_link"]:
        lines.extend(["", "متن آماده دعوت:", summary["invite_text"]])
    if not summary["enabled"]:
        lines.extend(["", summary["disabled_message"]])
    return "\n".join(lines)


def referral_help_text():
    return (
        "راهنمای دعوت دوستان 🎁\n"
        "━━━━━━━━━━━━━━\n"
        "کد یا لینک دعوت خود را برای دوستتان بفرستید. اگر دوست شما با همان لینک وارد شود و اولین خرید موفقش تایید شود، "
        "یک بسته هدیه ۲ گیگ و ۳۰ روزه برای شما آماده می‌شود. بسته‌ها با هم جمع می‌شوند و هنگام دریافت روی کانفیگ انتخابی اعمال می‌شوند."
    )


def help_text():
    return (
        "راهنما ❓\n"
        "━━━━━━━━━━━━━━\n"
        "خرید: از «خرید سرویس» پلن را انتخاب کنید، خلاصه را ببینید و پرداخت را تایید کنید.\n"
        "رسید: بعد از کارت‌به‌کارت، عکس واضح رسید را در همان گفتگو بفرستید.\n"
        "فعال‌سازی: بعد از بررسی پرداخت، لینک کانفیگ در ربات ارسال می‌شود.\n"
        "دریافت لینک: از «سرویس‌های من» روی دریافت لینک یا مدیریت سرویس بزنید.\n"
        "تمدید: از «تمدید سرویس» کانفیگ را انتخاب کنید و رسید تمدید را بفرستید.\n"
        "تست رایگان: از «دریافت تست رایگان» می‌توانید در بازه مجاز یک کانفیگ کوتاه‌مدت بگیرید.\n"
        "دعوت دوستان: لینک دعوت خود را بفرستید؛ بعد از خرید موفق زیرمجموعه، هدیه حجمی آماده دریافت می‌شود."
    )


def support_category_keyboard():
    rows = [
        [{"text": label, "callback_data": f"user:support_cat:{key}"}]
        for key, label in BOT_SUPPORT_CATEGORIES.items()
    ]
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def support_wait_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "انتخاب دسته", "callback_data": "user:support"}],
            [{"text": "لغو", "callback_data": "user:cancel"}],
        ]
    }


def start_support_flow(client, config, bot_user, *, chat_id):
    bot_user.reset_state()
    client.send_message(
        "موضوع پشتیبانی را انتخاب کنید.",
        chat_id=chat_id,
        reply_markup=support_category_keyboard(),
    )
    return {"ok": True, "handled": True}


def select_support_category(client, bot_user, category, *, chat_id):
    if category not in BOT_SUPPORT_CATEGORIES:
        client.send_message("این دسته پشتیبانی پیدا نشد.", chat_id=chat_id, reply_markup=support_category_keyboard())
        return {"ok": True, "success": False}
    bot_user.state = BOT_STATE_SUPPORT_WAIT_MESSAGE
    bot_user.state_data = {
        "support_category": category,
        "support_subject": BOT_SUPPORT_CATEGORIES[category],
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        f"{BOT_SUPPORT_CATEGORIES[category]}\nپیام خود را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=support_wait_keyboard(),
    )
    return {"ok": True, "handled": True}


def bot_support_contact_value(bot_user):
    if bot_user.username:
        return f"@{bot_user.username.lstrip('@')}"
    if bot_user.customer_id and bot_user.customer.phone_number:
        return bot_user.customer.phone_number
    return bot_user.provider_user_id or bot_user.chat_id


def create_support_ticket_from_bot(config, bot_user, text, message, *, chat_id):
    client = BotClient(config)
    body = (text or "").strip()
    if not body:
        client.send_message("پیام پشتیبانی خالی است. لطفا متن مشکل را ارسال کنید.", chat_id=chat_id, reply_markup=support_wait_keyboard())
        return {"ok": True, "success": False}

    data = bot_user.state_data or {}
    subject = data.get("support_subject") or BOT_SUPPORT_CATEGORIES["other"]
    store = config.store or get_current_store()
    conversation = SupportConversation.objects.create(
        store=store,
        customer=bot_user.customer if bot_user.customer_id else None,
        subject=subject,
        contact_value=bot_support_contact_value(bot_user),
        status=SupportConversation.Status.WAITING_ADMIN,
        last_customer_message_at=timezone.now(),
    )
    support_message = SupportMessage.objects.create(
        conversation=conversation,
        sender_type=SupportMessage.SenderType.CUSTOMER,
        customer=bot_user.customer if bot_user.customer_id else None,
        bot_config=config,
        body=body,
        metadata={
            "source": "bot_support",
            "provider": config.provider,
            "chat_id": chat_id,
            "message_id": get_message_id(message),
            "category": data.get("support_category") or "other",
        },
    )
    notify_support_message(conversation, support_message)
    bot_user.reset_state()
    client.send_message(
        (
            "پیام پشتیبانی ثبت شد.\n"
            "━━━━━━━━━━━━━━\n"
            f"شماره تیکت: {persian_digits(conversation.pk)}\n"
            f"وضعیت: {conversation.get_status_display()}"
        ),
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
    )
    return {"ok": True, "success": True, "support_conversation": conversation.pk}


def redeem_referral_reward_for_bot(client, bot_user, *, chat_id, public_id=""):
    if not bot_user.customer_id:
        client.send_message("حساب کاربری پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    active_configs = list(get_active_referral_configs(bot_user.customer))
    if not active_configs:
        client.send_message(
            "برای دریافت هدیه، ابتدا باید یک کانفیگ فعال داشته باشید.",
            chat_id=chat_id,
            reply_markup=referral_keyboard(bot_user, client.config),
        )
        return {"ok": True, "success": False}
    if public_id:
        vpn_config = next((item for item in active_configs if str(item.public_id) == str(public_id)), None)
        if not vpn_config:
            client.send_message("این کانفیگ فعال نیست یا پیدا نشد.", chat_id=chat_id, reply_markup=referral_keyboard(bot_user, client.config))
            return {"ok": True, "success": False}
    elif len(active_configs) == 1:
        vpn_config = active_configs[0]
    else:
        client.send_message(
            "کدام کانفیگ را برای دریافت هدیه انتخاب می‌کنید؟",
            chat_id=chat_id,
            reply_markup=referral_config_keyboard(bot_user),
        )
        return {"ok": True, "select_config": True}

    result = redeem_referral_rewards(bot_user.customer, vpn_config)
    client.send_message(
        result.message,
        chat_id=chat_id,
        reply_markup=referral_keyboard(bot_user, client.config),
    )
    return {"ok": True, "success": result.success}


def start_free_trial_flow(client, config, bot_user, *, chat_id):
    try:
        settings = validate_free_trial_settings(bot_config=config)
    except ValidationError:
        client.send_message(
            "در حال حاضر تست رایگان فعال نیست.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False}

    next_available_at = get_next_free_trial_available_at(
        bot_user.customer if bot_user.customer_id else None,
        telegram_user_id=bot_user.provider_user_id,
        store=settings["store"],
    )
    if next_available_at:
        client.send_message(
            f"شما قبلاً تست رایگان دریافت کرده‌اید. امکان دریافت بعدی از تاریخ {format_jalali_datetime(next_available_at)} وجود دارد.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "next_available_at": next_available_at}

    client.send_message(
        format_free_trial_preview(settings),
        chat_id=chat_id,
        reply_markup=free_trial_keyboard(),
    )
    return {"ok": True, "handled": True}


def confirm_free_trial_flow(client, config, bot_user, *, chat_id):
    result = create_free_trial_for_customer(
        bot_user.customer if bot_user.customer_id else None,
        telegram_user_id=bot_user.provider_user_id,
        bot_config=config,
    )
    bot_user.reset_state()
    client.send_message(
        format_free_trial_result(result),
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
    )
    return {
        "ok": True,
        "success": result.success,
        "free_trial_request": result.request.pk if result.request else None,
        "vpn_client": result.vpn_client.pk if result.vpn_client else None,
    }


def cancel_keyboard():
    return {"inline_keyboard": [[{"text": "انصراف", "callback_data": "user:cancel"}]]}


def config_lookup_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "لغو", "callback_data": "user:cancel"}],
            [{"text": "بازگشت به منو", "callback_data": "user:menu"}],
        ]
    }


def purchase_summary_keyboard(*, has_discount=False):
    discount_label = "تغییر کد تخفیف" if has_discount else "وارد کردن کد تخفیف"
    rows = [
        [{"text": "تایید خرید", "callback_data": "user:buy_confirm"}],
        [
            {"text": discount_label, "callback_data": "user:discount:start"},
            {"text": "رد شدن از کد تخفیف", "callback_data": "user:discount:skip"},
        ],
        [
            {"text": "برگشت", "callback_data": "user:buy_back_plans"},
            {"text": "لغو", "callback_data": "user:cancel"},
        ],
    ]
    return {"inline_keyboard": rows}


def payment_step_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "برگشت به خلاصه سفارش", "callback_data": "user:buy_back_summary"}],
            [{"text": "لغو", "callback_data": "user:cancel"}],
        ]
    }


def discount_code_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "رد شدن از کد تخفیف", "callback_data": "user:discount:skip"}],
            [{"text": "برگشت", "callback_data": "user:buy_back_summary"}],
            [{"text": "لغو", "callback_data": "user:cancel"}],
        ]
    }


def remove_reply_keyboard():
    return {"remove_keyboard": True}


def phone_request_keyboard():
    return {
        "keyboard": [
            [{"text": "ارسال شماره موبایل", "request_contact": True}],
            [{"text": "بعدا ثبت می‌کنم"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def quantity_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": persian_digits(1), "callback_data": "user:buyqty:1"},
                {"text": persian_digits(2), "callback_data": "user:buyqty:2"},
                {"text": persian_digits(3), "callback_data": "user:buyqty:3"},
            ],
            [
                {"text": persian_digits(5), "callback_data": "user:buyqty:5"},
                {"text": persian_digits(10), "callback_data": "user:buyqty:10"},
            ],
            [
                {"text": "برگشت", "callback_data": "user:buy_back_plans"},
                {"text": "لغو", "callback_data": "user:cancel"},
            ],
        ]
    }


def operator_keyboard(operators):
    rows = []
    for operator in operators:
        rows.append([{"text": operator.name, "callback_data": f"user:buyop:{operator.pk}"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def normalize_bot_number(value):
    return str(value or "").strip().translate(BOT_DIGIT_TRANSLATION)


def normalize_bot_phone_number(value):
    compact = normalize_bot_number(value)
    compact = re.sub(r"[\s\-\(\)\.]+", "", compact)
    if compact.startswith("0098"):
        compact = f"0{compact[4:]}"
    elif compact.startswith("+98") and len(compact) == 13:
        compact = f"0{compact[3:]}"
    elif compact.startswith("98") and len(compact) == 12:
        compact = f"0{compact[2:]}"
    elif compact.startswith("9") and len(compact) == 10:
        compact = f"0{compact}"
    return compact[:30]


def is_valid_bot_phone_number(phone_number):
    return bool(
        re.fullmatch(r"09\d{9}", phone_number or "")
        or re.fullmatch(r"\+\d{8,15}", phone_number or "")
        or re.fullmatch(r"\d{8,15}", phone_number or "")
    )


def extract_contact_phone(message):
    contact = first_present(message, "contact") or {}
    if not isinstance(contact, dict):
        return ""
    return first_present(contact, "phone_number", "phoneNumber", "phone") or ""


def save_bot_user_phone(bot_user, raw_phone):
    phone_number = normalize_bot_phone_number(raw_phone)
    if not is_valid_bot_phone_number(phone_number):
        return False, "شماره موبایل معتبر نیست. لطفا شماره را مثل 09123456789 ارسال کنید."

    if not bot_user.customer_id:
        bot_user.customer = get_or_create_bot_customer(display_name=bot_user.display_name, username=bot_user.username)
        bot_user.save(update_fields=["customer", "updated_at"])

    customer = bot_user.customer
    existing_customer = Customer.objects.filter(phone_number=phone_number).exclude(pk=customer.pk).first()
    if existing_customer:
        if not customer.orders.exists():
            bot_user.customer = update_customer_identity_from_bot(
                existing_customer,
                display_name=bot_user.display_name,
                username=bot_user.username,
            )
            bot_user.save(update_fields=["customer", "updated_at"])
            return True, "این شماره قبلا ثبت شده بود؛ پروفایل ربات به همان حساب وصل شد."
        return False, "این شماره قبلا برای یک پروفایل دیگر ثبت شده است. اگر شماره برای شماست، از پشتیبانی کمک بگیرید."

    changed_fields = []
    if customer.phone_number != phone_number:
        customer.phone_number = phone_number
        changed_fields.append("phone_number")
    if bot_user.display_name and (
        not customer.display_name
        or customer.display_name.startswith("Customer ")
        or customer.display_name == customer.phone_number
    ):
        customer.display_name = bot_user.display_name[:120]
        changed_fields.append("display_name")
    if bot_user.username and not customer.username and customer_username_is_available(customer, bot_user.username):
        customer.username = normalize_bot_username(bot_user.username)
        changed_fields.append("username")
    if changed_fields:
        customer.save(update_fields=[*changed_fields, "updated_at"])
    return True, "شماره موبایل در پروفایل شما ذخیره شد."


def custom_volume_prompt_text(store):
    price_per_gb = store_custom_volume_price_per_gb(store)
    currency = get_custom_volume_currency(store)
    return (
        "حجم دلخواه ۳۰ روزه\n"
        "━━━━━━━━━━━━━━\n"
        f"قیمت هر گیگ: {bot_money(price_per_gb, currency)}\n"
        f"حجم را به گیگابایت، بین {persian_digits(CUSTOM_VOLUME_MIN_GB)} تا {persian_digits(CUSTOM_VOLUME_MAX_GB)} ارسال کنید."
    )


def plan_keyboard(plans, *, prefix="user:buyplan", custom_volume=False, operator=None):
    rows = []
    operator_suffix = f":op:{operator.pk}" if operator else ""
    for plan in plans:
        rows.append(
            [
                {
                    "text": f"{plan.name} - {bot_money(plan.price, plan.currency)}",
                    "callback_data": f"{prefix}:{plan.pk}{operator_suffix}",
                }
            ]
        )
    if custom_volume:
        custom_callback = f"user:buycustom:{operator.pk}" if operator else "user:buycustom"
        rows.append([{"text": "حجم دلخواه ۳۰ روزه", "callback_data": custom_callback}])
    back_callback = "user:buy_back_ops" if operator else "user:menu"
    rows.append([{"text": "بازگشت", "callback_data": back_callback}])
    return {"inline_keyboard": rows}


def format_operator_lines(operators):
    lines = ["اپراتور اینترنتت را انتخاب کن", "━━━━━━━━━━━━━━", ""]
    for operator in operators:
        lines.append(operator.name)
    return "\n".join(lines).strip()


def format_plan_lines(plans, *, store=None, custom_volume=False, operator=None):
    lines = [f"پلن‌های فعال {operator.name}" if operator else "پلن‌های فعال", "━━━━━━━━━━━━━━", ""]
    for plan in plans:
        description = (plan.description or "").strip()
        if description:
            description = description.splitlines()[0][:120]
        lines.extend([
            f"{plan.name}\n"
            f"حجم: {bot_volume_label(plan.volume_gb)} | مدت: {persian_digits(plan.duration_days)} روز\n"
            f"مبلغ: {bot_money(plan.price, plan.currency)}"
            + (f"\n{description}" if description else ""),
            "",
        ])
    if custom_volume and store:
        price_per_gb = store_custom_volume_price_per_gb(store)
        currency = get_custom_volume_currency(store)
        lines.extend(
            [
                "حجم دلخواه",
                f"مدت: {persian_digits(CUSTOM_VOLUME_DURATION_DAYS)} روز",
                f"قیمت هر گیگ: {bot_money(price_per_gb, currency)}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def bot_discount_error_message(message):
    text = str(message or "")
    if "invalid" in text.lower():
        return "کد تخفیف معتبر نیست."
    if "not active" in text.lower():
        return "کد تخفیف فعال نیست."
    if "not valid right now" in text.lower():
        return "کد تخفیف در حال حاضر معتبر نیست."
    if "usage limit" in text.lower():
        return "ظرفیت استفاده از این کد تخفیف تمام شده است."
    if "not available for this account" in text.lower():
        return "این کد هدیه برای حساب شما قابل استفاده نیست."
    return text or "کد تخفیف قابل اعمال نیست."


def calculate_bot_pricing(plan, quantity=1, *, customer=None, discount_code=""):
    quantity = validate_order_quantity(quantity)
    original_amount = plan.price * quantity
    discount_code = DiscountCode.normalize_code(discount_code)
    if discount_code:
        discount, discount_amount, payable_amount = preview_discount(
            plan,
            discount_code,
            customer=customer,
            quantity=quantity,
        )
        return {
            "original_amount": original_amount,
            "discount_amount": discount_amount,
            "payable_amount": payable_amount,
            "discount_code": discount.code if discount else discount_code,
            "discount_label": f"کد تخفیف {discount.code if discount else discount_code}",
            "discount_source": Order.DiscountSource.MANUAL,
        }

    wholesale_percent = get_customer_wholesale_discount_percent(customer)
    discount_amount = calculate_percentage_discount(original_amount, wholesale_percent)
    return {
        "original_amount": original_amount,
        "discount_amount": discount_amount,
        "payable_amount": max(original_amount - discount_amount, 0),
        "discount_code": "",
        "discount_label": f"تخفیف همکاری {persian_digits(wholesale_percent)}٪" if discount_amount else "",
        "discount_source": Order.DiscountSource.WHOLESALE if discount_amount else Order.DiscountSource.NONE,
    }


def pricing_from_state(plan, quantity=1, *, customer=None, data=None):
    data = data or {}
    discount_code = data.get("discount_code") or ""
    if discount_code and data.get("discount_amount") not in (None, ""):
        original_amount = plan.price * validate_order_quantity(quantity)
        discount_amount = int(data.get("discount_amount") or 0)
        return {
            "original_amount": original_amount,
            "discount_amount": discount_amount,
            "payable_amount": max(original_amount - discount_amount, 0),
            "discount_code": DiscountCode.normalize_code(discount_code),
            "discount_label": f"کد تخفیف {DiscountCode.normalize_code(discount_code)}",
            "discount_source": Order.DiscountSource.MANUAL,
        }
    return calculate_bot_pricing(plan, quantity, customer=customer, discount_code=discount_code)


def store_payment_lines(store, plan, *, quantity=1, operator=None, pricing=None):
    quantity = validate_order_quantity(quantity)
    pricing = pricing or {"original_amount": plan.price * quantity, "discount_amount": 0, "payable_amount": plan.price * quantity}
    lines = [
        "پرداخت کارت به کارت",
        "━━━━━━━━━━━━━━",
        f"پلن انتخابی: {plan.name}",
    ]
    if operator:
        lines.append(f"اپراتور: {operator.name}")
    if quantity > 1:
        lines.extend(
            [
                f"تعداد کانفیگ: {persian_digits(quantity)}",
                f"قیمت هر عدد: {bot_money(plan.price, plan.currency)}",
            ]
        )
    if pricing.get("discount_amount"):
        lines.extend(
            [
                f"قیمت اصلی: {bot_money(pricing['original_amount'], plan.currency)}",
                f"تخفیف: {bot_money(pricing['discount_amount'], plan.currency)}",
                f"مبلغ نهایی: {bot_money(pricing['payable_amount'], plan.currency)}",
            ]
        )
    else:
        lines.append(f"مبلغ: {bot_money(pricing['payable_amount'], plan.currency)}")
    lines.extend(
        [
            "",
            "اطلاعات پرداخت",
            f"شماره کارت: {store.card_number}",
            f"به نام: {store.card_owner}",
        ]
    )
    if store.bank_name:
        lines.append(f"بانک: {store.bank_name}")
    if store.sheba_number:
        lines.append(f"شبا: {store.sheba_number}")
    return "\n".join(lines)


def format_purchase_summary(store, plan, *, quantity=1, operator=None, pricing=None, flow="purchase", vpn_client=None):
    quantity = validate_order_quantity(quantity)
    pricing = pricing or pricing_from_state(plan, quantity)
    title = "خلاصه تمدید" if flow == "renewal" else "خلاصه سفارش"
    lines = [
        title,
        "━━━━━━━━━━━━━━",
    ]
    if vpn_client:
        lines.append(f"سرویس: {bot_client_label(vpn_client)}")
    lines.extend(
        [
            f"پلن: {plan.name}",
            f"حجم: {bot_volume_label(plan.volume_gb)}",
            f"مدت: {persian_digits(plan.duration_days)} روز",
        ]
    )
    if operator:
        lines.append(f"اپراتور: {operator.name}")
    if quantity > 1:
        lines.append(f"تعداد کانفیگ: {persian_digits(quantity)}")
    lines.append(f"قیمت اصلی: {bot_money(pricing['original_amount'], plan.currency)}")
    if pricing.get("discount_amount"):
        lines.append(f"تخفیف: {bot_money(pricing['discount_amount'], plan.currency)}")
        if pricing.get("discount_label"):
            lines.append(f"نوع تخفیف: {pricing['discount_label']}")
    lines.append(f"مبلغ نهایی: {bot_money(pricing['payable_amount'], plan.currency)}")
    lines.extend(["", "برای ادامه، خرید را تایید کنید یا کد تخفیف وارد کنید."])
    return "\n".join(lines)


def format_payment_prompt(store, plan, *, quantity=1, operator=None, pricing=None, flow="purchase", vpn_client=None):
    intro = "پرداخت تمدید" if flow == "renewal" else "پرداخت سفارش"
    service_line = f"سرویس: {bot_client_label(vpn_client)}\n\n" if vpn_client else ""
    return (
        f"{intro}\n"
        "━━━━━━━━━━━━━━\n"
        f"{service_line}"
        f"{store_payment_lines(store, plan, quantity=quantity, operator=operator, pricing=pricing)}\n\n"
        "بعد از پرداخت، نام پرداخت‌کننده را ارسال کنید؛ سپس عکس رسید را می‌فرستید."
    )


def user_order_summary(order):
    is_renewal = bool((order.metadata or {}).get("renewal"))
    lines = [
        "درخواست تمدید شما ثبت شد" if is_renewal else "سفارش شما ثبت شد",
        "━━━━━━━━━━━━━━",
        "",
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name}",
        f"تعداد کانفیگ: {persian_digits(order.quantity or 1)}",
        f"مبلغ: {bot_money(order.amount, order.currency)}",
    ]
    if order.operator_id:
        lines.append(f"اپراتور: {order.operator.name}")
    lines.append(
        (
            "پرداخت شما برای تایید ارسال شد. پس از تایید، همین کانفیگ تمدید می‌شود."
            if is_renewal
            else "پرداخت شما برای تایید ارسال شد. پس از تایید، کانفیگ همین‌جا ارسال می‌شود."
        )
    )
    return "\n".join(lines)


def bot_client_label(client):
    return client.xui_email or client.username


def bot_subscription_clients(bot_user):
    if not bot_user.customer_id:
        return VPNClient.objects.none()
    return (
        VPNClient.objects.select_related("plan", "order", "inbound", "inbound__panel")
        .filter(
            order__customer=bot_user.customer,
            status__in=[
                VPNClient.Status.ACTIVE,
                VPNClient.Status.INACTIVE,
                VPNClient.Status.CREATED,
                VPNClient.Status.EXPIRED,
            ],
        )
        .order_by("-created_at")
    )


def bot_user_has_available_referral_reward(bot_user):
    if not bot_user.customer_id:
        return False
    try:
        return get_available_referral_gb(bot_user.customer) > 0
    except Exception:
        logger.exception("Could not load referral reward total for bot_user=%s", bot_user.pk)
        return False


def subscription_management_keyboard(bot_user, *, renew_mode=False):
    rows = []
    clients = list(bot_subscription_clients(bot_user)[:10])
    has_referral_reward = bot_user_has_available_referral_reward(bot_user)
    for index, client in enumerate(clients, start=1):
        suffix = persian_digits(index) if len(clients) > 1 else ""
        label = bot_client_label(client)
        if renew_mode:
            rows.append(
                [
                    {
                        "text": f"تمدید {suffix} {label}".strip()[:60],
                        "callback_data": f"user:client_renew:{client.public_id}",
                    }
                ]
            )
        else:
            rows.append(
                [
                    {
                        "text": f"دریافت لینک {suffix}".strip(),
                        "callback_data": f"user:client_config:{client.public_id}",
                    },
                    {
                        "text": f"تمدید {suffix}".strip(),
                        "callback_data": f"user:client_renew:{client.public_id}",
                    },
                ]
            )
            if has_referral_reward and client.status == VPNClient.Status.ACTIVE:
                rows.append(
                    [
                        {
                            "text": f"دریافت هدیه دعوت {suffix}".strip(),
                            "callback_data": f"user:referral_redeem:{client.public_id}",
                        }
                    ]
                )
    rows.append([{"text": "خرید اشتراک", "callback_data": "user:buy"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def profile_keyboard(bot_user):
    phone_label = "ویرایش شماره موبایل" if bot_user.customer_id and bot_user.customer.phone_number else "ثبت شماره موبایل"
    return {
        "inline_keyboard": [
            [{"text": phone_label, "callback_data": "user:profile_phone"}],
            [
                {"text": "اشتراک‌های من", "callback_data": "user:subs"},
                {"text": "سفارش‌های من", "callback_data": "user:orders"},
            ],
            [{"text": "منوی اصلی", "callback_data": "user:menu"}],
        ]
    }


def format_profile(bot_user):
    customer = bot_user.customer
    display_name = (customer.display_name if customer else "") or bot_user.display_name or "کاربر"
    username = (customer.username if customer else "") or bot_user.username
    username_label = f"@{username.lstrip('@')}" if username else "ثبت نشده"
    phone_label = customer.phone_number if customer and customer.phone_number else "ثبت نشده"
    if customer:
        orders_count = customer.orders.exclude(metadata__customer_hidden=True).count()
        clients = list(bot_subscription_clients(bot_user))
    else:
        orders_count = 0
        clients = []
    active_count = sum(1 for item in clients if item.status == VPNClient.Status.ACTIVE)
    remaining_bytes = sum(item.remaining_traffic_bytes for item in clients)

    lines = [
        "پروفایل شما",
        "━━━━━━━━━━━━━━",
        f"نام: {display_name}",
        f"نام کاربری: {username_label}",
        f"شماره موبایل: {phone_label}",
        "",
        f"سفارش‌ها: {persian_digits(orders_count)}",
        f"اشتراک فعال: {persian_digits(active_count)}",
        f"حجم باقی‌مانده ثبت‌شده: {bot_gb_from_bytes(remaining_bytes)} گیگابایت",
    ]
    if phone_label == "ثبت نشده":
        lines.extend(["", "برای پیگیری سریع‌تر سفارش‌ها می‌توانید شماره موبایل خود را ثبت کنید."])
    return "\n".join(lines)


def send_profile(client, bot_user, *, chat_id):
    if bot_user.customer_id:
        bot_user.customer.refresh_from_db()
    client.send_message(format_profile(bot_user), chat_id=chat_id, reply_markup=profile_keyboard(bot_user))
    return {"ok": True, "handled": True}


def start_profile_phone_flow(client, bot_user, *, chat_id):
    bot_user.state = BotUser.State.PROFILE_WAIT_PHONE
    bot_user.state_data = {}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "برای ذخیره شماره، دکمه ارسال شماره موبایل را بزنید یا شماره را به صورت متن ارسال کنید.",
        chat_id=chat_id,
        reply_markup=phone_request_keyboard(),
    )
    return {"ok": True, "handled": True}


def start_config_lookup_flow(client, bot_user, *, chat_id):
    bot_user.state = BOT_STATE_CONFIG_LOOKUP_WAIT_LINK
    bot_user.state_data = {}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "لینک کانفیگ خود را ارسال کنید تا باقی‌مانده حجم و زمان آن بررسی شود.",
        chat_id=chat_id,
        reply_markup=config_lookup_keyboard(),
    )
    return {"ok": True, "handled": True}


def config_lookup_rate_key(config, bot_user):
    user_key = bot_user.provider_user_id or bot_user.chat_id or "unknown"
    return f"bot:config-lookup-rate:{config.pk}:{user_key}"


def config_lookup_rate_limited(config, bot_user):
    key = config_lookup_rate_key(config, bot_user)
    try:
        if cache.add(key, 1, CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS):
            return False
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS)
            return False
        return count > CONFIG_LOOKUP_RATE_LIMIT_COUNT
    except Exception as exc:
        logger.warning("Config lookup cache rate limit failed key=%s error=%s", key, exc)

    now_ts = timezone.now().timestamp()
    attempts = [
        timestamp
        for timestamp in CONFIG_LOOKUP_RATE_FALLBACK.get(key, [])
        if now_ts - timestamp < CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS
    ]
    if len(attempts) >= CONFIG_LOOKUP_RATE_LIMIT_COUNT:
        CONFIG_LOOKUP_RATE_FALLBACK[key] = attempts
        return True
    attempts.append(now_ts)
    CONFIG_LOOKUP_RATE_FALLBACK[key] = attempts
    return False


def config_lookup_update_rate_key(config, bot_user):
    user_key = bot_user.provider_user_id or bot_user.chat_id or "unknown"
    return f"bot:config-lookup-update-rate:{config.pk}:{user_key}"


def config_lookup_update_rate_limited(config, bot_user):
    key = config_lookup_update_rate_key(config, bot_user)
    try:
        if cache.add(key, 1, CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS):
            return False
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS)
            return False
        return count > CONFIG_LOOKUP_UPDATE_RATE_LIMIT_COUNT
    except Exception as exc:
        logger.warning("Config lookup update cache rate limit failed key=%s error=%s", key, exc)

    now_ts = timezone.now().timestamp()
    attempts = [
        timestamp
        for timestamp in CONFIG_LOOKUP_UPDATE_RATE_FALLBACK.get(key, [])
        if now_ts - timestamp < CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS
    ]
    if len(attempts) >= CONFIG_LOOKUP_UPDATE_RATE_LIMIT_COUNT:
        CONFIG_LOOKUP_UPDATE_RATE_FALLBACK[key] = attempts
        return True
    attempts.append(now_ts)
    CONFIG_LOOKUP_UPDATE_RATE_FALLBACK[key] = attempts
    return False


def config_lookup_update_cache_key(config, bot_user, token):
    user_key = bot_user.provider_user_id or bot_user.chat_id or "unknown"
    return f"bot:config-lookup-update:{config.pk}:{user_key}:{token}"


CONFIG_LOOKUP_NO_UPDATE_MESSAGE = "این کانفیگ آپدیت ندارد."


def create_config_lookup_update_token(config, bot_user, result, *, original_config_text=""):
    panel = result.get("panel")
    inbound = result.get("inbound")
    panel_id = result.get("panel_id") or getattr(panel, "pk", None)
    inbound_id = result.get("inbound_id") or getattr(inbound, "inbound_id", None)
    identifier = str(result.get("identifier") or "").strip()
    if not panel_id or not inbound_id or not identifier:
        return ""
    token = secrets.token_urlsafe(12).rstrip("=")
    payload = {
        "panel_id": panel_id,
        "inbound_id": inbound_id,
        "identifier": identifier,
        "email": result.get("email") or "",
        "protocol": result.get("protocol") or "",
        "original_config_fingerprint": config_link_fingerprint(original_config_text),
    }
    try:
        cache.set(
            config_lookup_update_cache_key(config, bot_user, token),
            payload,
            CONFIG_LOOKUP_UPDATE_CACHE_SECONDS,
        )
    except Exception as exc:
        logger.warning("Could not cache config lookup update token: %s", exc)
        return ""
    return token


def config_lookup_result_keyboard(config, bot_user, result, *, original_config_text=""):
    rows = []
    if result.get("found"):
        token = create_config_lookup_update_token(
            config,
            bot_user,
            result,
            original_config_text=original_config_text,
        )
        if token:
            rows.append(
                [
                    {
                        "text": "آپدیت کانفیگ 🔄",
                        "callback_data": f"{CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX}{token}",
                    }
                ]
            )
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def append_config_lookup_panel_errors(text, result, *, is_admin=False):
    panel_errors = result.get("panel_errors") or []
    if not panel_errors:
        return text
    if is_admin:
        lines = ["", "خطای پنل‌ها:"]
        for error in panel_errors[:5]:
            panel_name = escape(str(error.get("panel") or error.get("panel_id") or "-"))
            message = escape(str(error.get("error") or "")[:200])
            lines.append(f"- {panel_name}: {message}")
        return f"{text}\n" + "\n".join(lines)
    return f"{text}\n\nبرخی پنل‌ها موقتاً در دسترس نبودند؛ نتیجه بر اساس پنل‌های قابل بررسی است."


def handle_config_lookup_text(config, bot_user, text, *, chat_id):
    client = BotClient(config)
    if not text:
        client.send_message(
            "لطفا لینک کانفیگ یا شناسه را به صورت متن ارسال کنید.",
            chat_id=chat_id,
            reply_markup=config_lookup_keyboard(),
        )
        return {"ok": True, "handled": True}

    if config_lookup_rate_limited(config, bot_user):
        bot_user.reset_state()
        client.send_message(
            "تعداد درخواست‌های بررسی شما زیاد شده. چند دقیقه دیگر دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "rate_limited": True}

    try:
        result = check_config_usage(text, store=config.store or get_current_store())
    except InvalidConfigLink:
        client.send_message(
            "لینک کانفیگ معتبر نیست. لطفا لینک VLESS، VMess، Trojan یا شناسه خام را ارسال کنید.",
            chat_id=chat_id,
            reply_markup=config_lookup_keyboard(),
        )
        return {"ok": True, "success": False, "invalid": True}
    except ConfigIdentifierMissing:
        client.send_message(
            "شناسه کلاینت از این کانفیگ استخراج نشد. لطفا لینک کامل کانفیگ را ارسال کنید.",
            chat_id=chat_id,
            reply_markup=config_lookup_keyboard(),
        )
        return {"ok": True, "success": False, "identifier_missing": True}
    except Exception as exc:
        logger.exception(
            "Config usage check failed user=%s error=%s",
            bot_user.provider_user_id or bot_user.chat_id,
            exc,
        )
        client.send_message(
            "بررسی کانفیگ به دلیل خطای موقت انجام نشد. چند دقیقه دیگر دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "lookup_failed"}

    message = append_config_lookup_panel_errors(
        result.get("message", ""),
        result,
        is_admin=is_admin_bot_user(config, bot_user),
    )
    bot_user.reset_state()
    client.send_message(
        message,
        chat_id=chat_id,
        reply_markup=(
            config_lookup_result_keyboard(config, bot_user, result, original_config_text=text)
            if result.get("found")
            else main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user))
        ),
    )
    return {"ok": True, "success": bool(result.get("found")), "config_lookup": True}


def handle_config_lookup_update_callback(config, bot_user, data, *, client, chat_id):
    token = data[len(CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX) :].strip()
    if not token:
        client.send_message(
            "درخواست آپدیت کانفیگ معتبر نیست. دوباره لینک کانفیگ را بررسی کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "invalid_update_token"}

    if config_lookup_update_rate_limited(config, bot_user):
        client.send_message(
            "تعداد درخواست‌های آپدیت کانفیگ زیاد شده. چند دقیقه دیگر دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "rate_limited": True}

    payload = cache.get(config_lookup_update_cache_key(config, bot_user, token))
    if not payload:
        client.send_message(
            "مهلت آپدیت این کانفیگ تمام شده. دوباره لینک کانفیگ را ارسال کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "expired": True}

    panel = Panel.objects.filter(pk=payload.get("panel_id"), is_active=True).first()
    if not panel:
        client.send_message(
            "پنل این کانفیگ در حال حاضر در دسترس نیست. چند دقیقه دیگر تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "panel_unavailable": True}

    try:
        details = build_config_link_for_identifier(
            panel,
            payload.get("inbound_id"),
            payload.get("identifier"),
        )
    except Exception as exc:
        logger.warning(
            "Config lookup update failed user=%s panel=%s inbound=%s identifier=%s error=%s",
            bot_user.provider_user_id or bot_user.chat_id,
            payload.get("panel_id"),
            payload.get("inbound_id"),
            mask_identifier(payload.get("identifier")),
            exc,
        )
        client.send_message(
            "ساخت لینک به‌روز از پنل ممکن نشد. کمی بعد دوباره امتحان کنید یا به پشتیبانی پیام بدهید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "update_failed"}

    updated_link = details.get("updated_config_link") or ""
    if not updated_link:
        client.send_message(
            "لینک به‌روز برای این کانفیگ ساخته نشد. لطفا دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "empty_updated_link"}

    if details.get("enabled") is False:
        client.send_message(
            CONFIG_LOOKUP_NO_UPDATE_MESSAGE,
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": True, "no_update": True, "inactive": True}

    original_fingerprint = payload.get("original_config_fingerprint") or ""
    updated_fingerprint = config_link_fingerprint(updated_link)
    if original_fingerprint and updated_fingerprint and original_fingerprint == updated_fingerprint:
        client.send_message(
            CONFIG_LOOKUP_NO_UPDATE_MESSAGE,
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "success": True, "no_update": True}

    label = details.get("remark") or details.get("email") or "کانفیگ"
    client.send_message(
        f"لینک به‌روز {label}:\n\n{updated_link}",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        parse_mode=None,
    )
    return {"ok": True, "success": True, "config_lookup_update": True}


def active_subscription_lines(bot_user, *, title="اشتراک‌های شما", force_refresh=False):
    clients = bot_subscription_clients(bot_user) if bot_user.customer_id else []
    lines = [title, "━━━━━━━━━━━━━━", ""]
    found = False
    for client in clients:
        found = True
        stats = sync_vpn_client_stats(client, force=force_refresh)
        remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", 0))
        used_gb = bot_gb_from_bytes(stats.get("used_traffic_bytes", 0))
        total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
        expires_at = stats.get("expiry_at") or client.expires_at
        expiry_label = bot_datetime(expires_at)
        lines.extend(
            [
                f"{client.plan.name if client.plan_id else bot_client_label(client)}",
                f"وضعیت: {bot_client_status(client)}",
                f"حجم کل: {total_gb} گیگابایت",
                f"حجم مصرف‌شده: {used_gb} گیگابایت",
                f"حجم باقی‌مانده: {remaining_gb} گیگابایت",
                f"تاریخ پایان: {expiry_label}",
            ]
        )
        if client.sub_link:
            lines.append(f"لینک کانفیگ: {client.sub_link}")
        elif client.direct_link:
            lines.append(f"لینک کانفیگ: {client.direct_link}")
        else:
            lines.append("لینک کانفیگ: هنوز آماده نیست")
        lines.append("")
    if not found:
        lines.append("هنوز اشتراک فعالی برای این حساب پیدا نشد.")
    return "\n".join(lines).strip()


def visible_bot_orders(bot_user, *, limit=10):
    if not bot_user.customer_id:
        return []
    orders = (
        Order.objects.select_related("plan", "operator", "store")
        .prefetch_related("vpn_clients")
        .filter(customer=bot_user.customer)
        .order_by("-created_at")
    )
    return [
        order
        for order in orders[: max(limit * 2, limit)]
        if not (order.metadata or {}).get("customer_hidden")
    ][:limit]


def user_orders_keyboard(orders):
    rows = []
    for order in orders:
        plan_name = order.plan.name if order.plan_id else "بدون پلن"
        label = f"{plan_name} | {bot_order_status(order)}"
        rows.append([{"text": label[:60], "callback_data": f"user:order:{order.order_tracking_code}"}])
    rows.append([{"text": "بازگشت به منو", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def order_management_keyboard(order):
    rows = []
    clients = list(order.vpn_clients.all())
    for index, client in enumerate(clients, start=1):
        suffix = persian_digits(index) if len(clients) > 1 else ""
        rows.append(
            [
                {"text": f"حجم {suffix}".strip(), "callback_data": f"user:client_usage:{client.public_id}"},
                {"text": f"بروزرسانی کانفیگ {suffix}".strip(), "callback_data": f"user:client_refresh:{client.public_id}"},
            ]
        )
        rows.append(
            [
                {"text": f"دریافت لینک کانفیگ {suffix}".strip(), "callback_data": f"user:client_config:{client.public_id}"},
                {"text": f"تمدید {suffix}".strip(), "callback_data": f"user:client_renew:{client.public_id}"},
            ]
        )

    if order.status in {Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED}:
        rows.append([{"text": "لغو این سفارش", "callback_data": f"user:order_cancel:{order.order_tracking_code}"}])

    rows.append([{"text": "بازگشت به سفارش‌ها", "callback_data": "user:orders"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_user_orders_list(bot_user):
    orders = visible_bot_orders(bot_user)
    if not orders:
        return "هنوز سفارشی برای این حساب ثبت نشده است.", user_orders_keyboard([])

    lines = [
        "سفارش‌های من",
        "━━━━━━━━━━━━━━",
        "برای دیدن جزئیات یا مدیریت، یکی از سفارش‌ها را انتخاب کنید.",
        "",
    ]
    for order in orders:
        created_at = bot_datetime(order.created_at)
        lines.extend(
            [
                f"شماره سفارش: {order.order_tracking_code}",
                f"پلن: {order.plan.name if order.plan_id else 'بدون پلن'}",
                *([f"اپراتور: {order.operator.name}"] if order.operator_id else []),
                f"وضعیت: {bot_order_status(order)}",
                f"تعداد: {persian_digits(order.quantity or 1)}",
                f"مبلغ: {bot_money(order.amount, order.currency)}",
                f"ثبت: {created_at}",
                *([f"دلیل رد: {order.rejection_reason}"] if order.rejection_reason else []),
                "",
            ]
        )
    return "\n".join(lines).strip(), user_orders_keyboard(orders)


def format_user_order_detail(order):
    created_at = bot_datetime(order.created_at)
    paid_at = (
        f"{persian_digits(order.payment_date.strftime('%Y/%m/%d'))}، {persian_digits(order.payment_time.strftime('%H:%M'))}"
        if order.payment_date and order.payment_time
        else "ثبت نشده"
    )
    lines = [
        "جزئیات سفارش",
        "━━━━━━━━━━━━━━",
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name if order.plan_id else 'بدون پلن'}",
        f"حجم/مدت: {bot_volume_label(order.plan.volume_gb) if order.plan_id else '-'} / {persian_digits(order.plan.duration_days if order.plan_id else '-')} روز",
        f"تعداد: {persian_digits(order.quantity or 1)}",
        f"مبلغ: {bot_money(order.amount, order.currency)}",
        f"وضعیت سفارش: {bot_order_status(order)}",
        f"وضعیت پرداخت: {bot_verification_status(order)}",
        f"زمان ثبت: {created_at}",
        f"زمان پرداخت: {paid_at}",
    ]
    if order.operator_id:
        lines.insert(5, f"اپراتور: {order.operator.name}")
    if order.bank_tracking_code:
        lines.append(f"کد پیگیری بانکی: {order.bank_tracking_code}")
    if order.rejection_reason:
        lines.extend(["", f"دلیل رد شدن: {order.rejection_reason}"])

    clients = list(order.vpn_clients.all())
    if clients:
        lines.extend(["", "کانفیگ‌ها"])
        for index, client in enumerate(clients, start=1):
            stats = sync_vpn_client_stats(client)
            remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", client.remaining_traffic_bytes))
            total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
            expiry_at = stats.get("expiry_at") or client.expires_at
            lines.extend(
                [
                    f"{persian_digits(index)}. {bot_client_label(client)}",
                    f"وضعیت: {bot_client_status(client)}",
                    f"حجم باقی‌مانده: {remaining_gb} از {total_gb} گیگابایت",
                    f"انقضا: {bot_datetime(expiry_at)}",
                ]
            )
    else:
        lines.extend(["", "بعد از تایید پرداخت، کانفیگ همین‌جا نمایش داده می‌شود."])

    return "\n".join(lines)


def get_bot_order(bot_user, tracking_code):
    if not bot_user.customer_id:
        return None
    order = (
        Order.objects.select_related("plan", "operator", "store")
        .prefetch_related("vpn_clients", "vpn_clients__inbound", "vpn_clients__inbound__panel")
        .filter(customer=bot_user.customer, order_tracking_code=tracking_code)
        .first()
    )
    if order and (order.metadata or {}).get("customer_hidden"):
        return None
    return order


def get_bot_client(bot_user, public_id):
    if not bot_user.customer_id:
        return None
    return (
        VPNClient.objects.select_related("plan", "order", "order__plan", "order__store", "inbound", "inbound__panel")
        .filter(order__customer=bot_user.customer, public_id=public_id)
        .first()
    )


PENDING_RENEWAL_STATUSES = {
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
}


def pending_renewal_order(bot_user, vpn_client):
    if not bot_user.customer_id or not vpn_client:
        return None
    return (
        Order.objects.select_related("plan", "operator", "store")
        .filter(
            customer=bot_user.customer,
            metadata__renewal_client_pk=vpn_client.pk,
            status__in=PENDING_RENEWAL_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )


def renewal_payment_prompt(store, vpn_client, *, pricing=None):
    plan = vpn_client.plan
    return format_payment_prompt(
        store,
        plan,
        quantity=1,
        operator=vpn_client.order.operator if vpn_client.order_id else None,
        pricing=pricing,
        flow="renewal",
        vpn_client=vpn_client,
    )


def bot_order_metadata(config, bot_user, *, source, extra=None):
    metadata = {
        "source": source,
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    metadata.update(extra or {})
    return metadata


def start_renewal_flow(client, config, bot_user, public_id, *, chat_id):
    vpn_client = get_bot_client(bot_user, public_id)
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False}
    if not vpn_client.plan_id:
        client.send_message("این کانفیگ پلن قابل تمدید ندارد.", chat_id=chat_id, reply_markup=client_config_keyboard(vpn_client))
        bot_user.reset_state()
        return {"ok": True, "success": False}

    pending_order = pending_renewal_order(bot_user, vpn_client)
    if pending_order:
        client.send_message(
            "برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید وجود دارد.\n\n"
            f"{format_user_order_detail(pending_order)}",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(pending_order),
        )
        bot_user.reset_state()
        return {"ok": True, "handled": True, "pending": True}

    store = vpn_client.store or vpn_client.order.store or vpn_client.plan.store or config.store or get_current_store()
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = {
        "flow": "renewal",
        "step": "summary",
        "renewal_client_public_id": str(vpn_client.public_id),
        "plan_id": vpn_client.plan_id,
        "quantity": 1,
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return send_renewal_summary(client, config, bot_user, chat_id=chat_id)


def client_config_keyboard(client):
    rows = [
        [{"text": "بروزرسانی از ثنایی", "callback_data": f"user:client_refresh:{client.public_id}"}],
        [{"text": "مشاهده حجم", "callback_data": f"user:client_usage:{client.public_id}"}],
        [{"text": "تمدید این کانفیگ", "callback_data": f"user:client_renew:{client.public_id}"}],
    ]
    customer = client.order.customer if client.order_id and client.order.customer_id else None
    if customer and get_available_referral_gb(customer) > 0 and client.status == VPNClient.Status.ACTIVE:
        rows.append([{"text": "دریافت هدیه دعوت", "callback_data": f"user:referral_redeem:{client.public_id}"}])
    if client.order_id:
        rows.append([{"text": "بازگشت به سفارش", "callback_data": f"user:order:{client.order.order_tracking_code}"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_client_config(client, *, stats=None, refreshed=False):
    stats = stats or sync_vpn_client_stats(client)
    remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", client.remaining_traffic_bytes))
    used_gb = bot_gb_from_bytes(stats.get("used_traffic_bytes", client.used_traffic_bytes))
    total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
    lines = [
        "کانفیگ شما" if not refreshed else "کانفیگ بروزرسانی شد",
        "━━━━━━━━━━━━━━",
        f"نام کانفیگ: {bot_client_label(client)}",
        f"وضعیت: {bot_client_status(client)}",
        f"حجم باقی‌مانده: {remaining_gb} گیگابایت",
        f"مصرف‌شده: {used_gb} از {total_gb} گیگابایت",
        f"انقضا: {bot_datetime(stats.get('expiry_at') or client.expires_at)}",
    ]
    if client.sub_link:
        lines.extend(["", "لینک اشتراک", client.sub_link])
    if client.direct_link:
        lines.extend(["", "لینک مستقیم", client.direct_link])
    if not client.sub_link and not client.direct_link:
        lines.extend(["", "لینک کانفیگ هنوز برای این سفارش آماده نیست."])
    if stats.get("panel_available") is False:
        lines.extend(["", "دسترسی به پنل ثنایی موقتاً برقرار نشد؛ آخرین اطلاعات ذخیره‌شده نمایش داده شد."])
    return "\n".join(lines)


def extract_receipt_file(message):
    photos = message.get("photo") or []
    if photos:
        photo = photos[-1]
        return {
            "file_id": first_present(photo, "file_id", "fileId"),
            "file_unique_id": first_present(photo, "file_unique_id", "fileUniqueId"),
            "kind": "photo",
            "file_name": "telegram-receipt.jpg",
            "message_id": get_message_id(message),
        }
    document = message.get("document") or {}
    if document:
        return {
            "file_id": first_present(document, "file_id", "fileId"),
            "file_unique_id": first_present(document, "file_unique_id", "fileUniqueId"),
            "kind": "document",
            "file_name": first_present(document, "file_name", "fileName") or "telegram-receipt",
            "mime_type": first_present(document, "mime_type", "mimeType") or "",
            "message_id": get_message_id(message),
        }
    return {}


def receipt_file_type_error(file_info):
    if not file_info or file_info.get("kind") == "photo":
        return ""
    mime_type = str(file_info.get("mime_type") or "").lower()
    if mime_type and mime_type not in PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES:
        return "فایل رسید باید تصویر JPG، PNG، WEBP یا GIF باشد."
    extension = PurePosixPath(file_info.get("file_name") or "").suffix.lower()
    if extension and extension not in PAYMENT_RECEIPT_ALLOWED_EXTENSIONS:
        return "فایل رسید باید تصویر JPG، PNG، WEBP یا GIF باشد."
    return ""


def safe_receipt_filename(file_info):
    raw_name = PurePosixPath(file_info.get("file_name") or "telegram-receipt").name
    raw_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip(".-") or "telegram-receipt"
    if "." not in raw_name:
        raw_name = f"{raw_name}.jpg"
    return raw_name[:120]


def download_receipt_content(client, file_info, metadata):
    file_id = file_info.get("file_id")
    if not file_id:
        return None
    try:
        file_payload = client.get_file(file_id)
        result = file_payload.get("result") or {}
        file_path = result.get("file_path") or result.get("filePath") or ""
        metadata["receipt"]["file_path"] = file_path
        content = client.download_file(file_path)
    except BotDeliveryError as exc:
        metadata["receipt"]["download_error"] = str(exc)
        return None
    if not content:
        return None
    return ContentFile(content, name=safe_receipt_filename(file_info))


def build_bot_receipt_metadata(config, file_info):
    return {
        "provider": config.provider,
        "kind": file_info.get("kind"),
        "file_id": file_info.get("file_id"),
        "file_unique_id": file_info.get("file_unique_id"),
        "message_id": file_info.get("message_id"),
        "file_name": file_info.get("file_name"),
        "mime_type": file_info.get("mime_type", ""),
    }


def attach_bot_receipt(client, config, file_info, metadata):
    if not file_info:
        return None
    metadata["receipt"] = build_bot_receipt_metadata(config, file_info)
    receipt_image = download_receipt_content(client, file_info, metadata)
    if receipt_image is None:
        metadata["receipt"]["download_error"] = (
            metadata["receipt"].get("download_error")
            or "file_download_unavailable"
        )
    return receipt_image


def send_final_order_status(client, order, *, title, chat_id, callback_query=None, prefix_message=""):
    text = format_order_message(order, title=title)
    if prefix_message:
        text = f"{prefix_message}\n\n{text}"

    message_ref = extract_callback_message_reference(callback_query or {}, fallback_chat_id=chat_id)
    try:
        if message_ref["chat_id"] and message_ref["message_id"]:
            client.edit_message(
                chat_id=message_ref["chat_id"],
                message_id=message_ref["message_id"],
                text=text,
                reply_markup=empty_inline_keyboard(),
            )
            return
    except BotDeliveryError:
        pass

    client.send_message(text, chat_id=chat_id)


def handle_bot_update(provider, webhook_secret, update, *, source="webhook"):
    config = BotConfiguration.objects.filter(
        provider=provider,
        webhook_secret=webhook_secret,
        is_active=True,
    ).first()
    if not config:
        logger.warning("Bot update ignored: config not found provider=%s secret=%s source=%s", provider, webhook_secret, source)
        return {"ok": False, "error": "Bot configuration not found."}

    safe_update = sanitize_bot_update_for_logging(update)
    logger.info("Incoming bot update provider=%s config=%s source=%s payload=%s", provider, config.pk, source, safe_update)
    log_event(
        config,
        event_type=BotEventLog.EventType.WEBHOOK,
        status=BotEventLog.Status.RECEIVED,
        message=f"Incoming {source} update for provider={provider}",
        raw_payload=safe_update,
    )
    maybe_send_due_sales_report(config)

    callback_query = get_callback_update(update)
    message = get_message_update(update)
    user_id = extract_user_id(update)
    chat_id = extract_chat_id(update)
    is_admin = config.is_admin_user(user_id) or config.is_admin_user(chat_id)
    logger.info("Bot update extraction config=%s source=%s user_id=%s chat_id=%s is_admin=%s", config.pk, source, user_id, chat_id, is_admin)

    callback_data = get_callback_data(callback_query) if callback_query else ""
    if callback_query and (callback_data.startswith("user:") or callback_data == CHECK_MEMBERSHIP_CALLBACK):
        return handle_user_update(config, update, chat_id=chat_id, user_id=user_id)

    if is_admin and callback_query and is_support_callback_data(callback_data):
        return handle_support_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and is_customer_analytics_callback_data(callback_data):
        return handle_customer_analytics_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and is_broadcast_callback_data(callback_data):
        return handle_broadcast_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and is_admin_menu_callback_data(callback_data):
        return handle_admin_menu_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and is_admin_callback_data(callback_data):
        return handle_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and message:
        admin_result = handle_message_update(config, message, chat_id=chat_id, admin_user_id=user_id or chat_id)
        if not admin_result.get("ignored"):
            return admin_result

    if not is_admin:
        return handle_user_update(config, update, chat_id=chat_id, user_id=user_id)

    if callback_query and is_support_callback_data(callback_data):
        return handle_support_callback_update(config, callback_query, chat_id=chat_id)
    if callback_query and is_customer_analytics_callback_data(callback_data):
        return handle_customer_analytics_callback_update(config, callback_query, chat_id=chat_id)
    if callback_query and is_broadcast_callback_data(callback_data):
        return handle_broadcast_callback_update(config, callback_query, chat_id=chat_id)
    if callback_query and is_admin_menu_callback_data(callback_data):
        return handle_admin_menu_callback_update(config, callback_query, chat_id=chat_id)
    if callback_query:
        return handle_callback_update(config, callback_query, chat_id=chat_id)
    if message:
        user_result = handle_user_update(config, update, chat_id=chat_id, user_id=user_id)
        if not user_result.get("ignored"):
            return user_result
    return {"ok": True, "ignored": True}


def parse_callback_data(data):
    data = (data or "").strip()
    parts = data.split(":")
    if len(parts) == 2 and parts[0] in {"approve", "reject", "detail"}:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[0] == "order" and parts[1] in {"approve", "reject", "detail"}:
        return parts[1], parts[2]
    return "", ""


def handle_user_update(config, update, *, chat_id, user_id):
    if not user_id or not chat_id:
        return {"ok": True, "ignored": True}

    bot_user = update_bot_user_from_update(config, update, chat_id=chat_id, user_id=user_id)
    callback_query = get_callback_update(update)
    if callback_query:
        return handle_user_callback(config, bot_user, callback_query, chat_id=chat_id)

    message = get_message_update(update)
    if message:
        return handle_user_message(config, bot_user, message, chat_id=chat_id)

    return {"ok": True, "ignored": True}


def is_admin_bot_user(config, bot_user):
    return config.is_admin_user(bot_user.provider_user_id) or config.is_admin_user(bot_user.chat_id)


def telegram_membership_required_response(config, bot_user, client, *, chat_id, force_refresh=False):
    if ensure_telegram_membership(
        config,
        bot_user,
        client=client,
        chat_id=chat_id,
        force_refresh=force_refresh,
    ):
        return None
    return {"ok": True, "membership_required": True}


def send_main_menu(client, bot_user, *, chat_id):
    bot_user.reset_state()
    profile_name = bot_user.customer.display_name if bot_user.customer_id else bot_user.display_name
    client.send_message(
        (
            f"سلام {profile_name or 'دوست عزیز'}.\n"
            "پروفایل شما آماده است؛ از اینجا می‌توانید پلن‌ها را ببینید، خرید یا تمدید ثبت کنید، "
            "سفارش‌ها را مدیریت کنید و حجم باقی‌مانده را بررسی کنید."
        ),
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(client.config, bot_user)),
    )
    return {"ok": True, "handled": True}


def parse_buy_plan_callback(data):
    match = re.fullmatch(r"user:buyplan:(\d+)(?::op:(\d+))?", data or "")
    if not match:
        return None, None
    plan_id, operator_id = match.groups()
    return int(plan_id), int(operator_id) if operator_id else None


def parse_buy_custom_callback(data):
    if data == "user:buycustom":
        return True, None
    match = re.fullmatch(r"user:buycustom:(\d+)", data or "")
    if not match:
        return False, None
    return True, int(match.group(1))


def get_purchase_operator_from_state(store, data):
    if not sales_mode_requires_operator(store):
        return None
    return get_active_operator(store, (data or {}).get("operator_id"))


def send_operator_list(client, store, *, chat_id):
    operators = list(get_active_operators(store))
    if not operators:
        client.send_message("در حال حاضر اپراتور فعالی برای خرید تعریف نشده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}
    client.send_message(
        format_operator_lines(operators),
        chat_id=chat_id,
        reply_markup=operator_keyboard(operators),
    )
    return {"ok": True, "handled": True, "operator_select": True}


def send_plan_list(client, config, *, chat_id, buy_mode=False, operator=None):
    store = config.store or get_current_store()
    if sales_mode_requires_operator(store):
        if operator is None:
            return send_operator_list(client, store, chat_id=chat_id)
        if not get_active_operators(store).filter(pk=operator.pk).exists():
            client.send_message(OPERATOR_INVALID_MESSAGE, chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}

    plans = list(get_store_plans(store, public_only=True, operator=operator))
    custom_volume = custom_volume_is_available(store)
    if operator and not operator_has_available_plans(store, operator):
        client.send_message(OPERATOR_NO_PLANS_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "handled": True}
    if not plans and not custom_volume:
        client.send_message("در حال حاضر پلن فعالی برای خرید وجود ندارد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    client.send_message(
        format_plan_lines(plans, store=store, custom_volume=custom_volume, operator=operator),
        chat_id=chat_id,
        reply_markup=plan_keyboard(plans, custom_volume=custom_volume, operator=operator),
    )
    return {"ok": True, "handled": True, "buy_mode": buy_mode}


def get_selected_purchase_plan(store, plan_id, *, operator=None):
    plan = get_store_plans(store, public_only=True, operator=operator).filter(pk=plan_id).first()
    if plan:
        return plan
    return get_store_plans(store, public_only=False, operator=operator).filter(pk=plan_id, is_custom_volume=True).first()


def purchase_context_from_state(config, bot_user):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        return store, None, None, None, OPERATOR_REQUIRED_MESSAGE
    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        return store, operator, None, None, "پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید."
    try:
        quantity = validate_order_quantity(data.get("quantity", 1))
    except ValidationError:
        quantity = 1
    return store, operator, plan, quantity, ""


def renewal_context_from_state(config, bot_user):
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        return None, None, None, "این کانفیگ پیدا نشد. لطفا دوباره تمدید را شروع کنید."
    if not vpn_client.plan_id:
        return vpn_client, None, None, "این کانفیگ پلن قابل تمدید ندارد."
    store = vpn_client.store or vpn_client.order.store or vpn_client.plan.store or config.store or get_current_store()
    return vpn_client, store, vpn_client.plan, ""


def send_purchase_summary(client, config, bot_user, *, chat_id):
    store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}
    try:
        pricing = pricing_from_state(plan, quantity, customer=bot_user.customer, data=bot_user.state_data)
    except ValidationError as exc:
        pricing = calculate_bot_pricing(plan, quantity, customer=bot_user.customer)
        data = bot_user.state_data or {}
        data.pop("discount_code", None)
        data.pop("discount_amount", None)
        data.pop("payable_amount", None)
        bot_user.state_data = data
        bot_user.save(update_fields=["state_data", "updated_at"])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id)
    data = bot_user.state_data or {}
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        format_purchase_summary(
            store,
            plan,
            quantity=quantity,
            operator=operator,
            pricing=pricing,
            flow=data.get("flow") or "purchase",
        ),
        chat_id=chat_id,
        reply_markup=purchase_summary_keyboard(has_discount=bool(data.get("discount_code"))),
    )
    return {"ok": True, "handled": True}


def send_renewal_summary(client, config, bot_user, *, chat_id):
    vpn_client, store, plan, error = renewal_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}
    try:
        pricing = pricing_from_state(plan, 1, customer=bot_user.customer, data=bot_user.state_data)
    except ValidationError as exc:
        pricing = calculate_bot_pricing(plan, 1, customer=bot_user.customer)
        data = bot_user.state_data or {}
        data.pop("discount_code", None)
        data.pop("discount_amount", None)
        data.pop("payable_amount", None)
        bot_user.state_data = data
        bot_user.save(update_fields=["state_data", "updated_at"])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id)
    data = bot_user.state_data or {}
    data["step"] = "summary"
    data["flow"] = "renewal"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    operator = vpn_client.order.operator if vpn_client.order_id else None
    client.send_message(
        format_purchase_summary(
            store,
            plan,
            quantity=1,
            operator=operator,
            pricing=pricing,
            flow="renewal",
            vpn_client=vpn_client,
        ),
        chat_id=chat_id,
        reply_markup=purchase_summary_keyboard(has_discount=bool(data.get("discount_code"))),
    )
    return {"ok": True, "handled": True}


def send_current_order_summary(client, config, bot_user, *, chat_id):
    if (bot_user.state_data or {}).get("flow") == "renewal":
        return send_renewal_summary(client, config, bot_user, chat_id=chat_id)
    return send_purchase_summary(client, config, bot_user, chat_id=chat_id)


def show_payment_step(client, config, bot_user, *, chat_id):
    data = bot_user.state_data or {}
    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state(config, bot_user)
        if error:
            bot_user.reset_state()
            client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False, "error": error}
        pricing = pricing_from_state(plan, 1, customer=bot_user.customer, data=data)
        data["step"] = "payment"
        bot_user.state = BotUser.State.BUY_WAIT_NAME
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            renewal_payment_prompt(store, vpn_client, pricing=pricing),
            chat_id=chat_id,
            reply_markup=payment_step_keyboard(),
        )
        return {"ok": True, "handled": True}

    store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}
    pricing = pricing_from_state(plan, quantity, customer=bot_user.customer, data=data)
    data["step"] = "payment"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        format_payment_prompt(
            store,
            plan,
            quantity=quantity,
            operator=operator,
            pricing=pricing,
        ),
        chat_id=chat_id,
        reply_markup=payment_step_keyboard(),
    )
    return {"ok": True, "handled": True}


def payment_prompt_after_name(config, bot_user):
    data = bot_user.state_data or {}
    if data.get("step") == "payment":
        return "حالا عکس رسید تمدید را ارسال کنید." if data.get("flow") == "renewal" else "حالا عکس رسید پرداخت را ارسال کنید."

    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state(config, bot_user)
        if error:
            return "حالا عکس رسید تمدید را ارسال کنید."
        pricing = pricing_from_state(plan, 1, customer=bot_user.customer, data=data)
        return f"{renewal_payment_prompt(store, vpn_client, pricing=pricing)}\n\nنام پرداخت‌کننده ثبت شد. حالا عکس رسید تمدید را ارسال کنید."

    store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        return "حالا عکس رسید پرداخت را ارسال کنید."
    pricing = pricing_from_state(plan, quantity, customer=bot_user.customer, data=data)
    return (
        f"{format_payment_prompt(store, plan, quantity=quantity, operator=operator, pricing=pricing)}\n\n"
        "نام پرداخت‌کننده ثبت شد. حالا عکس رسید پرداخت را ارسال کنید."
    )


def start_discount_code_flow(client, config, bot_user, *, chat_id):
    data = bot_user.state_data or {}
    if not data.get("plan_id"):
        client.send_message("ابتدا یک پلن انتخاب کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}
    data["step"] = "discount"
    bot_user.state = BOT_STATE_BUY_WAIT_DISCOUNT
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "کد تخفیف را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=discount_code_keyboard(),
    )
    return {"ok": True, "handled": True}


def apply_discount_code_for_bot(client, config, bot_user, code, *, chat_id):
    data = bot_user.state_data or {}
    code = DiscountCode.normalize_code(code)
    if not code:
        client.send_message("کد تخفیف خالی است. کد را ارسال کنید یا از این مرحله رد شوید.", chat_id=chat_id, reply_markup=discount_code_keyboard())
        return {"ok": True, "success": False}

    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state(config, bot_user)
        quantity = 1
    else:
        store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}

    try:
        pricing = calculate_bot_pricing(plan, quantity, customer=bot_user.customer, discount_code=code)
    except ValidationError as exc:
        logger.info("Bot discount preview failed code=%s user=%s error=%s", code, bot_user.pk, exc.messages[0])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id, reply_markup=discount_code_keyboard())
        return {"ok": True, "success": False}

    data["discount_code"] = pricing["discount_code"]
    data["discount_amount"] = int(pricing["discount_amount"])
    data["payable_amount"] = int(pricing["payable_amount"])
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(f"کد تخفیف {pricing['discount_code']} اعمال شد.", chat_id=chat_id)
    return send_current_order_summary(client, config, bot_user, chat_id=chat_id)


def skip_discount_for_bot(client, config, bot_user, *, chat_id):
    data = bot_user.state_data or {}
    data.pop("discount_code", None)
    data.pop("discount_amount", None)
    data.pop("payable_amount", None)
    data["step"] = "payment"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return show_payment_step(client, config, bot_user, chat_id=chat_id)


def start_purchase_flow(client, config, bot_user, plan_id, *, chat_id, operator_id=None):
    store = config.store or get_current_store()
    operator = None
    if sales_mode_requires_operator(store):
        operator = get_active_operator(store, operator_id)
        if not operator:
            client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
            bot_user.reset_state()
            return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_store_plans(store, public_only=True, operator=operator).filter(pk=plan_id).first()
    if not plan:
        client.send_message("این پلن پیدا نشد یا دیگر فعال نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Plan not found."}

    bot_user.state = BotUser.State.BUY_WAIT_QUANTITY
    bot_user.state_data = {
        "flow": "purchase",
        "plan_id": plan.pk,
        "operator_id": operator.pk if operator else "",
        "step": "quantity",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        (
            f"پلن انتخابی: {plan.name}\n"
            f"قیمت هر کانفیگ: {bot_money(plan.price, plan.currency)}\n\n"
            f"تعداد کانفیگ را انتخاب کنید یا عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} بفرستید."
        ),
        chat_id=chat_id,
        reply_markup=quantity_keyboard(),
    )
    return {"ok": True, "handled": True}


def continue_purchase_after_quantity(client, config, bot_user, quantity, *, chat_id):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Plan not found."}

    data["quantity"] = quantity
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return send_purchase_summary(client, config, bot_user, chat_id=chat_id)


def start_custom_volume_flow(client, config, bot_user, *, chat_id, operator_id=None):
    store = config.store or get_current_store()
    operator = None
    if sales_mode_requires_operator(store):
        operator = get_active_operator(store, operator_id)
        if not operator:
            client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
            bot_user.reset_state()
            return {"ok": True, "success": False, "error": "Operator required."}
    if not custom_volume_is_available(store):
        client.send_message("خرید حجم دلخواه هنوز فعال نشده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False}
    bot_user.state = BotUser.State.BUY_WAIT_CUSTOM_VOLUME
    bot_user.state_data = {
        "flow": "purchase",
        "operator_id": operator.pk if operator else "",
        "step": "custom_volume",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(custom_volume_prompt_text(store), chat_id=chat_id, reply_markup=cancel_keyboard())
    return {"ok": True, "handled": True}


def continue_purchase_after_custom_volume(client, config, bot_user, volume_value, *, chat_id):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Operator required."}

    try:
        volume_gb = normalize_custom_volume_gb(volume_value)
        plan = get_or_create_custom_volume_plan(store, volume_gb, operator=operator)
    except ValidationError as exc:
        client.send_message(exc.messages[0], chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False}

    bot_user.state = BotUser.State.BUY_WAIT_QUANTITY
    bot_user.state_data = {
        "flow": "purchase",
        "plan_id": plan.pk,
        "operator_id": operator.pk if operator else "",
        "custom_volume_gb": str(volume_gb),
        "step": "quantity",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        (
            f"حجم انتخابی: {bot_volume_label(volume_gb)}\n"
            f"مدت: {persian_digits(CUSTOM_VOLUME_DURATION_DAYS)} روز\n"
            f"قیمت هر کانفیگ: {bot_money(plan.price, plan.currency)}\n\n"
            f"تعداد کانفیگ را انتخاب کنید یا عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} بفرستید."
        ),
        chat_id=chat_id,
        reply_markup=quantity_keyboard(),
    )
    return {"ok": True, "handled": True}


def handle_user_callback(config, bot_user, callback_query, *, chat_id):
    client = BotClient(config)
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client.answer_callback(callback_id, "دریافت شد")
    delete_callback_message(client, callback_query, fallback_chat_id=chat_id)

    if data == CHECK_MEMBERSHIP_CALLBACK:
        membership_response = telegram_membership_required_response(
            config,
            bot_user,
            client,
            chat_id=chat_id,
            force_refresh=True,
        )
        if membership_response:
            return membership_response
        return send_main_menu(client, bot_user, chat_id=chat_id)

    if data.startswith("user:"):
        membership_response = telegram_membership_required_response(config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response

    if data == "user:menu":
        return send_main_menu(client, bot_user, chat_id=chat_id)
    if data == "user:cancel":
        bot_user.reset_state()
        client.send_message(
            "فرایند لغو شد.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "cancelled": True}
    if data == "user:help":
        client.send_message(help_text(), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)))
        return {"ok": True, "handled": True}
    if data == "user:free_trial":
        return start_free_trial_flow(client, config, bot_user, chat_id=chat_id)
    if data == "user:free_trial_confirm":
        return confirm_free_trial_flow(client, config, bot_user, chat_id=chat_id)
    if data == "user:free_trial_cancel":
        bot_user.reset_state()
        client.send_message(
            "دریافت تست رایگان لغو شد.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "cancelled": True}
    if data.startswith(CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX):
        return handle_config_lookup_update_callback(config, bot_user, data, client=client, chat_id=chat_id)
    if data == "user:config_lookup":
        return start_config_lookup_flow(client, bot_user, chat_id=chat_id)
    if data == "user:support":
        return start_support_flow(client, config, bot_user, chat_id=chat_id)
    if data.startswith("user:support_cat:"):
        category = data.rsplit(":", 1)[-1]
        return select_support_category(client, bot_user, category, chat_id=chat_id)
    if data == "user:buy_confirm":
        return show_payment_step(client, config, bot_user, chat_id=chat_id)
    if data == "user:discount:start":
        return start_discount_code_flow(client, config, bot_user, chat_id=chat_id)
    if data == "user:discount:skip":
        return skip_discount_for_bot(client, config, bot_user, chat_id=chat_id)
    if data == "user:buy_back_summary":
        return send_current_order_summary(client, config, bot_user, chat_id=chat_id)
    if data == "user:buy_back_ops":
        return send_operator_list(client, config.store or get_current_store(), chat_id=chat_id)
    if data == "user:buy_back_plans":
        state_data = bot_user.state_data or {}
        store = config.store or get_current_store()
        operator = get_purchase_operator_from_state(store, state_data)
        if sales_mode_requires_operator(store) and not operator:
            return send_operator_list(client, store, chat_id=chat_id)
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True, operator=operator)
    if data == "user:plans":
        return send_plan_list(client, config, chat_id=chat_id)
    if data == "user:buy":
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True)
    if data.startswith("user:buyop:"):
        operator_id = data.rsplit(":", 1)[-1]
        operator = get_active_operator(config.store or get_current_store(), operator_id)
        if not operator:
            client.send_message(OPERATOR_INVALID_MESSAGE, chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True, operator=operator)
    if data == "user:profile":
        return send_profile(client, bot_user, chat_id=chat_id)
    if data == "user:profile_phone":
        return start_profile_phone_flow(client, bot_user, chat_id=chat_id)
    if data == "user:referrals":
        client.send_message(format_referral_panel(bot_user, config), chat_id=chat_id, reply_markup=referral_keyboard(bot_user, config))
        return {"ok": True, "handled": True}
    if data == "user:referral_invite_text":
        if bot_user.customer_id:
            summary = get_referral_summary(
                bot_user.customer,
                store=config.store or get_current_store(),
                bot_config=config,
            )
            text = summary["invite_text"]
        else:
            text = "برای استفاده از دعوت دوستان، ابتدا از منوی اصلی پروفایل خود را بسازید."
        client.send_message(text, chat_id=chat_id, reply_markup=referral_keyboard(bot_user, config), parse_mode=None)
        return {"ok": True, "handled": True}
    if data == "user:referral_help":
        client.send_message(referral_help_text(), chat_id=chat_id, reply_markup=referral_keyboard(bot_user, config))
        return {"ok": True, "handled": True}
    if data == "user:referral_choose":
        client.send_message(
            "کدام کانفیگ را برای دریافت هدیه انتخاب می‌کنید؟",
            chat_id=chat_id,
            reply_markup=referral_config_keyboard(bot_user),
        )
        return {"ok": True, "handled": True}
    if data == "user:referral_redeem":
        return redeem_referral_reward_for_bot(client, bot_user, chat_id=chat_id)
    if data.startswith("user:referral_redeem:"):
        public_id = data.rsplit(":", 1)[-1]
        return redeem_referral_reward_for_bot(client, bot_user, chat_id=chat_id, public_id=public_id)
    if data == "user:renew":
        client.send_message(
            active_subscription_lines(bot_user, title="کدام کانفیگ را تمدید می‌کنید؟", force_refresh=True),
            chat_id=chat_id,
            reply_markup=subscription_management_keyboard(bot_user, renew_mode=True),
        )
        return {"ok": True, "handled": True}
    is_custom_callback, custom_operator_id = parse_buy_custom_callback(data)
    if is_custom_callback:
        return start_custom_volume_flow(client, config, bot_user, chat_id=chat_id, operator_id=custom_operator_id)
    if data == "user:orders":
        text, reply_markup = format_user_orders_list(bot_user)
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return {"ok": True, "handled": True}
    if data == "user:usage":
        client.send_message(
            active_subscription_lines(bot_user, title="حجم باقی‌مانده اشتراک‌های شما", force_refresh=True),
            chat_id=chat_id,
            reply_markup=subscription_management_keyboard(bot_user),
        )
        return {"ok": True, "handled": True}
    if data == "user:subs":
        client.send_message(
            active_subscription_lines(bot_user),
            chat_id=chat_id,
            reply_markup=subscription_management_keyboard(bot_user),
        )
        return {"ok": True, "handled": True}
    if data.startswith("user:buyplan:"):
        plan_id, operator_id = parse_buy_plan_callback(data)
        if not plan_id:
            client.send_message("شناسه پلن نامعتبر است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        return start_purchase_flow(client, config, bot_user, plan_id, chat_id=chat_id, operator_id=operator_id)
    if data.startswith("user:buyqty:"):
        raw_quantity = data.rsplit(":", 1)[-1]
        try:
            quantity = validate_order_quantity(normalize_bot_number(raw_quantity))
        except ValidationError:
            client.send_message(
                f"تعداد باید عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} باشد.",
                chat_id=chat_id,
                reply_markup=quantity_keyboard(),
            )
            return {"ok": True, "success": False}
        return continue_purchase_after_quantity(client, config, bot_user, quantity, chat_id=chat_id)
    if data.startswith("user:order:"):
        tracking_code = data.rsplit(":", 1)[-1]
        order = get_bot_order(bot_user, tracking_code)
        if not order:
            client.send_message("این سفارش پیدا نشد یا دیگر در دسترس نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        client.send_message(format_user_order_detail(order), chat_id=chat_id, reply_markup=order_management_keyboard(order))
        return {"ok": True, "handled": True}
    if data.startswith("user:order_cancel:"):
        tracking_code = data.rsplit(":", 1)[-1]
        order = get_bot_order(bot_user, tracking_code)
        if not order:
            client.send_message("این سفارش پیدا نشد یا دیگر در دسترس نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        if order.status == Order.Status.COMPLETED:
            client.send_message("سفارش تکمیل‌شده قابل لغو نیست. برای تغییر، از پشتیبانی کمک بگیرید.", chat_id=chat_id, reply_markup=order_management_keyboard(order))
            return {"ok": True, "success": False}
        from .order_actions import cancel_order

        result = cancel_order(order)
        if result.success:
            client.send_message("سفارش از فهرست شما حذف شد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        else:
            client.send_message(result.message, chat_id=chat_id, reply_markup=order_management_keyboard(order))
        return {"ok": True, "success": result.success}
    if data.startswith("user:client_usage:"):
        public_id = data.rsplit(":", 1)[-1]
        vpn_client = get_bot_client(bot_user, public_id)
        if not vpn_client:
            client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        stats = sync_vpn_client_stats(vpn_client, force=True)
        vpn_client.refresh_from_db()
        client.send_message(format_client_config(vpn_client, stats=stats), chat_id=chat_id, reply_markup=client_config_keyboard(vpn_client))
        return {"ok": True, "handled": True}
    if data.startswith("user:client_config:"):
        public_id = data.rsplit(":", 1)[-1]
        vpn_client = get_bot_client(bot_user, public_id)
        if not vpn_client:
            client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        client.send_message(format_client_config(vpn_client), chat_id=chat_id, reply_markup=client_config_keyboard(vpn_client))
        return {"ok": True, "handled": True}
    if data.startswith("user:client_refresh:"):
        public_id = data.rsplit(":", 1)[-1]
        vpn_client = get_bot_client(bot_user, public_id)
        if not vpn_client:
            client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        refreshed = refresh_vpn_client_links(vpn_client)
        if not refreshed:
            client.send_message(
                "بروزرسانی کانفیگ از پنل ثنایی انجام نشد. کمی بعد دوباره امتحان کنید یا به پشتیبانی پیام بدهید.",
                chat_id=chat_id,
                reply_markup=client_config_keyboard(vpn_client),
            )
            return {"ok": True, "success": False}
        stats = sync_vpn_client_stats(vpn_client, force=True)
        vpn_client.refresh_from_db()
        client.send_message(format_client_config(vpn_client, stats=stats, refreshed=True), chat_id=chat_id, reply_markup=client_config_keyboard(vpn_client))
        return {"ok": True, "success": True}
    if data.startswith("user:client_renew:"):
        public_id = data.rsplit(":", 1)[-1]
        return start_renewal_flow(client, config, bot_user, public_id, chat_id=chat_id)

    return {"ok": True, "ignored": True}


def handle_user_message(config, bot_user, message, *, chat_id):
    client = BotClient(config)
    text = get_message_text(message)
    lowered = text.lower()
    contact_phone = extract_contact_phone(message)

    if lowered in {"/cancel", "cancel", "لغو", "انصراف"}:
        bot_user.reset_state()
        client.send_message("فرایند لغو شد.", chat_id=chat_id, reply_markup=remove_reply_keyboard())
        membership_response = telegram_membership_required_response(config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response
        client.send_message(
            "از منوی زیر ادامه دهید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)),
        )
        return {"ok": True, "cancelled": True}

    if lowered == "start" or lowered == "/start" or lowered.startswith("/start "):
        referral_code = referral_code_from_start_text(text)
        if referral_code and bot_user.customer_id:
            apply_referral_code(bot_user.customer, referral_code)
        membership_response = telegram_membership_required_response(config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response
        return send_main_menu(client, bot_user, chat_id=chat_id)

    membership_response = telegram_membership_required_response(config, bot_user, client, chat_id=chat_id)
    if membership_response:
        return membership_response

    if contact_phone and bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        success, feedback = save_bot_user_phone(bot_user, contact_phone)
        bot_user.reset_state()
        client.send_message(feedback, chat_id=chat_id, reply_markup=remove_reply_keyboard())
        if success:
            return send_profile(client, bot_user, chat_id=chat_id)
        return {"ok": True, "success": False}

    if contact_phone and bot_user.state == BotUser.State.IDLE:
        success, feedback = save_bot_user_phone(bot_user, contact_phone)
        client.send_message(feedback, chat_id=chat_id, reply_markup=remove_reply_keyboard())
        if success:
            return send_profile(client, bot_user, chat_id=chat_id)
        return {"ok": True, "success": False}

    if is_admin_bot_user(config, bot_user) and lowered in {
        "گزارش مشتریان",
        "گزارش مشتریان 📊",
        "/customer_reports",
        "customer_reports",
    }:
        client.send_message(
            customer_analytics_menu_text(),
            chat_id=chat_id,
            reply_markup=customer_analytics_keyboard(),
        )
        return {"ok": True, "customer_analytics_menu": True}
    if is_admin_bot_user(config, bot_user) and lowered in {
        "ارسال پیام",
        "ارسال پیام 📣",
        "/broadcast",
        "broadcast",
    }:
        return start_broadcast_flow(client, config, bot_user, chat_id=chat_id)
    if lowered in {"بعدا ثبت می‌کنم", "بعدا", "بعداً", "skip"} and bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        bot_user.reset_state()
        client.send_message("باشه، هر وقت خواستید از بخش پروفایل شماره را ثبت کنید.", chat_id=chat_id, reply_markup=remove_reply_keyboard())
        return send_profile(client, bot_user, chat_id=chat_id)

    if is_admin_bot_user(config, bot_user) and bot_user.state == BotUser.State.BROADCAST_WAIT_TEXT:
        return handle_broadcast_text(config, bot_user, text, chat_id=chat_id)

    if is_admin_bot_user(config, bot_user) and bot_user.state == BotUser.State.BROADCAST_CONFIRM:
        client.send_message("برای ارسال یا لغو از دکمه‌های پیش‌نمایش استفاده کنید.", chat_id=chat_id, reply_markup=broadcast_confirm_keyboard())
        return {"ok": True, "broadcast_confirm_waiting": True}

    if lowered in {"/plans", "plans", "پلن", "پلن‌ها", "پلن ها"}:
        return send_plan_list(client, config, chat_id=chat_id)
    if lowered in {"/buy", "buy", "خرید", "خرید سرویس", "خرید سرویس 🛒"}:
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True)
    if lowered in {
        "/free_trial",
        "free_trial",
        "free trial",
        "تست رایگان",
        "دریافت تست رایگان",
        "🎁 دریافت تست رایگان",
    }:
        return start_free_trial_flow(client, config, bot_user, chat_id=chat_id)
    if lowered in {"/profile", "profile", "پروفایل", "حساب"}:
        return send_profile(client, bot_user, chat_id=chat_id)
    if lowered in {"/phone", "phone", "شماره", "موبایل"}:
        return start_profile_phone_flow(client, bot_user, chat_id=chat_id)
    if lowered in {"/orders", "orders", "سفارش", "سفارش‌ها", "سفارش ها", "سفارش‌های من 📦"}:
        text, reply_markup = format_user_orders_list(bot_user)
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return {"ok": True, "handled": True}
    if lowered in {"/usage", "usage", "حجم", "باقی مانده", "باقی‌مانده"}:
        client.send_message(
            active_subscription_lines(bot_user, title="حجم باقی‌مانده اشتراک‌های شما", force_refresh=True),
            chat_id=chat_id,
            reply_markup=subscription_management_keyboard(bot_user),
        )
        return {"ok": True, "handled": True}
    if lowered in {"/subscription", "/subs", "subscription", "سرویس‌های من", "سرویس های من", "سرویس‌های من 🔐"}:
        client.send_message(active_subscription_lines(bot_user), chat_id=chat_id, reply_markup=subscription_management_keyboard(bot_user))
        return {"ok": True, "handled": True}
    if lowered in {"/renew", "renew", "تمدید", "تمدید سرویس", "تمدید سرویس 🔄"}:
        client.send_message(
            active_subscription_lines(bot_user, title="کدام کانفیگ را تمدید می‌کنید؟", force_refresh=True),
            chat_id=chat_id,
            reply_markup=subscription_management_keyboard(bot_user, renew_mode=True),
        )
        return {"ok": True, "handled": True}
    if lowered in {
        "/check_config",
        "check_config",
        "مشاهده باقی‌مانده کانفیگ",
        "مشاهده باقی مانده کانفیگ",
        "مشاهده باقی‌مانده کانفیگ 📊",
    }:
        return start_config_lookup_flow(client, bot_user, chat_id=chat_id)
    if lowered in {"/referrals", "referrals", "دعوت دوستان", "دعوت دوستان 🎁"}:
        client.send_message(format_referral_panel(bot_user, config), chat_id=chat_id, reply_markup=referral_keyboard(bot_user, config))
        return {"ok": True, "handled": True}
    if lowered in {"/support", "support", "پشتیبانی", "پشتیبانی 💬"}:
        return start_support_flow(client, config, bot_user, chat_id=chat_id)
    if lowered in {"/help", "help", "راهنما", "راهنما ❓"}:
        client.send_message(help_text(), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user(config, bot_user)))
        return {"ok": True, "handled": True}

    if bot_user.state == BOT_STATE_CONFIG_LOOKUP_WAIT_LINK:
        return handle_config_lookup_text(config, bot_user, text, chat_id=chat_id)

    if bot_user.state == BOT_STATE_SUPPORT_WAIT_MESSAGE:
        return create_support_ticket_from_bot(config, bot_user, text, message, chat_id=chat_id)

    if bot_user.state == BOT_STATE_BUY_WAIT_DISCOUNT:
        if lowered in {"skip", "رد", "رد شدن", "بدون تخفیف"}:
            return skip_discount_for_bot(client, config, bot_user, chat_id=chat_id)
        return apply_discount_code_for_bot(client, config, bot_user, text, chat_id=chat_id)

    if bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        success, feedback = save_bot_user_phone(bot_user, text)
        if not success:
            client.send_message(feedback, chat_id=chat_id, reply_markup=phone_request_keyboard())
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(feedback, chat_id=chat_id, reply_markup=remove_reply_keyboard())
        return send_profile(client, bot_user, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_CUSTOM_VOLUME:
        return continue_purchase_after_custom_volume(client, config, bot_user, text, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_QUANTITY:
        try:
            quantity = validate_order_quantity(normalize_bot_number(text))
        except ValidationError:
            client.send_message(
                f"تعداد باید عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} باشد.",
                chat_id=chat_id,
                reply_markup=quantity_keyboard(),
            )
            return {"ok": True, "handled": True}
        return continue_purchase_after_quantity(client, config, bot_user, quantity, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_NAME:
        if not text:
            client.send_message("لطفا نام پرداخت‌کننده را به صورت متن ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        data = bot_user.state_data or {}
        data["sender_card_name"] = text[:100]
        data["payment_time"] = timezone.localtime(timezone.now()).strftime("%H:%M")
        if data.get("flow") == "renewal":
            if is_admin_bot_user(config, bot_user):
                bot_user.state_data = data
                bot_user.save(update_fields=["state_data", "updated_at"])
                return finalize_admin_direct_renewal(config, bot_user, chat_id=chat_id)
            prompt_text = payment_prompt_after_name(config, bot_user)
            bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
            data["step"] = "receipt"
            bot_user.state_data = data
            bot_user.save(update_fields=["state", "state_data", "updated_at"])
            client.send_message(prompt_text, chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        if is_admin_bot_user(config, bot_user):
            bot_user.state_data = data
            bot_user.save(update_fields=["state_data", "updated_at"])
            return finalize_admin_direct_purchase(config, bot_user, chat_id=chat_id)
        prompt_text = payment_prompt_after_name(config, bot_user)
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        data["step"] = "receipt"
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(prompt_text, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state in {
        BotUser.State.BUY_WAIT_LAST4,
        BotUser.State.BUY_WAIT_TIME,
        BotUser.State.BUY_WAIT_TRACKING,
    }:
        data = bot_user.state_data or {}
        data["payment_time"] = data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M")
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message("برای تکمیل سفارش فقط عکس رسید پرداخت را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_RECEIPT:
        file_info = extract_receipt_file(message)
        if not file_info:
            client.send_message("لطفا عکس رسید یا فایل تصویری رسید را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        type_error = receipt_file_type_error(file_info)
        if type_error:
            client.send_message(type_error, chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "success": False}
        if (bot_user.state_data or {}).get("flow") == "renewal":
            return finalize_bot_renewal(config, bot_user, message, file_info, chat_id=chat_id)
        return finalize_bot_purchase(config, bot_user, message, file_info, chat_id=chat_id)

    if text:
        client.send_message("برای شروع از منوی زیر استفاده کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    return {"ok": True, "ignored": True}


def create_bot_payment_order(*, config, bot_user, plan, metadata, receipt_image=None, require_receipt_image=False):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    quantity = data.get("quantity", 1)
    operator = get_purchase_operator_from_state(store, data)
    return create_manual_payment_order(
        store=store,
        customer=bot_user.customer,
        plan=plan,
        operator=operator,
        sender_card_name=(bot_user.state_data or {}).get("sender_card_name", ""),
        sender_card_last4="",
        payment_time=(bot_user.state_data or {}).get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M"),
        receipt_image=receipt_image,
        require_receipt_image=require_receipt_image,
        bank_tracking_code="",
        discount_code=(bot_user.state_data or {}).get("discount_code", ""),
        quantity=quantity,
        metadata=metadata,
    )


def submit_renewal_payment(order, bot_user, *, receipt_image=None, require_receipt_image=False):
    data = bot_user.state_data or {}
    try:
        order.submit_manual_payment(
            sender_card_name=data.get("sender_card_name", ""),
            sender_card_last4="",
            payment_time=data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M"),
            receipt_image=receipt_image,
            require_receipt_image=require_receipt_image,
        )
        order.save()
    except ValidationError as exc:
        return False, exc.messages[0]
    except OSError:
        logger.exception("Could not save renewal receipt image.")
        return False, "Could not save the receipt image. Please try a smaller JPG or PNG image, or contact support."
    return True, ""


def create_bot_renewal_order(config, bot_user, vpn_client, metadata):
    result = create_renewal_payment_order(
        customer=bot_user.customer,
        vpn_client=vpn_client,
        metadata=metadata,
        discount_code=(bot_user.state_data or {}).get("discount_code", ""),
    )
    if not result.success:
        return result
    return result


def finalize_admin_direct_renewal(config, bot_user, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        bot_user.reset_state()
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    metadata = bot_order_metadata(
        config,
        bot_user,
        source=f"{config.provider}_admin_bot_direct_renewal",
        extra={
            "admin_direct_purchase": True,
            "suppress_new_order_notification": True,
            "suppress_admin_order_updates": True,
        },
    )
    result = create_bot_renewal_order(config, bot_user, vpn_client, metadata)
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    submitted, submit_message = submit_renewal_payment(order, bot_user)
    if not submitted:
        client.send_message(submit_message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": submit_message}

    from .order_actions import activate_order

    activation = activate_order(order, user=None, notify=False)
    order.refresh_from_db()
    bot_user.reset_state()
    if not activation.success:
        client.send_message(
            f"سفارش تمدید ساخته شد اما تمدید کانفیگ ناموفق بود.\n{activation.message}",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(),
        )
        return {"ok": True, "success": False, "order": order.order_tracking_code, "message": activation.message}

    client.send_message(format_customer_order_event(order, event_type="approved"), chat_id=chat_id, reply_markup=main_menu_keyboard())
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_admin_direct_purchase(config, bot_user, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    store = config.store or get_current_store()
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        bot_user.reset_state()
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        bot_user.reset_state()
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": "Plan not found."}

    metadata = {
        "source": f"{config.provider}_admin_bot_direct",
        "admin_direct_purchase": True,
        "suppress_new_order_notification": True,
        "suppress_admin_order_updates": True,
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    result = create_bot_payment_order(
        config=config,
        bot_user=bot_user,
        plan=plan,
        metadata=metadata,
        receipt_image=None,
        require_receipt_image=False,
    )
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    from .order_actions import activate_order

    activation = activate_order(order, user=None, notify=False)
    order.refresh_from_db()
    bot_user.reset_state()
    if not activation.success:
        client.send_message(
            f"سفارش ساخته شد اما ساخت یا فعال‌سازی کانفیگ ناموفق بود.\n{activation.message}",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(),
        )
        return {"ok": True, "success": False, "order": order.order_tracking_code, "message": activation.message}

    client.send_message(format_customer_order_event(order, event_type="approved"), chat_id=chat_id, reply_markup=main_menu_keyboard())
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_bot_purchase(config, bot_user, message, file_info, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    store = config.store or get_current_store()
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        bot_user.reset_state()
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        bot_user.reset_state()
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": "Plan not found."}

    metadata = {
        "source": f"{config.provider}_bot",
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    receipt_image = attach_bot_receipt(client, config, file_info, metadata)

    result = create_bot_payment_order(
        config=config,
        bot_user=bot_user,
        plan=plan,
        metadata=metadata,
        receipt_image=receipt_image,
        require_receipt_image=bool(receipt_image),
    )
    if not result.success:
        logger.warning("Bot purchase creation failed user=%s message=%s", bot_user.pk, result.message)
        client.send_message(result.message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    bot_user.reset_state()
    if result.duplicate_detected:
        client.send_message(
            "این سفارش قبلاً ثبت شده و هنوز در انتظار بررسی است.\n\n"
            f"{format_user_order_detail(order)}",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(order),
        )
    else:
        client.send_message(user_order_summary(order), chat_id=chat_id, reply_markup=main_menu_keyboard())
    if file_info and not order.payment_receipt_image:
        forward_receipt_to_admin(client, order, file_info, from_chat_id=chat_id)
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_bot_renewal(config, bot_user, message, file_info, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        bot_user.reset_state()
        client.send_message("این کانفیگ پیدا نشد. لطفا دوباره تمدید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    pending_order = pending_renewal_order(bot_user, vpn_client)
    if pending_order:
        bot_user.reset_state()
        client.send_message(
            "برای این کانفیگ از قبل یک تمدید در انتظار پرداخت یا تایید وجود دارد.",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(pending_order),
        )
        return {"ok": True, "handled": True, "pending": True}

    metadata = bot_order_metadata(
        config,
        bot_user,
        source=f"{config.provider}_bot_renewal",
        extra={"suppress_new_order_notification": True},
    )
    receipt_image = attach_bot_receipt(client, config, file_info, metadata)
    if receipt_image is not None:
        try:
            validate_payment_receipt_image(receipt_image)
        except ValidationError as exc:
            client.send_message(exc.messages[0], chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "success": False, "error": exc.messages[0]}

    result = create_bot_renewal_order(config, bot_user, vpn_client, metadata)
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    submitted, submit_message = submit_renewal_payment(
        order,
        bot_user,
        receipt_image=receipt_image,
        require_receipt_image=bool(receipt_image),
    )
    if not submitted:
        logger.warning("Bot renewal payment submission failed user=%s order=%s message=%s", bot_user.pk, order.pk, submit_message)
        client.send_message(submit_message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": submit_message}

    bot_user.reset_state()
    order_metadata = dict(order.metadata or {})
    order_metadata.pop("suppress_new_order_notification", None)
    order.metadata = order_metadata
    order.save(update_fields=["metadata", "updated_at"])
    from .admin_notifications import schedule_notify_admins_payment_receipt

    schedule_notify_admins_payment_receipt(order.pk)
    client.send_message(user_order_summary(order), chat_id=chat_id, reply_markup=main_menu_keyboard())
    if file_info and not order.payment_receipt_image:
        forward_receipt_to_admin(client, order, file_info, from_chat_id=chat_id)
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def forward_receipt_to_admin(client, order, file_info, *, from_chat_id):
    if not file_info or not file_info.get("message_id"):
        return
    for admin_user_id in client.config.get_admin_user_ids():
        try:
            client.forward_message(from_chat_id=from_chat_id, message_id=file_info["message_id"], chat_id=admin_user_id)
        except BotDeliveryError as exc:
            log_event(
                client.config,
                event_type=BotEventLog.EventType.ERROR,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Could not forward receipt: {exc}",
                raw_payload={"order_id": order.order_tracking_code, "receipt": file_info, "admin_user_id": admin_user_id},
            )


def handle_support_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")

    action, conversation_id = parse_support_callback_data(data)
    admin_user_id = normalize_id(get_sender_object(callback_query))
    log_callback(
        config,
        status=BotEventLog.Status.RECEIVED,
        message=(
            f"support_callback_data={data!r}; action={action or '-'}; "
            f"conversation_id={conversation_id or '-'}; admin_user_id={admin_user_id or '-'}"
        ),
        raw_payload={
            "callback_data": data,
            "callback_id": callback_id,
            "admin_user_id": admin_user_id,
            "chat_id": chat_id,
            "callback_query": callback_query,
        },
    )

    if not action or not conversation_id:
        client.send_message(f"Malformed support callback data: <code>{escape(data)}</code>", chat_id=chat_id)
        return {"ok": True, "ignored": True, "error": "Malformed support callback data."}

    conversation = (
        SupportConversation.objects.select_related("store", "customer")
        .filter(pk=conversation_id)
        .first()
    )
    if not conversation:
        client.send_message("گفتگوی پشتیبانی پیدا نشد.", chat_id=chat_id)
        return {"ok": True, "success": False, "error": "Support conversation not found."}

    if action == "close":
        conversation.close()
        BotPendingAction.objects.filter(
            bot_config=config,
            support_conversation=conversation,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        try:
            message_ref = extract_callback_message_reference(callback_query or {}, fallback_chat_id=chat_id)
            if message_ref["chat_id"] and message_ref["message_id"]:
                client.edit_message(
                    chat_id=message_ref["chat_id"],
                    message_id=message_ref["message_id"],
                    text=format_support_message(conversation, title="گفتگوی پشتیبانی بسته شد"),
                    reply_markup=empty_inline_keyboard(),
                )
                return {"ok": True, "closed": True}
        except BotDeliveryError:
            pass
        client.send_message("گفتگوی پشتیبانی بسته شد.", chat_id=chat_id)
        return {"ok": True, "closed": True}

    if action == "reply":
        BotPendingAction.objects.filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        BotPendingAction.objects.create(
            bot_config=config,
            support_conversation=conversation,
            admin_user_id=admin_user_id,
            action=BotPendingAction.Action.SUPPORT_REPLY,
        )
        client.send_message(
            (
                "پاسخ این گفتگوی پشتیبانی را ارسال کن.\n\n"
                f"<b>شناسه گفتگو:</b> <code>{conversation.pk}</code>\n"
                f"<b>تماس مشتری:</b> <code>{escape(conversation.contact_value or '-')}</code>"
            ),
            chat_id=chat_id,
        )
        return {"ok": True, "waiting_for_support_reply": True}

    return {"ok": True, "ignored": True}


def handle_customer_analytics_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")

    report_key = data[len(CUSTOMER_ANALYTICS_CALLBACK_PREFIX):]
    if report_key == "menu":
        client.send_message(
            customer_analytics_menu_text(),
            chat_id=chat_id,
            reply_markup=customer_analytics_keyboard(),
        )
        return {"ok": True, "success": True, "customer_analytics_menu": True}

    text = format_customer_analytics_report(report_key, config=config)
    client.send_message(text, chat_id=chat_id, reply_markup=customer_analytics_keyboard())
    return {"ok": True, "success": True, "customer_analytics_report": report_key}


def handle_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")

    action, tracking_code = parse_callback_data(data)
    admin_user_id = normalize_id(get_sender_object(callback_query))
    log_callback(
        config,
        status=BotEventLog.Status.RECEIVED,
        message=(
            f"callback_data={data!r}; action={action or '-'}; "
            f"order_id={tracking_code or '-'}; admin_user_id={admin_user_id or '-'}"
        ),
        raw_payload={
            "callback_data": data,
            "callback_id": callback_id,
            "admin_user_id": admin_user_id,
            "chat_id": chat_id,
            "callback_query": callback_query,
        },
    )

    if not action or not tracking_code:
        log_callback(
            config,
            status=BotEventLog.Status.FAILED,
            message=f"Malformed callback data: {data!r}",
            raw_payload={"callback_data": data, "callback_query": callback_query},
        )
        client.send_message(f"Malformed callback data: <code>{data}</code>", chat_id=chat_id)
        return {"ok": True, "ignored": True, "error": "Malformed callback data."}

    order = Order.objects.select_related("plan", "store", "inbound", "inbound__panel").filter(
        order_tracking_code=tracking_code
    ).first()
    if not order:
        log_callback(
            config,
            status=BotEventLog.Status.FAILED,
            message=f"Order not found for callback_data={data!r}; order_id={tracking_code}",
            raw_payload={"callback_data": data, "order_id": tracking_code},
        )
        client.send_message("Order was not found.", chat_id=chat_id)
        return {"ok": True, "success": False, "error": "Order not found."}

    log_callback(
        config,
        status=BotEventLog.Status.RECEIVED,
        order=order,
        message=f"Processing {action} for order {order.order_tracking_code}; admin_user_id={admin_user_id or '-'}",
        raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
    )

    if action == "detail":
        client.send_message(
            format_order_message(order, title="جزئیات سفارش"),
            chat_id=chat_id,
            reply_markup=order_admin_keyboard(order),
        )
        log_callback(
            config,
            status=BotEventLog.Status.SUCCESS,
            order=order,
            message=f"Order detail sent for {order.order_tracking_code}; admin_user_id={admin_user_id or '-'}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        return {"ok": True, "success": True, "detail": True}

    if action == "approve":
        from .order_actions import activate_order

        log_callback(
            config,
            status=BotEventLog.Status.RECEIVED,
            order=order,
            message=f"Calling activate_order for order {order.order_tracking_code}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        result = activate_order(order, user=None, notify=True)
        order.refresh_from_db()
        updated_count = sync_admin_order_messages(
            order,
            title="Order approved" if result.success else "Order approval failed",
            event_type=BotEventLog.EventType.ORDER_APPROVED if result.success else BotEventLog.EventType.ERROR,
            prefix_message=result.message,
            respect_notify=False,
            configs=[config],
        )
        log_callback(
            config,
            status=BotEventLog.Status.SUCCESS if result.success else BotEventLog.Status.FAILED,
            order=order,
            message=f"activate_order finished for {order.order_tracking_code}: success={result.success}; message={result.message}",
            raw_payload={
                "callback_data": data,
                "order_id": tracking_code,
                "action": action,
                "success": result.success,
                "message": result.message,
                "status": order.status,
                "verification_status": order.verification_status,
            },
        )
        if not updated_count:
            send_final_order_status(
                client,
                order,
                title="Order approved" if result.success else "Order approval failed",
                chat_id=chat_id,
                callback_query=callback_query,
                prefix_message=result.message,
            )
        return {"ok": True, "success": result.success, "message": result.message}

    if action == "reject":
        order.refresh_from_db()
        if order.status == Order.Status.COMPLETED:
            log_callback(
                config,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Reject denied because order {order.order_tracking_code} is already completed.",
                raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
            )
            send_final_order_status(
                client,
                order,
                title="Order already completed",
                chat_id=chat_id,
                callback_query=callback_query,
                prefix_message="Completed orders cannot be rejected from the bot.",
            )
            return {"ok": True, "success": False, "message": "Completed order cannot be rejected."}

        BotPendingAction.objects.filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        BotPendingAction.objects.create(
            bot_config=config,
            order=order,
            admin_user_id=admin_user_id,
            action=BotPendingAction.Action.REJECT_ORDER,
        )
        log_callback(
            config,
            status=BotEventLog.Status.SUCCESS,
            order=order,
            message=f"Reject reason requested for order {order.order_tracking_code}; admin_user_id={admin_user_id}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        client.send_message(
            f"Please send the rejection reason for order <code>{order.order_tracking_code}</code>.",
            chat_id=chat_id,
        )
        return {"ok": True, "waiting_for_reason": True}

    return {"ok": True, "ignored": True}


def handle_message_update(config, message, *, chat_id, admin_user_id=""):
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True, "ignored": True}
    admin_user_id = str(admin_user_id or config.admin_user_id)

    pending = (
        BotPendingAction.objects.select_related(
            "order",
            "order__plan",
            "order__store",
            "support_conversation",
            "support_conversation__store",
            "support_conversation__customer",
        )
        .filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )
    if not pending:
        return {"ok": True, "ignored": True}

    if pending.action == BotPendingAction.Action.SUPPORT_REPLY:
        conversation = pending.support_conversation
        if not conversation:
            pending.status = BotPendingAction.Status.CANCELLED
            pending.resolved_at = timezone.now()
            pending.save(update_fields=["status", "resolved_at", "updated_at"])
            BotClient(config).send_message("این گفتگوی پشتیبانی دیگر پیدا نمی‌شود.", chat_id=chat_id)
            return {"ok": True, "success": False, "error": "Support conversation not found."}

        support_message = SupportMessage.objects.create(
            conversation=conversation,
            sender_type=SupportMessage.SenderType.ADMIN,
            bot_config=config,
            body=text,
            metadata={
                "provider": config.provider,
                "admin_user_id": admin_user_id,
                "chat_id": chat_id,
                "message_id": get_message_id(message),
            },
        )
        conversation.mark_answered()
        pending.mark_completed()
        log_event(
            config,
            event_type=BotEventLog.EventType.SUPPORT_REPLY,
            status=BotEventLog.Status.SUCCESS,
            message=f"Support reply saved for conversation {conversation.pk}",
            raw_payload={
                "conversation_id": conversation.pk,
                "support_message_id": support_message.pk,
                "chat_id": chat_id,
            },
        )
        BotClient(config).send_message(
            f"پاسخ پشتیبانی ثبت شد و در صفحه پشتیبانی مشتری نمایش داده می‌شود.\n\n<code>{escape(text[:500])}</code>",
            chat_id=chat_id,
        )
        return {"ok": True, "success": True, "support_conversation": conversation.pk}

    if pending.action != BotPendingAction.Action.REJECT_ORDER:
        return {"ok": True, "ignored": True}

    from .order_actions import reject_order

    log_callback(
        config,
        status=BotEventLog.Status.RECEIVED,
        order=pending.order,
        message=f"Reject reason received for order {pending.order.order_tracking_code}; reason={text!r}",
        raw_payload={"reason": text, "chat_id": chat_id},
    )
    result = reject_order(pending.order, reason=text, user=None, notify=True)
    pending.mark_completed()
    pending.order.refresh_from_db()
    updated_count = sync_admin_order_messages(
        pending.order,
        title="Order rejected" if result.success else "Order rejection failed",
        event_type=BotEventLog.EventType.ORDER_REJECTED if result.success else BotEventLog.EventType.ERROR,
        prefix_message=result.message,
        respect_notify=False,
        configs=[config],
    )
    log_callback(
        config,
        status=BotEventLog.Status.SUCCESS if result.success else BotEventLog.Status.FAILED,
        order=pending.order,
        message=f"reject_order finished for {pending.order.order_tracking_code}: success={result.success}; message={result.message}",
        raw_payload={
            "reason": text,
            "success": result.success,
            "message": result.message,
            "status": pending.order.status,
            "verification_status": pending.order.verification_status,
        },
    )
    if not updated_count:
        BotClient(config).send_message(
            f"{result.message}\n\n{format_order_message(pending.order, title='Order rejected')}",
            chat_id=chat_id,
        )
    return {"ok": True, "success": result.success, "message": result.message}


def build_sales_report(config):
    now = timezone.now()
    start = config.last_report_sent_at or (now - timedelta(hours=config.report_interval_hours))
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=now)
    if config.store_id:
        orders = orders.filter(store=config.store)

    completed = orders.filter(status=Order.Status.COMPLETED)
    pending = orders.filter(status=Order.Status.PENDING_VERIFICATION)
    rejected = orders.filter(status=Order.Status.REJECTED)
    gross = completed.aggregate(total=Sum("amount"))["total"] or 0
    completed_count = completed.count()
    plan_rows = (
        completed.values("plan__name")
        .annotate(count=Count("id"), total=Sum("amount"))
        .order_by("-count")[:5]
    )

    lines = [
        f"<b>{config.report_interval_hours}-hour sales report</b>",
        "",
        f"<b>Window:</b> {timezone.localtime(start).strftime('%Y-%m-%d %H:%M')} - {timezone.localtime(now).strftime('%H:%M')}",
        f"<b>Completed orders:</b> {completed_count}",
        f"<b>Pending verification:</b> {pending.count()}",
        f"<b>Rejected:</b> {rejected.count()}",
        f"<b>Revenue:</b> {money(gross)}",
    ]
    if plan_rows:
        lines.extend(["", "<b>Top plans:</b>"])
        for row in plan_rows:
            lines.append(f"- {row['plan__name'] or '-'}: {row['count']} / {money(row['total'] or 0)}")
    return "\n".join(lines), now


def send_due_sales_reports(*, force=False):
    sent = 0
    now = timezone.now()
    for config in active_bot_configs(reports=True):
        due_at = (
            config.last_report_sent_at + timedelta(hours=config.report_interval_hours)
            if config.last_report_sent_at
            else None
        )
        if not force and due_at and due_at > now:
            continue

        report, sent_at = build_sales_report(config)
        if send_to_config(
            config,
            text=report,
            event_type=BotEventLog.EventType.SALES_REPORT,
        ):
            config.last_report_sent_at = sent_at
            config.save(update_fields=["last_report_sent_at", "updated_at"])
            sent += 1
    return sent
