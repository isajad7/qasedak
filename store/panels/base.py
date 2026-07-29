from __future__ import annotations

from typing import Any, Protocol

from .capabilities import InboundHealthResult, PanelCapabilityReport


class PanelAdapter(Protocol):
    """Panel-family contract for provisioning and topology services."""

    panel: object
    family: str

    def get_capability_report(self) -> PanelCapabilityReport:
        ...

    def test_connection(self) -> bool:
        ...

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        ...

    def list_inbounds(self) -> list[Any]:
        ...

    def check_inbound(self, inbound: object) -> InboundHealthResult:
        ...

    def create_enabled_client(self, request: object) -> dict:
        ...

    def create_enabled_multi_inbound_client(self, request: object) -> dict:
        ...

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        ...
