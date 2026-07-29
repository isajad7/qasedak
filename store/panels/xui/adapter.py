from __future__ import annotations

from dataclasses import dataclass

from store.xui_api import XUIService, sanitize_xui_operational_text
from store.xui_compat import (
    PROFILE_LEGACY_SINGLE_NODE,
    PROFILE_MODERN_MULTI_NODE,
    PROFILE_MODERN_SINGLE_NODE,
    discover_xui_capabilities,
    inbound_remote_key,
)

from ..capabilities import CapabilityFlag, CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from ..errors import PanelOperationUnsupportedError


XUI_READ_ENDPOINTS = (
    "/panel/api/server/getPanelUpdateInfo",
    "/panel/api/server/status",
    "/panel/api/inbounds/list",
    "/panel/api/nodes/list",
    "/panel/api/hosts/list",
)

XUI_WRITE_ENDPOINTS = (
    "/panel/api/inbounds/addClient",
    "/panel/api/clients/add",
    "/panel/api/inbounds/updateClient/{identifier}",
    "/panel/api/clients/update/{email}",
    "/panel/api/inbounds/{inbound_id}/delClient/{identifier}",
    "/panel/api/clients/del/{email}",
)


def _xui_flags(profile) -> frozenset[str]:
    capabilities = profile.capabilities
    flags = {
        CapabilityFlag.LOGIN,
        CapabilityFlag.READ_INBOUNDS,
        CapabilityFlag.SUBSCRIPTION_LINKS,
        CapabilityFlag.DIRECT_LINKS,
    }
    if getattr(capabilities, "supports_nodes", False):
        flags.add(CapabilityFlag.READ_NODES)
    if profile.profile in {PROFILE_LEGACY_SINGLE_NODE, PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
        flags.add(CapabilityFlag.WRITE_CLIENTS)
    if profile.profile == PROFILE_LEGACY_SINGLE_NODE:
        flags.add(CapabilityFlag.PRECREATE_DISABLED_CLIENTS)
    if profile.profile == PROFILE_MODERN_MULTI_NODE:
        flags.add(CapabilityFlag.MULTI_INBOUND_CLIENTS)
    if getattr(capabilities, "supports_precise_client_scope", False):
        flags.add(CapabilityFlag.PRECISE_CLIENT_SCOPE)
    if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
        flags.add(CapabilityFlag.CSRF_LOGIN)
        flags.add(CapabilityFlag.CSRF_WRITE)
    return frozenset(flags)


def _report_from_xui_profile(panel, profile) -> PanelCapabilityReport:
    capability_profile = CapabilityProfile(
        family="xui",
        profile=profile.profile,
        version=profile.version,
        flags=_xui_flags(profile),
        metadata=profile.metadata,
    )
    login_method = "legacy_or_csrf"
    if profile.profile in {PROFILE_MODERN_SINGLE_NODE, PROFILE_MODERN_MULTI_NODE}:
        login_method = "legacy_with_csrf_fallback"
    warnings = ()
    if profile.profile == "unknown_safe":
        warnings = ("Destructive client operations must stay disabled until capabilities are known.",)
    return PanelCapabilityReport(
        family="xui",
        profile=capability_profile,
        supported=profile.profile != "unknown_safe",
        login_method=login_method,
        read_endpoints=XUI_READ_ENDPOINTS,
        write_endpoints=XUI_WRITE_ENDPOINTS,
        supported_protocols=("vless", "vmess", "trojan"),
        warnings=warnings,
        metadata={
            "source": (profile.metadata or {}).get("source", ""),
            "node_count": (profile.metadata or {}).get("node_count", 0),
            "inbound_count": (profile.metadata or {}).get("inbound_count", 0),
            "host_count": (profile.metadata or {}).get("host_count", 0),
        },
    )


@dataclass
class XUIProvisioningRequest:
    email_prefix: str
    total_gb: object
    duration_days: int
    inbound: object | None = None
    inbounds: list[object] | None = None
    limit_ip: int = 2
    client_uuid: str = ""
    sub_id: str = ""
    email: str = ""


class XUIPanelAdapter:
    family = "xui"

    def __init__(self, panel, *, service=None):
        self.panel = panel
        self.service = service or XUIService(panel)

    def test_connection(self) -> bool:
        self.service.login()
        return True

    def get_capability_report(self) -> PanelCapabilityReport:
        return self.detect_capabilities(live=False)

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        profile = discover_xui_capabilities(
            self.panel,
            live=live,
            service=self.service if live else None,
            write=write,
            use_cache=not live,
        )
        return _report_from_xui_profile(self.panel, profile)

    def list_inbounds(self) -> list[dict]:
        payload = self.service.authenticated_json("GET", "/panel/api/inbounds/list")
        obj = payload.get("obj") if isinstance(payload, dict) else []
        if isinstance(obj, dict):
            obj = obj.get("items") or obj.get("inbounds") or []
        return obj if isinstance(obj, list) else []

    def check_inbound(self, inbound) -> InboundHealthResult:
        try:
            data = self.service.get_inbound(inbound, use_cache=False)
        except Exception as exc:
            return InboundHealthResult(
                ok=False,
                inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
                remote_key=inbound_remote_key(inbound, panel=self.panel),
                status="error",
                message=sanitize_xui_operational_text(exc, panel=self.panel),
            )
        enabled = data.get("enable") if isinstance(data, dict) else None
        ok = enabled is not False
        return InboundHealthResult(
            ok=ok,
            inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
            remote_key=inbound_remote_key(inbound, panel=self.panel),
            status="ok" if ok else "disabled",
            message="" if ok else "Remote inbound is disabled.",
            metadata={"protocol": data.get("protocol"), "remark": data.get("remark")} if isinstance(data, dict) else {},
        )

    def create_enabled_client(self, request: XUIProvisioningRequest) -> dict:
        if not request.inbound:
            raise PanelOperationUnsupportedError("X-UI enabled client creation requires one inbound.")
        return self.service.create_enabled_client(
            email_prefix=request.email_prefix,
            total_gb=request.total_gb,
            duration_hours=int(request.duration_days or 0) * 24,
            inbound=request.inbound,
            limit_ip=request.limit_ip,
            client_uuid=request.client_uuid,
            sub_id=request.sub_id,
            email=request.email,
        )

    def create_enabled_multi_inbound_client(self, request: XUIProvisioningRequest) -> dict:
        inbounds = list(request.inbounds or [])
        if not inbounds:
            raise PanelOperationUnsupportedError("X-UI multi-inbound creation requires inbounds.")
        return self.service.create_enabled_multi_inbound_client(
            email_prefix=request.email_prefix,
            total_gb=request.total_gb,
            duration_hours=int(request.duration_days or 0) * 24,
            inbounds=inbounds,
            limit_ip=request.limit_ip,
            client_uuid=request.client_uuid,
            sub_id=request.sub_id,
            email=request.email,
        )

    def delete_client(self, inbound, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        return bool(
            self.service.delete_client_from_inbound(
                inbound,
                identifier,
                allow_multi_scope=allow_multi_scope,
            )
        )
