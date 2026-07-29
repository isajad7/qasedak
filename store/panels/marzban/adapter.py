from __future__ import annotations

from dataclasses import dataclass

from ..capabilities import CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from ..errors import PanelOperationUnsupportedError


@dataclass
class MarzbanAdapter:
    panel: object

    family = "marzban"

    def get_capability_report(self) -> PanelCapabilityReport:
        return self.detect_capabilities(live=False)

    def test_connection(self) -> bool:
        raise PanelOperationUnsupportedError("Marzban adapter is not implemented yet.")

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        return PanelCapabilityReport(
            family=self.family,
            supported=False,
            profile=CapabilityProfile(
                family=self.family,
                profile="unsupported_safe",
                metadata={"implemented": False, "live_requested": bool(live), "write_requested": bool(write)},
            ),
            warnings=("Marzban support is not implemented yet; no remote operations are allowed.",),
            errors=("Marzban adapter is not implemented yet.",),
            metadata={"implemented": False},
        )

    def list_inbounds(self) -> list:
        raise PanelOperationUnsupportedError("Marzban inbound listing is not implemented yet.")

    def check_inbound(self, inbound: object) -> InboundHealthResult:
        return InboundHealthResult(
            ok=False,
            inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
            status="unsupported",
            message="Marzban inbound checks are not implemented yet.",
        )

    def create_enabled_client(self, request: object) -> dict:
        raise PanelOperationUnsupportedError("Marzban provisioning is not implemented yet.")

    def create_enabled_multi_inbound_client(self, request: object) -> dict:
        raise PanelOperationUnsupportedError("Marzban multi-inbound provisioning is not implemented yet.")

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        raise PanelOperationUnsupportedError("Marzban client deletion is not implemented yet.")
