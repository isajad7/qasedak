from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import ConfigLink, CupItem, ExternalSubscriptionFeed, Panel, PlanDeliverySource
from .panels.errors import PanelIntegrationError
from .subscription_cups import apply_config_link_parse, create_config_link_from_raw


ALL_DYNAMIC_PROTOCOLS = (
    ConfigLink.Protocol.VLESS,
    ConfigLink.Protocol.VMESS,
    ConfigLink.Protocol.TROJAN,
    ConfigLink.Protocol.SS,
    ConfigLink.Protocol.HYSTERIA2,
    ConfigLink.Protocol.WIREGUARD,
)
ALL_DYNAMIC_SECURITY = ("reality", "tls", "none")
ALL_DYNAMIC_TRANSPORTS = (
    "tcp",
    "raw",
    "grpc",
    "ws",
    "xhttp",
    "splithttp",
    "http",
    "httpupgrade",
    "kcp",
    "quic",
    "udp",
    "wireguard",
)
DEFAULT_REFRESH_INTERVAL_HOURS = 12
DROP_PROTECTION_MIN_PREVIOUS_COUNT = 5
DROP_PROTECTION_RATIO = 0.70
FEED_ERROR_THRESHOLD = 2

POLICY_METADATA_KEY = "dynamic_subscription_policy"
SUPPORTED_SCHEMES = {*ALL_DYNAMIC_PROTOCOLS, "hy2"}
SECRET_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
CONFIG_RE = re.compile(r"\b(?:vless|vmess|trojan|ss|hysteria2|hy2|wireguard)://[^\s\"'<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedNativeConfig:
    raw_link: str
    protocol: str
    security: str = "none"
    transport: str = "tcp"
    remark: str = ""
    host: str = ""
    port: int | None = None
    has_pbk: bool = False
    semantic_fingerprint: str = ""
    valid: bool = False
    validation_reason: str = ""


@dataclass(frozen=True)
class NativeConfigFilterResult:
    policy: dict
    upstream_count: int = 0
    parsed_count: int = 0
    invalid_count: int = 0
    deduplicated_count: int = 0
    filtered_count: int = 0
    selected_count: int = 0
    selected_configs: list[ParsedNativeConfig] = field(default_factory=list)
    protocol_counts: dict = field(default_factory=dict)
    security_counts: dict = field(default_factory=dict)
    transport_counts: dict = field(default_factory=dict)
    invalid_reasons: dict = field(default_factory=dict)

    def to_safe_dict(self):
        return {
            "upstream_count": self.upstream_count,
            "parsed_count": self.parsed_count,
            "invalid_count": self.invalid_count,
            "deduplicated_count": self.deduplicated_count,
            "filtered_count": self.filtered_count,
            "selected_count": self.selected_count,
            "protocol_counts": dict(self.protocol_counts),
            "security_counts": dict(self.security_counts),
            "transport_counts": dict(self.transport_counts),
            "invalid_reasons": dict(self.invalid_reasons),
        }


@dataclass(frozen=True)
class ExternalSubscriptionRefreshSummary:
    feed_id: int | None
    ok: bool
    status: str
    dry_run: bool = False
    skipped: bool = False
    kept_last_good: bool = False
    error_code: str = ""
    upstream_count: int = 0
    parsed_count: int = 0
    invalid_count: int = 0
    deduplicated_count: int = 0
    filtered_count: int = 0
    selected_count: int = 0
    current_item_count: int = 0
    protocol_counts: dict = field(default_factory=dict)
    security_counts: dict = field(default_factory=dict)
    message: str = ""

    def to_safe_dict(self):
        return {
            "feed_id": self.feed_id,
            "ok": self.ok,
            "status": self.status,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "kept_last_good": self.kept_last_good,
            "error_code": self.error_code,
            "upstream_count": self.upstream_count,
            "parsed_count": self.parsed_count,
            "invalid_count": self.invalid_count,
            "deduplicated_count": self.deduplicated_count,
            "filtered_count": self.filtered_count,
            "selected_count": self.selected_count,
            "current_item_count": self.current_item_count,
            "protocol_counts": dict(self.protocol_counts),
            "security_counts": dict(self.security_counts),
            "message": safe_external_subscription_text(self.message),
        }


class ExternalSubscriptionRefreshError(Exception):
    def __init__(self, safe_message, *, code="external_subscription_refresh_failed"):
        super().__init__(safe_message)
        self.safe_message = safe_external_subscription_text(safe_message)
        self.code = str(code or "external_subscription_refresh_failed")[:80]


def safe_external_subscription_text(value, max_length=500):
    text = str(value or "")
    text = CONFIG_RE.sub("<config-link-redacted>", text)
    text = SECRET_URL_RE.sub("<url-redacted>", text)
    text = re.sub(r"(?i)(token|secret|password|api[_-]?key|x-api-key)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    return text[:max_length]


def default_external_subscription_filter_policy():
    return {
        "protocols": list(ALL_DYNAMIC_PROTOCOLS),
        "security": list(ALL_DYNAMIC_SECURITY),
        "transport": list(ALL_DYNAMIC_TRANSPORTS),
        "remark_include": "",
        "remark_exclude": "",
        "require_reality_pbk": True,
        "max_configs": None,
        "deduplicate_exact": True,
        "refresh_interval_hours": DEFAULT_REFRESH_INTERVAL_HOURS,
    }


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_positive_int(value, default=None, *, max_value=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    if max_value is not None:
        return min(number, max_value)
    return number


def _normalize_values(value, defaults, *, aliases=None):
    aliases = aliases or {}
    if value in (None, "", []):
        return list(defaults)
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",")]
    else:
        raw_values = [str(item or "").strip() for item in value]
    normalized = []
    allowed = set(defaults)
    for item in raw_values:
        lowered = aliases.get(item.lower(), item.lower())
        if lowered in {"*", "all"}:
            return list(defaults)
        if lowered in allowed and lowered not in normalized:
            normalized.append(lowered)
    return normalized or list(defaults)


def normalize_external_subscription_filter_policy(policy=None):
    base = default_external_subscription_filter_policy()
    policy = dict(policy or {})
    base["protocols"] = _normalize_values(policy.get("protocols"), ALL_DYNAMIC_PROTOCOLS, aliases={"hy2": ConfigLink.Protocol.HYSTERIA2})
    base["security"] = _normalize_values(policy.get("security"), ALL_DYNAMIC_SECURITY)
    base["transport"] = _normalize_values(policy.get("transport") or policy.get("transports"), ALL_DYNAMIC_TRANSPORTS)
    base["remark_include"] = str(policy.get("remark_include") or "").strip()[:200]
    base["remark_exclude"] = str(policy.get("remark_exclude") or "").strip()[:200]
    base["require_reality_pbk"] = _as_bool(policy.get("require_reality_pbk"), True)
    base["deduplicate_exact"] = _as_bool(policy.get("deduplicate_exact"), True)
    base["max_configs"] = _as_positive_int(policy.get("max_configs"), None, max_value=1000)
    base["refresh_interval_hours"] = _as_positive_int(
        policy.get("refresh_interval_hours"),
        DEFAULT_REFRESH_INTERVAL_HOURS,
        max_value=24 * 30,
    )
    return base


def source_supports_dynamic_subscription(source):
    if not source or source.source_type != PlanDeliverySource.SourceType.PANEL_INBOUND:
        return False
    panel = getattr(source, "panel", None) or getattr(getattr(source, "inbound", None), "panel", None)
    return str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD


def resolved_filter_policy_for_source(source):
    metadata = dict(getattr(source, "metadata", {}) or {})
    return normalize_external_subscription_filter_policy(metadata.get(POLICY_METADATA_KEY) or metadata.get("filter_policy") or {})


def _query_first(query, *keys, default=""):
    for key in keys:
        values = query.get(key)
        if values:
            return str(values[0] or "").strip().lower()
    return default


def _query_has(query, *keys):
    return any(str(value or "").strip() for key in keys for value in query.get(key, []))


def _safe_port(parts):
    try:
        return parts.port
    except ValueError:
        return None


def _canonical_protocol(value):
    protocol = str(value or "").strip().lower()
    if protocol == "hy2":
        return ConfigLink.Protocol.HYSTERIA2
    return protocol


def _semantic_fingerprint(*, protocol, security, transport, host, port, remark, has_pbk, valid, validation_reason):
    payload = {
        "protocol": protocol or "",
        "security": security or "",
        "transport": transport or "",
        "host": host or "",
        "port": port,
        "remark": (remark or "").strip().lower(),
        "has_pbk": bool(has_pbk),
        "valid": bool(valid),
        "validation_reason": validation_reason or "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _decode_vmess_payload(payload):
    payload = str(payload or "").strip()
    if not payload:
        return {}
    padded = f"{payload}{'=' * (-len(payload) % 4)}"
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")
        data = json.loads(decoded)
    except (binascii.Error, UnicodeEncodeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_vmess(raw_link, body):
    data = _decode_vmess_payload(body.split("#", 1)[0])
    host = str(data.get("add") or "").strip()[:255]
    port = _as_positive_int(data.get("port"), None, max_value=65535)
    remark = str(data.get("ps") or "").strip()[:255]
    transport = str(data.get("net") or "tcp").strip().lower() or "tcp"
    tls_value = str(data.get("tls") or "").strip().lower()
    security = tls_value if tls_value in ALL_DYNAMIC_SECURITY else ("tls" if tls_value else "none")
    valid = bool(data)
    reason = "" if valid else "invalid_vmess_payload"
    return _parsed(raw_link, ConfigLink.Protocol.VMESS, security, transport, remark, host, port, False, valid, reason)


def _parse_url_style(raw_link, protocol):
    candidate = str(raw_link or "").strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return _parsed(raw_link, protocol, "none", "tcp", "", "", None, False, False, "invalid_url")
    raw_query = parse_qs(parts.query, keep_blank_values=True)
    query = {str(key).lower(): value for key, value in raw_query.items()}
    remark = unquote(parts.fragment or "").strip()[:255]
    host = (parts.hostname or "").strip()[:255]
    port = _safe_port(parts)
    security = _query_first(query, "security", "securitytype", default="")
    if not security:
        security = "tls" if _query_has(query, "tls") else "none"
    if security not in ALL_DYNAMIC_SECURITY:
        security = "none"
    default_transport = "wireguard" if protocol == ConfigLink.Protocol.WIREGUARD else "tcp"
    transport = _query_first(query, "type", "transport", "network", "net", default=default_transport) or default_transport
    has_pbk = _query_has(query, "pbk", "publickey", "public_key")
    valid = protocol in ALL_DYNAMIC_PROTOCOLS
    reason = "" if valid else "unsupported_protocol"
    if valid and security == "reality" and not has_pbk:
        valid = False
        reason = "reality_missing_pbk"
    return _parsed(raw_link, protocol, security, transport, remark, host, port, has_pbk, valid, reason)


def _parsed(raw_link, protocol, security, transport, remark, host, port, has_pbk, valid, reason):
    protocol = _canonical_protocol(protocol)
    security = str(security or "none").strip().lower() or "none"
    transport = str(transport or "tcp").strip().lower() or "tcp"
    fingerprint = _semantic_fingerprint(
        protocol=protocol,
        security=security,
        transport=transport,
        host=host,
        port=port,
        remark=remark,
        has_pbk=has_pbk,
        valid=valid,
        validation_reason=reason,
    )
    return ParsedNativeConfig(
        raw_link=str(raw_link or ""),
        protocol=protocol,
        security=security,
        transport=transport,
        remark=str(remark or "")[:255],
        host=str(host or "")[:255],
        port=port,
        has_pbk=bool(has_pbk),
        semantic_fingerprint=fingerprint,
        valid=bool(valid),
        validation_reason=reason,
    )


def parse_native_config(raw_link):
    raw = str(raw_link or "")
    candidate = raw.strip()
    if "://" not in candidate:
        return _parsed(raw, ConfigLink.Protocol.UNKNOWN, "none", "tcp", "", "", None, False, False, "unsupported_protocol")
    scheme, body = candidate.split("://", 1)
    protocol = _canonical_protocol(scheme)
    if protocol not in ALL_DYNAMIC_PROTOCOLS:
        return _parsed(raw, ConfigLink.Protocol.UNKNOWN, "none", "tcp", "", "", None, False, False, "unsupported_protocol")
    if protocol == ConfigLink.Protocol.VMESS:
        return _parse_vmess(raw, body)
    return _parse_url_style(raw, protocol)


def _remark_matches(parsed, policy):
    remark = (parsed.remark or "").lower()
    include = str(policy.get("remark_include") or "").strip().lower()
    exclude = str(policy.get("remark_exclude") or "").strip().lower()
    if include and include not in remark:
        return False
    if exclude and exclude in remark:
        return False
    return True


def filter_native_configs(raw_configs, policy=None):
    policy = normalize_external_subscription_filter_policy(policy)
    raw_values = [str(item or "") for item in list(raw_configs or []) if str(item or "")]
    parsed = [parse_native_config(raw_link) for raw_link in raw_values]
    valid_configs = [item for item in parsed if item.valid]
    invalid_reasons = Counter(item.validation_reason or "invalid" for item in parsed if not item.valid)

    if policy["deduplicate_exact"]:
        seen = set()
        deduped = []
        for item in valid_configs:
            if item.raw_link in seen:
                continue
            seen.add(item.raw_link)
            deduped.append(item)
    else:
        deduped = list(valid_configs)
    deduplicated_count = len(valid_configs) - len(deduped)

    filtered = []
    for item in deduped:
        if item.protocol not in policy["protocols"]:
            continue
        if item.security not in policy["security"]:
            continue
        if item.transport not in policy["transport"]:
            continue
        if policy["require_reality_pbk"] and item.security == "reality" and not item.has_pbk:
            continue
        if not _remark_matches(item, policy):
            continue
        filtered.append(item)

    max_configs = policy.get("max_configs")
    selected = filtered[:max_configs] if max_configs else list(filtered)
    return NativeConfigFilterResult(
        policy=policy,
        upstream_count=len(raw_values),
        parsed_count=len(parsed),
        invalid_count=len(parsed) - len(valid_configs),
        deduplicated_count=deduplicated_count,
        filtered_count=len(filtered),
        selected_count=len(selected),
        selected_configs=selected,
        protocol_counts=dict(Counter(item.protocol for item in valid_configs)),
        security_counts=dict(Counter(item.security for item in valid_configs)),
        transport_counts=dict(Counter(item.transport for item in valid_configs)),
        invalid_reasons=dict(invalid_reasons),
    )


class ExternalSubscriptionFetcher:
    provider = ""

    def fetch_native_links(self, feed):
        raise NotImplementedError


class PasarGuardSubscriptionFetcher(ExternalSubscriptionFetcher):
    provider = Panel.Family.PASARGUARD

    def __init__(self, adapter_factory=None):
        if adapter_factory is None:
            from .panels import get_safe_panel_adapter

            adapter_factory = get_safe_panel_adapter
        self.adapter_factory = adapter_factory

    def fetch_native_links(self, feed):
        panel = getattr(feed, "panel", None)
        if not panel:
            raise ExternalSubscriptionRefreshError("Feed has no panel.", code="external_feed_panel_missing")
        subscription_url = str(getattr(feed, "protected_subscription_url", "") or "").strip()
        if not subscription_url:
            raise ExternalSubscriptionRefreshError("Feed has no upstream subscription URL.", code="external_feed_subscription_url_missing")
        try:
            adapter = self.adapter_factory(panel)
            client = getattr(adapter, "client", None)
            if not client or not hasattr(client, "fetch_native_links"):
                raise ExternalSubscriptionRefreshError("Provider fetcher is not available.", code="external_feed_fetcher_unavailable")
            return list(client.fetch_native_links(subscription_url) or [])
        except ExternalSubscriptionRefreshError:
            raise
        except PanelIntegrationError as exc:
            raise ExternalSubscriptionRefreshError(
                getattr(exc, "message", "") or "Provider fetch failed.",
                code=getattr(exc, "error_code", "") or "provider_fetch_failed",
            ) from exc
        except Exception as exc:
            raise ExternalSubscriptionRefreshError("Provider fetch failed.", code="provider_fetch_failed") from exc


def get_external_subscription_fetcher(provider, *, adapter_factory=None):
    provider = str(provider or "").strip().lower()
    if provider == Panel.Family.PASARGUARD:
        return PasarGuardSubscriptionFetcher(adapter_factory=adapter_factory)
    raise ExternalSubscriptionRefreshError("Provider is not supported.", code="external_feed_provider_unsupported")


def next_refresh_at_for_feed(feed, *, now=None, interval_hours=None):
    now = now or timezone.now()
    interval_hours = _as_positive_int(interval_hours, getattr(feed, "refresh_interval_hours", None) or DEFAULT_REFRESH_INTERVAL_HOURS)
    interval_seconds = interval_hours * 60 * 60
    jitter_window = min(30 * 60, max(60, interval_seconds // 10))
    identity = f"{getattr(feed, 'pk', '')}:{getattr(feed, 'provider', '')}:{getattr(feed, 'remote_identity_ref', '')}"
    jitter_seconds = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16) % jitter_window
    return now + timedelta(seconds=interval_seconds + jitter_seconds)


def source_owned_cup_item_count(feed):
    if not getattr(feed, "pk", None):
        return 0
    return CupItem.objects.filter(
        cup=feed.cup,
        is_active=True,
        config_link__is_active=True,
        config_link__external_feed=feed,
    ).count()


def _summary_from_filter(feed, filter_result, *, ok, status, dry_run=False, kept_last_good=False, error_code="", message="", skipped=False):
    return ExternalSubscriptionRefreshSummary(
        feed_id=getattr(feed, "pk", None),
        ok=ok,
        status=status,
        dry_run=dry_run,
        skipped=skipped,
        kept_last_good=kept_last_good,
        error_code=error_code,
        upstream_count=filter_result.upstream_count if filter_result else 0,
        parsed_count=filter_result.parsed_count if filter_result else 0,
        invalid_count=filter_result.invalid_count if filter_result else 0,
        deduplicated_count=filter_result.deduplicated_count if filter_result else 0,
        filtered_count=filter_result.filtered_count if filter_result else 0,
        selected_count=filter_result.selected_count if filter_result else 0,
        current_item_count=source_owned_cup_item_count(feed) if getattr(feed, "pk", None) else 0,
        protocol_counts=filter_result.protocol_counts if filter_result else {},
        security_counts=filter_result.security_counts if filter_result else {},
        message=message,
    )


def _mark_feed_failure(feed, *, code, message="", filter_result=None, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        feed = ExternalSubscriptionFeed.objects.select_for_update().select_related("cup").get(pk=feed.pk)
        feed.last_attempt_at = now
        feed.consecutive_failures += 1
        feed.status = ExternalSubscriptionFeed.Status.ERROR if feed.consecutive_failures >= FEED_ERROR_THRESHOLD else ExternalSubscriptionFeed.Status.DEGRADED
        feed.last_error_code = str(code or "external_subscription_refresh_failed")[:80]
        if filter_result:
            feed.last_seen_upstream_count = filter_result.upstream_count
            feed.last_filtered_count = filter_result.filtered_count
        feed.next_refresh_at = next_refresh_at_for_feed(feed, now=now)
        metadata = dict(feed.metadata or {})
        metadata["last_refresh_error"] = {
            "code": feed.last_error_code,
            "message": safe_external_subscription_text(message),
            "at": now.isoformat(),
            "kept_last_good": True,
        }
        if filter_result:
            metadata["last_filter_preview"] = filter_result.to_safe_dict()
        feed.metadata = metadata
        feed.save(
            update_fields=[
                "last_attempt_at",
                "consecutive_failures",
                "status",
                "last_error_code",
                "last_seen_upstream_count",
                "last_filtered_count",
                "next_refresh_at",
                "metadata",
                "updated_at",
            ]
        )
    return _summary_from_filter(
        feed,
        filter_result,
        ok=False,
        status=feed.status,
        kept_last_good=True,
        error_code=feed.last_error_code,
        message=message,
    )


def _config_metadata(feed, parsed, *, position):
    return {
        "source": "external_subscription_feed",
        "external_feed_id": feed.pk,
        "provider": feed.provider,
        "position": position,
        "protocol": parsed.protocol,
        "security": parsed.security,
        "transport": parsed.transport,
        "host": parsed.host,
        "port": parsed.port,
        "remark": parsed.remark,
        "has_pbk": parsed.has_pbk,
        "semantic_fingerprint": parsed.semantic_fingerprint,
    }


def _link_by_raw(existing_items):
    by_raw = {}
    for item in existing_items:
        raw_link = item.config_link.raw_link
        by_raw.setdefault(raw_link, []).append(item)
    return by_raw


def _reconcile_success(feed, filter_result, *, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        feed = ExternalSubscriptionFeed.objects.select_for_update().select_related("cup").get(pk=feed.pk)
        existing_items = list(
            CupItem.objects.select_for_update()
            .select_related("config_link")
            .filter(cup=feed.cup, config_link__external_feed=feed)
            .order_by("position", "pk")
        )
        reusable = _link_by_raw(existing_items)
        existing_positions = [item.position for item in existing_items]
        if existing_positions:
            base_position = min(existing_positions)
        else:
            max_position = (
                CupItem.objects.filter(cup=feed.cup, is_active=True)
                .exclude(config_link__external_feed=feed)
                .aggregate(max_position=Max("position"))
                .get("max_position")
            )
            base_position = int(max_position or 0) + 1

        active_item_ids = []
        active_link_ids = []
        for offset, parsed in enumerate(filter_result.selected_configs):
            position = base_position + offset
            item = None
            candidates = reusable.get(parsed.raw_link) or []
            if candidates:
                item = candidates.pop(0)
            metadata = _config_metadata(feed, parsed, position=position)
            if item:
                config_link = item.config_link
                apply_config_link_parse(
                    config_link,
                    parsed.raw_link,
                    source_type=ConfigLink.SourceType.EXTERNAL_SUBSCRIPTION,
                    source_panel=feed.panel,
                    source_inbound=None,
                    vpn_client=feed.vpn_client,
                    external_feed=feed,
                    metadata=metadata,
                )
                config_link.protocol = parsed.protocol
                config_link.remark = parsed.remark
                config_link.host = parsed.host
                config_link.port = parsed.port
                config_link.save()
                item.position = position
                item.is_active = True
                item.added_reason = "external_subscription_feed"
                item.metadata = metadata
                item.save(update_fields=["position", "is_active", "added_reason", "metadata", "updated_at"])
            else:
                config_link = create_config_link_from_raw(
                    parsed.raw_link,
                    source_type=ConfigLink.SourceType.EXTERNAL_SUBSCRIPTION,
                    source_panel=feed.panel,
                    vpn_client=feed.vpn_client,
                    external_feed=feed,
                    metadata=metadata,
                )
                config_link.protocol = parsed.protocol
                config_link.remark = parsed.remark
                config_link.host = parsed.host
                config_link.port = parsed.port
                config_link.save(update_fields=["protocol", "remark", "host", "port", "updated_at"])
                item = CupItem.objects.create(
                    cup=feed.cup,
                    config_link=config_link,
                    position=position,
                    is_active=True,
                    added_reason="external_subscription_feed",
                    metadata=metadata,
                )
            active_item_ids.append(item.pk)
            active_link_ids.append(config_link.pk)

        stale_items = [item for item in existing_items if item.pk not in active_item_ids]
        stale_item_ids = [item.pk for item in stale_items]
        stale_link_ids = [item.config_link_id for item in stale_items]
        if stale_item_ids:
            CupItem.objects.filter(pk__in=stale_item_ids).update(is_active=False, updated_at=now)
        if stale_link_ids:
            ConfigLink.objects.filter(pk__in=stale_link_ids, external_feed=feed).update(is_active=False, updated_at=now)

        feed.status = ExternalSubscriptionFeed.Status.HEALTHY
        feed.active = True
        feed.last_attempt_at = now
        feed.last_success_at = now
        feed.consecutive_failures = 0
        feed.last_error_code = ""
        feed.last_good_config_count = filter_result.selected_count
        feed.last_seen_upstream_count = filter_result.upstream_count
        feed.last_filtered_count = filter_result.filtered_count
        feed.refresh_interval_hours = filter_result.policy["refresh_interval_hours"]
        feed.next_refresh_at = next_refresh_at_for_feed(feed, now=now, interval_hours=feed.refresh_interval_hours)
        metadata = dict(feed.metadata or {})
        metadata["last_successful_refresh"] = {
            "at": now.isoformat(),
            **filter_result.to_safe_dict(),
        }
        metadata.pop("last_refresh_error", None)
        feed.metadata = metadata
        feed.save(
            update_fields=[
                "status",
                "active",
                "last_attempt_at",
                "last_success_at",
                "consecutive_failures",
                "last_error_code",
                "last_good_config_count",
                "last_seen_upstream_count",
                "last_filtered_count",
                "refresh_interval_hours",
                "next_refresh_at",
                "metadata",
                "updated_at",
            ]
        )

    return _summary_from_filter(feed, filter_result, ok=True, status=ExternalSubscriptionFeed.Status.HEALTHY)


def _should_block_massive_drop(feed, filter_result, *, force=False):
    if force:
        return False
    previous_count = int(getattr(feed, "last_good_config_count", 0) or source_owned_cup_item_count(feed) or 0)
    if previous_count < DROP_PROTECTION_MIN_PREVIOUS_COUNT:
        return False
    selected_count = int(filter_result.selected_count or 0)
    lost_count = previous_count - selected_count
    return lost_count > previous_count * DROP_PROTECTION_RATIO


def refresh_external_subscription_feed(feed_id, *, force=False, dry_run=False, candidate_raw_links=None, adapter_factory=None):
    feed = (
        ExternalSubscriptionFeed.objects.select_related("cup", "panel", "vpn_client", "delivery_source")
        .get(pk=getattr(feed_id, "pk", feed_id))
    )
    if (not feed.active or feed.status == ExternalSubscriptionFeed.Status.DISABLED) and not force:
        return ExternalSubscriptionRefreshSummary(
            feed_id=feed.pk,
            ok=False,
            status=ExternalSubscriptionFeed.Status.DISABLED,
            skipped=True,
            current_item_count=source_owned_cup_item_count(feed),
            error_code="external_feed_disabled",
            message="Feed is disabled.",
        )

    try:
        raw_links = list(candidate_raw_links) if candidate_raw_links is not None else get_external_subscription_fetcher(
            feed.provider,
            adapter_factory=adapter_factory,
        ).fetch_native_links(feed)
    except ExternalSubscriptionRefreshError as exc:
        if dry_run:
            return ExternalSubscriptionRefreshSummary(
                feed_id=feed.pk,
                ok=False,
                status=feed.status,
                dry_run=True,
                kept_last_good=True,
                current_item_count=source_owned_cup_item_count(feed),
                error_code=exc.code,
                message=exc.safe_message,
            )
        return _mark_feed_failure(feed, code=exc.code, message=exc.safe_message)

    filter_result = filter_native_configs(raw_links, feed.resolved_filter_policy)
    failure_code = ""
    if filter_result.selected_count == 0:
        failure_code = "upstream_empty" if filter_result.upstream_count == 0 else "filter_result_empty"
    elif _should_block_massive_drop(feed, filter_result, force=force):
        failure_code = "suspicious_config_drop"
    if dry_run:
        return _summary_from_filter(
            feed,
            filter_result,
            ok=not failure_code,
            status=feed.status,
            dry_run=True,
            kept_last_good=bool(failure_code),
            error_code=failure_code,
            message=failure_code or "Dry-run preview only.",
        )
    if failure_code:
        return _mark_feed_failure(
            feed,
            code=failure_code,
            message=failure_code,
            filter_result=filter_result,
        )
    return _reconcile_success(feed, filter_result)


def refresh_due_external_subscription_feeds(*, force=False, limit=None, dry_run=False, adapter_factory=None, now=None):
    now = now or timezone.now()
    queryset = ExternalSubscriptionFeed.objects.filter(active=True).exclude(status=ExternalSubscriptionFeed.Status.DISABLED)
    if not force:
        queryset = queryset.filter(next_refresh_at__lte=now)
    queryset = queryset.select_related("cup", "panel", "vpn_client", "delivery_source").order_by("next_refresh_at", "pk")
    if limit:
        queryset = queryset[: max(int(limit), 0)]
    results = []
    for feed in list(queryset):
        try:
            results.append(
                refresh_external_subscription_feed(
                    feed.pk,
                    force=force,
                    dry_run=dry_run,
                    adapter_factory=adapter_factory,
                )
            )
        except Exception as exc:
            results.append(
                ExternalSubscriptionRefreshSummary(
                    feed_id=feed.pk,
                    ok=False,
                    status=ExternalSubscriptionFeed.Status.ERROR,
                    error_code="external_feed_refresh_crashed",
                    current_item_count=source_owned_cup_item_count(feed),
                    message=safe_external_subscription_text(exc),
                    kept_last_good=True,
                )
            )
    return {
        "checked": len(results),
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok and not result.skipped),
        "skipped": sum(1 for result in results if result.skipped),
        "dry_run": dry_run,
        "results": [result.to_safe_dict() for result in results],
    }


def preview_external_subscription_filter(raw_links, *, source=None, policy=None):
    resolved_policy = policy or (resolved_filter_policy_for_source(source) if source else None)
    return filter_native_configs(raw_links, resolved_policy)


def register_external_subscription_feed_snapshot(
    *,
    cup,
    source,
    panel,
    vpn_client=None,
    protected_subscription_url,
    remote_identity_ref="",
    raw_links,
    config_links,
    filter_result=None,
    provider=None,
    metadata=None,
):
    if not cup or not getattr(cup, "pk", None):
        raise ExternalSubscriptionRefreshError("Subscription cup is required.", code="external_feed_cup_required")
    if not str(protected_subscription_url or "").strip():
        raise ExternalSubscriptionRefreshError("Upstream subscription URL is required.", code="external_feed_subscription_url_missing")
    provider = str(provider or getattr(panel, "family", "") or "").strip().lower()
    policy = resolved_filter_policy_for_source(source)
    filter_result = filter_result or filter_native_configs(raw_links, policy)
    if filter_result.selected_count == 0:
        raise ExternalSubscriptionRefreshError("Initial filter result is empty.", code="filter_result_empty")
    now = timezone.now()
    lookup = {
        "cup": cup,
        "delivery_source": source,
        "panel": panel,
        "vpn_client": vpn_client,
        "provider": provider,
    }
    with transaction.atomic():
        feed = (
            ExternalSubscriptionFeed.objects.select_for_update()
            .filter(**lookup)
            .order_by("pk")
            .first()
        )
        if not feed:
            feed = ExternalSubscriptionFeed(**lookup)
        feed.active = True
        feed.status = ExternalSubscriptionFeed.Status.HEALTHY
        feed.remote_identity_ref = str(remote_identity_ref or "")[:160]
        feed.protected_subscription_url = str(protected_subscription_url or "").strip()
        feed.resolved_filter_policy = filter_result.policy
        feed.refresh_interval_hours = filter_result.policy["refresh_interval_hours"]
        feed.last_attempt_at = now
        feed.last_success_at = now
        feed.consecutive_failures = 0
        feed.last_error_code = ""
        feed.last_good_config_count = filter_result.selected_count
        feed.last_seen_upstream_count = filter_result.upstream_count
        feed.last_filtered_count = filter_result.filtered_count
        feed.metadata = {
            **(feed.metadata or {}),
            **(metadata or {}),
            "subscription_url_saved": True,
            "initial_snapshot": True,
            "last_successful_refresh": {
                "at": now.isoformat(),
                **filter_result.to_safe_dict(),
            },
        }
        feed.save()
        feed.next_refresh_at = next_refresh_at_for_feed(feed, now=now, interval_hours=feed.refresh_interval_hours)
        feed.save(update_fields=["next_refresh_at", "updated_at"])

        parsed_by_raw = {item.raw_link: item for item in filter_result.selected_configs}
        selected_link_ids = []
        for position, config_link in enumerate(config_links, start=1):
            parsed = parsed_by_raw.get(config_link.raw_link) or parse_native_config(config_link.raw_link)
            config_metadata = {
                **(config_link.metadata or {}),
                **_config_metadata(feed, parsed, position=position),
            }
            apply_config_link_parse(
                config_link,
                config_link.raw_link,
                source_type=ConfigLink.SourceType.EXTERNAL_SUBSCRIPTION,
                source_panel=panel,
                source_inbound=getattr(source, "inbound", None),
                vpn_client=vpn_client,
                external_feed=feed,
                metadata=config_metadata,
            )
            config_link.protocol = parsed.protocol
            config_link.remark = parsed.remark
            config_link.host = parsed.host
            config_link.port = parsed.port
            config_link.save()
            selected_link_ids.append(config_link.pk)

        CupItem.objects.filter(cup=cup, config_link_id__in=selected_link_ids).update(
            added_reason="external_subscription_feed",
            updated_at=now,
        )
        stale_items = CupItem.objects.filter(cup=cup, config_link__external_feed=feed).exclude(config_link_id__in=selected_link_ids)
        stale_link_ids = list(stale_items.values_list("config_link_id", flat=True))
        stale_items.update(is_active=False, updated_at=now)
        if stale_link_ids:
            ConfigLink.objects.filter(pk__in=stale_link_ids, external_feed=feed).update(is_active=False, updated_at=now)
    return feed


def external_feed_health_summary_for_panel(panel):
    feeds = list(
        ExternalSubscriptionFeed.objects.filter(panel=panel, active=True)
        .exclude(status=ExternalSubscriptionFeed.Status.DISABLED)
        .order_by("pk")
    )
    problem_statuses = {ExternalSubscriptionFeed.Status.DEGRADED, ExternalSubscriptionFeed.Status.ERROR}
    problem_feeds = [feed for feed in feeds if feed.status in problem_statuses or feed.consecutive_failures]
    return {
        "feed_count": len(feeds),
        "problem_feed_count": len(problem_feeds),
        "status_counts": dict(Counter(feed.status for feed in feeds)),
        "issues": [
            {
                "feed_id": feed.pk,
                "status": feed.status,
                "last_error_code": feed.last_error_code,
                "consecutive_failures": feed.consecutive_failures,
                "last_seen_upstream_count": feed.last_seen_upstream_count,
                "last_filtered_count": feed.last_filtered_count,
                "last_good_config_count": feed.last_good_config_count,
            }
            for feed in problem_feeds[:20]
        ],
    }
