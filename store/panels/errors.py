from __future__ import annotations

import re


SECRET_KEYWORDS = {
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "cookies",
    "csrf",
    "csrf_token",
    "direct_link",
    "password",
    "proxy",
    "proxy_url",
    "secret",
    "session",
    "sub_id",
    "subid",
    "subscription",
    "subscription_link",
    "token",
    "uuid",
    "x-api-key",
    "x_api_key",
}

CONFIG_LINK_RE = re.compile(r"\b(?:vless|vmess|trojan|ss|ssr)://[^\s<>()]+", re.IGNORECASE)
SUBSCRIPTION_LINK_RE = re.compile(r"https?://[^\s<>()]+/sub/[A-Za-z0-9_-]+", re.IGNORECASE)
TOKENIZED_URL_RE = re.compile(r"https?://[^\s<>()]+/[A-Za-z0-9_-]{16,}(?:/[^\s<>()]*)?", re.IGNORECASE)
CREDENTIAL_URL_RE = re.compile(r"https?://[^/\s:@]+:[^@\s/]+@[^\s<>()]+", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", re.IGNORECASE | re.DOTALL)


def _panel_secrets(panel):
    if not panel:
        return []
    return [
        str(getattr(panel, attr, "") or "").strip()
        for attr in ("password", "username", "url", "proxy_url")
        if str(getattr(panel, attr, "") or "").strip()
    ]


def sanitize_error_value(value, *, panel=None):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            if any(keyword in key_text.lower() for keyword in SECRET_KEYWORDS):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = sanitize_error_value(item, panel=panel)
        return safe
    if isinstance(value, list):
        return [sanitize_error_value(item, panel=panel) for item in value[:100]]
    if isinstance(value, tuple):
        return tuple(sanitize_error_value(item, panel=panel) for item in value[:100])
    if isinstance(value, set):
        return sorted(sanitize_error_value(item, panel=panel) for item in list(value)[:100])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    text = CREDENTIAL_URL_RE.sub("<url-credentials-redacted>", text)
    text = TRACEBACK_RE.sub("<traceback-redacted>", text)
    text = CONFIG_LINK_RE.sub("<config-link-redacted>", text)
    text = SUBSCRIPTION_LINK_RE.sub("<subscription-link-redacted>", text)
    text = TOKENIZED_URL_RE.sub("<url-token-redacted>", text)
    text = UUID_RE.sub("<uuid-redacted>", text)
    text = re.sub(r"(?i)(password|token|csrf[-_ ]?token|session|cookie)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)\b(subId|sub_id|sub)[\"':=\s/]+[A-Za-z0-9_-]{8,}\b", r"\1=<sub-id-redacted>", text)
    text = re.sub(r"(?i)\b(?:csrf|token|secret|session|cookie)[-_a-z0-9]{6,}\b", "<secret-redacted>", text)
    for secret in _panel_secrets(panel):
        if len(secret) >= 3:
            text = text.replace(secret, "<redacted>")
    return text


def _safe_panel_info(panel=None, *, panel_id=None, panel_name="", panel_family="", capability_profile=""):
    panel_id = panel_id if panel_id is not None else getattr(panel, "pk", None)
    panel_name = panel_name or str(getattr(panel, "name", "") or "")
    panel_family = panel_family or str(getattr(panel, "family", "") or getattr(panel, "panel_family", "") or "")
    capability_profile = capability_profile or str(getattr(panel, "capability_profile", "") or "")
    return {
        "id": panel_id,
        "name": sanitize_error_value(panel_name),
        "family": sanitize_error_value(panel_family),
        "capability_profile": sanitize_error_value(capability_profile),
    }


def _safe_inbound_info(inbound=None, *, inbound_id=None, remote_inbound_id=None):
    inbound_id = inbound_id if inbound_id is not None else getattr(inbound, "pk", None)
    remote_inbound_id = remote_inbound_id if remote_inbound_id is not None else getattr(inbound, "inbound_id", None)
    return {
        "id": inbound_id,
        "remote_inbound_id": sanitize_error_value(remote_inbound_id),
        "label": sanitize_error_value(str(inbound or "")) if inbound is not None else "",
    }


class PanelIntegrationError(Exception):
    """Structured, safe-to-render panel integration exception."""

    default_error_code = "panel_integration_error"
    default_layer = "panel_integration"
    default_action = ""
    default_message = "خطای اتصال یا عملیات پنل رخ داد."
    default_remediation = "جزئیات پنل و اینباند را بررسی کنید و در صورت نیاز تست اتصال یا Sync capabilities را اجرا کنید."

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str = "",
        layer: str = "",
        action: str = "",
        technical_detail: str = "",
        remediation: str = "",
        panel=None,
        panel_id=None,
        panel_name: str = "",
        panel_family: str = "",
        capability_profile: str = "",
        inbound=None,
        inbound_id=None,
        remote_inbound_id=None,
        safe_context: dict | None = None,
        warnings: list[str] | tuple[str, ...] | None = None,
    ):
        self.error_code = error_code or self.default_error_code
        self.layer = layer or self.default_layer
        self.action = action or self.default_action
        self.message = sanitize_error_value(message or self.default_message, panel=panel)
        self.technical_detail = sanitize_error_value(technical_detail, panel=panel)
        self.remediation = sanitize_error_value(remediation or self.default_remediation, panel=panel)
        self.panel = _safe_panel_info(
            panel,
            panel_id=panel_id,
            panel_name=panel_name,
            panel_family=panel_family,
            capability_profile=capability_profile,
        )
        self.inbound = _safe_inbound_info(inbound, inbound_id=inbound_id, remote_inbound_id=remote_inbound_id)
        self.safe_context = sanitize_error_value(safe_context or {}, panel=panel)
        self.warnings = list(sanitize_error_value(list(warnings or []), panel=panel))
        super().__init__(self.message)

    @property
    def panel_id(self):
        return self.panel.get("id")

    @property
    def panel_name(self):
        return self.panel.get("name") or ""

    @property
    def panel_family(self):
        return self.panel.get("family") or ""

    @property
    def capability_profile(self):
        return self.panel.get("capability_profile") or ""

    @property
    def inbound_id(self):
        return self.inbound.get("id")

    @property
    def remote_inbound_id(self):
        return self.inbound.get("remote_inbound_id")

    def __str__(self):
        return str(self.message or self.error_code)

    def to_safe_dict(self):
        return {
            "error_code": sanitize_error_value(self.error_code),
            "layer": sanitize_error_value(self.layer),
            "action": sanitize_error_value(self.action),
            "message": sanitize_error_value(self.message),
            "technical_detail": sanitize_error_value(self.technical_detail),
            "remediation": sanitize_error_value(self.remediation),
            "panel": sanitize_error_value(self.panel),
            "panel_id": self.panel.get("id"),
            "panel_name": sanitize_error_value(self.panel.get("name")),
            "panel_family": sanitize_error_value(self.panel.get("family")),
            "capability_profile": sanitize_error_value(self.panel.get("capability_profile")),
            "inbound": sanitize_error_value(self.inbound),
            "inbound_id": self.inbound.get("id"),
            "remote_inbound_id": sanitize_error_value(self.inbound.get("remote_inbound_id")),
            "safe_context": sanitize_error_value(self.safe_context),
            "warnings": sanitize_error_value(self.warnings),
        }


class PanelAdapterError(PanelIntegrationError):
    """Base exception for panel adapter operations."""

    default_error_code = "panel_adapter_error"
    default_layer = "adapter_factory"
    default_message = "خطای adapter پنل رخ داد."


class UnsupportedPanelFamilyError(PanelAdapterError):
    default_error_code = "unsupported_panel_family"
    default_layer = "adapter_factory"
    default_message = "این خانواده پنل برای این عملیات پشتیبانی نمی‌شود."
    default_remediation = "family پنل را بررسی کنید. برای ساخت کانفیگ فعلاً X-UI پشتیبانی می‌شود."


class PanelFamilyUnsupportedError(UnsupportedPanelFamilyError):
    """Backward-compatible name for unsupported panel family errors."""


class PanelOperationUnsupportedError(PanelAdapterError):
    default_error_code = "panel_operation_unsupported"
    default_layer = "panel_write"
    default_message = "این عملیات برای پنل انتخاب‌شده پشتیبانی نمی‌شود."
    default_remediation = "قابلیت‌های پنل را Sync کنید یا پنل/اینباند دیگری انتخاب کنید."


class PanelCapabilityMissingError(PanelOperationUnsupportedError):
    default_error_code = "panel_capability_missing"
    default_layer = "capability_detection"
    default_message = "پنل انتخاب‌شده قابلیت لازم برای این عملیات را ندارد."


class PanelAdapterUnavailableError(PanelOperationUnsupportedError):
    default_error_code = "panel_adapter_unavailable"
    default_layer = "adapter_factory"
    default_message = "adapter عملیاتی برای این پنل آماده نیست."


class PanelLoginFailedError(PanelIntegrationError):
    default_error_code = "panel_login_failed"
    default_layer = "panel_login"
    default_action = "login"
    default_message = "ورود به پنل ناموفق بود."
    default_remediation = "اطلاعات ورود پنل، CSRF/2FA و دسترسی شبکه را بررسی کنید."


class PanelReadFailedError(PanelIntegrationError):
    default_error_code = "panel_read_failed"
    default_layer = "panel_read"
    default_message = "خواندن اطلاعات از پنل ناموفق بود."
    default_remediation = "Test connection / Sync capabilities را اجرا کنید و دسترسی API خواندنی پنل را بررسی کنید."


class PanelWriteForbiddenError(PanelIntegrationError):
    default_error_code = "panel_write_forbidden"
    default_layer = "panel_write"
    default_message = "پنل اجازه عملیات نوشتنی را نداد."
    default_remediation = "سطح دسترسی کاربر پنل، CSRF و endpoint نوشتن را بررسی کنید."


class PanelCreateClientFailedError(PanelIntegrationError):
    default_error_code = "panel_create_client_failed"
    default_layer = "panel_write"
    default_action = "create_client"
    default_message = "ساخت client روی پنل ناموفق بود."
    default_remediation = "اینباند، ظرفیت پنل، credentialها و قابلیت ساخت client را بررسی کنید."


class PanelDeleteClientFailedError(PanelIntegrationError):
    default_error_code = "panel_delete_client_failed"
    default_layer = "panel_write"
    default_action = "delete_client"
    default_message = "حذف client از پنل ناموفق بود."
    default_remediation = "client تست را در پنل جستجو و در صورت باقی‌ماندن، دستی حذف کنید."


class InboundUnsupportedError(PanelIntegrationError):
    default_error_code = "inbound_unsupported"
    default_layer = "inbound_validation"
    default_message = "اینباند انتخاب‌شده برای این عملیات پشتیبانی نمی‌شود."


class InboundValidationError(PanelIntegrationError):
    default_error_code = "inbound_validation_failed"
    default_layer = "inbound_validation"
    default_message = "اعتبارسنجی اینباند ناموفق بود."


class RoutingValidationError(PanelIntegrationError):
    default_error_code = "routing_validation_failed"
    default_layer = "routing_validation"
    default_action = "validate_route"
    default_message = "اعتبارسنجی مسیر فروش ناموفق بود."


class CupBuildValidationError(PanelIntegrationError):
    default_error_code = "cup_build_validation_failed"
    default_layer = "cup_builder"
    default_action = "build_subscription_cup"
    default_message = "اعتبارسنجی ساخت Subscription Cup ناموفق بود."


class CupPartialBuildError(PanelIntegrationError):
    default_error_code = "cup_partial_build"
    default_layer = "cup_builder"
    default_action = "build_subscription_cup"
    default_message = "Cup فقط برای بخشی از پنل‌ها ساخته شد."


class CupRemoteCreateFailedError(PanelCreateClientFailedError):
    default_error_code = "cup_remote_create_failed"
    default_layer = "cup_builder"
    default_action = "create_client"
    default_message = "ساخت کانفیگ روی پنل برای Subscription Cup ناموفق بود."


def panel_error_from_exception(
    exc,
    *,
    error_code: str = "panel_operation_failed",
    layer: str = "panel_write",
    action: str = "",
    message: str = "",
    remediation: str = "",
    panel=None,
    inbound=None,
    safe_context: dict | None = None,
):
    if isinstance(exc, PanelIntegrationError):
        return exc
    return PanelIntegrationError(
        message or "عملیات پنل ناموفق بود.",
        error_code=error_code,
        layer=layer,
        action=action,
        technical_detail=str(exc or ""),
        remediation=remediation,
        panel=panel,
        inbound=inbound,
        safe_context=safe_context,
    )


def safe_error_dict(exc, **kwargs):
    return panel_error_from_exception(exc, **kwargs).to_safe_dict()


def safe_error_message(exc, **kwargs):
    return panel_error_from_exception(exc, **kwargs).message
