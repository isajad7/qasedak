import base64
import hashlib
import json
import logging
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .naming import build_client_display_name, build_xui_client_email
from .xui_compat import (
    PROFILE_MODERN_MULTI_NODE,
    PROFILE_MODERN_SINGLE_NODE,
    assert_precise_client_scope,
    build_remote_client_key,
    classify_xui_error,
    detect_xui_version,
    discover_xui_capabilities,
    get_xui_adapter,
    inbound_remote_key,
    normalize_client,
    normalize_inbound,
    normalize_node,
    normalize_usage,
)
from .xui_compat.errors import XUIAmbiguousScopeError, XUICompatibilityError, XUIUnknownSafeModeError

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 30
PANEL_LOGIN_TIMEOUT_SECONDS = (5, 30)
PANEL_LOGIN_ATTEMPTS = 2
CLIENT_STATS_CACHE_SECONDS = 60
USAGE_SNAPSHOT_INTERVAL_SECONDS = 300
REALITY_PUBLIC_KEY_MISSING_MESSAGE = "Reality public key برای ساخت لینک پیدا نشد. لینک تولیدشده ممکن است قابل استفاده نباشد."


class XUIError(Exception):
    def __init__(
        self,
        message,
        *,
        category="unknown",
        http_status=None,
        endpoint="",
        response_snippet="",
        remediation_hint="",
    ):
        super().__init__(message)
        self.category = category or "unknown"
        self.http_status = http_status
        self.endpoint = endpoint or ""
        self.response_snippet = response_snippet or ""
        self.remediation_hint = remediation_hint or ""

    def safe_metadata(self):
        metadata = {"error_category": self.category}
        if self.http_status is not None:
            metadata["http_status"] = self.http_status
        if self.endpoint:
            metadata["endpoint"] = self.endpoint
        if self.response_snippet:
            metadata["response_snippet"] = self.response_snippet
        if self.remediation_hint:
            metadata["remediation_hint"] = self.remediation_hint
        return metadata


def xui_panel_proxy_url(panel=None, proxy_url=None):
    if proxy_url is None and panel is not None:
        proxy_url = getattr(panel, "proxy_url", "")
    proxy_url = (proxy_url or "").strip()
    if proxy_url:
        return proxy_url
    return (getattr(settings, "XUI_PANEL_PROXY_URL", "") or "").strip()


def xui_panel_proxies(panel=None, proxy_url=None):
    proxy_url = xui_panel_proxy_url(panel=panel, proxy_url=proxy_url)
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def configure_xui_session(session, panel=None, proxy_url=None):
    session.trust_env = False
    proxies = xui_panel_proxies(panel=panel, proxy_url=proxy_url)
    if proxies:
        session.proxies.update(proxies)
    return session


def resolve_inbound_panel(inbound, panel=None, *, require_active=True):
    if not inbound:
        raise XUIError("Inbound is required for panel operation.")
    try:
        inbound_panel = inbound.panel
    except Exception as exc:
        raise XUIError("Inbound is not linked to a panel.") from exc
    if not getattr(inbound, "panel_id", None) or not inbound_panel:
        raise XUIError("Inbound is not linked to a panel.")
    if panel is not None and getattr(panel, "pk", None) != getattr(inbound_panel, "pk", None):
        raise XUIError("Provided panel does not match inbound.panel.")
    if require_active:
        if not getattr(inbound, "is_active", False):
            raise XUIError("Inbound is inactive.")
        if not getattr(inbound_panel, "is_active", False):
            raise XUIError("Inbound panel is inactive.")
    return inbound_panel


PERSIAN_TO_ASCII = {
    "ا": "a",
    "آ": "a",
    "أ": "a",
    "إ": "a",
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ث": "s",
    "ج": "j",
    "چ": "ch",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "z",
    "ر": "r",
    "ز": "z",
    "ژ": "zh",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "z",
    "ط": "t",
    "ظ": "z",
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ک": "k",
    "ك": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "و": "v",
    "ه": "h",
    "ة": "h",
    "ی": "y",
    "ي": "y",
    "ى": "y",
    "ئ": "y",
    "ؤ": "v",
    "ء": "",
    "‌": "_",
    "٠": "0",
    "۰": "0",
    "١": "1",
    "۱": "1",
    "٢": "2",
    "۲": "2",
    "٣": "3",
    "۳": "3",
    "٤": "4",
    "۴": "4",
    "٥": "5",
    "۵": "5",
    "٦": "6",
    "۶": "6",
    "٧": "7",
    "۷": "7",
    "٨": "8",
    "۸": "8",
    "٩": "9",
    "۹": "9",
}

COMMON_PERSIAN_NAME_TOKENS = {
    "علی": "ali",
    "رضا": "reza",
    "رضایی": "rezaei",
    "محمد": "mohammad",
    "مهدی": "mahdi",
    "حسین": "hosein",
    "حسینی": "hoseini",
    "حسن": "hasan",
    "امیر": "amir",
    "امیرحسین": "amirhosein",
    "فاطمه": "fatemeh",
    "زهرا": "zahra",
    "سارا": "sara",
    "مریم": "maryam",
}


def bytes_from_gb(value):
    try:
        gb_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        gb_value = Decimal("0")
    return int(gb_value * Decimal(1024 ** 3))


def clean_decimal_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    label = format(number.normalize(), "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def transliterate_to_ascii(value, *, fallback="user"):
    value = (value or "").strip().lower()
    converted = []
    for token in re.split(r"(\s+|[-_.]+)", value):
        if not token:
            continue
        if token in COMMON_PERSIAN_NAME_TOKENS:
            converted.append(COMMON_PERSIAN_NAME_TOKENS[token])
            continue
        for char in token:
            if char in PERSIAN_TO_ASCII:
                converted.append(PERSIAN_TO_ASCII[char])
            elif char.isascii() and char.isalnum():
                converted.append(char)
            elif char.isspace() or char in {"-", "_", "."}:
                converted.append("_")
    slug = "".join(converted)
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or fallback


def build_config_email_prefix(payer_name, total_gb, tracking_code=""):
    return build_client_display_name(preferred_name=payer_name, short_id=tracking_code)


def _decoded_json_value(value):
    if isinstance(value, (dict, list, tuple)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "{[":
            return value
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return value
    return value


def first_value(value):
    value = _decoded_json_value(value)
    if isinstance(value, list) and value:
        return first_value(value[0])
    if isinstance(value, tuple) and value:
        return first_value(value[0])
    if isinstance(value, str):
        return value
    return ""


def first_present_value(*values):
    for value in values:
        if value is None or value == "":
            continue
        nested = first_value(value)
        if nested != "":
            return nested
    return ""


def append_param(params, key, value):
    if value is None or value == "":
        return
    params.append((key, str(value)))


def _nested_mapping(value):
    value = _decoded_json_value(value)
    return value if isinstance(value, dict) else {}


def _native_config_link_from_value(value):
    value = _decoded_json_value(value)
    if isinstance(value, str):
        text = value.strip()
        if re.match(r"(?i)^(vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic)://", text):
            return text
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            native_link = _native_config_link_from_value(item)
            if native_link:
                return native_link
        return ""
    if isinstance(value, dict):
        direct_keys = (
            "direct_link",
            "directLink",
            "shareLink",
            "share_link",
            "configLink",
            "config_link",
            "link",
            "uri",
        )
        for key in direct_keys:
            native_link = _native_config_link_from_value(value.get(key))
            if native_link:
                return native_link
        return ""
    return ""


def native_config_link_from_sources(*sources):
    for source in sources:
        native_link = _native_config_link_from_value(source)
        if native_link:
            return native_link
    return ""


def _response_obj_mapping(response):
    if not isinstance(response, dict):
        return {}
    obj = response.get("obj")
    return obj if isinstance(obj, dict) else {}


def build_vless_query_params(stream_settings, client_data=None):
    client_data = client_data or {}
    stream_settings = stream_settings or {}
    network = str(stream_settings.get("network") or "tcp").strip().lower()
    security = str(stream_settings.get("security") or "none").strip().lower()
    params = []

    append_param(params, "type", network)
    append_param(params, "security", security)
    append_param(params, "encryption", "none")
    append_param(params, "flow", client_data.get("flow"))

    if security == "reality":
        reality_settings = _nested_mapping(stream_settings.get("realitySettings"))
        reality_inner_settings = _nested_mapping(reality_settings.get("settings"))
        reality_dest_settings = _nested_mapping(reality_inner_settings.get("dest"))
        client_reality_settings = _nested_mapping(client_data.get("realitySettings"))
        public_key = first_present_value(
            reality_settings.get("publicKey"),
            reality_inner_settings.get("publicKey"),
            client_reality_settings.get("publicKey"),
            client_data.get("publicKey"),
            client_data.get("pbk"),
        )
        if not public_key:
            raise XUIError(
                REALITY_PUBLIC_KEY_MISSING_MESSAGE,
                category="reality_public_key_missing",
                remediation_hint="Reality inbound streamSettings.realitySettings.settings.publicKey را بررسی کنید یا لینک native پنل را استفاده کنید.",
            )
        append_param(
            params,
            "pbk",
            public_key,
        )
        append_param(
            params,
            "fp",
            first_present_value(
                reality_settings.get("fingerprint"),
                reality_inner_settings.get("fingerprint"),
                client_reality_settings.get("fingerprint"),
                client_data.get("fingerprint"),
                "chrome",
            ),
        )
        append_param(
            params,
            "sni",
            first_present_value(
                reality_settings.get("serverName"),
                reality_settings.get("serverNames"),
                reality_inner_settings.get("serverName"),
                reality_inner_settings.get("serverNames"),
                reality_dest_settings.get("serverName"),
                client_reality_settings.get("serverName"),
                client_reality_settings.get("serverNames"),
                client_data.get("sni"),
            ),
        )
        append_param(
            params,
            "sid",
            first_present_value(
                reality_settings.get("shortId"),
                reality_settings.get("shortIds"),
                reality_inner_settings.get("shortId"),
                reality_inner_settings.get("shortIds"),
                client_reality_settings.get("shortId"),
                client_reality_settings.get("shortIds"),
                client_data.get("sid"),
            ),
        )
        append_param(
            params,
            "spx",
            first_present_value(
                reality_settings.get("spiderX"),
                reality_inner_settings.get("spiderX"),
                client_reality_settings.get("spiderX"),
                "/",
            ),
        )
    elif security == "tls":
        tls_settings = _nested_mapping(stream_settings.get("tlsSettings"))
        append_param(params, "sni", tls_settings.get("serverName"))
        alpn = tls_settings.get("alpn")
        if isinstance(alpn, list) and alpn:
            append_param(params, "alpn", ",".join(alpn))
        append_param(params, "fp", tls_settings.get("fingerprint"))

    if network == "ws":
        ws_settings = _nested_mapping(stream_settings.get("wsSettings"))
        headers = ws_settings.get("headers") or {}
        append_param(params, "path", ws_settings.get("path") or "/")
        append_param(params, "host", headers.get("Host") or headers.get("host") or ws_settings.get("host"))
    elif network == "grpc":
        grpc_settings = _nested_mapping(stream_settings.get("grpcSettings"))
        append_param(params, "serviceName", grpc_settings.get("serviceName"))
        append_param(params, "authority", grpc_settings.get("authority"))
        append_param(params, "mode", "multi" if grpc_settings.get("multiMode") else "gun")
    elif network == "tcp":
        tcp_settings = _nested_mapping(stream_settings.get("tcpSettings"))
        header = tcp_settings.get("header") or {}
        header_type = header.get("type")
        if header_type and header_type != "none":
            append_param(params, "headerType", header_type)
            request_settings = header.get("request") or {}
            append_param(params, "path", first_value(request_settings.get("path")))
            headers = request_settings.get("headers") or {}
            append_param(params, "host", first_present_value(headers.get("Host"), headers.get("host")))

    return urlencode(params, doseq=False)


def build_trojan_query_params(stream_settings, client_data=None):
    params = [
        (key, value)
        for key, value in parse_qsl(
            build_vless_query_params(stream_settings, client_data),
            keep_blank_values=True,
        )
        if key not in {"encryption", "flow"}
    ]
    return urlencode(params, doseq=False)


def build_vmess_payload(*, address, port, stream_settings, client_data, remark):
    stream_settings = stream_settings or {}
    client_data = client_data or {}
    network = str(stream_settings.get("network") or "tcp").strip().lower()
    security = str(stream_settings.get("security") or "none").strip().lower()
    payload = {
        "v": "2",
        "ps": remark or client_data.get("email") or "",
        "add": address,
        "port": str(port or ""),
        "id": str(client_data.get("id") or ""),
        "aid": str(client_data.get("alterId") or client_data.get("alter_id") or 0),
        "scy": client_data.get("security") or "auto",
        "net": network,
        "type": "none",
        "host": "",
        "path": "",
        "tls": "tls" if security == "tls" else "",
        "sni": "",
    }

    if network == "ws":
        ws_settings = stream_settings.get("wsSettings") or {}
        headers = ws_settings.get("headers") or {}
        payload["host"] = headers.get("Host") or ws_settings.get("host") or ""
        payload["path"] = ws_settings.get("path") or "/"
    elif network == "grpc":
        grpc_settings = stream_settings.get("grpcSettings") or {}
        payload["type"] = "gun"
        payload["path"] = grpc_settings.get("serviceName") or ""
    elif network == "tcp":
        tcp_settings = stream_settings.get("tcpSettings") or {}
        header = tcp_settings.get("header") or {}
        payload["type"] = header.get("type") or "none"
        request_settings = header.get("request") or {}
        payload["path"] = first_value(request_settings.get("path"))
        headers = request_settings.get("headers") or {}
        payload["host"] = first_value(headers.get("Host"))

    if security == "tls":
        tls_settings = stream_settings.get("tlsSettings") or {}
        payload["sni"] = tls_settings.get("serverName") or ""

    return payload


def encode_vmess_link(payload):
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"vmess://{encoded.rstrip('=')}"


def parse_xui_datetime(value):
    if not value:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timezone.datetime.fromtimestamp(timestamp, tz=timezone.get_current_timezone())


def xui_datetime_to_millis(value):
    if value is None:
        return None
    if value == 0 or value == "0":
        return 0
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise XUIError("Expiry time must be a datetime or timestamp.") from exc
    if timestamp and timestamp < 10_000_000_000:
        timestamp *= 1000
    return timestamp


CLIENT_IDENTIFIER_FIELDS = (
    "id",
    "email",
    "password",
    "subId",
    "sub_id",
    "remark",
    "name",
    "tgId",
)


def parse_xui_json_object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_xui_json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def parse_xui_json_list_or_single(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


def parse_xui_client_stats(inbound_data):
    inbound_data = inbound_data or {}
    stats = []
    for key in ("clientStats", "client_stats", "clientTraffics", "client_traffics"):
        stats.extend(item for item in parse_xui_json_list_or_single(inbound_data.get(key)) if isinstance(item, dict))
    obj = inbound_data.get("obj")
    if isinstance(obj, dict):
        for key in ("clientStats", "client_stats", "clientTraffics", "client_traffics"):
            stats.extend(item for item in parse_xui_json_list_or_single(obj.get(key)) if isinstance(item, dict))
    return stats


def xui_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def xui_int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_xui_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def xui_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def lookup_values(data, fields=CLIENT_IDENTIFIER_FIELDS):
    if not isinstance(data, dict):
        return []
    values = []
    for field in fields:
        value = data.get(field)
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
        elif value is not None:
            value = str(value).strip()
            if value:
                values.append(value)
    return values


def match_client_identifier(data, identifier):
    identifier = str(identifier or "").strip()
    if not identifier:
        return ""
    lowered_identifier = identifier.lower()
    for field in CLIENT_IDENTIFIER_FIELDS:
        value = data.get(field) if isinstance(data, dict) else None
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if candidate and (candidate == identifier or candidate.lower() == lowered_identifier):
                return field
    return ""


def has_any_key(data, keys):
    return isinstance(data, dict) and any(key in data for key in keys)


def has_usage_stats(data):
    return has_any_key(data, ("up", "down", "upload", "download", "used", "usedTraffic", "used_traffic"))


def mask_xui_value(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:6]}...{value[-4:]}"


def sanitize_xui_operational_text(value, *, panel=None, max_length=500):
    text = str(value or "")
    for secret in (
        getattr(panel, "password", "") if panel else "",
        getattr(panel, "username", "") if panel else "",
        getattr(panel, "url", "") if panel else "",
        getattr(panel, "proxy_url", "") if panel else "",
    ):
        secret = str(secret or "").strip()
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"https?://[^\s<>()]+", "<url-redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:vless|vmess|trojan|ss|ssr)://[^\s<>()]+", "<config-link-redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)(csrf[-_ ]?token|session|cookie)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)\bcsrf[-_a-z0-9]{6,}\b", "<csrf-redacted>", text)
    text = re.sub(r"(?i)\b(?:session|cookie)[-_a-z0-9]{8,}\b", "<session-redacted>", text)
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        lambda match: mask_xui_value(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )
    if len(text) > max_length:
        text = f"{text[:max_length - 1]}..."
    return text


def _safe_response_snippet(response, *, panel=None, max_length=240):
    text = getattr(response, "text", "") or ""
    if not text:
        try:
            text = json.dumps(response.json(), ensure_ascii=False)
        except Exception:
            text = ""
    text = sanitize_xui_operational_text(text, panel=panel, max_length=max_length)
    text = re.sub(r"(?i)(csrf[-_ ]?token|session|cookie)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)\bcsrf[-_a-z0-9]{6,}\b", "<csrf-redacted>", text)
    text = re.sub(r"(?i)\b(?:session|cookie)[-_a-z0-9]{8,}\b", "<session-redacted>", text)
    return text


def _xui_http_category(status_code, *, endpoint=""):
    if status_code == 403:
        if endpoint in {"login", "csrf-token", "getTwoFactorEnable"}:
            return "http_403_csrf_required"
        return "http_403"
    if status_code == 401:
        return "http_401"
    return "unexpected_response"


def _xui_remediation_hint(category):
    return {
        "http_403_csrf_required": "CSRF/login flow mismatch; use the 3X-UI 3.5 CSRF login flow and verify panel security settings.",
        "http_403": "Panel refused the request with HTTP 403; verify login/session permissions and panel security settings.",
        "write_api_forbidden": "Panel refused an authenticated write request with HTTP 403 after CSRF refresh; verify write permissions, CSRF settings, and panel security rules.",
        "http_401": "Panel returned HTTP 401; verify panel username/password and account permissions.",
        "auth_failed": "Verify panel username/password and account permissions.",
        "two_factor_required": "Disable two-factor login for this panel account or use an automation account without 2FA.",
        "network_timeout": "Check panel reachability, firewall rules, and timeout settings.",
        "connection_refused": "Check that the panel service is running and the host/port are reachable.",
        "dns_error": "Check the panel hostname and DNS resolution.",
        "tls_error": "Check the panel TLS certificate and HTTPS configuration.",
        "invalid_url": "Check the panel URL format, including scheme, host, port, and base path.",
        "unexpected_response": "Panel returned an unexpected response; verify panel version and API compatibility.",
        "unsupported_version": "Panel version/profile is not supported by the current integration.",
    }.get(category or "", "Check panel connectivity and X-UI compatibility.")


def classify_xui_exception(exc):
    if isinstance(exc, XUIError):
        return exc.category or "unknown", str(exc) or "Panel operation failed.", exc.safe_metadata()
    if isinstance(exc, requests.Timeout):
        return "network_timeout", "Panel request timed out.", {
            "error_category": "network_timeout",
            "remediation_hint": _xui_remediation_hint("network_timeout"),
        }
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error", "Panel TLS/SSL request failed.", {
            "error_category": "tls_error",
            "remediation_hint": _xui_remediation_hint("tls_error"),
        }
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema)):
        return "invalid_url", "Panel URL is invalid.", {
            "error_category": "invalid_url",
            "remediation_hint": _xui_remediation_hint("invalid_url"),
        }
    if isinstance(exc, requests.ConnectionError):
        message = str(exc).lower()
        if "name or service not known" in message or "temporary failure in name resolution" in message or "nodename nor servname" in message:
            category = "dns_error"
        elif "connection refused" in message:
            category = "connection_refused"
        else:
            category = "connection_refused"
        return category, "Panel connection failed.", {
            "error_category": category,
            "remediation_hint": _xui_remediation_hint(category),
        }
    if isinstance(exc, requests.RequestException):
        return "unknown", "Panel request failed.", {
            "error_category": "unknown",
            "remediation_hint": _xui_remediation_hint("unknown"),
        }
    return "unknown", "Panel operation failed.", {
        "error_category": "unknown",
        "remediation_hint": _xui_remediation_hint("unknown"),
    }


def hash_xui_identifier(value):
    value = str(value or "").strip()
    if not value:
        return ""
    salt = getattr(settings, "SECRET_KEY", "") or ""
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def _usage_source_and_values(client, stats):
    client = client or {}
    stats = stats or {}
    usage_source = stats if has_usage_stats(stats) else client if has_usage_stats(client) else {}
    stats_available = bool(usage_source)
    upload = download = used = None
    if stats_available:
        upload = xui_int(first_xui_value(usage_source.get("up"), usage_source.get("upload")), 0)
        download = xui_int(first_xui_value(usage_source.get("down"), usage_source.get("download")), 0)
        used = xui_int_or_none(
            first_xui_value(
                usage_source.get("used"),
                usage_source.get("usedTraffic"),
                usage_source.get("used_traffic"),
            )
        )
        if used is None:
            used = upload + download
    return usage_source, stats_available, upload, download, used


def _client_identifier_for_usage(client, stats):
    for data in (client or {}, stats or {}):
        for field_name in ("id", "uuid", "password", "subId", "sub_id", "email"):
            value = str(data.get(field_name) or "").strip()
            if value:
                return value
    return ""


def _client_online_from_sources(client, stats, online_clients, online_available):
    explicit = first_xui_value(
        (stats or {}).get("online"),
        (stats or {}).get("isOnline"),
        (client or {}).get("online"),
        (client or {}).get("isOnline"),
    )
    if explicit is not None:
        return xui_bool(explicit)
    if online_available:
        online_values = {str(value).strip() for value in (online_clients or set()) if str(value).strip()}
        candidates = set(lookup_values(client or {}))
        candidates.update(lookup_values(stats or {}))
        return any(candidate in online_values for candidate in candidates)
    return None


def normalize_xui_usage_client(*, inbound, inbound_data, client, stats, online_clients=None, online_available=False):
    client = client or {}
    stats = stats or {}
    identifier = _client_identifier_for_usage(client, stats)
    identifier_hash = hash_xui_identifier(identifier)
    if not identifier_hash:
        return None

    usage_source, stats_available, upload, download, used = _usage_source_and_values(client, stats)
    total = xui_int_or_none(
        first_xui_value(
            stats.get("total"),
            stats.get("totalGB"),
            client.get("totalGB"),
            client.get("total"),
            (inbound_data or {}).get("totalGB"),
            (inbound_data or {}).get("total"),
        )
    )
    expiry_time = parse_xui_datetime(first_xui_value(stats.get("expiryTime"), client.get("expiryTime")))
    enabled_value = first_xui_value(client.get("enable"), client.get("enabled"), stats.get("enable"), stats.get("enabled"))
    last_online_at = parse_xui_datetime(
        first_xui_value(
            stats.get("lastOnline"),
            stats.get("lastOnlineTime"),
            stats.get("last_online_at"),
            client.get("lastOnline"),
            client.get("lastOnlineTime"),
        )
    )
    email = str(first_xui_value(client.get("email"), stats.get("email")) or "").strip()
    source = "clientStats" if usage_source is stats else "client" if usage_source is client else ""
    return {
        "inbound_id": getattr(inbound, "pk", None),
        "remote_inbound_id": getattr(inbound, "inbound_id", None),
        "node_id": getattr(inbound, "xui_node_id", "") or "",
        "remote_key": inbound_remote_key(inbound, panel=getattr(inbound, "panel", None)),
        "identifier_hash": identifier_hash,
        "identifier_masked": mask_xui_value(identifier),
        "email_masked": mask_xui_value(email),
        "upload_bytes": upload if upload is not None else 0,
        "download_bytes": download if download is not None else 0,
        "used_bytes": used if used is not None else 0,
        "total_bytes": total,
        "expiry_time": expiry_time,
        "enabled": xui_bool(enabled_value) if enabled_value is not None else None,
        "online": _client_online_from_sources(client, stats, online_clients, online_available),
        "source": source,
        "stats_available": stats_available,
        "metadata": {
            "matched_usage_source": source,
            "has_usage_stats": stats_available,
            "last_online_at": last_online_at.isoformat() if last_online_at else "",
            "remote_inbound_id": getattr(inbound, "inbound_id", None),
            "node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(inbound, panel=getattr(inbound, "panel", None)),
        },
    }


def related_client_stats(client_stats, client=None, *, identifier=""):
    client = client or {}
    identifier = str(identifier or "").strip()
    related_values = {identifier.lower()} if identifier else set()
    related_values.update(value.lower() for value in lookup_values(client))
    for stats in client_stats:
        if not isinstance(stats, dict):
            continue
        if match_client_identifier(stats, identifier):
            return stats
        stats_values = {value.lower() for value in lookup_values(stats)}
        if related_values.intersection(stats_values):
            return stats
    return {}


def find_xui_client_and_stats(inbound_data, identifier):
    settings = parse_xui_json_object((inbound_data or {}).get("settings"))
    clients = settings.get("clients") or []
    if not isinstance(clients, list):
        clients = []
    client_stats = parse_xui_client_stats(inbound_data)

    target_client = None
    matched_field = ""
    for panel_client in clients:
        matched_field = match_client_identifier(panel_client, identifier)
        if matched_field:
            target_client = panel_client
            break

    target_stats = related_client_stats(client_stats, target_client, identifier=identifier)
    if target_stats and not matched_field:
        matched_field = match_client_identifier(target_stats, identifier) or "clientStats"

    if not target_client and target_stats:
        stats_email = str(target_stats.get("email") or "").strip()
        stats_id = str(target_stats.get("id") or "").strip()
        for panel_client in clients:
            if (
                (stats_email and str(panel_client.get("email") or "").strip() == stats_email)
                or (stats_id and str(panel_client.get("id") or "").strip() == stats_id)
            ):
                target_client = panel_client
                matched_field = matched_field or "clientStats"
                break

    return target_client, target_stats, matched_field, clients, client_stats


@dataclass
class XUIClientStats:
    uuid: str = ""
    email: str = ""
    inbound_id: int | None = None
    total_traffic_bytes: int = 0
    used_upload_bytes: int = 0
    used_download_bytes: int = 0
    used_traffic_bytes: int = 0
    remaining_traffic_bytes: int = 0
    expiry_at: object = None
    last_online_at: object = None
    is_enabled: bool = False
    is_expired: bool = False
    panel_available: bool = True
    error: str = ""
    raw: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def to_dict(self):
        return {
            "uuid": self.uuid,
            "email": self.email,
            "inbound_id": self.inbound_id,
            "total_traffic_bytes": self.total_traffic_bytes,
            "used_upload_bytes": self.used_upload_bytes,
            "used_download_bytes": self.used_download_bytes,
            "used_traffic_bytes": self.used_traffic_bytes,
            "remaining_traffic_bytes": self.remaining_traffic_bytes,
            "expiry_at": self.expiry_at,
            "last_online_at": self.last_online_at,
            "is_enabled": self.is_enabled,
            "is_expired": self.is_expired,
            "panel_available": self.panel_available,
            "error": self.error,
            "raw": self.raw,
            "history": self.history,
        }


class XUIService:
    def __init__(self, panel, *, timeout_seconds=None):
        self.panel = panel
        self.base_url = self._normalize_base_url(panel.url)
        self.session = configure_xui_session(requests.Session(), panel=panel)
        self._logged_in = False
        self.csrf_token = ""
        if timeout_seconds is None:
            self.timeout_seconds = PANEL_TIMEOUT_SECONDS
            self.login_timeout = PANEL_LOGIN_TIMEOUT_SECONDS
        else:
            self.timeout_seconds = max(int(timeout_seconds or PANEL_TIMEOUT_SECONDS), 1)
            connect_timeout = min(5, self.timeout_seconds)
            self.login_timeout = (connect_timeout, self.timeout_seconds)

    def _normalize_base_url(self, value):
        url = str(value or "").strip().rstrip("/")
        if not url:
            raise XUIError(
                "Panel URL is invalid.",
                category="invalid_url",
                remediation_hint=_xui_remediation_hint("invalid_url"),
            )
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise XUIError(
                "Panel URL is invalid.",
                category="invalid_url",
                remediation_hint=_xui_remediation_hint("invalid_url"),
            )
        return url

    def _url(self, path=""):
        path = str(path or "").strip()
        if not path:
            return self.base_url
        return f"{self.base_url}/{path.lstrip('/')}"

    def _base_page_url(self):
        return f"{self.base_url}/"

    def _origin(self):
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _inbound_id(self, inbound_or_id):
        return getattr(inbound_or_id, "inbound_id", inbound_or_id)

    def _inbound_cache_key(self, inbound_or_id):
        if hasattr(inbound_or_id, "inbound_id"):
            scope = inbound_remote_key(inbound_or_id, panel=self.panel)
            pk = getattr(inbound_or_id, "pk", "") or ""
            return f"xui:inbound:{self.panel.pk}:{pk}:{scope}"
        return f"xui:inbound:{self.panel.pk}:legacy:{inbound_or_id}"

    def _assert_precise_scope(self, inbound, identifier="", *, operation="read", allow_multi_scope=False):
        return assert_precise_client_scope(
            self.panel,
            inbound,
            identifier,
            operation=operation,
            allow_multi_scope=allow_multi_scope,
        )

    def _compat_profile(self, *, live=False, write=False):
        if live:
            return discover_xui_capabilities(self.panel, live=True, service=self, write=write, use_cache=False)
        return get_xui_adapter(self.panel).profile

    def _uses_modern_client_api(self):
        return self._compat_profile().profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}

    def _endpoint_missing(self, exc):
        text = str(exc or "").lower()
        return "http 404" in text or "404" == text.strip() or "not found" in text or "no route" in text

    def _ensure_success(self, response, default_message):
        if not isinstance(response, dict):
            raise XUIError(default_message)
        if not response.get("success"):
            raise XUIError(response.get("msg") or default_message)
        return response

    def _modern_client_attachments(self, email):
        email = str(email or "").strip()
        if not email:
            return []
        data = self.authenticated_json("GET", f"/panel/api/clients/get/{quote(email, safe='')}")
        self._ensure_success(data, "Client attachment lookup failed.")
        obj = data.get("obj") or {}
        inbound_ids = obj.get("inboundIds") or obj.get("inbound_ids") or []
        if not isinstance(inbound_ids, list):
            return []
        cleaned = []
        for inbound_id in inbound_ids:
            try:
                cleaned.append(int(inbound_id))
            except (TypeError, ValueError):
                continue
        return cleaned

    def _assert_modern_email_single_attachment(self, inbound, email, *, operation, allow_multi_scope=False):
        if allow_multi_scope:
            return
        inbound_ids = self._modern_client_attachments(email)
        if inbound_ids and inbound_ids != [int(inbound.inbound_id)]:
            raise XUIAmbiguousScopeError(
                f"Modern 3X-UI {operation} is email-wide; client is attached to multiple inbounds."
            )
        if not inbound_ids:
            raise XUIAmbiguousScopeError(
                f"Modern 3X-UI {operation} scope could not be verified from client attachments."
            )

    def _post_legacy_client_create(self, inbound, client_data):
        payload = {
            "id": inbound.inbound_id,
            "settings": json.dumps({"clients": [client_data]}),
        }
        response = self.request_json(
            "POST",
            "/panel/api/inbounds/addClient",
            json=payload,
            headers={"Accept": "application/json"},
        )
        return self._ensure_success(response, "Could not add client to panel.")

    def _post_modern_client_create(self, inbound, client_data, *, inbound_ids=None):
        if client_data.get("enable") is False:
            raise XUICompatibilityError(
                "Official 3X-UI client API cannot safely create a disabled client; refusing create-inactive."
            )
        inbound_ids = [int(value) for value in (inbound_ids or [inbound.inbound_id])]
        response = self.authenticated_json(
            "POST",
            "/panel/api/clients/add",
            json={"client": client_data, "inboundIds": inbound_ids},
            headers={"Accept": "application/json"},
        )
        return self._ensure_success(response, "Could not add client to panel.")

    def _post_client_create(self, inbound, client_data):
        if self._uses_modern_client_api():
            return self._post_modern_client_create(inbound, client_data)
        try:
            return self._post_legacy_client_create(inbound, client_data)
        except Exception as exc:
            if not self._endpoint_missing(exc):
                raise
            profile = self._compat_profile(live=True, write=True)
            if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                return self._post_modern_client_create(inbound, client_data)
            raise

    def _post_legacy_client_update(self, inbound, api_identifier, client_data):
        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/updateClient/{quote(api_identifier, safe='')}",
            data={
                "id": inbound.inbound_id,
                "settings": json.dumps({"clients": [client_data]}),
            },
        )
        return self._ensure_success(response, "Panel rejected client update.")

    def _post_modern_client_update(self, inbound, client_data, *, email=""):
        email = str(email or client_data.get("email") or "").strip()
        if not email:
            raise XUIError("Client email is required for modern 3X-UI update.")
        query = urlencode({"inboundIds": str(inbound.inbound_id)})
        response = self.authenticated_json(
            "POST",
            f"/panel/api/clients/update/{quote(email, safe='')}?{query}",
            json=client_data,
            headers={"Accept": "application/json"},
        )
        return self._ensure_success(response, "Panel rejected client update.")

    def _post_client_update(self, inbound, api_identifier, client_data, *, email=""):
        if self._uses_modern_client_api():
            return self._post_modern_client_update(inbound, client_data, email=email)
        try:
            return self._post_legacy_client_update(inbound, api_identifier, client_data)
        except Exception as exc:
            if not self._endpoint_missing(exc):
                raise
            profile = self._compat_profile(live=True, write=True)
            if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                return self._post_modern_client_update(inbound, client_data, email=email)
            raise

    def _post_legacy_client_delete(self, inbound, api_identifier):
        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/{inbound.inbound_id}/delClient/{quote(api_identifier, safe='')}",
            headers={"Accept": "application/json"},
        )
        return response

    def _post_modern_client_delete(self, inbound, email, *, keep_traffic=False, allow_multi_scope=False):
        self._assert_modern_email_single_attachment(
            inbound,
            email,
            operation="delete",
            allow_multi_scope=allow_multi_scope,
        )
        query = urlencode({"keepTraffic": "1" if keep_traffic else "0"})
        response = self.authenticated_json(
            "POST",
            f"/panel/api/clients/del/{quote(email, safe='')}?{query}",
            headers={"Accept": "application/json"},
        )
        return response

    def _post_client_delete(self, inbound, api_identifier, *, email="", allow_multi_scope=False):
        email = str(email or "").strip()
        if self._uses_modern_client_api():
            if not email:
                raise XUIError("Client email is required for modern 3X-UI delete.")
            return self._post_modern_client_delete(inbound, email, allow_multi_scope=allow_multi_scope)
        try:
            return self._post_legacy_client_delete(inbound, api_identifier)
        except Exception as exc:
            if not self._endpoint_missing(exc):
                raise
            profile = self._compat_profile(live=True, write=True)
            if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                if not email:
                    raise XUIError("Client email is required for modern 3X-UI delete.") from exc
                return self._post_modern_client_delete(inbound, email, allow_multi_scope=allow_multi_scope)
            raise

    def _post_client_reset_traffic(self, inbound, email, *, allow_multi_scope=False):
        email = str(email or "").strip()
        if not email:
            raise XUIError("Client email is required for traffic reset.")
        if self._uses_modern_client_api():
            self._assert_modern_email_single_attachment(
                inbound,
                email,
                operation="traffic reset",
                allow_multi_scope=allow_multi_scope,
            )
            response = self.authenticated_json(
                "POST",
                f"/panel/api/clients/resetTraffic/{quote(email, safe='')}",
                headers={"Accept": "application/json"},
            )
            return self._ensure_success(response, "Panel rejected traffic reset.")
        try:
            response = self.authenticated_json(
                "POST",
                f"/panel/api/inbounds/{inbound.inbound_id}/resetClientTraffic/{quote(email, safe='')}",
                headers={"Accept": "application/json"},
            )
            return self._ensure_success(response, "Panel rejected traffic reset.")
        except Exception as exc:
            if not self._endpoint_missing(exc):
                raise
            profile = self._compat_profile(live=True, write=True)
            if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                self._assert_modern_email_single_attachment(
                    inbound,
                    email,
                    operation="traffic reset",
                    allow_multi_scope=allow_multi_scope,
                )
                response = self.authenticated_json(
                    "POST",
                    f"/panel/api/clients/resetTraffic/{quote(email, safe='')}",
                    headers={"Accept": "application/json"},
                )
                return self._ensure_success(response, "Panel rejected traffic reset.")
            raise

    def _login_request_with_retries(self, request_func):
        last_exception = None
        for attempt in range(1, PANEL_LOGIN_ATTEMPTS + 1):
            try:
                return request_func()
            except requests.RequestException as exc:
                last_exception = exc
                if attempt >= PANEL_LOGIN_ATTEMPTS:
                    raise
                logger.warning(
                    "Panel login request failed for panel %s; retrying (%s/%s).",
                    self.panel.pk,
                    attempt,
                    PANEL_LOGIN_ATTEMPTS,
                )
                time.sleep(min(attempt, 2))
        raise last_exception or XUIError("Panel login failed.", category="unknown")

    def _login_response_json(self, response, *, endpoint):
        try:
            return response.json()
        except ValueError as exc:
            raise XUIError(
                "Panel returned an invalid login response.",
                category="unexpected_response",
                http_status=getattr(response, "status_code", None),
                endpoint=endpoint,
                response_snippet=_safe_response_snippet(response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("unexpected_response"),
            ) from exc

    def _raise_login_http_error(self, response, *, endpoint):
        status_code = getattr(response, "status_code", None)
        category = _xui_http_category(status_code, endpoint=endpoint)
        if category == "http_403_csrf_required":
            message = "Panel login failed with HTTP 403; CSRF-aware login may be required."
        elif category == "http_401":
            message = "Panel login failed with HTTP 401."
        else:
            message = f"Panel login failed with HTTP {status_code}."
        raise XUIError(
            message,
            category=category,
            http_status=status_code,
            endpoint=endpoint,
            response_snippet=_safe_response_snippet(response, panel=self.panel),
            remediation_hint=_xui_remediation_hint(category),
        )

    def _legacy_login(self):
        response = self._login_request_with_retries(
            lambda: self.session.post(
                self._url("login"),
                data={"username": self.panel.username, "password": self.panel.password},
                timeout=self.login_timeout,
            )
        )
        if response.status_code != 200:
            self._raise_login_http_error(response, endpoint="login")
        payload = self._login_response_json(response, endpoint="login")
        if not payload.get("success"):
            raise XUIError(
                payload.get("msg") or "Panel login was rejected.",
                category="auth_failed",
                http_status=response.status_code,
                endpoint="login",
                response_snippet=_safe_response_snippet(response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("auth_failed"),
            )
        return payload

    def _csrf_login_headers(self, token=""):
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
            "Origin": self._origin(),
            "Referer": self._base_page_url(),
        }
        if token:
            headers["X-CSRF-Token"] = token
        return headers

    def _unsafe_request_headers(self, headers=None):
        merged = self._csrf_login_headers(self.csrf_token)
        merged["Content-Type"] = "application/json"
        merged.update(headers or {})
        merged["X-Requested-With"] = "XMLHttpRequest"
        merged["Accept"] = "application/json, text/plain, */*"
        merged["Origin"] = self._origin()
        merged["Referer"] = self._base_page_url()
        if self.csrf_token:
            merged["X-CSRF-Token"] = self.csrf_token
        return merged

    def _request_needs_csrf(self, method):
        return str(method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def _csrf_response_json(self, response, *, endpoint):
        if response.status_code != 200:
            self._raise_login_http_error(response, endpoint=endpoint)
        return self._login_response_json(response, endpoint=endpoint)

    def _csrf_login(self):
        self._login_request_with_retries(
            lambda: self.session.get(
                self._base_page_url(),
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=self.login_timeout,
            )
        )
        csrf_response = self.session.get(
            self._url("csrf-token"),
            headers=self._csrf_login_headers(),
            timeout=self.login_timeout,
        )
        csrf_payload = self._csrf_response_json(csrf_response, endpoint="csrf-token")
        token = csrf_payload.get("obj") if csrf_payload.get("success") else ""
        if not isinstance(token, str) or not token:
            raise XUIError(
                "Panel returned an invalid CSRF token response.",
                category="unexpected_response",
                http_status=csrf_response.status_code,
                endpoint="csrf-token",
                response_snippet=_safe_response_snippet(csrf_response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("unexpected_response"),
            )
        self.csrf_token = token

        two_factor_response = self.session.post(
            self._url("getTwoFactorEnable"),
            headers=self._csrf_login_headers(token),
            timeout=self.login_timeout,
        )
        two_factor_payload = self._csrf_response_json(two_factor_response, endpoint="getTwoFactorEnable")
        if not two_factor_payload.get("success"):
            raise XUIError(
                two_factor_payload.get("msg") or "Panel returned an invalid two-factor response.",
                category="unexpected_response",
                http_status=two_factor_response.status_code,
                endpoint="getTwoFactorEnable",
                response_snippet=_safe_response_snippet(two_factor_response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("unexpected_response"),
            )
        if two_factor_payload.get("obj") is True:
            raise XUIError(
                "Two-factor login is enabled for this panel account.",
                category="two_factor_required",
                http_status=two_factor_response.status_code,
                endpoint="getTwoFactorEnable",
                remediation_hint=_xui_remediation_hint("two_factor_required"),
            )

        login_response = self.session.post(
            self._url("login"),
            data={
                "username": self.panel.username,
                "password": self.panel.password,
                "twoFactorCode": "",
            },
            headers=self._csrf_login_headers(token),
            timeout=self.login_timeout,
        )
        login_payload = self._csrf_response_json(login_response, endpoint="login")
        if not login_payload.get("success"):
            raise XUIError(
                login_payload.get("msg") or "Panel login was rejected.",
                category="auth_failed",
                http_status=login_response.status_code,
                endpoint="login",
                response_snippet=_safe_response_snippet(login_response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("auth_failed"),
            )
        return login_payload

    def refresh_csrf_token(self):
        self._login_request_with_retries(
            lambda: self.session.get(
                self._base_page_url(),
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=self.login_timeout,
            )
        )
        csrf_response = self.session.get(
            self._url("csrf-token"),
            headers=self._csrf_login_headers(),
            timeout=self.login_timeout,
        )
        csrf_payload = self._csrf_response_json(csrf_response, endpoint="csrf-token")
        token = csrf_payload.get("obj") if csrf_payload.get("success") else ""
        if not isinstance(token, str) or not token:
            raise XUIError(
                "Panel returned an invalid CSRF token response.",
                category="unexpected_response",
                http_status=csrf_response.status_code,
                endpoint="csrf-token",
                response_snippet=_safe_response_snippet(csrf_response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("unexpected_response"),
            )
        self.csrf_token = token
        return token

    def login(self):
        if self._logged_in:
            return self.session
        try:
            self._legacy_login()
        except XUIError as exc:
            if exc.category != "http_403_csrf_required":
                raise
            self._csrf_login()
        self._logged_in = True
        return self.session

    def request_json(self, method, path, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout_seconds
        endpoint = str(path or "").lstrip("/")
        unsafe = self._request_needs_csrf(method)
        csrf_unsafe = bool(unsafe and (self.csrf_token or self._uses_modern_client_api()))
        if csrf_unsafe:
            if not self.csrf_token:
                self.refresh_csrf_token()
            kwargs["headers"] = self._unsafe_request_headers(kwargs.get("headers"))
        response = self._login_request_with_retries(lambda: self.session.request(method, self._url(path), **kwargs))
        if csrf_unsafe and response.status_code == 403:
            self.refresh_csrf_token()
            kwargs["headers"] = self._unsafe_request_headers(kwargs.get("headers"))
            response = self._login_request_with_retries(lambda: self.session.request(method, self._url(path), **kwargs))
        if response.status_code != 200:
            category = _xui_http_category(response.status_code, endpoint=endpoint)
            if csrf_unsafe and response.status_code == 403:
                category = "write_api_forbidden"
            raise XUIError(
                f"Panel request failed with HTTP {response.status_code}.",
                category=category,
                http_status=response.status_code,
                endpoint=endpoint,
                response_snippet=_safe_response_snippet(response, panel=self.panel),
                remediation_hint=_xui_remediation_hint(category),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise XUIError(
                "Panel returned invalid JSON.",
                category="unexpected_response",
                http_status=response.status_code,
                endpoint=str(path or "").lstrip("/"),
                response_snippet=_safe_response_snippet(response, panel=self.panel),
                remediation_hint=_xui_remediation_hint("unexpected_response"),
            ) from exc

    def authenticated_json(self, method, path, **kwargs):
        self.login()
        return self.request_json(method, path, **kwargs)

    def get_inbound(self, inbound_id, *, use_cache=True):
        remote_inbound_id = self._inbound_id(inbound_id)
        cache_key = self._inbound_cache_key(inbound_id)
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        data = self.authenticated_json("GET", f"/panel/api/inbounds/get/{remote_inbound_id}")
        if not data.get("success"):
            raise XUIError(data.get("msg") or "Inbound was not found on panel.")
        inbound_data = data.get("obj") or {}
        cache.set(cache_key, inbound_data, CLIENT_STATS_CACHE_SECONDS)
        return inbound_data

    def get_inbound_clients(self, inbound_id, *, use_cache=True):
        inbound_data = self.get_inbound(inbound_id, use_cache=use_cache)
        try:
            settings = json.loads(inbound_data.get("settings") or "{}")
        except (TypeError, ValueError):
            settings = {}
        return settings.get("clients", [])

    def find_client_by_identifier(self, identifier):
        from .models import Inbound

        identifier = str(identifier or "").strip()
        if not identifier:
            return None

        inbounds = list(
            Inbound.objects.filter(panel=self.panel, is_active=True).order_by("inbound_id")
        )
        inbound_errors = []
        successful_inbound_reads = 0

        for inbound in inbounds:
            try:
                inbound_data = self.get_inbound(inbound, use_cache=False)
                successful_inbound_reads += 1
            except Exception as exc:
                safe_error = sanitize_xui_operational_text(exc, panel=self.panel)
                inbound_errors.append(f"{inbound.inbound_id}: {safe_error}")
                logger.warning(
                    "Could not read inbound during config lookup panel=%s inbound=%s error=%s",
                    self.panel.pk,
                    inbound.inbound_id,
                    safe_error,
                )
                continue

            target_client, target_stats, matched_field, _clients, _client_stats = find_xui_client_and_stats(
                inbound_data,
                identifier,
            )

            if not target_client and not target_stats:
                continue

            target_client = target_client or {}
            target_stats = target_stats or {}
            email = str(target_client.get("email") or target_stats.get("email") or "").strip()
            if email and not has_usage_stats(target_stats):
                try:
                    traffic = self.get_client_traffic(email, use_cache=False)
                except Exception as exc:
                    safe_error = sanitize_xui_operational_text(exc, panel=self.panel)
                    logger.warning(
                        "Could not read client traffic during config lookup panel=%s inbound=%s email=%s error=%s",
                        self.panel.pk,
                        inbound.inbound_id,
                        mask_xui_value(email),
                        safe_error,
                    )
                else:
                    if isinstance(traffic, dict):
                        target_stats = traffic
            remark = str(
                target_client.get("remark")
                or target_client.get("name")
                or target_stats.get("remark")
                or ""
            ).strip()
            usage_source = target_stats if has_usage_stats(target_stats) else target_client if has_usage_stats(target_client) else {}
            stats_available = bool(usage_source)
            upload = None
            download = None
            used = None
            if stats_available:
                upload = xui_int(first_xui_value(usage_source.get("up"), usage_source.get("upload")), 0)
                download = xui_int(first_xui_value(usage_source.get("down"), usage_source.get("download")), 0)
                used = xui_int_or_none(
                    first_xui_value(
                        usage_source.get("used"),
                        usage_source.get("usedTraffic"),
                        usage_source.get("used_traffic"),
                    )
                )
                if used is None:
                    used = upload + download

            total = xui_int(
                first_xui_value(
                    target_client.get("totalGB"),
                    target_client.get("total"),
                    target_stats.get("total"),
                    target_stats.get("totalGB"),
                    inbound_data.get("totalGB"),
                    inbound_data.get("total"),
                ),
                0,
            )
            expiry_at = parse_xui_datetime(
                first_xui_value(target_client.get("expiryTime"), target_stats.get("expiryTime"))
            )
            last_online_at = parse_xui_datetime(
                first_xui_value(
                    target_stats.get("lastOnline"),
                    target_stats.get("lastOnlineTime"),
                    target_stats.get("last_online_at"),
                    target_client.get("lastOnline"),
                )
            )
            remaining = max(total - used, 0) if total and used is not None else None
            if not total:
                remaining = 0
            enabled_value = first_xui_value(target_client.get("enable"), target_stats.get("enable"))
            enabled = xui_bool(enabled_value) if enabled_value is not None else True
            is_expired = bool(expiry_at and expiry_at <= timezone.now()) or bool(
                total and remaining == 0 and used is not None
            )

            return {
                "panel": self.panel,
                "panel_id": self.panel.pk,
                "panel_name": self.panel.name,
                "inbound": inbound,
                "inbound_id": inbound.inbound_id,
                "node_id": getattr(inbound, "xui_node_id", "") or "",
                "node_name": getattr(inbound, "xui_node_name", "") or "",
                "remote_key": inbound_remote_key(inbound, panel=self.panel),
                "inbound_remark": inbound.remark or inbound_data.get("remark") or "",
                "protocol": (inbound_data.get("protocol") or inbound.protocol or "").lower(),
                "identifier": identifier,
                "client": target_client,
                "client_stats": target_stats,
                "matched_field": matched_field,
                "email": email,
                "remark": remark,
                "enabled": enabled,
                "total_bytes": total,
                "used_bytes": used,
                "remaining_bytes": remaining,
                "upload_bytes": upload,
                "download_bytes": download,
                "expiry_time": expiry_at,
                "total_traffic_bytes": total,
                "used_upload_bytes": upload,
                "used_download_bytes": download,
                "used_traffic_bytes": used,
                "remaining_traffic_bytes": remaining,
                "expiry_at": expiry_at,
                "last_online_at": last_online_at,
                "is_enabled": enabled,
                "is_expired": is_expired,
                "stats_available": stats_available,
                "config_link_updated": False,
                "raw": {
                    "client": {
                        "id": mask_xui_value(target_client.get("id")),
                        "email": mask_xui_value(email),
                        "subId": mask_xui_value(target_client.get("subId") or target_client.get("sub_id")),
                        "has_password": bool(target_client.get("password")),
                    },
                    "client_stats": {
                        "email": mask_xui_value(target_stats.get("email")),
                        "id": mask_xui_value(target_stats.get("id")),
                        "has_usage": stats_available,
                    },
                    "inbound": {
                        "id": inbound_data.get("id"),
                        "remark": inbound_data.get("remark"),
                        "protocol": inbound_data.get("protocol"),
                    },
                },
            }

        if inbound_errors and not successful_inbound_reads:
            raise XUIError(
                "Could not read active inbounds for panel: " + "; ".join(inbound_errors[:3])
            )
        return None

    def get_client_traffic(self, email, *, use_cache=True):
        normalized_email = email or ""
        cache_key = f"xui:client-traffic:{self.panel.pk}:{normalized_email}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        paths = (
            [f"/panel/api/clients/traffic/{quote(normalized_email, safe='')}"]
            if self._uses_modern_client_api()
            else [
                f"/panel/api/inbounds/getClientTraffics/{quote(normalized_email, safe='')}",
                f"/panel/api/clients/traffic/{quote(normalized_email, safe='')}",
            ]
        )
        last_error = None
        data = None
        for path in paths:
            try:
                data = self.authenticated_json("GET", path)
                break
            except Exception as exc:
                last_error = exc
                if not self._endpoint_missing(exc):
                    raise
                profile = self._compat_profile(live=True, write=True)
                if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                    continue
                raise
        if data is None:
            raise XUIError("Client traffic was not found.") from last_error
        self._ensure_success(data, "Client traffic was not found.")
        traffic = data.get("obj") or {}
        cache.set(cache_key, traffic, CLIENT_STATS_CACHE_SECONDS)
        return traffic

    def get_online_clients(self, *, suppress_errors=True):
        paths = (
            ["/panel/api/clients/onlines"]
            if self._uses_modern_client_api()
            else ["/panel/api/inbounds/onlines", "/panel/api/clients/onlines"]
        )
        data = None
        last_error = None
        try:
            for path in paths:
                try:
                    data = self.authenticated_json("POST", path)
                    break
                except Exception as exc:
                    last_error = exc
                    if not self._endpoint_missing(exc):
                        raise
                    profile = self._compat_profile(live=True, write=True)
                    if profile.profile not in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
                        raise
            if data is None:
                raise XUIError("Online-client endpoint was not available.") from last_error
        except Exception:
            if suppress_errors:
                return set()
            raise

        obj = data.get("obj") or data.get("online") or []
        if isinstance(obj, str):
            obj = [obj]
        return {str(item) for item in obj if item}

    def build_sub_base_url(self, inbound_data=None):
        sub_settings = {}
        if inbound_data:
            try:
                sub_settings = json.loads(inbound_data.get("subSettings") or "{}")
            except (TypeError, ValueError):
                sub_settings = {}
        parsed_url = urlparse(self.panel.url)
        hostname = parsed_url.hostname or parsed_url.netloc
        sub_port = sub_settings.get("subPort") or 2096
        return f"{parsed_url.scheme}://{hostname}:{sub_port}"

    def build_direct_link(self, *, inbound, inbound_data, client_uuid, client_data, email, hosts=None):
        resolve_inbound_panel(inbound, self.panel, require_active=False)
        client_data = dict(client_data or {})
        for target_key, inbound_attr in (
            ("pbk", "pbk"),
            ("fingerprint", "fingerprint"),
            ("sni", "sni"),
            ("sid", "sid"),
        ):
            if not client_data.get(target_key):
                fallback_value = getattr(inbound, inbound_attr, "") or ""
                if fallback_value:
                    client_data[target_key] = fallback_value
        native_link = native_config_link_from_sources(client_data, inbound_data)
        if native_link:
            return native_link
        protocol = (inbound_data.get("protocol") or inbound.protocol or "vless").lower()
        share_strategy = str(inbound_data.get("shareAddrStrategy") or "").strip()
        share_address = str(inbound_data.get("shareAddr") or "").strip()
        listen_address = str(inbound_data.get("listen") or "").strip()
        address = inbound.server_ip or urlparse(self.panel.url).hostname or ""
        if share_strategy == "custom" and share_address:
            address = share_address
        elif share_strategy == "listen" and listen_address and listen_address not in {"0.0.0.0", "::", "::0"}:
            address = listen_address
        port = str(inbound_data.get("port") or inbound.port).strip()
        remark = email or client_data.get("remark") or client_data.get("email") or ""

        stream_settings = parse_xui_json_object(inbound_data.get("streamSettings") or {})
        if inbound_data.get("realitySettings") and not stream_settings.get("realitySettings"):
            stream_settings["realitySettings"] = inbound_data.get("realitySettings")

        host = next(
            (
                item
                for item in (hosts or [])
                if isinstance(item, dict)
                and not item.get("isDisabled")
                and not item.get("isHidden")
                and str(item.get("address") or "").strip()
            ),
            None,
        )
        if host:
            address = str(host.get("address") or address).strip()
            if host.get("port"):
                port = str(host.get("port"))
            if host.get("sni"):
                stream_settings.setdefault("tlsSettings", {})["serverName"] = str(host.get("sni"))
            if host.get("hostHeader"):
                stream_settings.setdefault("wsSettings", {}).setdefault("headers", {})["Host"] = str(host.get("hostHeader"))
            if host.get("path"):
                stream_settings.setdefault("wsSettings", {})["path"] = str(host.get("path"))

        if protocol == "vmess":
            vmess_client = dict(client_data or {})
            vmess_client["id"] = str(vmess_client.get("id") or client_uuid or "")
            return encode_vmess_link(
                build_vmess_payload(
                    address=address,
                    port=port,
                    stream_settings=stream_settings,
                    client_data=vmess_client,
                    remark=remark,
                )
            )

        if protocol == "trojan":
            password = str(client_data.get("password") or client_uuid or "").strip()
            params = build_trojan_query_params(stream_settings, client_data)
            if not params:
                params = inbound.config_params or "security=none&type=tcp"
            return f"trojan://{quote(password, safe='')}@{address}:{port}?{params}#{quote(remark, safe='')}"

        params = build_vless_query_params(stream_settings, client_data)
        if not params:
            params = inbound.config_params or "type=tcp&security=none"
        return f"vless://{client_uuid}@{address}:{port}?{params}#{quote(email, safe='')}"

    def get_hosts_for_inbound(self, inbound_id, *, suppress_errors=True):
        try:
            data = self.authenticated_json("GET", f"/panel/api/hosts/byInbound/{inbound_id}")
        except Exception:
            if suppress_errors:
                return []
            raise
        obj = data.get("obj") if isinstance(data, dict) else []
        return obj if isinstance(obj, list) else []

    def build_config_link_for_identifier(self, inbound_id, identifier, *, inbound=None, node_id=""):
        from .models import Inbound

        identifier = str(identifier or "").strip()
        if not identifier:
            raise XUIError("Client identifier is required.")

        if inbound is None:
            queryset = Inbound.objects.filter(
                panel=self.panel,
                inbound_id=inbound_id,
                is_active=True,
            )
            if node_id:
                queryset = queryset.filter(xui_node_id=str(node_id))
            matches = list(queryset.order_by("pk")[:2])
            if len(matches) > 1:
                raise XUIAmbiguousScopeError("Inbound lookup is ambiguous without node scope.")
            inbound = matches[0] if matches else None
        if not inbound:
            raise XUIError("Active inbound was not found for config update.")

        inbound_data = self.get_inbound(inbound, use_cache=False)
        target_client, target_stats, matched_field, _clients, _client_stats = find_xui_client_and_stats(
            inbound_data,
            identifier,
        )
        if not target_client:
            raise XUIError("Client settings were not found on the panel.")

        protocol = (inbound_data.get("protocol") or inbound.protocol or "vless").lower()
        email = str(target_client.get("email") or target_stats.get("email") or "").strip()
        remark = str(target_client.get("remark") or target_client.get("name") or email or "").strip()
        if protocol == "trojan":
            client_secret = str(target_client.get("password") or identifier).strip()
        else:
            client_secret = str(target_client.get("id") or target_stats.get("id") or identifier).strip()
        if not client_secret:
            raise XUIError("Client identifier was not available for link generation.")
        enabled_value = first_xui_value(target_client.get("enable"), target_stats.get("enable"))
        enabled = xui_bool(enabled_value) if enabled_value is not None else True

        hosts = self.get_hosts_for_inbound(inbound.inbound_id)
        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_secret,
            client_data=target_client,
            email=remark or email,
            hosts=hosts,
        )
        return {
            "updated_config_link": direct_link,
            "protocol": protocol,
            "remark": remark,
            "email": email,
            "enabled": enabled,
            "panel": self.panel,
            "inbound": inbound,
            "inbound_id": inbound.inbound_id,
            "node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(inbound, panel=self.panel),
            "matched_field": matched_field,
            "config_link_updated": True,
        }

    def _find_client_in_inbound(self, inbound, identifier):
        resolve_inbound_panel(inbound, self.panel, require_active=False)
        identifier = str(identifier or "").strip()
        if not identifier:
            raise XUIError("Client identifier is required.")
        inbound_data = self.get_inbound(inbound, use_cache=False)
        target_client, target_stats, matched_field, clients, client_stats = find_xui_client_and_stats(
            inbound_data,
            identifier,
        )
        if not target_client and not target_stats:
            raise XUIError("Client identifier was not found on this inbound.")
        return {
            "inbound_data": inbound_data,
            "client": target_client or {},
            "client_stats": target_stats or {},
            "matched_field": matched_field,
            "clients": clients,
            "all_client_stats": client_stats,
        }

    def _client_api_identifier(self, client_data, identifier):
        for key in ("id", "uuid", "password"):
            value = str((client_data or {}).get(key) or "").strip()
            if value:
                return value
        return str(identifier or "").strip()

    def _clear_client_caches(self, inbound, client_data=None, email=""):
        cache.delete(self._inbound_cache_key(inbound))
        for value in {email, (client_data or {}).get("email")}:
            value = str(value or "").strip()
            if value:
                cache.delete(f"xui:client-traffic:{self.panel.pk}:{value}")

    def _try_delete_client_stats(self, inbound, email):
        email = str(email or "").strip()
        if not email:
            return False
        try:
            response = self.authenticated_json(
                "POST",
                f"/panel/api/inbounds/{inbound.inbound_id}/delClientTraffic/{quote(email)}",
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            safe_error = sanitize_xui_operational_text(exc, panel=self.panel)
            logger.info(
                "Best-effort X-UI client traffic deletion skipped panel=%s inbound=%s email=%s error=%s",
                self.panel.pk,
                inbound.inbound_id,
                mask_xui_value(email),
                safe_error,
            )
            return False
        return bool(response.get("success"))

    def delete_client_from_inbound(self, inbound, identifier, *, allow_multi_scope=False):
        found = self._find_client_in_inbound(inbound, identifier)
        target_client = dict(found.get("client") or {})
        target_stats = dict(found.get("client_stats") or {})
        email = str(target_client.get("email") or target_stats.get("email") or "").strip()
        api_identifier = self._client_api_identifier(target_client, identifier)
        if not api_identifier:
            raise XUIError("Client identifier was not available for deletion.")
        self._assert_precise_scope(
            inbound,
            api_identifier,
            operation="delete",
            allow_multi_scope=allow_multi_scope,
        )

        response = self._post_client_delete(
            inbound,
            api_identifier,
            email=email,
            allow_multi_scope=allow_multi_scope,
        )
        if not response.get("success"):
            message = str(response.get("msg") or "")
            if "not found" not in message.lower() and "不存在" not in message:
                raise XUIError(message or "Panel rejected client deletion.")

        stats_deleted = self._try_delete_client_stats(inbound, email)
        self._clear_client_caches(inbound, target_client, email=email)

        inbound_data = self.get_inbound(inbound, use_cache=False)
        try:
            remaining_client, remaining_stats, _matched, _clients, _stats = find_xui_client_and_stats(
                inbound_data,
                identifier,
            )
        except Exception as exc:
            raise XUIError("Could not verify client deletion on panel.") from exc
        if remaining_client:
            raise XUIError("Panel deletion could not be verified.")

        return {
            "deleted": True,
            "stats_deleted": stats_deleted,
            "stats_remaining": bool(remaining_stats),
            "panel": self.panel,
            "inbound": inbound,
            "inbound_id": inbound.inbound_id,
            "node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(inbound, panel=self.panel),
            "identifier": identifier,
            "email": email,
            "matched_field": found.get("matched_field") or "",
            "old_total_bytes": xui_int(
                first_xui_value(
                    target_client.get("totalGB"),
                    target_client.get("total"),
                    target_stats.get("total"),
                    target_stats.get("totalGB"),
                ),
                0,
            ),
            "old_expiry_time": parse_xui_datetime(
                first_xui_value(target_client.get("expiryTime"), target_stats.get("expiryTime"))
            ),
            "raw": {
                "client": target_client,
                "client_stats": target_stats,
            },
        }

    def update_client_traffic_and_expiry(
        self,
        inbound,
        identifier,
        *,
        total_bytes=None,
        expiry_time=None,
        enable=None,
        allow_multi_scope=False,
    ):
        found = self._find_client_in_inbound(inbound, identifier)
        target_client = dict(found.get("client") or {})
        target_stats = dict(found.get("client_stats") or {})
        if not target_client:
            raise XUIError("Client settings were not found on the panel.")

        api_identifier = self._client_api_identifier(target_client, identifier)
        if not api_identifier:
            raise XUIError("Client identifier was not available for update.")
        self._assert_precise_scope(
            inbound,
            api_identifier,
            operation="update",
            allow_multi_scope=allow_multi_scope,
        )

        old_total = xui_int(
            first_xui_value(
                target_client.get("totalGB"),
                target_client.get("total"),
                target_stats.get("total"),
                target_stats.get("totalGB"),
            ),
            0,
        )
        old_expiry = parse_xui_datetime(first_xui_value(target_client.get("expiryTime"), target_stats.get("expiryTime")))

        if total_bytes is not None:
            total_bytes = int(total_bytes)
            if total_bytes < 0:
                raise XUIError("Traffic limit cannot be negative.")
            target_client["totalGB"] = total_bytes
        expiry_millis = xui_datetime_to_millis(expiry_time) if expiry_time is not None else None
        if expiry_millis is not None:
            target_client["expiryTime"] = expiry_millis
        if enable is not None:
            target_client["enable"] = bool(enable)

        email = str(target_client.get("email") or target_stats.get("email") or "").strip()
        self._post_client_update(inbound, api_identifier, target_client, email=email)

        self._clear_client_caches(inbound, target_client)
        verified = self._find_client_in_inbound(inbound, identifier)
        verified_client = verified.get("client") or {}
        verified_stats = verified.get("client_stats") or {}
        verified_total = xui_int(
            first_xui_value(
                verified_client.get("totalGB"),
                verified_client.get("total"),
                verified_stats.get("total"),
                verified_stats.get("totalGB"),
            ),
            0,
        )
        verified_expiry = parse_xui_datetime(
            first_xui_value(verified_client.get("expiryTime"), verified_stats.get("expiryTime"))
        )
        verified_enable_value = first_xui_value(verified_client.get("enable"), verified_stats.get("enable"))
        verified_enable = xui_bool(verified_enable_value) if verified_enable_value is not None else None

        if total_bytes is not None and verified_total != total_bytes:
            raise XUIError("Panel traffic update could not be verified.")
        if expiry_millis is not None:
            expected_expiry = parse_xui_datetime(expiry_millis)
            if bool(expected_expiry) != bool(verified_expiry):
                raise XUIError("Panel expiry update could not be verified.")
            if expected_expiry and verified_expiry:
                delta = abs((verified_expiry - expected_expiry).total_seconds())
                if delta > 2:
                    raise XUIError("Panel expiry update could not be verified.")
        if enable is not None and verified_enable is not None and verified_enable != bool(enable):
            raise XUIError("Panel enabled-state update could not be verified.")

        return {
            "updated": True,
            "panel": self.panel,
            "inbound": inbound,
            "inbound_id": inbound.inbound_id,
            "node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(inbound, panel=self.panel),
            "identifier": identifier,
            "matched_field": verified.get("matched_field") or found.get("matched_field") or "",
            "old_total_bytes": old_total,
            "new_total_bytes": verified_total,
            "old_expiry_time": old_expiry,
            "new_expiry_time": verified_expiry,
            "enabled": verified_enable,
            "email": str(verified_client.get("email") or verified_stats.get("email") or target_client.get("email") or "").strip(),
            "raw": {
                "client": verified_client,
                "client_stats": verified_stats,
            },
        }

    def get_client_config_details(self, vpn_client):
        if not vpn_client.inbound_id or not vpn_client.inbound.panel_id:
            raise XUIError("VPN client is not linked to a panel inbound.")
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=False)

        inbound_data = self.get_inbound(vpn_client.inbound, use_cache=False)
        try:
            settings = json.loads(inbound_data.get("settings") or "{}")
        except (TypeError, ValueError) as exc:
            raise XUIError("Inbound settings could not be parsed.") from exc

        email = vpn_client.xui_email or vpn_client.username
        clients = settings.get("clients", [])
        target_client = next(
            (
                client
                for client in clients
                if client.get("id") == str(vpn_client.uuid)
                or client.get("email") == email
            ),
            None,
        )
        if not target_client:
            raise XUIError("Client UUID was not found on the panel.")

        client_uuid = str(target_client.get("id") or vpn_client.uuid or "")
        client_email = target_client.get("email") or email
        sub_id = target_client.get("subId") or target_client.get("sub_id") or vpn_client.sub_id
        hosts = self.get_hosts_for_inbound(vpn_client.inbound.inbound_id)
        direct_link = self.build_direct_link(
            inbound=vpn_client.inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=target_client,
            email=client_email,
            hosts=hosts,
        )
        sub_link = f"{self.build_sub_base_url(inbound_data)}/sub/{sub_id}" if sub_id else vpn_client.sub_link
        return {
            "uuid": client_uuid,
            "email": client_email,
            "sub_id": sub_id or "",
            "sub_link": sub_link,
            "direct_link": direct_link,
            "raw": target_client,
        }

    def create_inactive_client(self, *, email_prefix, total_gb, expire_days, inbound, limit_ip=2):
        resolve_inbound_panel(inbound, self.panel, require_active=True)
        self._assert_precise_scope(inbound, operation="create")
        self.login()
        client_uuid = str(uuid.uuid4())
        sub_id = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        email = build_xui_client_email(email_prefix, client_uuid)
        total_bytes = bytes_from_gb(total_gb)

        client_data = {
            "id": client_uuid,
            "alterId": 0,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": 0,
            "enable": False,
            "tgId": 0,
            "subId": sub_id,
        }
        create_response = self._post_client_create(inbound, client_data)
        create_obj = _response_obj_mapping(create_response)
        create_client = create_obj.get("client") if isinstance(create_obj.get("client"), dict) else {}

        inbound_data = self.get_inbound(inbound, use_cache=False)
        remote_client = {**client_data, **create_obj, **create_client}
        hosts = self.get_hosts_for_inbound(inbound.inbound_id)
        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=remote_client,
            email=email,
            hosts=hosts,
        )
        sub_base_url = self.build_sub_base_url(inbound_data)
        remote_client_key = build_remote_client_key(self.panel, inbound, client_uuid)

        return {
            "uuid": client_uuid,
            "email": email,
            "sub_id": sub_id,
            "sub_link": f"{sub_base_url}/sub/{sub_id}",
            "direct_link": direct_link,
            "xui_node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_client_key": remote_client_key,
            "remote_scope": {
                "panel_id": getattr(self.panel, "pk", None),
                "inbound_id": inbound.inbound_id,
                "node_id": getattr(inbound, "xui_node_id", "") or "",
                "inbound_remote_key": inbound_remote_key(inbound, panel=self.panel),
            },
            "raw": remote_client,
        }

    def create_enabled_client(
        self,
        *,
        email_prefix,
        total_gb,
        duration_hours,
        inbound,
        limit_ip=1,
        client_uuid="",
        sub_id="",
        email="",
    ):
        resolve_inbound_panel(inbound, self.panel, require_active=True)
        self._assert_precise_scope(inbound, operation="create")
        self.login()
        client_uuid = str(client_uuid or uuid.uuid4())
        sub_id = str(sub_id or "".join(random.choices(string.ascii_letters + string.digits, k=16)))
        email = str(email or build_xui_client_email(email_prefix, client_uuid)).strip()
        total_bytes = bytes_from_gb(total_gb)
        expiry_time = int(time.time() * 1000) + (int(duration_hours) * 3_600_000)

        client_data = {
            "id": client_uuid,
            "alterId": 0,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": 0,
            "subId": sub_id,
        }
        create_response = self._post_client_create(inbound, client_data)
        create_obj = _response_obj_mapping(create_response)
        create_client = create_obj.get("client") if isinstance(create_obj.get("client"), dict) else {}

        inbound_data = self.get_inbound(inbound, use_cache=False)
        target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, client_uuid)
        if not target_client and not target_stats and email:
            target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, email)
        if not target_client and not target_stats:
            raise XUIError("Client creation could not be verified on panel.")
        enabled_value = first_xui_value((target_client or {}).get("enable"), (target_stats or {}).get("enable"))
        if enabled_value is not None and not xui_bool(enabled_value):
            raise XUIError("Client was created but is not enabled on panel.")
        remote_client = {**client_data, **create_obj, **create_client, **(target_client or {})}
        hosts = self.get_hosts_for_inbound(inbound.inbound_id)
        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=remote_client,
            email=email,
            hosts=hosts,
        )
        sub_base_url = self.build_sub_base_url(inbound_data)
        remote_client_key = build_remote_client_key(self.panel, inbound, client_uuid)

        return {
            "uuid": client_uuid,
            "email": email,
            "sub_id": sub_id,
            "sub_link": f"{sub_base_url}/sub/{sub_id}",
            "direct_link": direct_link,
            "expires_at": parse_xui_datetime(expiry_time),
            "xui_node_id": getattr(inbound, "xui_node_id", "") or "",
            "remote_client_key": remote_client_key,
            "remote_scope": {
                "panel_id": getattr(self.panel, "pk", None),
                "inbound_id": inbound.inbound_id,
                "node_id": getattr(inbound, "xui_node_id", "") or "",
                "inbound_remote_key": inbound_remote_key(inbound, panel=self.panel),
            },
            "raw": remote_client,
        }

    def create_enabled_multi_inbound_client(
        self,
        *,
        email_prefix,
        total_gb,
        duration_hours,
        inbounds,
        limit_ip=1,
        client_uuid="",
        sub_id="",
        email="",
    ):
        inbounds = [inbound for inbound in (inbounds or []) if inbound]
        if not inbounds:
            raise XUIError("At least one inbound is required for multi-inbound create.")
        panel_ids = {getattr(inbound, "panel_id", None) for inbound in inbounds}
        if panel_ids != {getattr(self.panel, "pk", None)}:
            raise XUIError("All bundle inbounds must belong to the same panel.")
        for inbound in inbounds:
            resolve_inbound_panel(inbound, self.panel, require_active=True)
            self._assert_precise_scope(inbound, operation="create")
        profile = self._compat_profile(live=False)
        if profile.profile != PROFILE_MODERN_MULTI_NODE:
            raise XUICompatibilityError("Multi-inbound create requires a modern multi-node panel.")

        self.login()
        client_uuid = str(client_uuid or uuid.uuid4())
        sub_id = str(sub_id or "".join(random.choices(string.ascii_letters + string.digits, k=16)))
        email = str(email or build_xui_client_email(email_prefix, client_uuid)).strip()
        total_bytes = bytes_from_gb(total_gb)
        expiry_time = int(time.time() * 1000) + (int(duration_hours) * 3_600_000)
        client_data = {
            "id": client_uuid,
            "alterId": 0,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": 0,
            "subId": sub_id,
        }
        inbound_ids = [int(inbound.inbound_id) for inbound in inbounds]
        create_response = self._post_modern_client_create(inbounds[0], client_data, inbound_ids=inbound_ids)
        create_obj = _response_obj_mapping(create_response)
        create_client = create_obj.get("client") if isinstance(create_obj.get("client"), dict) else {}

        per_inbound = []
        sub_link = ""
        for inbound in inbounds:
            inbound_data = self.get_inbound(inbound, use_cache=False)
            target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, client_uuid)
            if not target_client and not target_stats and email:
                target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, email)
            if not target_client and not target_stats:
                raise XUIError("Client creation could not be verified on every bundle inbound.")
            enabled_value = first_xui_value((target_client or {}).get("enable"), (target_stats or {}).get("enable"))
            if enabled_value is not None and not xui_bool(enabled_value):
                raise XUIError("Client was created but is not enabled on every bundle inbound.")
            remote_client = {**client_data, **create_obj, **create_client, **(target_client or {})}
            hosts = self.get_hosts_for_inbound(inbound.inbound_id)
            direct_link = self.build_direct_link(
                inbound=inbound,
                inbound_data=inbound_data,
                client_uuid=client_uuid,
                client_data=remote_client,
                email=email,
                hosts=hosts,
            )
            sub_link = sub_link or f"{self.build_sub_base_url(inbound_data)}/sub/{sub_id}"
            per_inbound.append(
                {
                    "inbound_pk": getattr(inbound, "pk", None),
                    "uuid": client_uuid,
                    "email": email,
                    "sub_id": sub_id,
                    "sub_link": f"{self.build_sub_base_url(inbound_data)}/sub/{sub_id}",
                    "direct_link": direct_link,
                    "expires_at": parse_xui_datetime(expiry_time),
                    "xui_node_id": getattr(inbound, "xui_node_id", "") or "",
                    "remote_client_key": build_remote_client_key(self.panel, inbound, client_uuid),
                    "remote_scope": {
                        "panel_id": getattr(self.panel, "pk", None),
                        "inbound_id": inbound.inbound_id,
                        "node_id": getattr(inbound, "xui_node_id", "") or "",
                        "inbound_remote_key": inbound_remote_key(inbound, panel=self.panel),
                    },
                    "raw": remote_client,
                }
            )

        primary = per_inbound[0]
        return {
            **primary,
            "sub_link": sub_link or primary.get("sub_link", ""),
            "bundle_inbound_results": per_inbound,
            "bundle_inbound_ids": inbound_ids,
            "raw": {**client_data, "bundle_inbound_ids": inbound_ids},
        }

    def update_client_enabled(self, order):
        resolve_inbound_panel(order.inbound, self.panel, require_active=True)
        self._assert_precise_scope(order.inbound, getattr(order, "uuid", ""), operation="enable")
        inbound_data = self.get_inbound(order.inbound, use_cache=False)
        try:
            settings = json.loads(inbound_data.get("settings") or "{}")
        except (TypeError, ValueError) as exc:
            raise XUIError("Inbound settings could not be parsed.") from exc

        clients = settings.get("clients", [])
        target_client = next(
            (client for client in clients if client.get("id") == str(order.uuid)),
            None,
        )
        if not target_client:
            raise XUIError("Client UUID was not found on the panel.")

        current_time = int(time.time() * 1000)
        target_client["enable"] = True
        target_client["expiryTime"] = current_time + (order.plan.duration_days * 86_400_000)

        email = str(target_client.get("email") or getattr(order, "username", "") or "").strip()
        self._post_client_update(order.inbound, str(order.uuid), target_client, email=email)
        return True

    def update_client_subscription(self, vpn_client, plan):
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=True)
        self._assert_precise_scope(vpn_client.inbound, getattr(vpn_client, "uuid", ""), operation="renew")
        inbound_data = self.get_inbound(vpn_client.inbound, use_cache=False)
        try:
            settings = json.loads(inbound_data.get("settings") or "{}")
        except (TypeError, ValueError) as exc:
            raise XUIError("Inbound settings could not be parsed.") from exc

        clients = settings.get("clients", [])
        target_client = next(
            (
                client
                for client in clients
                if client.get("id") == str(vpn_client.uuid)
                or client.get("email") == (vpn_client.xui_email or vpn_client.username)
            ),
            None,
        )
        if not target_client:
            raise XUIError("Client UUID was not found on the panel.")

        current_time = int(time.time() * 1000)
        current_expiry = int(target_client.get("expiryTime") or 0)
        expiry_base = max(current_time, current_expiry)
        target_client["enable"] = True
        target_client["totalGB"] = bytes_from_gb(plan.volume_gb)
        target_client["limitIp"] = plan.device_limit
        target_client["expiryTime"] = expiry_base + (plan.duration_days * 86_400_000)

        email = vpn_client.xui_email or vpn_client.username
        self._post_client_update(vpn_client.inbound, str(vpn_client.uuid), target_client, email=email)

        self._post_client_reset_traffic(vpn_client.inbound, email)

        cache.delete(self._inbound_cache_key(vpn_client.inbound))
        cache.delete(f"xui:client-traffic:{self.panel.pk}:{email}")
        cache.delete(f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}")
        return {
            "expiry_at": parse_xui_datetime(target_client["expiryTime"]),
            "node_id": getattr(vpn_client.inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(vpn_client.inbound, panel=self.panel),
            "raw": target_client,
        }

    def add_client_traffic(self, vpn_client, extra_gb, *, extra_days=0):
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=True)
        self._assert_precise_scope(vpn_client.inbound, getattr(vpn_client, "uuid", ""), operation="update")
        inbound_data = self.get_inbound(vpn_client.inbound, use_cache=False)
        try:
            settings = json.loads(inbound_data.get("settings") or "{}")
        except (TypeError, ValueError) as exc:
            raise XUIError("Inbound settings could not be parsed.") from exc

        email = vpn_client.xui_email or vpn_client.username
        clients = settings.get("clients", [])
        target_client = next(
            (
                client
                for client in clients
                if client.get("id") == str(vpn_client.uuid)
                or client.get("email") == email
            ),
            None,
        )
        if not target_client:
            raise XUIError("Client UUID was not found on the panel.")

        current_total = int(target_client.get("totalGB") or vpn_client.traffic_limit_bytes or 0)
        new_total = current_total + bytes_from_gb(extra_gb)
        target_client["totalGB"] = new_total
        current_expiry = int(target_client.get("expiryTime") or 0)
        expiry_unlimited = current_expiry == 0
        if extra_days and not expiry_unlimited:
            current_time = int(time.time() * 1000)
            expiry_base = max(current_time, current_expiry)
            target_client["expiryTime"] = expiry_base + (int(extra_days) * 86_400_000)

        self._post_client_update(vpn_client.inbound, str(vpn_client.uuid), target_client, email=email)

        cache.delete(self._inbound_cache_key(vpn_client.inbound))
        cache.delete(f"xui:client-traffic:{self.panel.pk}:{email}")
        cache.delete(f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}")
        return {
            "total_traffic_bytes": new_total,
            "expiry_at": parse_xui_datetime(target_client.get("expiryTime")),
            "expiry_unlimited": expiry_unlimited,
            "node_id": getattr(vpn_client.inbound, "xui_node_id", "") or "",
            "remote_key": inbound_remote_key(vpn_client.inbound, panel=self.panel),
            "raw": target_client,
        }

    def delete_client(self, order):
        resolve_inbound_panel(order.inbound, self.panel, require_active=False)
        self._assert_precise_scope(order.inbound, getattr(order, "uuid", ""), operation="delete")
        api_identifier = str(getattr(order, "uuid", "") or "").strip()
        email = getattr(order, "username", "") or ""
        if hasattr(order, "xui_email"):
            email = order.xui_email or email
        if hasattr(order, "vpn_clients"):
            try:
                linked_client = order.vpn_clients.order_by("created_at", "pk").first()
            except Exception:
                linked_client = None
            if linked_client:
                email = linked_client.xui_email or linked_client.username or email
        if self._uses_modern_client_api() or not email:
            found = self._find_client_in_inbound(order.inbound, api_identifier)
            target_client = found.get("client") or {}
            target_stats = found.get("client_stats") or {}
            email = str(target_client.get("email") or target_stats.get("email") or email or "").strip()
            api_identifier = self._client_api_identifier(target_client, api_identifier)
        response = self._post_client_delete(order.inbound, api_identifier, email=email)
        if not response.get("success"):
            message = str(response.get("msg") or "")
            if "not found" in message.lower() or "不存在" in message:
                return True
            raise XUIError(message or "Panel rejected client deletion.")
        cache.delete(self._inbound_cache_key(order.inbound))
        return True

    def get_client_stats(self, vpn_client, *, use_cache=True):
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=False)
        cache_key = f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        email = vpn_client.xui_email or vpn_client.username
        try:
            traffic = self.get_client_traffic(email, use_cache=use_cache)
            clients = self.get_inbound_clients(vpn_client.inbound, use_cache=use_cache)
            panel_client = next(
                (
                    client
                    for client in clients
                    if client.get("id") == str(vpn_client.uuid)
                    or client.get("email") == email
                ),
                {},
            )
            online_clients = self.get_online_clients()
            last_online_at = timezone.now() if email in online_clients else vpn_client.last_online_at

            upload = int(traffic.get("up") or panel_client.get("up") or 0)
            download = int(traffic.get("down") or panel_client.get("down") or 0)
            used = upload + download
            total = int(
                traffic.get("total")
                or traffic.get("totalGB")
                or panel_client.get("totalGB")
                or vpn_client.traffic_limit_bytes
                or 0
            )
            expiry_at = parse_xui_datetime(
                traffic.get("expiryTime") or panel_client.get("expiryTime")
            )
            remaining = max(total - used, 0) if total else 0
            is_expired = bool(expiry_at and expiry_at <= timezone.now()) or bool(total and remaining <= 0)
            stats = XUIClientStats(
                uuid=str(vpn_client.uuid or panel_client.get("id") or ""),
                email=email,
                inbound_id=vpn_client.inbound.inbound_id if vpn_client.inbound_id else None,
                total_traffic_bytes=total,
                used_upload_bytes=upload,
                used_download_bytes=download,
                used_traffic_bytes=used,
                remaining_traffic_bytes=remaining,
                expiry_at=expiry_at,
                last_online_at=last_online_at,
                is_enabled=bool(traffic.get("enable", panel_client.get("enable", False))),
                is_expired=is_expired,
                raw={"traffic": traffic, "client": panel_client},
                history=get_usage_history(vpn_client),
            ).to_dict()
        except Exception as exc:
            logger.warning(
                "Could not fetch X-UI client stats: %s",
                sanitize_xui_operational_text(exc, panel=self.panel),
            )
            stats = XUIClientStats(
                uuid=str(vpn_client.uuid or ""),
                email=email,
                inbound_id=vpn_client.inbound.inbound_id if vpn_client.inbound_id else None,
                total_traffic_bytes=vpn_client.traffic_limit_bytes,
                used_upload_bytes=vpn_client.used_upload_bytes,
                used_download_bytes=vpn_client.used_download_bytes,
                used_traffic_bytes=vpn_client.used_traffic_bytes,
                remaining_traffic_bytes=vpn_client.remaining_traffic_bytes,
                expiry_at=vpn_client.expires_at,
                last_online_at=vpn_client.last_online_at,
                is_enabled=vpn_client.status == vpn_client.Status.ACTIVE,
                is_expired=vpn_client.is_expired,
                panel_available=False,
                error=sanitize_xui_operational_text(exc, panel=self.panel),
                raw=vpn_client.xui_raw,
                history=get_usage_history(vpn_client),
            ).to_dict()

        cache.set(cache_key, stats, CLIENT_STATS_CACHE_SECONDS)
        return stats


def get_usage_history(vpn_client, limit=30):
    snapshots = list(vpn_client.usage_snapshots.order_by("-recorded_at")[:limit])
    snapshots.reverse()
    return [
        {
            "recorded_at": snapshot.recorded_at,
            "total_traffic_bytes": snapshot.total_traffic_bytes,
            "used_upload_bytes": snapshot.used_upload_bytes,
            "used_download_bytes": snapshot.used_download_bytes,
            "used_traffic_bytes": snapshot.used_traffic_bytes,
            "remaining_traffic_bytes": snapshot.remaining_traffic_bytes,
        }
        for snapshot in snapshots
    ]


def get_inbound_client_stats(panel, inbound):
    service = XUIService(panel)
    service.login()
    inbound_data = service.get_inbound(inbound, use_cache=False)
    client_stats = parse_xui_client_stats(inbound_data)
    settings_data = parse_xui_json_object(inbound_data.get("settings"))
    clients = settings_data.get("clients") or []
    if not isinstance(clients, list):
        clients = []
    return {
        "panel": panel,
        "inbound": inbound,
        "inbound_data": inbound_data,
        "clients": clients,
        "client_stats": client_stats,
    }


def get_panel_inbounds_with_stats(panel):
    from .models import Inbound

    service = XUIService(panel)
    service.login()
    rows = []
    for inbound in Inbound.objects.filter(panel=panel, is_active=True).order_by("inbound_id"):
        inbound_data = service.get_inbound(inbound, use_cache=False)
        settings_data = parse_xui_json_object(inbound_data.get("settings"))
        clients = settings_data.get("clients") or []
        if not isinstance(clients, list):
            clients = []
        rows.append(
            {
                "inbound": inbound,
                "inbound_data": inbound_data,
                "clients": clients,
                "client_stats": parse_xui_client_stats(inbound_data),
            }
        )
    return rows


def collect_panel_usage_stats(panel):
    from .models import Inbound

    captured_at = timezone.now()
    store = getattr(panel, "store", None)
    if not getattr(panel, "is_active", False):
        return {
            "panel": panel,
            "status": "skipped",
            "captured_at": captured_at,
            "total_upload_bytes": 0,
            "total_download_bytes": 0,
            "total_used_bytes": 0,
            "clients": [],
            "clients_count": 0,
            "online_clients_count": 0,
            "checked_inbounds_count": 0,
            "active_inbounds_count": 0,
            "error_message": "Panel is inactive.",
            "metadata": {"reason": "panel_inactive"},
        }
    if store is not None and not getattr(store, "panel_usage_tracking_enabled", True):
        return {
            "panel": panel,
            "status": "skipped",
            "captured_at": captured_at,
            "total_upload_bytes": 0,
            "total_download_bytes": 0,
            "total_used_bytes": 0,
            "clients": [],
            "clients_count": 0,
            "online_clients_count": 0,
            "checked_inbounds_count": 0,
            "active_inbounds_count": 0,
            "error_message": "Panel usage tracking is disabled for this store.",
            "metadata": {"reason": "tracking_disabled"},
        }

    service = XUIService(panel)
    try:
        service.login()
    except Exception as exc:
        error_message = sanitize_xui_operational_text(exc, panel=panel)
        return {
            "panel": panel,
            "status": "failed",
            "captured_at": captured_at,
            "total_upload_bytes": 0,
            "total_download_bytes": 0,
            "total_used_bytes": 0,
            "clients": [],
            "clients_count": 0,
            "online_clients_count": 0,
            "checked_inbounds_count": 0,
            "active_inbounds_count": 0,
            "error_message": error_message,
            "metadata": {"error": exc.__class__.__name__},
        }

    try:
        online_clients = service.get_online_clients(suppress_errors=False)
        online_available = True
    except Exception as exc:
        online_clients = set()
        online_available = False
        online_error = sanitize_xui_operational_text(exc, panel=panel)
    else:
        online_error = ""

    active_inbounds = list(Inbound.objects.filter(panel=panel, is_active=True).order_by("inbound_id"))
    clients = []
    inbound_errors = []
    missing_stats = 0
    checked_inbounds = 0
    total_upload = 0
    total_download = 0
    total_used = 0
    seen_per_panel = set()
    duplicate_usage_count = 0
    duplicate_usage_keys = set()

    def add_usage_row(normalized):
        nonlocal total_upload, total_download, total_used, missing_stats, duplicate_usage_count
        identifier_hash = normalized["identifier_hash"]
        if identifier_hash in seen_per_panel:
            duplicate_usage_count += 1
            duplicate_usage_keys.add(identifier_hash)
            return False
        clients.append(normalized)
        seen_per_panel.add(identifier_hash)
        if normalized["stats_available"]:
            total_upload += int(normalized["upload_bytes"] or 0)
            total_download += int(normalized["download_bytes"] or 0)
            total_used += int(normalized["used_bytes"] or 0)
        else:
            missing_stats += 1
        return True

    for inbound in active_inbounds:
        try:
            inbound_data = service.get_inbound(inbound, use_cache=False)
        except Exception as exc:
            inbound_errors.append(
                {
                    "inbound_id": inbound.inbound_id,
                    "inbound_pk": inbound.pk,
                    "error": sanitize_xui_operational_text(exc, panel=panel),
                }
            )
            continue

        checked_inbounds += 1
        settings_data = parse_xui_json_object(inbound_data.get("settings"))
        panel_clients = settings_data.get("clients") or []
        if not isinstance(panel_clients, list):
            panel_clients = []
        client_stats = parse_xui_client_stats(inbound_data)
        matched_stat_ids = set()

        for client in panel_clients:
            if not isinstance(client, dict):
                continue
            stats = related_client_stats(client_stats, client)
            if stats:
                matched_stat_ids.add(id(stats))
            normalized = normalize_xui_usage_client(
                inbound=inbound,
                inbound_data=inbound_data,
                client=client,
                stats=stats,
                online_clients=online_clients,
                online_available=online_available,
            )
            if not normalized:
                missing_stats += 1
                continue
            add_usage_row(normalized)

        for stats in client_stats:
            if not isinstance(stats, dict) or id(stats) in matched_stat_ids:
                continue
            normalized = normalize_xui_usage_client(
                inbound=inbound,
                inbound_data=inbound_data,
                client={},
                stats=stats,
                online_clients=online_clients,
                online_available=online_available,
            )
            if not normalized:
                missing_stats += 1
                continue
            add_usage_row(normalized)

    status = "ok"
    error_message = ""
    if inbound_errors or missing_stats:
        status = "partial"
        if inbound_errors:
            error_message = "One or more inbounds could not be read."
        elif missing_stats:
            error_message = "Some client traffic stats were missing."

    return {
        "panel": panel,
        "status": status,
        "captured_at": captured_at,
        "total_upload_bytes": total_upload,
        "total_download_bytes": total_download,
        "total_used_bytes": total_used,
        "clients": clients,
        "clients_count": len(seen_per_panel),
        "online_clients_count": len({item["identifier_hash"] for item in clients if item.get("online") is True}),
        "checked_inbounds_count": checked_inbounds,
        "active_inbounds_count": len(active_inbounds),
        "error_message": error_message,
        "metadata": {
            "inbound_errors": inbound_errors[:20],
            "inbound_error_count": len(inbound_errors),
            "missing_client_stats_count": missing_stats,
            "duplicate_usage_count": duplicate_usage_count,
            "duplicate_usage_keys": sorted(duplicate_usage_keys)[:20],
            "online_api_available": online_available,
            "online_api_error": online_error,
        },
    }


def find_client_by_identifier(panel, identifier):
    return XUIService(panel).find_client_by_identifier(identifier)


def delete_client_from_inbound(panel, inbound, identifier, *, allow_multi_scope=False):
    return XUIService(panel).delete_client_from_inbound(
        inbound,
        identifier,
        allow_multi_scope=allow_multi_scope,
    )


def update_client_traffic_and_expiry(
    panel,
    inbound,
    identifier,
    total_bytes=None,
    expiry_time=None,
    enable=None,
    *,
    allow_multi_scope=False,
):
    return XUIService(panel).update_client_traffic_and_expiry(
        inbound,
        identifier,
        total_bytes=total_bytes,
        expiry_time=expiry_time,
        enable=enable,
        allow_multi_scope=allow_multi_scope,
    )


def update_client_traffic(panel, inbound, identifier, total_bytes):
    return update_client_traffic_and_expiry(panel, inbound, identifier, total_bytes=total_bytes)


def update_client_expiry(panel, inbound, identifier, expiry_time):
    return update_client_traffic_and_expiry(panel, inbound, identifier, expiry_time=expiry_time)


def build_config_link_for_identifier(panel, inbound_id, identifier, *, inbound=None, node_id=""):
    return XUIService(panel).build_config_link_for_identifier(
        inbound_id,
        identifier,
        inbound=inbound,
        node_id=node_id,
    )


def sync_vpn_client_stats(vpn_client, *, force=False, create_snapshot=True):
    from .models import VPNClient, VPNClientUsageSnapshot

    if getattr(vpn_client, "is_deleted", False):
        return XUIClientStats(
            uuid=str(vpn_client.uuid or ""),
            email=vpn_client.xui_email or vpn_client.username,
            panel_available=False,
            error="VPN client has been deleted locally.",
        ).to_dict()

    if not vpn_client.inbound_id or not vpn_client.inbound.panel_id:
        return XUIClientStats(
            uuid=str(vpn_client.uuid or ""),
            email=vpn_client.xui_email or vpn_client.username,
            panel_available=False,
            error="VPN client is not linked to an active inbound.",
        ).to_dict()

    stats = XUIService(vpn_client.inbound.panel).get_client_stats(
        vpn_client,
        use_cache=not force,
    )
    if stats.get("panel_available"):
        vpn_client.sync_usage_fields(stats)
        if stats.get("is_expired"):
            vpn_client.status = VPNClient.Status.EXPIRED
        elif stats.get("is_enabled"):
            vpn_client.status = VPNClient.Status.ACTIVE
        elif vpn_client.status == VPNClient.Status.ACTIVE:
            vpn_client.status = VPNClient.Status.INACTIVE

        vpn_client.save(
            update_fields=[
                "used_upload_bytes",
                "used_download_bytes",
                "used_traffic_bytes",
                "traffic_limit_bytes",
                "expires_at",
                "last_online_at",
                "last_synced_at",
                "xui_raw",
                "status",
                "updated_at",
            ]
        )

        if create_snapshot:
            last_snapshot = vpn_client.usage_snapshots.order_by("-recorded_at").first()
            should_snapshot = (
                last_snapshot is None
                or (timezone.now() - last_snapshot.recorded_at).total_seconds()
                >= USAGE_SNAPSHOT_INTERVAL_SECONDS
            )
            if should_snapshot:
                VPNClientUsageSnapshot.objects.create(
                    vpn_client=vpn_client,
                    total_traffic_bytes=stats.get("total_traffic_bytes", 0),
                    used_upload_bytes=stats.get("used_upload_bytes", 0),
                    used_download_bytes=stats.get("used_download_bytes", 0),
                    used_traffic_bytes=stats.get("used_traffic_bytes", 0),
                    remaining_traffic_bytes=stats.get("remaining_traffic_bytes", 0),
                    raw=stats.get("raw", {}),
                )
                stats["history"] = get_usage_history(vpn_client)

    return stats


def refresh_vpn_client_links(vpn_client):
    if not vpn_client.inbound_id or not vpn_client.inbound.panel_id:
        return None

    old_uuid = vpn_client.uuid
    old_email = vpn_client.xui_email
    try:
        details = XUIService(vpn_client.inbound.panel).get_client_config_details(vpn_client)
    except Exception as exc:
        logger.warning(
            "Could not refresh X-UI client links: %s",
            sanitize_xui_operational_text(exc, panel=vpn_client.inbound.panel),
        )
        return None

    changed_fields = []
    for field, value in (
        ("uuid", details.get("uuid")),
        ("xui_email", details.get("email")),
        ("sub_id", details.get("sub_id")),
        ("sub_link", details.get("sub_link")),
        ("direct_link", details.get("direct_link")),
        ("xui_raw", details.get("raw", {})),
    ):
        if value is not None and getattr(vpn_client, field) != value:
            setattr(vpn_client, field, value)
            changed_fields.append(field)

    if changed_fields:
        vpn_client.save(update_fields=[*changed_fields, "updated_at"])

    order = vpn_client.order
    if order:
        order_changed_fields = []
        for field, value in (
            ("uuid", details.get("uuid")),
            ("sub_link", details.get("sub_link")),
            ("direct_link", details.get("direct_link")),
        ):
            if value is not None and getattr(order, field) != value:
                setattr(order, field, value)
                order_changed_fields.append(field)
        if order_changed_fields:
            order.save(update_fields=[*order_changed_fields, "updated_at"])

    cache.delete(f"xui:client-stats:{vpn_client.pk}:{old_uuid}:{old_email}")
    cache.delete(f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}")
    cache.delete(f"xui:client-traffic:{vpn_client.inbound.panel.pk}:{old_email or vpn_client.username}")
    cache.delete(f"xui:client-traffic:{vpn_client.inbound.panel.pk}:{vpn_client.xui_email or vpn_client.username}")
    return details


def create_inactive_client_details(email_prefix, total_gb, expire_days, panel, inbound, limit_ip=2):
    try:
        resolved_panel = resolve_inbound_panel(inbound, panel, require_active=True)
        return XUIService(resolved_panel).create_inactive_client(
            email_prefix=email_prefix,
            total_gb=total_gb,
            expire_days=expire_days,
            inbound=inbound,
            limit_ip=limit_ip,
        )
    except Exception as exc:
        logger.warning(
            "Could not create inactive X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=panel),
        )
        return None


def create_trial_client_details(email_prefix, total_gb, duration_hours, panel, inbound, limit_ip=1):
    try:
        resolved_panel = resolve_inbound_panel(inbound, panel, require_active=True)
        return XUIService(resolved_panel).create_enabled_client(
            email_prefix=email_prefix,
            total_gb=total_gb,
            duration_hours=duration_hours,
            inbound=inbound,
            limit_ip=limit_ip,
        )
    except Exception as exc:
        logger.warning(
            "Could not create free trial X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=panel),
        )
        return None


def create_enabled_client_details(
    email_prefix,
    total_gb,
    duration_days,
    panel,
    inbound,
    limit_ip=2,
    *,
    client_uuid="",
    sub_id="",
    email="",
):
    try:
        resolved_panel = resolve_inbound_panel(inbound, panel, require_active=True)
        return XUIService(resolved_panel).create_enabled_client(
            email_prefix=email_prefix,
            total_gb=total_gb,
            duration_hours=int(duration_days or 0) * 24,
            inbound=inbound,
            limit_ip=limit_ip,
            client_uuid=client_uuid,
            sub_id=sub_id,
            email=email,
        )
    except Exception as exc:
        logger.warning(
            "Could not create enabled X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=panel),
        )
        return None


def create_enabled_multi_inbound_client_details(
    email_prefix,
    total_gb,
    duration_days,
    panel,
    inbounds,
    limit_ip=2,
    *,
    client_uuid="",
    sub_id="",
    email="",
):
    try:
        inbounds = [inbound for inbound in (inbounds or []) if inbound]
        if not inbounds:
            raise XUIError("At least one inbound is required.")
        resolved_panel = resolve_inbound_panel(inbounds[0], panel, require_active=True)
        return XUIService(resolved_panel).create_enabled_multi_inbound_client(
            email_prefix=email_prefix,
            total_gb=total_gb,
            duration_hours=int(duration_days or 0) * 24,
            inbounds=inbounds,
            limit_ip=limit_ip,
            client_uuid=client_uuid,
            sub_id=sub_id,
            email=email,
        )
    except Exception as exc:
        logger.warning(
            "Could not create enabled multi-inbound X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=panel),
        )
        return None


def renew_client(vpn_client, plan):
    try:
        panel = resolve_inbound_panel(vpn_client.inbound, require_active=True)
        return XUIService(panel).update_client_subscription(vpn_client, plan)
    except Exception as exc:
        logger.warning(
            "Could not renew X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=getattr(vpn_client.inbound, "panel", None)),
        )
        return None


def add_client_traffic(vpn_client, extra_gb, *, extra_days=0):
    try:
        panel = resolve_inbound_panel(vpn_client.inbound, require_active=True)
        return XUIService(panel).add_client_traffic(vpn_client, extra_gb, extra_days=extra_days)
    except Exception as exc:
        logger.warning(
            "Could not add X-UI client referral traffic: %s",
            sanitize_xui_operational_text(exc, panel=getattr(vpn_client.inbound, "panel", None)),
        )
        return None


def create_inactive_client(email_prefix, total_gb, expire_days, panel, inbound):
    result = create_inactive_client_details(
        email_prefix=email_prefix,
        total_gb=total_gb,
        expire_days=expire_days,
        panel=panel,
        inbound=inbound,
    )
    if not result:
        return None, None, None
    return result["uuid"], result["direct_link"], result["sub_link"]


def sync_inbound_data(panel_url, username, password, inbound_id, *, proxy_url=None):
    session = configure_xui_session(requests.Session(), proxy_url=proxy_url)
    try:
        login_res = session.post(
            f"{panel_url.rstrip('/')}/login",
            data={"username": username, "password": password},
            timeout=PANEL_TIMEOUT_SECONDS,
        )
        if login_res.status_code != 200 or not login_res.json().get("success"):
            return False, "Panel login failed."

        response = session.get(
            f"{panel_url.rstrip('/')}/panel/api/inbounds/get/{inbound_id}",
            timeout=PANEL_TIMEOUT_SECONDS,
        )
        if response.status_code == 200 and response.json().get("success"):
            data = response.json()["obj"]
            stream_settings = json.loads(data.get("streamSettings", "{}"))
            result = {
                "protocol": data.get("protocol", "vless"),
                "port": str(data.get("port") or ""),
                "config_params": build_vless_query_params(stream_settings),
                "network_type": stream_settings.get("network", "tcp"),
                "security": stream_settings.get("security", "none"),
                "sni": "",
                "fingerprint": "",
                "pbk": "",
                "sid": "",
                "ws_path": "",
                "ws_host": "",
            }
            if result["security"] == "reality":
                reality_settings = stream_settings.get("realitySettings", {})
                server_names = reality_settings.get("serverNames") or []
                short_ids = reality_settings.get("shortIds") or []
                result["sni"] = server_names[0] if server_names else ""
                result["fingerprint"] = reality_settings.get("fingerprint", "chrome")
                result["pbk"] = reality_settings.get("settings", {}).get("publicKey", "")
                result["sid"] = short_ids[0] if short_ids else ""

            if result["network_type"] == "ws":
                ws_settings = stream_settings.get("wsSettings", {})
                result["ws_path"] = ws_settings.get("path", "/")
                result["ws_host"] = ws_settings.get("headers", {}).get("Host", "")
            return True, result
        return False, "Inbound was not found."
    except Exception as exc:
        panel_like = SimpleNamespace(url=panel_url, username=username, password=password, proxy_url=proxy_url)
        return False, sanitize_xui_operational_text(exc, panel=panel_like)


def login_to_panel(panel, *, raise_errors=False):
    try:
        return XUIService(panel).login()
    except Exception as exc:
        logger.warning("Could not login to X-UI panel: %s", sanitize_xui_operational_text(exc, panel=panel))
        if raise_errors:
            raise
        return None


def enable_client(order):
    try:
        panel = resolve_inbound_panel(order.inbound, require_active=True)
        return XUIService(panel).update_client_enabled(order)
    except Exception as exc:
        logger.warning(
            "Could not enable X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=getattr(order.inbound, "panel", None)),
        )
        return False


def delete_client(order):
    try:
        panel = resolve_inbound_panel(order.inbound, require_active=False)
        return XUIService(panel).delete_client(order)
    except Exception as exc:
        logger.warning(
            "Could not delete X-UI client: %s",
            sanitize_xui_operational_text(exc, panel=getattr(order.inbound, "panel", None)),
        )
        return False
