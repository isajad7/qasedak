from __future__ import annotations

from .errors import PanelFamilyUnsupportedError
from .marzban.adapter import MarzbanAdapter
from .pasarguard.adapter import PasarGuardPanelAdapter
from .unsupported import UnsupportedPanelAdapter
from .xui.adapter import XUIPanelAdapter


FAMILY_XUI = "xui"
FAMILY_3X_UI = "3x-ui"
FAMILY_SANAEI = "sanaei"
FAMILY_MARZBAN = "marzban"
FAMILY_PASARGUARD = "pasarguard"
FAMILY_UNKNOWN = "unknown"

XUI_FAMILIES = {FAMILY_XUI, FAMILY_3X_UI, FAMILY_SANAEI, ""}


def panel_family(panel) -> str:
    for attr in ("family", "panel_family", "panel_type", "type"):
        value = str(getattr(panel, attr, "") or "").strip().lower()
        if value:
            return value
    metadata = getattr(panel, "metadata", None) or {}
    if isinstance(metadata, dict):
        value = str(metadata.get("panel_family") or metadata.get("family") or "").strip().lower()
        if value:
            return value
    return FAMILY_XUI


class PanelAdapterFactory:
    def for_panel(self, panel):
        family = panel_family(panel)
        if family in XUI_FAMILIES:
            return XUIPanelAdapter(panel)
        if family == FAMILY_MARZBAN:
            return MarzbanAdapter(panel)
        if family == FAMILY_PASARGUARD:
            return PasarGuardPanelAdapter(panel)
        if family == FAMILY_UNKNOWN:
            return UnsupportedPanelAdapter(panel, family=family, reason="Panel family is unknown; remote operations are disabled.")
        return UnsupportedPanelAdapter(
            panel,
            family=family or FAMILY_UNKNOWN,
            reason=f"Panel family is not supported: {family or 'unknown'}",
        )

    def safe_for_panel(self, panel):
        try:
            return self.for_panel(panel)
        except PanelFamilyUnsupportedError as exc:
            family = panel_family(panel) or FAMILY_UNKNOWN
            return UnsupportedPanelAdapter(panel, family=family, reason=str(exc))


def get_panel_adapter(panel):
    return PanelAdapterFactory().for_panel(panel)


def get_safe_panel_adapter(panel):
    return PanelAdapterFactory().safe_for_panel(panel)
