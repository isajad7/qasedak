from dataclasses import dataclass

from django.db.models import Q

from .capabilities import PROFILE_UNKNOWN_SAFE, XUICompatibilityProfile, discover_xui_capabilities
from .errors import XUIAmbiguousScopeError, XUIUnknownSafeModeError


DESTRUCTIVE_OPERATIONS = {"create", "update", "delete", "enable", "disable", "reset_traffic", "renew"}


@dataclass(frozen=True)
class XUIAdapter:
    panel: object
    profile: XUICompatibilityProfile

    @property
    def capabilities(self):
        return self.profile.capabilities

    def assert_operation_allowed(self, operation):
        if self.profile.profile == PROFILE_UNKNOWN_SAFE and operation in DESTRUCTIVE_OPERATIONS:
            raise XUIUnknownSafeModeError("X-UI compatibility is unknown; destructive operation refused.")


def get_xui_adapter(panel):
    return XUIAdapter(panel=panel, profile=discover_xui_capabilities(panel, live=False))


def inbound_node_id(inbound):
    return str(getattr(inbound, "xui_node_id", "") or "").strip()


def inbound_remote_key(inbound, *, panel=None):
    panel_id = getattr(panel, "pk", None) or getattr(inbound, "panel_id", None) or ""
    node_id = inbound_node_id(inbound) or "local"
    inbound_id = getattr(inbound, "inbound_id", "") or ""
    return str(getattr(inbound, "xui_remote_key", "") or "").strip() or f"{panel_id}:{node_id}:{inbound_id}"


def build_remote_client_key(panel, inbound, remote_client_identity):
    return ":".join(
        [
            str(getattr(panel, "pk", "") or getattr(inbound, "panel_id", "") or ""),
            inbound_node_id(inbound) or "local",
            str(getattr(inbound, "inbound_id", "") or ""),
            str(remote_client_identity or "").strip(),
        ]
    )


def local_scope_conflicts(panel, inbound):
    from store.models import Inbound

    if not getattr(panel, "pk", None) or not getattr(inbound, "pk", None):
        return []
    return list(
        Inbound.objects.filter(panel=panel, inbound_id=inbound.inbound_id)
        .exclude(pk=inbound.pk)
        .values("pk", "inbound_id", "xui_node_id", "xui_node_name")[:20]
    )


def local_client_matches(panel, inbound, identifier):
    from store.models import VPNClient

    identifier = str(identifier or "").strip()
    if not identifier or not getattr(inbound, "pk", None):
        return []
    return list(
        VPNClient.objects.filter(inbound=inbound)
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
        .filter(
            Q(uuid__iexact=identifier)
            | Q(xui_email__iexact=identifier)
            | Q(username__iexact=identifier)
            | Q(sub_id__iexact=identifier)
            | Q(remote_client_key__iexact=build_remote_client_key(panel, inbound, identifier))
        )
        .values("pk", "inbound_id", "xui_node_id", "remote_client_key")[:20]
    )


def assert_precise_client_scope(panel, inbound, identifier="", *, operation="read", allow_multi_scope=False):
    adapter = get_xui_adapter(panel)
    adapter.assert_operation_allowed(operation)

    if not inbound or getattr(inbound, "panel_id", None) != getattr(panel, "pk", None):
        raise XUIAmbiguousScopeError("Operation scope must include a panel-owned inbound.")

    source = str(getattr(inbound, "xui_source", "") or "local")
    if source == "synchronized_node" and not inbound_node_id(inbound):
        raise XUIAmbiguousScopeError("Synchronized node inbound is missing node identity.")

    conflicts = local_scope_conflicts(panel, inbound)
    if conflicts and not allow_multi_scope:
        if not adapter.capabilities.supports_precise_client_scope or not inbound_node_id(inbound):
            raise XUIAmbiguousScopeError("Inbound ID is ambiguous across node scopes.")

    matches = local_client_matches(panel, inbound, identifier)
    if len(matches) > 1 and not allow_multi_scope:
        raise XUIAmbiguousScopeError("Client identifier matches multiple local records in this scope.")

    return {
        "panel_id": getattr(panel, "pk", None),
        "inbound_id": getattr(inbound, "inbound_id", None),
        "node_id": inbound_node_id(inbound),
        "remote_key": inbound_remote_key(inbound, panel=panel),
        "local_conflicts": conflicts,
        "local_client_matches": matches,
        "allow_multi_scope": bool(allow_multi_scope),
    }
