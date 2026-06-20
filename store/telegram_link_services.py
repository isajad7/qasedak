from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import logging
import secrets
from datetime import timedelta
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from .bot_targets import get_customer_telegram_targets
from .configuration_services import get_telegram_bot_username_source
from .models import BotUser, Customer, WebTelegramLinkToken

logger = logging.getLogger(__name__)

WEB_TELEGRAM_LINK_TOKEN_BYTES = 32
WEB_TELEGRAM_LINK_TOKEN_TTL_DAYS = 7
BOT_USERNAME_MISSING_MESSAGE = "نام کاربری ربات تنظیم نشده است."
WEB_TELEGRAM_LINK_SUCCESS_MESSAGE = (
    "✅ حساب شما به ربات وصل شد\n\n"
    "از این به بعد می‌توانید سرویس‌ها، تمدید، یادآوری‌ها و پشتیبانی را از داخل ربات مدیریت کنید."
)
WEB_TELEGRAM_ALREADY_LINKED_MESSAGE = "حساب شما قبلاً به ربات وصل شده است."
WEB_TELEGRAM_CONFLICT_MESSAGE = (
    "این حساب تلگرام قبلاً به یک حساب دیگر وصل شده است. برای تغییر اتصال با پشتیبانی تماس بگیرید."
)
WEB_TELEGRAM_INVALID_MESSAGE = "لینک اتصال نامعتبر یا منقضی شده است. لطفاً از داشبورد سایت لینک جدید بگیرید."


@dataclass(frozen=True)
class WebTelegramLink:
    telegram_link: str = ""
    raw_token: str = ""
    token: WebTelegramLinkToken | None = None
    bot_username: str = ""
    missing_username_message: str = ""

    @property
    def is_configured(self):
        return bool(self.bot_username)


@dataclass(frozen=True)
class TelegramLinkStatus:
    is_linked: bool
    bot_users_count: int
    latest_token_status: str = ""
    bot_username: str = ""
    bot_url: str = ""
    missing_username_message: str = ""


@dataclass(frozen=True)
class TelegramLinkResult:
    success: bool
    code: str
    message: str
    customer: Customer | None = None
    token: WebTelegramLinkToken | None = None


def hash_web_telegram_link_token(raw_token):
    return sha256(str(raw_token or "").encode("utf-8")).hexdigest()


def _clean_source(source):
    source = str(source or WebTelegramLinkToken.Source.DASHBOARD).strip()
    if source in WebTelegramLinkToken.Source.values:
        return source
    return WebTelegramLinkToken.Source.DASHBOARD


def _new_raw_token():
    return secrets.token_urlsafe(WEB_TELEGRAM_LINK_TOKEN_BYTES)


def _db_telegram_bot_username(bot_config=None, *, store=None):
    username_source = get_telegram_bot_username_source(bot_config=bot_config, store=store)
    if username_source.source != "bot_configuration":
        return ""
    return username_source.value.strip().lstrip("@")


def revoke_old_active_link_tokens(customer):
    if not customer:
        return 0
    now = timezone.now()
    return WebTelegramLinkToken.objects.filter(
        customer=customer,
        status=WebTelegramLinkToken.Status.ACTIVE,
    ).update(
        status=WebTelegramLinkToken.Status.REVOKED,
        revoked_at=now,
        updated_at=now,
    )


def create_web_telegram_link_token(customer, source="dashboard", *, metadata=None):
    if not customer or not getattr(customer, "pk", None):
        raise ValueError("A saved customer is required.")

    expires_at = timezone.now() + timedelta(days=WEB_TELEGRAM_LINK_TOKEN_TTL_DAYS)
    source = _clean_source(source)
    metadata = dict(metadata or {})

    with transaction.atomic():
        customer = Customer.objects.select_for_update().get(pk=customer.pk)
        revoke_old_active_link_tokens(customer)
        for _ in range(5):
            raw_token = _new_raw_token()
            token_hash = hash_web_telegram_link_token(raw_token)
            if not WebTelegramLinkToken.objects.filter(token_hash=token_hash).exists():
                token = WebTelegramLinkToken.objects.create(
                    customer=customer,
                    token_hash=token_hash,
                    status=WebTelegramLinkToken.Status.ACTIVE,
                    expires_at=expires_at,
                    source=source,
                    metadata=metadata,
                )
                return raw_token, token
    raise RuntimeError("Could not create a unique Telegram link token.")


def generate_web_telegram_link(customer, source="dashboard", *, bot_config=None, store=None, metadata=None):
    username = _db_telegram_bot_username(bot_config=bot_config, store=store)
    if not username:
        return WebTelegramLink(missing_username_message=BOT_USERNAME_MISSING_MESSAGE)

    raw_token, token = create_web_telegram_link_token(customer, source=source, metadata=metadata)
    return WebTelegramLink(
        telegram_link=f"https://t.me/{quote(username)}?start=link_{quote(raw_token, safe='')}",
        raw_token=raw_token,
        token=token,
        bot_username=username,
    )


def validate_web_telegram_link_token(raw_token):
    token_hash = hash_web_telegram_link_token(raw_token)
    token = (
        WebTelegramLinkToken.objects.select_related("customer", "bot_user")
        .filter(token_hash=token_hash)
        .first()
    )
    if not token:
        return None
    if token.status != WebTelegramLinkToken.Status.ACTIVE:
        return None
    if token.expires_at <= timezone.now():
        WebTelegramLinkToken.objects.filter(
            pk=token.pk,
            status=WebTelegramLinkToken.Status.ACTIVE,
        ).update(
            status=WebTelegramLinkToken.Status.EXPIRED,
            updated_at=timezone.now(),
        )
        token.status = WebTelegramLinkToken.Status.EXPIRED
        return None
    return token


def _mark_token_used(token, bot_user, now):
    token.status = WebTelegramLinkToken.Status.USED
    token.bot_user = bot_user
    token.used_at = now
    token.save(update_fields=["status", "bot_user", "used_at", "updated_at"])


def link_bot_user_to_customer(raw_token, bot_user, telegram_user_id=None):
    if not bot_user or not getattr(bot_user, "pk", None):
        return TelegramLinkResult(False, "invalid_bot_user", WEB_TELEGRAM_INVALID_MESSAGE)

    now = timezone.now()
    token_hash = hash_web_telegram_link_token(raw_token)
    with transaction.atomic():
        token = (
            WebTelegramLinkToken.objects.select_for_update()
            .select_related("customer", "bot_user")
            .filter(token_hash=token_hash)
            .first()
        )
        if not token:
            return TelegramLinkResult(False, "invalid_token", WEB_TELEGRAM_INVALID_MESSAGE, token=token)

        bot_user = BotUser.objects.select_for_update().select_related("customer").get(pk=bot_user.pk)
        if token.status == WebTelegramLinkToken.Status.USED:
            if token.bot_user_id == bot_user.pk and bot_user.customer_id == token.customer_id:
                return TelegramLinkResult(True, "already_linked", WEB_TELEGRAM_ALREADY_LINKED_MESSAGE, token.customer, token)
            return TelegramLinkResult(False, "invalid_token", WEB_TELEGRAM_INVALID_MESSAGE, token=token)
        if token.status != WebTelegramLinkToken.Status.ACTIVE:
            return TelegramLinkResult(False, "invalid_token", WEB_TELEGRAM_INVALID_MESSAGE, token=token)
        if token.expires_at <= now:
            token.status = WebTelegramLinkToken.Status.EXPIRED
            token.save(update_fields=["status", "updated_at"])
            return TelegramLinkResult(False, "expired_token", WEB_TELEGRAM_INVALID_MESSAGE, token=token)

        customer = token.customer
        if bot_user.customer_id == customer.pk:
            _mark_token_used(token, bot_user, now)
            return TelegramLinkResult(True, "already_linked", WEB_TELEGRAM_ALREADY_LINKED_MESSAGE, customer, token)

        if bot_user.customer_id and bot_user.customer_id != customer.pk:
            logger.warning(
                "Telegram web link conflict bot_user=%s existing_customer=%s target_customer=%s telegram_user_id=%s",
                bot_user.pk,
                bot_user.customer_id,
                customer.pk,
                telegram_user_id or bot_user.provider_user_id,
            )
            return TelegramLinkResult(False, "bot_user_customer_conflict", WEB_TELEGRAM_CONFLICT_MESSAGE, customer, token)

        bot_user.customer = customer
        bot_user.is_active = True
        bot_user.save(update_fields=["customer", "is_active", "updated_at"])
        _mark_token_used(token, bot_user, now)
        return TelegramLinkResult(True, "linked", build_telegram_linking_message(customer), customer, token)


def get_customer_telegram_link_status(customer, *, store=None):
    if not customer:
        return TelegramLinkStatus(
            is_linked=False,
            bot_users_count=0,
            missing_username_message=BOT_USERNAME_MISSING_MESSAGE,
        )

    targets = get_customer_telegram_targets(customer, store=store)
    bot_username = _db_telegram_bot_username(store=store)
    latest_token = customer.web_telegram_link_tokens.order_by("-created_at").first()
    return TelegramLinkStatus(
        is_linked=bool(targets),
        bot_users_count=customer.bot_users.filter(is_active=True).count(),
        latest_token_status=getattr(latest_token, "status", "") or "",
        bot_username=bot_username,
        bot_url=f"https://t.me/{quote(bot_username)}" if bot_username else "",
        missing_username_message="" if bot_username else BOT_USERNAME_MISSING_MESSAGE,
    )


def build_telegram_linking_message(customer):
    return WEB_TELEGRAM_LINK_SUCCESS_MESSAGE
