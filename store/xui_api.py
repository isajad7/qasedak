import base64
import json
import logging
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .naming import build_client_display_name, build_xui_client_email

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 10
PANEL_LOGIN_TIMEOUT_SECONDS = (5, 7)
PANEL_LOGIN_ATTEMPTS = 2
CLIENT_STATS_CACHE_SECONDS = 60
USAGE_SNAPSHOT_INTERVAL_SECONDS = 300


class XUIError(Exception):
    pass


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


def first_value(value):
    if isinstance(value, list) and value:
        return value[0]
    if isinstance(value, str):
        return value
    return ""


def append_param(params, key, value):
    if value is None or value == "":
        return
    params.append((key, str(value)))


def build_vless_query_params(stream_settings, client_data=None):
    client_data = client_data or {}
    stream_settings = stream_settings or {}
    network = stream_settings.get("network") or "tcp"
    security = stream_settings.get("security") or "none"
    params = []

    append_param(params, "type", network)
    append_param(params, "security", security)
    append_param(params, "encryption", "none")
    append_param(params, "flow", client_data.get("flow"))

    if security == "reality":
        reality_settings = stream_settings.get("realitySettings") or {}
        reality_inner_settings = reality_settings.get("settings") or {}
        append_param(
            params,
            "pbk",
            reality_inner_settings.get("publicKey") or reality_settings.get("publicKey"),
        )
        append_param(
            params,
            "fp",
            reality_settings.get("fingerprint")
            or reality_inner_settings.get("fingerprint")
            or "chrome",
        )
        append_param(params, "sni", first_value(reality_settings.get("serverNames")))
        append_param(params, "sid", first_value(reality_settings.get("shortIds")))
        append_param(params, "spx", reality_settings.get("spiderX") or "/")
    elif security == "tls":
        tls_settings = stream_settings.get("tlsSettings") or {}
        append_param(params, "sni", tls_settings.get("serverName"))
        alpn = tls_settings.get("alpn")
        if isinstance(alpn, list) and alpn:
            append_param(params, "alpn", ",".join(alpn))
        append_param(params, "fp", tls_settings.get("fingerprint"))

    if network == "ws":
        ws_settings = stream_settings.get("wsSettings") or {}
        headers = ws_settings.get("headers") or {}
        append_param(params, "path", ws_settings.get("path") or "/")
        append_param(params, "host", headers.get("Host") or ws_settings.get("host"))
    elif network == "grpc":
        grpc_settings = stream_settings.get("grpcSettings") or {}
        append_param(params, "serviceName", grpc_settings.get("serviceName"))
        append_param(params, "authority", grpc_settings.get("authority"))
        append_param(params, "mode", "multi" if grpc_settings.get("multiMode") else "gun")
    elif network == "tcp":
        tcp_settings = stream_settings.get("tcpSettings") or {}
        header = tcp_settings.get("header") or {}
        header_type = header.get("type")
        if header_type and header_type != "none":
            append_param(params, "headerType", header_type)
            request_settings = header.get("request") or {}
            append_param(params, "path", first_value(request_settings.get("path")))
            headers = request_settings.get("headers") or {}
            append_param(params, "host", first_value(headers.get("Host")))

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
    network = stream_settings.get("network") or "tcp"
    security = stream_settings.get("security") or "none"
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
    def __init__(self, panel):
        self.panel = panel
        self.base_url = panel.url.rstrip("/")
        self.session = configure_xui_session(requests.Session(), panel=panel)
        self._logged_in = False

    def login(self):
        if self._logged_in:
            return self.session
        last_exception = None
        for attempt in range(1, PANEL_LOGIN_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/login",
                    data={"username": self.panel.username, "password": self.panel.password},
                    timeout=PANEL_LOGIN_TIMEOUT_SECONDS,
                )
                break
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
        else:
            raise last_exception or XUIError("Panel login failed.")
        if response.status_code != 200:
            raise XUIError("Panel login failed.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise XUIError("Panel returned an invalid login response.") from exc
        if not payload.get("success"):
            raise XUIError(payload.get("msg") or "Panel login was rejected.")
        self._logged_in = True
        return self.session

    def request_json(self, method, path, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = PANEL_TIMEOUT_SECONDS
        response = self.session.request(method, f"{self.base_url}{path}", **kwargs)
        if response.status_code != 200:
            raise XUIError(f"Panel request failed with HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise XUIError("Panel returned invalid JSON.") from exc

    def authenticated_json(self, method, path, **kwargs):
        self.login()
        return self.request_json(method, path, **kwargs)

    def get_inbound(self, inbound_id, *, use_cache=True):
        cache_key = f"xui:inbound:{self.panel.pk}:{inbound_id}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        data = self.authenticated_json("GET", f"/panel/api/inbounds/get/{inbound_id}")
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
                inbound_data = self.get_inbound(inbound.inbound_id, use_cache=False)
                successful_inbound_reads += 1
            except Exception as exc:
                inbound_errors.append(f"{inbound.inbound_id}: {exc}")
                logger.warning(
                    "Could not read inbound during config lookup panel=%s inbound=%s error=%s",
                    self.panel.pk,
                    inbound.inbound_id,
                    exc,
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
                    logger.warning(
                        "Could not read client traffic during config lookup panel=%s inbound=%s email=%s error=%s",
                        self.panel.pk,
                        inbound.inbound_id,
                        mask_xui_value(email),
                        exc,
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

        data = self.authenticated_json(
            "GET",
            f"/panel/api/inbounds/getClientTraffics/{quote(normalized_email)}",
        )
        if not data.get("success"):
            raise XUIError(data.get("msg") or "Client traffic was not found.")
        traffic = data.get("obj") or {}
        cache.set(cache_key, traffic, CLIENT_STATS_CACHE_SECONDS)
        return traffic

    def get_online_clients(self):
        try:
            data = self.authenticated_json("POST", "/panel/api/inbounds/onlines")
        except Exception:
            return set()

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

    def build_direct_link(self, *, inbound, inbound_data, client_uuid, client_data, email):
        resolve_inbound_panel(inbound, self.panel, require_active=False)
        protocol = (inbound_data.get("protocol") or inbound.protocol or "vless").lower()
        address = inbound.server_ip or urlparse(self.panel.url).hostname or ""
        port = str(inbound_data.get("port") or inbound.port).strip()
        remark = email or client_data.get("remark") or client_data.get("email") or ""

        try:
            stream_settings = json.loads(inbound_data.get("streamSettings") or "{}")
        except (TypeError, ValueError):
            stream_settings = {}

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

    def build_config_link_for_identifier(self, inbound_id, identifier):
        from .models import Inbound

        identifier = str(identifier or "").strip()
        if not identifier:
            raise XUIError("Client identifier is required.")

        inbound = Inbound.objects.filter(
            panel=self.panel,
            inbound_id=inbound_id,
            is_active=True,
        ).first()
        if not inbound:
            raise XUIError("Active inbound was not found for config update.")

        inbound_data = self.get_inbound(inbound.inbound_id, use_cache=False)
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

        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_secret,
            client_data=target_client,
            email=remark or email,
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
            "matched_field": matched_field,
            "config_link_updated": True,
        }

    def get_client_config_details(self, vpn_client):
        if not vpn_client.inbound_id or not vpn_client.inbound.panel_id:
            raise XUIError("VPN client is not linked to a panel inbound.")
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=False)

        inbound_data = self.get_inbound(vpn_client.inbound.inbound_id, use_cache=False)
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
        direct_link = self.build_direct_link(
            inbound=vpn_client.inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=target_client,
            email=client_email,
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
            "tgId": "",
            "subId": sub_id,
        }
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
        if not response.get("success"):
            raise XUIError(response.get("msg") or "Could not add client to panel.")

        inbound_data = self.get_inbound(inbound.inbound_id, use_cache=False)
        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=client_data,
            email=email,
        )
        sub_base_url = self.build_sub_base_url(inbound_data)

        return {
            "uuid": client_uuid,
            "email": email,
            "sub_id": sub_id,
            "sub_link": f"{sub_base_url}/sub/{sub_id}",
            "direct_link": direct_link,
            "raw": client_data,
        }

    def create_enabled_client(self, *, email_prefix, total_gb, duration_hours, inbound, limit_ip=1):
        resolve_inbound_panel(inbound, self.panel, require_active=True)
        self.login()
        client_uuid = str(uuid.uuid4())
        sub_id = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        email = build_xui_client_email(email_prefix, client_uuid)
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
            "tgId": "",
            "subId": sub_id,
        }
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
        if not response.get("success"):
            raise XUIError(response.get("msg") or "Could not add client to panel.")

        inbound_data = self.get_inbound(inbound.inbound_id, use_cache=False)
        direct_link = self.build_direct_link(
            inbound=inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data=client_data,
            email=email,
        )
        sub_base_url = self.build_sub_base_url(inbound_data)

        return {
            "uuid": client_uuid,
            "email": email,
            "sub_id": sub_id,
            "sub_link": f"{sub_base_url}/sub/{sub_id}",
            "direct_link": direct_link,
            "expires_at": parse_xui_datetime(expiry_time),
            "raw": client_data,
        }

    def update_client_enabled(self, order):
        resolve_inbound_panel(order.inbound, self.panel, require_active=True)
        inbound_data = self.get_inbound(order.inbound.inbound_id, use_cache=False)
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

        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/updateClient/{order.uuid}",
            data={
                "id": order.inbound.inbound_id,
                "settings": json.dumps({"clients": [target_client]}),
            },
        )
        if not response.get("success"):
            raise XUIError(response.get("msg") or "Panel rejected client activation.")
        return True

    def update_client_subscription(self, vpn_client, plan):
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=True)
        inbound_data = self.get_inbound(vpn_client.inbound.inbound_id, use_cache=False)
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

        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/updateClient/{vpn_client.uuid}",
            data={
                "id": vpn_client.inbound.inbound_id,
                "settings": json.dumps({"clients": [target_client]}),
            },
        )
        if not response.get("success"):
            raise XUIError(response.get("msg") or "Panel rejected client renewal.")

        email = vpn_client.xui_email or vpn_client.username
        reset_response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/{vpn_client.inbound.inbound_id}/resetClientTraffic/{quote(email)}",
            headers={"Accept": "application/json"},
        )
        if not reset_response.get("success"):
            raise XUIError(reset_response.get("msg") or "Panel rejected traffic reset.")

        cache.delete(f"xui:inbound:{self.panel.pk}:{vpn_client.inbound.inbound_id}")
        cache.delete(f"xui:client-traffic:{self.panel.pk}:{email}")
        cache.delete(f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}")
        return {
            "expiry_at": parse_xui_datetime(target_client["expiryTime"]),
            "raw": target_client,
        }

    def add_client_traffic(self, vpn_client, extra_gb, *, extra_days=0):
        resolve_inbound_panel(vpn_client.inbound, self.panel, require_active=True)
        inbound_data = self.get_inbound(vpn_client.inbound.inbound_id, use_cache=False)
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

        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/updateClient/{vpn_client.uuid}",
            data={
                "id": vpn_client.inbound.inbound_id,
                "settings": json.dumps({"clients": [target_client]}),
            },
        )
        if not response.get("success"):
            raise XUIError(response.get("msg") or "Panel rejected client traffic update.")

        cache.delete(f"xui:inbound:{self.panel.pk}:{vpn_client.inbound.inbound_id}")
        cache.delete(f"xui:client-traffic:{self.panel.pk}:{email}")
        cache.delete(f"xui:client-stats:{vpn_client.pk}:{vpn_client.uuid}:{vpn_client.xui_email}")
        return {
            "total_traffic_bytes": new_total,
            "expiry_at": parse_xui_datetime(target_client.get("expiryTime")),
            "expiry_unlimited": expiry_unlimited,
            "raw": target_client,
        }

    def delete_client(self, order):
        resolve_inbound_panel(order.inbound, self.panel, require_active=False)
        response = self.authenticated_json(
            "POST",
            f"/panel/api/inbounds/{order.inbound.inbound_id}/delClient/{order.uuid}",
            headers={"Accept": "application/json"},
        )
        if not response.get("success"):
            message = str(response.get("msg") or "")
            if "not found" in message.lower() or "不存在" in message:
                return True
            raise XUIError(message or "Panel rejected client deletion.")
        cache.delete(f"xui:inbound:{self.panel.pk}:{order.inbound.inbound_id}")
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
            clients = self.get_inbound_clients(vpn_client.inbound.inbound_id, use_cache=use_cache)
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
            logger.warning("Could not fetch X-UI client stats: %s", exc)
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
                error=str(exc),
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


def find_client_by_identifier(panel, identifier):
    return XUIService(panel).find_client_by_identifier(identifier)


def build_config_link_for_identifier(panel, inbound_id, identifier):
    return XUIService(panel).build_config_link_for_identifier(inbound_id, identifier)


def sync_vpn_client_stats(vpn_client, *, force=False, create_snapshot=True):
    from .models import VPNClient, VPNClientUsageSnapshot

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
        logger.warning("Could not refresh X-UI client links: %s", exc)
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
        logger.warning("Could not create inactive X-UI client: %s", exc)
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
        logger.warning("Could not create free trial X-UI client: %s", exc)
        return None


def renew_client(vpn_client, plan):
    try:
        panel = resolve_inbound_panel(vpn_client.inbound, require_active=True)
        return XUIService(panel).update_client_subscription(vpn_client, plan)
    except Exception as exc:
        logger.warning("Could not renew X-UI client: %s", exc)
        return None


def add_client_traffic(vpn_client, extra_gb, *, extra_days=0):
    try:
        panel = resolve_inbound_panel(vpn_client.inbound, require_active=True)
        return XUIService(panel).add_client_traffic(vpn_client, extra_gb, extra_days=extra_days)
    except Exception as exc:
        logger.warning("Could not add X-UI client referral traffic: %s", exc)
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
        return False, str(exc)


def login_to_panel(panel):
    try:
        return XUIService(panel).login()
    except Exception as exc:
        logger.warning("Could not login to X-UI panel: %s", exc)
        return None


def enable_client(order):
    try:
        panel = resolve_inbound_panel(order.inbound, require_active=True)
        return XUIService(panel).update_client_enabled(order)
    except Exception as exc:
        logger.warning("Could not enable X-UI client: %s", exc)
        return False


def delete_client(order):
    try:
        panel = resolve_inbound_panel(order.inbound, require_active=False)
        return XUIService(panel).delete_client(order)
    except Exception as exc:
        logger.warning("Could not delete X-UI client: %s", exc)
        return False
