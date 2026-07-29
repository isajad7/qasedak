from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


SENSITIVE_KEYS = {
    "password",
    "token",
    "csrf",
    "csrf_token",
    "cookie",
    "cookies",
    "session",
    "authorization",
    "proxy_url",
    "sub_id",
    "subid",
    "uuid",
    "subscription",
    "subscription_link",
    "direct_link",
}

CONFIG_LINK_PATTERN = re.compile(r"\b(?:vless|vmess|trojan|ss|ssr)://[^\s<>()]+", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _safe_text(value):
    text = str(value or "")
    text = re.sub(r"https?://[^/\s:@]+:[^@\s/]+@[^\s<>()]+", "<url-credentials-redacted>", text, flags=re.IGNORECASE)
    text = CONFIG_LINK_PATTERN.sub("<config-link-redacted>", text)
    text = UUID_PATTERN.sub("<uuid-redacted>", text)
    text = re.sub(r"(?i)(csrf[-_ ]?token|session|cookie)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    return text


def sanitize_capability_metadata(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            if any(sensitive in key_text.lower() for sensitive in SENSITIVE_KEYS):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = sanitize_capability_metadata(item)
        return safe
    if isinstance(value, list):
        return [sanitize_capability_metadata(item) for item in value[:100]]
    if isinstance(value, tuple):
        return tuple(sanitize_capability_metadata(item) for item in value[:100])
    if isinstance(value, str):
        return _safe_text(value)
    return value


class CapabilityFlag:
    LOGIN = "login"
    READ_INBOUNDS = "read_inbounds"
    READ_NODES = "read_nodes"
    WRITE_CLIENTS = "write_clients"
    MULTI_INBOUND_CLIENTS = "multi_inbound_clients"
    PRECREATE_DISABLED_CLIENTS = "precreate_disabled_clients"
    PRECISE_CLIENT_SCOPE = "precise_client_scope"
    SUBSCRIPTION_LINKS = "subscription_links"
    DIRECT_LINKS = "direct_links"
    CSRF_LOGIN = "csrf_login"
    CSRF_WRITE = "csrf_write"


@dataclass(frozen=True)
class CapabilityProfile:
    family: str
    profile: str = "unknown_safe"
    version: str = ""
    flags: frozenset[str] = frozenset()
    metadata: dict = field(default_factory=dict)

    def supports(self, flag: str) -> bool:
        return flag in self.flags

    def to_dict(self) -> dict:
        data = asdict(self)
        data["flags"] = sorted(self.flags)
        data["metadata"] = sanitize_capability_metadata(data.get("metadata") or {})
        return data


@dataclass(frozen=True)
class PanelCapabilityReport:
    family: str
    profile: CapabilityProfile
    supported: bool = True
    login_method: str = ""
    read_endpoints: tuple[str, ...] = ()
    write_endpoints: tuple[str, ...] = ()
    supported_protocols: tuple[str, ...] = ()
    unsupported_protocols: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

    @property
    def detected_version(self):
        return self.profile.version

    @property
    def capability_profile(self):
        return self.profile.profile

    @property
    def supports_login(self):
        return self.profile.supports(CapabilityFlag.LOGIN)

    @property
    def supports_read_inbounds(self):
        return self.profile.supports(CapabilityFlag.READ_INBOUNDS)

    @property
    def supports_create_client(self):
        return self.profile.supports(CapabilityFlag.WRITE_CLIENTS)

    @property
    def supports_delete_client(self):
        return self.profile.supports(CapabilityFlag.WRITE_CLIENTS)

    @property
    def supports_multi_inbound_create(self):
        return self.profile.supports(CapabilityFlag.MULTI_INBOUND_CLIENTS)

    @property
    def supports_subscription(self):
        return self.profile.supports(CapabilityFlag.SUBSCRIPTION_LINKS)

    @property
    def requires_csrf_for_login(self):
        return self.profile.supports(CapabilityFlag.CSRF_LOGIN)

    @property
    def requires_csrf_for_write(self):
        return self.profile.supports(CapabilityFlag.CSRF_WRITE)

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "detected_version": self.detected_version,
            "capability_profile": self.capability_profile,
            "supported": self.supported,
            "supports_login": self.supports_login,
            "supports_read_inbounds": self.supports_read_inbounds,
            "supports_create_client": self.supports_create_client,
            "supports_delete_client": self.supports_delete_client,
            "supports_multi_inbound_create": self.supports_multi_inbound_create,
            "supports_subscription": self.supports_subscription,
            "requires_csrf_for_login": self.requires_csrf_for_login,
            "requires_csrf_for_write": self.requires_csrf_for_write,
            "supported_protocols": list(self.supported_protocols),
            "unsupported_protocols": list(self.unsupported_protocols),
            "profile": self.profile.to_dict(),
            "login_method": self.login_method,
            "read_endpoints": list(self.read_endpoints),
            "write_endpoints": list(self.write_endpoints),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "metadata": sanitize_capability_metadata(self.metadata),
        }


@dataclass(frozen=True)
class InboundHealthResult:
    ok: bool
    inbound_id: str = ""
    remote_key: str = ""
    status: str = "unknown"
    message: str = ""
    metadata: dict = field(default_factory=dict)
