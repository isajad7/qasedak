import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from .models import (
    BotConfiguration,
    BotEventLog,
    BotUser,
    Customer,
    Order,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    normalize_payment_digits,
)
from .telegram_bot.client import BotClient, BotDeliveryError


logger = logging.getLogger(__name__)

SUPPORT_WORKBENCH_LIMIT = 20
DIRECT_MESSAGE_MAX_LENGTH = 2000
DUPLICATE_SEND_WINDOW = timedelta(seconds=45)

CONFIG_LINK_PATTERN = re.compile(r"\b(?:vless|vmess|trojan|ss)://[^\s<>'\"]+", re.IGNORECASE)
SUB_LINK_PATTERN = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
BOT_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
PROXY_CREDENTIAL_PATTERN = re.compile(r"\bhttps?://[^/\s:@]+:[^@\s]+@[^\s]+", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?98|0)?9(?:[\s\-]?\d){9}(?!\w)")
LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{28,}\b")

MESSAGE_TEMPLATES = [
    {
        "key": "connection_help",
        "label": "راهنمای اتصال",
        "body": (
            "سلام، برای مشکل اتصال لطفاً یک‌بار اینترنت را عوض کنید، برنامه را کامل ببندید و دوباره باز کنید. "
            "اگر خطا باقی بود نام برنامه، نوع اینترنت و متن خطا را بفرستید تا بررسی کنیم."
        ),
    },
    {
        "key": "payment_review",
        "label": "بررسی پرداخت",
        "body": (
            "سلام، پرداخت شما در حال بررسی است. اگر رسید یا شماره پیگیری جدید دارید همینجا ارسال کنید تا سریع‌تر تطبیق بدهیم."
        ),
    },
    {
        "key": "resend_config",
        "label": "دریافت کانفیگ",
        "body": (
            "سلام، کانفیگ شما از بخش سفارش/سرویس قابل دریافت است. اگر دکمه یا لینک را نمی‌بینید اطلاع بدهید تا دوباره ارسال کنیم."
        ),
    },
    {
        "key": "renewal_help",
        "label": "اطلاع تمدید",
        "body": "سلام، برای تمدید سرویس کافی است از صفحه سرویس‌های من گزینه تمدید را بزنید و رسید پرداخت را ثبت کنید.",
    },
    {
        "key": "support_followup",
        "label": "درخواست اسکرین‌شات",
        "body": "سلام، برای بررسی دقیق‌تر لطفاً اسکرین‌شات خطا و نام برنامه‌ای که با آن وصل می‌شوید را ارسال کنید.",
    },
]


@dataclass
class SupportSendResult:
    ok: bool
    sent_count: int = 0
    support_message: SupportMessage | None = None
    safe_error: str = ""
    duplicate: bool = False


def add_query(url, params=None):
    query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    if not query:
        return url
    return f"{url}?{urlencode(query)}"


def support_workbench_url(section="", store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    url = add_query(reverse("admin_store_support_workbench"), params)
    if section:
        url = f"{url}#{section}"
    return url


def support_review_url(conversation):
    return reverse("admin_store_support_review", args=[conversation.pk])


def customer_message_url(customer):
    return reverse("admin_store_customer_message", args=[customer.pk])


def customer_review_url(customer):
    return reverse("admin_store_customer_review", args=[customer.pk])


def order_review_url(order):
    return reverse("admin_store_order_review", args=[order.pk])


def service_review_url(vpn_client):
    return reverse("admin_store_service_review", args=[vpn_client.pk])


def mask_phone(value):
    cleaned = "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit() or ch == "+")
    if not cleaned:
        return ""
    prefix = cleaned[:4] if cleaned.startswith("+") else cleaned[:3]
    suffix = cleaned[-2:] if len(cleaned) > 5 else ""
    return f"{prefix}***{suffix}" if suffix else "***"


def mask_identifier(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        return mask_email(text)
    if len(text) <= 8:
        return f"{text[:2]}***"
    return f"{text[:4]}...{text[-3:]}"


def mask_email(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return mask_identifier(text)
    local, domain = text.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[:2]}***@{domain}"


def safe_customer_name(customer):
    if not customer:
        return "مشتری ثبت نشده"
    phone = getattr(customer, "phone_number", "") or ""
    name = (getattr(customer, "display_name", "") or getattr(customer, "username", "") or "").strip()
    if phone and normalize_payment_digits(name) == normalize_payment_digits(phone):
        return mask_phone(phone)
    if EMAIL_PATTERN.fullmatch(name):
        return mask_email(name)
    return name or f"Customer {str(customer.public_id)[:8]}"


def sanitize_support_message(text, *, limit=None):
    value = str(text or "")
    if not value:
        return ""

    value = PROXY_CREDENTIAL_PATTERN.sub("<proxy-hidden>", value)
    value = CONFIG_LINK_PATTERN.sub("<config-link-hidden>", value)
    value = SUB_LINK_PATTERN.sub("<subscription-link-hidden>", value)
    value = BOT_TOKEN_PATTERN.sub("<token-hidden>", value)
    value = UUID_PATTERN.sub("<identifier-hidden>", value)
    value = EMAIL_PATTERN.sub(lambda match: mask_email(match.group(0)), value)
    value = PHONE_PATTERN.sub(lambda match: mask_phone(match.group(0)), value)
    value = LONG_TOKEN_PATTERN.sub("<token-hidden>", value)
    if limit and len(value) > limit:
        value = f"{value[:limit].rstrip()}..."
    return value


def safe_log_value(value):
    if isinstance(value, dict):
        return {str(key): safe_log_value_for_key(key, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_log_value(item) for item in value]
    return sanitize_support_message(value, limit=300)


def safe_log_value_for_key(key, value):
    key_text = str(key or "").lower()
    if any(marker in key_text for marker in ("token", "password", "secret", "proxy", "config", "uuid", "chat_id")):
        return mask_identifier(value)
    if any(marker in key_text for marker in ("phone", "email")):
        return sanitize_support_message(value, limit=120)
    return safe_log_value(value)


def telegram_targets_for_customer(customer, store=None, *, limit=5):
    if not customer:
        return []
    queryset = (
        BotUser.objects.select_related("bot_config", "customer")
        .filter(
            customer=customer,
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(chat_id="")
    )
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))
    return list(queryset.order_by("-last_seen_at", "-created_at", "-pk")[:limit])


def customer_services(customer):
    if not customer:
        return VPNClient.objects.none()
    return (
        VPNClient.objects.select_related("store", "order", "plan", "inbound", "inbound__panel")
        .filter(Q(order__customer=customer) | Q(free_trial_requests__customer=customer))
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
        .distinct()
    )


def customer_store(customer, targets=None):
    latest_order = customer.orders.select_related("store").filter(store__isnull=False).order_by("-created_at").first()
    if latest_order and latest_order.store_id:
        return latest_order.store
    for target in targets or []:
        if target.bot_config and target.bot_config.store_id:
            return target.bot_config.store
    return Store.objects.filter(is_active=True).first() or Store.objects.first()


def base_support_queryset(store=None):
    queryset = (
        SupportConversation.objects.select_related("store", "customer")
        .annotate(
            messages_count=Count("messages", distinct=True),
            last_message_at=Max("messages__created_at"),
            telegram_target_count=Count(
                "customer__bot_users",
                filter=Q(
                    customer__bot_users__is_active=True,
                    customer__bot_users__bot_config__is_active=True,
                    customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                )
                & ~Q(customer__bot_users__chat_id=""),
                distinct=True,
            ),
            related_orders_count=Count("customer__orders", distinct=True),
            related_services_count=Count(
                "customer__orders__vpn_clients",
                filter=~Q(customer__orders__vpn_clients__status=VPNClient.Status.DELETED)
                & Q(customer__orders__vpn_clients__deleted_at__isnull=True),
                distinct=True,
            ),
            failed_admin_messages_count=Count(
                "messages",
                filter=Q(
                    messages__sender_type=SupportMessage.SenderType.ADMIN,
                    messages__metadata__delivery_status="failed",
                ),
                distinct=True,
            ),
        )
    )
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset


def status_tone(status):
    return {
        SupportConversation.Status.OPEN: "info",
        SupportConversation.Status.WAITING_ADMIN: "warning",
        SupportConversation.Status.ANSWERED: "success",
        SupportConversation.Status.CLOSED: "secondary",
    }.get(status, "secondary")


def support_row(conversation):
    customer = conversation.customer
    target_count = getattr(conversation, "telegram_target_count", None)
    if target_count is None:
        target_count = len(telegram_targets_for_customer(customer, store=conversation.store))
    related_orders_count = getattr(conversation, "related_orders_count", None)
    if related_orders_count is None:
        related_orders_count = customer.orders.count() if customer else 0
    related_services_count = getattr(conversation, "related_services_count", None)
    if related_services_count is None:
        related_services_count = customer_services(customer).count() if customer else 0
    last_message = conversation.messages.order_by("-created_at", "-pk").only("sender_type", "created_at").first()
    return {
        "conversation": conversation,
        "subject": sanitize_support_message(conversation.subject, limit=80) or "گفتگوی پشتیبانی",
        "review_url": support_review_url(conversation),
        "change_url": reverse("admin:store_supportconversation_change", args=[conversation.pk]),
        "customer": customer,
        "customer_name": safe_customer_name(customer),
        "customer_url": customer_review_url(customer) if customer else "",
        "customer_message_url": customer_message_url(customer) if customer else "",
        "phone_masked": mask_phone(getattr(customer, "phone_number", "")) if customer else "",
        "username_masked": mask_identifier(getattr(customer, "username", "")) if customer else "",
        "telegram_label": "متصل" if target_count else "بدون مقصد تلگرام",
        "telegram_tone": "success" if target_count else "warning",
        "telegram_target_count": target_count,
        "related_orders_count": related_orders_count,
        "related_services_count": related_services_count,
        "status_label": conversation.get_status_display(),
        "status_tone": status_tone(conversation.status),
        "last_message_sender": last_message.get_sender_type_display() if last_message else "-",
        "last_message_at": getattr(conversation, "last_message_at", None) or (last_message.created_at if last_message else None),
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "send_failed": bool(getattr(conversation, "failed_admin_messages_count", 0)),
    }


def support_list(queryset, limit=SUPPORT_WORKBENCH_LIMIT):
    return [support_row(item) for item in queryset.order_by("-updated_at", "-pk")[:limit]]


def support_section(key, title, description, queryset, *, tone="info", limit=SUPPORT_WORKBENCH_LIMIT):
    return {
        "key": key,
        "title": title,
        "description": description,
        "count": queryset.count(),
        "tone": tone,
        "items": support_list(queryset, limit=limit),
    }


def get_support_workbench_context(store=None, limit=SUPPORT_WORKBENCH_LIMIT):
    now = timezone.now()
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    base = base_support_queryset(store)
    open_items = base.exclude(status=SupportConversation.Status.CLOSED)
    needs_reply = base.filter(status=SupportConversation.Status.WAITING_ADMIN)
    today = base.filter(updated_at__gte=today_start, updated_at__lt=today_end)
    problematic = base.filter(
        Q(customer__isnull=True)
        | Q(telegram_target_count=0)
        | Q(failed_admin_messages_count__gt=0)
        | (Q(related_orders_count=0) & Q(related_services_count=0))
    ).distinct()
    recent_closed = base.filter(status=SupportConversation.Status.CLOSED)
    sections = [
        support_section("open", "باز", "گفتگوهایی که هنوز بسته نشده‌اند.", open_items, tone="info", limit=limit),
        support_section("needs-reply", "نیازمند پاسخ", "پیام‌هایی که منتظر پاسخ owner هستند.", needs_reply, tone="warning", limit=limit),
        support_section("today", "امروز", "گفتگوهای ساخته یا بروزرسانی‌شده امروز.", today, tone="primary", limit=limit),
        support_section(
            "problematic",
            "مشکل‌دار",
            "بدون مقصد تلگرام، ارسال ناموفق، یا مشتری/سفارش/سرویس نامشخص.",
            problematic,
            tone="danger",
            limit=limit,
        ),
        support_section("closed", "بسته‌شده‌های اخیر", "آخرین گفتگوهای بسته‌شده.", recent_closed, tone="secondary", limit=limit),
    ]
    return {
        "sections": sections,
        "summary_counts": {
            "open": sections[0]["count"],
            "needs_reply": sections[1]["count"],
            "today": sections[2]["count"],
            "problematic": sections[3]["count"],
            "closed": sections[4]["count"],
        },
        "section_limit": limit,
    }


def get_support_action_items(store=None):
    context = get_support_workbench_context(store=store, limit=1)
    counts = context["summary_counts"]
    items = []
    if counts["needs_reply"]:
        items.append(
            {
                "title": "پیام پشتیبانی بی‌پاسخ",
                "description": f"{counts['needs_reply']:,} گفتگو نیازمند پاسخ است.",
                "tone": "warning",
                "url": support_workbench_url("needs-reply", store),
            }
        )
    if counts["problematic"]:
        items.append(
            {
                "title": "Support مشکل‌دار",
                "description": f"{counts['problematic']:,} گفتگو target یا ارتباط قابل اتکا ندارد.",
                "tone": "danger",
                "url": support_workbench_url("problematic", store),
            }
        )
    return items


def get_review_conversation(support_id):
    return (
        SupportConversation.objects.select_related("store", "customer")
        .prefetch_related("messages", "customer__bot_users__bot_config")
        .get(pk=support_id)
    )


def support_source_label(conversation):
    first_message = conversation.messages.order_by("created_at", "pk").first()
    source = (first_message.metadata or {}).get("source") if first_message else ""
    return {
        "bot_support": "Telegram",
        "web_support": "Web",
        "admin_direct_message": "Admin",
    }.get(source, source or "Web/Admin")


def get_support_review_context(support_id):
    conversation = get_review_conversation(support_id)
    customer = conversation.customer
    targets = telegram_targets_for_customer(customer, store=conversation.store)
    services = customer_services(customer).order_by("-created_at", "-pk")[:10] if customer else []
    orders = customer.orders.select_related("store", "plan").order_by("-created_at", "-pk")[:10] if customer else []
    messages = [
        {
            "message": message,
            "sender_type": message.sender_type,
            "sender_label": message.get_sender_type_display(),
            "sender_tone": {
                SupportMessage.SenderType.CUSTOMER: "warning",
                SupportMessage.SenderType.ADMIN: "success",
                SupportMessage.SenderType.SYSTEM: "secondary",
            }.get(message.sender_type, "secondary"),
            "body": sanitize_support_message(message.body),
            "created_at": message.created_at,
            "delivery_status": (message.metadata or {}).get("delivery_status", ""),
        }
        for message in conversation.messages.order_by("created_at", "pk")
    ]
    last_message_at = messages[-1]["created_at"] if messages else conversation.updated_at
    return {
        "conversation": conversation,
        "customer_summary": {
            "customer": customer,
            "name": safe_customer_name(customer),
            "id": customer.pk if customer else "",
            "phone_masked": mask_phone(getattr(customer, "phone_number", "")) if customer else "",
            "username_masked": mask_identifier(getattr(customer, "username", "")) if customer else "",
            "telegram_label": "متصل" if targets else "بدون مقصد تلگرام",
            "telegram_tone": "success" if targets else "warning",
            "telegram_target_count": len(targets),
            "active_services_count": customer_services(customer).filter(status=VPNClient.Status.ACTIVE).count() if customer else 0,
            "recent_orders_count": customer.orders.count() if customer else 0,
            "review_url": customer_review_url(customer) if customer else "",
            "message_url": customer_message_url(customer) if customer else "",
            "change_url": reverse("admin:store_customer_change", args=[customer.pk]) if customer else "",
        },
        "ticket_summary": {
            "status": conversation.get_status_display(),
            "status_tone": status_tone(conversation.status),
            "subject": sanitize_support_message(conversation.subject, limit=120),
            "created_at": conversation.created_at,
            "last_message_at": last_message_at,
            "last_customer_message_at": conversation.last_customer_message_at,
            "last_admin_message_at": conversation.last_admin_message_at,
            "source": support_source_label(conversation),
        },
        "timeline_messages": messages,
        "related_orders": [
            {
                "order": order,
                "tracking_code": order.order_tracking_code,
                "status": order.get_status_display(),
                "plan": order.plan.name if order.plan_id else "-",
                "created_at": order.created_at,
                "review_url": order_review_url(order),
            }
            for order in orders
        ],
        "related_services": [
            {
                "client": client,
                "label": mask_identifier(client.xui_email or client.username or client.public_id),
                "status": client.get_status_display(),
                "plan": client.plan.name if client.plan_id else "-",
                "review_url": service_review_url(client),
            }
            for client in services
        ],
        "message_templates": MESSAGE_TEMPLATES,
        "workbench_url": support_workbench_url(store=conversation.store),
        "admin_change_url": reverse("admin:store_supportconversation_change", args=[conversation.pk]),
        "actions": {
            "can_reply": bool(customer),
            "can_close": conversation.status != SupportConversation.Status.CLOSED,
            "can_reopen": conversation.status == SupportConversation.Status.CLOSED,
            "can_mark_followup": conversation.status != SupportConversation.Status.WAITING_ADMIN,
            "has_telegram_target": bool(targets),
            "has_note_model": False,
        },
    }


def get_customer_message_context(customer_id):
    customer = Customer.objects.prefetch_related("bot_users__bot_config").get(pk=customer_id)
    targets = telegram_targets_for_customer(customer)
    latest_conversation = customer.support_conversations.order_by("-updated_at", "-pk").first()
    return {
        "customer": customer,
        "customer_summary": {
            "name": safe_customer_name(customer),
            "phone_masked": mask_phone(customer.phone_number),
            "username_masked": mask_identifier(customer.username),
            "telegram_label": "متصل" if targets else "بدون مقصد تلگرام",
            "telegram_tone": "success" if targets else "warning",
            "telegram_target_count": len(targets),
            "review_url": customer_review_url(customer),
            "latest_support_url": support_review_url(latest_conversation) if latest_conversation else "",
        },
        "message_templates": MESSAGE_TEMPLATES,
        "can_send": bool(targets),
    }


def message_hash(text, actor, purpose):
    actor_id = getattr(actor, "pk", "") or ""
    raw = f"{actor_id}:{purpose}:{text.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def recent_duplicate_message(conversation, text, actor, purpose):
    digest = message_hash(text, actor, purpose)
    since = timezone.now() - DUPLICATE_SEND_WINDOW
    return (
        conversation.messages.filter(
            sender_type=SupportMessage.SenderType.ADMIN,
            created_at__gte=since,
            metadata__body_hash=digest,
            metadata__delivery_status="sent",
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def log_support_delivery(bot_config, *, status, message, raw_payload=None):
    return BotEventLog.objects.create(
        bot_config=bot_config,
        event_type=BotEventLog.EventType.SUPPORT_REPLY,
        status=status,
        message=sanitize_support_message(message, limit=300),
        raw_payload=safe_log_value(raw_payload or {}),
    )


def deliver_to_primary_target(customer, text, *, store=None, conversation=None):
    targets = telegram_targets_for_customer(customer, store=store, limit=1)
    if not targets:
        log_support_delivery(
            None,
            status=BotEventLog.Status.SKIPPED,
            message="support reply skipped: no Telegram target",
            raw_payload={
                "customer_id": getattr(customer, "pk", None),
                "conversation_id": getattr(conversation, "pk", None),
                "reason": "no_personal_telegram_target",
            },
        )
        return 0, "برای این مشتری مقصد تلگرام فعال ثبت نشده است."

    sent = 0
    last_error = ""
    target = targets[0]
    try:
        BotClient(target.bot_config).send_message(text, chat_id=target.chat_id, parse_mode=None)
    except BotDeliveryError as exc:
        last_error = sanitize_support_message(exc, limit=300) or "ارسال تلگرام انجام نشد."
        log_support_delivery(
            target.bot_config,
            status=BotEventLog.Status.FAILED,
            message=last_error,
            raw_payload={
                "customer_id": getattr(customer, "pk", None),
                "bot_user_id": target.pk,
                "chat_id": target.chat_id,
                "conversation_id": getattr(conversation, "pk", None),
            },
        )
        logger.warning("Admin support Telegram send failed customer=%s bot_user=%s: %s", customer.pk, target.pk, last_error)
        return 0, last_error

    sent += 1
    log_support_delivery(
        target.bot_config,
        status=BotEventLog.Status.SENT,
        message="support reply sent",
        raw_payload={
            "customer_id": getattr(customer, "pk", None),
            "bot_user_id": target.pk,
            "chat_id": target.chat_id,
            "conversation_id": getattr(conversation, "pk", None),
        },
    )
    return sent, ""


def create_admin_support_message(conversation, text, actor, *, purpose, delivery_status, sent_count=0, error=""):
    return SupportMessage.objects.create(
        conversation=conversation,
        sender_type=SupportMessage.SenderType.ADMIN,
        customer=conversation.customer,
        body=text,
        metadata={
            "source": purpose,
            "actor_id": getattr(actor, "pk", None),
            "actor_username": getattr(actor, "get_username", lambda: "")(),
            "body_hash": message_hash(text, actor, purpose),
            "delivery_status": delivery_status,
            "sent_count": sent_count,
            "safe_error": sanitize_support_message(error, limit=300),
        },
    )


@transaction.atomic
def send_support_reply(support, message, actor):
    conversation = support if isinstance(support, SupportConversation) else get_review_conversation(support)
    text = str(message or "").strip()
    if not text:
        return SupportSendResult(ok=False, safe_error="متن پاسخ خالی است.")
    if len(text) > DIRECT_MESSAGE_MAX_LENGTH:
        return SupportSendResult(ok=False, safe_error="متن پاسخ باید حداکثر ۲۰۰۰ کاراکتر باشد.")
    if not conversation.customer_id:
        return SupportSendResult(ok=False, safe_error="این گفتگو به مشتری مشخص وصل نیست.")

    duplicate = recent_duplicate_message(conversation, text, actor, "support_reply")
    if duplicate:
        return SupportSendResult(ok=True, support_message=duplicate, duplicate=True)

    sent_count, error = deliver_to_primary_target(conversation.customer, text, store=conversation.store, conversation=conversation)
    delivery_status = "sent" if sent_count else ("skipped" if "مقصد تلگرام" in error else "failed")
    support_message = create_admin_support_message(
        conversation,
        text,
        actor,
        purpose="support_reply",
        delivery_status=delivery_status,
        sent_count=sent_count,
        error=error,
    )
    conversation.mark_answered()
    return SupportSendResult(
        ok=bool(sent_count),
        sent_count=sent_count,
        support_message=support_message,
        safe_error=error,
    )


def get_or_create_direct_message_conversation(customer, targets):
    conversation = (
        customer.support_conversations.exclude(status=SupportConversation.Status.CLOSED)
        .order_by("-updated_at", "-pk")
        .first()
    )
    if conversation:
        return conversation
    return SupportConversation.objects.create(
        store=customer_store(customer, targets),
        customer=customer,
        subject="پیام مستقیم ادمین",
        contact_value=customer.phone_number or customer.username,
        status=SupportConversation.Status.ANSWERED,
    )


@transaction.atomic
def send_customer_direct_message(customer, message, actor):
    if not isinstance(customer, Customer):
        customer = Customer.objects.get(pk=customer)
    text = str(message or "").strip()
    if not text:
        return SupportSendResult(ok=False, safe_error="متن پیام خالی است.")
    if len(text) > DIRECT_MESSAGE_MAX_LENGTH:
        return SupportSendResult(ok=False, safe_error="متن پیام باید حداکثر ۲۰۰۰ کاراکتر باشد.")

    targets = telegram_targets_for_customer(customer, limit=1)
    if not targets:
        log_support_delivery(
            None,
            status=BotEventLog.Status.SKIPPED,
            message="customer direct message skipped: no Telegram target",
            raw_payload={"customer_id": customer.pk, "reason": "no_personal_telegram_target"},
        )
        return SupportSendResult(ok=False, safe_error="برای این مشتری مقصد تلگرام فعال ثبت نشده است.")

    conversation = get_or_create_direct_message_conversation(customer, targets)
    duplicate = recent_duplicate_message(conversation, text, actor, "customer_direct_message")
    if duplicate:
        return SupportSendResult(ok=True, support_message=duplicate, duplicate=True)

    sent_count, error = deliver_to_primary_target(customer, text, store=conversation.store, conversation=conversation)
    delivery_status = "sent" if sent_count else "failed"
    support_message = create_admin_support_message(
        conversation,
        text,
        actor,
        purpose="customer_direct_message",
        delivery_status=delivery_status,
        sent_count=sent_count,
        error=error,
    )
    conversation.mark_answered()
    return SupportSendResult(
        ok=bool(sent_count),
        sent_count=sent_count,
        support_message=support_message,
        safe_error=error,
    )
