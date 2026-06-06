from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from .models import BotConfiguration, BotUser, FreeTrialRequest


@dataclass(frozen=True)
class TelegramTarget:
    chat_id: str
    telegram_user_id: str = ""
    bot_config: BotConfiguration | None = None
    source: str = ""


def _clean_target_id(value):
    return str(value or "").strip()


def _default_telegram_bot_config(*, store=None):
    queryset = (
        BotConfiguration.objects.filter(
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        )
        .exclude(bot_token="")
        .order_by("pk")
    )
    if store:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.first()


def _append_unique(targets, seen, target):
    key = (target.chat_id, getattr(target.bot_config, "pk", None), target.source)
    if not target.chat_id or key in seen:
        return
    seen.add(key)
    targets.append(target)


def get_customer_telegram_targets(customer, bot_config=None, *, store=None):
    if not customer:
        return []

    targets = []
    seen = set()
    direct_config = bot_config or _default_telegram_bot_config(store=store)
    for field_name in ("telegram_id", "telegram_chat_id", "chat_id"):
        value = _clean_target_id(getattr(customer, field_name, ""))
        if value:
            _append_unique(
                targets,
                seen,
                TelegramTarget(
                    chat_id=value,
                    telegram_user_id=value,
                    bot_config=direct_config,
                    source=f"customer.{field_name}",
                ),
            )

    bot_users = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=customer,
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(chat_id="")
        .exclude(bot_config__bot_token="")
    )
    if bot_config:
        bot_users = bot_users.filter(bot_config=bot_config)
    elif store:
        bot_users = bot_users.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))

    for bot_user in bot_users.order_by("-last_seen_at", "-updated_at", "pk"):
        _append_unique(
            targets,
            seen,
            TelegramTarget(
                chat_id=_clean_target_id(bot_user.chat_id),
                telegram_user_id=_clean_target_id(bot_user.provider_user_id),
                bot_config=bot_user.bot_config,
                source="bot_user",
            ),
        )

    return targets


def get_primary_customer_telegram_target(customer, bot_config=None, *, store=None):
    targets = get_customer_telegram_targets(customer, bot_config=bot_config, store=store)
    return targets[0] if targets else None


def customer_has_telegram_target(customer, *, store=None):
    return bool(get_primary_customer_telegram_target(customer, store=store))


def _free_trial_request_for_client(vpn_client):
    if not vpn_client or not getattr(vpn_client, "pk", None):
        return None
    return (
        FreeTrialRequest.objects.select_related("customer")
        .filter(vpn_client=vpn_client)
        .order_by("-created_at", "-pk")
        .first()
    )


def _customer_for_vpn_client(vpn_client):
    order = getattr(vpn_client, "order", None)
    if order and getattr(order, "customer", None):
        return order.customer
    trial_request = _free_trial_request_for_client(vpn_client)
    return getattr(trial_request, "customer", None)


def _trial_request_telegram_targets(vpn_client, *, store=None):
    trial_request = _free_trial_request_for_client(vpn_client)
    telegram_user_id = _clean_target_id(getattr(trial_request, "telegram_user_id", ""))
    if not telegram_user_id:
        return []

    bot_users = (
        BotUser.objects.select_related("bot_config")
        .filter(
            provider_user_id=telegram_user_id,
            is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
            bot_config__is_active=True,
        )
        .exclude(chat_id="")
        .exclude(bot_config__bot_token="")
    )
    if store:
        bot_users = bot_users.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))

    return [
        TelegramTarget(
            chat_id=_clean_target_id(bot_user.chat_id),
            telegram_user_id=_clean_target_id(bot_user.provider_user_id),
            bot_config=bot_user.bot_config,
            source="free_trial_request.bot_user",
        )
        for bot_user in bot_users.order_by("-last_seen_at", "-updated_at", "pk")
    ]


def get_vpn_client_telegram_targets(vpn_client, *, store=None):
    customer = _customer_for_vpn_client(vpn_client)
    targets = get_customer_telegram_targets(customer, store=store)
    if targets:
        return targets
    return _trial_request_telegram_targets(vpn_client, store=store)
