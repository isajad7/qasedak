import logging
import re
from datetime import timedelta
from html import escape
from pathlib import PurePosixPath

import requests
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import BotConfiguration, BotEventLog, BotPendingAction, BotUser, Customer, Order, VPNClient, parse_payment_time
from .order_services import create_manual_payment_order, get_current_store, get_store_plans
from .xui_api import sync_vpn_client_stats

logger = logging.getLogger(__name__)

BOT_TIMEOUT_SECONDS = 12


class BotDeliveryError(Exception):
    pass


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

    def call(self, method, payload):
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=BOT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise BotDeliveryError(str(exc)) from exc

        if data.get("ok") is False:
            raise BotDeliveryError(data.get("description") or data.get("message") or "Bot API rejected request.")
        return data

    def send_message(self, text, *, reply_markup=None, chat_id=None):
        payload = {
            "chat_id": str(chat_id or self.config.admin_user_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

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

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path):
        if not file_path:
            return None
        file_base = self.FILE_BASE_URLS[self.config.provider].format(token=self.config.bot_token).rstrip("/")
        file_url = f"{file_base}/{file_path.lstrip('/')}"
        try:
            response = requests.get(file_url, timeout=BOT_TIMEOUT_SECONDS)
            response.raise_for_status()
        except Exception as exc:
            raise BotDeliveryError(str(exc)) from exc
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


def active_bot_configs(order=None, *, reports=False):
    configs = BotConfiguration.objects.filter(is_active=True).exclude(bot_token="").exclude(admin_user_id="")
    if order and order.store_id:
        configs = configs.filter(Q(store=order.store) | Q(store__isnull=True))
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


def order_admin_keyboard(order):
    tracking_code = order.order_tracking_code
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approve:{tracking_code}"},
                {"text": "Reject", "callback_data": f"reject:{tracking_code}"},
            ]
        ]
    }


def empty_inline_keyboard():
    return {"inline_keyboard": []}


def format_order_message(order, *, title="New VPN order"):
    metadata = order.metadata or {}
    receipt_analysis = metadata.get("receipt_analysis") or {}
    receipt_text = (metadata.get("receipt_text") or "").strip()
    lines = [
        f"<b>{title}</b>",
        "",
        f"<b>Tracking:</b> <code>{order.order_tracking_code}</code>",
        f"<b>Plan:</b> {order.plan.name if order.plan_id else '-'}",
        f"<b>Traffic:</b> {order.plan.volume_gb if order.plan_id else '-'} GB",
        f"<b>Amount:</b> {money(order.amount, order.currency)}",
        f"<b>Status:</b> {order.get_status_display()}",
        f"<b>Verification:</b> {order.get_verification_status_display()}",
        f"<b>Payer:</b> {order.sender_card_name or '-'}",
        f"<b>Card last4:</b> {order.sender_card_last4 or '-'}",
        f"<b>Payment time:</b> {order.payment_date or '-'} {order.payment_time.strftime('%H:%M') if order.payment_time else ''}",
        f"<b>Config:</b> <code>{order.username}</code>",
    ]
    if order.bank_tracking_code:
        lines.append(f"<b>Bank tracking:</b> <code>{order.bank_tracking_code}</code>")
    if receipt_analysis:
        status = receipt_analysis.get("status") or "-"
        lines.append(f"<b>Receipt check:</b> {status}")
        expected = receipt_analysis.get("expected_amount_irr")
        detected = receipt_analysis.get("matched_amount_irr")
        if expected is not None:
            lines.append(f"<b>Expected receipt amount:</b> {money(expected, 'IRR')}")
        if detected is not None:
            lines.append(f"<b>Detected receipt amount:</b> {money(detected, 'IRR')}")
        if receipt_analysis.get("requires_admin_review"):
            lines.append("<b>Warning:</b> Manual receipt review needed.")
        if receipt_analysis.get("warning"):
            lines.append(f"<b>Receipt warning:</b> {escape(str(receipt_analysis['warning']))}")
    if receipt_text:
        excerpt = receipt_text[:350] + ("..." if len(receipt_text) > 350 else "")
        lines.append(f"<b>Receipt text:</b> {escape(excerpt)}")
    if order.rejection_reason:
        lines.append(f"<b>Reject reason:</b> {order.rejection_reason}")
    return "\n".join(lines)


def log_event(config, *, event_type, status, order=None, message="", raw_payload=None):
    return BotEventLog.objects.create(
        bot_config=config,
        order=order,
        event_type=event_type,
        status=status,
        message=message,
        raw_payload=raw_payload or {},
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


def send_to_config(config, *, text, event_type, order=None, reply_markup=None):
    client = BotClient(config)
    try:
        client.send_message(text, reply_markup=reply_markup)
    except BotDeliveryError as exc:
        config.last_error = str(exc)
        config.save(update_fields=["last_error", "updated_at"])
        log_event(
            config,
            event_type=event_type,
            status=BotEventLog.Status.FAILED,
            order=order,
            message=str(exc),
        )
        logger.warning("Bot notification failed for %s: %s", config, exc)
        return False

    if config.last_error:
        config.last_error = ""
        config.save(update_fields=["last_error", "updated_at"])
    log_event(config, event_type=event_type, status=BotEventLog.Status.SENT, order=order, message=text)
    return True


def notify_new_order(order):
    for config in active_bot_configs(order):
        if not config.notify_new_orders:
            continue
        send_to_config(
            config,
            text=format_order_message(order),
            event_type=BotEventLog.EventType.NEW_ORDER,
            order=order,
            reply_markup=order_admin_keyboard(order),
        )


def notify_order_event(order, *, event_type):
    title_by_event = {
        "approved": "Order approved",
        "rejected": "Order rejected",
    }
    log_type_by_event = {
        "approved": BotEventLog.EventType.ORDER_APPROVED,
        "rejected": BotEventLog.EventType.ORDER_REJECTED,
    }
    for config in active_bot_configs(order):
        if not config.notify_order_updates:
            continue
        send_to_config(
            config,
            text=format_order_message(order, title=title_by_event.get(event_type, "Order updated")),
            event_type=log_type_by_event.get(event_type, BotEventLog.EventType.WEBHOOK),
            order=order,
        )
    notify_customer_order_event(order, event_type=event_type)


def format_customer_order_event(order, *, event_type):
    if event_type == "approved":
        lines = [
            "<b>اشتراک شما فعال شد</b>",
            "",
            f"<b>کد پیگیری:</b> <code>{order.order_tracking_code}</code>",
            f"<b>پلن:</b> {order.plan.name if order.plan_id else '-'}",
        ]
        if order.sub_link:
            lines.append(f"<b>لینک اشتراک:</b> {order.sub_link}")
        if order.direct_link:
            lines.extend(["", "<b>لینک مستقیم:</b>", f"<code>{order.direct_link}</code>"])
        return "\n".join(lines)

    if event_type == "rejected":
        lines = [
            "<b>پرداخت شما تایید نشد</b>",
            "",
            f"<b>کد پیگیری:</b> <code>{order.order_tracking_code}</code>",
        ]
        if order.rejection_reason:
            lines.append(f"<b>دلیل:</b> {order.rejection_reason}")
        lines.append("برای پیگیری با پشتیبانی در ارتباط باشید.")
        return "\n".join(lines)

    return format_order_message(order, title="Order updated")


def notify_customer_order_event(order, *, event_type):
    if not order.customer_id or (order.metadata or {}).get("suppress_customer_notification"):
        return 0

    bot_users = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=order.customer,
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(chat_id="")
    )
    if order.store_id:
        bot_users = bot_users.filter(Q(bot_config__store=order.store) | Q(bot_config__store__isnull=True))

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


def is_admin_callback_data(data):
    action, tracking_code = parse_callback_data(data)
    return bool(action and tracking_code)


def sender_display_name(sender):
    first_name = first_present(sender, "first_name", "firstName") or ""
    last_name = first_present(sender, "last_name", "lastName") or ""
    username = first_present(sender, "username") or ""
    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if not display_name and username:
        display_name = f"@{username}"
    return display_name


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
            bot_user.customer = Customer.objects.create(display_name=display_name)
        elif display_name and bot_user.customer.display_name.startswith("Customer "):
            bot_user.customer.display_name = display_name
            bot_user.customer.save(update_fields=["display_name", "updated_at"])
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

    customer = Customer.objects.create(display_name=display_name)
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


def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "خرید اشتراک", "callback_data": "user:buy"}],
            [{"text": "مشاهده پلن‌ها", "callback_data": "user:plans"}],
            [{"text": "اشتراک فعال من", "callback_data": "user:subs"}],
        ]
    }


def cancel_keyboard():
    return {"inline_keyboard": [[{"text": "انصراف", "callback_data": "user:cancel"}]]}


def plan_keyboard(plans, *, prefix="user:buyplan"):
    rows = []
    for plan in plans:
        rows.append(
            [
                {
                    "text": f"{plan.name} - {money(plan.price, plan.currency)}",
                    "callback_data": f"{prefix}:{plan.pk}",
                }
            ]
        )
    rows.append([{"text": "بازگشت", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_plan_lines(plans):
    lines = ["<b>پلن‌های فعال</b>", ""]
    for plan in plans:
        lines.append(
            f"• <b>{plan.name}</b> | {plan.volume_gb} GB | {plan.duration_days} روز | {money(plan.price, plan.currency)}"
        )
    return "\n".join(lines)


def store_payment_lines(store, plan):
    lines = [
        f"<b>پلن انتخابی:</b> {plan.name}",
        f"<b>مبلغ:</b> {money(plan.price, plan.currency)}",
        "",
        "<b>اطلاعات پرداخت کارت به کارت</b>",
        f"<b>شماره کارت:</b> <code>{store.card_number}</code>",
        f"<b>به نام:</b> {store.card_owner}",
    ]
    if store.bank_name:
        lines.append(f"<b>بانک:</b> {store.bank_name}")
    if store.sheba_number:
        lines.append(f"<b>شبا:</b> <code>{store.sheba_number}</code>")
    return "\n".join(lines)


def user_order_summary(order):
    lines = [
        "<b>سفارش شما ثبت شد</b>",
        "",
        f"<b>کد پیگیری:</b> <code>{order.order_tracking_code}</code>",
        f"<b>پلن:</b> {order.plan.name}",
        f"<b>مبلغ:</b> {money(order.amount, order.currency)}",
        "پرداخت شما برای تایید ارسال شد. پس از تایید، کانفیگ همین‌جا ارسال می‌شود.",
    ]
    return "\n".join(lines)


def active_subscription_lines(bot_user):
    clients = (
        VPNClient.objects.select_related("plan", "order", "inbound", "inbound__panel")
        .filter(
            order__customer=bot_user.customer,
            status__in=[VPNClient.Status.ACTIVE, VPNClient.Status.INACTIVE, VPNClient.Status.CREATED],
        )
        .order_by("-created_at")
        if bot_user.customer_id
        else []
    )
    lines = ["<b>اشتراک‌های شما</b>", ""]
    found = False
    for client in clients:
        found = True
        stats = sync_vpn_client_stats(client)
        remaining_gb = round((stats.get("remaining_traffic_bytes", 0) or 0) / (1024 ** 3), 2)
        expires_at = client.expires_at
        expiry_label = timezone.localtime(expires_at).strftime("%Y-%m-%d %H:%M") if expires_at else "-"
        lines.extend(
            [
                f"<b>{client.plan.name if client.plan_id else client.username}</b>",
                f"وضعیت: {client.get_status_display()}",
                f"حجم باقی‌مانده: {remaining_gb} GB",
                f"انقضا: {expiry_label}",
            ]
        )
        if client.status == client.Status.ACTIVE and client.sub_link:
            lines.append(f"لینک اشتراک: {client.sub_link}")
        if client.status == client.Status.ACTIVE and client.direct_link:
            lines.append(f"<code>{client.direct_link}</code>")
        lines.append("")
    if not found:
        lines.append("هنوز اشتراک فعالی برای این حساب پیدا نشد.")
    return "\n".join(lines).strip()


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


def handle_bot_update(provider, webhook_secret, update):
    config = BotConfiguration.objects.filter(
        provider=provider,
        webhook_secret=webhook_secret,
        is_active=True,
    ).first()
    if not config:
        logger.warning("Bot webhook ignored: config not found provider=%s secret=%s", provider, webhook_secret)
        return {"ok": False, "error": "Bot configuration not found."}

    logger.info("Incoming bot webhook provider=%s config=%s payload=%s", provider, config.pk, update)
    log_event(
        config,
        event_type=BotEventLog.EventType.WEBHOOK,
        status=BotEventLog.Status.RECEIVED,
        message=f"Incoming webhook for provider={provider}",
        raw_payload=update,
    )
    maybe_send_due_sales_report(config)

    callback_query = get_callback_update(update)
    message = get_message_update(update)
    user_id = extract_user_id(update)
    chat_id = extract_chat_id(update)
    is_admin = str(user_id) == str(config.admin_user_id)
    logger.info("Bot webhook extraction config=%s user_id=%s chat_id=%s is_admin=%s", config.pk, user_id, chat_id, is_admin)

    if is_admin and callback_query and is_admin_callback_data(get_callback_data(callback_query)):
        return handle_callback_update(config, callback_query, chat_id=chat_id)

    if is_admin and message:
        admin_result = handle_message_update(config, message, chat_id=chat_id)
        if not admin_result.get("ignored"):
            return admin_result

    if not is_admin:
        return handle_user_update(config, update, chat_id=chat_id, user_id=user_id)

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
    if len(parts) == 2 and parts[0] in {"approve", "reject"}:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[0] == "order" and parts[1] in {"approve", "reject"}:
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


def send_main_menu(client, bot_user, *, chat_id):
    bot_user.reset_state()
    client.send_message(
        "سلام! از اینجا می‌توانید پلن‌ها را ببینید، خرید ثبت کنید یا اشتراک فعال خود را بررسی کنید.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(),
    )
    return {"ok": True, "handled": True}


def send_plan_list(client, config, *, chat_id, buy_mode=False):
    store = config.store or get_current_store()
    plans = list(get_store_plans(store, public_only=True))
    if not plans:
        client.send_message("در حال حاضر پلن فعالی برای خرید وجود ندارد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    client.send_message(
        format_plan_lines(plans),
        chat_id=chat_id,
        reply_markup=plan_keyboard(plans),
    )
    return {"ok": True, "handled": True, "buy_mode": buy_mode}


def start_purchase_flow(client, config, bot_user, plan_id, *, chat_id):
    store = config.store or get_current_store()
    plan = get_store_plans(store, public_only=True).filter(pk=plan_id).first()
    if not plan:
        client.send_message("این پلن پیدا نشد یا دیگر فعال نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Plan not found."}

    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = {"plan_id": plan.pk}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        f"{store_payment_lines(store, plan)}\n\nبعد از پرداخت، نام پرداخت‌کننده را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=cancel_keyboard(),
    )
    return {"ok": True, "handled": True}


def handle_user_callback(config, bot_user, callback_query, *, chat_id):
    client = BotClient(config)
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client.answer_callback(callback_id, "Received")

    if data == "user:menu":
        return send_main_menu(client, bot_user, chat_id=chat_id)
    if data == "user:cancel":
        bot_user.reset_state()
        client.send_message("فرایند لغو شد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "cancelled": True}
    if data == "user:plans":
        return send_plan_list(client, config, chat_id=chat_id)
    if data == "user:buy":
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True)
    if data == "user:subs":
        client.send_message(active_subscription_lines(bot_user), chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}
    if data.startswith("user:buyplan:"):
        plan_id = data.rsplit(":", 1)[-1]
        if not plan_id.isdigit():
            client.send_message("شناسه پلن نامعتبر است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        return start_purchase_flow(client, config, bot_user, int(plan_id), chat_id=chat_id)

    return {"ok": True, "ignored": True}


def handle_user_message(config, bot_user, message, *, chat_id):
    client = BotClient(config)
    text = get_message_text(message)
    lowered = text.lower()

    if lowered in {"/start", "start"}:
        return send_main_menu(client, bot_user, chat_id=chat_id)
    if lowered in {"/cancel", "cancel", "لغو", "انصراف"}:
        bot_user.reset_state()
        client.send_message("فرایند لغو شد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "cancelled": True}
    if lowered in {"/plans", "plans"}:
        return send_plan_list(client, config, chat_id=chat_id)
    if lowered in {"/buy", "buy"}:
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True)
    if lowered in {"/subscription", "/subs", "subscription"}:
        client.send_message(active_subscription_lines(bot_user), chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_NAME:
        if not text:
            client.send_message("لطفا نام پرداخت‌کننده را به صورت متن ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        data = bot_user.state_data or {}
        data["sender_card_name"] = text[:100]
        bot_user.state = BotUser.State.BUY_WAIT_LAST4
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message("۴ رقم آخر کارت پرداخت‌کننده را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_LAST4:
        digits = re.sub(r"\D+", "", text)
        if len(digits) != 4:
            client.send_message("لطفا دقیقا ۴ رقم آخر کارت را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        data = bot_user.state_data or {}
        data["sender_card_last4"] = digits
        bot_user.state = BotUser.State.BUY_WAIT_TIME
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message("ساعت پرداخت را با فرمت HH:MM ارسال کنید. مثال: <code>14:35</code>", chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_TIME:
        try:
            parsed_time = parse_payment_time(text)
        except ValidationError:
            client.send_message("فرمت ساعت درست نیست. لطفا مثل <code>14:35</code> ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        data = bot_user.state_data or {}
        data["payment_time"] = parsed_time.strftime("%H:%M")
        bot_user.state = BotUser.State.BUY_WAIT_TRACKING
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message("کد پیگیری بانکی را ارسال کنید. اگر ندارید، یک خط تیره <code>-</code> بفرستید.", chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_TRACKING:
        data = bot_user.state_data or {}
        data["bank_tracking_code"] = "" if text in {"-", "ندارم"} else text[:50]
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            "رسید پرداخت را به صورت عکس یا فایل ارسال کنید. اگر رسید ندارید، <code>/skip</code> را بفرستید.",
            chat_id=chat_id,
            reply_markup=cancel_keyboard(),
        )
        return {"ok": True, "handled": True}

    if bot_user.state == BotUser.State.BUY_WAIT_RECEIPT:
        file_info = extract_receipt_file(message)
        if not file_info and lowered not in {"/skip", "skip", "-", "ندارم"}:
            client.send_message("لطفا عکس/فایل رسید را ارسال کنید یا <code>/skip</code> را بفرستید.", chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "handled": True}
        return finalize_bot_purchase(config, bot_user, message, file_info, chat_id=chat_id)

    if text:
        client.send_message("برای شروع از منوی زیر استفاده کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    return {"ok": True, "ignored": True}


def finalize_bot_purchase(config, bot_user, message, file_info, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    store = config.store or get_current_store()
    plan = get_store_plans(store, public_only=True).filter(pk=data.get("plan_id")).first()
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
    receipt_image = None
    if file_info:
        metadata["receipt"] = {
            "provider": config.provider,
            "kind": file_info.get("kind"),
            "file_id": file_info.get("file_id"),
            "file_unique_id": file_info.get("file_unique_id"),
            "message_id": file_info.get("message_id"),
            "file_name": file_info.get("file_name"),
            "mime_type": file_info.get("mime_type", ""),
        }
        receipt_image = download_receipt_content(client, file_info, metadata)

    result = create_manual_payment_order(
        store=store,
        customer=bot_user.customer,
        plan=plan,
        sender_card_name=data.get("sender_card_name", ""),
        sender_card_last4=data.get("sender_card_last4", ""),
        payment_time=data.get("payment_time", ""),
        receipt_image=receipt_image,
        bank_tracking_code=data.get("bank_tracking_code", ""),
        metadata=metadata,
    )
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    bot_user.reset_state()
    client.send_message(user_order_summary(order), chat_id=chat_id, reply_markup=main_menu_keyboard())
    forward_receipt_to_admin(client, order, file_info, from_chat_id=chat_id)
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def forward_receipt_to_admin(client, order, file_info, *, from_chat_id):
    if not file_info or not file_info.get("message_id"):
        return
    try:
        client.forward_message(from_chat_id=from_chat_id, message_id=file_info["message_id"])
    except BotDeliveryError as exc:
        log_event(
            client.config,
            event_type=BotEventLog.EventType.ERROR,
            status=BotEventLog.Status.FAILED,
            order=order,
            message=f"Could not forward receipt: {exc}",
            raw_payload={"order_id": order.order_tracking_code, "receipt": file_info},
        )


def handle_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    client = BotClient(config)
    client.answer_callback(callback_id, "Received")

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
            admin_user_id=config.admin_user_id,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        BotPendingAction.objects.create(
            bot_config=config,
            order=order,
            admin_user_id=config.admin_user_id,
            action=BotPendingAction.Action.REJECT_ORDER,
        )
        log_callback(
            config,
            status=BotEventLog.Status.SUCCESS,
            order=order,
            message=f"Reject reason requested for order {order.order_tracking_code}; admin_user_id={config.admin_user_id}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        client.send_message(
            f"Please send the rejection reason for order <code>{order.order_tracking_code}</code>.",
            chat_id=chat_id,
        )
        return {"ok": True, "waiting_for_reason": True}

    return {"ok": True, "ignored": True}


def handle_message_update(config, message, *, chat_id):
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True, "ignored": True}

    pending = (
        BotPendingAction.objects.select_related("order", "order__plan", "order__store")
        .filter(
            bot_config=config,
            admin_user_id=config.admin_user_id,
            status=BotPendingAction.Status.PENDING,
            action=BotPendingAction.Action.REJECT_ORDER,
        )
        .order_by("-created_at")
        .first()
    )
    if not pending:
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
