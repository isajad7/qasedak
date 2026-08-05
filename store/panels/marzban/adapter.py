from __future__ import annotations

from dataclasses import dataclass

from ..capabilities import CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from ..errors import PanelAdapterUnavailableError


@dataclass
class MarzbanAdapter:
    panel: object

    family = "marzban"

    def _not_implemented(self, action: str, *, inbound=None):
        return PanelAdapterUnavailableError(
            "پنل Marzban هنوز برای ساخت مستقیم کانفیگ در این بخش پیاده‌سازی نشده است.",
            action=action,
            technical_detail="Marzban adapter placeholder is read-safe only; remote operations are disabled.",
            remediation="برای ساخت کانفیگ از پنل X-UI استفاده کنید یا adapter Marzban را قبل از فعال‌سازی عملیات نوشتنی تکمیل کنید.",
            panel=self.panel,
            panel_family=self.family,
            capability_profile="unsupported_safe",
            inbound=inbound,
            safe_context={"implemented": False},
        )

    def get_capability_report(self) -> PanelCapabilityReport:
        return self.detect_capabilities(live=False)

    def test_connection(self) -> bool:
        raise self._not_implemented("test_connection")

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        return PanelCapabilityReport(
            family=self.family,
            supported=False,
            profile=CapabilityProfile(
                family=self.family,
                profile="unsupported_safe",
                metadata={"implemented": False, "live_requested": bool(live), "write_requested": bool(write)},
            ),
            warnings=("پنل Marzban هنوز برای عملیات remote در این بخش پیاده‌سازی نشده است (not implemented).",),
            errors=("Marzban adapter is not implemented yet.",),
            metadata={"implemented": False, "remediation": "برای ساخت کانفیگ فعلاً از پنل X-UI استفاده کنید."},
        )

    def list_inbounds(self) -> list:
        raise self._not_implemented("list_inbounds")

    def check_inbound(self, inbound: object) -> InboundHealthResult:
        return InboundHealthResult(
            ok=False,
            inbound_id=str(getattr(inbound, "inbound_id", "") or ""),
            status="unsupported",
            message="بررسی اینباند Marzban هنوز پیاده‌سازی نشده است.",
        )

    def create_enabled_client(self, request: object) -> dict:
        raise self._not_implemented("create_client", inbound=getattr(request, "inbound", None))

    def create_enabled_multi_inbound_client(self, request: object) -> dict:
        raise self._not_implemented("create_multi_inbound_client")

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        raise self._not_implemented("delete_client", inbound=inbound)
