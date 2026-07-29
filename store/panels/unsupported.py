from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from .errors import PanelOperationUnsupportedError


@dataclass
class UnsupportedPanelAdapter:
    panel: object
    family: str = "unknown"
    reason: str = "Panel family is not supported."

    def get_capability_report(self) -> PanelCapabilityReport:
        return PanelCapabilityReport(
            family=self.family,
            supported=False,
            profile=CapabilityProfile(family=self.family, profile="unsupported_safe"),
            warnings=(self.reason,),
            errors=(self.reason,),
            metadata={"implemented": False},
        )

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        return self.get_capability_report()

    def test_connection(self) -> bool:
        raise PanelOperationUnsupportedError(self.reason)

    def list_inbounds(self) -> list:
        raise PanelOperationUnsupportedError(self.reason)

    def check_inbound(self, inbound: object) -> InboundHealthResult:
        return InboundHealthResult(
            ok=False,
            inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
            status="unsupported",
            message=self.reason,
        )

    def create_enabled_client(self, request: object) -> dict:
        raise PanelOperationUnsupportedError(self.reason)

    def create_enabled_multi_inbound_client(self, request: object) -> dict:
        raise PanelOperationUnsupportedError(self.reason)

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        raise PanelOperationUnsupportedError(self.reason)
