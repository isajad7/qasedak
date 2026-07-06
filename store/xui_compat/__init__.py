from .adapters import assert_precise_client_scope, build_remote_client_key, get_xui_adapter, inbound_remote_key
from .capabilities import (
    PROFILE_LEGACY_SINGLE_NODE,
    PROFILE_MODERN_MULTI_NODE,
    PROFILE_MODERN_SINGLE_NODE,
    PROFILE_UNKNOWN_SAFE,
    detect_xui_version,
    discover_xui_capabilities,
)
from .normalizers import classify_xui_error, normalize_client, normalize_inbound, normalize_node, normalize_usage

__all__ = [
    "PROFILE_LEGACY_SINGLE_NODE",
    "PROFILE_MODERN_MULTI_NODE",
    "PROFILE_MODERN_SINGLE_NODE",
    "PROFILE_UNKNOWN_SAFE",
    "assert_precise_client_scope",
    "build_remote_client_key",
    "classify_xui_error",
    "detect_xui_version",
    "discover_xui_capabilities",
    "get_xui_adapter",
    "inbound_remote_key",
    "normalize_client",
    "normalize_inbound",
    "normalize_node",
    "normalize_usage",
]
