import re

from store.models import BotConfiguration
from store.telegram_bot.client import BotClient, BotDeliveryError


VALID = "valid"
CHAT_NOT_FOUND = "chat_not_found"
BOT_BLOCKED = "bot_blocked"
INVALID_CHAT_ID = "invalid_chat_id"
TIMEOUT = "timeout"
API_ERROR = "api_error"
MISSING_TARGET = "missing_target"


CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _result(ok, reason, safe_error=""):
    return {
        "ok": bool(ok),
        "reason": reason,
        "safe_error": str(safe_error or "")[:160],
    }


def _safe_error(message, *, chat_id="", token=""):
    text = str(message or "")
    for sensitive in (str(token or ""), str(chat_id or "")):
        if sensitive:
            text = text.replace(sensitive, "<redacted>")
    return text[:160]


def _valid_chat_id_shape(chat_id):
    value = str(chat_id or "").strip()
    if not value:
        return False
    if len(value) > 128:
        return False
    return CONTROL_CHAR_RE.search(value) is None


def classify_telegram_target_error(message):
    lowered = str(message or "").casefold()
    if "chat not found" in lowered:
        return CHAT_NOT_FOUND
    if "bot was blocked" in lowered or "blocked by the user" in lowered:
        return BOT_BLOCKED
    if "forbidden" in lowered:
        return BOT_BLOCKED
    if "timed out" in lowered or "timeout" in lowered:
        return TIMEOUT
    if "chat_id" in lowered and ("empty" in lowered or "invalid" in lowered):
        return INVALID_CHAT_ID
    if "bad request" in lowered and "chat" in lowered and "not found" not in lowered:
        return INVALID_CHAT_ID
    return API_ERROR


def validate_telegram_target(bot_user=None, chat_id=None, *, bot_config=None, timeout=10):
    target_chat_id = str(chat_id or getattr(bot_user, "chat_id", "") or "").strip()
    config = bot_config or getattr(bot_user, "bot_config", None)

    if not target_chat_id or not config:
        return _result(False, MISSING_TARGET)
    if not _valid_chat_id_shape(target_chat_id):
        return _result(False, INVALID_CHAT_ID)
    if (
        not getattr(config, "is_active", False)
        or getattr(config, "provider", "") != BotConfiguration.Provider.TELEGRAM
        or not getattr(config, "bot_token", "")
    ):
        return _result(False, MISSING_TARGET)

    try:
        BotClient(config).get_chat(target_chat_id, timeout=timeout)
    except BotDeliveryError as exc:
        safe_error = _safe_error(exc, chat_id=target_chat_id, token=getattr(config, "bot_token", ""))
        return _result(False, classify_telegram_target_error(safe_error), safe_error)
    except Exception as exc:
        safe_error = _safe_error(exc, chat_id=target_chat_id, token=getattr(config, "bot_token", ""))
        return _result(False, classify_telegram_target_error(safe_error), safe_error)

    return _result(True, VALID)
