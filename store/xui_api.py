import json
import logging
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode, urlparse

import requests
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 10
CLIENT_STATS_CACHE_SECONDS = 60
USAGE_SNAPSHOT_INTERVAL_SECONDS = 300


class XUIError(Exception):
    pass


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
    name = transliterate_to_ascii(payer_name)
    traffic = clean_decimal_label(total_gb).replace(".", "p")
    suffix = (tracking_code or uuid.uuid4().hex)[:8].lower()
    return f"{name}_{traffic}gb_{suffix}"[:90].strip("_")


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
        self.session = requests.Session()
        self._logged_in = False

    def login(self):
        if self._logged_in:
            return self.session
        response = self.session.post(
            f"{self.base_url}/login",
            data={"username": self.panel.username, "password": self.panel.password},
            timeout=PANEL_TIMEOUT_SECONDS,
        )
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
        protocol = (inbound_data.get("protocol") or inbound.protocol or "vless").lower()
        address = inbound.server_ip or urlparse(self.panel.url).hostname or ""
        port = str(inbound_data.get("port") or inbound.port).strip()

        try:
            stream_settings = json.loads(inbound_data.get("streamSettings") or "{}")
        except (TypeError, ValueError):
            stream_settings = {}

        if protocol != "vless":
            fallback_params = inbound.config_params or build_vless_query_params(stream_settings, client_data)
            return f"{protocol}://{client_uuid}@{address}:{port}?{fallback_params}#{quote(email, safe='')}"

        params = build_vless_query_params(stream_settings, client_data)
        if not params:
            params = inbound.config_params or "type=tcp&security=none"
        return f"vless://{client_uuid}@{address}:{port}?{params}#{quote(email, safe='')}"

    def create_inactive_client(self, *, email_prefix, total_gb, expire_days, inbound, limit_ip=2):
        self.login()
        client_uuid = str(uuid.uuid4())
        sub_id = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        email = f"{email_prefix}_{client_uuid[:8]}"
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

    def update_client_enabled(self, order):
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

    def get_client_stats(self, vpn_client, *, use_cache=True):
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


def create_inactive_client_details(email_prefix, total_gb, expire_days, panel, inbound, limit_ip=2):
    try:
        return XUIService(panel).create_inactive_client(
            email_prefix=email_prefix,
            total_gb=total_gb,
            expire_days=expire_days,
            inbound=inbound,
            limit_ip=limit_ip,
        )
    except Exception as exc:
        logger.warning("Could not create inactive X-UI client: %s", exc)
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


def sync_inbound_data(panel_url, username, password, inbound_id):
    session = requests.Session()
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
        return XUIService(order.inbound.panel).update_client_enabled(order)
    except Exception as exc:
        logger.warning("Could not enable X-UI client: %s", exc)
        return False
