import re
import unicodedata
import uuid


MAX_XUI_CLIENT_EMAIL_LENGTH = 40
XUI_CLIENT_UUID_SUFFIX_LENGTH = 8
CLIENT_EMAIL_PREFIX_MAX_LENGTH = MAX_XUI_CLIENT_EMAIL_LENGTH - XUI_CLIENT_UUID_SUFFIX_LENGTH - 1
DEFAULT_SHORT_ID_LENGTH = 8

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_PERSIAN_CHAR_TRANSLATION = str.maketrans(
    {
        "ك": "ک",
        "ي": "ی",
        "ى": "ی",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "‌": "_",
    }
)
_PLACEHOLDER_NAMES = {
    "رسید_تصویری",
    "رسيد_تصويری",
    "رسيد_تصويري",
    "receipt_image",
    "image_receipt",
}


def _normalize_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).translate(_DIGIT_TRANSLATION).translate(_PERSIAN_CHAR_TRANSLATION)


def clean_client_name(value, *, max_length=24, fallback="user"):
    text = _normalize_text(value).casefold().strip()
    cleaned = []
    previous_separator = False
    for char in text:
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            cleaned.append(char)
            previous_separator = False
            continue
        if char in {"_", "-", ".", " "} or category.startswith(("P", "S", "Z")):
            if not previous_separator:
                cleaned.append("_")
                previous_separator = True

    result = re.sub(r"_+", "_", "".join(cleaned)).strip("_")
    if max_length and len(result) > max_length:
        result = result[:max_length].strip("_")
    return result or fallback


def _clean_optional_name(value, *, max_length=24):
    cleaned = clean_client_name(value, max_length=max_length, fallback="")
    if cleaned in _PLACEHOLDER_NAMES:
        return ""
    return cleaned


def _looks_like_default_customer_name(customer, value):
    value = str(value or "").strip()
    if not value:
        return True
    public_id = str(getattr(customer, "public_id", "") or "")
    default_public = f"Customer {public_id[:8]}".strip()
    default_pk = f"Customer {getattr(customer, 'pk', '')}".strip()
    return value.casefold() in {default_public.casefold(), default_pk.casefold()}


def _metadata_bot_username(metadata):
    bot_metadata = (metadata or {}).get("bot") if isinstance(metadata, dict) else {}
    if isinstance(bot_metadata, dict):
        return bot_metadata.get("username") or ""
    return ""


def _customer_identifier(customer):
    if not customer:
        return ""
    if getattr(customer, "pk", None):
        return f"customer_{customer.pk}"
    public_id = str(getattr(customer, "public_id", "") or "")
    if public_id:
        return f"customer_{public_id[:8]}"
    return ""


def _name_candidates(customer=None, order=None, *, preferred_name="", metadata=None):
    if preferred_name:
        yield preferred_name
    if order is not None:
        yield getattr(order, "sender_card_name", "")
    if customer is None and order is not None:
        customer = getattr(order, "customer", None)
    if customer is not None:
        display_name = getattr(customer, "display_name", "")
        if not _looks_like_default_customer_name(customer, display_name):
            yield display_name
        yield getattr(customer, "username", "")
    yield _metadata_bot_username(metadata or (getattr(order, "metadata", None) if order is not None else None))
    if customer is not None:
        yield getattr(customer, "phone_number", "")
        yield _customer_identifier(customer)


def _short_identifier(value="", *, customer=None, order=None, length=DEFAULT_SHORT_ID_LENGTH):
    candidates = [
        value,
        getattr(order, "order_tracking_code", "") if order is not None else "",
        getattr(order, "pk", "") if order is not None else "",
        getattr(order, "uuid", "") if order is not None else "",
        getattr(customer, "pk", "") if customer is not None else "",
        str(getattr(customer, "public_id", "") or "")[:length] if customer is not None else "",
        uuid.uuid4().hex,
    ]
    for candidate in candidates:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(candidate or "")).lower()
        if cleaned:
            return cleaned[:length]
    return uuid.uuid4().hex[:length]


def _trim_base_for_parts(base, *, prefix="", short_id="", max_length=CLIENT_EMAIL_PREFIX_MAX_LENGTH):
    parts_length = len(short_id)
    separators = 1 if short_id else 0
    if prefix:
        parts_length += len(prefix)
        separators += 1
    available = max(max_length - parts_length - separators, 1)
    return clean_client_name(base, max_length=available, fallback="user")


def build_client_display_name(
    customer=None,
    order=None,
    *,
    preferred_name="",
    prefix="",
    short_id="",
    metadata=None,
    max_length=CLIENT_EMAIL_PREFIX_MAX_LENGTH,
):
    metadata = metadata if metadata is not None else (getattr(order, "metadata", None) if order is not None else None)
    short = _short_identifier(short_id, customer=customer, order=order)
    prefix_clean = clean_client_name(prefix, max_length=8, fallback="") if prefix else ""

    base = ""
    for candidate in _name_candidates(customer, order, preferred_name=preferred_name, metadata=metadata):
        base = _clean_optional_name(candidate, max_length=max_length)
        if base:
            break
    if not base:
        base = clean_client_name(_customer_identifier(customer), max_length=max_length, fallback="user")

    base = _trim_base_for_parts(base, prefix=prefix_clean, short_id=short, max_length=max_length)
    parts = [part for part in (prefix_clean, base, short) if part]
    return "_".join(parts)[:max_length].strip("_") or "user"


def build_trial_client_name(customer=None, request=None, *, telegram_user_id="", short_id=""):
    if customer is None and request is not None:
        customer = getattr(request, "customer", None)
    telegram_user_id = telegram_user_id or getattr(request, "telegram_user_id", "")
    return build_client_display_name(
        customer,
        preferred_name="",
        prefix="trial",
        short_id=short_id or telegram_user_id or getattr(customer, "pk", ""),
    )


def build_xui_client_email(email_prefix, client_uuid, *, max_length=MAX_XUI_CLIENT_EMAIL_LENGTH):
    suffix = re.sub(r"[^A-Za-z0-9]+", "", str(client_uuid or "")).lower()[:XUI_CLIENT_UUID_SUFFIX_LENGTH]
    suffix = suffix or uuid.uuid4().hex[:XUI_CLIENT_UUID_SUFFIX_LENGTH]
    prefix_limit = max(max_length - len(suffix) - 1, 1)
    prefix = clean_client_name(email_prefix, max_length=prefix_limit, fallback="user")
    return f"{prefix}_{suffix}"[:max_length].strip("_")
