import csv
import json
import re
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Min, OuterRef, Q, Subquery, Sum
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.http import urlencode
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin as BaseImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from payments.models import IncomingPaymentSMS

from .admin_access import get_user_product_roles, mask_staff_email, user_has_capability
from .admin_catalog import (
    ROUTE_STATUS_FALLBACK,
    ROUTE_STATUS_MISSING,
    catalog_plan_edit_url,
    catalog_plan_review_url,
    catalog_url,
    get_inbound_sales_readiness,
    get_plan_route_status,
    set_plan_active_state,
    duplicate_plan_for_admin,
)
from .models import (
    BotConfiguration,
    BotAdminOrderMessage,
    BotEventLog,
    BotPendingAction,
    BotUser,
    BroadcastMessage,
    BroadcastRecipient,
    ConfigAllocation,
    ConfigInventoryAsset,
    ConfigInventoryPool,
    ConfigLink,
    CupFillerRule,
    CupFulfillmentRecipe,
    CupItem,
    Customer,
    CustomerAnalyticsReport,
    CustomerReward,
    DiscountCode,
    ExternalSubscriptionFeed,
    FreeTrialRequest,
    Inbound,
    LegacyWizWizImportJob,
    LegacyWizWizImportMessageBatch,
    LegacyWizWizImportMessageRecipient,
    LegacyWizWizImportRow,
    Order,
    Operator,
    Panel,
    PanelHealthAlertSettings,
    PanelClientUsageSnapshot,
    PanelDailyUsage,
    PanelHealthCheckLog,
    PanelHealthStatus,
    PanelUsageSnapshot,
    Plan,
    PlanDeliveryConfig,
    PlanDeliverySource,
    PlanInboundRoute,
    QasedakBackupJob,
    QasedakRestoreJob,
    Referral,
    ReferralRewardLedger,
    RevenueOfferLog,
    Store,
    normalize_payment_digits,
    SupportConversation,
    SupportMessage,
    SubscriptionCup,
    VPNClient,
    VPNClientActionLog,
    VPNClientReminderLog,
    VPNClientUsageSnapshot,
    WebTelegramLinkToken,
    DailyAdminReportLog,
)
from .orchestrator_v2.models import ServerNode, TenantInstance
from .admin_setup import (
    active_sales_inbounds,
    active_sellable_plans,
    active_telegram_configs,
    build_store_admin_summary,
    has_real_payment_card,
    missing_route_labels,
    setup_wizard_index_url,
)
from .deployment.owner_toolkit import OwnerToolkitError, TenantOwnerToolkit
from .admin_revenue import revenue_control_url
from .admin_campaigns import campaign_review_url, masked_target_label, recipient_error_label
from .admin_support_services import customer_message_url, sanitize_support_message, support_review_url
from .broadcast_services import create_campaign_recipients, resolve_campaign_recipients
from .legacy_wizwiz_import_services import (
    analyze_wizwiz_import_job,
    apply_wizwiz_import_job,
    calculate_file_sha256,
    create_legacy_import_message_batch,
    export_wizwiz_import_rows_csv,
    preview_legacy_import_message_batch,
    send_legacy_import_message_batch,
    validate_legacy_import_message_text,
    wizwiz_simple_restore,
)
from .customer_analytics import (
    METRIC_FIELDS,
    PERIOD_ALL_TIME,
    PERIOD_CURRENT_MONTH,
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_7_DAYS,
    PERIOD_TODAY,
    SEGMENT_ACTIVE_CONFIG,
    SEGMENT_ACTIVE_CUSTOMERS,
    SEGMENT_ALL,
    SEGMENT_CUSTOMERS_WITHOUT_ORDER,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_LOYAL,
    SEGMENT_NEW_CUSTOMER,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_BUYER,
    SEGMENT_TOP_REFERRER,
    annotate_customer_queryset,
    get_customers_by_segment,
    get_period_range,
    good_customer_min_total_amount,
    inactive_customer_days,
    loyal_customer_min_orders_30d,
)
from .telegram_bot.notifications import build_sales_report, send_to_config
from .order_actions import activate_order, reject_order
from .plan_route_services import (
    BULK_ROUTE_STRATEGY_ADD_NEW,
    BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
    BULK_ROUTE_STRATEGY_SKIP_EXISTING,
    BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    apply_bulk_plan_routes,
    get_bulk_route_target_plans,
    get_valid_sales_inbounds,
    normalize_plan_ids,
    preview_bulk_plan_routes,
)
from .admin_config_inventory import config_inventory_import, fulfillment_recipe_preview
from .config_inventory_services import (
    config_allocation_status_label,
    get_pool_stock_summary,
    inventory_allocation_mode_label,
    inventory_asset_status_label,
)
from .plan_fulfillment_services import (
    ERROR_STATUS,
    INACTIVE_STATUS,
    NOT_CONFIGURED_STATUS,
    READY_STATUS,
    WARNING_STATUS,
    plan_fulfillment_status_for_plan,
)
from .subscription_cups import build_subscription_cup_url, mask_config_link_for_display, mask_subscription_url, parse_config_link
from .external_subscription_sources import (
    refresh_external_subscription_feed,
    safe_external_subscription_text,
    source_owned_cup_item_count,
)
from .panel_health_services import (
    PanelHealthAlertService,
    get_panel_health_alert_recipient_summary,
    panel_alert_failure_threshold_count,
    panel_alert_repeat_interval_minutes,
)
from .xui_api import mask_xui_value, sync_inbound_data


User = get_user_model()

admin.site.site_header = _("VPN Store Administration")
admin.site.site_title = _("VPN Store Admin")
admin.site.index_title = _("Operations dashboard")
admin.site.index_template = "admin/dashboard.html"


def _panel_health_admin_reason(result):
    metadata = result.get("metadata") or {}
    http_status = result.get("http_status") or metadata.get("http_status")
    error_code = result.get("error_code") or metadata.get("error_category") or ""
    if error_code == "http_403_csrf_required":
        return "HTTP 403 - CSRF/login flow mismatch"
    if error_code == "two_factor_required":
        return "two_factor_required - two-factor login is enabled"
    if http_status:
        return f"HTTP {http_status} - {error_code or 'panel request failed'}"
    return error_code or result.get("error_message") or result.get("summary") or "unknown"


class ImportExportModelAdmin(BaseImportExportModelAdmin):
    """Keep raw import/export surfaces for superusers only."""

    def has_import_permission(self, request):
        parent = getattr(super(), "has_import_permission", None)
        allowed = parent(request) if parent else True
        return bool(getattr(request.user, "is_superuser", False) and allowed)

    def has_export_permission(self, request):
        parent = getattr(super(), "has_export_permission", None)
        allowed = parent(request) if parent else True
        return bool(getattr(request.user, "is_superuser", False) and allowed)


def format_usage_bytes(value):
    from .panel_usage_services import format_bytes_fa

    return format_bytes_fa(value)


def mask_url_credentials(value):
    raw_url = str(value or "").strip()
    if not raw_url:
        return "-"
    parsed = urlsplit(raw_url)
    if not parsed.hostname:
        return raw_url
    auth = ""
    if parsed.username:
        auth = "****@"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{auth}{parsed.hostname}{port}"
    path = parsed.path or ""
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def admin_mask_phone(value):
    cleaned = "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit() or ch == "+")
    if not cleaned:
        return ""
    prefix = cleaned[:4] if cleaned.startswith("+") else cleaned[:3]
    suffix = cleaned[-2:] if len(cleaned) > 5 else ""
    return f"{prefix}***{suffix}" if suffix else "***"


def admin_mask_identifier(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        local, domain = text.split("@", 1)
        return f"{local[:2]}***@{domain}" if local else f"***@{domain}"
    if len(text) <= 8:
        return f"{text[:2]}***"
    return f"{text[:6]}...{text[-4:]}"


def admin_safe_customer_label(customer):
    if not customer:
        return _("No customer")
    phone = getattr(customer, "phone_number", "") or ""
    label = (getattr(customer, "display_name", "") or getattr(customer, "username", "") or "").strip()
    if phone and normalize_payment_digits(label) == normalize_payment_digits(phone):
        return admin_mask_phone(phone)
    return label or f"Customer {str(customer.public_id)[:8]}"


class UserResource(resources.ModelResource):
    class Meta:
        model = User
        import_id_fields = ("username",)
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


class GroupResource(resources.ModelResource):
    class Meta:
        model = Group
        import_id_fields = ("name",)
        fields = ("id", "name")
        export_order = fields
        skip_unchanged = True
        report_skipped = True


try:
    admin.site.unregister(User)
except NotRegistered:
    pass

try:
    admin.site.unregister(Group)
except NotRegistered:
    pass


@admin.register(User)
class UserAdmin(ImportExportModelAdmin, DjangoUserAdmin):
    resource_class = UserResource
    list_display = (
        "username",
        "staff_display_name",
        "masked_staff_email",
        "staff_role_display",
        "is_staff",
        "is_active",
        "last_login",
        "quick_staff_review",
    )
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "first_name", "last_name")
    date_hierarchy = "date_joined"
    ordering = ("username",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        return user_has_capability(request.user, "staff.view") or user_has_capability(request.user, "staff.manage")

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        return user_has_capability(request.user, "staff.view") or user_has_capability(request.user, "staff.manage")

    def has_add_permission(self, request):
        return bool(request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_fieldsets(request, obj)
        return (
            (
                _("Profile"),
                {
                    "fields": (
                        "username",
                        "staff_display_name",
                        "masked_staff_email",
                        "is_active",
                        "is_staff",
                        "last_login",
                        "date_joined",
                    )
                },
            ),
            (
                _("Qasedak access"),
                {
                    "fields": (
                        "staff_role_display",
                        "quick_staff_review",
                    )
                },
            ),
        )

    def get_readonly_fields(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_readonly_fields(request, obj)
        return (
            "username",
            "staff_display_name",
            "masked_staff_email",
            "is_active",
            "is_staff",
            "last_login",
            "date_joined",
            "staff_role_display",
            "quick_staff_review",
        )

    @admin.display(description=_("Display name"), ordering="first_name")
    def staff_display_name(self, obj):
        name = obj.get_full_name().strip()
        return name or obj.username

    @admin.display(description=_("Email"))
    def masked_staff_email(self, obj):
        return mask_staff_email(obj.email) or "-"

    @admin.display(description=_("Qasedak role"))
    def staff_role_display(self, obj):
        if obj.is_superuser:
            return format_html('<span class="badge bg-danger">{}</span>', _("superuser"))
        roles = get_user_product_roles(obj)
        if not roles:
            return format_html('<span class="badge bg-warning text-dark">{}</span>', _("No product role"))
        return format_html_join(
            " ",
            '<span class="badge bg-info text-dark">{}</span>',
            ((role["label"],) for role in roles),
        )

    @admin.display(description=_("Staff Center"))
    def quick_staff_review(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_store_staff_review", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Review"))


@admin.register(Group)
class GroupAdmin(ImportExportModelAdmin, DjangoGroupAdmin):
    resource_class = GroupResource
    list_display = ("name",)
    list_filter = ("permissions__content_type__app_label",)
    search_fields = ("name",)
    ordering = ("name",)

    def has_module_permission(self, request):
        return bool(request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_add_permission(self, request):
        return bool(request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)


class StoreAdminForm(forms.ModelForm):
    card_number = forms.CharField(
        label=_("card number"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("برای تغییر شماره کارت، مقدار کامل ۱۶ رقمی را وارد کنید. مقدار فعلی کامل نمایش داده نمی‌شود."),
    )
    new_smsforwarder_webhook_token = forms.CharField(
        label=_("Set or rotate SMSForwarder webhook token"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("پس از تغییر توکن، همین مقدار را در اپ SMS Forwarder هم تنظیم کنید."),
    )

    class Meta:
        model = Store
        fields = "__all__"

    def clean_card_number(self):
        value = normalize_payment_digits(self.cleaned_data.get("card_number") or "").strip()
        if value:
            return value
        if self.instance and self.instance.pk:
            return self.instance.card_number
        raise forms.ValidationError(_("شماره کارت برای فروش دستی لازم است."))

    def save(self, commit=True):
        store = super().save(commit=False)
        token = self.cleaned_data.get("new_smsforwarder_webhook_token")
        if token:
            store.set_smsforwarder_webhook_token(token)
        if commit:
            store.save()
            self.save_m2m()
        return store


class PanelHealthAlertSettingsAdminForm(forms.ModelForm):
    class Meta:
        model = PanelHealthAlertSettings
        fields = "__all__"
        labels = {
            "name": _("نام"),
            "domain": _("دامنه"),
            "is_active": _("فعال است"),
            "panel_monitor_enabled": _("فعال‌سازی بررسی سلامت پنل"),
            "panel_monitor_alerts_enabled": _("فعال‌سازی هشدارهای سلامت پنل"),
            "panel_health_alerts_enabled": _("فعال‌سازی هشدار خرابی پنل"),
            "panel_health_alert_check_interval_minutes": _("فاصله بررسی سلامت پنل"),
            "panel_health_alert_repeat_interval_minutes": _("فاصله تکرار هشدار"),
            "panel_health_alert_failure_threshold_count": _("تعداد خطای پشت‌سرهم قبل از هشدار"),
            "panel_health_recovery_alert_enabled": _("ارسال پیام بعد از بازیابی پنل"),
            "panel_health_quiet_hours_enabled": _("فعال‌سازی ساعات سکوت"),
            "panel_health_quiet_hours_start": _("شروع ساعات سکوت"),
            "panel_health_quiet_hours_end": _("پایان ساعات سکوت"),
        }
        help_texts = {
            "panel_monitor_enabled": _("اگر غیرفعال باشد health check پنل‌ها اجرا نمی‌شود."),
            "panel_monitor_alerts_enabled": _("کلید سازگاری با مانیتورینگ قدیمی؛ برای ارسال هشدار باید فعال باشد."),
            "panel_health_alerts_enabled": _("پیام‌ها فقط برای ادمین‌های تنظیم‌شده در ربات ارسال می‌شود، نه مشتری‌ها."),
            "panel_health_alert_check_interval_minutes": _("این مقدار برای زمان‌بندی cron/systemd استفاده می‌شود؛ خود Django timer نصب نمی‌کند."),
            "panel_health_alert_repeat_interval_minutes": _("تا قبل از این فاصله، هشدار خطای تکراری دوباره ارسال نمی‌شود."),
            "panel_health_alert_failure_threshold_count": _("بعد از این تعداد خطای پشت‌سرهم هشدار ارسال می‌شود."),
        }


class PanelHealthAlertAdminMixin:
    @admin.display(description=_("گیرندگان هشدار"))
    def panel_health_alert_recipient_summary(self, obj):
        if not obj:
            return "-"
        summary = get_panel_health_alert_recipient_summary(obj)
        masked = summary["masked"]
        if not masked:
            url = reverse("admin:store_botconfiguration_changelist")
            if obj.pk:
                url = f"{url}?{urlencode({'store__id__exact': obj.pk, 'provider__exact': BotConfiguration.Provider.TELEGRAM})}"
            return format_html(
                '<span class="badge bg-warning text-dark">{}</span> <a class="button" href="{}">{}</a>',
                _("No Telegram admin recipients"),
                url,
                _("Manage BotConfiguration"),
            )
        return format_html(
            '{} <span class="small text-muted">{}</span>',
            format_html('<span class="badge bg-success">{} admin(s)</span>', summary["count"]),
            ", ".join(masked),
        )

    @admin.display(description=_("آخرین وضعیت هشدار"))
    def panel_health_last_alert_status(self, obj):
        if not obj:
            return "-"
        statuses = PanelHealthStatus.objects.filter(panel__store=obj).select_related("panel")
        problem_count = statuses.filter(status__in=[PanelHealthStatus.Status.WARNING, PanelHealthStatus.Status.ERROR]).count()
        latest_alert = statuses.exclude(last_alert_sent_at__isnull=True).order_by("-last_alert_sent_at", "-updated_at").first()
        latest_status = statuses.order_by("-last_checked_at", "-updated_at").first()
        parts = [
            _("پنل‌های دارای مشکل: %(count)s") % {"count": problem_count},
            _("آخرین وضعیت: %(status)s") % {"status": latest_status.status if latest_status else "-"},
        ]
        if latest_alert:
            alert_at = timezone.localtime(latest_alert.last_alert_sent_at).strftime("%Y-%m-%d %H:%M")
            parts.append(_("آخرین هشدار: panel=%(panel)s at %(time)s") % {"panel": latest_alert.panel_id, "time": alert_at})
            if latest_alert.last_alert_error_code:
                parts.append(_("کد خطا: %(code)s") % {"code": latest_alert.last_alert_error_code})
        return format_html("<br>".join("{}" for _ in parts), *parts)

    @admin.display(description=_("وضعیت هشدار"))
    def panel_health_alerts_status(self, obj):
        if not obj or not obj.panel_health_alerts_enabled:
            return format_html('<span class="badge bg-secondary">{}</span>', _("Disabled"))
        return format_html('<span class="badge bg-success">{}</span>', _("Enabled"))


@admin.register(Store)
class StoreAdmin(PanelHealthAlertAdminMixin, ImportExportModelAdmin):
    form = StoreAdminForm
    import_export_change_list_template = "admin/store/store/change_list.html"
    revenue_engine_fields = (
        "revenue_engine_enabled",
        "revenue_engine_dry_run",
        "revenue_engine_quiet_hours_enabled",
        "revenue_engine_quiet_hours_start",
        "revenue_engine_quiet_hours_end",
        "revenue_engine_timezone",
        "renewal_engine_enabled",
        "upsell_engine_enabled",
        "retention_engine_enabled",
        "ai_revenue_optimizer_enabled",
        "revenue_optimization_enabled",
        "revenue_max_offers_per_user_per_day",
        "revenue_max_offers_per_user_per_week",
        "revenue_max_total_offers_per_day",
        "revenue_min_ai_confidence",
        "revenue_offer_cooldown_hours",
        "retention_offer_cooldown_hours",
    )
    list_display = (
        "name",
        "domain",
        "sales_status",
        "payment_status",
        "telegram_status",
        "xui_status",
        "revenue_status",
        "is_active",
        "updated_at",
    )
    list_filter = (
        "is_active",
        "sales_mode",
        "revenue_engine_enabled",
        "revenue_engine_dry_run",
    )
    search_fields = ("name", "english_name", "domain")
    date_hierarchy = "created_at"
    prepopulated_fields = {"slug": ("english_name",)}
    autocomplete_fields = ("free_trial_panel", "free_trial_inbound")
    readonly_fields = (
        "admin_setup_summary",
        "setup_center_link",
        "revenue_control_center_link",
        "telegram_bot_hint",
        "panel_health_alert_recipient_summary",
        "panel_health_last_alert_status",
        "card_number_status",
        "sms_webhook_token_status",
        "sms_webhook_token_rotation_help",
        "created_at",
        "updated_at",
    )

    def get_fieldsets(self, request, obj=None):
        revenue_url = revenue_control_url(obj)
        if self.has_change_permission(request, obj):
            payment_fields = (
                "card_number_status",
                "card_number",
                "card_owner",
                "bank_name",
                "sheba_number",
                "receipt_image_only_payment",
                "payment_sms_time_zone",
                "new_smsforwarder_webhook_token",
                "sms_webhook_token_status",
                "sms_webhook_token_rotation_help",
            )
        else:
            payment_fields = (
                "card_number_status",
                "card_owner",
                "bank_name",
                "sheba_number",
                "receipt_image_only_payment",
                "payment_sms_time_zone",
                "sms_webhook_token_status",
                "sms_webhook_token_rotation_help",
            )
        return (
            (
                _("Quick Setup / وضعیت کلی"),
                {
                    "fields": ("admin_setup_summary", "setup_center_link", "revenue_control_center_link", "is_active"),
                    "description": _("بعد از نصب minimal از Setup Center برای تکمیل Store، پرداخت، تلگرام، پنل، پلن‌ها و Revenue استفاده کن."),
                },
            ),
            (
                _("Brand / Identity"),
                {
                    "fields": (
                        "name",
                        "english_name",
                        "slug",
                        "domain",
                        "hero_title",
                        "hero_text",
                    ),
                },
            ),
            (
                _("Payment"),
                {
                    "fields": payment_fields,
                    "description": _("شماره کارت و توکن‌ها کامل نمایش داده نمی‌شوند؛ برای تغییر، مقدار جدید را وارد کن."),
                },
            ),
            (
                _("Customer / Sales Settings"),
                {
                    "fields": (
                        "sales_mode",
                        "plan_inbound_routing_enabled",
                        "allow_global_inbound_fallback",
                        "custom_volume_price_per_gb",
                        "referral_system_enabled",
                        "referral_reward_gb",
                        "referral_reward_duration_days",
                        "referral_min_order_required",
                        "free_trial_enabled",
                        "free_trial_panel",
                        "free_trial_inbound",
                        "free_trial_traffic_gb",
                        "free_trial_duration_hours",
                        "free_trial_cooldown_days",
                    ),
                },
            ),
            (
                _("Telegram / Bot Related"),
                {
                    "fields": (
                        "telegram_bot_hint",
                        "telegram_channel",
                        "telegram_support",
                        "bale_support",
                        "support_email",
                    ),
                    "description": _("تنظیمات اصلی ربات در BotConfiguration است؛ این بخش فقط لینک‌ها و کانال‌های عمومی Store را نگه می‌دارد."),
                },
            ),
            (
                _("هشدار سلامت پنل‌ها"),
                {
                    "fields": (
                        "panel_health_alert_recipient_summary",
                        "panel_health_last_alert_status",
                        "panel_monitor_enabled",
                        "panel_monitor_alerts_enabled",
                        "panel_health_alerts_enabled",
                        "panel_health_alert_check_interval_minutes",
                        "panel_health_alert_repeat_interval_minutes",
                        "panel_health_alert_failure_threshold_count",
                        "panel_health_recovery_alert_enabled",
                        "panel_health_quiet_hours_enabled",
                        "panel_health_quiet_hours_start",
                        "panel_health_quiet_hours_end",
                    ),
                    "description": _("ارسال فقط به ادمین‌های Telegram تنظیم‌شده در BotConfiguration انجام می‌شود؛ توکن ربات اینجا نمایش داده نمی‌شود."),
                },
            ),
            (
                _("Revenue Engine Controls"),
                {
                    "classes": ("collapse",),
                    "fields": self.revenue_engine_fields,
                    "description": format_html(
                        '{} <a class="button" href="{}">{}</a>',
                        _("Advanced: برای نصب جدید dry_run را فعال نگه دار تا پیام واقعی ارسال نشود."),
                        revenue_url,
                        _("Open Revenue Control Center"),
                    ),
                },
            ),
            (
                _("Reminders / Reports / Monitoring"),
                {
                    "fields": (
                        "analytics_enabled",
                        "broadcast_enabled",
                        "renewal_reminders_enabled",
                        "low_traffic_reminders_enabled",
                        "daily_admin_report_enabled",
                        "daily_admin_report_time",
                        "daily_admin_report_timezone",
                        "daily_admin_report_include_panel_health",
                        "daily_admin_report_include_financials",
                        "daily_admin_report_include_errors",
                        "panel_usage_tracking_enabled",
                        "panel_usage_report_enabled",
                    ),
                },
            ),
            (
                _("Advanced / Legacy"),
                {
                    "classes": ("collapse",),
                    "fields": (
                        "broadcast_rate_limit_per_second",
                        "broadcast_max_recipients_per_campaign",
                        "renewal_reminders_start_at",
                        "reminder_days_before_expiry",
                        "reminder_days_after_expiry",
                        "low_traffic_percent_threshold",
                        "low_traffic_gb_threshold",
                        "panel_monitor_check_timeout_seconds",
                        "panel_monitor_alert_cooldown_minutes",
                        "panel_monitor_max_log_age_days",
                        "panel_usage_snapshot_retention_days",
                        "panel_usage_active_user_method",
                        "reminder_cooldown_hours",
                        "reminder_max_per_client_per_day",
                        "good_customer_min_total_amount",
                        "loyal_customer_min_orders_30d",
                        "top_customers_limit",
                        "inactive_customer_days",
                        "created_at",
                        "updated_at",
                    ),
                },
            ),
        )

    def status_badge(self, status, label):
        css_class = {
            "done": "bg-success",
            "safe": "bg-info",
            "warning": "bg-warning text-dark",
            "missing": "bg-secondary",
            "error": "bg-danger",
        }.get(status, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css_class, label)

    @admin.display(description=_("Sales / routes"))
    def sales_status(self, obj):
        plans_count = active_sellable_plans(obj).count()
        missing_routes = missing_route_labels(obj)
        if missing_routes:
            return self.status_badge("error", _("Route missing"))
        if plans_count:
            return self.status_badge("done", _("%(count)s plan(s)") % {"count": plans_count})
        if getattr(obj, "custom_volume_price_per_gb", 0):
            return self.status_badge("done", _("Custom volume"))
        return self.status_badge("warning", _("No public plan"))

    @admin.display(description=_("Payment"))
    def payment_status(self, obj):
        if has_real_payment_card(obj):
            return self.status_badge("done", _("Configured"))
        if obj.card_number or obj.card_owner:
            return self.status_badge("warning", _("Review setup"))
        return self.status_badge("missing", _("Missing"))

    @admin.display(description=_("Telegram"))
    def telegram_status(self, obj):
        configs = list(active_telegram_configs(obj))
        if not configs:
            return self.status_badge("warning", _("Not configured"))
        ready = [config for config in configs if config.bot_token and config.get_admin_user_ids()]
        if ready:
            return self.status_badge("done", _("%(count)s active") % {"count": len(ready)})
        return self.status_badge("warning", _("Incomplete"))

    @admin.display(description=_("X-UI / panel"))
    def xui_status(self, obj):
        panel_count = obj.panels.filter(is_active=True).count()
        inbound_count = active_sales_inbounds(obj).count()
        if panel_count and inbound_count:
            return self.status_badge("done", _("%(count)s inbound(s)") % {"count": inbound_count})
        if panel_count:
            return self.status_badge("warning", _("No sales inbound"))
        return self.status_badge("warning", _("No panel"))

    @admin.display(description=_("Revenue"))
    def revenue_status(self, obj):
        if not obj.revenue_engine_enabled:
            return self.status_badge("warning", _("Disabled"))
        if obj.revenue_engine_dry_run:
            return self.status_badge("safe", _("Dry-run"))
        return self.status_badge("warning", _("Real send"))

    @admin.display(description=_("Setup summary"))
    def admin_setup_summary(self, obj):
        if not obj:
            return _("Save the Store first, then review Setup Center.")
        rows = []
        for label, value, status in build_store_admin_summary(obj):
            rows.append((label, self.status_badge(status, value)))
        setup_url = reverse("admin_store_setup_center") + f"?{urlencode({'store': obj.pk})}"
        wizard_url = setup_wizard_index_url(obj)
        summary = format_html_join(
            "",
            '<li><strong>{}</strong> {}</li>',
            rows,
        )
        return format_html(
            '<div class="qasedak-admin-summary"><ul>{}</ul><p><a class="button" href="{}">{}</a> <a class="button" href="{}">{}</a></p></div>',
            summary,
            wizard_url,
            _("Open Setup Wizard"),
            setup_url,
            _("Open Setup Center"),
        )

    @admin.display(description=_("Setup Center"))
    def setup_center_link(self, obj):
        url = reverse("admin_store_setup_center")
        wizard_url = reverse("admin_store_setup_wizard")
        if obj and obj.pk:
            url = f"{url}?{urlencode({'store': obj.pk})}"
            wizard_url = setup_wizard_index_url(obj)
        return format_html(
            '<a class="button" href="{}">{}</a> <a class="button" href="{}">{}</a>',
            wizard_url,
            _("راه‌اندازی مرحله‌ای"),
            url,
            _("Setup Center"),
        )

    @admin.display(description=_("Revenue Control Center"))
    def revenue_control_center_link(self, obj):
        return format_html(
            '<a class="button" href="{}">{}</a>',
            revenue_control_url(obj),
            _("کنترل درآمد هوشمند"),
        )

    @admin.display(description=_("Card number"))
    def card_number_status(self, obj):
        if not obj or not obj.card_number:
            return self.status_badge("missing", _("Not configured"))
        hint = str(obj.card_number)[-4:]
        return format_html("{} {}", self.status_badge("done", _("Configured")), _("ending in %(hint)s") % {"hint": hint})

    @admin.display(description=_("BotConfiguration"))
    def telegram_bot_hint(self, obj):
        url = reverse("admin:store_botconfiguration_changelist")
        if obj and obj.pk:
            url = f"{url}?{urlencode({'store__id__exact': obj.pk})}"
            count = active_telegram_configs(obj).count()
        else:
            count = 0
        return format_html(
            '{} <a class="button" href="{}">{}</a>',
            _("%(count)s active Telegram bot configuration(s).") % {"count": count},
            url,
            _("Manage BotConfiguration"),
        )

    @admin.display(description=_("Referral traffic GB"), ordering="referral_reward_gb")
    def referral_reward_traffic_gb(self, obj):
        return obj.referral_reward_gb

    @admin.display(description=_("SMS webhook token"))
    def sms_webhook_token_status(self, obj):
        if getattr(obj, "smsforwarder_webhook_token_hash", ""):
            hint = obj.smsforwarder_webhook_token_hint or "----"
            return _("Configured, ending in %(hint)s") % {"hint": hint}
        return _("Not configured")

    @admin.display(description=_("SMS token rotation"))
    def sms_webhook_token_rotation_help(self, obj):
        return _("پس از تغییر توکن، همین مقدار را در اپ SMS Forwarder هم تنظیم کنید.")

    @admin.display(description=_("Post-install setup checklist"))
    def post_install_setup_checklist(self, obj):
        items = (
            "Review Store identity and support channels.",
            "Configure payment/card and SMSForwarder webhook settings.",
            "Create or enable Telegram BotConfiguration and optional proxy/force-join settings.",
            "Create X-UI/Sanaei Panel, sync or create Inbound, then create Plan and PlanInboundRoute.",
            "Run doctor/check_integrations, test a purchase, then review Revenue Engine dry-run logs.",
        )
        return format_html("<ol>{}</ol>", format_html_join("", "<li>{}</li>", ((item,) for item in items)))

    def save_model(self, request, obj, form, change):
        token_changed = bool(form.cleaned_data.get("new_smsforwarder_webhook_token"))
        super().save_model(request, obj, form, change)
        if token_changed:
            messages.success(
                request,
                _("SMSForwarder webhook token was updated. Configure the same token in the SMS Forwarder app."),
            )


@admin.register(PanelHealthAlertSettings)
class PanelHealthAlertSettingsAdmin(PanelHealthAlertAdminMixin, ImportExportModelAdmin):
    form = PanelHealthAlertSettingsAdminForm
    change_form_template = "admin/store/panelhealthalertsettings/change_form.html"
    change_list_template = "admin/store/panelhealthalertsettings/change_list.html"
    list_display = (
        "store_name",
        "store_domain",
        "panel_health_alerts_status",
        "panel_health_alert_recipient_summary",
        "updated_at",
    )
    list_filter = ("is_active", "panel_health_alerts_enabled", "panel_health_quiet_hours_enabled")
    search_fields = ("name", "english_name", "domain")
    readonly_fields = (
        "panel_health_alert_recipient_summary",
        "panel_health_last_alert_status",
        "telegram_bot_hint",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("وضعیت هشدار"),
            {
                "fields": (
                    "name",
                    "domain",
                    "is_active",
                    "panel_monitor_enabled",
                    "panel_monitor_alerts_enabled",
                    "panel_health_alerts_enabled",
                ),
                "description": _("این تنظیمات فقط برای پیام‌های ادمین استفاده می‌شود و به مشتری‌ها پیام ارسال نمی‌کند."),
            },
        ),
        (
            _("زمان‌بندی"),
            {
                "fields": (
                    "panel_health_alert_check_interval_minutes",
                    "panel_health_recovery_alert_enabled",
                ),
            },
        ),
        (
            _("جلوگیری از اسپم"),
            {
                "fields": (
                    "panel_health_alert_repeat_interval_minutes",
                    "panel_health_alert_failure_threshold_count",
                ),
            },
        ),
        (
            _("ساعات سکوت"),
            {
                "fields": (
                    "panel_health_quiet_hours_enabled",
                    "panel_health_quiet_hours_start",
                    "panel_health_quiet_hours_end",
                ),
            },
        ),
        (
            _("گیرندگان ادمین"),
            {
                "fields": (
                    "panel_health_alert_recipient_summary",
                    "telegram_bot_hint",
                ),
                "description": _("گیرنده‌ها از BotConfiguration تلگرام خوانده می‌شوند و به صورت masked نمایش داده می‌شوند."),
            },
        ),
        (
            _("عیب‌یابی"),
            {
                "classes": ("collapse",),
                "fields": ("panel_health_last_alert_status", "created_at", "updated_at"),
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("نام"), ordering="name")
    def store_name(self, obj):
        return obj.name

    @admin.display(description=_("دامنه"), ordering="domain")
    def store_domain(self, obj):
        return obj.domain or "-"

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "title": _("تنظیمات هشدار سلامت پنل‌ها"),
            "subtitle": _("این تنظیمات فقط برای پیام‌های ادمین استفاده می‌شود و به مشتری‌ها پیام ارسال نمی‌کند."),
        }
        return super().changelist_view(request, extra_context=extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "title": _("تنظیمات هشدار سلامت پنل‌ها"),
            "subtitle": _("این تنظیمات فقط برای پیام‌های ادمین استفاده می‌شود و به مشتری‌ها پیام ارسال نمی‌کند."),
        }
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    @admin.display(description=_("تنظیمات ربات"))
    def telegram_bot_hint(self, obj):
        url = reverse("admin:store_botconfiguration_changelist")
        if obj and obj.pk:
            url = f"{url}?{urlencode({'store__id__exact': obj.pk, 'provider__exact': BotConfiguration.Provider.TELEGRAM})}"
            count = obj.bot_configurations.filter(provider=BotConfiguration.Provider.TELEGRAM, is_active=True).count()
        else:
            count = 0
        return format_html(
            '{} <a class="button" href="{}">{}</a>',
            _("%(count)s active Telegram bot configuration(s).") % {"count": count},
            url,
            _("مدیریت تنظیمات ربات"),
        )


@admin.register(Operator)
class OperatorAdmin(ImportExportModelAdmin):
    list_display = ("name", "store", "slug", "is_active", "sort_order", "updated_at")
    list_filter = ("store", "is_active")
    search_fields = ("name", "slug", "description", "store__name", "store__english_name")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    list_editable = ("is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


class BotEventLogInline(admin.TabularInline):
    model = BotEventLog
    extra = 0
    can_delete = False
    fields = ("event_type", "status", "order", "message", "created_at")
    readonly_fields = fields
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(RevenueOfferLog)
class RevenueOfferLogAdmin(ImportExportModelAdmin):
    list_display = (
        "created_at",
        "status",
        "engine_type",
        "event_type",
        "offer_type",
        "customer_ref",
        "decision_source",
        "skip_reason",
        "ai_confidence",
        "sent_at",
        "converted_at",
    )
    list_filter = ("store", "engine_type", "status", "decision_source", "event_type", "created_at")
    search_fields = (
        "=id",
        "=customer__id",
        "=bot_user__id",
        "=vpn_client__id",
        "event_type",
        "offer_type",
        "variant",
        "skip_reason",
    )
    readonly_fields = (
        "control_center_link",
        "redacted_metadata",
        "redacted_error_message",
        "created_at",
        "sent_at",
        "converted_at",
    )
    exclude = ("metadata", "error_message")
    date_hierarchy = "created_at"
    list_select_related = ("store", "customer", "bot_user", "vpn_client")
    autocomplete_fields = ("store", "customer", "bot_user", "vpn_client")

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("customer"))
    def customer_ref(self, obj):
        if obj.customer_id:
            return f"Customer #{obj.customer_id}"
        if obj.bot_user_id:
            return f"BotUser #{obj.bot_user_id}"
        if obj.vpn_client_id:
            return f"VPNClient #{obj.vpn_client_id}"
        return "-"

    @admin.display(description=_("Revenue Control Center"))
    def control_center_link(self, obj):
        return format_html(
            '<a class="button" href="{}">{}</a>',
            revenue_control_url(getattr(obj, "store", None)),
            _("Back to Revenue Control Center"),
        )

    def _redact_metadata_value(self, value, key=""):
        sensitive_key_parts = (
            "token",
            "secret",
            "password",
            "proxy",
            "uuid",
            "link",
            "config",
            "phone",
            "email",
            "card",
            "chat_id",
            "telegram_id",
            "provider_user_id",
            "url",
        )
        key_lower = str(key or "").lower()
        if any(part in key_lower for part in sensitive_key_parts):
            return "<redacted>"
        if isinstance(value, dict):
            return {item_key: self._redact_metadata_value(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [self._redact_metadata_value(item, key) for item in value]
        if isinstance(value, str):
            lowered = value.lower()
            looks_sensitive = (
                "://" in lowered
                or "/sub/" in lowered
                or "@" in value
                or re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value)
                or re.search(r"[A-Za-z0-9_-]{24,}", value)
                or re.search(r"\d{10,}", value)
            )
            if looks_sensitive:
                return "<redacted>"
        return value

    @admin.display(description=_("metadata"))
    def redacted_metadata(self, obj):
        redacted = self._redact_metadata_value(obj.metadata or {})
        return format_html("<pre>{}</pre>", json.dumps(redacted, ensure_ascii=False, indent=2, sort_keys=True))

    @admin.display(description=_("error message"))
    def redacted_error_message(self, obj):
        redacted = self._redact_metadata_value(obj.error_message or "")
        return redacted or "-"


class BotConfigurationAdminForm(forms.ModelForm):
    bot_token = forms.CharField(
        label=_("bot token"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("برای تغییر token مقدار کامل را وارد کنید. مقدار فعلی کامل نمایش داده نمی‌شود."),
    )

    class Meta:
        model = BotConfiguration
        fields = "__all__"

    def clean_bot_token(self):
        value = (self.cleaned_data.get("bot_token") or "").strip()
        if value:
            return value
        if self.instance and self.instance.pk:
            return self.instance.bot_token
        raise forms.ValidationError(_("Bot token برای BotConfiguration جدید لازم است."))


@admin.register(BotConfiguration)
class BotConfigurationAdmin(ImportExportModelAdmin):
    form = BotConfigurationAdminForm
    list_display = (
        "name",
        "provider",
        "store",
        "telegram_bot_username",
        "is_active",
        "admin_status",
        "token_status",
        "force_telegram_channel_join",
    )
    list_filter = (
        "provider",
        "store",
        "is_active",
        "force_telegram_channel_join",
        "notify_new_orders",
        "notify_order_updates",
        "send_sales_reports",
    )
    search_fields = (
        "name",
        "admin_user_id",
        "additional_admin_user_ids",
        "telegram_bot_username",
        "telegram_required_channel_id",
        "telegram_required_channel_username",
        "store__name",
    )
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    readonly_fields = (
        "token_status",
        "masked_webhook_secret",
        "webhook_path_hint",
        "event_logs_link",
        "force_join_configuration_warning",
        "last_error",
        "created_at",
        "updated_at",
        "last_report_sent_at",
    )
    fieldsets = (
        (
            _("Connection"),
            {
                "fields": (
                    "name",
                    "store",
                    "provider",
                    "telegram_bot_username",
                    "token_status",
                    "bot_token",
                    "admin_user_id",
                    "additional_admin_user_ids",
                    "is_active",
                )
            },
        ),
        (
            _("Telegram force join"),
            {
                "fields": (
                    "force_telegram_channel_join",
                    "telegram_required_channel_id",
                    "telegram_required_channel_username",
                    "telegram_required_channel_invite_link",
                    "telegram_join_check_message",
                    "force_join_configuration_warning",
                )
            },
        ),
        (
            _("Notifications"),
            {
                "fields": (
                    "notify_new_orders",
                    "notify_order_updates",
                    "send_sales_reports",
                    "report_interval_hours",
                    "last_report_sent_at",
                )
            },
        ),
        (
            _("Webhook"),
            {
                "classes": ("collapse",),
                "fields": ("masked_webhook_secret", "webhook_path_hint"),
            },
        ),
        (
            _("Diagnostics"),
            {
                "classes": ("collapse",),
                "fields": ("event_logs_link", "last_error", "created_at", "updated_at"),
            },
        ),
    )
    actions = ("send_test_message", "send_sales_report_now")

    def get_fieldsets(self, request, obj=None):
        if self.has_change_permission(request, obj):
            return super().get_fieldsets(request, obj)
        return (
            (
                _("Connection"),
                {
                    "fields": (
                        "name",
                        "store",
                        "provider",
                        "telegram_bot_username",
                        "token_status",
                        "is_active",
                    )
                },
            ),
            (
                _("Telegram force join"),
                {
                    "fields": (
                        "force_telegram_channel_join",
                        "telegram_required_channel_username",
                        "force_join_configuration_warning",
                    )
                },
            ),
            (
                _("Notifications"),
                {
                    "fields": (
                        "notify_new_orders",
                        "notify_order_updates",
                        "send_sales_reports",
                        "report_interval_hours",
                        "last_report_sent_at",
                    )
                },
            ),
            (
                _("Webhook"),
                {
                    "classes": ("collapse",),
                    "fields": ("masked_webhook_secret", "webhook_path_hint"),
                },
            ),
            (
                _("Diagnostics"),
                {
                    "classes": ("collapse",),
                    "fields": ("event_logs_link", "last_error", "created_at", "updated_at"),
                },
            ),
        )

    @admin.display(description=_("Bot token"))
    def token_status(self, obj):
        if obj and (obj.bot_token or "").strip():
            return format_html('<span class="badge bg-success">{}</span>', _("Configured"))
        return format_html('<span class="badge bg-secondary">{}</span>', _("Not configured"))

    @admin.display(description=_("Admin status"))
    def admin_status(self, obj):
        count = len(obj.get_admin_user_ids()) if obj else 0
        if count:
            return format_html('<span class="badge bg-success">{} admin(s)</span>', count)
        return format_html('<span class="badge bg-warning text-dark">{}</span>', _("Missing admins"))

    @admin.display(description=_("Webhook secret"))
    def masked_webhook_secret(self, obj):
        if not obj or not obj.pk:
            return _("Save first to generate webhook secret.")
        return _("Generated and hidden.")

    @admin.display(description=_("Webhook path"))
    def webhook_path_hint(self, obj):
        if not obj or not obj.pk:
            return _("Save first to generate webhook path.")
        return f"/bot/{obj.provider}/<hidden>/webhook/"

    @admin.display(description=_("Bot event logs"))
    def event_logs_link(self, obj):
        if not obj or not obj.pk:
            return _("Save first to view logs.")
        url = f"{reverse('admin:store_boteventlog_changelist')}?{urlencode({'bot_config__id__exact': obj.pk})}"
        return format_html('<a class="button" href="{}">{}</a>', url, _("View bot event logs"))

    @admin.display(description=_("Force join status"))
    def force_join_configuration_warning(self, obj):
        if not obj or not obj.force_telegram_channel_join:
            return "-"
        if obj.telegram_required_channel_id or obj.telegram_required_channel_username:
            return _("Channel settings look complete.")
        return format_html(
            '<strong style="color:#b45309;">{}</strong>',
            _("Force Join is enabled, but Channel ID or Username is not configured."),
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.force_telegram_channel_join and not (
            obj.telegram_required_channel_id or obj.telegram_required_channel_username
        ):
            messages.warning(
                request,
                _("Force Join is enabled, but Channel ID or Username is not configured."),
            )

    @admin.action(description=_("Send test message"))
    def send_test_message(self, request, queryset):
        sent = 0
        for config in queryset:
            if send_to_config(
                config,
                text=_("Bot connection test from Django admin."),
                event_type=BotEventLog.EventType.WEBHOOK,
            ):
                sent += 1
        messages.success(request, _("%(count)s test message(s) sent.") % {"count": sent})

    @admin.action(description=_("Send sales report now"))
    def send_sales_report_now(self, request, queryset):
        sent = 0
        for config in queryset:
            report, sent_at = build_sales_report(config)
            if send_to_config(
                config,
                text=report,
                event_type=BotEventLog.EventType.SALES_REPORT,
            ):
                config.last_report_sent_at = sent_at
                config.save(update_fields=["last_report_sent_at", "updated_at"])
                sent += 1
        messages.success(request, _("%(count)s sales report(s) sent.") % {"count": sent})


@admin.register(BotAdminOrderMessage)
class BotAdminOrderMessageAdmin(ImportExportModelAdmin):
    list_display = ("bot_config", "order", "admin_user_id", "chat_id", "message_id", "message_kind", "created_at")
    list_filter = ("bot_config", "message_kind", "created_at")
    search_fields = ("order__order_tracking_code", "admin_user_id", "chat_id", "message_id")
    autocomplete_fields = ("bot_config", "order")
    readonly_fields = ("created_at", "updated_at", "metadata")
    date_hierarchy = "created_at"
    list_select_related = ("bot_config", "order")


@admin.register(BotUser)
class BotUserAdmin(ImportExportModelAdmin):
    change_list_template = "admin/store/botuser/change_list.html"
    list_display = (
        "display_name",
        "provider_user_id",
        "chat_id",
        "username",
        "bot_config",
        "customer",
        "state",
        "is_active",
        "last_seen_at",
    )
    list_filter = ("bot_config", "state", "is_active", "last_seen_at", "created_at")
    search_fields = (
        "display_name",
        "username",
        "first_name",
        "last_name",
        "provider_user_id",
        "chat_id",
        "customer__username",
        "customer__phone_number",
    )
    autocomplete_fields = ("bot_config", "customer")
    readonly_fields = ("created_at", "updated_at", "last_seen_at")
    date_hierarchy = "last_seen_at"
    list_select_related = ("bot_config", "customer")
    ordering = ("-last_seen_at",)
    actions = ("activate_users", "deactivate_users", "reset_state_selected")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "wizwiz-restore/",
                self.admin_site.admin_view(self.wizwiz_restore_view),
                name="store_botuser_wizwiz_restore",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context.update(
            {
                "wizwiz_restore_url": reverse("admin:store_botuser_wizwiz_restore"),
                "can_import_wizwiz": self._has_legacy_import_permission(request, "add"),
            }
        )
        return super().changelist_view(request, extra_context=extra_context)

    def _has_legacy_import_permission(self, request, action):
        return request.user.is_active and request.user.is_staff and request.user.has_perm(
            f"store.{action}_legacywizwizimportjob"
        )

    def _require_legacy_import_permission(self, request, action):
        if not self._has_legacy_import_permission(request, action):
            raise PermissionDenied

    def _get_wizwiz_job(self, request, job_id, *, action="view"):
        self._require_legacy_import_permission(request, action)
        try:
            return LegacyWizWizImportJob.objects.get(pk=job_id)
        except LegacyWizWizImportJob.DoesNotExist as exc:
            raise Http404 from exc

    def _require_simple_restore_permission(self, request):
        self._require_legacy_import_permission(request, "add")
        self._require_legacy_import_permission(request, "change")

    def wizwiz_restore_view(self, request):
        self._require_simple_restore_permission(request)
        restore_state = request.session.get("wizwiz_restore_last") or {}
        import_form = WizWizSimpleRestoreForm()
        message_form = WizWizSimpleRestoreMessageForm()

        if request.method == "POST":
            action = request.POST.get("action")
            if action == "start_import":
                import_form = WizWizSimpleRestoreForm(request.POST, request.FILES)
                if import_form.is_valid():
                    uploaded_file = import_form.cleaned_data["sql_file"]
                    try:
                        result = wizwiz_simple_restore(
                            uploaded_file,
                            created_by=request.user,
                            title=Path(uploaded_file.name or "wizwiz.sql").name,
                            skip_admins=True,
                            import_agents=True,
                            only_wallet_positive=False,
                            update_existing=False,
                            create_customers=True,
                        )
                    except Exception:
                        messages.error(request, _("Import failed. Please verify the backup file and try again."))
                    else:
                        request.session["wizwiz_restore_last"] = {
                            "job_id": result["job_id"],
                            "result": result,
                            "message_result": None,
                        }
                        request.session.modified = True
                        messages.success(request, _("WizWiz restore completed."))
                        return HttpResponseRedirect(reverse("admin:store_botuser_wizwiz_restore"))
                else:
                    messages.error(request, _("Please upload a valid WizWiz SQL backup file."))
            elif action == "send_message":
                message_form = WizWizSimpleRestoreMessageForm(request.POST)
                job_id = restore_state.get("job_id")
                if not job_id:
                    messages.error(request, _("Run an import before sending a message."))
                elif message_form.is_valid():
                    job = self._get_wizwiz_job(request, job_id, action="change")
                    try:
                        batch = create_legacy_import_message_batch(
                            job,
                            message_form.cleaned_data["text"],
                            request.user,
                        )
                        counts = send_legacy_import_message_batch(batch)
                    except Exception:
                        messages.error(request, _("Message send failed. Please try again."))
                    else:
                        restore_state["message_result"] = {
                            "sent": counts["sent"],
                            "failed": counts["failed"],
                            "skipped": counts["skipped_no_chat_id"],
                            "blocked": counts["blocked"],
                        }
                        request.session["wizwiz_restore_last"] = restore_state
                        request.session.modified = True
                        messages.success(request, _("Message sent to imported users."))
                        return HttpResponseRedirect(reverse("admin:store_botuser_wizwiz_restore"))
                else:
                    messages.error(request, _("Please fix the message form errors."))
            elif action == "clear":
                request.session.pop("wizwiz_restore_last", None)
                request.session.modified = True
                return HttpResponseRedirect(reverse("admin:store_botuser_wizwiz_restore"))

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("WizWiz Restore"),
            "import_form": import_form,
            "message_form": message_form,
            "restore_state": restore_state,
            "restore_result": restore_state.get("result"),
            "message_result": restore_state.get("message_result"),
        }
        return TemplateResponse(request, "admin/store/botuser/wizwiz_restore.html", context)

    @admin.action(description=_("Activate selected bot users"))
    def activate_users(self, request, queryset):
        updated = queryset.update(is_active=True, updated_at=timezone.now())
        messages.success(request, _("%(count)s bot user(s) activated.") % {"count": updated})

    @admin.action(description=_("Deactivate selected bot users"))
    def deactivate_users(self, request, queryset):
        updated = queryset.update(is_active=False, updated_at=timezone.now())
        messages.success(request, _("%(count)s bot user(s) deactivated.") % {"count": updated})

    @admin.action(description=_("Reset selected bot user states"))
    def reset_state_selected(self, request, queryset):
        updated = queryset.update(state=BotUser.State.IDLE, state_data={}, updated_at=timezone.now())
        messages.success(request, _("%(count)s bot user state(s) reset.") % {"count": updated})


@admin.register(WebTelegramLinkToken)
class WebTelegramLinkTokenAdmin(ImportExportModelAdmin):
    list_display = (
        "customer",
        "status",
        "bot_user",
        "source",
        "created_at",
        "expires_at",
        "used_at",
    )
    list_filter = ("status", "source", "created_at")
    search_fields = (
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
        "customer__referral_code",
        "bot_user__provider_user_id",
        "bot_user__chat_id",
        "bot_user__username",
    )
    autocomplete_fields = ("customer", "bot_user")
    readonly_fields = (
        "token_hash",
        "created_at",
        "updated_at",
        "used_at",
        "revoked_at",
        "metadata",
    )
    date_hierarchy = "created_at"
    list_select_related = ("customer", "bot_user", "bot_user__bot_config")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    can_delete = False
    fields = ("sender_type", "body_preview", "bot_config", "created_at")
    readonly_fields = fields
    ordering = ("created_at", "id")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Body"))
    def body_preview(self, obj):
        body = obj.body or ""
        return body[:140] + ("..." if len(body) > 140 else "")


@admin.register(SupportConversation)
class SupportConversationAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "customer_summary",
        "store",
        "masked_contact_value",
        "status_badge",
        "last_message_snippet",
        "created_at",
        "updated_at",
        "quick_review_link",
    )
    list_filter = ("status", "store", "created_at", "updated_at")
    search_fields = (
        "=id",
        "=customer__id",
        "contact_value",
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
    )
    autocomplete_fields = ("store", "customer")
    readonly_fields = ("created_at", "updated_at", "last_customer_message_at", "last_admin_message_at", "closed_at")
    date_hierarchy = "updated_at"
    list_select_related = ("store", "customer")
    inlines = (SupportMessageInline,)
    actions = ("close_conversations", "mark_waiting_admin")

    def qadmin_badge(self, label, tone="secondary"):
        css_class = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "info": "bg-info text-dark",
            "secondary": "bg-secondary",
        }.get(tone, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css_class, label)

    @admin.display(description=_("Customer"), ordering="customer__display_name")
    def customer_summary(self, obj):
        if not obj.customer_id:
            return self.qadmin_badge(_("No customer"), "secondary")
        label = admin_safe_customer_label(obj.customer)
        phone = admin_mask_phone(obj.customer.phone_number)
        url = reverse("admin_store_customer_review", args=[obj.customer_id])
        if phone:
            return format_html('<a href="{}">{}</a><br><small>{}</small>', url, label, phone)
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description=_("Contact"))
    def masked_contact_value(self, obj):
        return sanitize_support_message(obj.contact_value, limit=80) or "-"

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        tone = {
            SupportConversation.Status.OPEN: "info",
            SupportConversation.Status.WAITING_ADMIN: "warning",
            SupportConversation.Status.ANSWERED: "success",
            SupportConversation.Status.CLOSED: "secondary",
        }.get(obj.status, "secondary")
        return self.qadmin_badge(obj.get_status_display(), tone)

    @admin.display(description=_("Last message"))
    def last_message_snippet(self, obj):
        message = obj.messages.order_by("-created_at", "-pk").first()
        if not message:
            return "-"
        body = sanitize_support_message(message.body, limit=90)
        return format_html("{}<br><small>{}</small>", body or "-", message.get_sender_type_display())

    @admin.display(description=_("Review"))
    def quick_review_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        return format_html('<a class="button" href="{}">{}</a>', support_review_url(obj), _("Review"))

    @admin.action(description=_("Close selected support conversations"))
    def close_conversations(self, request, queryset):
        now = timezone.now()
        updated = queryset.exclude(status=SupportConversation.Status.CLOSED).update(
            status=SupportConversation.Status.CLOSED,
            closed_at=now,
            updated_at=now,
        )
        messages.success(request, _("%(count)s support conversation(s) closed.") % {"count": updated})

    @admin.action(description=_("Mark selected support conversations as waiting for admin"))
    def mark_waiting_admin(self, request, queryset):
        updated = queryset.update(
            status=SupportConversation.Status.WAITING_ADMIN,
            updated_at=timezone.now(),
        )
        messages.success(request, _("%(count)s support conversation(s) updated.") % {"count": updated})


@admin.register(SupportMessage)
class SupportMessageAdmin(ImportExportModelAdmin):
    list_display = ("id", "conversation", "sender_type", "customer", "bot_config", "created_at")
    list_filter = ("sender_type", "conversation__status", "bot_config", "created_at")
    search_fields = ("body", "conversation__contact_value", "customer__display_name", "customer__username", "customer__phone_number")
    autocomplete_fields = ("conversation", "customer", "bot_config")
    readonly_fields = ("created_at", "updated_at", "metadata")
    date_hierarchy = "created_at"
    list_select_related = ("conversation", "customer", "bot_config")


@admin.register(BotPendingAction)
class BotPendingActionAdmin(ImportExportModelAdmin):
    list_display = ("bot_config", "order", "support_conversation", "admin_user_id", "action", "status", "created_at", "resolved_at")
    list_filter = ("bot_config", "action", "status", "created_at")
    search_fields = ("order__order_tracking_code", "support_conversation__contact_value", "admin_user_id")
    autocomplete_fields = ("bot_config", "order", "support_conversation")
    readonly_fields = ("created_at", "updated_at", "resolved_at")
    date_hierarchy = "created_at"
    list_select_related = ("bot_config", "order", "support_conversation")


@admin.register(BotEventLog)
class BotEventLogAdmin(ImportExportModelAdmin):
    list_display = ("event_type", "status", "bot_config", "order", "created_at")
    list_filter = ("event_type", "status", "bot_config", "created_at")
    search_fields = ("message", "order__order_tracking_code", "bot_config__name")
    autocomplete_fields = ("bot_config", "order")
    readonly_fields = ("created_at", "updated_at", "raw_payload")
    date_hierarchy = "created_at"
    list_select_related = ("bot_config", "order")


class PlanBulkPriceForm(forms.Form):
    price_per_gb = forms.DecimalField(
        label=_("قیمت هر گیگ"),
        min_value=Decimal("0"),
        max_digits=14,
        decimal_places=3,
        help_text=_("برای همه پلن‌ها اعمال می‌شود و واحد پول هر پلن همان مقدار فعلی خودش می‌ماند."),
        widget=forms.NumberInput(
            attrs={
                "class": "vIntegerField",
                "min": "0",
                "step": "0.001",
                "placeholder": "100000",
            }
        ),
    )


def calculate_plan_price_from_per_gb(volume_gb, price_per_gb):
    raw_price = Decimal(volume_gb or 0) * Decimal(price_per_gb)
    return int(raw_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class SalesInboundChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        panel_name = getattr(getattr(obj, "panel", None), "name", None) or _("بدون پنل")
        remark = obj.remark or _("بدون remark")
        return f"{panel_name} / inbound {obj.xui_inbound_id} / {remark} / {obj.protocol}"


class BulkPlanInboundRouteForm(forms.Form):
    PLAN_SELECTION_ALL_ACTIVE = "all_active"
    PLAN_SELECTION_MANUAL = "manual"
    ROUTE_MODE_SINGLE = "single"
    ROUTE_MODE_MULTI = "multi"

    store = forms.ModelChoiceField(
        label=_("Store"),
        queryset=Store.objects.none(),
        required=True,
        help_text=_("پلن‌ها و inboundها بر اساس این فروشگاه فیلتر می‌شوند."),
    )
    route_mode = forms.ChoiceField(
        label=_("Route mode"),
        choices=(
            (ROUTE_MODE_SINGLE, _("Single inbound")),
            (ROUTE_MODE_MULTI, _("Multi-inbound bundle")),
        ),
        initial=ROUTE_MODE_SINGLE,
        required=False,
        widget=forms.RadioSelect,
        help_text=_("Single برای پنل‌های معمولی فقط یک inbound را route می‌کند؛ Multi چند inbound از یک پنل modern multi-node می‌سازد."),
    )
    inbound = SalesInboundChoiceField(
        label=_("Destination inbound"),
        queryset=Inbound.objects.none(),
        required=False,
        help_text=_("فقط inboundهای فعال، قابل فروش، غیر legacy و متصل به پنل فعال نمایش داده می‌شوند."),
    )
    bundle_inbounds = forms.ModelMultipleChoiceField(
        label=_("Bundle inbounds"),
        queryset=Inbound.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(_("bundle inbounds"), is_stacked=False),
        help_text=_("برای حالت Multi حداقل دو inbound از همان پنل modern multi-node انتخاب کن."),
    )
    operator = forms.ModelChoiceField(
        label=_("Operator"),
        queryset=Operator.objects.none(),
        required=False,
        help_text=_("خالی یعنی route عمومی برای پلن ساخته یا به‌روزرسانی شود."),
    )
    plan_selection_mode = forms.ChoiceField(
        label=_("Plans"),
        choices=(
            (PLAN_SELECTION_ALL_ACTIVE, _("All active plans")),
            (PLAN_SELECTION_MANUAL, _("Selected plans")),
        ),
        initial=PLAN_SELECTION_ALL_ACTIVE,
        widget=forms.RadioSelect,
    )
    plans = forms.ModelMultipleChoiceField(
        label=_("Selected plans"),
        queryset=Plan.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(_("plans"), is_stacked=False),
        help_text=_("اگر انتخاب دستی فعال باشد، فقط همین پلن‌های فعال route می‌گیرند."),
    )
    priority = forms.IntegerField(
        label=_("Priority"),
        min_value=0,
        initial=100,
        help_text=_("عدد کمتر یعنی اولویت بالاتر."),
    )
    weight = forms.IntegerField(
        label=_("Weight"),
        min_value=1,
        initial=1,
    )
    existing_strategy = forms.ChoiceField(
        label=_("Existing routes"),
        choices=(
            (BULK_ROUTE_STRATEGY_UPDATE_EXISTING, _("Route موجود آپدیت شود، برای پلن بدون route ساخته شود")),
            (BULK_ROUTE_STRATEGY_SKIP_EXISTING, _("پلن‌هایی که route فعال دارند دست‌نخورده بمانند")),
            (BULK_ROUTE_STRATEGY_ADD_NEW, _("Route جدید اضافه شود و routeهای فعال قبلی بمانند")),
            (BULK_ROUTE_STRATEGY_REPLACE_ACTIVE, _("Routeهای فعال قبلی غیرفعال شوند و یک route مقصد فعال بماند")),
        ),
        initial=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        help_text=_("update_existing، skip_existing، add_new و replace_active همان رفتار قبلی سرویس bulk route هستند."),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        initial="Bulk assigned from admin",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store_queryset = Store.objects.filter(is_active=True).order_by("name", "pk")
        self.fields["store"].queryset = store_queryset

        store = self.selected_store()
        if not self.is_bound and not self.initial.get("store") and store:
            self.initial["store"] = store.pk

        sales_inbounds = get_valid_sales_inbounds(store)
        self.fields["inbound"].queryset = sales_inbounds
        self.fields["bundle_inbounds"].queryset = sales_inbounds
        self.fields["operator"].queryset = self.active_operators(store)
        self.fields["plans"].queryset = get_bulk_route_target_plans(store=store, all_active=True)

    def selected_store(self):
        raw_value = self.data.get(self.add_prefix("store")) if self.is_bound else self.initial.get("store")
        if isinstance(raw_value, Store):
            return raw_value
        if raw_value:
            try:
                return Store.objects.filter(is_active=True).get(pk=raw_value)
            except (Store.DoesNotExist, ValueError, TypeError):
                return None
        return Store.objects.filter(is_active=True).order_by("name", "pk").first()

    def active_operators(self, store):
        operators = Operator.objects.filter(is_active=True)
        if store:
            operators = operators.filter(Q(store=store) | Q(store__isnull=True))
        return operators.order_by("sort_order", "name", "pk")

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("plan_selection_mode") == self.PLAN_SELECTION_MANUAL and not cleaned_data.get("plans"):
            self.add_error("plans", _("برای انتخاب دستی، حداقل یک پلن فعال انتخاب کن."))
        route_mode = cleaned_data.get("route_mode") or self.ROUTE_MODE_SINGLE
        cleaned_data["route_mode"] = route_mode
        if route_mode == self.ROUTE_MODE_MULTI:
            bundle_inbounds = list(cleaned_data.get("bundle_inbounds") or [])
            if len(bundle_inbounds) < 2:
                self.add_error("bundle_inbounds", _("برای route چند اینباندی حداقل دو inbound انتخاب کن."))
            panel_ids = {inbound.panel_id for inbound in bundle_inbounds}
            if len(panel_ids) > 1:
                self.add_error("bundle_inbounds", _("همه inboundهای bundle باید از یک پنل باشند."))
            panel = bundle_inbounds[0].panel if bundle_inbounds else None
            if panel and panel.capability_profile != Panel.CapabilityProfile.MODERN_MULTI_NODE:
                self.add_error("bundle_inbounds", _("پنل bundle باید modern multi-node باشد."))
            if cleaned_data.get("existing_strategy") not in {
                BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
                BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
            }:
                self.add_error("existing_strategy", _("برای Multi فقط replace/update مجاز است."))
        elif not cleaned_data.get("inbound"):
            self.add_error("inbound", _("برای حالت Single یک inbound انتخاب کن."))
        return cleaned_data

    def service_kwargs(self):
        plan_selection_mode = self.cleaned_data["plan_selection_mode"]
        all_active = plan_selection_mode == self.PLAN_SELECTION_ALL_ACTIVE
        selected_plan_ids = []
        if not all_active:
            selected_plan_ids = [plan.pk for plan in self.cleaned_data["plans"]]

        route_mode = self.cleaned_data["route_mode"]
        return {
            "store": self.cleaned_data["store"],
            "inbound": self.cleaned_data["inbound"],
            "inbounds": list(self.cleaned_data.get("bundle_inbounds") or []),
            "multi_inbound_bundle": route_mode == self.ROUTE_MODE_MULTI,
            "operator": self.cleaned_data.get("operator"),
            "selected_plan_ids": selected_plan_ids,
            "all_active": all_active,
            "priority": self.cleaned_data["priority"],
            "weight": self.cleaned_data["weight"],
            "existing_strategy": self.cleaned_data["existing_strategy"],
            "note": self.cleaned_data.get("note") or "",
        }


class PlanInboundRouteInline(admin.TabularInline):
    model = PlanInboundRoute
    extra = 0
    fields = (
        "store",
        "operator",
        "inbound",
        "route_status",
        "is_active",
        "priority",
        "weight",
        "note",
    )
    readonly_fields = ("route_status",)
    autocomplete_fields = ("store", "operator", "inbound")
    show_change_link = True

    @admin.display(description=_("Route status"))
    def route_status(self, obj):
        if not obj or not obj.pk:
            return "-"
        if obj.is_active and obj.plan_id and not obj.plan.is_active:
            return format_html('<span class="badge bg-warning text-dark">{}</span>', _("پلن غیرفعال است"))
        inbound = getattr(obj, "inbound", None)
        panel = getattr(inbound, "panel", None) if inbound else None
        if not obj.is_active:
            return _("Inactive")
        if not inbound or not getattr(inbound, "is_active", False):
            return format_html('<span class="badge bg-danger">{}</span>', _("Inbound inactive"))
        if not getattr(inbound, "available_for_new_orders", True):
            return format_html('<span class="badge bg-danger">{}</span>', _("Legacy / unavailable"))
        if not panel or not panel.is_active:
            return format_html('<span class="badge bg-danger">{}</span>', _("Panel inactive"))
        return format_html('<span class="badge bg-success">{}</span>', _("Ready"))


class PlanRouteReadinessFilter(admin.SimpleListFilter):
    title = _("Route readiness")
    parameter_name = "catalog_route"

    def lookups(self, request, model_admin):
        return (
            ("ready", _("Ready")),
            ("missing", _("Missing explicit route")),
            ("invalid", _("Invalid route")),
            ("fallback", _("Using fallback")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        matching_ids = []
        for plan in queryset.select_related("store").prefetch_related("operators"):
            status = get_plan_route_status(plan, plan.store)
            if value == "ready" and status["is_ready"] and status["code"] != ROUTE_STATUS_FALLBACK:
                matching_ids.append(plan.pk)
            elif value == "missing" and status["code"] == ROUTE_STATUS_MISSING:
                matching_ids.append(plan.pk)
            elif value == "invalid" and status["is_invalid"]:
                matching_ids.append(plan.pk)
            elif value == "fallback" and status["code"] == ROUTE_STATUS_FALLBACK:
                matching_ids.append(plan.pk)
        return queryset.filter(pk__in=matching_ids)


class PlanInboundDeliveryFilter(admin.SimpleListFilter):
    title = _("تحویل اینباند")
    parameter_name = "inbound_delivery"

    def lookups(self, request, model_admin):
        return (
            ("has", _("دارای route فعال")),
            ("none", _("بدون route فعال")),
        )

    def queryset(self, request, queryset):
        if self.value() == "has":
            return queryset.filter(inbound_routes__is_active=True).distinct()
        if self.value() == "none":
            return queryset.exclude(inbound_routes__is_active=True).distinct()
        return queryset


class PlanCupDeliveryFilter(admin.SimpleListFilter):
    title = _("تحویل ساب")
    parameter_name = "cup_delivery"

    def lookups(self, request, model_admin):
        return (
            ("active", _("Recipe فعال")),
            ("none", _("بدون Recipe")),
            ("inactive", _("فقط Recipe غیرفعال")),
        )

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.filter(cup_fulfillment_recipes__is_active=True).distinct()
        if self.value() == "none":
            return queryset.filter(cup_fulfillment_recipes__isnull=True)
        if self.value() == "inactive":
            return queryset.filter(cup_fulfillment_recipes__isnull=False).exclude(cup_fulfillment_recipes__is_active=True).distinct()
        return queryset


class PlanCupReadinessFilter(admin.SimpleListFilter):
    title = _("وضعیت Recipe")
    parameter_name = "cup_readiness"

    def lookups(self, request, model_admin):
        return (
            (READY_STATUS, _("آماده")),
            (WARNING_STATUS, _("هشدار")),
            (ERROR_STATUS, _("خطا")),
            (INACTIVE_STATUS, _("غیرفعال")),
            (NOT_CONFIGURED_STATUS, _("ندارد")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        matching_ids = []
        for plan in queryset.select_related("store"):
            status = plan_fulfillment_status_for_plan(plan)
            if status["status_code"] == value:
                matching_ids.append(plan.pk)
        return queryset.filter(pk__in=matching_ids)


def _admin_badge(label, tone="secondary"):
    css = {
        "success": "bg-success",
        "ready": "bg-success",
        "warning": "bg-warning text-dark",
        "danger": "bg-danger",
        "error": "bg-danger",
        "info": "bg-info",
        "slate": "bg-secondary",
        "secondary": "bg-secondary",
    }.get(tone or "secondary", "bg-secondary")
    return format_html('<span class="badge {}">{}</span>', css, label)


def _plan_admin_active_route_count(plan):
    value = getattr(plan, "admin_active_route_count", None)
    if value is None:
        value = plan.inbound_routes.filter(is_active=True).count()
    return value


def _plan_admin_active_recipe_count(plan):
    value = getattr(plan, "admin_active_recipe_count", None)
    if value is None:
        value = plan.cup_fulfillment_recipes.filter(is_active=True).count()
    return value


def _plan_admin_recipe_count(plan):
    value = getattr(plan, "admin_recipe_count", None)
    if value is None:
        value = plan.cup_fulfillment_recipes.count()
    return value


def _plan_admin_route_status(plan):
    if not hasattr(plan, "_admin_route_status"):
        plan._admin_route_status = get_plan_route_status(plan, plan.store)
    return plan._admin_route_status


def _plan_admin_fulfillment_status(plan):
    if not hasattr(plan, "_admin_fulfillment_status"):
        plan._admin_fulfillment_status = plan_fulfillment_status_for_plan(plan)
    return plan._admin_fulfillment_status


def _delivery_row(label, value):
    return format_html('<div class="plan-delivery-row"><span>{}</span><strong>{}</strong></div>', label, value)


def _delivery_button(url, label, css_class="button"):
    return format_html('<a class="{}" href="{}">{}</a>', css_class, url, label)


def _disabled_delivery_button(label):
    return format_html('<span class="button plan-delivery-disabled">{}</span>', label)


def _recipe_source_summary(readiness):
    if not readiness:
        return _("۰ پنل + ۰ مخزن")
    return _("%(panels)s پنل + %(pools)s مخزن") % {
        "panels": readiness.get("panel_source_count", 0),
        "pools": readiness.get("inventory_pool_count", 0),
    }


def _plan_delivery_readiness(plan, fulfillment_status, route_status, active_route_count):
    if not getattr(plan, "is_active", False):
        return _("ناقص"), "danger"
    if fulfillment_status["recipe"]:
        if fulfillment_status["status_code"] == READY_STATUS:
            return _("آماده فروش"), "success"
        if fulfillment_status["status_code"] == WARNING_STATUS:
            return _("آماده با هشدار"), "warning"
        return _("ناقص"), "danger"
    if active_route_count:
        if route_status["is_invalid"] or route_status["code"] == ROUTE_STATUS_MISSING:
            return _("ناقص"), "danger"
        if route_status["code"] == ROUTE_STATUS_FALLBACK:
            return _("آماده با هشدار"), "warning"
        return _("آماده فروش"), "success"
    return _("ناقص"), "danger"


def _plan_effective_delivery_message(fulfillment_status, active_route_count):
    if fulfillment_status["recipe"]:
        return _("این پلن با ساب اختصاصی قاصدک تحویل داده می‌شود.")
    if active_route_count:
        return _("این پلن با مسیر قدیمی اینباند تحویل داده می‌شود.")
    return _("برای این پلن هنوز مسیر تحویل تنظیم نشده است.")


@admin.register(Plan)
class PlanAdmin(ImportExportModelAdmin):
    import_export_change_list_template = "admin/store/plan/change_list.html"
    change_form_template = "admin/store/plan/change_form.html"
    inlines = (PlanInboundRouteInline,)
    list_display = (
        "name",
        "store",
        "volume_gb",
        "duration_days",
        "price_with_currency",
        "active_public_status",
        "custom_volume_badge",
        "multi_inbound_bundle",
        "inbound_delivery_column",
        "cup_delivery_column",
        "delivery_status_column",
        "delivery_sources_column",
        "catalog_route_status",
        "catalog_inbound_destination",
        "routing_builder_link",
        "quick_review_link",
        "fulfillment_status_badge",
        "fulfillment_recipe_actions",
    )
    readonly_fields = ("service_delivery_summary",)
    fieldsets = (
        (
            _("Plan"),
            {
                "fields": (
                    "store",
                    "operators",
                    "name",
                    "slug",
                    "description",
                    "volume_gb",
                    "duration_days",
                    "price",
                    "currency",
                    "device_limit",
                )
            },
        ),
        (
            _("Sales"),
            {
                "fields": (
                    "is_active",
                    "is_public",
                    "is_custom_volume",
                    "sort_order",
                    "multi_inbound_bundle",
                )
            },
        ),
        (_("تحویل سرویس"), {"fields": ("service_delivery_summary",)}),
    )
    list_filter = (
        "store",
        "is_active",
        "is_public",
        "is_custom_volume",
        "multi_inbound_bundle",
        "duration_days",
        "volume_gb",
        PlanInboundDeliveryFilter,
        PlanCupDeliveryFilter,
        PlanCupReadinessFilter,
        PlanRouteReadinessFilter,
        ("operators", admin.RelatedOnlyFieldListFilter),
        "currency",
    )
    search_fields = ("name", "description", "operators__name", "operators__slug")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ()
    filter_horizontal = ("operators",)
    actions = [
        "bulk_assign_inbound_routes",
        "activate_selected_plans",
        "deactivate_selected_plans",
        "duplicate_selected_plans",
    ]

    @admin.display(description=_("Operators"))
    def operator_names(self, obj):
        names = [operator.name for operator in obj.operators.all()[:4]]
        if not names:
            return "-"
        total = obj.operators.count()
        suffix = f" +{total - len(names)}" if total > len(names) else ""
        return ", ".join(names) + suffix

    @admin.display(description=_("Active routes"), ordering="admin_active_route_count")
    def active_route_count(self, obj):
        value = getattr(obj, "admin_active_route_count", None)
        if value is None:
            value = obj.inbound_routes.filter(is_active=True).count()
        return value

    @admin.display(description=_("Routes"))
    def routes_link(self, obj):
        url = f"{reverse('admin:store_planinboundroute_changelist')}?{urlencode({'plan__id__exact': obj.pk})}"
        return format_html('<a class="button" href="{}">{}</a>', url, _("View routes"))

    @admin.display(description=_("Price"), ordering="price")
    def price_with_currency(self, obj):
        return f"{obj.price:,} {obj.currency}"

    @admin.display(description=_("Active / public"))
    def active_public_status(self, obj):
        active = format_html(
            '<span class="badge {}">{}</span>',
            "bg-success" if obj.is_active else "bg-secondary",
            _("Active") if obj.is_active else _("Inactive"),
        )
        public = format_html(
            '<span class="badge {}">{}</span>',
            "bg-success" if obj.is_public else "bg-secondary",
            _("Public") if obj.is_public else _("Private"),
        )
        return format_html("{} {}", active, public)

    @admin.display(description=_("Custom volume"), boolean=True, ordering="is_custom_volume")
    def custom_volume_badge(self, obj):
        return obj.is_custom_volume

    @admin.display(description=_("تحویل اینباند"), ordering="admin_active_route_count")
    def inbound_delivery_column(self, obj):
        route_count = _plan_admin_active_route_count(obj)
        route_status = _plan_admin_route_status(obj)
        if route_count:
            return format_html(
                '{} <small style="display:block; margin-top:2px;">{}</small>',
                _("اینباند: %(count)s مسیر") % {"count": route_count},
                route_status["label"],
            )
        return format_html('<span class="text-muted">{}</span>', _("اینباند: ندارد"))

    @admin.display(description=_("تحویل ساب"), ordering="admin_active_recipe_count")
    def cup_delivery_column(self, obj):
        status = _plan_admin_fulfillment_status(obj)
        if status["recipe"]:
            return _admin_badge(_("Recipe فعال"), "success")
        if _plan_admin_recipe_count(obj):
            return _admin_badge(_("Recipe غیرفعال"), "slate")
        return _admin_badge(_("ندارد"), "slate")

    @admin.display(description=_("وضعیت تحویل"))
    def delivery_status_column(self, obj):
        fulfillment_status = _plan_admin_fulfillment_status(obj)
        route_status = _plan_admin_route_status(obj)
        label, tone = _plan_delivery_readiness(obj, fulfillment_status, route_status, _plan_admin_active_route_count(obj))
        return _admin_badge(label, tone)

    @admin.display(description=_("منابع تحویل"))
    def delivery_sources_column(self, obj):
        fulfillment_status = _plan_admin_fulfillment_status(obj)
        readiness = fulfillment_status.get("readiness")
        if fulfillment_status["recipe"]:
            return _recipe_source_summary(readiness)
        route_count = _plan_admin_active_route_count(obj)
        if route_count:
            return _("مسیر اینباند: %(count)s") % {"count": route_count}
        return "-"

    @admin.display(description=_("Route status"))
    def catalog_route_status(self, obj):
        status = get_plan_route_status(obj, obj.store)
        css = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "info": "bg-info",
        }.get(status["tone"], "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css, status["label"])

    @admin.display(description=_("Inbound"))
    def catalog_inbound_destination(self, obj):
        status = get_plan_route_status(obj, obj.store)
        return status["destination"] or "-"

    @admin.display(description=_("Review"))
    def quick_review_link(self, obj):
        return format_html('<a class="button" href="{}">{}</a>', catalog_plan_review_url(obj), _("Review"))

    @admin.display(description=_("Routing builder"))
    def routing_builder_link(self, obj):
        url = reverse("admin_store_panel_center_routing_detail", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("تنظیم مسیر"))

    @admin.display(description=_("تحویل خودکار ساب"))
    def fulfillment_status_badge(self, obj):
        status = _plan_admin_fulfillment_status(obj)
        css = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "slate": "bg-secondary",
        }.get(status["status_tone"], "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css, status["status_label"])

    @admin.display(description=_("Cup fulfillment"))
    def fulfillment_recipe_actions(self, obj):
        setup_url = f"{catalog_plan_edit_url(obj)}#delivery"
        status = _plan_admin_fulfillment_status(obj)
        recipe = status["recipe"]
        setup_button = format_html(
            '<a class="button" href="{}">{}</a>',
            setup_url,
            _("تنظیم تحویل"),
        )
        if recipe:
            preview_url = reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk])
            simulate_url = reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk])
            preview_button = format_html(
                ' <a class="button" href="{}">{}</a>',
                preview_url,
                _("پیش‌نمایش دستور تحویل"),
            )
            simulate_button = format_html(
                ' <a class="button" href="{}">{}</a>',
                simulate_url,
                _("تست شبیه‌سازی"),
            )
            return format_html("{}{}{}", setup_button, preview_button, simulate_button)
        add_url = f"{reverse('admin:store_cupfulfillmentrecipe_add')}?{urlencode({'plan': obj.pk})}"
        create_button = format_html(
            ' <a class="button" href="{}">{}</a>',
            add_url,
            _("ساخت Recipe جدید"),
        )
        return format_html("{}{}", setup_button, create_button)

    @admin.display(description=_("تحویل سرویس"))
    def service_delivery_summary(self, obj):
        if not obj or not obj.pk:
            return format_html(
                '<div class="plan-service-delivery plan-service-delivery-helper" dir="rtl">'
                '<div class="plan-delivery-heading"><h3>{}</h3></div>'
                '<p>{}</p>'
                '<ul><li>{}</li><li>{}</li></ul>'
                '<p class="plan-delivery-note">{}</p>'
                "</div>",
                _("مرحله بعد از ساخت پلن"),
                _("بعد از ذخیره پلن، می‌توانید تحویل سرویس را تنظیم کنید:"),
                _("اتصال به اینباندها"),
                _("یا ساخت Recipe برای تحویل ساب اختصاصی"),
                _("برای رفتن مستقیم به تنظیم تحویل، از دکمه «ذخیره و تنظیم تحویل» پایین فرم استفاده کنید."),
            )

        route_count = _plan_admin_active_route_count(obj)
        route_status = _plan_admin_route_status(obj)
        fulfillment_status = _plan_admin_fulfillment_status(obj)
        recipe = fulfillment_status["recipe"]
        readiness = fulfillment_status.get("readiness") or {}
        setup_url = f"{catalog_plan_edit_url(obj)}#delivery"
        route_builder_url = reverse("admin_store_panel_center_routing_detail", args=[obj.pk])
        route_list_url = f"{reverse('admin:store_planinboundroute_changelist')}?{urlencode({'plan__id__exact': obj.pk})}"
        readiness_label, readiness_tone = _plan_delivery_readiness(obj, fulfillment_status, route_status, route_count)
        effective_message = _plan_effective_delivery_message(fulfillment_status, route_count)

        inbound_rows = format_html(
            "{}{}{}{}",
            _delivery_row(_("تعداد routeهای فعال"), route_count),
            _delivery_row(_("وضعیت چنداینباندی"), _admin_badge(_("فعال") if obj.multi_inbound_bundle else _("غیرفعال"), "success" if obj.multi_inbound_bundle else "slate")),
            _delivery_row(_("آمادگی مسیر"), _admin_badge(route_status["label"], route_status["tone"])),
            _delivery_row(_("مقصد فعلی"), route_status.get("destination") or "-"),
        )
        inbound_actions = format_html(
            "{}{}{}",
            _delivery_button(route_builder_url, _("مدیریت اینباندهای پلن")),
            _delivery_button(route_builder_url, _("تست مسیر اینباندها")),
            _delivery_button(route_list_url, _("مشاهده routeها")),
        )

        if recipe:
            recipe_status_label = _("آماده") if fulfillment_status["status_code"] == READY_STATUS else (
                _("هشدار") if fulfillment_status["status_code"] == WARNING_STATUS else _("خطا")
            )
            recipe_status = _admin_badge(recipe_status_label, fulfillment_status["status_tone"])
            recipe_label = recipe.title
            rule_count = int(readiness.get("required_rule_count", 0) or 0) + int(readiness.get("optional_rule_count", 0) or 0)
            expected_config_count = readiness.get("expected_config_count", 0)
            source_summary = _recipe_source_summary(readiness)
            recipe_url = reverse("admin_store_plan_fulfillment_recipe", args=[recipe.pk])
            preview_url = reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk])
            simulate_url = reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk])
            recipe_actions = format_html(
                "{}{}{}{}",
                _delivery_button(setup_url, _("ویرایش تحویل در Plan editor")),
                _delivery_button(preview_url, _("پیش‌نمایش آمادگی")),
                _delivery_button(simulate_url, _("تست شبیه‌سازی")),
                _delivery_button(recipe_url, _("مشاهده Recipe")),
            )
        else:
            recipe_status = _admin_badge(_("غیرفعال") if _plan_admin_recipe_count(obj) else _("ندارد"), "slate")
            recipe_label = _("برای این پلن هنوز دستور تحویل ساب تعریف نشده است.")
            rule_count = 0
            expected_config_count = 0
            source_summary = _("۰ پنل + ۰ مخزن")
            recipe_actions = format_html(
                "{}{}{}{}",
                _delivery_button(setup_url, _("ویرایش تحویل در Plan editor")),
                _disabled_delivery_button(_("پیش‌نمایش آمادگی")),
                _disabled_delivery_button(_("تست شبیه‌سازی")),
                _disabled_delivery_button(_("مشاهده Recipe")),
            )

        inactive_recipes = []
        if not recipe or _plan_admin_recipe_count(obj) > 1:
            inactive_recipes = list(obj.cup_fulfillment_recipes.filter(is_active=False).order_by("priority", "pk")[:5])
        inactive_recipes_html = ""
        if inactive_recipes:
            inactive_links = format_html_join(
                "، ",
                '<a href="{}">{}</a>',
                ((reverse("admin:store_cupfulfillmentrecipe_change", args=[item.pk]), item.title) for item in inactive_recipes),
            )
            inactive_recipes_html = format_html(
                '<details class="plan-delivery-details"><summary>{}</summary><div>{}</div></details>',
                _("Recipeهای غیرفعال"),
                inactive_links,
            )

        cup_rows = format_html(
            "{}{}{}{}{}{}",
            _delivery_row(_("Recipe فعال"), recipe_label),
            _delivery_row(_("وضعیت Recipe"), recipe_status),
            _delivery_row(_("تعداد قانون‌ها"), rule_count),
            _delivery_row(_("کانفیگ مورد انتظار"), expected_config_count),
            _delivery_row(_("منابع پنل"), readiness.get("panel_source_count", 0) if readiness else 0),
            _delivery_row(_("منابع مخزن"), readiness.get("inventory_pool_count", 0) if readiness else 0),
        )

        return format_html(
            '<div class="plan-service-delivery" dir="rtl">'
            '<div class="plan-delivery-summary">'
            '<div><h3>{}</h3><p>{}</p></div>'
            '<div class="plan-delivery-status">{}</div>'
            "</div>"
            '<div class="plan-delivery-cards">'
            '<section class="plan-delivery-card">'
            '<div class="plan-delivery-card-head"><span>{}</span><h4>{}</h4></div>'
            "<p>{}</p>"
            '<div class="plan-delivery-list">{}</div>'
            '<div class="plan-delivery-actions">{}</div>'
            "</section>"
            '<section class="plan-delivery-card">'
            '<div class="plan-delivery-card-head"><span>{}</span><h4>{}</h4></div>'
            "<p>{}</p>"
            '<div class="plan-delivery-list">{}</div>'
            '<div class="plan-delivery-source-summary">{}</div>'
            '<div class="plan-delivery-actions">{}</div>'
            "{}"
            "</section>"
            "</div>"
            "</div>",
            _("تحویل سرویس"),
            effective_message,
            _admin_badge(readiness_label, readiness_tone),
            "A",
            _("اتصال مستقیم به اینباندها"),
            _("روش قدیمی/مستقیم برای ساخت کانفیگ از اینباندهای متصل به پلن."),
            inbound_rows,
            inbound_actions,
            "B",
            _("تحویل با ساب اختصاصی قاصدک"),
            _("بعد از خرید، برای مشتری یک Cup ساخته می‌شود و لینک /sub اختصاصی تحویل داده می‌شود."),
            cup_rows,
            _("منابع تحویل: %(sources)s") % {"sources": source_summary},
            recipe_actions,
            inactive_recipes_html,
        )

    @admin.display(description=_("خلاصه تحویل خودکار"))
    def fulfillment_recipe_summary(self, obj):
        if obj and obj.pk:
            return self.service_delivery_summary(obj)
        if not obj or not obj.pk:
            return "-"
        status = plan_fulfillment_status_for_plan(obj)
        setup_url = f"{catalog_plan_edit_url(obj)}#delivery"
        if not status["recipe"]:
            return format_html(
                '<div><span class="badge bg-secondary">{}</span></div><p><a class="button" href="{}">{}</a></p>',
                _("بدون تحویل خودکار"),
                setup_url,
                _("تنظیم تحویل"),
            )
        recipe = status["recipe"]
        readiness = status["readiness"] or {}
        preview_url = reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk])
        simulate_url = reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk])
        return format_html(
            '<div><span class="badge bg-info">{}</span> <strong>{}</strong></div>'
            '<div>{}: {} · {}: {} · {}: {}</div>'
            '<p><a class="button" href="{}">{}</a> <a class="button" href="{}">{}</a> <a class="button" href="{}">{}</a></p>',
            status["status_label"],
            recipe.title,
            _("منبع پنل"),
            readiness.get("panel_source_count", 0),
            _("مخزن"),
            readiness.get("inventory_pool_count", 0),
            _("کانفیگ مورد انتظار"),
            readiness.get("expected_config_count", 0),
            setup_url,
            _("تنظیم تحویل"),
            preview_url,
            _("پیش‌نمایش دستور تحویل"),
            simulate_url,
            _("تست شبیه‌سازی"),
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("operators")
            .annotate(
                admin_active_route_count=Count("inbound_routes", filter=Q(inbound_routes__is_active=True), distinct=True),
                admin_recipe_count=Count("cup_fulfillment_recipes", distinct=True),
                admin_active_recipe_count=Count(
                    "cup_fulfillment_recipes",
                    filter=Q(cup_fulfillment_recipes__is_active=True),
                    distinct=True,
                ),
            )
        )

    def response_add(self, request, obj, post_url_continue=None):
        if "_save_and_configure_fulfillment" in request.POST:
            self.message_user(request, _("پلن ذخیره شد. حالا تحویل سرویس را تنظیم کنید."), messages.SUCCESS)
            return HttpResponseRedirect(f"{catalog_plan_edit_url(obj)}#delivery")
        return super().response_add(request, obj, post_url_continue)

    def changelist_view(self, request, extra_context=None):
        bulk_price_form = PlanBulkPriceForm()

        if request.method == "POST" and "_apply_price_per_gb" in request.POST:
            if not self.has_change_permission(request):
                raise PermissionDenied

            bulk_price_form = PlanBulkPriceForm(request.POST)
            if bulk_price_form.is_valid():
                price_per_gb = bulk_price_form.cleaned_data["price_per_gb"]
                updated_count, total_count = self.apply_price_per_gb(price_per_gb)
                messages.success(
                    request,
                    _(
                        "قیمت هر گیگ %(price_per_gb)s اعمال شد؛ %(updated_count)s پلن از %(total_count)s پلن به‌روزرسانی شد."
                    )
                    % {
                        "price_per_gb": price_per_gb,
                        "updated_count": updated_count,
                        "total_count": total_count,
                    },
                )
                return HttpResponseRedirect(request.get_full_path())
            messages.error(request, _("قیمت هر گیگ را درست وارد کن."))
            return HttpResponseRedirect(request.get_full_path())

        context = {
            **(extra_context or {}),
            "bulk_price_per_gb_form": bulk_price_form,
            "bulk_route_assign_url": reverse("admin:store_planinboundroute_bulk_assign"),
            "catalog_url": catalog_url(),
        }
        return super().changelist_view(request, context)

    def apply_price_per_gb(self, price_per_gb):
        now = timezone.now()
        plans = list(self.model.objects.filter(is_custom_volume=False).only("id", "price", "volume_gb"))
        plans_to_update = []

        for plan in plans:
            new_price = calculate_plan_price_from_per_gb(plan.volume_gb, price_per_gb)
            if plan.price == new_price:
                continue
            plan.price = new_price
            plan.updated_at = now
            plans_to_update.append(plan)

        if plans_to_update:
            with transaction.atomic():
                self.model.objects.bulk_update(plans_to_update, ["price", "updated_at"])

        Store.objects.update(custom_volume_price_per_gb=price_per_gb, updated_at=now)

        return len(plans_to_update), len(plans)

    @admin.action(description=_("Set sales inbound for selected plans"))
    def bulk_assign_inbound_routes(self, request, queryset):
        selected_plan_ids = ",".join(str(plan_id) for plan_id in queryset.values_list("pk", flat=True))
        params = urlencode(
            {
                "plan_selection_mode": BulkPlanInboundRouteForm.PLAN_SELECTION_MANUAL,
                "plan_ids": selected_plan_ids,
            }
        )
        return HttpResponseRedirect(f"{reverse('admin:store_planinboundroute_bulk_assign')}?{params}")

    @admin.action(description=_("Activate selected plans when sales route is ready"))
    def activate_selected_plans(self, request, queryset):
        activated = 0
        blocked = 0
        for plan in queryset.select_related("store"):
            try:
                if set_plan_active_state(plan, True, actor=request.user):
                    activated += 1
            except ValidationError as exc:
                blocked += 1
                messages.error(request, _("%(plan)s فعال نشد: %(error)s") % {"plan": plan.name, "error": "; ".join(exc.messages)})
        if activated:
            messages.success(request, _("%(count)s plan(s) activated.") % {"count": activated})
        if blocked:
            messages.warning(request, _("%(count)s plan(s) blocked by route readiness.") % {"count": blocked})

    @admin.action(description=_("Deactivate selected plans"))
    def deactivate_selected_plans(self, request, queryset):
        updated = queryset.filter(is_active=True).update(is_active=False, updated_at=timezone.now())
        messages.success(request, _("%(count)s plan(s) deactivated.") % {"count": updated})

    @admin.action(description=_("Duplicate selected plans as inactive without active routes"))
    def duplicate_selected_plans(self, request, queryset):
        created = 0
        for plan in queryset:
            duplicate_plan_for_admin(plan, actor=request.user)
            created += 1
        messages.success(request, _("%(count)s inactive plan duplicate(s) created without active routes.") % {"count": created})


@admin.register(PlanInboundRoute)
class PlanInboundRouteAdmin(ImportExportModelAdmin):
    import_export_change_list_template = "admin/store/planinboundroute/change_list.html"
    list_display = (
        "plan",
        "plan_review_link",
        "store",
        "operator",
        "inbound",
        "panel",
        "is_active",
        "priority",
        "weight",
        "inbound_available_for_new_orders",
        "inbound_health_monitor_enabled",
        "route_health",
        "created_at",
    )
    list_filter = (
        "is_active",
        "store",
        "plan",
        "operator",
        "inbound__panel",
        "inbound__available_for_new_orders",
    )
    search_fields = (
        "plan__name",
        "plan__slug",
        "operator__name",
        "operator__slug",
        "inbound__remark",
        "=inbound__inbound_id",
        "inbound__panel__name",
        "inbound__panel__url",
        "store__name",
        "store__english_name",
    )
    date_hierarchy = "created_at"
    list_select_related = ("store", "plan", "operator", "inbound", "inbound__panel")
    autocomplete_fields = ("store", "plan", "operator", "inbound")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "bulk-assign/",
                self.admin_site.admin_view(self.bulk_assign_view),
                name="store_planinboundroute_bulk_assign",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        context = {
            **(extra_context or {}),
            "bulk_route_assign_url": reverse("admin:store_planinboundroute_bulk_assign"),
            "catalog_url": catalog_url(),
        }
        return super().changelist_view(request, context)

    def bulk_assign_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied

        preview = None
        if request.method == "POST":
            form = BulkPlanInboundRouteForm(request.POST)
            if form.is_valid():
                kwargs = form.service_kwargs()
                if "_apply" in request.POST:
                    try:
                        result = apply_bulk_plan_routes(**kwargs)
                    except ValidationError as exc:
                        messages.error(
                            request,
                            _("Routeها اعمال نشدند؛ خطا باعث rollback کامل شد: %(error)s")
                            % {"error": self.format_validation_error(exc)},
                        )
                    else:
                        if result["errors"]:
                            preview = result
                            messages.error(request, _("قبل از اعمال، خطاهای preview را برطرف کن."))
                        else:
                            messages.success(request, self.bulk_apply_success_message(result))
                            for warning in result["warnings"][:5]:
                                messages.warning(request, warning)
                            return HttpResponseRedirect(reverse("admin:store_planinboundroute_changelist"))
                else:
                    preview = preview_bulk_plan_routes(**kwargs)
                    if preview["errors"]:
                        messages.error(request, _("Preview خطا دارد؛ routeها هنوز اعمال نشده‌اند."))
                    elif preview["warnings"]:
                        messages.warning(request, _("Preview با هشدار آماده است؛ قبل از تایید آن‌ها را بررسی کن."))
            else:
                messages.error(request, _("فرم تنظیم گروهی routeها معتبر نیست."))
        else:
            form = BulkPlanInboundRouteForm(initial=self.bulk_assign_initial(request))

        context = {
            **self.admin_site.each_context(request),
            "title": _("Bulk assign inbound routes"),
            "subtitle": _("تنظیم گروهی مسیر فروش پلن‌ها"),
            "opts": self.model._meta,
            "form": form,
            "preview": preview,
            "media": self.media + form.media,
            "changelist_url": reverse("admin:store_planinboundroute_changelist"),
        }
        return TemplateResponse(request, "admin/store/planinboundroute/bulk_assign.html", context)

    def bulk_assign_initial(self, request):
        initial = {
            "route_mode": BulkPlanInboundRouteForm.ROUTE_MODE_SINGLE,
            "plan_selection_mode": BulkPlanInboundRouteForm.PLAN_SELECTION_ALL_ACTIVE,
            "priority": 100,
            "weight": 1,
            "existing_strategy": BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
            "note": "Bulk assigned from admin",
        }
        plan_ids = normalize_plan_ids(request.GET.get("plan_ids"))
        store = None
        store_id = request.GET.get("store")
        if store_id:
            try:
                store = Store.objects.filter(is_active=True).get(pk=store_id)
            except (Store.DoesNotExist, ValueError, TypeError):
                store = None

        if not store and plan_ids:
            plan_store_ids = list(
                Plan.objects.filter(pk__in=plan_ids, is_active=True, store__isnull=False)
                .values_list("store_id", flat=True)
                .distinct()
            )
            if len(plan_store_ids) == 1:
                store = Store.objects.filter(is_active=True, pk=plan_store_ids[0]).first()

        if not store:
            store = Store.objects.filter(is_active=True).order_by("name", "pk").first()
        if store:
            initial["store"] = store.pk

        if plan_ids:
            valid_plan_ids = list(
                get_bulk_route_target_plans(store=store, selected_plan_ids=plan_ids).values_list("pk", flat=True)
            )
            initial["plan_selection_mode"] = BulkPlanInboundRouteForm.PLAN_SELECTION_MANUAL
            initial["plans"] = valid_plan_ids

        return initial

    def bulk_apply_success_message(self, result):
        return (
            _("Routeها با موفقیت اعمال شدند: %(created)s ساخته شد، %(updated)s آپدیت شد، %(skipped)s skip شد، %(deactivated)s غیرفعال شد.")
            % {
                "created": result["created"],
                "updated": result["updated"],
                "skipped": result["skipped"],
                "deactivated": result["deactivated"],
            }
        )

    def format_validation_error(self, exc):
        if hasattr(exc, "message_dict"):
            parts = []
            for field, field_messages in exc.message_dict.items():
                parts.append(f"{field}: {', '.join(str(message) for message in field_messages)}")
            return "; ".join(parts)
        return "; ".join(str(message) for message in getattr(exc, "messages", [str(exc)]))

    @admin.display(description=_("Panel"), ordering="inbound__panel__name")
    def panel(self, obj):
        return obj.inbound.panel if obj.inbound_id else "-"

    @admin.display(description=_("Plan review"))
    def plan_review_link(self, obj):
        if not obj.plan_id:
            return "-"
        return format_html('<a class="button" href="{}">{}</a>', catalog_plan_review_url(obj.plan), _("Review"))

    @admin.display(description=_("Available for new orders"), boolean=True, ordering="inbound__available_for_new_orders")
    def inbound_available_for_new_orders(self, obj):
        return bool(obj.inbound_id and obj.inbound.available_for_new_orders)

    @admin.display(description=_("Health monitor"), boolean=True, ordering="inbound__health_monitor_enabled")
    def inbound_health_monitor_enabled(self, obj):
        return bool(obj.inbound_id and obj.inbound.health_monitor_enabled)

    @admin.display(description=_("Route health"))
    def route_health(self, obj):
        if obj.is_active and obj.plan_id and not obj.plan.is_active:
            return format_html('<span class="badge bg-warning text-dark">{}</span>', _("پلن غیرفعال است"))
        inbound = obj.inbound if obj.inbound_id else None
        panel = inbound.panel if inbound else None
        if not obj.is_active:
            return _("Inactive")
        if not inbound or not inbound.is_active:
            return format_html('<span class="badge bg-danger">{}</span>', _("Inbound inactive"))
        if not inbound.available_for_new_orders:
            return format_html('<span class="badge bg-danger">{}</span>', _("Legacy / unavailable"))
        if not panel or not panel.is_active:
            return format_html('<span class="badge bg-danger">{}</span>', _("Panel inactive"))
        return format_html('<span class="badge bg-success">{}</span>', _("Ready"))


class PanelAdminForm(forms.ModelForm):
    password = forms.CharField(
        label=_("password"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("برای تغییر password مقدار جدید را وارد کنید. مقدار فعلی کامل نمایش داده نمی‌شود."),
    )
    proxy_url = forms.URLField(
        label=_("HTTP proxy URL"),
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("Proxy URL کامل نمایش داده نمی‌شود. برای تغییر مقدار جدید را وارد کنید."),
    )
    clear_proxy_url = forms.BooleanField(
        label=_("Clear proxy URL"),
        required=False,
        help_text=_("اگر فعال شود proxy ذخیره‌شده حذف می‌شود."),
    )

    class Meta:
        model = Panel
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        password = (cleaned_data.get("password") or "").strip()
        if password:
            cleaned_data["password"] = password
        elif self.instance and self.instance.pk:
            cleaned_data["password"] = self.instance.password
        else:
            self.add_error("password", _("Password برای Panel جدید لازم است."))

        proxy_url = (cleaned_data.get("proxy_url") or "").strip()
        if cleaned_data.get("clear_proxy_url"):
            cleaned_data["proxy_url"] = ""
        elif proxy_url:
            cleaned_data["proxy_url"] = proxy_url
        elif self.instance and self.instance.pk:
            cleaned_data["proxy_url"] = self.instance.proxy_url
        return cleaned_data


@admin.register(Panel)
class PanelAdmin(ImportExportModelAdmin):
    form = PanelAdminForm
    list_display = (
        "name",
        "store",
        "masked_url",
        "is_active",
        "panel_health_status",
        "panel_alert_status",
        "compatibility_profile_status",
        "credential_status",
        "uses_proxy",
        "panel_center_link",
        "inbounds_link",
        "last_sync_at",
    )
    list_filter = ("store", "family", "is_active", "health_alert_enabled", "capability_profile")
    search_fields = ("name", "url", "username", "proxy_url")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    readonly_fields = (
        "masked_url",
        "credential_status",
        "proxy_status",
        "panel_center_link",
        "latest_health_details",
        "health_alert_details",
        "health_alert_actions",
        "inbounds_link",
        "plan_fulfillment_links",
        "compatibility_metadata_summary",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Connection"),
            {
                "fields": (
                    "name",
                    "store",
                    "family",
                    "url",
                    "username",
                    "password",
                    "credential_status",
                    "is_active",
                )
            },
        ),
        (
            _("Proxy"),
            {
                "classes": ("collapse",),
                "fields": ("proxy_status", "proxy_url", "clear_proxy_url"),
            },
        ),
        (
            _("Operations"),
            {
                "fields": ("panel_center_link", "inbounds_link", "plan_fulfillment_links", "latest_health_details", "last_sync_at", "created_at", "updated_at"),
            },
        ),
        (
            _("هشدار سلامت این پنل"),
            {
                "fields": (
                    "health_alert_enabled",
                    "alert_repeat_interval_minutes",
                    "failure_threshold_count",
                    "health_alert_details",
                    "health_alert_actions",
                ),
                "description": _("این تنظیمات فقط وقتی استفاده می‌شوند که هشدار سلامت پنل‌ها در Store فعال شده باشد."),
            },
        ),
        (
            _("X-UI compatibility"),
            {
                "fields": (
                    "detected_xui_version",
                    "capability_profile",
                    "last_capability_check_at",
                    "compatibility_metadata_summary",
                ),
            },
        ),
    )
    actions = ("run_health_check",)

    def get_fieldsets(self, request, obj=None):
        if self.has_change_permission(request, obj):
            return super().get_fieldsets(request, obj)
        return (
            (
                _("Connection"),
                {
                    "fields": (
                        "name",
                        "store",
                        "masked_url",
                        "credential_status",
                        "is_active",
                    )
                },
            ),
            (
                _("Proxy"),
                {
                    "classes": ("collapse",),
                    "fields": ("proxy_status",),
                },
            ),
            (
                _("Operations"),
                {
                    "fields": (
                        "inbounds_link",
                        "plan_fulfillment_links",
                        "latest_health_details",
                        "health_alert_details",
                        "health_alert_actions",
                        "last_sync_at",
                        "created_at",
                        "updated_at",
                    ),
                },
            ),
        )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("health_status").annotate(admin_inbound_count=Count("inbounds"))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/health-alerts/enable/",
                self.admin_site.admin_view(self.enable_health_alert),
                name="store_panel_health_alert_enable",
            ),
            path(
                "<path:object_id>/health-alerts/disable/",
                self.admin_site.admin_view(self.disable_health_alert),
                name="store_panel_health_alert_disable",
            ),
            path(
                "<path:object_id>/health-alerts/test/",
                self.admin_site.admin_view(self.send_health_alert_test_message),
                name="store_panel_health_alert_test",
            ),
            path(
                "<path:object_id>/health-alerts/dry-run/",
                self.admin_site.admin_view(self.dry_run_health_alert),
                name="store_panel_health_alert_dry_run",
            ),
        ]
        return custom_urls + urls

    def _panel_for_alert_action(self, request, object_id):
        panel = self.get_object(request, object_id)
        if panel is None:
            raise Http404("Panel not found")
        if not request.user.is_staff or not self.has_change_permission(request, panel):
            raise PermissionDenied
        return panel

    def _redirect_to_panel(self, panel):
        return HttpResponseRedirect(reverse("admin:store_panel_change", args=[panel.pk]))

    def enable_health_alert(self, request, object_id):
        panel = self._panel_for_alert_action(request, object_id)
        panel.health_alert_enabled = True
        panel.save(update_fields=["health_alert_enabled", "updated_at"])
        messages.success(request, _("هشدار سلامت برای این پنل فعال شد."))
        return self._redirect_to_panel(panel)

    def disable_health_alert(self, request, object_id):
        panel = self._panel_for_alert_action(request, object_id)
        panel.health_alert_enabled = False
        panel.save(update_fields=["health_alert_enabled", "updated_at"])
        messages.success(request, _("هشدار سلامت برای این پنل غیرفعال شد."))
        return self._redirect_to_panel(panel)

    def send_health_alert_test_message(self, request, object_id):
        panel = self._panel_for_alert_action(request, object_id)
        if request.method != "POST":
            context = {
                **self.admin_site.each_context(request),
                "title": _("تأیید ارسال پیام تستی هشدار سلامت"),
                "panel": panel,
                "opts": self.model._meta,
                "back_url": reverse("admin:store_panel_change", args=[panel.pk]),
            }
            return TemplateResponse(request, "admin/store/panel/confirm_health_alert_test.html", context)
        if request.POST.get("confirm") != "yes":
            messages.warning(request, _("ارسال پیام تستی تأیید نشد."))
            return self._redirect_to_panel(panel)
        result = PanelHealthAlertService().send_test_message(panel)
        if result.get("alert_sent_count"):
            messages.success(
                request,
                _("پیام تست هشدار سلامت پنل برای %(count)s ادمین ارسال شد.")
                % {"count": result.get("alert_sent_count", 0)},
            )
        else:
            messages.warning(
                request,
                _("پیام تست ارسال نشد؛ BotConfiguration تلگرام و گیرنده‌های ادمین را بررسی کنید."),
            )
        return self._redirect_to_panel(panel)

    def dry_run_health_alert(self, request, object_id):
        panel = self._panel_for_alert_action(request, object_id)
        from .panel_health_services import check_panel_health

        result = check_panel_health(panel, send_alerts=True, dry_run=True)
        messages.info(
            request,
            _(
                "Dry-run هشدار سلامت انجام شد: status=%(status)s would_send=%(would_send)s skip_reason=%(reason)s alerts_sent=0"
            )
            % {
                "status": result.get("status") or "-",
                "would_send": bool(result.get("would_send_alert")),
                "reason": result.get("alert_skip_reason") or "-",
            },
        )
        return self._redirect_to_panel(panel)

    @admin.display(description=_("URL"), ordering="url")
    def masked_url(self, obj):
        return mask_url_credentials(obj.url)

    @admin.display(description=_("Inbounds"), ordering="admin_inbound_count")
    def inbound_count(self, obj):
        return getattr(obj, "admin_inbound_count", None) if getattr(obj, "admin_inbound_count", None) is not None else obj.inbounds.count()

    @admin.display(description=_("Inbounds"))
    def inbounds_link(self, obj):
        if not obj or not obj.pk:
            return _("Save first to view inbounds.")
        url = f"{reverse('admin:store_inbound_changelist')}?{urlencode({'panel__id__exact': obj.pk})}"
        label = _("%(count)s inbound(s)") % {"count": self.inbound_count(obj)}
        return format_html('<a class="button" href="{}">{}</a>', url, label)

    @admin.display(description=_("Panel Center"))
    def panel_center_link(self, obj):
        if not obj or not obj.pk:
            return _("Save first to open Panel Center.")
        url = reverse("admin_store_panel_center_detail", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("مرکز اتصال پنل"))

    @admin.display(description=_("تحویل خودکار پلن"))
    def plan_fulfillment_links(self, obj):
        if not obj or not obj.pk:
            return "-"
        add_url = f"{reverse('admin:store_cupfillerrule_add')}?{urlencode({'source_type': CupFillerRule.SourceType.PANEL_INBOUNDS, 'panel': obj.pk})}"
        rules_url = f"{reverse('admin:store_cupfillerrule_changelist')}?{urlencode({'panel__id__exact': obj.pk})}"
        readiness_url = reverse("admin_store_panel_center_test", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a>',
            add_url,
            _("استفاده در Recipe پلن"),
            rules_url,
            _("مشاهده پلن‌های متصل"),
            readiness_url,
            _("تست readiness"),
        )

    @admin.display(description=_("Proxy"), boolean=True)
    def uses_proxy(self, obj):
        return bool(obj.proxy_url)

    @admin.display(description=_("Credentials"))
    def credential_status(self, obj):
        if obj and obj.username and obj.password:
            return format_html('<span class="badge bg-success">{}</span>', _("Configured"))
        return format_html('<span class="badge bg-warning text-dark">{}</span>', _("Incomplete"))

    @admin.display(description=_("Proxy status"))
    def proxy_status(self, obj):
        if obj and obj.proxy_url:
            return format_html("{} {}", format_html('<span class="badge bg-success">{}</span>', _("Configured")), mask_url_credentials(obj.proxy_url))
        return format_html('<span class="badge bg-secondary">{}</span>', _("Not configured"))

    @admin.display(description=_("Health"))
    def panel_health_status(self, obj):
        try:
            health_status = obj.health_status
        except PanelHealthStatus.DoesNotExist:
            return format_html('<span class="badge bg-secondary">{}</span>', _("No check"))
        tone = {
            PanelHealthStatus.Status.OK: "bg-success",
            PanelHealthStatus.Status.WARNING: "bg-warning text-dark",
            PanelHealthStatus.Status.ERROR: "bg-danger",
            PanelHealthStatus.Status.DISABLED: "bg-secondary",
        }.get(health_status.status, "bg-secondary")
        details = []
        if health_status.error_code:
            details.append(health_status.error_code)
        http_status = (health_status.metadata or {}).get("http_status")
        if http_status:
            details.append(f"HTTP {http_status}")
        if health_status.error_message:
            details.append(health_status.error_message)
        if health_status.last_checked_at:
            details.append(timezone.localtime(health_status.last_checked_at).strftime("%Y-%m-%d %H:%M"))
        return format_html(
            '<span class="badge {}">{}</span>{}',
            tone,
            health_status.get_status_display(),
            format_html('<div class="small text-muted">{}</div>', " | ".join(details)) if details else "",
        )

    @admin.display(description=_("هشدار"))
    def panel_alert_status(self, obj):
        if not getattr(obj, "health_alert_enabled", True):
            return format_html('<span class="badge bg-secondary">{}</span>', _("غیرفعال"))
        store = getattr(obj, "store", None)
        if not getattr(store, "panel_health_alerts_enabled", False):
            return format_html('<span class="badge bg-warning text-dark">{}</span>', _("در Store غیرفعال"))
        return format_html('<span class="badge bg-success">{}</span>', _("فعال"))

    @admin.display(description=_("وضعیت هشدار سلامت"))
    def health_alert_details(self, obj):
        if not obj:
            return "-"
        settings = PanelHealthAlertService().get_settings(obj)
        repeat_interval = panel_alert_repeat_interval_minutes(obj, settings)
        threshold = panel_alert_failure_threshold_count(obj, settings)
        try:
            health_status = obj.health_status
        except PanelHealthStatus.DoesNotExist:
            health_status = None
        last_alert = (
            timezone.localtime(health_status.last_alert_sent_at).strftime("%Y-%m-%d %H:%M")
            if health_status and health_status.last_alert_sent_at
            else "-"
        )
        last_error = health_status.error_message or health_status.summary if health_status else "-"
        rows = [
            (_("هشدار برای این پنل"), _("فعال است") if obj.health_alert_enabled else _("غیرفعال است")),
            (_("وضعیت فعلی پنل"), health_status.status if health_status else _("No check")),
            (_("آخرین خطا"), last_error or "-"),
            (_("آخرین هشدار ارسال‌شده"), last_alert),
            (
                _("آخرین بازیابی"),
                timezone.localtime(health_status.last_recovery_at).strftime("%Y-%m-%d %H:%M")
                if health_status and health_status.last_recovery_at
                else "-",
            ),
            (_("تعداد خطاهای پشت‌سرهم"), health_status.consecutive_failures if health_status else 0),
            (_("فاصله تکرار موثر"), _("%(minutes)s minute(s)") % {"minutes": repeat_interval}),
            (_("آستانه خطای موثر"), threshold),
            (_("Override"), _("فعال") if obj.alert_repeat_interval_minutes or obj.failure_threshold_count else _("استفاده از تنظیمات کلی")),
        ]
        return format_html(
            '<div class="qasedak-admin-summary"><ul>{}</ul></div>',
            format_html_join("", "<li><strong>{}</strong>: {}</li>", rows),
        )

    @admin.display(description=_("دکمه‌های هشدار"))
    def health_alert_actions(self, obj):
        if not obj or not obj.pk:
            return _("Save first to manage alerts.")
        enable_url = reverse("admin:store_panel_health_alert_enable", args=[obj.pk])
        disable_url = reverse("admin:store_panel_health_alert_disable", args=[obj.pk])
        test_url = reverse("admin:store_panel_health_alert_test", args=[obj.pk])
        dry_run_url = reverse("admin:store_panel_health_alert_dry_run", args=[obj.pk])
        panel_center_url = reverse("admin_store_panel_center_detail", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a>',
            enable_url,
            _("فعال‌سازی هشدار برای این پنل"),
            disable_url,
            _("غیرفعال‌سازی هشدار برای این پنل"),
            dry_run_url,
            _("اجرای Dry-run هشدار"),
            panel_center_url,
            _("اجرای بررسی سلامت"),
            test_url,
            _("ارسال پیام تستی فقط به ادمین"),
        )

    @admin.display(description=_("Latest health details"))
    def latest_health_details(self, obj):
        if not obj:
            return "-"
        try:
            health_status = obj.health_status
        except PanelHealthStatus.DoesNotExist:
            return _("No health check has been recorded yet.")
        metadata = health_status.metadata or {}
        http_status = metadata.get("http_status")
        remediation_hint = metadata.get("remediation_hint") or ""
        checked_at = (
            timezone.localtime(health_status.last_checked_at).strftime("%Y-%m-%d %H:%M")
            if health_status.last_checked_at
            else "-"
        )
        lines = [
            _("status=%(status)s") % {"status": health_status.status},
            _("error_code=%(code)s") % {"code": health_status.error_code or "-"},
            _("message=%(message)s") % {"message": health_status.error_message or health_status.summary or "-"},
            _("checked_at=%(checked_at)s") % {"checked_at": checked_at},
        ]
        if http_status:
            lines.append(_("http_status=HTTP %(status)s") % {"status": http_status})
        if remediation_hint:
            lines.append(_("remediation_hint=%(hint)s") % {"hint": remediation_hint})
        return format_html("<br>".join("{}" for _ in lines), *lines)

    @admin.display(description=_("Compatibility"), ordering="capability_profile")
    def compatibility_profile_status(self, obj):
        from .panels import get_safe_panel_adapter

        report = get_safe_panel_adapter(obj).get_capability_report()
        profile = report.capability_profile or obj.capability_profile or _("Legacy/undetected")
        tone = {
            Panel.CapabilityProfile.LEGACY_SINGLE_NODE: "bg-success",
            Panel.CapabilityProfile.MODERN_SINGLE_NODE: "bg-warning text-dark",
            Panel.CapabilityProfile.MODERN_MULTI_NODE: "bg-warning text-dark",
            Panel.CapabilityProfile.UNKNOWN_SAFE: "bg-danger",
        }.get(report.capability_profile or obj.capability_profile, "bg-secondary")
        if not report.supported:
            tone = "bg-secondary"
        version = report.detected_version or obj.detected_xui_version or "-"
        return format_html(
            '<span class="badge {}">{}</span> <span>{}</span> <span class="small text-muted">{}</span>',
            tone,
            profile,
            version,
            report.family,
        )

    @admin.display(description=_("Compatibility metadata"))
    def compatibility_metadata_summary(self, obj):
        metadata = obj.capability_metadata or {}
        if not metadata:
            return "-"
        return _(
            "nodes=%(nodes)s hosts=%(hosts)s inbounds=%(inbounds)s source=%(source)s checked_at=%(checked_at)s"
        ) % {
            "nodes": metadata.get("node_count", 0),
            "hosts": metadata.get("host_count", 0),
            "inbounds": metadata.get("inbound_count", 0),
            "source": metadata.get("source", "-"),
            "checked_at": metadata.get("checked_at", "-"),
        }

    @admin.display(description=_("Latest daily usage"))
    def latest_daily_usage(self, obj):
        usage = obj.daily_usages.order_by("-usage_date").first()
        if not usage:
            return "-"
        return _("%(date)s: %(usage)s (%(quality)s)") % {
            "date": usage.usage_date,
            "usage": format_usage_bytes(usage.used_bytes),
            "quality": usage.data_quality,
        }

    @admin.action(description=_("Run health check without alerts"))
    def run_health_check(self, request, queryset):
        from .panel_health_services import check_panel_health

        checked = 0
        errors = 0
        for panel in queryset.select_related("store"):
            try:
                result = check_panel_health(panel, send_alerts=False)
                checked += 1
                if result.get("status") == PanelHealthStatus.Status.OK:
                    messages.success(request, _("Health check passed for %(panel)s") % {"panel": panel.name})
                else:
                    errors += 1
                    messages.warning(
                        request,
                        _("Health check failed for %(panel)s: %(reason)s")
                        % {"panel": panel.name, "reason": _panel_health_admin_reason(result)},
                    )
            except Exception as exc:
                errors += 1
                messages.error(request, _("Could not check %(panel)s: %(error)s") % {"panel": panel, "error": exc})
        if checked:
            messages.success(request, _("%(count)s panel(s) checked.") % {"count": checked})
        if errors:
            messages.warning(request, _("%(count)s panel health check(s) failed.") % {"count": errors})


@admin.register(PanelHealthStatus)
class PanelHealthStatusAdmin(ImportExportModelAdmin):
    list_display = (
        "panel",
        "status",
        "error_code",
        "error_message_short",
        "remediation_hint_short",
        "last_checked_at",
        "last_ok_at",
        "last_error_at",
        "consecutive_failures",
        "response_time_ms",
        "summary_short",
    )
    list_filter = ("status", "panel__store", "last_checked_at", "last_error_at")
    search_fields = ("panel__name", "summary", "error_code", "error_message")
    date_hierarchy = "last_checked_at"
    list_select_related = ("panel", "panel__store")
    autocomplete_fields = ("panel",)
    readonly_fields = (
        "last_checked_at",
        "last_ok_at",
        "last_error_at",
        "last_alert_sent_at",
        "last_recovery_alert_sent_at",
        "consecutive_failures",
        "consecutive_successes",
        "response_time_ms",
        "error_code",
        "error_message",
        "summary",
        "metadata",
        "created_at",
        "updated_at",
    )
    actions = ("run_health_check",)

    @admin.display(description=_("Summary"))
    def summary_short(self, obj):
        return (obj.summary or "-")[:140]

    @admin.display(description=_("Error message"))
    def error_message_short(self, obj):
        return (obj.error_message or obj.summary or "-")[:140]

    @admin.display(description=_("Remediation"))
    def remediation_hint_short(self, obj):
        return ((obj.metadata or {}).get("remediation_hint") or "-")[:140]

    @admin.action(description=_("Run health check for selected panels without alerts"))
    def run_health_check(self, request, queryset):
        from .panel_health_services import check_panel_health

        checked = 0
        errors = 0
        for health_status in queryset.select_related("panel", "panel__store"):
            try:
                result = check_panel_health(health_status.panel, send_alerts=False)
                checked += 1
                if result.get("status") == PanelHealthStatus.Status.OK:
                    messages.success(
                        request,
                        _("Health check passed for %(panel)s") % {"panel": health_status.panel.name},
                    )
                else:
                    errors += 1
                    messages.warning(
                        request,
                        _("Health check failed for %(panel)s: %(reason)s")
                        % {"panel": health_status.panel.name, "reason": _panel_health_admin_reason(result)},
                    )
            except Exception as exc:
                errors += 1
                messages.error(
                    request,
                    _("Could not check %(panel)s: %(error)s") % {"panel": health_status.panel, "error": exc},
                )
        if checked:
            messages.success(request, _("%(count)s panel(s) checked.") % {"count": checked})
        if errors:
            messages.warning(request, _("%(count)s panel health check(s) failed.") % {"count": errors})


@admin.register(PanelHealthCheckLog)
class PanelHealthCheckLogAdmin(ImportExportModelAdmin):
    list_display = (
        "panel",
        "status",
        "error_code",
        "error_message_short",
        "remediation_hint_short",
        "checked_at",
        "response_time_ms",
        "login_ok",
        "inbounds_checked",
        "inbounds_ok",
        "inbounds_warning",
        "inbounds_error",
        "alert_sent",
    )
    list_filter = ("status", "panel", "panel__store", "checked_at", "alert_sent")
    search_fields = ("panel__name", "error_code", "error_message", "metadata")
    date_hierarchy = "checked_at"
    list_select_related = ("panel", "panel__store")
    readonly_fields = (
        "panel",
        "status",
        "checked_at",
        "response_time_ms",
        "login_ok",
        "inbounds_checked",
        "inbounds_ok",
        "inbounds_warning",
        "inbounds_error",
        "error_code",
        "error_message",
        "metadata",
        "alert_sent",
    )

    @admin.display(description=_("Error message"))
    def error_message_short(self, obj):
        return (obj.error_message or "-")[:140]

    @admin.display(description=_("Remediation"))
    def remediation_hint_short(self, obj):
        return ((obj.metadata or {}).get("remediation_hint") or "-")[:140]


@admin.register(PanelUsageSnapshot)
class PanelUsageSnapshotAdmin(ImportExportModelAdmin):
    list_display = (
        "panel",
        "captured_at",
        "status",
        "used_bytes_display",
        "clients_count",
        "online_clients_count",
        "checked_inbounds_count",
        "active_inbounds_count",
    )
    list_filter = ("status", "panel", "panel__store", "captured_at")
    search_fields = ("panel__name", "error_message", "metadata")
    date_hierarchy = "captured_at"
    list_select_related = ("panel", "panel__store")
    readonly_fields = (
        "panel",
        "captured_at",
        "status",
        "total_upload_bytes",
        "total_download_bytes",
        "total_used_bytes",
        "clients_count",
        "online_clients_count",
        "active_inbounds_count",
        "checked_inbounds_count",
        "error_message",
        "metadata",
        "created_at",
    )

    @admin.display(description=_("Used"))
    def used_bytes_display(self, obj):
        return format_usage_bytes(obj.total_used_bytes)


@admin.register(PanelClientUsageSnapshot)
class PanelClientUsageSnapshotAdmin(ImportExportModelAdmin):
    list_display = (
        "panel",
        "inbound",
        "captured_at",
        "client_identifier_masked",
        "used_bytes_display",
        "online",
        "enabled",
        "source",
    )
    list_filter = ("panel", "panel__store", "inbound", "captured_at", "online", "enabled", "source")
    search_fields = ("panel__name", "client_identifier_hash", "client_identifier_masked", "email_masked")
    date_hierarchy = "captured_at"
    list_select_related = ("panel", "panel__store", "inbound")
    readonly_fields = (
        "panel",
        "inbound",
        "captured_at",
        "client_identifier_hash",
        "client_identifier_masked",
        "email_masked",
        "upload_bytes",
        "download_bytes",
        "used_bytes",
        "total_bytes",
        "expiry_time",
        "enabled",
        "online",
        "source",
        "metadata",
    )

    @admin.display(description=_("Used"))
    def used_bytes_display(self, obj):
        return format_usage_bytes(obj.used_bytes)


@admin.register(PanelDailyUsage)
class PanelDailyUsageAdmin(ImportExportModelAdmin):
    list_display = (
        "panel",
        "usage_date",
        "timezone",
        "used_bytes_display",
        "active_users_count",
        "online_users_count",
        "data_quality",
        "calculated_at",
    )
    list_filter = ("data_quality", "panel", "panel__store", "usage_date", "timezone")
    search_fields = ("panel__name", "metadata")
    date_hierarchy = "usage_date"
    list_select_related = ("panel", "panel__store")
    readonly_fields = (
        "panel",
        "usage_date",
        "timezone",
        "upload_bytes",
        "download_bytes",
        "used_bytes",
        "active_users_count",
        "online_users_count",
        "clients_count_start",
        "clients_count_end",
        "snapshot_start_at",
        "snapshot_end_at",
        "data_quality",
        "metadata",
        "calculated_at",
    )

    @admin.display(description=_("Used"))
    def used_bytes_display(self, obj):
        return format_usage_bytes(obj.used_bytes)


class OrderResource(resources.ModelResource):
    store = fields.Field(
        column_name="store_id",
        attribute="store",
        widget=ForeignKeyWidget(Store, "id"),
    )
    customer = fields.Field(
        column_name="customer_id",
        attribute="customer",
        widget=ForeignKeyWidget(Customer, "id"),
    )
    plan = fields.Field(
        column_name="plan_id",
        attribute="plan",
        widget=ForeignKeyWidget(Plan, "id"),
    )
    operator = fields.Field(
        column_name="operator_id",
        attribute="operator",
        widget=ForeignKeyWidget(Operator, "id"),
    )
    discount_code = fields.Field(
        column_name="discount_code",
        attribute="discount_code",
        widget=ForeignKeyWidget(DiscountCode, "code"),
    )
    inbound = fields.Field(
        column_name="inbound_id",
        attribute="inbound",
        widget=ForeignKeyWidget(Inbound, "id"),
    )

    class Meta:
        model = Order
        import_id_fields = ("order_tracking_code",)
        fields = (
            "id",
            "public_id",
            "order_tracking_code",
            "store",
            "customer",
            "plan",
            "operator",
            "quantity",
            "status",
            "verification_status",
            "payment_method",
            "is_paid",
            "original_amount",
            "discount_code",
            "discount_code_text",
            "discount_source",
            "discount_amount",
            "amount",
            "currency",
            "sender_card_name",
            "sender_card_last4",
            "payment_date",
            "payment_time",
            "bank_tracking_code",
            "card_last_four",
            "inbound",
            "username",
            "uuid",
            "sub_link",
            "direct_link",
            "created_at",
            "updated_at",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


class OrderSMSMatchStatusFilter(admin.SimpleListFilter):
    title = _("SMS match status")
    parameter_name = "sms_match"

    def lookups(self, request, model_admin):
        return (
            ("none", _("No SMS match")),
            ("has_sms", _("Has SMS match")),
            ("matched", _("Matched SMS")),
            ("confirmed", _("Confirmed by SMS")),
            ("multiple", _("Multiple SMS matches")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "none":
            return queryset.filter(incoming_payment_sms__isnull=True)
        if value == "has_sms":
            return queryset.filter(incoming_payment_sms__isnull=False).distinct()
        if value == "matched":
            return queryset.filter(incoming_payment_sms__status=IncomingPaymentSMS.Status.MATCHED).distinct()
        if value == "confirmed":
            return queryset.filter(incoming_payment_sms__status=IncomingPaymentSMS.Status.CONFIRMED).distinct()
        if value == "multiple":
            return queryset.annotate(sms_matches_count=Count("incoming_payment_sms", distinct=True)).filter(
                sms_matches_count__gt=1
            )
        return queryset


class OrderDeliveryStatusFilter(admin.SimpleListFilter):
    title = _("delivery status")
    parameter_name = "delivery_status"

    def lookups(self, request, model_admin):
        return (
            ("pending", _("Not started")),
            ("ready", _("Config ready")),
            ("active", _("Active/delivered")),
            ("failed", _("Needs attention")),
            ("no_config", _("No config")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "pending":
            return queryset.filter(
                Q(status__in=[Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION])
                | Q(provisioning_status__in=[Order.ProvisioningStatus.PENDING, Order.ProvisioningStatus.PROVISIONING])
            )
        if value == "ready":
            return queryset.filter(vpn_clients__status__in=[VPNClient.Status.CREATED, VPNClient.Status.INACTIVE]).distinct()
        if value == "active":
            return queryset.filter(vpn_clients__status=VPNClient.Status.ACTIVE).distinct()
        if value == "failed":
            return queryset.filter(
                Q(metadata__panel_provisioning_deferred=True)
                | Q(metadata__panel_provisioning_last_failed_at__isnull=False)
                | Q(provisioning_status=Order.ProvisioningStatus.FAILED)
                | Q(vpn_clients__status=VPNClient.Status.ERROR)
                | Q(status__in=[Order.Status.CONFIRMED, Order.Status.COMPLETED], inbound__isnull=True)
                | Q(status=Order.Status.COMPLETED, vpn_clients__isnull=True)
            ).distinct()
        if value == "no_config":
            return queryset.filter(
                vpn_clients__isnull=True,
                status__in=[Order.Status.CONFIRMED, Order.Status.COMPLETED],
            ).distinct()
        return queryset


class MatchedPaymentSMSInline(admin.TabularInline):
    model = IncomingPaymentSMS.matched_orders.through
    extra = 0
    can_delete = False
    verbose_name = _("matched payment SMS")
    verbose_name_plural = _("matched payment SMS messages")
    fields = ("sms_admin_link", "sms_status", "sms_amount", "sms_datetime", "received_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("incomingpaymentsms")

    @admin.display(description=_("SMS"))
    def sms_admin_link(self, obj):
        url = reverse("admin:payments_incomingpaymentsms_change", args=[obj.incomingpaymentsms_id])
        return format_html('<a href="{}">#{}</a>', url, obj.incomingpaymentsms_id)

    @admin.display(description=_("Status"), ordering="incomingpaymentsms__status")
    def sms_status(self, obj):
        return obj.incomingpaymentsms.get_status_display()

    @admin.display(description=_("Amount"), ordering="incomingpaymentsms__amount")
    def sms_amount(self, obj):
        return f"{obj.incomingpaymentsms.amount:,}"

    @admin.display(description=_("SMS time"), ordering="incomingpaymentsms__sms_datetime")
    def sms_datetime(self, obj):
        return obj.incomingpaymentsms.sms_datetime

    @admin.display(description=_("Received at"), ordering="incomingpaymentsms__received_at")
    def received_at(self, obj):
        return obj.incomingpaymentsms.received_at


class VPNClientInline(admin.TabularInline):
    model = VPNClient
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name = _("VPN client")
    verbose_name_plural = _("VPN clients")
    fields = (
        "username",
        "inbound",
        "status",
        "deleted_at",
        "remote_deleted_at",
        "traffic_limit_bytes",
        "used_traffic_bytes",
        "activated_at",
        "expires_at",
        "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("inbound")


class VPNClientActionLogInline(admin.TabularInline):
    model = VPNClientActionLog
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "action",
        "actor_type",
        "actor_telegram_id",
        "status",
        "xui_identifier_masked",
        "old_total_bytes",
        "new_total_bytes",
        "old_expiry_time",
        "new_expiry_time",
        "created_at",
        "completed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class OrderReferralRewardLedgerInline(admin.TabularInline):
    model = ReferralRewardLedger
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "inviter",
        "invited",
        "reward_gb",
        "reward_duration_days",
        "status",
        "redeemed_config",
        "applied_traffic_gb",
        "applied_duration_days",
        "available_at",
        "redeemed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(ImportExportModelAdmin):
    resource_class = OrderResource
    list_display = (
        "order_tracking_code",
        "owner_customer",
        "plan",
        "amount_display",
        "payment_status_badge",
        "delivery_status_badge",
        "created_at",
        "quick_review_link",
    )
    list_filter = (
        "status",
        "verification_status",
        "provisioning_status",
        OrderDeliveryStatusFilter,
        "store",
        ("plan", admin.RelatedOnlyFieldListFilter),
        ("operator", admin.RelatedOnlyFieldListFilter),
        "payment_method",
        OrderSMSMatchStatusFilter,
        "discount_source",
        "created_at",
    )
    search_fields = (
        "order_tracking_code",
        "bank_tracking_code",
        "sender_card_name",
        "sender_card_last4",
        "username",
        "operator__name",
        "operator__slug",
        "discount_code__code",
        "discount_code_text",
        "plan__name",
        "customer__username",
        "customer__display_name",
        "customer__phone_number",
    )
    autocomplete_fields = ("store", "customer", "plan", "operator", "discount_code", "inbound", "verified_by")
    date_hierarchy = "created_at"
    list_select_related = ("store", "customer", "plan", "operator", "discount_code", "inbound", "verified_by")
    ordering = ("-created_at",)
    readonly_fields = (
        "public_id",
        "quantity",
        "original_amount",
        "discount_code",
        "discount_code_text",
        "discount_source",
        "discount_amount",
        "amount",
        "currency",
        "created_at",
        "updated_at",
        "payment_submitted_at",
        "admin_notified_at",
        "admin_receipt_notified_at",
        "receipt_analysis_summary",
        "metadata_safe_summary",
        "masked_config_links",
        "owner_customer",
        "payment_status_badge",
        "delivery_status_badge",
        "provisioning_status",
        "provisioning_attempts",
        "last_provisioning_error",
        "provisioned_at",
        "provisioning_idempotency_key",
        "quick_review_link",
        "verified_at",
    )
    fieldsets = (
        (
            _("Summary"),
            {
                "fields": (
                    "public_id",
                    "order_tracking_code",
                    "status",
                    "verification_status",
                    "quick_review_link",
                    "created_at",
                    "updated_at",
                )
            },
        ),
        (
            _("Customer"),
            {
                "fields": (
                    "store",
                    "customer",
                    "owner_customer",
                    "operator",
                )
            },
        ),
        (
            _("Plan / Service"),
            {
                "fields": (
                    "plan",
                    "quantity",
                    "original_amount",
                    "discount_code",
                    "discount_code_text",
                    "discount_source",
                    "discount_amount",
                    "amount",
                    "currency",
                )
            },
        ),
        (
            _("Payment"),
            {
                "fields": (
                    "payment_status_badge",
                    "payment_method",
                    "is_paid",
                    "verified_by",
                    "verified_at",
                    "payment_submitted_at",
                    "sender_card_name",
                    "sender_card_last4",
                    "payment_date",
                    "payment_time",
                    "payment_receipt_image",
                    "receipt_analysis_summary",
                    "bank_tracking_code",
                    "card_last_four",
                    "rejection_reason",
                )
            },
        ),
        (
            _("Delivery"),
            {
                "fields": (
                    "delivery_status_badge",
                    "provisioning_status",
                    "provisioning_attempts",
                    "last_provisioning_error",
                    "provisioned_at",
                    "inbound",
                    "username",
                    "masked_config_links",
                )
            },
        ),
        (
            _("Admin / Advanced"),
            {
                "classes": ("collapse",),
                "fields": (
                    "admin_notified_at",
                    "admin_receipt_notified_at",
                    "payment_gateway",
                    "gateway_authority",
                    "gateway_reference_id",
                    "provisioning_idempotency_key",
                    "metadata_safe_summary",
                ),
            },
        ),
    )
    actions = ("mark_as_confirmed", "verify_and_activate_selected")
    inlines = (MatchedPaymentSMSInline, VPNClientInline, OrderReferralRewardLedgerInline)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("vpn_clients")
            .annotate(sms_match_count=Count("incoming_payment_sms", distinct=True))
        )

    def qadmin_badge(self, label, tone="secondary"):
        css_class = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "info": "bg-info text-dark",
            "secondary": "bg-secondary",
        }.get(tone, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css_class, label)

    def mask_admin_phone(self, value):
        cleaned = "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit() or ch == "+")
        if not cleaned:
            return ""
        prefix = cleaned[:4] if cleaned.startswith("+") else cleaned[:3]
        suffix = cleaned[-2:] if len(cleaned) > 5 else ""
        return f"{prefix}***{suffix}" if suffix else "***"

    def safe_customer_label(self, customer):
        if not customer:
            return _("No customer")
        phone = getattr(customer, "phone_number", "") or ""
        label = (getattr(customer, "display_name", "") or getattr(customer, "username", "") or "").strip()
        if phone and normalize_payment_digits(label) == normalize_payment_digits(phone):
            return self.mask_admin_phone(phone)
        return label or f"Customer {str(customer.public_id)[:8]}"

    @admin.display(description=_("Customer"), ordering="customer__display_name")
    def owner_customer(self, obj):
        if not obj or not obj.customer_id:
            return self.qadmin_badge(_("No customer"), "secondary")
        label = self.safe_customer_label(obj.customer)
        phone = self.mask_admin_phone(obj.customer.phone_number)
        url = reverse("admin:store_customer_change", args=[obj.customer_id])
        if phone:
            return format_html('<a href="{}">{}</a><br><small>{}</small>', url, label, phone)
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description=_("Amount"), ordering="amount")
    def amount_display(self, obj):
        labels = {
            Plan.Currency.TOMAN: _("Toman"),
            Plan.Currency.IRR: _("IRR"),
            Plan.Currency.USD: "USD",
        }
        return f"{int(obj.amount or 0):,} {labels.get(obj.currency, obj.currency)}"

    @admin.display(description=_("Payment"), ordering="verification_status")
    def payment_status_badge(self, obj):
        if not obj:
            return "-"
        if obj.verification_status == Order.VerificationStatus.REJECTED or obj.status == Order.Status.REJECTED:
            return self.qadmin_badge(_("Rejected"), "danger")
        if obj.verification_status == Order.VerificationStatus.VERIFIED:
            return self.qadmin_badge(_("Verified"), "success")
        if obj.is_paid or obj.payment_submitted_at or obj.payment_receipt_image:
            return self.qadmin_badge(_("Needs review"), "warning")
        if obj.status == Order.Status.PENDING_PAYMENT:
            return self.qadmin_badge(_("Waiting payment"), "secondary")
        return self.qadmin_badge(_("Unknown"), "secondary")

    def order_delivery_status(self, obj):
        metadata = obj.metadata or {}
        if obj.status in {Order.Status.REJECTED, Order.Status.CANCELLED}:
            return _("Stopped"), "secondary"
        if obj.provisioning_status == Order.ProvisioningStatus.FAILED:
            return _("Provisioning failed"), "danger"
        if obj.provisioning_status in {Order.ProvisioningStatus.PENDING, Order.ProvisioningStatus.PROVISIONING} and obj.is_paid:
            return _("Awaiting provisioning"), "warning"
        if metadata.get("panel_provisioning_deferred") or metadata.get("panel_provisioning_last_failed_at"):
            return _("Provisioning failed"), "danger"
        clients = list(obj.vpn_clients.all())
        if not clients:
            if obj.status in {Order.Status.CONFIRMED, Order.Status.COMPLETED} or obj.verification_status == Order.VerificationStatus.VERIFIED:
                return _("No config"), "danger"
            if obj.is_paid:
                return _("Ready for review"), "warning"
            return _("Not started"), "secondary"
        if any(client.status == VPNClient.Status.ERROR for client in clients):
            return _("Config error"), "danger"
        active_count = sum(1 for client in clients if client.status == VPNClient.Status.ACTIVE)
        if active_count == len(clients):
            return _("Active/delivered"), "success"
        if active_count:
            return _("Partially active"), "warning"
        return _("Config ready"), "info"

    @admin.display(description=_("Delivery"))
    def delivery_status_badge(self, obj):
        if not obj:
            return "-"
        label, tone = self.order_delivery_status(obj)
        return self.qadmin_badge(label, tone)

    @admin.display(description=_("Review"))
    def quick_review_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_store_order_review", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Review"))

    @admin.display(description=_("Config links"))
    def masked_config_links(self, obj):
        if not obj:
            return "-"
        rows = []
        if obj.uuid:
            rows.append(_("UUID saved: ending %(suffix)s") % {"suffix": str(obj.uuid)[-4:]})
        if obj.sub_link:
            rows.append(_("Subscription link saved (hidden)."))
        if obj.direct_link:
            rows.append(_("Direct config link saved (hidden)."))
        return format_html_join("<br>", "{}", ((row,) for row in rows)) if rows else "-"

    @admin.display(description=_("Safe metadata summary"))
    def metadata_safe_summary(self, obj):
        metadata = obj.metadata or {}
        safe_keys = (
            "custom_volume",
            "custom_volume_gb",
            "panel_provisioning_deferred",
            "panel_provisioning_reason",
            "panel_provisioning_missing_clients",
            "provisioning_strategy",
            "renewed_at",
        )
        rows = [(key, metadata.get(key)) for key in safe_keys if metadata.get(key) not in (None, "", False)]
        receipt_analysis = metadata.get("receipt_analysis") or {}
        if receipt_analysis:
            rows.append(("receipt_analysis.status", receipt_analysis.get("status") or "-"))
            if receipt_analysis.get("warning"):
                rows.append(("receipt_analysis.warning", receipt_analysis.get("warning")))
        if not rows:
            return "-"
        return format_html_join("", "<div><strong>{}</strong>: {}</div>", rows)

    @admin.display(description=_("Discount"), ordering="discount_code__code")
    def discount_badge(self, obj):
        if not obj.discount_code_id and not obj.discount_code_text:
            return "-"
        label = obj.discount_code.code if obj.discount_code_id else obj.discount_code_text
        if obj.discount_code_id:
            url = reverse("admin:store_discountcode_change", args=[obj.discount_code_id])
            return format_html('<a href="{}">{}</a>', url, label)
        return label

    @admin.display(description=_("SMS match"), ordering="sms_match_count")
    def sms_match_summary(self, obj):
        count = getattr(obj, "sms_match_count", None)
        if count is None:
            count = obj.incoming_payment_sms.count()
        if not count:
            return format_html('<span class="badge bg-secondary">{}</span>', _("None"))
        if count == 1:
            payment_sms = obj.incoming_payment_sms.only("id", "status").first()
            if payment_sms and payment_sms.status == IncomingPaymentSMS.Status.CONFIRMED:
                return format_html('<span class="badge bg-success">{}</span>', _("Confirmed"))
            return format_html('<span class="badge bg-info text-dark">{}</span>', _("Matched"))
        return format_html('<span class="badge bg-warning text-dark">{}</span>', _("Multiple: %(count)s") % {"count": count})

    @admin.display(description=_("Receipt check"))
    def receipt_analysis_badge(self, obj):
        analysis = (obj.metadata or {}).get("receipt_analysis") or {}
        status = analysis.get("status")
        if status == "matched":
            return format_html('<span class="badge bg-success">{}</span>', _("Matched"))
        if status in {"mismatch", "not_found", "image_only", "unsupported_currency"}:
            return format_html('<span class="badge bg-warning text-dark">{}</span>', status)
        return format_html('<span class="badge bg-secondary">{}</span>', _("None"))

    @admin.display(description=_("Referral reward"))
    def referral_reward_status(self, obj):
        ledger = getattr(obj, "referral_reward_ledgers", None)
        if not ledger:
            return "-"
        reward = obj.referral_reward_ledgers.first()
        if not reward:
            return "-"
        url = reverse("admin:store_referralrewardledger_change", args=[reward.pk])
        return format_html('<a href="{}">{} · {} GB</a>', url, reward.get_status_display(), reward.reward_gb)

    @admin.display(description=_("Receipt analysis"))
    def receipt_analysis_summary(self, obj):
        analysis = (obj.metadata or {}).get("receipt_analysis") or {}
        if not analysis:
            return "-"
        expected = analysis.get("expected_amount_irr")
        detected = analysis.get("matched_amount_irr")
        warning = analysis.get("warning") or ""
        candidates = analysis.get("candidates") or []
        candidate_labels = ", ".join(f"{item.get('amount_irr'):,}" for item in candidates[:3] if item.get("amount_irr"))
        return format_html(
            "<div><strong>Status:</strong> {}</div>"
            "<div><strong>Expected:</strong> {}</div>"
            "<div><strong>Detected:</strong> {}</div>"
            "<div><strong>Candidates:</strong> {}</div>"
            "<div><strong>Warning:</strong> {}</div>",
            analysis.get("status") or "-",
            f"{expected:,} IRR" if expected is not None else "-",
            f"{detected:,} IRR" if detected is not None else "-",
            candidate_labels or "-",
            warning or "-",
        )

    def save_model(self, request, obj, form, change):
        old_obj = Order.objects.get(pk=obj.pk) if change else None
        activation_requested = (
            old_obj
            and old_obj.status != Order.Status.COMPLETED
            and (
                obj.status == Order.Status.COMPLETED
                or (
                    old_obj.verification_status != Order.VerificationStatus.VERIFIED
                    and obj.verification_status == Order.VerificationStatus.VERIFIED
                )
            )
        )
        rejection_requested = (
            old_obj
            and old_obj.status != Order.Status.REJECTED
            and (
                obj.status == Order.Status.REJECTED
                or (
                    old_obj.verification_status != Order.VerificationStatus.REJECTED
                    and obj.verification_status == Order.VerificationStatus.REJECTED
                )
            )
        )

        if activation_requested:
            self.activate_order(request, obj, old_obj=old_obj, save_order=False)
        elif rejection_requested:
            self.reject_order(request, obj, old_obj=old_obj)

        super().save_model(request, obj, form, change)

    def activate_order(self, request, obj, *, old_obj=None, save_order=True):
        result = activate_order(obj, user=request.user, notify=True)
        if result.success:
            messages.success(request, _("%(message)s Order %(tracking_code)s.") % {
                "message": result.message,
                "tracking_code": obj.order_tracking_code,
            })
            return True

        if old_obj:
            obj.status = old_obj.status
            obj.verification_status = old_obj.verification_status
            obj.verified_by = old_obj.verified_by
            obj.verified_at = old_obj.verified_at
        messages.error(request, _("%(message)s Order %(tracking_code)s.") % {
            "message": result.message,
            "tracking_code": obj.order_tracking_code,
        })
        return False

    def reject_order(self, request, obj, *, old_obj=None):
        result = reject_order(obj, reason=obj.rejection_reason, user=request.user, notify=True)
        if result.success:
            messages.success(request, _("%(message)s Order %(tracking_code)s.") % {
                "message": result.message,
                "tracking_code": obj.order_tracking_code,
            })
            obj.refresh_from_db()
            return True

        if old_obj:
            obj.status = old_obj.status
            obj.verification_status = old_obj.verification_status
            obj.verified_by = old_obj.verified_by
            obj.verified_at = old_obj.verified_at
            obj.rejection_reason = old_obj.rejection_reason
        messages.error(request, _("%(message)s Order %(tracking_code)s.") % {
            "message": result.message,
            "tracking_code": obj.order_tracking_code,
        })
        return False

    @admin.action(description=_("Mark selected orders as confirmed"))
    def mark_as_confirmed(self, request, queryset):
        now = timezone.now()
        eligible = queryset.exclude(
            status__in=[
                Order.Status.COMPLETED,
                Order.Status.REJECTED,
                Order.Status.CANCELLED,
            ]
        )
        updated = eligible.update(
            is_paid=True,
            status=Order.Status.CONFIRMED,
            verification_status=Order.VerificationStatus.VERIFIED,
            verified_by=request.user,
            verified_at=now,
            updated_at=now,
        )
        skipped = queryset.count() - updated
        if updated:
            messages.success(request, _("%(count)s order(s) marked as confirmed.") % {"count": updated})
        if skipped:
            messages.warning(request, _("%(count)s order(s) skipped because they are completed, rejected, or cancelled.") % {"count": skipped})

    @admin.action(description=_("Verify payment and activate selected VPN clients"))
    def verify_and_activate_selected(self, request, queryset):
        success_count = 0
        for order in queryset.select_related("plan", "inbound", "inbound__panel"):
            if order.status == Order.Status.COMPLETED:
                continue
            if self.activate_order(request, order):
                success_count += 1
        if success_count:
            messages.success(request, _("%(count)s order(s) verified and activated.") % {"count": success_count})


class InboundAdminForm(forms.ModelForm):
    config_params = forms.CharField(
        label=_("configuration parameters"),
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("مقدار کامل config نمایش داده نمی‌شود. برای تغییر، مقدار جدید را وارد کن."),
    )

    class Meta:
        model = Inbound
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        config_params = (cleaned_data.get("config_params") or "").strip()
        if config_params:
            cleaned_data["config_params"] = config_params
        elif self.instance and self.instance.pk:
            cleaned_data["config_params"] = self.instance.config_params
        else:
            self.add_error("config_params", _("Configuration parameters برای inbound جدید لازم است."))
        return cleaned_data


@admin.register(Inbound)
class InboundAdmin(ImportExportModelAdmin):
    legacy_default_note = "Legacy inbound kept for old orders/clients; not present in current X-UI."
    form = InboundAdminForm

    list_display = (
        "display_name",
        "panel_status",
        "xui_inbound_id",
        "xui_node_scope",
        "protocol",
        "is_active",
        "available_for_new_orders",
        "health_monitor_enabled",
        "active_plan_route_count",
        "sales_readiness",
        "catalog_link",
    )
    list_filter = (
        "is_active",
        "available_for_new_orders",
        "health_monitor_enabled",
        "panel",
        "xui_source",
        "xui_node_id",
        "protocol",
        "panel__store",
        "network_type",
        "security",
    )
    search_fields = (
        "remark",
        "legacy_note",
        "server_ip",
        "port",
        "xui_node_name",
        "xui_node_id",
        "xui_remote_key",
        "=inbound_id",
        "panel__name",
        "panel__url",
        "panel__store__name",
        "panel__store__english_name",
    )
    date_hierarchy = "created_at"
    list_select_related = ("panel", "panel__store")
    autocomplete_fields = ("panel",)
    readonly_fields = (
        "sales_readiness",
        "active_plan_route_count",
        "plan_fulfillment_links",
        "active_route_warning",
        "xui_remote_scope",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Summary"),
            {
                "fields": (
                    "panel",
                    "inbound_id",
                    "remark",
                    "protocol",
                    "sales_readiness",
                    "active_plan_route_count",
                    "plan_fulfillment_links",
                    "is_active",
                )
            },
        ),
        (
            _("Sales availability"),
            {
                "fields": (
                    "available_for_new_orders",
                    "max_clients",
                    "current_users",
                    "active_route_warning",
                )
            },
        ),
        (
            _("Health monitoring"),
            {
                "fields": ("health_monitor_enabled", "last_synced_at"),
            },
        ),
        (
            _("X-UI scope"),
            {
                "fields": (
                    "xui_source",
                    "xui_node_id",
                    "xui_node_name",
                    "xui_remote_key",
                    "xui_remote_scope",
                    "is_synced_from_node",
                ),
            },
        ),
        (
            _("Technical details"),
            {
                "classes": ("collapse",),
                "fields": (
                    "server_ip",
                    "port",
                    "config_params",
                    "network_type",
                    "security",
                    "sni",
                    "fingerprint",
                    "pbk",
                    "sid",
                    "ws_path",
                    "ws_host",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
        (
            _("Legacy"),
            {
                "classes": ("collapse",),
                "fields": ("legacy_note",),
            },
        ),
    )
    actions = ["sync_from_panel", "mark_as_legacy", "enable_for_new_orders_and_health"]

    def get_fieldsets(self, request, obj=None):
        if self.has_change_permission(request, obj):
            return super().get_fieldsets(request, obj)
        return (
            (
                _("Summary"),
                {
                    "fields": (
                        "panel",
                        "inbound_id",
                        "remark",
                        "protocol",
                        "sales_readiness",
                        "active_plan_route_count",
                        "plan_fulfillment_links",
                        "is_active",
                    )
                },
            ),
            (
                _("Sales availability"),
                {
                    "fields": (
                        "available_for_new_orders",
                        "max_clients",
                        "current_users",
                        "active_route_warning",
                    )
                },
            ),
            (
                _("Health monitoring"),
                {
                    "fields": ("health_monitor_enabled", "last_synced_at"),
                },
            ),
            (
                _("Technical details"),
                {
                    "classes": ("collapse",),
                    "fields": (
                        "server_ip",
                        "port",
                        "network_type",
                        "security",
                        "created_at",
                        "updated_at",
                    ),
                },
            ),
            (
                _("Legacy"),
                {
                    "classes": ("collapse",),
                    "fields": ("legacy_note",),
                },
            ),
        )

    @admin.display(description=_("Name"))
    def display_name(self, obj):
        label = obj.remark or f"Inbound {obj.inbound_id}"
        panel = getattr(obj, "panel", None)
        panel_name = getattr(panel, "name", None) or _("بدون پنل")
        return f"{panel_name} - {label}"

    @admin.display(description=_("Panel"), ordering="panel__name")
    def panel_status(self, obj):
        panel = getattr(obj, "panel", None)
        if not panel:
            return format_html('<span class="badge bg-danger">{}</span>', _("بدون پنل"))
        if not panel.is_active:
            return format_html(
                '<span class="badge bg-warning text-dark">{}: {}</span>',
                _("پنل غیرفعال"),
                panel.name,
            )
        return panel.name

    @admin.display(description=_("X-UI inbound ID"), ordering="inbound_id")
    def xui_inbound_id(self, obj):
        return obj.inbound_id

    @admin.display(description=_("Node"), ordering="xui_node_id")
    def xui_node_scope(self, obj):
        node = obj.xui_node_name or obj.xui_node_id
        if not node:
            return format_html('<span class="badge bg-secondary">{}</span>', _("local"))
        return format_html('<span class="badge bg-info">{}</span>', mask_xui_value(node))

    @admin.display(description=_("X-UI remote scope"))
    def xui_remote_scope(self, obj):
        return mask_xui_value(obj.xui_remote_scope_key)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(admin_active_plan_route_count=Count("plan_routes", filter=Q(plan_routes__is_active=True)))
        )

    @admin.display(description=_("Legacy"))
    def legacy_status(self, obj):
        if obj.legacy_note:
            return _("Legacy")
        return "-"

    @admin.display(description=_("Active plan routes"), ordering="admin_active_plan_route_count")
    def active_plan_route_count(self, obj):
        if not obj or not obj.pk:
            return 0
        value = getattr(obj, "admin_active_plan_route_count", None)
        if value is None:
            value = obj.plan_routes.filter(is_active=True).count()
        return value

    @admin.display(description=_("Route warning"))
    def active_route_warning(self, obj):
        if not obj or not obj.pk:
            return "-"
        active_routes = self.active_plan_route_count(obj)
        if active_routes and not obj.available_for_new_orders:
            return format_html('<span class="badge bg-danger">{}</span>', _("Active route on legacy inbound"))
        if active_routes and not obj.is_active:
            return format_html('<span class="badge bg-danger">{}</span>', _("Active route on inactive inbound"))
        if active_routes and obj.panel_id and not obj.panel.is_active:
            return format_html('<span class="badge bg-danger">{}</span>', _("Active route on inactive panel"))
        return "-"

    @admin.display(description=_("Sales readiness"))
    def sales_readiness(self, obj):
        if not obj or not obj.pk:
            return "-"
        readiness = get_inbound_sales_readiness(obj, getattr(getattr(obj, "panel", None), "store", None))
        css = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "info": "bg-info",
        }.get(readiness["tone"], "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css, readiness["label"])

    @admin.display(description=_("Catalog"))
    def catalog_link(self, obj):
        store = getattr(getattr(obj, "panel", None), "store", None)
        return format_html('<a class="button" href="{}">{}</a>', catalog_url(store), _("Catalog"))

    @admin.display(description=_("تحویل خودکار پلن"))
    def plan_fulfillment_links(self, obj):
        if not obj or not obj.pk:
            return "-"
        panel_id = getattr(obj, "panel_id", None)
        add_url = f"{reverse('admin:store_cupfillerrule_add')}?{urlencode({'source_type': CupFillerRule.SourceType.PANEL_INBOUNDS, 'panel': panel_id, 'inbounds': obj.pk})}"
        rules_url = f"{reverse('admin:store_cupfillerrule_changelist')}?{urlencode({'inbounds__id__exact': obj.pk})}"
        readiness_url = reverse("admin_store_panel_center_inbounds", args=[panel_id]) if panel_id else reverse("admin_store_plan_fulfillment")
        return format_html(
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a>',
            add_url,
            _("استفاده در Recipe پلن"),
            rules_url,
            _("مشاهده پلن‌های متصل"),
            readiness_url,
            _("تست readiness"),
        )

    @admin.action(description=_("Mark as legacy / exclude from sales and health monitor"))
    def mark_as_legacy(self, request, queryset):
        updated = 0
        for inbound in queryset:
            changed_fields = []
            if inbound.available_for_new_orders:
                inbound.available_for_new_orders = False
                changed_fields.append("available_for_new_orders")
            if inbound.health_monitor_enabled:
                inbound.health_monitor_enabled = False
                changed_fields.append("health_monitor_enabled")
            if not (inbound.legacy_note or "").strip():
                inbound.legacy_note = self.legacy_default_note
                changed_fields.append("legacy_note")
            if changed_fields:
                inbound.save(update_fields=[*changed_fields, "updated_at"])
                updated += 1
        if request is not None:
            self.message_user(
                request,
                _("%(count)s inbound(s) marked as legacy and excluded from new orders and health monitor.")
                % {"count": updated},
                level=messages.SUCCESS,
            )

    @admin.action(description=_("Enable for new orders and health monitor"))
    def enable_for_new_orders_and_health(self, request, queryset):
        updated = queryset.update(
            available_for_new_orders=True,
            health_monitor_enabled=True,
            updated_at=timezone.now(),
        )
        if request is not None:
            self.message_user(
                request,
                _(
                    "%(count)s inbound(s) enabled for new orders and health monitor. "
                    "Use this only after confirming the inbound exists in X-UI."
                )
                % {"count": updated},
                level=messages.WARNING,
            )

    @admin.action(description=_("Sync inbound settings from Sanaei/X-UI"))
    def sync_from_panel(self, request, queryset):
        success_count = 0
        error_count = 0

        for inbound in queryset.select_related("panel"):
            panel = inbound.panel
            success, result = sync_inbound_data(
                panel_url=panel.url,
                username=panel.username,
                password=panel.password,
                inbound_id=inbound.inbound_id,
                proxy_url=panel.proxy_url,
            )

            if success:
                inbound.protocol = result["protocol"]
                inbound.port = result["port"] or inbound.port
                inbound.config_params = result["config_params"] or inbound.config_params
                inbound.network_type = result["network_type"]
                inbound.security = result["security"]
                inbound.sni = result["sni"]
                inbound.fingerprint = result["fingerprint"]
                inbound.pbk = result["pbk"]
                inbound.sid = result["sid"]
                inbound.ws_path = result["ws_path"]
                inbound.ws_host = result["ws_host"]
                inbound.last_synced_at = timezone.now()
                inbound.save()
                success_count += 1
            else:
                error_count += 1
                self.message_user(
                    request,
                    _("Could not sync %(inbound)s: %(result)s") % {"inbound": inbound, "result": result},
                    level=messages.ERROR,
                )

        if success_count:
            self.message_user(
                request,
                _("%(count)s inbound(s) synced successfully.") % {"count": success_count},
                level=messages.SUCCESS,
            )
        if error_count:
            self.message_user(
                request,
                _("%(count)s inbound(s) failed to sync.") % {"count": error_count},
                level=messages.WARNING,
            )


@admin.register(VPNClient)
class VPNClientAdmin(ImportExportModelAdmin):
    inlines = (VPNClientActionLogInline,)
    list_display = (
        "service_short_label",
        "owner_customer",
        "plan_order_label",
        "panel_inbound_label",
        "status_active_badge",
        "expiry_days_left",
        "traffic_summary",
        "telegram_delivery_status",
        "quick_service_review_link",
    )
    list_filter = (
        "status",
        "store",
        ("inbound__panel", admin.RelatedOnlyFieldListFilter),
        ("inbound", admin.RelatedOnlyFieldListFilter),
        "created_at",
        "expires_at",
    )
    search_fields = (
        "username",
        "xui_email",
        "order__order_tracking_code",
        "order__customer__display_name",
        "order__customer__username",
        "order__customer__phone_number",
    )
    date_hierarchy = "created_at"
    list_select_related = ("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
    readonly_fields = (
        "public_id",
        "masked_public_id",
        "service_short_label",
        "owner_customer",
        "plan_order_label",
        "panel_inbound_label",
        "status_active_badge",
        "expiry_days_left",
        "traffic_summary",
        "telegram_delivery_status",
        "masked_config_links",
        "quick_service_review_link",
        "deleted_at",
        "remote_deleted_at",
        "created_at",
        "updated_at",
        "last_reminder_sent_at",
    )
    fieldsets = (
        (
            _("Summary"),
            {
                "fields": (
                    "masked_public_id",
                    "service_short_label",
                    "quick_service_review_link",
                    "status",
                    "status_active_badge",
                    "created_at",
                    "updated_at",
                )
            },
        ),
        (
            _("Customer / Order"),
            {
                "fields": (
                    "store",
                    "order",
                    "owner_customer",
                    "plan",
                    "plan_order_label",
                )
            },
        ),
        (
            _("Service"),
            {
                "fields": (
                    "inbound",
                    "panel_inbound_label",
                    "duration_days",
                    "device_limit",
                    "activated_at",
                    "expires_at",
                    "expiry_days_left",
                    "disabled_at",
                )
            },
        ),
        (
            _("Usage"),
            {
                "fields": (
                    "traffic_limit_bytes",
                    "used_upload_bytes",
                    "used_download_bytes",
                    "used_traffic_bytes",
                    "traffic_summary",
                    "last_online_at",
                    "last_synced_at",
                    "last_reminder_sent_at",
                )
            },
        ),
        (
            _("Delivery"),
            {
                "fields": (
                    "masked_config_links",
                    "telegram_delivery_status",
                )
            },
        ),
        (
            _("Advanced"),
            {
                "classes": ("collapse",),
                "fields": (
                    "deleted_at",
                    "remote_deleted_at",
                    "delete_reason",
                    "deleted_by_customer",
                    "deleted_by_admin_telegram_id",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                admin_last_reminder_sent_at=Max("reminder_logs__sent_at"),
                admin_telegram_bot_users_count=Count(
                    "order__customer__bot_users",
                    filter=Q(
                        order__customer__bot_users__is_active=True,
                        order__customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                    ),
                    distinct=True,
                ),
            )
        )

    def qadmin_badge(self, label, tone="secondary"):
        css_class = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "info": "bg-info text-dark",
            "secondary": "bg-secondary",
        }.get(tone, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css_class, label)

    @admin.display(description=_("Service"), ordering="id")
    def service_short_label(self, obj):
        label = admin_mask_identifier(obj.xui_email or obj.username or obj.public_id)
        return f"#{obj.pk} · {label}"

    @admin.display(description=_("Public ID"))
    def masked_public_id(self, obj):
        return admin_mask_identifier(obj.public_id)

    @admin.display(description=_("Customer"), ordering="order__customer__display_name")
    def owner_customer(self, obj):
        customer = obj.order.customer if obj.order_id and obj.order.customer_id else None
        if not customer:
            return self.qadmin_badge(_("No customer"), "secondary")
        url = reverse("admin_store_customer_review", args=[customer.pk])
        label = admin_safe_customer_label(customer)
        phone = admin_mask_phone(customer.phone_number)
        if phone:
            return format_html('<a href="{}">{}</a><br><small>{}</small>', url, label, phone)
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description=_("Plan / Order"), ordering="plan__name")
    def plan_order_label(self, obj):
        plan = obj.plan.name if obj.plan_id else "-"
        if obj.order_id:
            url = reverse("admin_store_order_review", args=[obj.order_id])
            return format_html('{}<br><small><a href="{}">{}</a></small>', plan, url, obj.order.order_tracking_code)
        return plan

    @admin.display(description=_("Panel / Inbound"), ordering="inbound__panel__name")
    def panel_inbound_label(self, obj):
        if not obj.inbound_id:
            return self.qadmin_badge(_("Missing inbound"), "danger")
        panel = obj.inbound.panel.name if obj.inbound.panel_id else _("No panel")
        inbound = obj.inbound.remark or obj.inbound.inbound_id
        return format_html("{}<br><small>{}</small>", panel, inbound)

    @admin.display(description=_("Status"), ordering="status")
    def status_active_badge(self, obj):
        tone = {
            VPNClient.Status.ACTIVE: "success",
            VPNClient.Status.ERROR: "danger",
            VPNClient.Status.EXPIRED: "warning",
            VPNClient.Status.SUSPENDED: "warning",
            VPNClient.Status.CREATED: "info",
        }.get(obj.status, "secondary")
        return self.qadmin_badge(obj.get_status_display(), tone)

    @admin.display(description=_("Expires / days left"), ordering="expires_at")
    def expiry_days_left(self, obj):
        if not obj.expires_at:
            return _("Unknown")
        seconds = int((obj.expires_at - timezone.now()).total_seconds())
        days = 0 if seconds <= 0 else (seconds + 86_399) // 86_400
        return format_html(
            "{}<br><small>{} days</small>",
            timezone.localtime(obj.expires_at).strftime("%Y-%m-%d %H:%M"),
            days,
        )

    @admin.display(description=_("Traffic"))
    def traffic_summary(self, obj):
        total = int(obj.traffic_limit_bytes or 0)
        used = int(obj.used_traffic_bytes or 0)
        remaining = max(total - used, 0) if total else 0
        if not total:
            return _("Unknown")
        return format_html(
            "{} used<br><small>{} remaining / {} total</small>",
            format_usage_bytes(used),
            format_usage_bytes(remaining),
            format_usage_bytes(total),
        )

    @admin.display(description=_("Telegram"))
    def telegram_delivery_status(self, obj):
        count = getattr(obj, "admin_telegram_bot_users_count", None)
        if count is None and obj.order_id and obj.order.customer_id:
            count = obj.order.customer.bot_users.filter(
                is_active=True,
                bot_config__provider=BotConfiguration.Provider.TELEGRAM,
            ).count()
        return self.qadmin_badge(_("Linked"), "success") if count else self.qadmin_badge(_("No target"), "warning")

    @admin.display(description=_("Config links"))
    def masked_config_links(self, obj):
        rows = []
        if obj.uuid:
            rows.append(_("UUID saved: ending %(suffix)s") % {"suffix": str(obj.uuid)[-4:]})
        if obj.sub_link:
            rows.append(_("Subscription link saved (hidden)."))
        if obj.direct_link:
            rows.append(_("Direct config link saved (hidden)."))
        return format_html_join("<br>", "{}", ((row,) for row in rows)) if rows else "-"

    @admin.display(description=_("Review"))
    def quick_service_review_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_store_service_review", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Review"))

    @admin.display(description=_("Last reminder"), ordering="admin_last_reminder_sent_at")
    def last_reminder_sent_at(self, obj):
        return getattr(obj, "admin_last_reminder_sent_at", None) or "-"


def _inventory_required_label(value):
    return _("الزامی") if value else _("اختیاری")


def _cup_rule_source_type_label(value):
    return {
        CupFillerRule.SourceType.PANEL_INBOUNDS: _("پنل‌ها و اینباندها"),
        CupFillerRule.SourceType.INVENTORY_POOL: _("مخزن کانفیگ"),
    }.get(value, value or "-")


def _cup_recipe_failure_policy_label(value):
    return {
        CupFulfillmentRecipe.FailurePolicy.STRICT: _("سخت‌گیرانه"),
        CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED: _("اجازه تحویل ناقص"),
    }.get(value, value or "-")


def _apply_admin_form_labels(form, labels, choices=None):
    for field_name, label in labels.items():
        if field_name in form.base_fields:
            form.base_fields[field_name].label = label
    for field_name, field_choices in (choices or {}).items():
        if field_name in form.base_fields:
            form.base_fields[field_name].choices = field_choices
    return form


def _safe_metadata_for_admin(value, key=""):
    if isinstance(value, dict):
        return {item_key: _safe_metadata_for_admin(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_safe_metadata_for_admin(item, key) for item in value[:50]]
    lowered_key = str(key or "").lower()
    if "masked" not in lowered_key and any(marker in lowered_key for marker in ("raw_link", "direct_link", "sub_link", "subscription_url", "token", "secret", "password", "private", "key")):
        return "***"
    return value


def _admin_metadata_safe_summary(metadata):
    safe_metadata = _safe_metadata_for_admin(metadata or {})
    if not safe_metadata:
        return "-"
    return format_html(
        '<pre dir="ltr" style="white-space:pre-wrap;overflow-wrap:anywhere;max-width:960px">{}</pre>',
        json.dumps(safe_metadata, ensure_ascii=False, indent=2, default=str),
    )


def _raw_link_reveal_controls(obj, *, masked_link):
    if not obj or not getattr(obj, "pk", None) or not getattr(obj, "raw_link", ""):
        return "-"
    target_id = f"config-full-link-{obj._meta.model_name}-{obj.pk}"
    return format_html(
        '<div class="config-link-reveal-controls">'
        '<code dir="ltr" class="config-link-masked">{}</code>'
        '<div class="config-link-actions">'
        '<button type="button" class="button" data-config-link-reveal="{}" data-config-full-link="{}">{}</button> '
        '<button type="button" class="button" data-config-link-copy="{}">{}</button> '
        '<button type="button" class="button" data-config-link-copy="{}">{}</button>'
        '</div>'
        '<code id="{}" dir="ltr" class="config-link-full" hidden></code>'
        '</div>',
        masked_link,
        target_id,
        obj.raw_link,
        _("نمایش لینک کامل"),
        obj.raw_link,
        _("کپی لینک کامل"),
        masked_link,
        _("کپی لینک ماسک‌شده"),
        target_id,
    )


def _style_raw_link_secret_widget(formfield):
    formfield.widget = forms.PasswordInput(
        render_value=True,
        attrs={
            "dir": "ltr",
            "autocomplete": "off",
            "class": "vLargeTextField config-link-secret-input",
        },
    )
    formfield.help_text = _("لینک کامل برای ویرایش ذخیره شده اما به صورت masked نمایش داده می‌شود. برای مشاهده از دکمه نمایش لینک کامل استفاده کنید.")
    return formfield


def _apply_parsed_link_fields(obj, *, form, change):
    raw_link = str(getattr(obj, "raw_link", "") or "").strip()
    parsed = parse_config_link(raw_link)
    obj.raw_link = parsed.raw_link
    obj.normalized_link = parsed.normalized_link
    obj.normalized_hash = parsed.normalized_hash
    obj.protocol = parsed.protocol
    obj.host = parsed.host
    obj.port = parsed.port
    changed_fields = set(getattr(form, "changed_data", []) or [])
    if (not change or "raw_link" in changed_fields) and "remark" not in changed_fields and parsed.remark:
        obj.remark = parsed.remark
    else:
        obj.remark = str(getattr(obj, "remark", "") or "").strip()[:255]
    return parsed


@admin.register(ExternalSubscriptionFeed)
class ExternalSubscriptionFeedAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "provider",
        "panel_label",
        "source_label",
        "masked_remote_identity",
        "status",
        "last_success_at",
        "next_refresh_at",
        "last_seen_upstream_count",
        "last_filtered_count",
        "source_owned_item_count",
        "consecutive_failures",
    )
    list_filter = ("provider", "status", "active", "panel", "created_at", "next_refresh_at")
    search_fields = ("remote_identity_ref", "panel__name", "cup__title", "cup__order__order_tracking_code")
    autocomplete_fields = ("cup", "delivery_source", "panel", "vpn_client")
    actions = ("refresh_now", "preview", "enable_auto_refresh", "disable_auto_refresh")
    readonly_fields = (
        "provider",
        "cup",
        "delivery_source",
        "panel",
        "vpn_client",
        "masked_remote_identity",
        "subscription_url_saved",
        "status",
        "last_attempt_at",
        "last_success_at",
        "next_refresh_at",
        "consecutive_failures",
        "last_good_config_count",
        "last_seen_upstream_count",
        "last_filtered_count",
        "last_error_code",
        "source_owned_item_count",
        "policy_safe_summary",
        "metadata_safe_summary",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Dynamic Subscription Source"),
            {
                "fields": (
                    "provider",
                    "active",
                    "status",
                    "cup",
                    "delivery_source",
                    "panel",
                    "vpn_client",
                    "masked_remote_identity",
                    "subscription_url_saved",
                )
            },
        ),
        (
            _("Refresh state"),
            {
                "fields": (
                    "last_attempt_at",
                    "last_success_at",
                    "next_refresh_at",
                    "consecutive_failures",
                    "last_good_config_count",
                    "last_seen_upstream_count",
                    "last_filtered_count",
                    "last_error_code",
                    "source_owned_item_count",
                )
            },
        ),
        (
            _("Policy and metadata"),
            {
                "classes": ("collapse",),
                "fields": ("policy_safe_summary", "metadata_safe_summary", "created_at", "updated_at"),
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("cup", "delivery_source", "panel", "vpn_client")

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("Panel"), ordering="panel__name")
    def panel_label(self, obj):
        return obj.panel or "-"

    @admin.display(description=_("Source"))
    def source_label(self, obj):
        return obj.delivery_source or "-"

    @admin.display(description=_("Remote identity"))
    def masked_remote_identity(self, obj):
        value = str(getattr(obj, "remote_identity_ref", "") or "")
        if not value:
            return "-"
        if len(value) <= 8:
            return f"{value[:2]}***"
        return f"{value[:4]}...{value[-4:]}"

    @admin.display(description=_("Upstream URL saved"), boolean=True)
    def subscription_url_saved(self, obj):
        return bool(getattr(obj, "protected_subscription_url", "") or "")

    @admin.display(description=_("Current Cup items"))
    def source_owned_item_count(self, obj):
        return source_owned_cup_item_count(obj)

    @admin.display(description=_("Policy"))
    def policy_safe_summary(self, obj):
        return _admin_metadata_safe_summary(getattr(obj, "resolved_filter_policy", {}) or {})

    @admin.display(description=_("Metadata"))
    def metadata_safe_summary(self, obj):
        return _admin_metadata_safe_summary(getattr(obj, "metadata", {}) or {})

    @admin.action(description=_("Refresh selected dynamic feeds now"))
    def refresh_now(self, request, queryset):
        refreshed = 0
        failed = 0
        for feed in queryset:
            summary = refresh_external_subscription_feed(feed.pk, force=True)
            refreshed += int(summary.ok)
            failed += int(not summary.ok)
        messages.info(request, _("Dynamic feed refresh finished: ok=%(ok)s failed=%(failed)s") % {"ok": refreshed, "failed": failed})

    @admin.action(description=_("Preview selected dynamic feeds"))
    def preview(self, request, queryset):
        previews = []
        for feed in queryset[:10]:
            summary = refresh_external_subscription_feed(feed.pk, force=True, dry_run=True)
            previews.append(
                "feed=%(feed)s input=%(input)s invalid=%(invalid)s selected=%(selected)s status=%(status)s error=%(error)s"
                % {
                    "feed": feed.pk,
                    "input": summary.upstream_count,
                    "invalid": summary.invalid_count,
                    "selected": summary.selected_count,
                    "status": safe_external_subscription_text(summary.status),
                    "error": safe_external_subscription_text(summary.error_code or "-"),
                }
            )
        messages.info(request, " | ".join(previews) if previews else _("No feeds selected."))

    @admin.action(description=_("Enable auto refresh"))
    def enable_auto_refresh(self, request, queryset):
        count = queryset.update(active=True, status=ExternalSubscriptionFeed.Status.HEALTHY, updated_at=timezone.now())
        messages.success(request, _("Auto refresh enabled for %(count)s feeds.") % {"count": count})

    @admin.action(description=_("Disable auto refresh"))
    def disable_auto_refresh(self, request, queryset):
        count = queryset.update(active=False, status=ExternalSubscriptionFeed.Status.DISABLED, updated_at=timezone.now())
        messages.success(request, _("Auto refresh disabled for %(count)s feeds.") % {"count": count})


@admin.register(ConfigInventoryPool)
class ConfigInventoryPoolAdmin(ImportExportModelAdmin):
    change_list_template = "admin/store/config_inventory/pool_change_list.html"
    list_display = (
        "id",
        "pool_title",
        "connected_plan_label",
        "allocation_mode_label",
        "available_stock",
        "ready_assets",
        "assigned_assets",
        "active_status",
        "priority_label",
        "pool_action_links",
        "created_at",
    )
    list_filter = ("allocation_mode", "is_active", "connected_plan", "created_at")
    search_fields = ("title", "description", "connected_plan__name")
    autocomplete_fields = ("connected_plan",)
    readonly_fields = ("pool_action_links", "stock_summary", "created_at", "updated_at")
    fieldsets = (
        (_("مخزن کانفیگ"), {"fields": ("title", "description", "connected_plan", "is_active", "priority", "pool_action_links")}),
        (
            _("قانون تخصیص"),
            {
                "fields": (
                    "allocation_mode",
                    "max_allocations_per_asset",
                    "traffic_limit_gb",
                    "duration_days",
                    "stock_summary",
                )
            },
        ),
        (_("داده‌های فنی"), {"classes": ("collapse",), "fields": ("metadata", "created_at", "updated_at")}),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("import/", self.admin_site.admin_view(self.import_assets_view), name="store_configinventorypool_import"),
        ]
        return custom + urls

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        form = _apply_admin_form_labels(
            form,
            {
                "title": _("نام مخزن"),
                "description": _("توضیحات"),
                "connected_plan": _("پلن متصل"),
                "is_active": _("فعال"),
                "priority": _("اولویت"),
                "allocation_mode": _("نوع تخصیص"),
                "max_allocations_per_asset": _("حداکثر استفاده از هر کانفیگ"),
                "traffic_limit_gb": _("حجم هر کانفیگ (GB)"),
                "duration_days": _("مدت اعتبار (روز)"),
                "metadata": _("داده‌های فنی"),
            },
            {
                "allocation_mode": [(value, inventory_allocation_mode_label(value)) for value, _label in ConfigInventoryPool.AllocationMode.choices],
            },
        )
        if "max_allocations_per_asset" in form.base_fields:
            form.base_fields["max_allocations_per_asset"].help_text = _("اگر خالی باشد، در حالت اشتراکی نامحدود محدودیتی اعمال نمی‌شود.")
        return form

    @admin.display(description=_("نام مخزن"), ordering="title")
    def pool_title(self, obj):
        return obj.title

    @admin.display(description=_("پلن متصل"), ordering="connected_plan__name")
    def connected_plan_label(self, obj):
        return obj.connected_plan or "-"

    @admin.display(description=_("نوع تخصیص"), ordering="allocation_mode")
    def allocation_mode_label(self, obj):
        return inventory_allocation_mode_label(obj.allocation_mode)

    @admin.display(description=_("موجودی قابل فروش"))
    def available_stock(self, obj):
        summary = get_pool_stock_summary(obj)
        capacity = summary["available_capacity"]
        if capacity is None:
            return _("نامحدود (%(count)s کانفیگ قابل استفاده)") % {"count": summary["usable_asset_count"]}
        return capacity

    @admin.display(description=_("آماده"))
    def ready_assets(self, obj):
        return ConfigInventoryAsset.objects.filter(pool=obj, status=ConfigInventoryAsset.Status.AVAILABLE).count()

    @admin.display(description=_("تخصیص‌داده‌شده"))
    def assigned_assets(self, obj):
        return ConfigInventoryAsset.objects.filter(pool=obj, status=ConfigInventoryAsset.Status.ASSIGNED).count()

    @admin.display(description=_("فعال"), boolean=True, ordering="is_active")
    def active_status(self, obj):
        return obj.is_active

    @admin.display(description=_("اولویت"), ordering="priority")
    def priority_label(self, obj):
        return obj.priority

    @admin.display(description=_("عملیات"))
    def pool_action_links(self, obj):
        import_url = reverse("admin_store_config_inventory_import")
        if not obj or not obj.pk:
            return format_html('<a class="button" href="{}">{}</a>', import_url, _("وارد کردن کانفیگ"))
        quick_url = reverse("admin_store_cup_center_quick_build")
        assets_url = reverse("admin:store_configinventoryasset_changelist")
        recipe_add_url = f"{reverse('admin:store_cupfillerrule_add')}?{urlencode({'source_type': CupFillerRule.SourceType.INVENTORY_POOL, 'inventory_pool': obj.pk})}"
        recipe_rules_url = f"{reverse('admin:store_cupfillerrule_changelist')}?{urlencode({'inventory_pool__id__exact': obj.pk})}"
        return format_html(
            '<a class="button" href="{}?pool={}">{}</a> '
            '<a class="button" href="{}?inventory_pool={}">{}</a> '
            '<a class="button" href="{}?pool__id__exact={}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a>',
            import_url,
            obj.pk,
            _("وارد کردن کانفیگ"),
            quick_url,
            obj.pk,
            _("ساخت سریع Cup با این مخزن"),
            assets_url,
            obj.pk,
            _("مشاهده کانفیگ‌های مخزن"),
            recipe_add_url,
            _("استفاده در Recipe پلن"),
            recipe_rules_url,
            _("مشاهده پلن‌های متصل"),
        )

    @admin.display(description=_("خلاصه موجودی"))
    def stock_summary(self, obj):
        if not obj or not obj.pk:
            return "-"
        summary = get_pool_stock_summary(obj)
        if not summary["asset_count"]:
            import_url = reverse("admin_store_config_inventory_import")
            return format_html(
                '<div class="help">{}</div><p><a class="button" href="{}?pool={}">{}</a></p>',
                _("این مخزن هنوز کانفیگی ندارد. از دکمه وارد کردن کانفیگ استفاده کنید."),
                import_url,
                obj.pk,
                _("وارد کردن کانفیگ"),
            )
        rows = [
            (_("نوع تخصیص"), inventory_allocation_mode_label(summary["allocation_mode"])),
            (_("کل کانفیگ‌ها"), summary["asset_count"]),
            (_("کانفیگ‌های قابل استفاده"), summary["usable_asset_count"]),
            (_("ظرفیت قابل فروش"), _("نامحدود") if summary["available_capacity"] is None else summary["available_capacity"]),
        ]
        for status, count in summary["status_counts"].items():
            if count:
                rows.append((inventory_asset_status_label(status), count))
        return format_html_join("", "<div><strong>{}</strong>: {}</div>", rows)

    def import_assets_view(self, request):
        return config_inventory_import(request)


@admin.register(ConfigInventoryAsset)
class ConfigInventoryAssetAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "asset_pool",
        "protocol_label",
        "masked_raw_link",
        "asset_status",
        "current_allocations_label",
        "max_allocations_label",
        "source_batch_label",
        "expires_at_label",
        "created_at",
    )
    list_filter = ("status", "protocol", "pool", "source_batch", "created_at", "expires_at")
    search_fields = ("normalized_hash", "remark", "host", "source_batch")
    autocomplete_fields = ("pool",)
    readonly_fields = (
        "masked_raw_link",
        "raw_link_reveal_controls",
        "normalized_link_masked",
        "normalized_link",
        "normalized_hash",
        "protocol",
        "host",
        "port",
        "current_allocations",
        "metadata_safe_summary",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (_("لینک کانفیگ"), {"fields": ("pool", "raw_link", "raw_link_reveal_controls", "masked_raw_link", "protocol", "remark", "host", "port", "status")}),
        (
            _("تخصیص"),
            {
                "fields": (
                    "traffic_limit_gb",
                    "expires_at",
                    "max_allocations",
                    "current_allocations",
                    "source_batch",
                )
            },
        ),
        (_("داده‌های فنی"), {"classes": ("collapse",), "fields": ("normalized_link_masked", "normalized_hash", "metadata_safe_summary", "created_at", "updated_at")}),
    )
    list_select_related = ("pool",)

    class Media:
        js = ("admin/config_link_reveal.js",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        return _apply_admin_form_labels(
            form,
            {
                "pool": _("مخزن کانفیگ"),
                "raw_link": _("لینک کانفیگ"),
                "raw_link_reveal_controls": _("نمایش / کپی کنترل‌شده"),
                "protocol": _("پروتکل"),
                "remark": _("نام/Remark"),
                "host": _("میزبان"),
                "port": _("پورت"),
                "status": _("وضعیت"),
                "traffic_limit_gb": _("حجم (GB)"),
                "expires_at": _("انقضا"),
                "max_allocations": _("حداکثر استفاده"),
                "current_allocations": _("تعداد استفاده"),
                "source_batch": _("نام دسته / Batch"),
                "normalized_link_masked": _("لینک نرمال‌شده masked"),
                "normalized_hash": _("هش نرمال‌شده"),
                "metadata_safe_summary": _("داده‌های فنی امن"),
            },
            {
                "status": [(value, inventory_asset_status_label(value)) for value, _label in ConfigInventoryAsset.Status.choices],
            },
        )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "raw_link" and formfield:
            return _style_raw_link_secret_widget(formfield)
        return formfield

    @admin.display(description=_("مخزن کانفیگ"), ordering="pool__title")
    def asset_pool(self, obj):
        return obj.pool

    @admin.display(description=_("پروتکل"), ordering="protocol")
    def protocol_label(self, obj):
        return obj.protocol

    @admin.display(description=_("لینک کانفیگ"))
    def masked_raw_link(self, obj):
        if not obj or not obj.raw_link:
            return "-"
        return mask_config_link_for_display(obj.raw_link)

    @admin.display(description=_("نمایش / کپی کنترل‌شده"))
    def raw_link_reveal_controls(self, obj):
        return _raw_link_reveal_controls(obj, masked_link=self.masked_raw_link(obj))

    @admin.display(description=_("لینک نرمال‌شده masked"))
    def normalized_link_masked(self, obj):
        if not obj or not obj.normalized_link:
            return "-"
        return mask_config_link_for_display(obj.normalized_link)

    @admin.display(description=_("داده‌های فنی امن"))
    def metadata_safe_summary(self, obj):
        return _admin_metadata_safe_summary(getattr(obj, "metadata", {}) or {})

    @admin.display(description=_("وضعیت"), ordering="status")
    def asset_status(self, obj):
        return inventory_asset_status_label(obj.status)

    @admin.display(description=_("تعداد استفاده"), ordering="current_allocations")
    def current_allocations_label(self, obj):
        return obj.current_allocations

    @admin.display(description=_("حداکثر استفاده"), ordering="max_allocations")
    def max_allocations_label(self, obj):
        return obj.max_allocations if obj.max_allocations is not None else _("نامحدود")

    @admin.display(description=_("نام دسته / Batch"), ordering="source_batch")
    def source_batch_label(self, obj):
        return obj.source_batch or "-"

    @admin.display(description=_("انقضا"), ordering="expires_at")
    def expires_at_label(self, obj):
        return obj.expires_at or "-"

    def save_model(self, request, obj, form, change):
        parsed = _apply_parsed_link_fields(obj, form=form, change=change)
        super().save_model(request, obj, form, change)
        if not change or "raw_link" in (getattr(form, "changed_data", []) or []):
            messages.success(
                request,
                _("لینک دوباره parse شد: پروتکل %(protocol)s، میزبان %(host)s، پایان هش %(suffix)s.")
                % {"protocol": parsed.protocol, "host": parsed.host or "-", "suffix": (parsed.normalized_hash or "")[-8:] or "-"},
            )


@admin.register(ConfigAllocation)
class ConfigAllocationAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "asset_label",
        "allocation_mode_label",
        "allocation_status",
        "cup_label",
        "order_label",
        "allocated_at_label",
        "released_at_label",
    )
    list_filter = ("status", "allocation_mode", "allocated_at", "released_at")
    search_fields = ("asset__normalized_hash", "asset__remark", "cup__token", "order__order_tracking_code")
    autocomplete_fields = ("asset", "cup", "order")
    readonly_fields = ("allocated_at", "created_at", "updated_at")
    list_select_related = ("asset", "cup", "order")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        return _apply_admin_form_labels(
            form,
            {
                "asset": _("کانفیگ آماده"),
                "cup": _("SubscriptionCup"),
                "order": _("سفارش"),
                "allocation_mode": _("نوع تخصیص"),
                "status": _("وضعیت"),
                "allocated_at": _("زمان تخصیص"),
                "released_at": _("زمان آزادسازی"),
                "metadata": _("داده‌های فنی"),
            },
            {
                "allocation_mode": [(value, inventory_allocation_mode_label(value)) for value, _label in ConfigInventoryPool.AllocationMode.choices],
                "status": [(value, config_allocation_status_label(value)) for value, _label in ConfigAllocation.Status.choices],
            },
        )

    @admin.display(description=_("کانفیگ آماده"), ordering="asset")
    def asset_label(self, obj):
        return obj.asset

    @admin.display(description=_("نوع تخصیص"), ordering="allocation_mode")
    def allocation_mode_label(self, obj):
        return inventory_allocation_mode_label(obj.allocation_mode)

    @admin.display(description=_("وضعیت"), ordering="status")
    def allocation_status(self, obj):
        return config_allocation_status_label(obj.status)

    @admin.display(description=_("SubscriptionCup"), ordering="cup")
    def cup_label(self, obj):
        return obj.cup or "-"

    @admin.display(description=_("سفارش"), ordering="order")
    def order_label(self, obj):
        return obj.order or "-"

    @admin.display(description=_("زمان تخصیص"), ordering="allocated_at")
    def allocated_at_label(self, obj):
        return obj.allocated_at

    @admin.display(description=_("زمان آزادسازی"), ordering="released_at")
    def released_at_label(self, obj):
        return obj.released_at or "-"


class CupFillerRuleInline(admin.TabularInline):
    model = CupFillerRule
    verbose_name = _("قانون پرکننده Cup")
    verbose_name_plural = _("قوانین پرکننده Cup")
    extra = 0
    show_change_link = True
    autocomplete_fields = ("panel", "inventory_pool")
    filter_horizontal = ("inbounds",)
    fields = (
        "position",
        "source_type",
        "quantity",
        "required",
        "panel",
        "inbounds",
        "inventory_pool",
        "allocation_mode",
        "is_active",
    )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if formfield:
            labels = {
                "position": _("ترتیب"),
                "source_type": _("منبع"),
                "quantity": _("تعداد"),
                "required": _("الزامی"),
                "panel": _("پنل"),
                "inventory_pool": _("مخزن کانفیگ"),
                "allocation_mode": _("نوع تخصیص"),
                "is_active": _("فعال"),
            }
            if db_field.name in labels:
                formfield.label = labels[db_field.name]
            if db_field.name == "source_type":
                formfield.choices = [(value, _cup_rule_source_type_label(value)) for value, _label in CupFillerRule.SourceType.choices]
            if db_field.name == "allocation_mode":
                formfield.choices = [("", "---------"), *[(value, inventory_allocation_mode_label(value)) for value, _label in ConfigInventoryPool.AllocationMode.choices]]
        return formfield

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        formfield = super().formfield_for_manytomany(db_field, request, **kwargs)
        if formfield and db_field.name == "inbounds":
            formfield.label = _("اینباندها")
        return formfield


@admin.register(CupFulfillmentRecipe)
class CupFulfillmentRecipeAdmin(ImportExportModelAdmin):
    inlines = (CupFillerRuleInline,)
    list_display = (
        "id",
        "recipe_title",
        "plan_label",
        "active_status",
        "priority_label",
        "failure_policy_label",
        "active_rule_count",
        "preview_link",
        "created_at",
    )
    list_filter = ("is_active", "failure_policy", "plan", "created_at")
    search_fields = ("title", "plan__name")
    autocomplete_fields = ("plan",)
    readonly_fields = ("preview_link", "created_at", "updated_at")
    fieldsets = (
        (_("دستور پر کردن Cup"), {"fields": ("title", "plan", "is_active", "priority", "failure_policy", "preview_link")}),
        (_("داده‌های فنی"), {"classes": ("collapse",), "fields": ("metadata", "created_at", "updated_at")}),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("<int:recipe_id>/preview/", self.admin_site.admin_view(self.preview_view), name="store_cupfulfillmentrecipe_preview"),
        ]
        return custom + urls

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("plan").annotate(admin_active_rule_count=Count("rules", filter=Q(rules__is_active=True)))

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        return _apply_admin_form_labels(
            form,
            {
                "title": _("عنوان دستور"),
                "plan": _("پلن متصل"),
                "is_active": _("فعال"),
                "priority": _("اولویت"),
                "failure_policy": _("رفتار در خطا"),
                "metadata": _("داده‌های فنی"),
            },
            {
                "failure_policy": [(value, _cup_recipe_failure_policy_label(value)) for value, _label in CupFulfillmentRecipe.FailurePolicy.choices],
            },
        )

    @admin.display(description=_("عنوان دستور"), ordering="title")
    def recipe_title(self, obj):
        return obj.title

    @admin.display(description=_("پلن متصل"), ordering="plan__name")
    def plan_label(self, obj):
        return obj.plan

    @admin.display(description=_("فعال"), boolean=True, ordering="is_active")
    def active_status(self, obj):
        return obj.is_active

    @admin.display(description=_("اولویت"), ordering="priority")
    def priority_label(self, obj):
        return obj.priority

    @admin.display(description=_("رفتار در خطا"), ordering="failure_policy")
    def failure_policy_label(self, obj):
        return _cup_recipe_failure_policy_label(obj.failure_policy)

    @admin.display(description=_("قوانین فعال"), ordering="admin_active_rule_count")
    def active_rule_count(self, obj):
        return getattr(obj, "admin_active_rule_count", None) if getattr(obj, "admin_active_rule_count", None) is not None else obj.rules.filter(is_active=True).count()

    @admin.display(description=_("عملیات"))
    def preview_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        builder_url = reverse("admin_store_plan_fulfillment_recipe", args=[obj.pk])
        preview_url = reverse("admin_store_plan_fulfillment_recipe_preview", args=[obj.pk])
        simulate_url = reverse("admin_store_plan_fulfillment_recipe_simulate", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a> '
            '<a class="button" href="{}">{}</a>',
            builder_url,
            _("دستور تحویل"),
            preview_url,
            _("پیش‌نمایش دستور تحویل"),
            simulate_url,
            _("تست شبیه‌سازی"),
        )

    def preview_view(self, request, recipe_id):
        return fulfillment_recipe_preview(request, recipe_id)


class PlanDeliverySourceInline(admin.TabularInline):
    model = PlanDeliverySource
    extra = 0
    fields = (
        "source_type",
        "panel",
        "inbound",
        "inventory_pool",
        "quantity",
        "required",
        "priority",
        "is_fallback",
        "active",
        "label",
    )
    autocomplete_fields = ("panel", "inbound", "inventory_pool")


@admin.register(PlanDeliveryConfig)
class PlanDeliveryConfigAdmin(ImportExportModelAdmin):
    inlines = (PlanDeliverySourceInline,)
    list_display = (
        "id",
        "plan_label",
        "delivery_mode",
        "failure_policy",
        "active",
        "active_source_count",
        "created_at",
    )
    list_filter = ("delivery_mode", "failure_policy", "active", "created_at")
    search_fields = ("plan__name", "plan__slug")
    autocomplete_fields = ("plan",)
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("plan").annotate(
            admin_active_source_count=Count("sources", filter=Q(sources__active=True))
        )

    @admin.display(description=_("پلن"), ordering="plan__name")
    def plan_label(self, obj):
        return obj.plan

    @admin.display(description=_("منابع فعال"), ordering="admin_active_source_count")
    def active_source_count(self, obj):
        return getattr(obj, "admin_active_source_count", None) if getattr(obj, "admin_active_source_count", None) is not None else obj.sources.filter(active=True).count()


@admin.register(PlanDeliverySource)
class PlanDeliverySourceAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "plan_label",
        "source_type",
        "source_label",
        "quantity",
        "required",
        "priority",
        "is_fallback",
        "active",
    )
    list_filter = ("source_type", "required", "is_fallback", "active", "created_at")
    search_fields = (
        "delivery_config__plan__name",
        "label",
        "panel__name",
        "inbound__remark",
        "inventory_pool__title",
    )
    autocomplete_fields = ("delivery_config", "panel", "inbound", "inventory_pool")
    list_select_related = ("delivery_config", "delivery_config__plan", "panel", "inbound", "inventory_pool")

    @admin.display(description=_("پلن"), ordering="delivery_config__plan__name")
    def plan_label(self, obj):
        return obj.delivery_config.plan

    @admin.display(description=_("منبع"))
    def source_label(self, obj):
        if obj.source_type == PlanDeliverySource.SourceType.INVENTORY_POOL:
            return obj.inventory_pool or "-"
        return obj.inbound or obj.panel or "-"


@admin.register(CupFillerRule)
class CupFillerRuleAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "recipe_label",
        "position_label",
        "source_type_label",
        "quantity_label",
        "required_label",
        "panel_label",
        "inventory_pool_label",
        "allocation_mode_label",
        "active_status",
    )
    list_filter = ("source_type", "required", "is_active", "panel", "inventory_pool", "created_at")
    search_fields = ("recipe__title", "recipe__plan__name", "panel__name", "inventory_pool__title")
    autocomplete_fields = ("recipe", "panel", "inventory_pool")
    filter_horizontal = ("inbounds",)
    list_select_related = ("recipe", "panel", "inventory_pool")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        return _apply_admin_form_labels(
            form,
            {
                "recipe": _("دستور پر کردن Cup"),
                "position": _("ترتیب"),
                "source_type": _("منبع"),
                "quantity": _("تعداد"),
                "required": _("الزامی"),
                "panel": _("پنل"),
                "inbounds": _("اینباندها"),
                "inventory_pool": _("مخزن کانفیگ"),
                "allocation_mode": _("نوع تخصیص"),
                "is_active": _("فعال"),
                "metadata": _("داده‌های فنی"),
            },
            {
                "source_type": [(value, _cup_rule_source_type_label(value)) for value, _label in CupFillerRule.SourceType.choices],
                "allocation_mode": [("", "---------"), *[(value, inventory_allocation_mode_label(value)) for value, _label in ConfigInventoryPool.AllocationMode.choices]],
            },
        )

    @admin.display(description=_("دستور"), ordering="recipe__title")
    def recipe_label(self, obj):
        return obj.recipe

    @admin.display(description=_("ترتیب"), ordering="position")
    def position_label(self, obj):
        return obj.position

    @admin.display(description=_("منبع"), ordering="source_type")
    def source_type_label(self, obj):
        return _cup_rule_source_type_label(obj.source_type)

    @admin.display(description=_("تعداد"), ordering="quantity")
    def quantity_label(self, obj):
        return obj.quantity

    @admin.display(description=_("الزامی"), ordering="required")
    def required_label(self, obj):
        return _inventory_required_label(obj.required)

    @admin.display(description=_("پنل"), ordering="panel__name")
    def panel_label(self, obj):
        return obj.panel or "-"

    @admin.display(description=_("مخزن کانفیگ"), ordering="inventory_pool__title")
    def inventory_pool_label(self, obj):
        return obj.inventory_pool or "-"

    @admin.display(description=_("نوع تخصیص"), ordering="allocation_mode")
    def allocation_mode_label(self, obj):
        return inventory_allocation_mode_label(obj.allocation_mode) if obj.allocation_mode else "-"

    @admin.display(description=_("فعال"), boolean=True, ordering="is_active")
    def active_status(self, obj):
        return obj.is_active


@admin.register(ConfigLink)
class ConfigLinkAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "protocol",
        "masked_raw_link",
        "remark",
        "host",
        "port",
        "source_type",
        "source_panel",
        "source_inbound",
        "external_feed",
        "vpn_client",
        "is_active",
        "created_at",
    )
    list_filter = ("protocol", "source_type", "is_active", "source_panel", "source_inbound", "external_feed", "created_at")
    search_fields = (
        "raw_link",
        "normalized_hash",
        "remark",
        "host",
        "vpn_client__username",
        "vpn_client__xui_email",
        "vpn_client__order__order_tracking_code",
    )
    autocomplete_fields = ("source_panel", "source_inbound", "vpn_client", "external_feed")
    readonly_fields = (
        "normalized_link_masked",
        "normalized_hash",
        "protocol",
        "host",
        "port",
        "masked_raw_link",
        "raw_link_reveal_controls",
        "raw_link_change_warning",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Link"),
            {
                "fields": (
                    "raw_link",
                    "raw_link_change_warning",
                    "raw_link_reveal_controls",
                    "masked_raw_link",
                    "protocol",
                    "remark",
                    "host",
                    "port",
                    "is_active",
                )
            },
        ),
        (
            _("Source"),
            {
                "fields": (
                    "source_type",
                    "source_panel",
                    "source_inbound",
                    "external_feed",
                    "vpn_client",
                )
            },
        ),
        (
            _("Metadata"),
            {
                "classes": ("collapse",),
                "fields": (
                    "normalized_link_masked",
                    "normalized_hash",
                    "metadata",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    class Media:
        js = ("admin/config_link_reveal.js",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj=obj, **kwargs)
        return _apply_admin_form_labels(
            form,
            {
                "raw_link": _("لینک کانفیگ"),
                "raw_link_change_warning": _("هشدار"),
                "raw_link_reveal_controls": _("نمایش / کپی کنترل‌شده"),
                "remark": _("نام/Remark"),
                "is_active": _("فعال"),
                "metadata": _("Metadata"),
                "external_feed": _("Dynamic source"),
            },
        )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "raw_link" and formfield:
            return _style_raw_link_secret_widget(formfield)
        return formfield

    @admin.display(description=_("Config link"))
    def masked_raw_link(self, obj):
        if not obj or not obj.raw_link:
            return "-"
        return mask_config_link_for_display(obj.raw_link)

    @admin.display(description=_("نمایش / کپی کنترل‌شده"))
    def raw_link_reveal_controls(self, obj):
        return _raw_link_reveal_controls(obj, masked_link=self.masked_raw_link(obj))

    @admin.display(description=_("هشدار"))
    def raw_link_change_warning(self, obj):
        return _("تغییر این لینک روی خروجی Cupهایی که از آن استفاده می‌کنند اثر می‌گذارد.")

    @admin.display(description=_("Normalized link"))
    def normalized_link_masked(self, obj):
        if not obj or not obj.normalized_link:
            return "-"
        return mask_config_link_for_display(obj.normalized_link)

    def save_model(self, request, obj, form, change):
        parsed = _apply_parsed_link_fields(obj, form=form, change=change)
        super().save_model(request, obj, form, change)
        if not change or "raw_link" in (getattr(form, "changed_data", []) or []):
            messages.success(
                request,
                _("لینک دوباره parse شد: پروتکل %(protocol)s، میزبان %(host)s، پایان هش %(suffix)s.")
                % {"protocol": parsed.protocol, "host": parsed.host or "-", "suffix": (parsed.normalized_hash or "")[-8:] or "-"},
            )


class SubscriptionCupItemInline(admin.TabularInline):
    model = CupItem
    extra = 0
    show_change_link = True
    autocomplete_fields = ("config_link",)
    fields = (
        "position",
        "is_active",
        "added_reason",
        "config_link",
        "config_link_protocol",
        "masked_config_link",
        "created_at",
    )
    readonly_fields = ("config_link_protocol", "masked_config_link", "created_at")

    @admin.display(description=_("Protocol"))
    def config_link_protocol(self, obj):
        return getattr(getattr(obj, "config_link", None), "protocol", "") or "-"

    @admin.display(description=_("Config"))
    def masked_config_link(self, obj):
        config_link = getattr(obj, "config_link", None)
        if not config_link:
            return "-"
        suffix = (config_link.normalized_hash or "")[-8:] or str(config_link.pk or "-")
        return _("Config link saved (hidden), hash ending %(suffix)s") % {"suffix": suffix}


@admin.register(SubscriptionCup)
class SubscriptionCupAdmin(ImportExportModelAdmin):
    inlines = (SubscriptionCupItemInline,)
    list_display = (
        "id",
        "title",
        "customer",
        "order",
        "vpn_client",
        "status_badge",
        "item_count",
        "masked_subscription_url",
        "cup_center_link",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "expires_at", "plan")
    search_fields = (
        "title",
        "token",
        "order__order_tracking_code",
        "vpn_client__username",
        "vpn_client__xui_email",
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
    )
    autocomplete_fields = ("customer", "order", "plan", "vpn_client")
    readonly_fields = (
        "token",
        "subscription_url",
        "masked_subscription_url",
        "cup_center_link",
        "item_count",
        "metadata_safe_summary",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Summary"),
            {
                "fields": (
                    "title",
                    "status",
                    "token",
                    "subscription_url",
                    "masked_subscription_url",
                    "cup_center_link",
                    "item_count",
                    "created_at",
                    "updated_at",
                )
            },
        ),
        (
            _("Owner"),
            {
                "fields": (
                    "customer",
                    "order",
                    "plan",
                    "vpn_client",
                )
            },
        ),
        (
            _("Limits"),
            {
                "fields": (
                    "expires_at",
                    "traffic_limit_bytes",
                    "device_limit",
                )
            },
        ),
        (
            _("Metadata"),
            {
                "classes": ("collapse",),
                "fields": ("metadata_safe_summary",),
            },
        ),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("customer", "order", "plan", "vpn_client")
            .annotate(admin_item_count=Count("items", filter=Q(items__is_active=True), distinct=True))
        )

    def qadmin_badge(self, label, tone="secondary"):
        css_class = {
            "success": "bg-success",
            "warning": "bg-warning text-dark",
            "danger": "bg-danger",
            "secondary": "bg-secondary",
        }.get(tone, "bg-secondary")
        return format_html('<span class="badge {}">{}</span>', css_class, label)

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        if obj.status == SubscriptionCup.Status.ACTIVE and not obj.is_expired:
            return self.qadmin_badge(_("Active"), "success")
        if obj.status == SubscriptionCup.Status.DISABLED:
            return self.qadmin_badge(_("Disabled"), "danger")
        return self.qadmin_badge(_("Expired"), "warning")

    @admin.display(description=_("Items"), ordering="admin_item_count")
    def item_count(self, obj):
        count = getattr(obj, "admin_item_count", None)
        return count if count is not None else obj.items.filter(is_active=True).count()

    @admin.display(description=_("Subscription URL"))
    def subscription_url(self, obj):
        if not obj or not obj.pk:
            return "-"
        return format_html('<code dir="ltr">{}</code>', build_subscription_cup_url(obj))

    @admin.display(description=_("Masked URL"))
    def masked_subscription_url(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = build_subscription_cup_url(obj)
        return format_html('<code dir="ltr">{}</code>', mask_subscription_url(url, obj.token))

    @admin.display(description=_("Cup Center"))
    def cup_center_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_store_cup_center_detail", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Manage Cup"))

    @admin.display(description=_("Safe metadata summary"))
    def metadata_safe_summary(self, obj):
        metadata = obj.metadata or {}
        rows = [
            ("external_panel_subscription_link_saved", metadata.get("external_panel_subscription_link_saved")),
            ("last_rebuilt_at", metadata.get("last_rebuilt_at")),
        ]
        rows = [(key, value) for key, value in rows if value not in (None, "", False)]
        if not rows:
            return "-"
        return format_html_join("", "<div><strong>{}</strong>: {}</div>", rows)


@admin.register(CupItem)
class CupItemAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "cup",
        "position",
        "config_link_protocol",
        "masked_config_link",
        "is_active",
        "added_reason",
        "created_at",
    )
    list_filter = ("is_active", "config_link__protocol", "added_reason", "created_at")
    search_fields = (
        "cup__token",
        "cup__title",
        "config_link__normalized_hash",
        "config_link__remark",
        "config_link__host",
    )
    autocomplete_fields = ("cup", "config_link")
    readonly_fields = ("config_link_protocol", "masked_config_link", "created_at", "updated_at")
    list_select_related = ("cup", "config_link")

    @admin.display(description=_("Protocol"), ordering="config_link__protocol")
    def config_link_protocol(self, obj):
        return getattr(getattr(obj, "config_link", None), "protocol", "") or "-"

    @admin.display(description=_("Config"))
    def masked_config_link(self, obj):
        config_link = getattr(obj, "config_link", None)
        if not config_link:
            return "-"
        suffix = (config_link.normalized_hash or "")[-8:] or str(config_link.pk or "-")
        return _("Config link saved (hidden), hash ending %(suffix)s") % {"suffix": suffix}


@admin.register(VPNClientActionLog)
class VPNClientActionLogAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "vpn_client",
        "customer",
        "actor_type",
        "actor_telegram_id",
        "action",
        "status",
        "panel",
        "inbound",
        "xui_identifier_masked",
        "created_at",
        "completed_at",
    )
    list_filter = ("actor_type", "action", "status", "panel", "inbound", "created_at")
    search_fields = (
        "vpn_client__username",
        "vpn_client__xui_email",
        "vpn_client__uuid",
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
        "actor_telegram_id",
        "xui_identifier_masked",
        "error_message",
    )
    date_hierarchy = "created_at"
    list_select_related = ("vpn_client", "customer", "panel", "inbound")
    readonly_fields = (
        "vpn_client",
        "customer",
        "actor_type",
        "actor_telegram_id",
        "action",
        "panel",
        "inbound",
        "xui_identifier_masked",
        "old_total_bytes",
        "new_total_bytes",
        "old_expiry_time",
        "new_expiry_time",
        "status",
        "error_message",
        "metadata",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(FreeTrialRequest)
class FreeTrialRequestAdmin(ImportExportModelAdmin):
    list_display = (
        "id",
        "customer",
        "telegram_user_id",
        "status",
        "panel",
        "inbound",
        "vpn_client",
        "traffic_gb",
        "duration_hours",
        "delivered_at",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "panel", "inbound", "created_at", "delivered_at")
    search_fields = (
        "telegram_user_id",
        "customer__username",
        "customer__phone_number",
        "customer__display_name",
        "vpn_client__username",
        "vpn_client__xui_email",
        "vpn_client__uuid",
        "config_link",
        "error_message",
    )
    autocomplete_fields = ("customer", "panel", "inbound", "vpn_client")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_select_related = ("customer", "panel", "inbound", "vpn_client")


class ReferralInline(admin.TabularInline):
    model = Referral
    fk_name = "referrer"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("referred_customer", "referral_code", "status", "first_order", "purchased_at", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class CustomerRewardInline(admin.TabularInline):
    model = CustomerReward
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("reward_type", "title", "status", "discount_code", "plan", "milestone", "earned_at", "used_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class ReferralRewardLedgerInline(admin.TabularInline):
    model = ReferralRewardLedger
    fk_name = "inviter"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "invited",
        "order",
        "reward_gb",
        "reward_duration_days",
        "status",
        "redeemed_config",
        "applied_traffic_gb",
        "applied_duration_days",
        "created_at",
        "redeemed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class CustomerResource(resources.ModelResource):
    referred_by = fields.Field(
        column_name="referred_by_id",
        attribute="referred_by",
        widget=ForeignKeyWidget(Customer, "id"),
    )

    class Meta:
        model = Customer
        import_id_fields = ("public_id",)
        fields = (
            "id",
            "public_id",
            "username",
            "phone_number",
            "display_name",
            "is_wholesale",
            "default_discount_percent",
            "referral_code",
            "referred_by",
            "referral_code_used",
            "referred_at",
            "last_seen_at",
            "is_active",
            "created_at",
            "updated_at",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True


@admin.register(Customer)
class CustomerAdmin(ImportExportModelAdmin):
    resource_class = CustomerResource
    list_display = (
        "customer_display",
        "masked_phone_display",
        "masked_email_display",
        "telegram_connection_status",
        "active_clients_total",
        "orders_total",
        "open_support_total",
        "last_order_date",
        "customer_review_link",
        "customer_message_link",
    )
    list_filter = ("is_active", "is_wholesale", "created_at", "last_seen_at")
    search_fields = (
        "display_name",
        "username",
        "phone_number",
        "referral_code",
        "referred_by__referral_code",
    )
    readonly_fields = (
        "public_id",
        "masked_public_id",
        "customer_display",
        "masked_phone_display",
        "masked_email_display",
        "referral_code",
        "referral_code_used",
        "referred_at",
        "first_ip",
        "last_ip",
        "user_agent_hash",
        "telegram_connection_status",
        "telegram_bot_users_total",
        "latest_web_telegram_token_status",
        "active_clients_total",
        "orders_total",
        "open_support_total",
        "last_order_date",
        "customer_review_link",
        "customer_message_link",
        "created_at",
        "updated_at",
        "last_seen_at",
    )
    fieldsets = (
        (
            _("Summary"),
            {
                "fields": (
                    "masked_public_id",
                    "display_name",
                    "username",
                    "masked_phone_display",
                    "masked_email_display",
                    "is_active",
                    "customer_review_link",
                    "telegram_connection_status",
                    "active_clients_total",
                    "orders_total",
                    "open_support_total",
                    "last_order_date",
                    "customer_message_link",
                )
            },
        ),
        (
            _("Wholesale / Referrals"),
            {
                "fields": (
                    "is_wholesale",
                    "default_discount_percent",
                    "referral_code",
                    "referred_by",
                    "referral_code_used",
                    "referred_at",
                )
            },
        ),
        (
            _("Technical"),
            {
                "classes": ("collapse",),
                "fields": (
                    "first_ip",
                    "last_ip",
                    "user_agent_hash",
                    "latest_web_telegram_token_status",
                    "telegram_bot_users_total",
                    "created_at",
                    "updated_at",
                    "last_seen_at",
                ),
            },
        ),
    )
    autocomplete_fields = ("referred_by",)
    date_hierarchy = "created_at"
    list_select_related = ("referred_by",)
    inlines = (ReferralInline, CustomerRewardInline, ReferralRewardLedgerInline)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                admin_orders_count=Count("orders", distinct=True),
                admin_active_clients_count=Count(
                    "orders__vpn_clients",
                    filter=Q(orders__vpn_clients__status=VPNClient.Status.ACTIVE),
                    distinct=True,
                ),
                admin_last_order_at=Max("orders__created_at"),
                admin_invited_count=Count("referrals_made", distinct=True),
                admin_successful_referrals_count=Count(
                    "referrals_made",
                    filter=Q(referrals_made__status=Referral.Status.PURCHASED),
                    distinct=True,
                ),
                admin_available_referral_gb=Sum(
                    "referral_gb_rewards__reward_gb",
                    filter=Q(referral_gb_rewards__status=ReferralRewardLedger.Status.AVAILABLE),
                ),
                admin_available_referral_packages=Count(
                    "referral_gb_rewards",
                    filter=Q(referral_gb_rewards__status=ReferralRewardLedger.Status.AVAILABLE),
                    distinct=True,
                ),
                admin_available_referral_days=Sum(
                    "referral_gb_rewards__reward_duration_days",
                    filter=Q(referral_gb_rewards__status=ReferralRewardLedger.Status.AVAILABLE),
                ),
                admin_redeemed_referral_gb=Sum(
                    "referral_gb_rewards__reward_gb",
                    filter=Q(referral_gb_rewards__status=ReferralRewardLedger.Status.REDEEMED),
                ),
                admin_last_reminder_sent_at=Max("vpn_reminder_logs__sent_at"),
                admin_telegram_bot_users_count=Count(
                    "bot_users",
                    filter=Q(
                        bot_users__is_active=True,
                        bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                    ),
                    distinct=True,
                ),
                admin_latest_web_telegram_token_status=Subquery(
                    WebTelegramLinkToken.objects.filter(customer=OuterRef("pk"))
                    .order_by("-created_at")
                    .values("status")[:1]
                ),
                admin_open_support_count=Count(
                    "support_conversations",
                    filter=~Q(support_conversations__status=SupportConversation.Status.CLOSED),
                    distinct=True,
                ),
            )
        )

    @admin.display(description=_("Public ID"))
    def masked_public_id(self, obj):
        return admin_mask_identifier(obj.public_id)

    @admin.display(description=_("Customer"), ordering="display_name")
    def customer_display(self, obj):
        label = admin_safe_customer_label(obj)
        url = reverse("admin_store_customer_review", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description=_("Phone"), ordering="phone_number")
    def masked_phone_display(self, obj):
        return admin_mask_phone(obj.phone_number) or "-"

    @admin.display(description=_("Email"))
    def masked_email_display(self, obj):
        return "-"

    @admin.display(description=_("Active clients"), ordering="admin_active_clients_count")
    def active_clients_total(self, obj):
        value = getattr(obj, "admin_active_clients_count", None)
        if value is not None:
            return value
        return obj.orders.filter(vpn_clients__status=VPNClient.Status.ACTIVE).distinct().count()

    @admin.display(description=_("Last order"), ordering="admin_last_order_at")
    def last_order_date(self, obj):
        value = getattr(obj, "admin_last_order_at", None)
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "-"

    @admin.display(description=_("Review"))
    def customer_review_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin_store_customer_review", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Review"))

    @admin.display(description=_("Message"))
    def customer_message_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        return format_html('<a class="button" href="{}">{}</a>', customer_message_url(obj), _("Message"))

    @admin.display(description=_("Open support"), ordering="admin_open_support_count")
    def open_support_total(self, obj):
        value = getattr(obj, "admin_open_support_count", None)
        if value is not None:
            return value
        return obj.support_conversations.exclude(status=SupportConversation.Status.CLOSED).count()

    @admin.display(description=_("Orders"))
    def orders_total(self, obj):
        value = getattr(obj, "admin_orders_count", None)
        return value if value is not None else obj.orders.count()

    @admin.display(description=_("Last reminder"), ordering="admin_last_reminder_sent_at")
    def last_reminder_sent_at(self, obj):
        return getattr(obj, "admin_last_reminder_sent_at", None) or "-"

    @admin.display(description=_("Invited"))
    def invited_total(self, obj):
        value = getattr(obj, "admin_invited_count", None)
        return value if value is not None else obj.referrals_made.count()

    @admin.display(description=_("Successful"))
    def successful_referrals_total(self, obj):
        value = getattr(obj, "admin_successful_referrals_count", None)
        return value if value is not None else obj.referrals_made.filter(status=Referral.Status.PURCHASED).count()

    @admin.display(description=_("Available GB"))
    def available_referral_gb_total(self, obj):
        return getattr(obj, "admin_available_referral_gb", None) or Decimal("0")

    @admin.display(description=_("Available packages"))
    def available_referral_packages_total(self, obj):
        return getattr(obj, "admin_available_referral_packages", None) or 0

    @admin.display(description=_("Available days"))
    def available_referral_duration_total(self, obj):
        return getattr(obj, "admin_available_referral_days", None) or 0

    @admin.display(description=_("Redeemed GB"))
    def redeemed_referral_gb_total(self, obj):
        return getattr(obj, "admin_redeemed_referral_gb", None) or Decimal("0")

    @admin.display(description=_("Telegram linked"))
    def telegram_connection_status(self, obj):
        count = getattr(obj, "admin_telegram_bot_users_count", None)
        if count is None:
            count = obj.bot_users.filter(
                is_active=True,
                bot_config__provider=BotConfiguration.Provider.TELEGRAM,
            ).count()
        return _("Connected") if count else _("Not connected")

    @admin.display(description=_("Telegram BotUsers"))
    def telegram_bot_users_total(self, obj):
        count = getattr(obj, "admin_telegram_bot_users_count", None)
        if count is not None:
            return count
        return obj.bot_users.filter(
            is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        ).count()

    @admin.display(description=_("Latest link token"))
    def latest_web_telegram_token_status(self, obj):
        return getattr(obj, "admin_latest_web_telegram_token_status", None) or "-"


ANALYTICS_PERIOD_LABELS = {
    PERIOD_TODAY: _("Today"),
    PERIOD_LAST_7_DAYS: _("Last 7 days"),
    PERIOD_LAST_30_DAYS: _("Last 30 days"),
    PERIOD_CURRENT_MONTH: _("Current month"),
    PERIOD_ALL_TIME: _("All time"),
}

ANALYTICS_SEGMENT_LABELS = {
    SEGMENT_ALL: _("All"),
    SEGMENT_ACTIVE_CUSTOMERS: _("Active customers"),
    SEGMENT_ACTIVE_CONFIG: _("Customers with active config"),
    SEGMENT_CUSTOMERS_WITHOUT_ORDER: _("Customers without order"),
    SEGMENT_LOYAL: _("Loyal"),
    SEGMENT_GOOD: _("Good"),
    SEGMENT_TOP_BUYER: _("Top buyer"),
    SEGMENT_TOP_REFERRER: _("Top referrer"),
    SEGMENT_INACTIVE: _("Inactive"),
    SEGMENT_NO_ORDER: _("No order"),
    SEGMENT_NEW_CUSTOMER: _("New customer"),
}

ANALYTICS_METRIC_LABELS = {
    "amount": _("Purchase amount"),
    "orders": _("Order count"),
    "renewals": _("Renewals"),
    "volume": _("Purchased volume"),
}


class CustomerAnalyticsPeriodFilter(admin.SimpleListFilter):
    title = _("period")
    parameter_name = "analytics_period"

    def lookups(self, request, model_admin):
        return tuple(ANALYTICS_PERIOD_LABELS.items())

    def queryset(self, request, queryset):
        return queryset


class CustomerAnalyticsSegmentFilter(admin.SimpleListFilter):
    title = _("segment")
    parameter_name = "analytics_segment"

    def lookups(self, request, model_admin):
        return tuple(ANALYTICS_SEGMENT_LABELS.items())

    def queryset(self, request, queryset):
        return queryset


class CustomerAnalyticsMetricFilter(admin.SimpleListFilter):
    title = _("metric")
    parameter_name = "analytics_metric"

    def lookups(self, request, model_admin):
        return tuple(ANALYTICS_METRIC_LABELS.items())

    def queryset(self, request, queryset):
        return queryset


@admin.register(CustomerAnalyticsReport)
class CustomerAnalyticsReportAdmin(admin.ModelAdmin):
    list_display = (
        "customer_display",
        "contact_display",
        "successful_orders_display",
        "total_paid_display",
        "renewal_orders_display",
        "purchased_volume_display",
        "successful_referrals_display",
        "last_purchase_display",
        "segment_display",
    )
    list_filter = (
        CustomerAnalyticsPeriodFilter,
        CustomerAnalyticsSegmentFilter,
        CustomerAnalyticsMetricFilter,
    )
    search_fields = (
        "display_name",
        "username",
        "phone_number",
        "referral_code",
        "bot_users__provider_user_id",
        "bot_users__chat_id",
    )
    actions = ("export_customer_analytics_csv",)
    list_per_page = 50
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _period_range(self, request):
        period = request.GET.get("analytics_period") or PERIOD_LAST_30_DAYS
        try:
            return get_period_range(period)
        except ValueError:
            return get_period_range(PERIOD_LAST_30_DAYS)

    def _segment(self, request):
        return request.GET.get("analytics_segment") or SEGMENT_ALL

    def _metric(self, request):
        metric = request.GET.get("analytics_metric") or "amount"
        return metric if metric in METRIC_FIELDS else "amount"

    def _order_queryset(self, queryset, metric):
        field_name = METRIC_FIELDS.get(metric, METRIC_FIELDS["amount"])
        return queryset.order_by(f"-{field_name}", "-analytics_total_paid_amount", "display_name", "pk")

    def get_queryset(self, request):
        date_from, date_to = self._period_range(request)
        segment = self._segment(request)
        metric = self._metric(request)

        queryset = super().get_queryset(request)
        if segment != SEGMENT_ALL:
            segment_queryset = get_customers_by_segment(segment, date_from=date_from, date_to=date_to)
            queryset = queryset.filter(pk__in=Subquery(segment_queryset.values("pk")))

        queryset = annotate_customer_queryset(queryset, date_from=date_from, date_to=date_to)
        queryset = queryset.annotate(
            analytics_bot_user_id=Subquery(
                BotUser.objects.filter(customer=OuterRef("pk"))
                .order_by("created_at", "pk")
                .values("provider_user_id")[:1]
            )
        )

        self._analytics_top_buyer_ids = set(
            get_customers_by_segment(SEGMENT_TOP_BUYER, date_from=date_from, date_to=date_to)
            .values_list("pk", flat=True)
        )
        self._analytics_top_referrer_ids = set(
            get_customers_by_segment(SEGMENT_TOP_REFERRER, date_from=date_from, date_to=date_to)
            .values_list("pk", flat=True)
        )
        return self._order_queryset(queryset, metric)

    @admin.display(description=_("Customer"), ordering="display_name")
    def customer_display(self, obj):
        url = reverse("admin:store_customer_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.display_name or obj.username or obj.phone_number or obj.pk)

    @admin.display(description=_("Phone / Telegram"))
    def contact_display(self, obj):
        values = [value for value in (obj.phone_number, obj.username, getattr(obj, "analytics_bot_user_id", "")) if value]
        return " / ".join(values) if values else "-"

    @admin.display(description=_("Successful orders"), ordering="analytics_successful_orders_count")
    def successful_orders_display(self, obj):
        return getattr(obj, "analytics_successful_orders_count", 0) or 0

    @admin.display(description=_("Total amount"), ordering="analytics_total_paid_amount")
    def total_paid_display(self, obj):
        return f"{int(getattr(obj, 'analytics_total_paid_amount', 0) or 0):,}"

    @admin.display(description=_("Renewals"), ordering="analytics_renewal_orders_count")
    def renewal_orders_display(self, obj):
        return getattr(obj, "analytics_renewal_orders_count", 0) or 0

    @admin.display(description=_("Purchased GB"), ordering="analytics_total_purchased_gb")
    def purchased_volume_display(self, obj):
        value = getattr(obj, "analytics_total_purchased_gb", Decimal("0")) or Decimal("0")
        return value.normalize() if isinstance(value, Decimal) else value

    @admin.display(description=_("Successful referrals"), ordering="analytics_successful_referrals_count")
    def successful_referrals_display(self, obj):
        return getattr(obj, "analytics_successful_referrals_count", 0) or 0

    @admin.display(description=_("Last purchase"), ordering="analytics_last_purchase_at")
    def last_purchase_display(self, obj):
        value = getattr(obj, "analytics_last_purchase_at", None)
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "-"

    @admin.display(description=_("Segment"))
    def segment_display(self, obj):
        segment = self._segment_for_annotated_customer(obj)
        return ANALYTICS_SEGMENT_LABELS.get(segment, segment)

    def _segment_for_annotated_customer(self, obj):
        if obj.pk in getattr(self, "_analytics_top_buyer_ids", set()):
            return SEGMENT_TOP_BUYER
        if obj.pk in getattr(self, "_analytics_top_referrer_ids", set()):
            return SEGMENT_TOP_REFERRER
        if (
            (getattr(obj, "analytics_successful_orders_count", 0) or 0) >= loyal_customer_min_orders_30d()
            or (getattr(obj, "analytics_renewal_orders_count", 0) or 0) >= 1
        ):
            return SEGMENT_LOYAL
        if (getattr(obj, "analytics_total_paid_amount", 0) or 0) >= good_customer_min_total_amount():
            return SEGMENT_GOOD
        if not getattr(obj, "analytics_successful_orders_count", 0):
            return SEGMENT_NO_ORDER
        inactive_cutoff = timezone.now() - timedelta(days=inactive_customer_days())
        if getattr(obj, "analytics_last_purchase_at", None) and obj.analytics_last_purchase_at < inactive_cutoff:
            return SEGMENT_INACTIVE
        return SEGMENT_ACTIVE_CUSTOMERS

    @admin.action(description=_("Export selected analytics as CSV"))
    def export_customer_analytics_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="customer-analytics.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "customer",
                "phone",
                "username",
                "telegram_id",
                "successful_orders_count",
                "total_paid_amount",
                "renewal_orders_count",
                "total_purchased_gb",
                "successful_referrals_count",
                "last_purchase_at",
                "segment",
            ]
        )
        for customer in queryset:
            last_purchase = getattr(customer, "analytics_last_purchase_at", None)
            writer.writerow(
                [
                    customer.display_name,
                    customer.phone_number,
                    customer.username,
                    getattr(customer, "analytics_bot_user_id", ""),
                    getattr(customer, "analytics_successful_orders_count", 0) or 0,
                    getattr(customer, "analytics_total_paid_amount", 0) or 0,
                    getattr(customer, "analytics_renewal_orders_count", 0) or 0,
                    getattr(customer, "analytics_total_purchased_gb", Decimal("0")) or Decimal("0"),
                    getattr(customer, "analytics_successful_referrals_count", 0) or 0,
                    timezone.localtime(last_purchase).isoformat() if last_purchase else "",
                    self._segment_for_annotated_customer(customer),
                ]
            )
        return response


class LegacyWizWizImportJobForm(forms.ModelForm):
    class Meta:
        model = LegacyWizWizImportJob
        fields = "__all__"

    def clean_uploaded_file(self):
        uploaded_file = self.cleaned_data.get("uploaded_file")
        if not uploaded_file:
            if self.instance and self.instance.pk:
                return uploaded_file
            raise ValidationError(_("Please upload a WizWiz SQL backup file."))
        if getattr(uploaded_file, "size", 0) <= 0:
            raise ValidationError(_("Uploaded file is empty."))
        if Path(uploaded_file.name or "").suffix.lower() != ".sql":
            raise ValidationError(_("Only .sql backup files are accepted."))
        return uploaded_file


class WizWizSimpleRestoreForm(forms.Form):
    sql_file = forms.FileField(label=_("SQL backup file"))

    def clean_sql_file(self):
        uploaded_file = self.cleaned_data.get("sql_file")
        if not uploaded_file:
            raise ValidationError(_("Please upload a WizWiz SQL backup file."))
        if getattr(uploaded_file, "size", 0) <= 0:
            raise ValidationError(_("Uploaded file is empty."))
        if Path(uploaded_file.name or "").suffix.lower() != ".sql":
            raise ValidationError(_("Only .sql backup files are accepted."))
        return uploaded_file


class WizWizSimpleRestoreMessageForm(forms.Form):
    text = forms.CharField(
        label=_("پیام به کاربران import شده"),
        widget=forms.Textarea(attrs={"rows": 7}),
    )

    def clean_text(self):
        return validate_legacy_import_message_text(self.cleaned_data.get("text"))


class LegacyWizWizImportMessageForm(forms.Form):
    text = forms.CharField(
        label=_("Message text"),
        widget=forms.Textarea(attrs={"rows": 7}),
        help_text=_("This message is sent only to BotUsers created/linked by this import job."),
    )
    confirm_large_send = forms.BooleanField(
        label=_("I confirm sending to more than 1000 recipients"),
        required=False,
    )

    def clean_text(self):
        return validate_legacy_import_message_text(self.cleaned_data.get("text"))


LEGACY_WIZWIZ_JOB_SUMMARY_FIELDS = (
    "parsed_users_count",
    "valid_users_count",
    "invalid_rows_count",
    "duplicate_in_file_count",
    "existing_bot_users_count",
    "existing_customers_count",
    "would_create_bot_users_count",
    "would_create_customers_count",
    "admins_count",
    "agents_count",
    "wallet_positive_count",
    "created_bot_users_count",
    "created_customers_count",
    "linked_existing_count",
    "updated_existing_count",
    "skipped_count",
    "failed_count",
)


def _legacy_wizwiz_summary_items(job):
    return (
        (_("Parsed users"), job.parsed_users_count),
        (_("Valid users"), job.valid_users_count),
        (_("Invalid rows"), job.invalid_rows_count),
        (_("Duplicate rows in file"), job.duplicate_in_file_count),
        (_("Existing BotUsers"), job.existing_bot_users_count),
        (_("Existing Customers"), job.existing_customers_count),
        (_("Would create BotUsers"), job.would_create_bot_users_count),
        (_("Would create Customers"), job.would_create_customers_count),
        (_("Admins"), job.admins_count),
        (_("Agents"), job.agents_count),
        (_("Wallet positive"), job.wallet_positive_count),
        (_("Created BotUsers"), job.created_bot_users_count),
        (_("Created Customers"), job.created_customers_count),
        (_("Linked existing"), job.linked_existing_count),
        (_("Updated existing"), job.updated_existing_count),
        (_("Skipped"), job.skipped_count),
        (_("Failed"), job.failed_count),
    )


@admin.register(LegacyWizWizImportJob)
class LegacyWizWizImportJobAdmin(ImportExportModelAdmin):
    form = LegacyWizWizImportJobForm
    change_form_template = "admin/store/legacywizwizimportjob/change_form.html"
    list_display = (
        "id",
        "title",
        "original_filename",
        "status",
        "parsed_users_count",
        "valid_users_count",
        "created_bot_users_count",
        "created_customers_count",
        "existing_bot_users_count",
        "failed_count",
        "created_at",
        "analyzed_at",
        "applied_at",
    )
    list_filter = ("status", "source", "created_at", "applied_at")
    search_fields = ("title", "original_filename", "file_sha256", "metadata", "error_message")
    date_hierarchy = "created_at"
    autocomplete_fields = ("created_by",)
    readonly_fields = (
        "private_file_display",
        "rows_link",
        "status",
        "file_sha256",
        "file_size",
        *LEGACY_WIZWIZ_JOB_SUMMARY_FIELDS,
        "error_message",
        "metadata",
        "analyzed_at",
        "applied_at",
        "failed_at",
        "created_at",
        "updated_at",
    )
    actions = ("analyze_selected", "cancel_selected", "export_rows_csv")

    def get_model_perms(self, request):
        return {}

    def has_module_permission(self, request):
        return False

    def has_view_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fieldsets(self, request, obj=None):
        file_field = "uploaded_file" if obj is None else "private_file_display"
        return (
            (
                _("Upload"),
                {
                    "fields": (
                        "source",
                        "title",
                        file_field,
                        "original_filename",
                        "file_size",
                        "file_sha256",
                        "created_by",
                        "status",
                        "mode",
                    )
                },
            ),
            (
                _("Options"),
                {
                    "fields": (
                        "skip_admins",
                        "import_agents",
                        "only_agents",
                        "only_wallet_positive",
                        "update_existing",
                        "create_customers",
                    )
                },
            ),
            (_("Analyze summary"), {"fields": LEGACY_WIZWIZ_JOB_SUMMARY_FIELDS[:11]}),
            (_("Apply summary"), {"fields": LEGACY_WIZWIZ_JOB_SUMMARY_FIELDS[11:]}),
            (
                _("Status"),
                {
                    "fields": (
                        "rows_link",
                        "error_message",
                        "metadata",
                        "created_at",
                        "updated_at",
                        "analyzed_at",
                        "applied_at",
                        "failed_at",
                    )
                },
            ),
        )

    def save_model(self, request, obj, form, change):
        incoming_file = form.cleaned_data.get("uploaded_file")
        if incoming_file:
            obj.original_filename = Path(incoming_file.name or "").name
            obj.file_size = getattr(incoming_file, "size", 0) or 0
        if not obj.created_by_id and request.user.is_authenticated:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        if obj.uploaded_file:
            changed_fields = []
            if not obj.original_filename:
                obj.original_filename = Path(obj.uploaded_file.name or "").name
                changed_fields.append("original_filename")
            try:
                file_size = Path(obj.uploaded_file.path).stat().st_size
                file_sha256 = calculate_file_sha256(obj.uploaded_file.path)
            except (OSError, NotImplementedError):
                file_size = obj.file_size
                file_sha256 = obj.file_sha256
            if file_size != obj.file_size:
                obj.file_size = file_size
                changed_fields.append("file_size")
            if file_sha256 and file_sha256 != obj.file_sha256:
                obj.file_sha256 = file_sha256
                changed_fields.append("file_sha256")
            if changed_fields:
                obj.save(update_fields=[*changed_fields, "updated_at"])
            if obj.file_sha256:
                duplicate = (
                    LegacyWizWizImportJob.objects.filter(file_sha256=obj.file_sha256)
                    .exclude(pk=obj.pk)
                    .order_by("-created_at")
                    .first()
                )
                if duplicate:
                    messages.warning(
                        request,
                        _("A job with the same file SHA256 already exists: #%(id)s (%(status)s).")
                        % {"id": duplicate.pk, "status": duplicate.get_status_display()},
                    )

    @admin.display(description=_("Private file"))
    def private_file_display(self, obj):
        if not obj or not obj.uploaded_file:
            return "-"
        return _("%(name)s (%(size)s bytes, stored privately)") % {
            "name": obj.original_filename or Path(obj.uploaded_file.name).name,
            "size": f"{obj.file_size:,}",
        }

    @admin.display(description=_("Rows"))
    def rows_link(self, obj):
        if not obj or not obj.pk:
            return "-"
        url = reverse("admin:store_legacywizwizimportrow_changelist")
        query = urlencode({"job__id__exact": obj.pk})
        return format_html('<a href="{}?{}">{}</a>', url, query, _("View preview/import rows"))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/analyze/",
                self.admin_site.admin_view(self.analyze_view),
                name="store_legacywizwizimportjob_analyze",
            ),
            path(
                "<path:object_id>/apply/",
                self.admin_site.admin_view(self.apply_view),
                name="store_legacywizwizimportjob_apply",
            ),
            path(
                "<path:object_id>/export-csv/",
                self.admin_site.admin_view(self.export_csv_view),
                name="store_legacywizwizimportjob_export_csv",
            ),
            path(
                "<path:object_id>/message/",
                self.admin_site.admin_view(self.message_view),
                name="store_legacywizwizimportjob_message",
            ),
        ]
        return custom_urls + urls

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        job = self.get_object(request, object_id)
        extra_context.update(
            {
                "legacy_analyze_url": reverse("admin:store_legacywizwizimportjob_analyze", args=[object_id]),
                "legacy_apply_url": reverse("admin:store_legacywizwizimportjob_apply", args=[object_id]),
                "legacy_export_csv_url": reverse("admin:store_legacywizwizimportjob_export_csv", args=[object_id]),
                "legacy_message_url": reverse("admin:store_legacywizwizimportjob_message", args=[object_id]),
                "legacy_campaign_url": f"{reverse('admin_store_campaign_new')}?{urlencode({'audience': BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED})}",
                "legacy_message_form": LegacyWizWizImportMessageForm(),
            }
        )
        if job:
            extra_context.update(
                {
                    "legacy_summary_items": _legacy_wizwiz_summary_items(job),
                    "legacy_message_batches": job.message_batches.order_by("-created_at", "-pk")[:10],
                }
            )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def _get_job_for_action(self, request, object_id):
        job = self.get_object(request, object_id)
        if not job:
            raise Http404
        if not self.has_change_permission(request, job):
            raise PermissionDenied
        return job

    def analyze_view(self, request, object_id):
        job = self._get_job_for_action(request, object_id)
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))
        try:
            summary = analyze_wizwiz_import_job(job)
        except Exception as exc:
            messages.error(request, _("Analyze failed: %(error)s") % {"error": exc})
        else:
            messages.success(
                request,
                _("Analyze complete: %(valid)s valid, %(invalid)s invalid, %(duplicates)s duplicate(s).")
                % {
                    "valid": summary["valid_users"],
                    "invalid": summary["invalid_rows"],
                    "duplicates": summary["duplicates"],
                },
            )
        return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))

    def apply_view(self, request, object_id):
        job = self._get_job_for_action(request, object_id)
        if request.method != "POST":
            context = {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "original": job,
                "job": job,
                "title": _("Confirm legacy WizWiz import apply"),
            }
            return TemplateResponse(request, "admin/store/legacywizwizimportjob/confirm_apply.html", context)
        try:
            summary = apply_wizwiz_import_job(job)
        except Exception as exc:
            messages.error(request, _("Apply failed: %(error)s") % {"error": exc})
        else:
            messages.success(
                request,
                _("Apply complete: %(created)s BotUser(s), %(customers)s Customer(s), %(failed)s failed row(s).")
                % {
                    "created": summary["created_bot_users"],
                    "customers": summary["created_customers"],
                    "failed": summary["failed"],
                },
            )
        return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))

    def export_csv_view(self, request, object_id):
        job = self._get_job_for_action(request, object_id)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="wizwiz-import-{job.pk}-rows.csv"'
        export_wizwiz_import_rows_csv(job, response)
        return response

    def message_view(self, request, object_id):
        job = self._get_job_for_action(request, object_id)
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))
        form = LegacyWizWizImportMessageForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Please fix the message form errors."))
            return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))
        try:
            preview = preview_legacy_import_message_batch(job, form.cleaned_data["text"])
            if request.POST.get("action") == "preview_message":
                messages.info(
                    request,
                    _("Message preview: %(total)s row(s), %(sendable)s sendable, %(skipped)s skipped without chat id.")
                    % {
                        "total": preview["recipients_total"],
                        "sendable": preview["sendable"],
                        "skipped": preview["skipped_no_chat_id"],
                    },
                )
                return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))
            if preview["requires_large_send_confirmation"] and not form.cleaned_data.get("confirm_large_send"):
                messages.error(request, _("Confirm large sends before sending to more than 1000 recipients."))
                return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))
            batch = create_legacy_import_message_batch(job, form.cleaned_data["text"], request.user)
            counts = send_legacy_import_message_batch(batch)
        except Exception as exc:
            messages.error(request, _("Message send failed: %(error)s") % {"error": exc})
        else:
            messages.success(
                request,
                _(
                    "Message batch processed: %(sent)s sent, %(failed)s failed, %(blocked)s blocked, %(skipped)s skipped."
                )
                % {
                    "sent": counts["sent"],
                    "failed": counts["failed"],
                    "blocked": counts["blocked"],
                    "skipped": counts["skipped_no_chat_id"],
                },
            )
        return HttpResponseRedirect(reverse("admin:store_legacywizwizimportjob_change", args=[job.pk]))

    @admin.action(description=_("Analyze selected WizWiz import jobs"))
    def analyze_selected(self, request, queryset):
        analyzed = 0
        for job in queryset:
            try:
                analyze_wizwiz_import_job(job)
            except Exception as exc:
                messages.error(request, _("#%(id)s analyze failed: %(error)s") % {"id": job.pk, "error": exc})
                continue
            analyzed += 1
        messages.success(request, _("%(count)s import job(s) analyzed.") % {"count": analyzed})

    @admin.action(description=_("Cancel selected WizWiz import jobs"))
    def cancel_selected(self, request, queryset):
        updated = queryset.exclude(
            status__in=(
                LegacyWizWizImportJob.Status.APPLIED,
                LegacyWizWizImportJob.Status.APPLYING,
            )
        ).update(status=LegacyWizWizImportJob.Status.CANCELLED, updated_at=timezone.now())
        messages.success(request, _("%(count)s import job(s) cancelled.") % {"count": updated})

    @admin.action(description=_("Export selected WizWiz import rows as CSV"))
    def export_rows_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="wizwiz-import-rows.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "job_id",
                "telegram_user_id_masked",
                "old_username",
                "status",
                "reason",
                "bot_user_id",
                "customer_id",
                "old_wallet",
                "old_is_agent",
                "old_is_admin",
                "created_at",
            ]
        )
        rows = LegacyWizWizImportRow.objects.filter(job__in=queryset).select_related("job", "bot_user", "customer")
        for row in rows.order_by("job_id", "created_at", "pk"):
            writer.writerow(
                [
                    row.job_id,
                    row.telegram_user_id_masked,
                    row.old_username,
                    row.status,
                    row.reason,
                    row.bot_user_id or "",
                    row.customer_id or "",
                    row.old_wallet if row.old_wallet is not None else "",
                    row.old_is_agent,
                    row.old_is_admin,
                    timezone.localtime(row.created_at).isoformat() if row.created_at else "",
                ]
            )
        return response


@admin.register(LegacyWizWizImportRow)
class LegacyWizWizImportRowAdmin(ImportExportModelAdmin):
    list_display = (
        "job",
        "telegram_user_id_masked",
        "old_username",
        "status",
        "reason",
        "bot_user",
        "customer",
        "old_wallet",
        "old_is_agent",
        "old_is_admin",
        "created_at",
    )
    list_filter = ("status", "old_is_agent", "old_is_admin", "job", "created_at")
    search_fields = (
        "telegram_user_id",
        "telegram_user_id_masked",
        "old_username",
        "reason",
        "customer__display_name",
        "customer__username",
        "bot_user__provider_user_id",
    )
    readonly_fields = (
        "job",
        "source",
        "legacy_pk",
        "telegram_user_id_masked",
        "old_name_masked",
        "old_username",
        "old_phone_masked",
        "old_wallet",
        "old_is_admin",
        "old_is_agent",
        "old_freetrial",
        "old_refcode",
        "old_refered_by",
        "status",
        "reason",
        "bot_user",
        "customer",
        "metadata",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields
    autocomplete_fields = ("job", "bot_user", "customer")
    date_hierarchy = "created_at"
    list_select_related = ("job", "bot_user", "customer")

    def get_model_perms(self, request):
        return {}

    def has_module_permission(self, request):
        return False

    def has_view_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class LegacyWizWizImportMessageRecipientInline(admin.TabularInline):
    model = LegacyWizWizImportMessageRecipient
    extra = 0
    can_delete = False
    fields = (
        "row",
        "telegram_user_id_masked",
        "bot_user",
        "customer",
        "status",
        "error_message",
        "sent_at",
        "created_at",
    )
    readonly_fields = fields
    ordering = ("created_at", "pk")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LegacyWizWizImportMessageBatch)
class LegacyWizWizImportMessageBatchAdmin(ImportExportModelAdmin):
    list_display = (
        "job",
        "status",
        "total_recipients",
        "sent_count",
        "failed_count",
        "skipped_count",
        "blocked_count",
        "created_at",
        "sent_at",
    )
    list_filter = ("status", "job", "created_at", "sent_at")
    search_fields = ("job__title", "job__original_filename", "text", "metadata")
    autocomplete_fields = ("job", "created_by")
    readonly_fields = (
        "job",
        "text",
        "status",
        "created_by",
        "total_recipients",
        "sent_count",
        "failed_count",
        "skipped_count",
        "blocked_count",
        "metadata",
        "sent_at",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields
    inlines = (LegacyWizWizImportMessageRecipientInline,)
    date_hierarchy = "created_at"

    def get_model_perms(self, request):
        return {}

    def has_module_permission(self, request):
        return False

    def has_view_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LegacyWizWizImportMessageRecipient)
class LegacyWizWizImportMessageRecipientAdmin(ImportExportModelAdmin):
    list_display = (
        "batch",
        "row",
        "status",
        "bot_user",
        "customer",
        "telegram_user_id_masked",
        "sent_at",
        "created_at",
    )
    list_filter = ("status", "batch", "created_at", "sent_at")
    search_fields = (
        "telegram_user_id_masked",
        "error_message",
        "batch__job__title",
        "customer__display_name",
        "customer__username",
        "bot_user__username",
    )
    autocomplete_fields = ("batch", "row", "bot_user", "customer")
    readonly_fields = (
        "batch",
        "row",
        "bot_user",
        "customer",
        "telegram_user_id_masked",
        "status",
        "error_message",
        "sent_at",
        "metadata",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields
    date_hierarchy = "created_at"
    list_select_related = ("batch", "row", "bot_user", "customer")

    def get_model_perms(self, request):
        return {}

    def has_module_permission(self, request):
        return False

    def has_view_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class BroadcastRecipientInline(admin.TabularInline):
    model = BroadcastRecipient
    extra = 0
    can_delete = False
    fields = ("customer", "channel", "masked_target", "status", "safe_error", "sent_at", "created_at")
    readonly_fields = fields
    ordering = ("created_at", "pk")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Target"))
    def masked_target(self, obj):
        return masked_target_label(obj)

    @admin.display(description=_("Safe error"))
    def safe_error(self, obj):
        return recipient_error_label(obj)


@admin.register(BroadcastMessage)
class BroadcastMessageAdmin(ImportExportModelAdmin):
    list_display = (
        "title",
        "audience_label",
        "status",
        "recipients_total",
        "sent_count",
        "failed_count",
        "created_at",
        "quick_review",
    )
    list_filter = ("status", "audience_type", "created_at")
    search_fields = ("title", "=id")
    date_hierarchy = "created_at"
    autocomplete_fields = ("store",)
    readonly_fields = (
        "audience_preview",
        "total_recipients",
        "success_count",
        "failed_count",
        "sent_at",
        "metadata",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Campaign"),
            {
                "fields": (
                    "store",
                    "title",
                    "audience_type",
                    "channel",
                    "status",
                    "scheduled_at",
                    "audience_preview",
                )
            },
        ),
        (
            _("Message"),
            {"fields": ("message_text",)},
        ),
        (
            _("Delivery"),
            {
                "fields": (
                    "total_recipients",
                    "success_count",
                    "failed_count",
                    "sent_at",
                )
            },
        ),
        (
            _("Advanced"),
            {"classes": ("collapse",), "fields": ("metadata",)},
        ),
        (
            _("Timestamps"),
            {"classes": ("collapse",), "fields": ("created_at", "updated_at")},
        ),
    )
    inlines = (BroadcastRecipientInline,)
    actions = ()

    @admin.display(description=_("Recipients"))
    def recipients_total(self, obj):
        return obj.total_recipients or obj.recipients.count()

    @admin.display(description=_("Audience"))
    def audience_label(self, obj):
        return obj.get_audience_type_display()

    @admin.display(description=_("Sent"))
    def sent_count(self, obj):
        return obj.success_count

    @admin.display(description=_("Review"))
    def quick_review(self, obj):
        return format_html('<a href="{}">{}</a>', campaign_review_url(obj), _("Review"))

    @admin.display(description=_("Audience preview"))
    def audience_preview(self, obj):
        if not obj:
            return _("Save the campaign to preview recipients.")
        try:
            total = len(resolve_campaign_recipients(obj))
        except Exception as exc:
            return _("Could not resolve recipients: %(error)s") % {"error": exc}
        return _("%(count)s recipient(s) found.") % {"count": f"{total:,}"}

    @admin.action(description=_("Queue selected campaigns"))
    def queue_selected(self, request, queryset):
        queued = 0
        for campaign in queryset:
            try:
                campaign.full_clean()
                create_campaign_recipients(campaign)
            except ValidationError as exc:
                messages.error(request, _("%(title)s was not queued: %(error)s") % {
                    "title": campaign.title,
                    "error": "; ".join(exc.messages),
                })
                continue
            if campaign.status != BroadcastMessage.Status.CANCELLED:
                campaign.status = BroadcastMessage.Status.QUEUED
                campaign.save(update_fields=["status", "updated_at"])
                queued += 1
        messages.success(request, _("%(count)s campaign(s) queued.") % {"count": queued})

    @admin.action(description=_("Cancel selected campaigns"))
    def cancel_selected(self, request, queryset):
        updated = queryset.exclude(status=BroadcastMessage.Status.SENT).update(
            status=BroadcastMessage.Status.CANCELLED,
            updated_at=timezone.now(),
        )
        messages.success(request, _("%(count)s campaign(s) cancelled.") % {"count": updated})


@admin.register(BroadcastRecipient)
class BroadcastRecipientAdmin(ImportExportModelAdmin):
    list_display = (
        "campaign",
        "target_summary",
        "status",
        "safe_error",
        "sent_at",
        "campaign_review",
    )
    list_filter = ("status", "channel", "campaign__status", "created_at", "sent_at")
    search_fields = (
        "campaign__title",
        "=id",
        "=campaign__id",
        "=customer__id",
    )
    autocomplete_fields = ("campaign", "customer")
    readonly_fields = (
        "campaign",
        "customer",
        "channel",
        "target_summary",
        "status",
        "safe_error",
        "sent_at",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields
    date_hierarchy = "created_at"
    list_select_related = ("campaign", "customer")

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("Target"))
    def target_summary(self, obj):
        return f"Customer #{obj.customer_id or '-'} / {masked_target_label(obj)}"

    @admin.display(description=_("Safe error"))
    def safe_error(self, obj):
        return recipient_error_label(obj)

    @admin.display(description=_("Review"))
    def campaign_review(self, obj):
        return format_html('<a href="{}">{}</a>', campaign_review_url(obj.campaign), _("Review"))


@admin.register(Referral)
class ReferralAdmin(ImportExportModelAdmin):
    list_display = (
        "referrer",
        "referred_customer",
        "referral_code",
        "status",
        "first_order",
        "purchased_at",
        "created_at",
    )
    list_filter = ("status", "created_at", "purchased_at")
    search_fields = (
        "referrer__username",
        "referrer__phone_number",
        "referrer__referral_code",
        "referred_customer__username",
        "referred_customer__phone_number",
        "referred_customer__referral_code",
        "referral_code",
        "first_order__order_tracking_code",
    )
    autocomplete_fields = ("referrer", "referred_customer", "first_order")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    list_select_related = ("referrer", "referred_customer", "first_order")


@admin.register(CustomerReward)
class CustomerRewardAdmin(ImportExportModelAdmin):
    list_display = (
        "customer",
        "reward_type",
        "title",
        "status",
        "discount_code",
        "plan",
        "milestone",
        "earned_at",
    )
    list_filter = ("reward_type", "status", "earned_at")
    search_fields = (
        "customer__username",
        "customer__phone_number",
        "customer__referral_code",
        "title",
        "discount_code__code",
    )
    autocomplete_fields = ("customer", "referral", "discount_code", "plan")
    readonly_fields = ("created_at", "updated_at", "earned_at")
    date_hierarchy = "earned_at"
    list_select_related = ("customer", "referral", "discount_code", "plan")


@admin.register(ReferralRewardLedger)
class ReferralRewardLedgerAdmin(ImportExportModelAdmin):
    list_display = (
        "inviter",
        "invited",
        "order",
        "reward_traffic_gb",
        "reward_duration_days",
        "status",
        "redeemed_config",
        "applied_traffic_gb",
        "applied_duration_days",
        "created_at",
        "redeemed_at",
    )
    list_filter = (
        "status",
        "created_at",
        ("inviter", admin.RelatedOnlyFieldListFilter),
        ("invited", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = (
        "inviter__display_name",
        "inviter__username",
        "inviter__phone_number",
        "inviter__referral_code",
        "invited__display_name",
        "invited__username",
        "invited__phone_number",
        "invited__referral_code",
        "order__order_tracking_code",
        "redeemed_config__username",
        "redeemed_config__xui_email",
    )
    autocomplete_fields = ("inviter", "invited", "order", "redeemed_config")
    readonly_fields = ("created_at", "updated_at", "available_at", "redeemed_at")
    date_hierarchy = "created_at"
    list_select_related = ("inviter", "invited", "order", "redeemed_config")

    @admin.display(description=_("Reward traffic GB"), ordering="reward_gb")
    def reward_traffic_gb(self, obj):
        return obj.reward_gb


class DiscountUsageInline(admin.TabularInline):
    model = Order
    fk_name = "discount_code"
    extra = 0
    can_delete = False
    show_change_link = True
    ordering = ("-created_at",)
    fields = (
        "order_admin_link",
        "plan",
        "original_amount",
        "discount_amount",
        "amount",
        "status",
        "verification_status",
        "created_at",
        "payment_submitted_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("plan")

    @admin.display(description=_("Order"))
    def order_admin_link(self, obj):
        url = reverse("admin:store_order_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.order_tracking_code)


@admin.register(DiscountCode)
class DiscountCodeAdmin(ImportExportModelAdmin):
    list_display = (
        "code",
        "availability_status",
        "value_display",
        "usage_display",
        "usage_percentage_display",
        "used_count",
        "redeemed_orders_count",
        "total_discount_granted_display",
        "last_used_at",
        "is_active",
        "valid_from",
        "valid_until",
    )
    list_filter = ("discount_type", "is_active", "valid_from", "valid_until")
    search_fields = ("code",)
    date_hierarchy = "created_at"
    filter_horizontal = ("applicable_plans",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "used_count",
        "remaining_uses",
        "usage_percentage_display",
        "redeemed_orders_count",
        "total_discount_granted_display",
        "first_used_at",
        "last_used_at",
    )
    fieldsets = (
        (
            _("Discount setup"),
            {
                "fields": (
                    "code",
                    "is_active",
                    "discount_type",
                    "value",
                )
            },
        ),
        (
            _("Usage limits"),
            {
                "fields": (
                    "max_usage",
                    "used_count",
                    "remaining_uses",
                    "usage_percentage_display",
                )
            },
        ),
        (
            _("Validity window"),
            {
                "fields": (
                    "valid_from",
                    "valid_until",
                )
            },
        ),
        (
            _("Plan scope"),
            {
                "fields": ("applicable_plans",),
                "description": _("Leave empty to allow this discount for every active plan."),
            },
        ),
        (
            _("Usage audit"),
            {
                "fields": (
                    "redeemed_orders_count",
                    "total_discount_granted_display",
                    "first_used_at",
                    "last_used_at",
                )
            },
        ),
        (
            _("Timestamps"),
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )
    inlines = (DiscountUsageInline,)
    actions = (
        "activate_codes",
        "deactivate_codes",
        "recalculate_used_counts",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                orders_count_annotation=Count("orders", distinct=True),
                total_discount_annotation=Sum("orders__discount_amount"),
                first_used_annotation=Min("orders__created_at"),
                last_used_annotation=Max("orders__created_at"),
            )
        )

    @admin.display(description=_("Status"))
    def availability_status(self, obj):
        now = timezone.now()
        if not obj.is_active:
            return format_html('<span style="color:#ef4444;font-weight:600;">{}</span>', _("Inactive"))
        if obj.valid_from and now < obj.valid_from:
            return format_html('<span style="color:#f59e0b;font-weight:600;">{}</span>', _("Scheduled"))
        if obj.valid_until and now > obj.valid_until:
            return format_html('<span style="color:#ef4444;font-weight:600;">{}</span>', _("Expired"))
        if not obj.has_usage_available():
            return format_html('<span style="color:#ef4444;font-weight:600;">{}</span>', _("Limit reached"))
        return format_html('<span style="color:#10b981;font-weight:600;">{}</span>', _("Usable"))

    @admin.display(description=_("Discount"), ordering="value")
    def value_display(self, obj):
        if obj.discount_type == DiscountCode.DiscountType.PERCENTAGE:
            return f"{obj.value}%"
        return f"{obj.value:,}"

    @admin.display(description=_("Usage"), ordering="used_count")
    def usage_display(self, obj):
        if obj.max_usage is None:
            return _("%(used)s / Unlimited") % {"used": f"{obj.used_count:,}"}
        return f"{obj.used_count:,} / {obj.max_usage:,}"

    @admin.display(description=_("Usage %"))
    def usage_percentage_display(self, obj):
        if obj.max_usage is None:
            return _("Unlimited")
        if obj.max_usage == 0:
            return "0%"
        return f"{min(round((obj.used_count / obj.max_usage) * 100), 100)}%"

    @admin.display(description=_("Remaining uses"))
    def remaining_uses(self, obj):
        if obj.max_usage is None:
            return _("Unlimited")
        return max(obj.max_usage - obj.used_count, 0)

    @admin.display(description=_("Used orders"))
    def redeemed_orders_count(self, obj):
        value = getattr(obj, "orders_count_annotation", None)
        if value is not None:
            return value
        return obj.orders.count()

    @admin.display(description=_("Total discount granted"))
    def total_discount_granted_display(self, obj):
        value = getattr(obj, "total_discount_annotation", None)
        if value is None:
            value = obj.orders.aggregate(total=Sum("discount_amount"))["total"]
        return f"{value or 0:,}"

    @admin.display(description=_("First used"))
    def first_used_at(self, obj):
        value = getattr(obj, "first_used_annotation", None)
        if value is not None:
            return value
        return obj.orders.order_by("created_at").values_list("created_at", flat=True).first() or "-"

    @admin.display(description=_("Last used"))
    def last_used_at(self, obj):
        value = getattr(obj, "last_used_annotation", None)
        if value is not None:
            return value
        return obj.orders.order_by("-created_at").values_list("created_at", flat=True).first() or "-"

    @admin.action(description=_("Activate selected discount codes"))
    def activate_codes(self, request, queryset):
        updated = queryset.update(is_active=True, updated_at=timezone.now())
        messages.success(request, _("%(count)s discount code(s) activated.") % {"count": updated})

    @admin.action(description=_("Deactivate selected discount codes"))
    def deactivate_codes(self, request, queryset):
        updated = queryset.update(is_active=False, updated_at=timezone.now())
        messages.success(request, _("%(count)s discount code(s) deactivated.") % {"count": updated})

    @admin.action(description=_("Recalculate used_count from related orders"))
    def recalculate_used_counts(self, request, queryset):
        updated = 0
        for discount in queryset:
            used_count = discount.orders.count()
            DiscountCode.objects.filter(pk=discount.pk).update(
                used_count=used_count,
                updated_at=timezone.now(),
            )
            updated += 1
        messages.success(request, _("%(count)s discount code counter(s) recalculated.") % {"count": updated})



@admin.register(VPNClientUsageSnapshot)
class VPNClientUsageSnapshotAdmin(ImportExportModelAdmin):
    list_display = (
        "vpn_client",
        "recorded_at",
        "used_traffic_bytes",
        "remaining_traffic_bytes",
        "total_traffic_bytes",
    )
    list_filter = ("recorded_at",)
    search_fields = ("vpn_client__username", "vpn_client__uuid", "vpn_client__order__order_tracking_code")
    date_hierarchy = "recorded_at"
    list_select_related = ("vpn_client", "vpn_client__order")
    readonly_fields = (
        "vpn_client",
        "recorded_at",
        "total_traffic_bytes",
        "used_upload_bytes",
        "used_download_bytes",
        "used_traffic_bytes",
        "remaining_traffic_bytes",
        "raw",
    )


@admin.register(VPNClientReminderLog)
class VPNClientReminderLogAdmin(ImportExportModelAdmin):
    list_display = (
        "customer",
        "vpn_client",
        "reminder_type",
        "trigger_key",
        "status",
        "sent_to_telegram_id",
        "created_at",
        "sent_at",
    )
    list_filter = ("reminder_type", "trigger_key", "status", "created_at")
    search_fields = (
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
        "vpn_client__username",
        "vpn_client__xui_email",
        "vpn_client__uuid",
        "sent_to_telegram_id",
        "error_message",
    )
    date_hierarchy = "created_at"
    list_select_related = ("customer", "vpn_client")
    readonly_fields = (
        "customer",
        "vpn_client",
        "reminder_type",
        "trigger_key",
        "trigger_date",
        "sent_to_telegram_id",
        "status",
        "message_text",
        "error_message",
        "created_at",
        "updated_at",
        "sent_at",
    )


@admin.register(DailyAdminReportLog)
class DailyAdminReportLogAdmin(ImportExportModelAdmin):
    list_display = (
        "store",
        "report_date",
        "status",
        "sent_to_count",
        "sent_at",
        "created_at",
    )
    list_filter = ("status", "store", "report_date", "created_at", "sent_at")
    search_fields = ("store__name", "store__english_name", "message_text", "error_message")
    date_hierarchy = "report_date"
    list_select_related = ("store",)
    readonly_fields = (
        "store",
        "report_date",
        "period_start",
        "period_end",
        "status",
        "sent_to_count",
        "message_text",
        "error_message",
        "metadata",
        "created_at",
        "sent_at",
    )


@admin.register(QasedakBackupJob)
class QasedakBackupJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "backup_type",
        "status",
        "file_name",
        "file_size",
        "includes_media",
        "includes_env",
        "created_by",
    )
    list_filter = ("status", "backup_type", "includes_media", "includes_env", "created_at")
    search_fields = ("file_name", "sha256", "created_by__username")
    readonly_fields = (
        "created_by",
        "created_at",
        "backup_type",
        "status",
        "file_path",
        "file_name",
        "file_size",
        "sha256",
        "includes_media",
        "includes_env",
        "includes_system",
        "safe_summary",
        "error_message",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(QasedakRestoreJob)
class QasedakRestoreJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "uploaded_file_name",
        "status",
        "includes_media",
        "includes_env",
        "created_by",
    )
    list_filter = ("status", "includes_media", "includes_env", "created_at")
    search_fields = ("uploaded_file_name", "uploaded_sha256", "created_by__username")
    readonly_fields = (
        "created_by",
        "created_at",
        "uploaded_file_path",
        "uploaded_file_name",
        "uploaded_sha256",
        "status",
        "validation_summary",
        "restore_plan",
        "pre_restore_backup_path",
        "error_message",
        "includes_env",
        "includes_media",
        "requires_service_restart",
        "validated_at",
        "restored_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ServerNode)
class ServerNodeAdmin(admin.ModelAdmin):
    list_display = ("name", "ip", "ssh_user", "ssh_port", "status", "max_instances", "current_instances", "updated_at")
    list_filter = ("status", "credential_mode")
    search_fields = ("name", "ip", "ssh_user")
    readonly_fields = ("current_instances", "last_checked_at", "safe_metadata", "created_at", "updated_at")


@admin.register(TenantInstance)
class TenantInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "tenant_id",
        "status",
        "deployment_status",
        "subdomain",
        "port",
        "container_name",
        "admin_url_link",
        "updated_at",
    )
    list_filter = ("status", "deployment_status", "customer_domain_dns_status", "server_node")
    search_fields = ("tenant_id", "subdomain", "container_name", "instance_url")
    readonly_fields = (
        "tenant_id",
        "server_node",
        "container_name",
        "port",
        "domain",
        "subdomain",
        "customer_domain",
        "customer_domain_dns_status",
        "status",
        "deployment_status",
        "instance_url",
        "admin_url_link",
        "credential_file_path",
        "nginx_config_path",
        "container_id",
        "docker_image_version",
        "last_deployed_at",
        "last_health",
        "runtime_state",
        "error_message",
        "created_by",
        "created_at",
        "updated_at",
    )
    actions = ("status_action", "restart_action", "suspend_action", "resume_action", "enable_https_action")

    def has_add_permission(self, request):
        return False

    def admin_url_link(self, obj):
        if not obj or not obj.instance_url:
            return "-"
        url = f"{obj.instance_url.rstrip('/')}/admin/"
        return format_html('<a href="{}" target="_blank" rel="noopener">Open admin</a>', url)

    admin_url_link.short_description = "Tenant admin"

    def credential_file_path(self, obj):
        return (obj.runtime_state or {}).get("admin_credentials_path", "-")

    credential_file_path.short_description = "Credential file path"

    def _toolkit(self):
        return TenantOwnerToolkit()

    def _run_for_queryset(self, request, queryset, action, label):
        ok = 0
        for tenant in queryset:
            try:
                action(tenant)
                ok += 1
            except OwnerToolkitError as exc:
                self.message_user(request, f"{tenant.tenant_id}: {exc}", level=messages.ERROR)
        if ok:
            self.message_user(request, f"{label}: {ok} tenant(s) processed.", level=messages.SUCCESS)

    @admin.action(description="Status")
    def status_action(self, request, queryset):
        toolkit = self._toolkit()

        def run(tenant):
            result = toolkit.tenant_status(tenant_id=tenant.tenant_id).safe_dict()
            self.message_user(
                request,
                (
                    f"{tenant.tenant_id}: status={result.get('status')} "
                    f"container={result.get('container_status')} health={result.get('health_status')} "
                    f"bot={result.get('bot_worker_status')} webhook={result.get('webhook_status')} "
                    f"setup={result.get('setup_status')} https={result.get('https_status')}"
                ),
                level=messages.INFO,
            )

        self._run_for_queryset(request, queryset, run, "Status")

    @admin.action(description="Restart")
    def restart_action(self, request, queryset):
        toolkit = self._toolkit()
        self._run_for_queryset(request, queryset, lambda tenant: toolkit.restart_tenant(tenant_id=tenant.tenant_id), "Restart")

    @admin.action(description="Suspend")
    def suspend_action(self, request, queryset):
        toolkit = self._toolkit()
        self._run_for_queryset(
            request,
            queryset,
            lambda tenant: toolkit.suspend_tenant(tenant_id=tenant.tenant_id, reason="Suspended from Django Admin"),
            "Suspend",
        )

    @admin.action(description="Resume")
    def resume_action(self, request, queryset):
        toolkit = self._toolkit()
        self._run_for_queryset(request, queryset, lambda tenant: toolkit.resume_tenant(tenant_id=tenant.tenant_id), "Resume")

    @admin.action(description="Enable HTTPS")
    def enable_https_action(self, request, queryset):
        toolkit = self._toolkit()
        self._run_for_queryset(
            request,
            queryset,
            lambda tenant: toolkit.enable_https(tenant_id=tenant.tenant_id, email="admin@panelwpvideo.ir"),
            "Enable HTTPS",
        )
