from dataclasses import dataclass


class XUICompatibilityError(Exception):
    """Base error for compatibility and safe-scope decisions."""


class XUIUnknownSafeModeError(XUICompatibilityError):
    pass


class XUIAmbiguousScopeError(XUICompatibilityError):
    pass


class XUINodeOfflineError(XUICompatibilityError):
    pass


@dataclass(frozen=True)
class XUIErrorClassification:
    code: str
    message: str
    retryable: bool = False
    node_offline: bool = False
    auth_failed: bool = False
    not_found: bool = False
