import logging

from django.core.cache import cache

from .models import BotConfiguration


logger = logging.getLogger(__name__)

CHECK_MEMBERSHIP_CALLBACK = "check_membership"
DEFAULT_JOIN_CHECK_MESSAGE = "برای استفاده از ربات ابتدا عضو کانال شوید."
TELEGRAM_MEMBERSHIP_CACHE_SECONDS = 5 * 60
TELEGRAM_ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}


def telegram_force_join_enabled(config):
    return bool(
        config
        and config.provider == BotConfiguration.Provider.TELEGRAM
        and config.force_telegram_channel_join
    )


def normalize_required_channel_username(username):
    username = str(username or "").strip().lstrip("@")
    return f"@{username}" if username else ""


def get_required_channel_identifier(config):
    channel_id = str(getattr(config, "telegram_required_channel_id", "") or "").strip()
    if channel_id:
        return channel_id
    return normalize_required_channel_username(getattr(config, "telegram_required_channel_username", ""))


def get_required_channel_join_url(config):
    invite_link = str(getattr(config, "telegram_required_channel_invite_link", "") or "").strip()
    if invite_link:
        return invite_link
    username = normalize_required_channel_username(getattr(config, "telegram_required_channel_username", ""))
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    return ""


def membership_cache_key(config, user_id, channel_identifier):
    config_id = getattr(config, "pk", None) or "unsaved"
    return f"telegram-membership:{config_id}:{channel_identifier}:{user_id}"


def telegram_user_id_payload(user_id):
    value = str(user_id or "").strip()
    return int(value) if value.isdigit() else value


def get_required_join_keyboard(config):
    rows = []
    join_url = get_required_channel_join_url(config)
    if join_url:
        rows.append([{"text": "عضویت در کانال", "url": join_url}])
    rows.append([{"text": "بررسی عضویت", "callback_data": CHECK_MEMBERSHIP_CALLBACK}])
    return {"inline_keyboard": rows}


def is_user_member_of_required_channel(config, user_id, *, client=None, use_cache=True, bypass_admin=True):
    if not telegram_force_join_enabled(config):
        return True

    user_id = str(user_id or "").strip()
    if not user_id:
        return False

    if bypass_admin and config.is_admin_user(user_id):
        return True

    channel_identifier = get_required_channel_identifier(config)
    if not channel_identifier:
        logger.warning("Telegram force join is enabled but no channel identifier is configured config=%s", config.pk)
        return False

    cache_key = membership_cache_key(config, user_id, channel_identifier)
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)

    if client is None:
        from .bots import BotClient

        client = BotClient(config)

    try:
        response = client.call(
            "getChatMember",
            {
                "chat_id": channel_identifier,
                "user_id": telegram_user_id_payload(user_id),
            },
        )
    except Exception as exc:
        logger.warning(
            "Telegram membership check failed config=%s channel=%s user=%s error=%s",
            config.pk,
            channel_identifier,
            user_id,
            exc,
        )
        return False

    result = response.get("result") if isinstance(response, dict) else {}
    status = str((result or {}).get("status") or "").lower()
    is_member = status in TELEGRAM_ALLOWED_MEMBER_STATUSES
    cache.set(cache_key, is_member, TELEGRAM_MEMBERSHIP_CACHE_SECONDS)
    return is_member


def ensure_telegram_membership(config, bot_user, *, client=None, chat_id=None, force_refresh=False):
    user_id = getattr(bot_user, "provider_user_id", "") or chat_id
    if is_user_member_of_required_channel(config, user_id, client=client, use_cache=not force_refresh):
        return True

    if client is None:
        from .bots import BotClient

        client = BotClient(config)

    message = str(getattr(config, "telegram_join_check_message", "") or "").strip() or DEFAULT_JOIN_CHECK_MESSAGE
    try:
        client.send_message(
            message,
            chat_id=chat_id or getattr(bot_user, "chat_id", None),
            reply_markup=get_required_join_keyboard(config),
        )
    except Exception as exc:
        logger.warning("Could not send Telegram join prompt config=%s user=%s error=%s", config.pk, user_id, exc)
    return False
