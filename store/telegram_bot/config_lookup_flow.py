import logging
import secrets

from django.core.cache import cache
from django.utils import timezone

from store.config_lookup import (
    ConfigIdentifierMissing,
    InvalidConfigLink,
    check_config_usage,
    config_link_fingerprint,
    mask_identifier,
)
from store.models import Panel
from store.order_services import get_current_store
from store.vpn_client_management_services import find_local_vpn_client_for_lookup
from store.xui_api import build_config_link_for_identifier

from .config_delivery import send_config_links_message
from .constants import CONFIG_MANAGEMENT_CACHE_SECONDS
from .services_flow import bot_user_cache_identity
from .user_menu import main_menu_keyboard

logger = logging.getLogger(__name__)

BOT_STATE_CONFIG_LOOKUP_WAIT_LINK = "config_lookup_wait_link"
CONFIG_LOOKUP_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CACHE_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX = "user:config_lookup_update:"
ADMIN_CONFIG_DELETE_CALLBACK_PREFIX = "admin:config_delete:"
ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX = "admin:config_edit_traffic:"
ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX = "admin:config_edit_expiry:"
ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX = "admin:config_refresh_link:"
CONFIG_LOOKUP_RATE_FALLBACK = {}
CONFIG_LOOKUP_UPDATE_RATE_FALLBACK = {}
CONFIG_LOOKUP_NO_UPDATE_MESSAGE = "این کانفیگ آپدیت ندارد."


def default_is_admin_bot_user(config, bot_user):
    return bool(
        bot_user
        and (
            config.is_admin_user(getattr(bot_user, "provider_user_id", ""))
            or config.is_admin_user(getattr(bot_user, "chat_id", ""))
        )
    )


def config_lookup_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "لغو", "callback_data": "user:cancel"}],
            [{"text": "بازگشت به منو", "callback_data": "user:menu"}],
        ]
    }


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


def admin_config_management_cache_key(config, admin_user_id, token):
    return f"bot:admin-config-management:{config.pk}:{admin_user_id or 'unknown'}:{token}"


def create_admin_config_management_token(config, bot_user, result):
    panel = result.get("panel")
    inbound = result.get("inbound")
    panel_id = result.get("panel_id") or getattr(panel, "pk", None)
    inbound_id = result.get("inbound_id") or getattr(inbound, "inbound_id", None)
    identifier = str(result.get("identifier") or "").strip()
    if not panel_id or not inbound_id or not identifier:
        return ""

    local_match = find_local_vpn_client_for_lookup(panel, inbound, identifier, result=result)
    token = secrets.token_urlsafe(12).rstrip("=")
    payload = {
        "panel_id": panel_id,
        "panel_name": getattr(panel, "name", "") or result.get("panel_name") or "",
        "inbound_id": inbound_id,
        "inbound_remark": getattr(inbound, "remark", "") or result.get("inbound_remark") or "",
        "identifier": identifier,
        "masked_identifier": result.get("masked_identifier") or mask_identifier(identifier),
        "email": result.get("email") or "",
        "protocol": result.get("protocol") or "",
        "enabled": result.get("enabled"),
        "total_bytes": result.get("total_bytes") or result.get("total_traffic_bytes") or 0,
        "used_bytes": result.get("used_bytes") or result.get("used_traffic_bytes"),
        "expiry_time": result.get("expiry_time") or result.get("expiry_at"),
        "vpn_client_id": local_match.vpn_client.pk if local_match.vpn_client else None,
        "local_match_status": local_match.status,
    }
    try:
        cache.set(
            admin_config_management_cache_key(config, bot_user_cache_identity(bot_user), token),
            payload,
            CONFIG_MANAGEMENT_CACHE_SECONDS,
        )
    except Exception as exc:
        logger.warning("Could not cache admin config management token: %s", exc)
        return ""
    return token


def get_admin_config_management_payload(config, admin_user_id, token):
    return cache.get(admin_config_management_cache_key(config, admin_user_id, token))


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


def config_lookup_result_keyboard(
    config,
    bot_user,
    result,
    *,
    original_config_text="",
    is_admin_bot_user_func=default_is_admin_bot_user,
    create_admin_config_management_token_func=create_admin_config_management_token,
    create_config_lookup_update_token_func=create_config_lookup_update_token,
):
    rows = []
    if result.get("found"):
        if is_admin_bot_user_func(config, bot_user):
            token = create_admin_config_management_token_func(config, bot_user, result)
            if token:
                rows.extend(
                    [
                        [
                            {
                                "text": "حذف کانفیگ 🗑",
                                "callback_data": f"{ADMIN_CONFIG_DELETE_CALLBACK_PREFIX}{token}",
                            },
                            {
                                "text": "دریافت/آپدیت لینک 🔄",
                                "callback_data": f"{ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX}{token}",
                            },
                        ],
                        [
                            {
                                "text": "ویرایش حجم 📦",
                                "callback_data": f"{ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX}{token}",
                            },
                            {
                                "text": "ویرایش زمان ⏱",
                                "callback_data": f"{ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX}{token}",
                            },
                        ],
                    ]
                )
        else:
            token = create_config_lookup_update_token_func(
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
            panel_name = str(error.get("panel") or error.get("panel_id") or "-")
            message = str(error.get("error") or "")[:200]
            from html import escape

            lines.append(f"- {escape(panel_name)}: {escape(message)}")
        return f"{text}\n" + "\n".join(lines)
    return f"{text}\n\nبرخی پنل‌ها موقتاً در دسترس نبودند؛ نتیجه بر اساس پنل‌های قابل بررسی است."


def handle_config_lookup_text(
    config,
    bot_user,
    text,
    *,
    chat_id,
    client_cls,
    is_admin_bot_user_func=default_is_admin_bot_user,
    check_config_usage_func=check_config_usage,
    get_current_store_func=get_current_store,
    config_lookup_result_keyboard_func=config_lookup_result_keyboard,
):
    client = client_cls(config)
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
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "rate_limited": True}

    try:
        result = check_config_usage_func(text, store=config.store or get_current_store_func())
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
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "lookup_failed"}

    is_admin = is_admin_bot_user_func(config, bot_user)
    message = append_config_lookup_panel_errors(
        result.get("message", ""),
        result,
        is_admin=is_admin,
    )
    bot_user.reset_state()
    client.send_message(
        message,
        chat_id=chat_id,
        reply_markup=(
            config_lookup_result_keyboard_func(
                config,
                bot_user,
                result,
                original_config_text=text,
                is_admin_bot_user_func=is_admin_bot_user_func,
            )
            if result.get("found")
            else main_menu_keyboard(is_admin=is_admin)
        ),
    )
    return {"ok": True, "success": bool(result.get("found")), "config_lookup": True}


def handle_config_lookup_update_callback(
    config,
    bot_user,
    data,
    *,
    client,
    chat_id,
    is_admin_bot_user_func=default_is_admin_bot_user,
    build_config_link_for_identifier_func=build_config_link_for_identifier,
):
    token = data[len(CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX) :].strip()
    if not token:
        client.send_message(
            "درخواست آپدیت کانفیگ معتبر نیست. دوباره لینک کانفیگ را بررسی کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "invalid_update_token"}

    if config_lookup_update_rate_limited(config, bot_user):
        client.send_message(
            "تعداد درخواست‌های آپدیت کانفیگ زیاد شده. چند دقیقه دیگر دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "rate_limited": True}

    payload = cache.get(config_lookup_update_cache_key(config, bot_user, token))
    if not payload:
        client.send_message(
            "مهلت آپدیت این کانفیگ تمام شده. دوباره لینک کانفیگ را ارسال کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "expired": True}

    panel = Panel.objects.filter(pk=payload.get("panel_id"), is_active=True).first()
    if not panel:
        client.send_message(
            "پنل این کانفیگ در حال حاضر در دسترس نیست. چند دقیقه دیگر تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "panel_unavailable": True}

    try:
        details = build_config_link_for_identifier_func(
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
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "update_failed"}

    updated_link = details.get("updated_config_link") or ""
    if not updated_link:
        client.send_message(
            "لینک به‌روز برای این کانفیگ ساخته نشد. لطفا دوباره تلاش کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "error": "empty_updated_link"}

    if details.get("enabled") is False:
        client.send_message(
            CONFIG_LOOKUP_NO_UPDATE_MESSAGE,
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": True, "no_update": True, "inactive": True}

    original_fingerprint = payload.get("original_config_fingerprint") or ""
    updated_fingerprint = config_link_fingerprint(updated_link)
    if original_fingerprint and updated_fingerprint and original_fingerprint == updated_fingerprint:
        client.send_message(
            CONFIG_LOOKUP_NO_UPDATE_MESSAGE,
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": True, "no_update": True}

    label = details.get("remark") or details.get("email") or "کانفیگ"
    send_config_links_message(
        client,
        chat_id,
        direct_link=updated_link,
        title=f"✅ کانفیگ به‌روز {label}",
    )
    return {"ok": True, "success": True, "config_lookup_update": True}
