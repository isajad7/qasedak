from dataclasses import dataclass
from itertools import chain
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils.crypto import constant_time_compare

from .models import BotConfiguration, Store, normalize_telegram_bot_username


DEFAULT_PAYMENT_SMS_TIME_ZONE = "Asia/Tehran"


@dataclass(frozen=True)
class ConfigurationValue:
    value: str
    source: str
    instance: object = None


@dataclass(frozen=True)
class SMSWebhookTokenStatus:
    store: Store | None
    has_legacy_settings_token: bool

    @property
    def has_db_token(self):
        return self.store is not None

    @property
    def is_configured(self):
        return self.has_db_token or self.has_legacy_settings_token


def _legacy_setting(name):
    return str(getattr(settings, name, "") or "").strip()


def get_active_bot_configuration(*, provider=BotConfiguration.Provider.TELEGRAM, store=None):
    queryset = BotConfiguration.objects.filter(provider=provider, is_active=True)
    store_id = getattr(store, "pk", store) if store is not None else None
    if store_id:
        store_config = queryset.filter(store_id=store_id).order_by("pk").first()
        if store_config:
            return store_config
        global_config = queryset.filter(store__isnull=True).order_by("pk").first()
        if global_config:
            return global_config
    return queryset.order_by("pk").first()


def _first_configured_telegram_username(queryset):
    config = queryset.exclude(telegram_bot_username="").order_by("pk").first()
    username = normalize_telegram_bot_username(getattr(config, "telegram_bot_username", ""))
    if username:
        return ConfigurationValue(username, "bot_configuration", config)
    return ConfigurationValue("", "missing")


def get_telegram_bot_username_source(bot_config=None, *, store=None):
    if bot_config and bot_config.provider == BotConfiguration.Provider.TELEGRAM:
        username = normalize_telegram_bot_username(bot_config.telegram_bot_username)
        if username:
            return ConfigurationValue(username, "bot_configuration", bot_config)

    store_id = getattr(store, "pk", store) if store is not None else None
    if not store_id and bot_config:
        store_id = bot_config.store_id

    active_configs = BotConfiguration.objects.filter(
        provider=BotConfiguration.Provider.TELEGRAM,
        is_active=True,
    )
    if store_id:
        result = _first_configured_telegram_username(active_configs.filter(store_id=store_id))
        if result.value:
            return result
        result = _first_configured_telegram_username(active_configs.filter(store__isnull=True))
        if result.value:
            return result

        legacy_username = normalize_telegram_bot_username(
            _legacy_setting("TELEGRAM_BOT_USERNAME") or _legacy_setting("BOT_USERNAME")
        )
        if legacy_username:
            return ConfigurationValue(legacy_username, "settings")
        return ConfigurationValue("", "missing")

    if bot_config is not None:
        result = _first_configured_telegram_username(active_configs.filter(store__isnull=True))
        if result.value:
            return result
        legacy_username = normalize_telegram_bot_username(
            _legacy_setting("TELEGRAM_BOT_USERNAME") or _legacy_setting("BOT_USERNAME")
        )
        if legacy_username:
            return ConfigurationValue(legacy_username, "settings")
        return ConfigurationValue("", "missing")

    result = _first_configured_telegram_username(active_configs)
    if result.value:
        return result

    legacy_username = normalize_telegram_bot_username(
        _legacy_setting("TELEGRAM_BOT_USERNAME") or _legacy_setting("BOT_USERNAME")
    )
    if legacy_username:
        return ConfigurationValue(legacy_username, "settings")

    return ConfigurationValue("", "missing")


def get_telegram_bot_username(bot_config=None, *, store=None):
    return get_telegram_bot_username_source(bot_config=bot_config, store=store).value


def _stores_with_smsforwarder_token_hash():
    active_stores = Store.objects.filter(is_active=True).exclude(
        smsforwarder_webhook_token_hash="",
    ).order_by("pk")
    inactive_stores = Store.objects.filter(is_active=False).exclude(
        smsforwarder_webhook_token_hash="",
    ).order_by("pk")
    return chain(active_stores, inactive_stores)


def get_sms_webhook_token_hash_or_config():
    store = next(_stores_with_smsforwarder_token_hash(), None)
    if not store:
        return "", None
    return store.smsforwarder_webhook_token_hash, store


def get_smsforwarder_webhook_token_status():
    _, store = get_sms_webhook_token_hash_or_config()
    return SMSWebhookTokenStatus(
        store=store,
        has_legacy_settings_token=bool(_legacy_setting("SMSFORWARDER_WEBHOOK_TOKEN")),
    )


def is_smsforwarder_webhook_token_configured():
    return get_smsforwarder_webhook_token_status().is_configured


def verify_smsforwarder_webhook_token(raw_token):
    token = str(raw_token or "").strip()
    if not token:
        return False

    for store in _stores_with_smsforwarder_token_hash():
        if check_password(token, store.smsforwarder_webhook_token_hash):
            return True

    legacy_token = _legacy_setting("SMSFORWARDER_WEBHOOK_TOKEN")
    return bool(legacy_token and constant_time_compare(token, legacy_token))


def _valid_timezone_name(value):
    name = str(value or "").strip()
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ""
    return name


def get_payment_sms_timezone_name_source(*, store=None):
    store_timezone = _valid_timezone_name(getattr(store, "payment_sms_time_zone", "")) if store else ""
    if store_timezone:
        return ConfigurationValue(store_timezone, "store", store)

    stores = Store.objects.filter(payment_sms_time_zone__gt="").order_by("-is_active", "pk")
    for candidate in stores:
        timezone_name = _valid_timezone_name(candidate.payment_sms_time_zone)
        if timezone_name:
            return ConfigurationValue(timezone_name, "store", candidate)

    legacy_timezone = _valid_timezone_name(_legacy_setting("PAYMENT_SMS_TIME_ZONE"))
    if legacy_timezone:
        return ConfigurationValue(legacy_timezone, "settings")

    return ConfigurationValue(DEFAULT_PAYMENT_SMS_TIME_ZONE, "default")


def get_payment_sms_timezone_name(*, store=None):
    return get_payment_sms_timezone_name_source(store=store).value


def get_payment_sms_timezone(*, store=None):
    timezone_name = get_payment_sms_timezone_name(store=store)
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_PAYMENT_SMS_TIME_ZONE)
