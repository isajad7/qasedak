import base64
import binascii
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlsplit

from django.db.models import Q
from django.utils import timezone

from .jalali import format_jalali_datetime, persian_digits
from .models import Panel
from .xui_api import XUIError, find_client_by_identifier

logger = logging.getLogger(__name__)

CONFIG_NOT_FOUND_MESSAGE = "این کانفیگ در پنل‌های ما پیدا نشد."
PANEL_LOOKUP_FAILED_MESSAGE = "در حال حاضر امکان بررسی پنل‌ها وجود ندارد. چند دقیقه دیگر تلاش کنید."

SUPPORTED_CONFIG_SCHEMES = {"vless", "vmess", "trojan"}
RAW_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{2,180}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CONFIG_LINK_RE = re.compile(r"\b(?:vless|vmess|trojan)://\S+", re.IGNORECASE)


class ConfigLookupError(ValueError):
    pass


class InvalidConfigLink(ConfigLookupError):
    pass


class ConfigIdentifierMissing(ConfigLookupError):
    pass


@dataclass
class ConfigLookupResult:
    found: bool = False
    panel: object = None
    inbound: object = None
    protocol: str = ""
    identifier: str = ""
    email: str = ""
    remark: str = ""
    enabled: bool | None = None
    total_bytes: int = 0
    used_bytes: int | None = None
    remaining_bytes: int | None = None
    upload_bytes: int | None = None
    download_bytes: int | None = None
    expiry_time: object = None
    remaining_seconds: int | None = None
    remaining_days: int | None = None
    config_link_updated: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(self.__dict__)


def mask_identifier(identifier):
    value = str(identifier or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:6]}...{value[-4:]}"


def _remaining_time_parts(expiry_time):
    if not expiry_time:
        return None, None
    remaining_seconds = int((expiry_time - timezone.now()).total_seconds())
    if remaining_seconds <= 0:
        return 0, 0
    remaining_days = (remaining_seconds + 86_399) // 86_400
    return remaining_seconds, remaining_days


def standardize_lookup_result(result, *, identifier, masked_identifier, panel_errors, searched_panels):
    result = dict(result or {})
    panel = result.get("panel")
    inbound = result.get("inbound")
    expiry_time = result.get("expiry_time") or result.get("expiry_at")
    remaining_seconds, remaining_days = _remaining_time_parts(expiry_time)

    for target_key, source_keys in (
        ("panel_id", ("panel_id",)),
        ("inbound_id", ("inbound_id",)),
        ("total_bytes", ("total_bytes", "total_traffic_bytes")),
        ("used_bytes", ("used_bytes", "used_traffic_bytes")),
        ("remaining_bytes", ("remaining_bytes", "remaining_traffic_bytes")),
        ("upload_bytes", ("upload_bytes", "used_upload_bytes")),
        ("download_bytes", ("download_bytes", "used_download_bytes")),
        ("enabled", ("enabled", "is_enabled")),
    ):
        if result.get(target_key) is None:
            for source_key in source_keys:
                if source_key in result and result.get(source_key) is not None:
                    result[target_key] = result.get(source_key)
                    break

    result.update(
        {
            "found": True,
            "identifier": identifier,
            "masked_identifier": masked_identifier,
            "panel": panel,
            "panel_id": result.get("panel_id") or getattr(panel, "pk", None),
            "panel_name": getattr(panel, "name", None) or result.get("panel_name") or "",
            "inbound": inbound,
            "inbound_id": result.get("inbound_id") or getattr(inbound, "inbound_id", None),
            "inbound_remark": result.get("inbound_remark") or getattr(inbound, "remark", "") or "",
            "expiry_time": expiry_time,
            "expiry_at": expiry_time,
            "remaining_seconds": remaining_seconds,
            "remaining_days": remaining_days,
            "config_link_updated": bool(result.get("config_link_updated", False)),
            "panel_errors": panel_errors,
            "searched_panels": searched_panels,
        }
    )
    for target_key, source_key in (
        ("total_traffic_bytes", "total_bytes"),
        ("used_traffic_bytes", "used_bytes"),
        ("remaining_traffic_bytes", "remaining_bytes"),
        ("used_upload_bytes", "upload_bytes"),
        ("used_download_bytes", "download_bytes"),
        ("is_enabled", "enabled"),
    ):
        if result.get(target_key) is None and source_key in result:
            result[target_key] = result.get(source_key)
    return result


def _config_link_candidate(config_text):
    text = str(config_text or "").strip()
    if not text:
        raise InvalidConfigLink("Empty config text.")

    match = CONFIG_LINK_RE.search(text)
    if match:
        return match.group(0).strip()
    return text


def _decode_vmess_payload(encoded):
    encoded = str(encoded or "").strip()
    if not encoded:
        raise ConfigIdentifierMissing("VMess payload is empty.")
    padding = "=" * (-len(encoded) % 4)
    payload = f"{encoded}{padding}"
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise InvalidConfigLink("VMess payload is not valid base64.") from exc
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidConfigLink("VMess payload is not valid JSON.") from exc


def _canonical_config_query(query):
    ignored_empty_keys = {"host", "path", "sni", "fp", "pbk", "sid", "spx", "alpn", "flow"}
    params = []
    for key, value in parse_qsl(query or "", keep_blank_values=True):
        key = str(key or "").strip()
        value = str(value or "").strip()
        if not key:
            continue
        if value == "" and key.lower() in ignored_empty_keys:
            continue
        params.append((key.lower(), value))
    return sorted(params)


def canonical_config_link(config_text):
    candidate = _config_link_candidate(config_text)
    lowered = candidate.lower()

    if lowered.startswith("vmess://"):
        encoded = candidate[len("vmess://") :].split("#", 1)[0].split("?", 1)[0].strip()
        payload = _decode_vmess_payload(encoded)
        payload.pop("ps", None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    if lowered.startswith("vless://") or lowered.startswith("trojan://"):
        parsed = urlsplit(candidate)
        return json.dumps(
            {
                "scheme": (parsed.scheme or "").lower(),
                "username": unquote(parsed.username or ""),
                "host": (parsed.hostname or "").lower(),
                "port": parsed.port or "",
                "query": _canonical_config_query(parsed.query),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    raise InvalidConfigLink("Config text is not a supported config link.")


def config_link_fingerprint(config_text):
    try:
        canonical = canonical_config_link(config_text)
    except ConfigLookupError:
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_client_identifier_from_config(config_text):
    candidate = _config_link_candidate(config_text)
    lowered = candidate.lower()

    if lowered.startswith("vless://"):
        parsed = urlsplit(candidate)
        identifier = unquote(parsed.username or "").strip()
        if not identifier:
            raise ConfigIdentifierMissing("VLESS client id was not found.")
        return identifier

    if lowered.startswith("trojan://"):
        parsed = urlsplit(candidate)
        identifier = unquote(parsed.username or "").strip()
        if not identifier:
            raise ConfigIdentifierMissing("Trojan password was not found.")
        return identifier

    if lowered.startswith("vmess://"):
        encoded = candidate[len("vmess://") :].split("#", 1)[0].split("?", 1)[0].strip()
        payload = _decode_vmess_payload(encoded)
        identifier = str((payload or {}).get("id") or "").strip()
        if not identifier:
            raise ConfigIdentifierMissing("VMess client id was not found.")
        return identifier

    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() not in SUPPORTED_CONFIG_SCHEMES:
        raise InvalidConfigLink("Unsupported config scheme.")

    raw_identifier = candidate.strip()
    if UUID_RE.fullmatch(raw_identifier) or RAW_IDENTIFIER_RE.fullmatch(raw_identifier):
        return raw_identifier
    raise InvalidConfigLink("Config text is not a supported link or raw identifier.")


def lookup_client_across_panels(identifier, *, store=None):
    identifier = str(identifier or "").strip()
    if not identifier:
        raise ConfigIdentifierMissing("Client identifier is empty.")

    panels = Panel.objects.filter(is_active=True).order_by("name", "id")
    if store is not None:
        panels = panels.filter(Q(store=store) | Q(store__isnull=True))

    panel_errors = []
    searched_panels = 0
    masked = mask_identifier(identifier)
    for panel in panels:
        try:
            result = find_client_by_identifier(panel, identifier)
            searched_panels += 1
        except Exception as exc:
            panel_errors.append({"panel": panel.name, "panel_id": panel.pk, "error": str(exc)})
            logger.warning(
                "Config lookup panel failed panel=%s identifier=%s error=%s",
                panel.pk,
                masked,
                exc,
            )
            continue
        if result:
            return standardize_lookup_result(
                result,
                identifier=identifier,
                masked_identifier=masked,
                panel_errors=panel_errors,
                searched_panels=searched_panels,
            )

    return {
        "found": False,
        "identifier": identifier,
        "masked_identifier": masked,
        "panel_errors": panel_errors,
        "searched_panels": searched_panels,
    }


def _int_value(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _optional_int_value(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present_result(result, *keys, default=None):
    for key in keys:
        if key in result and result.get(key) is not None:
            return result.get(key)
    return default


def _format_gb(value):
    try:
        number = Decimal(int(value or 0)) / Decimal(1024 ** 3)
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal("0")
    label = f"{number.quantize(Decimal('0.01')):f}".rstrip("0").rstrip(".")
    return persian_digits(label or "0")


def _format_optional_gb(value):
    if value is None:
        return "نامشخص"
    return f"{_format_gb(value)} گیگ"


def _format_total_bytes(value):
    if not _int_value(value):
        return "نامحدود"
    return f"{_format_gb(value)} گیگ"


def _format_remaining_bytes(total, remaining):
    if not _int_value(total):
        return "نامحدود"
    if remaining is None:
        return "نامشخص"
    return f"{_format_gb(remaining)} گیگ"


def _format_time_remaining(expiry_at):
    if not expiry_at:
        return "نامحدود"
    now = timezone.now()
    if expiry_at <= now:
        return "منقضی شده"
    total_seconds = max(int((expiry_at - now).total_seconds()), 0)
    if total_seconds >= 86_400:
        days = (total_seconds + 86_399) // 86_400
        return f"{persian_digits(days)} روز"
    hours = max(total_seconds // 3_600, 1)
    return f"{persian_digits(hours)} ساعت"


def _client_display_name(result):
    client = result.get("client") or {}
    stats = result.get("client_stats") or {}
    for key in ("remark", "email", "name"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    for key in ("remark", "email", "name"):
        value = str(client.get(key) or stats.get(key) or "").strip()
        if value:
            return value
    return "بدون نام"


def format_client_usage_result(result):
    if not result or not result.get("found"):
        if result and result.get("panel_errors") and not result.get("searched_panels"):
            return PANEL_LOOKUP_FAILED_MESSAGE
        return CONFIG_NOT_FOUND_MESSAGE

    panel = result.get("panel")
    inbound = result.get("inbound")
    panel_name = getattr(panel, "name", None) or result.get("panel_name") or "نامشخص"
    inbound_number = result.get("inbound_id") or getattr(inbound, "inbound_id", "-")
    inbound_label = (
        getattr(inbound, "remark", None)
        or result.get("inbound_remark")
        or f"Inbound {persian_digits(inbound_number)}"
    )
    protocol = str(result.get("protocol") or getattr(inbound, "protocol", "") or "نامشخص").upper()
    enabled = _first_present_result(result, "enabled", "is_enabled")
    status = "غیرفعال ❌" if enabled is False else "فعال ✅"
    total = _int_value(_first_present_result(result, "total_bytes", "total_traffic_bytes", default=0))
    used = _optional_int_value(_first_present_result(result, "used_bytes", "used_traffic_bytes"))
    remaining = _optional_int_value(_first_present_result(result, "remaining_bytes", "remaining_traffic_bytes"))
    expiry_at = result.get("expiry_time") or result.get("expiry_at")
    stats_available = bool(result.get("stats_available", used is not None))

    lines = [
        "📊 وضعیت کانفیگ شما",
        "",
        f"نام: {_client_display_name(result)}",
        f"سرور: {panel_name}",
        f"پروتکل: {protocol}",
        f"وضعیت: {status}",
        "",
        f"حجم کل: {_format_total_bytes(total)}",
        f"مصرف‌شده: {_format_optional_gb(used)}",
        f"باقی‌مانده: {_format_remaining_bytes(total, remaining)}",
        "",
        f"زمان انقضا: {format_jalali_datetime(expiry_at, default='نامحدود') if expiry_at else 'نامحدود'}",
    ]
    if expiry_at and expiry_at <= timezone.now():
        lines.append("وضعیت زمانی: منقضی شده")
    else:
        lines.append(f"باقی‌مانده زمانی: {_format_time_remaining(expiry_at)}")
    lines.append(f"Inbound: {inbound_label}")
    if not stats_available:
        lines.extend(["", "آمار مصرف از پنل در دسترس نبود."])
    return "\n".join(lines)


def check_config_usage(config_text, *, store=None):
    identifier = extract_client_identifier_from_config(config_text)
    logger.info("Checking config usage identifier=%s", mask_identifier(identifier))
    result = lookup_client_across_panels(identifier, store=store)
    result["message"] = format_client_usage_result(result)
    return result
