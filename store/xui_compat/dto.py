from dataclasses import dataclass, field


@dataclass(frozen=True)
class XUINode:
    external_node_id: str = ""
    name: str = ""
    status: str = "unknown"
    is_local: bool = False
    enabled: bool | None = None
    last_seen: object = None
    capabilities: dict = field(default_factory=dict)
    safe_address_label: str = ""


@dataclass(frozen=True)
class XUIInbound:
    panel_id: int | None = None
    node_external_id: str = ""
    node_name: str = ""
    inbound_external_id: str = ""
    remote_key: str = ""
    remark: str = ""
    protocol: str = ""
    active: bool | None = None
    source: str = "local"
    managed_share_address: str = ""
    share_address_strategy: str = ""
    sync_mode: str = ""
    usage_quality: str = "unknown"


@dataclass(frozen=True)
class XUIClient:
    remote_client_id: str = ""
    email: str = ""
    credential_type: str = ""
    inbound_external_id: str = ""
    node_external_id: str = ""
    remote_key: str = ""
    enabled: bool | None = None
    expiry: object = None
    traffic_limit: int | None = None
    usage: dict = field(default_factory=dict)
    online: bool | None = None
    source: str = ""


@dataclass(frozen=True)
class XUIUsage:
    upload_bytes: int | None = None
    download_bytes: int | None = None
    used_bytes: int | None = None
    total_bytes: int | None = None
    quality: str = "unknown"
    source: str = ""
    online: bool | None = None
