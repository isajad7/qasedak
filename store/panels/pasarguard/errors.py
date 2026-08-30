from __future__ import annotations

from ..errors import (
    PanelCreateClientFailedError,
    PanelDeleteClientFailedError,
    PanelIntegrationError,
    PanelLoginFailedError,
    PanelReadFailedError,
    PanelWriteForbiddenError,
)


class PasarGuardIntegrationError(PanelIntegrationError):
    default_error_code = "pasarguard_integration_error"
    default_layer = "pasarguard_api"
    default_message = "خطای اتصال یا عملیات PasarGuard رخ داد."
    default_remediation = "آدرس پایه پنل، API key و دسترسی endpointهای PasarGuard را بررسی کنید."


class PasarGuardAuthenticationError(PanelLoginFailedError):
    default_error_code = "pasarguard_auth_failed"
    default_layer = "pasarguard_auth"
    default_message = "احراز هویت PasarGuard ناموفق بود."
    default_remediation = "API key پنل PasarGuard را بررسی کنید."


class PasarGuardReadError(PanelReadFailedError):
    default_error_code = "pasarguard_read_failed"
    default_layer = "pasarguard_read"
    default_message = "خواندن اطلاعات از PasarGuard ناموفق بود."
    default_remediation = "دسترسی خواندن API و مسیرهای /api/system، /api/groups یا /api/groups/simple را بررسی کنید."


class PasarGuardWriteForbiddenError(PanelWriteForbiddenError):
    default_error_code = "pasarguard_write_forbidden"
    default_layer = "pasarguard_write"
    default_message = "PasarGuard اجازه عملیات نوشتنی را نداد."
    default_remediation = "سطح دسترسی API key و endpointهای user را بررسی کنید."


class PasarGuardCreateUserError(PanelCreateClientFailedError):
    default_error_code = "pasarguard_create_user_failed"
    default_layer = "pasarguard_user"
    default_action = "create_user"
    default_message = "ساخت کاربر در PasarGuard ناموفق بود."
    default_remediation = "group_ids، quota، expire، API key و وضعیت پنل PasarGuard را بررسی کنید."


class PasarGuardUserConflictError(PasarGuardCreateUserError):
    default_error_code = "pasarguard_user_conflict"
    default_message = "کاربر PasarGuard با همین نام وجود دارد اما متعلق به این سفارش نیست."
    default_remediation = "نام کاربر remote را بررسی کنید یا سفارش را با شناسه deterministic دیگری دوباره اجرا کنید."


class PasarGuardDeleteUserError(PanelDeleteClientFailedError):
    default_error_code = "pasarguard_delete_user_failed"
    default_layer = "pasarguard_user"
    default_action = "delete_user"
    default_message = "حذف کاربر PasarGuard ناموفق بود."
    default_remediation = "کاربر remote را در پنل PasarGuard بررسی و در صورت نیاز دستی حذف کنید."
