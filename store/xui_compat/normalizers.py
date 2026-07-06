from urllib.parse import urlsplit

from django.utils import timezone

from .dto import XUIClient, XUIInbound, XUINode, XUIUsage
from .errors import XUIErrorClassification


SECRET_KEYS = {"password", "token", "apiToken", "api_token", "uuid", "id", "subId", "sub_id", "link"}


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _string(value):
    return str(value or "").strip()


def _bool_or_none(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "online"}


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _datetime_from_epoch(value):
    timestamp = _int_or_none(value)
    if not timestamp:
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timezone.datetime.fromtimestamp(timestamp, tz=timezone.get_current_timezone())


def safe_address_label(payload):
    scheme = _string(payload.get("scheme")) or "https"
    address = _string(payload.get("address") or payload.get("host") or payload.get("server"))
    port = _string(payload.get("port"))
    if not address:
        return ""
    parsed = urlsplit(address if "://" in address else f"{scheme}://{address}")
    host = parsed.hostname or address.split("@")[-1].split("/")[0]
    if parsed.port and not port:
        port = str(parsed.port)
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def redact_sensitive(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret.lower() in lowered for secret in SECRET_KEYS):
                clean[key] = "<redacted>"
            else:
                clean[key] = redact_sensitive(item)
        return clean
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str) and "://" in value and ("@" in value or "/sub/" in value):
        return "<redacted-url>"
    return value


def normalize_node(payload, context=None):
    payload = payload or {}
    context = context or {}
    external_node_id = _string(_first(payload.get("guid"), payload.get("id"), context.get("node_id")))
    status = _string(payload.get("status")) or "unknown"
    return XUINode(
        external_node_id=external_node_id,
        name=_string(_first(payload.get("name"), payload.get("remark"), external_node_id)),
        status=status,
        is_local=bool(context.get("is_local")) or external_node_id in {"", "0", "local"},
        enabled=_bool_or_none(payload.get("enable")),
        last_seen=_datetime_from_epoch(_first(payload.get("lastHeartbeat"), payload.get("updatedAt"))),
        capabilities={
            "inbound_sync_mode": _string(payload.get("inboundSyncMode")),
            "outbound_tag": bool(payload.get("outboundTag")),
            "transitive": bool(payload.get("transitive")),
            "xray_state": _string(payload.get("xrayState")),
        },
        safe_address_label=safe_address_label(payload),
    )


def normalize_inbound(payload, context=None):
    payload = payload or {}
    context = context or {}
    panel_id = context.get("panel_id")
    node_external_id = _string(
        _first(payload.get("originNodeGuid"), payload.get("nodeGuid"), payload.get("nodeId"), context.get("node_id"))
    )
    inbound_external_id = _string(_first(payload.get("id"), payload.get("inboundId"), context.get("inbound_id")))
    node_name = _string(context.get("node_name"))
    if not node_name and node_external_id:
        node_name = _string(context.get("node_names", {}).get(node_external_id))
    source = "synchronized_node" if node_external_id else "local"
    remote_key = ":".join([str(panel_id or ""), node_external_id or "local", inbound_external_id])
    return XUIInbound(
        panel_id=panel_id,
        node_external_id=node_external_id,
        node_name=node_name,
        inbound_external_id=inbound_external_id,
        remote_key=remote_key,
        remark=_string(payload.get("remark")),
        protocol=_string(payload.get("protocol")).lower(),
        active=_bool_or_none(payload.get("enable")),
        source=source,
        managed_share_address=_string(payload.get("shareAddr")),
        share_address_strategy=_string(payload.get("shareAddrStrategy")),
        sync_mode=_string(context.get("sync_mode")),
        usage_quality="complete" if payload.get("clientStats") is not None else "unknown",
    )


def normalize_usage(payload, context=None):
    payload = payload or {}
    upload = _int_or_none(_first(payload.get("up"), payload.get("upload")))
    download = _int_or_none(_first(payload.get("down"), payload.get("download")))
    used = _int_or_none(_first(payload.get("used"), payload.get("usedTraffic"), payload.get("used_traffic")))
    if used is None and upload is not None and download is not None:
        used = upload + download
    total = _int_or_none(_first(payload.get("total"), payload.get("totalGB")))
    quality = "complete" if used is not None else "unknown"
    if context and context.get("partial"):
        quality = "partial"
    return XUIUsage(
        upload_bytes=upload,
        download_bytes=download,
        used_bytes=used,
        total_bytes=total,
        quality=quality,
        source=_string((context or {}).get("source")),
        online=_bool_or_none(_first(payload.get("online"), payload.get("isOnline"))),
    )


def normalize_client(payload, context=None):
    payload = payload or {}
    context = context or {}
    remote_client_id = _string(_first(payload.get("uuid"), payload.get("id"), payload.get("password"), payload.get("email")))
    credential_type = "uuid" if payload.get("id") or payload.get("uuid") else "password" if payload.get("password") else "email"
    node_external_id = _string(context.get("node_external_id") or context.get("node_id"))
    inbound_external_id = _string(context.get("inbound_external_id") or context.get("inbound_id"))
    panel_id = _string(context.get("panel_id"))
    remote_key = ":".join([panel_id, node_external_id or "local", inbound_external_id, remote_client_id])
    usage = normalize_usage(payload.get("traffic") or payload, context={"source": context.get("source")}).__dict__
    return XUIClient(
        remote_client_id=remote_client_id,
        email=_string(payload.get("email")),
        credential_type=credential_type,
        inbound_external_id=inbound_external_id,
        node_external_id=node_external_id,
        remote_key=remote_key,
        enabled=_bool_or_none(payload.get("enable")),
        expiry=_datetime_from_epoch(payload.get("expiryTime")),
        traffic_limit=_int_or_none(_first(payload.get("totalGB"), payload.get("total"))),
        usage=usage,
        online=usage.get("online"),
        source=_string(context.get("source")),
    )


def classify_xui_error(response_or_exception):
    status_code = getattr(response_or_exception, "status_code", None)
    text = ""
    success = None
    if hasattr(response_or_exception, "json"):
        try:
            payload = response_or_exception.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            success = payload.get("success")
            text = _string(payload.get("msg") or payload.get("error"))
    if not text:
        text = _string(response_or_exception)
    lowered = text.lower()
    if status_code in {401, 403} or "auth" in lowered or "login" in lowered or "unauthorized" in lowered:
        return XUIErrorClassification("auth_failed", "Authentication failed.", auth_failed=True)
    if status_code == 404 or "not found" in lowered or "record not found" in lowered:
        return XUIErrorClassification("not_found", "Remote object was not found.", not_found=True)
    if "timeout" in lowered or "connection" in lowered or "offline" in lowered or "no route" in lowered:
        return XUIErrorClassification("node_offline", "Node or panel is unreachable.", retryable=True, node_offline=True)
    if success is False:
        return XUIErrorClassification("api_rejected", text or "API rejected the request.")
    return XUIErrorClassification("unknown_error", text or response_or_exception.__class__.__name__)
