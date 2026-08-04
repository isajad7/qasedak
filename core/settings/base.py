"""
Shared Django settings for the project.

Environment-specific settings live in development.py and production.py.
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"Set {name} to an integer value.") from exc


def database_url_settings(database_url):
    parsed = urlparse(database_url)
    scheme = (parsed.scheme or "").strip().lower()

    if scheme in {"postgres", "postgresql"}:
        query = parse_qs(parsed.query)
        sslmode = (query.get("sslmode") or [os.environ.get("POSTGRES_SSLMODE", "prefer")])[0]
        try:
            port = parsed.port
        except ValueError as exc:
            raise ImproperlyConfigured("DATABASE_URL contains an invalid port.") from exc

        config = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote((parsed.path or "").lstrip("/")) or os.environ.get("POSTGRES_DB", "qasedak"),
            "USER": unquote(parsed.username or os.environ.get("POSTGRES_USER", "qasedak")),
            "PASSWORD": unquote(parsed.password or os.environ.get("POSTGRES_PASSWORD", "")),
            "HOST": parsed.hostname or os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": str(port or env_int("POSTGRES_PORT", 5432)),
            "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        }
        if sslmode:
            config["OPTIONS"] = {"sslmode": sslmode}
        return config

    if scheme in {"sqlite", "sqlite3"}:
        path = unquote(parsed.path or "")
        if parsed.netloc:
            path = f"/{parsed.netloc}{path}"
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": path or os.environ.get("SQLITE_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
        }

    raise ImproperlyConfigured(
        "DATABASE_URL must use one of: postgres://, postgresql://, sqlite://, sqlite3://."
    )


def database_settings():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return database_url_settings(database_url)

    engine = os.environ.get("DATABASE_ENGINE", "sqlite").strip().lower()
    if engine in {"sqlite", "sqlite3"}:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
        }

    if engine in {"postgres", "postgresql"}:
        sslmode = os.environ.get("POSTGRES_SSLMODE", "prefer").strip()
        config = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "qasedak").strip() or "qasedak",
            "USER": os.environ.get("POSTGRES_USER", "qasedak").strip() or "qasedak",
            "PASSWORD": env_required("POSTGRES_PASSWORD"),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1",
            "PORT": str(env_int("POSTGRES_PORT", 5432)),
            "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        }
        if sslmode:
            config["OPTIONS"] = {"sslmode": sslmode}
        return config

    raise ImproperlyConfigured(
        "DATABASE_ENGINE must be one of: sqlite, postgres, postgresql."
    )


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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
    "default": database_settings(),
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

QASEDAK_PRIVATE_BACKUP_ROOT = Path(
    os.environ.get("QASEDAK_PRIVATE_BACKUP_ROOT", BASE_DIR / "backups" / "admin_center")
)
QASEDAK_TENANT_ROOT = Path(os.environ.get("QASEDAK_TENANT_ROOT", "/opt/qasedak-tenants"))
QASEDAK_RESTORE_UPLOAD_ROOT = Path(
    os.environ.get("QASEDAK_RESTORE_UPLOAD_ROOT", BASE_DIR / "backups" / "restore_uploads")
)
QASEDAK_BACKUP_MAX_UPLOAD_SIZE = int(
    os.environ.get("QASEDAK_BACKUP_MAX_UPLOAD_SIZE", 512 * 1024 * 1024)
)
QASEDAK_BACKUP_MAX_EXTRACTED_SIZE = int(
    os.environ.get("QASEDAK_BACKUP_MAX_EXTRACTED_SIZE", 2 * 1024 * 1024 * 1024)
)
QASEDAK_ADMIN_RESTORE_ENABLED = env_bool("QASEDAK_ADMIN_RESTORE_ENABLED", False)


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
    "navigation_expanded": False,
    "show_theme_chooser": False,
    "show_ui_builder": False,
    "use_google_fonts_cdn": False,
    "custom_css": "admin/css/jazzmin-custom.css",
    "custom_js": "admin/qasedak_admin.js",
    "hide_apps": [
        "auth",
        "store",
        "payments",
    ],
    "custom_links": {
        "Dashboard": [
            {"name": _("Dashboard"), "url": "admin:index", "icon": "fas fa-chart-line"},
        ],
        "Sales": [
            {"name": _("Orders"), "url": "admin_store_order_workbench", "icon": "fas fa-shopping-cart", "permissions": ["store.view_order"]},
            {"name": _("Products / Plans"), "url": "admin_store_catalog", "icon": "fas fa-box-open", "permissions": ["store.view_plan"]},
            {"name": _("Sales Routes"), "url": "admin:store_planinboundroute_changelist", "icon": "fas fa-route", "permissions": ["store.view_planinboundroute"]},
            {"name": _("Services / VPN Clients"), "url": "admin_store_service_workbench", "icon": "fas fa-shield-alt", "permissions": ["store.view_vpnclient"]},
            {"name": _("Subscription Cup Manager"), "url": "admin_store_cup_center", "icon": "fas fa-link", "permissions": ["store.view_subscriptioncup"]},
            {"name": _("Quick Subscription Builder"), "url": "admin_store_cup_center_quick_build", "icon": "fas fa-wand-magic-sparkles", "permissions": ["store.add_subscriptioncup"]},
            {"model": "payments.IncomingPaymentSMS"},
        ],
        "Customers": [
            {"model": "store.Customer"},
            {"model": "store.BotUser"},
            {"model": "store.SupportConversation"},
            {"model": "store.SupportMessage"},
        ],
        "Infrastructure": [
            {"name": _("Panel Integration Center"), "url": "admin_store_panel_center", "icon": "fas fa-plug", "permissions": ["store.view_panel"]},
            {"name": _("Plan Routing Builder"), "url": "admin_store_panel_center_routing", "icon": "fas fa-route", "permissions": ["store.view_plan"]},
            {"model": "store.Panel"},
            {"model": "store.Inbound"},
            {"model": "store.BotConfiguration"},
        ],
        "Marketing & Revenue": [
            {"name": _("Campaigns"), "url": "admin_store_campaign_workbench", "icon": "fas fa-bullhorn", "permissions": ["store.change_broadcastmessage"]},
            {"model": "store.DiscountCode"},
            {"model": "store.Referral"},
            {"model": "store.CustomerReward"},
            {"name": _("Revenue Engine"), "url": "admin_store_revenue_control", "icon": "fas fa-chart-pie", "permissions": ["store.change_store"]},
        ],
        "Reports": [
            {"name": _("Reports & Analytics"), "url": "admin_store_reports_center", "icon": "fas fa-chart-bar", "permissions": ["store.view_order"]},
            {"model": "store.BotPendingAction"},
            {"model": "store.BotEventLog"},
        ],
        "Management": [
            {"model": "store.Store"},
            {"model": "auth.User"},
            {"model": "auth.Group"},
            {"name": _("Staff Access"), "url": "admin_store_staff_access", "icon": "fas fa-user-lock", "permissions": ["auth.view_user"]},
            {"name": _("Backup / Restore"), "url": "admin_store_backup_center", "icon": "fas fa-database", "permissions": ["store.view_qasedakbackupjob"]},
        ],
    },
    "order_with_respect_to": [
        "Dashboard",
        "Sales",
        "Customers",
        "Infrastructure",
        "Marketing & Revenue",
        "Reports",
        "Management",
        "store.Order",
        "store.Plan",
        "store.PlanInboundRoute",
        "store.VPNClient",
        "payments.IncomingPaymentSMS",
        "store.Customer",
        "store.BotUser",
        "store.SupportConversation",
        "store.SupportMessage",
        "admin_store_panel_center",
        "admin_store_panel_center_routing",
        "admin_store_cup_center",
        "admin_store_cup_center_quick_build",
        "store.Panel",
        "store.Inbound",
        "store.BotConfiguration",
        "store.BroadcastMessage",
        "store.DiscountCode",
        "store.Referral",
        "store.CustomerReward",
        "store.RevenueOfferLog",
        "store.BotPendingAction",
        "store.BotEventLog",
        "store.Store",
        "auth.User",
        "auth.Group",
    ],
    "icons": {
        "Dashboard": "fas fa-chart-line",
        "Sales": "fas fa-cash-register",
        "Customers": "fas fa-users",
        "Infrastructure": "fas fa-server",
        "Marketing & Revenue": "fas fa-bullhorn",
        "Reports": "fas fa-chart-bar",
        "Management": "fas fa-cog",
        "auth.User": "fas fa-user-shield",
        "auth.Group": "fas fa-users-cog",
        "store.Customer": "fas fa-user-tag",
        "store.Referral": "fas fa-share-alt",
        "store.CustomerReward": "fas fa-gift",
        "store.Store": "fas fa-store-alt",
        "store.Operator": "fas fa-sitemap",
        "store.Plan": "fas fa-box-open",
        "store.PlanInboundRoute": "fas fa-route",
        "store.DiscountCode": "fas fa-tags",
        "store.Order": "fas fa-shopping-cart",
        "store.Panel": "fas fa-server",
        "store.Inbound": "fas fa-network-wired",
        "store.VPNClient": "fas fa-shield-alt",
        "store.SubscriptionCup": "fas fa-link",
        "store.ConfigLink": "fas fa-code",
        "store.CupItem": "fas fa-list",
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
    "sidebar_nav_flat_style": False,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}
