import re
from dataclasses import asdict, dataclass, field
import json

from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from .normalizers import normalize_inbound, normalize_node, redact_sensitive


PROFILE_LEGACY_SINGLE_NODE = "legacy_single_node"
PROFILE_MODERN_SINGLE_NODE = "modern_single_node"
PROFILE_MODERN_MULTI_NODE = "modern_multi_node"
PROFILE_UNKNOWN_SAFE = "unknown_safe"

CAPABILITY_CACHE_SECONDS = 15 * 60


@dataclass(frozen=True)
class XUICapabilities:
    supports_nodes: bool = False
    supports_node_filter: bool = False
    supports_node_metrics: bool = False
    supports_global_client_usage: bool = False
    supports_managed_hosts: bool = False
    supports_share_address_strategy: bool = False
    supports_client_used_traffic: bool = False
    supports_online_stats: bool = False
    supports_api_token_auth: bool = False
    supports_precise_client_scope: bool = False
    supports_live_config_apply: bool = False


@dataclass(frozen=True)
class XUICompatibilityProfile:
    profile: str = PROFILE_UNKNOWN_SAFE
    version: str = ""
    capabilities: XUICapabilities = field(default_factory=XUICapabilities)
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "profile": self.profile,
            "version": self.version,
            "capabilities": asdict(self.capabilities),
            "metadata": self.metadata,
        }


def _version_tuple(version):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _extract_version(*payloads):
    keys = (
        "version",
        "panelVersion",
        "currentVersion",
        "current_version",
        "curVersion",
        "installedVersion",
        "installed_version",
    )
    for payload in payloads:
        if isinstance(payload, dict):
            candidates = [payload]
            obj = payload.get("obj")
            if isinstance(obj, dict):
                candidates.append(obj)
            for candidate in candidates:
                for key in keys:
                    value = candidate.get(key)
                    if value:
                        return str(value)
    return ""


def _json_safe(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def capabilities_for_profile(profile, version=""):
    parsed = _version_tuple(version)
    is_33_plus = bool(parsed and parsed >= (3, 3, 0))
    is_34_plus = bool(parsed and parsed >= (3, 4, 0))
    if profile == PROFILE_LEGACY_SINGLE_NODE:
        return XUICapabilities(
            supports_client_used_traffic=True,
            supports_online_stats=True,
        )
    if profile == PROFILE_MODERN_SINGLE_NODE:
        return XUICapabilities(
            supports_global_client_usage=is_33_plus,
            supports_client_used_traffic=True,
            supports_online_stats=True,
            supports_api_token_auth=is_33_plus,
            supports_precise_client_scope=True,
            supports_live_config_apply=is_33_plus,
        )
    if profile == PROFILE_MODERN_MULTI_NODE:
        return XUICapabilities(
            supports_nodes=True,
            supports_node_filter=True,
            supports_node_metrics=True,
            supports_global_client_usage=True,
            supports_managed_hosts=is_34_plus,
            supports_share_address_strategy=True,
            supports_client_used_traffic=True,
            supports_online_stats=True,
            supports_api_token_auth=True,
            supports_precise_client_scope=True,
            supports_live_config_apply=is_33_plus,
        )
    return XUICapabilities()


def profile_from_observations(version="", nodes=None, inbounds=None, hosts=None, live_failed=False):
    if live_failed:
        return PROFILE_UNKNOWN_SAFE
    nodes = nodes or []
    inbounds = inbounds or []
    hosts = hosts or []
    parsed = _version_tuple(version)
    has_node_payload = bool(nodes) or any(
        isinstance(inbound, dict)
        and (
            inbound.get("nodeId") not in (None, "", 0)
            or inbound.get("originNodeGuid")
            or inbound.get("shareAddrStrategy")
        )
        for inbound in inbounds
    )
    if has_node_payload:
        return PROFILE_MODERN_MULTI_NODE
    if parsed and parsed[0] >= 3:
        return PROFILE_MODERN_MULTI_NODE if hosts else PROFILE_MODERN_SINGLE_NODE
    if parsed and parsed[0] < 3:
        return PROFILE_LEGACY_SINGLE_NODE
    if inbounds:
        return PROFILE_LEGACY_SINGLE_NODE
    return PROFILE_UNKNOWN_SAFE


def _cache_key(panel):
    return f"xui:compatibility:{getattr(panel, 'pk', 'unsaved')}"


def _obj_list(payload):
    if not isinstance(payload, dict):
        return []
    obj = payload.get("obj")
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        items = obj.get("items") or obj.get("nodes") or obj.get("inbounds") or obj.get("hosts")
        return items if isinstance(items, list) else []
    return []


def _safe_get(service, path):
    return service.authenticated_json("GET", path)


def detect_xui_version(panel, *, live=False, service=None):
    if not live:
        return getattr(panel, "detected_xui_version", "") or ""
    profile = discover_xui_capabilities(panel, live=True, service=service, write=False)
    return profile.version


def discover_xui_capabilities(panel, *, live=False, service=None, write=False, use_cache=True):
    if use_cache:
        cached = cache.get(_cache_key(panel))
        if cached and (live or cached.get("profile") != PROFILE_UNKNOWN_SAFE):
            return XUICompatibilityProfile(
                profile=cached.get("profile") or PROFILE_UNKNOWN_SAFE,
                version=cached.get("version") or "",
                capabilities=XUICapabilities(**(cached.get("capabilities") or {})),
                metadata=cached.get("metadata") or {},
            )

    if not live:
        version = getattr(panel, "detected_xui_version", "") or ""
        profile = getattr(panel, "capability_profile", "") or ""
        metadata = dict(getattr(panel, "capability_metadata", None) or {})
        if not profile:
            profile = profile_from_observations(version=version, inbounds=[{"id": 1}])
        result = XUICompatibilityProfile(
            profile=profile,
            version=version,
            capabilities=capabilities_for_profile(profile, version),
            metadata=metadata,
        )
        cache.set(_cache_key(panel), result.to_dict(), CAPABILITY_CACHE_SECONDS)
        return result

    if service is None:
        from store.xui_api import XUIService

        service = XUIService(panel)

    live_failed = False
    errors = []
    update_info = {}
    status = {}
    inbounds_payload = {}
    nodes_payload = {}
    hosts_payload = {}
    try:
        service.login()
    except Exception as exc:
        live_failed = True
        errors.append({"endpoint": "login", "error": str(exc.__class__.__name__)})

    if not live_failed:
        for endpoint, path in (
            ("panel_update", "/panel/api/server/getPanelUpdateInfo"),
            ("server_status", "/panel/api/server/status"),
            ("inbounds", "/panel/api/inbounds/list"),
            ("nodes", "/panel/api/nodes/list"),
            ("hosts", "/panel/api/hosts/list"),
        ):
            try:
                payload = _safe_get(service, path)
            except Exception as exc:
                errors.append({"endpoint": endpoint, "error": str(exc.__class__.__name__)})
                payload = {}
            if endpoint == "panel_update":
                update_info = payload
            elif endpoint == "server_status":
                status = payload
            elif endpoint == "inbounds":
                inbounds_payload = payload
            elif endpoint == "nodes":
                nodes_payload = payload
            elif endpoint == "hosts":
                hosts_payload = payload

    version = _extract_version(update_info, status)
    inbounds = _obj_list(inbounds_payload)
    nodes = _obj_list(nodes_payload)
    hosts = _obj_list(hosts_payload)
    profile = profile_from_observations(version=version, nodes=nodes, inbounds=inbounds, hosts=hosts, live_failed=live_failed)
    normalized_nodes = [normalize_node(node).__dict__ for node in nodes[:50] if isinstance(node, dict)]
    normalized_inbounds = [
        normalize_inbound(inbound, context={"panel_id": getattr(panel, "pk", None)}).__dict__
        for inbound in inbounds[:100]
        if isinstance(inbound, dict)
    ]
    metadata = _json_safe({
        "checked_at": timezone.now().isoformat(),
        "source": "live" if not live_failed else "live_failed",
        "node_count": len(nodes),
        "inbound_count": len(inbounds),
        "host_count": len(hosts),
        "nodes": redact_sensitive(normalized_nodes),
        "sample_inbounds": redact_sensitive(normalized_inbounds[:20]),
        "errors": errors[:20],
    })
    result = XUICompatibilityProfile(
        profile=profile,
        version=version,
        capabilities=capabilities_for_profile(profile, version),
        metadata=metadata,
    )
    cache.set(_cache_key(panel), result.to_dict(), CAPABILITY_CACHE_SECONDS)

    if write and getattr(panel, "pk", None):
        panel.detected_xui_version = result.version
        panel.capability_profile = result.profile
        panel.capability_metadata = result.metadata
        panel.last_capability_check_at = timezone.now()
        panel.save(
            update_fields=[
                "detected_xui_version",
                "capability_profile",
                "capability_metadata",
                "last_capability_check_at",
                "updated_at",
            ]
        )
    return result
