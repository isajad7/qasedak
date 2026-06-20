import re

from store.config_lookup import mask_identifier


CONFIG_LOOKUP_LINK_LOG_RE = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
CONFIG_SUBSCRIPTION_LINK_LOG_RE = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)
WEB_TELEGRAM_LINK_TOKEN_LOG_RE = re.compile(r"(?i)(link_)[^\s<>'\"]+")
EMAIL_LOG_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CONFIG_LOOKUP_UUID_LOG_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
RECEIPT_FILE_LOG_KEYS = {"fileid", "fileuniqueid", "filepath"}
SECRET_LOG_KEYS = {
    "apikey",
    "apisecret",
    "bottoken",
    "key",
    "password",
    "privatekey",
    "proxypassword",
    "publickey",
    "secret",
    "sessionkey",
    "token",
    "webhooksecret",
}


def sanitize_bot_text_for_logging(value):
    if not isinstance(value, str):
        return value
    sanitized = CONFIG_LOOKUP_LINK_LOG_RE.sub("<config-link-redacted>", value)
    sanitized = CONFIG_SUBSCRIPTION_LINK_LOG_RE.sub("<config-link-redacted>", sanitized)
    sanitized = WEB_TELEGRAM_LINK_TOKEN_LOG_RE.sub(r"\1<redacted>", sanitized)
    sanitized = EMAIL_LOG_RE.sub("<email-redacted>", sanitized)
    return CONFIG_LOOKUP_UUID_LOG_RE.sub(
        lambda match: f"<identifier:{mask_identifier(match.group(0))}>",
        sanitized,
    )


def _normalized_log_key(key):
    return re.sub(r"[^a-z0-9]+", "", str(key or "").casefold())


def _is_secret_log_key(normalized_key):
    if normalized_key.startswith("link") and normalized_key != "linktoken":
        return False
    return (
        normalized_key in SECRET_LOG_KEYS
        or "token" in normalized_key
        or "password" in normalized_key
        or normalized_key.endswith("secret")
    )


def _sanitize_mapping_item(key, item, *, update_payload=False):
    safe_key = sanitize_bot_text_for_logging(key)
    normalized_key = _normalized_log_key(key)
    if normalized_key in RECEIPT_FILE_LOG_KEYS:
        return safe_key, "<receipt-file-redacted>"
    if _is_secret_log_key(normalized_key):
        return safe_key, "<redacted-token>"
    sanitizer = sanitize_bot_update_for_logging if update_payload else sanitize_bot_event_log_value
    return safe_key, sanitizer(item)


def sanitize_bot_update_for_logging(value):
    if isinstance(value, str):
        return sanitize_bot_text_for_logging(value)
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in {"text", "caption"}:
                safe_key = sanitize_bot_text_for_logging(key)
                sanitized[safe_key] = sanitize_bot_text_for_logging(item)
            else:
                safe_key, safe_item = _sanitize_mapping_item(key, item, update_payload=True)
                sanitized[safe_key] = safe_item
        return sanitized
    if isinstance(value, list):
        return [sanitize_bot_update_for_logging(item) for item in value]
    return value


def sanitize_bot_event_log_value(value):
    if isinstance(value, str):
        return sanitize_bot_text_for_logging(value)
    if isinstance(value, dict):
        return dict(_sanitize_mapping_item(key, item) for key, item in value.items())
    if isinstance(value, list):
        return [sanitize_bot_event_log_value(item) for item in value]
    return value
