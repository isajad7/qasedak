class PanelAdapterError(Exception):
    """Base exception for panel adapter operations."""


class PanelFamilyUnsupportedError(PanelAdapterError):
    """Raised when a panel family has no supported adapter."""


class PanelOperationUnsupportedError(PanelAdapterError):
    """Raised when an adapter cannot perform a requested operation."""
