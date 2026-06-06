"""
Shared Django settings for the project.

Environment-specific settings live in development.py and production.py.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _


BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=None):
    value = os.environ.get(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def env_required(name):
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"Set the {name} environment variable.")
    return value


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-hrf$n9ubs9qr&u!+%gva^ku%fg@f=ds98u*47i=(b7vjp=rdm+",
)

DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])


INSTALLED_APPS = [
    "jazzmin",
    "import_export",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "payments.apps.PaymentsConfig",
    "store.apps.StoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "store.middleware.CustomerTrackingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "store.context_processors.store_info",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = os.environ.get("DJANGO_LANGUAGE_CODE", "fa")

LANGUAGES = [
    ("en", _("English")),
    ("fa", _("Persian")),
]

LOCALE_PATHS = [
    BASE_DIR / "locale",
]

TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Tehran")

USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
STATIC_ROOT = BASE_DIR / "static_root"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


PAYMENT_RECEIPT_MAX_UPLOAD_SIZE = int(
    os.environ.get("PAYMENT_RECEIPT_MAX_UPLOAD_SIZE", 5 * 1024 * 1024)
)
SMSFORWARDER_WEBHOOK_TOKEN = os.environ.get("SMSFORWARDER_WEBHOOK_TOKEN", "")
PAYMENT_SMS_TIME_ZONE = os.environ.get("PAYMENT_SMS_TIME_ZONE", "Asia/Tehran")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()
TELEGRAM_PROXY_URL = os.environ.get("TELEGRAM_PROXY_URL", "").strip()
TELEGRAM_PROXY_PROTOCOL = os.environ.get("TELEGRAM_PROXY_PROTOCOL", "").strip()
TELEGRAM_PROXY_HOST = os.environ.get("TELEGRAM_PROXY_HOST", "").strip()
TELEGRAM_PROXY_PORT = os.environ.get("TELEGRAM_PROXY_PORT", "").strip()
TELEGRAM_PROXY_USERNAME = os.environ.get("TELEGRAM_PROXY_USERNAME", "").strip()
TELEGRAM_PROXY_PASSWORD = os.environ.get("TELEGRAM_PROXY_PASSWORD", "").strip()
TELEGRAM_WEBHOOK_RESPONSE_ENABLED = env_bool("TELEGRAM_WEBHOOK_RESPONSE_ENABLED", False)
BOT_API_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("BOT_API_CONNECT_TIMEOUT_SECONDS", "3"))
BOT_API_READ_TIMEOUT_SECONDS = float(os.environ.get("BOT_API_READ_TIMEOUT_SECONDS", "8"))
XUI_PANEL_PROXY_URL = os.environ.get("XUI_PANEL_PROXY_URL", "").strip()

IMPORT_EXPORT_USE_TRANSACTIONS = True


JAZZMIN_SETTINGS = {
    "site_title": _("VPN Store Admin"),
    "site_header": _("VPN Store Administration"),
    "site_brand": _("VPN Store"),
    "welcome_sign": _("Welcome to the VPN Store admin panel"),
    "copyright": _("VPN Store"),
    "search_model": [
        "store.Order",
        "store.Customer",
        "store.SupportConversation",
        "payments.IncomingPaymentSMS",
        "auth.User",
    ],
    "language_chooser": True,
    "show_sidebar": True,
    "navigation_expanded": True,
    "show_theme_chooser": False,
    "show_ui_builder": False,
    "use_google_fonts_cdn": False,
    "custom_css": "admin/css/jazzmin-custom.css",
    "hide_apps": [
        "auth",
        "store",
        "payments",
    ],
    "custom_links": {
        "Users": [
            {"model": "auth.User"},
            {"model": "auth.Group"},
            {"model": "store.Customer"},
            {"model": "store.Referral"},
            {"model": "store.CustomerReward"},
            {"model": "store.BotUser"},
        ],
        "Shop": [
            {"model": "store.Store"},
            {"model": "store.Operator"},
            {"model": "store.Plan"},
            {"model": "store.DiscountCode"},
            {"model": "store.Order"},
            {"model": "store.Panel"},
            {"model": "store.Inbound"},
            {"model": "store.VPNClient"},
            {"model": "store.VPNClientUsageSnapshot"},
            {"model": "store.SupportConversation"},
            {"model": "store.SupportMessage"},
            {"model": "store.BotConfiguration"},
            {"model": "store.BotPendingAction"},
            {"model": "store.BotEventLog"},
        ],
        "Payments": [
            {"model": "payments.IncomingPaymentSMS"},
        ],
    },
    "order_with_respect_to": [
        "Users",
        "Shop",
        "Payments",
        "auth.User",
        "auth.Group",
        "store.Customer",
        "store.Referral",
        "store.CustomerReward",
        "store.BotUser",
        "store.Store",
        "store.Operator",
        "store.Plan",
        "store.DiscountCode",
        "store.Order",
        "store.Panel",
        "store.Inbound",
        "store.VPNClient",
        "store.VPNClientUsageSnapshot",
        "store.SupportConversation",
        "store.SupportMessage",
        "store.BotConfiguration",
        "store.BotPendingAction",
        "store.BotEventLog",
        "payments.IncomingPaymentSMS",
    ],
    "icons": {
        "Users": "fas fa-users",
        "Shop": "fas fa-store",
        "Payments": "fas fa-money-check-alt",
        "auth.User": "fas fa-user-shield",
        "auth.Group": "fas fa-users-cog",
        "store.Customer": "fas fa-user-tag",
        "store.Referral": "fas fa-share-alt",
        "store.CustomerReward": "fas fa-gift",
        "store.Store": "fas fa-store-alt",
        "store.Operator": "fas fa-sitemap",
        "store.Plan": "fas fa-box-open",
        "store.DiscountCode": "fas fa-tags",
        "store.Order": "fas fa-shopping-cart",
        "store.Panel": "fas fa-server",
        "store.Inbound": "fas fa-network-wired",
        "store.VPNClient": "fas fa-shield-alt",
        "store.VPNClientUsageSnapshot": "fas fa-chart-line",
        "store.SupportConversation": "fas fa-comments",
        "store.SupportMessage": "fas fa-comment-dots",
        "store.BotUser": "fas fa-robot",
        "store.BotConfiguration": "fas fa-cogs",
        "store.BotPendingAction": "fas fa-tasks",
        "store.BotEventLog": "fas fa-clipboard-list",
        "payments.IncomingPaymentSMS": "fas fa-sms",
    },
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {
        "store.order": "collapsible",
        "payments.incomingpaymentsms": "collapsible",
        "auth.user": "horizontal_tabs",
    },
}

JAZZMIN_UI_TWEAKS = {
    "theme": "default",
    "default_theme_mode": "light",
    "navbar": "navbar-white navbar-light",
    "sidebar": "sidebar-dark-primary",
    "accent": "accent-primary",
    "sidebar_nav_compact_style": True,
    "sidebar_nav_flat_style": True,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}
