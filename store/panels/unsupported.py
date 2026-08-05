from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from .errors import UnsupportedPanelFamilyError


@dataclass
class UnsupportedPanelAdapter:
    panel: object
    family: str = "unknown"
    reason: str = "Panel family is not supported."

    def _operation_error(self, action: str, *, inbound=None):
        return UnsupportedPanelFamilyError(
            "این پنل هنوز برای ساخت کانفیگ قابل استفاده نیست.",
            action=action,
            technical_detail=self.reason,
            remediation="از Panel Center گزینه Test connection / Sync capabilities را اجرا کنید، یا family پنل را روی X-UI تنظیم کنید.",
            panel=self.panel,
            panel_family=self.family,
            capability_profile="unsupported_safe",
            inbound=inbound,
            safe_context={
                "required_capability": "supports_create_client" if "create" in action else action,
                "reason": self.reason,
            },
        )

    def get_capability_report(self) -> PanelCapabilityReport:
        message = "این خانواده پنل برای عملیات remote پشتیبانی نمی‌شود."
        return PanelCapabilityReport(
            family=self.family,
            supported=False,
            profile=CapabilityProfile(family=self.family, profile="unsupported_safe"),
            warnings=(message,),
            errors=(self.reason,),
            metadata={"implemented": False, "remediation": "Panel family را بررسی یا روی X-UI تنظیم کنید."},
        )

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        return self.get_capability_report()

    def test_connection(self) -> bool:
        raise self._operation_error("test_connection")

    def list_inbounds(self) -> list:
        raise self._operation_error("list_inbounds")

    def check_inbound(self, inbound: object) -> InboundHealthResult:
        return InboundHealthResult(
            ok=False,
            inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
            status="unsupported",
            message="این خانواده پنل برای بررسی اینباند پشتیبانی نمی‌شود.",
        )

    def create_enabled_client(self, request: object) -> dict:
        inbound = getattr(request, "inbound", None)
        raise self._operation_error("create_client", inbound=inbound)

    def create_enabled_multi_inbound_client(self, request: object) -> dict:
        raise self._operation_error("create_multi_inbound_client")

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        raise self._operation_error("delete_client", inbound=inbound)
