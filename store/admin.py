import csv
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Min, OuterRef, Q, Subquery, Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from payments.models import IncomingPaymentSMS

from .models import (
    BotConfiguration,
    BotAdminOrderMessage,
    BotEventLog,
    BotPendingAction,
    BotUser,
    BroadcastMessage,
    BroadcastRecipient,
    Customer,
    CustomerAnalyticsReport,
    CustomerReward,
    DiscountCode,
    FreeTrialRequest,
    Inbound,
    Order,
    Operator,
    Panel,
    Plan,
    Referral,
    ReferralRewardLedger,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    VPNClientReminderLog,
    VPNClientUsageSnapshot,
)
from .broadcast_services import create_campaign_recipients, resolve_campaign_recipients, send_campaign
from .bots import build_sales_report, send_to_config
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
from .order_actions import activate_order, reject_order
from .xui_api import sync_inbound_data


User = get_user_model()

admin.site.site_header = _("VPN Store Administration")
admin.site.site_title = _("VPN Store Admin")
admin.site.index_title = _("Operations dashboard")
admin.site.index_template = "admin/dashboard.html"


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
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "is_active",
        "last_login",
    )
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "email", "first_name", "last_name")
    date_hierarchy = "date_joined"
    ordering = ("username",)


@admin.register(Group)
class GroupAdmin(ImportExportModelAdmin, DjangoGroupAdmin):
    resource_class = GroupResource
    list_display = ("name",)
    list_filter = ("permissions__content_type__app_label",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Store)
class StoreAdmin(ImportExportModelAdmin):
    list_display = (
        "name",
        "english_name",
        "domain",
        "sales_mode",
        "telegram_support",
        "bale_support",
        "receipt_image_only_payment",
        "custom_volume_price_per_gb",
        "referral_system_enabled",
        "referral_reward_traffic_gb",
        "referral_reward_duration_days",
        "free_trial_enabled",
        "free_trial_traffic_gb",
        "free_trial_duration_hours",
        "free_trial_cooldown_days",
        "analytics_enabled",
        "broadcast_enabled",
        "renewal_reminders_enabled",
        "renewal_reminders_start_at",
        "low_traffic_reminders_enabled",
        "broadcast_rate_limit_per_second",
        "broadcast_max_recipients_per_campaign",
        "is_active",
        "updated_at",
    )
    list_filter = (
        "sales_mode",
        "is_active",
        "receipt_image_only_payment",
        "referral_system_enabled",
        "free_trial_enabled",
        "analytics_enabled",
        "broadcast_enabled",
        "renewal_reminders_enabled",
        "renewal_reminders_start_at",
        "low_traffic_reminders_enabled",
    )
    search_fields = ("name", "english_name", "domain", "telegram_support", "bale_support")
    date_hierarchy = "created_at"
    prepopulated_fields = {"slug": ("english_name",)}
    autocomplete_fields = ("free_trial_panel", "free_trial_inbound")

    @admin.display(description=_("Referral traffic GB"), ordering="referral_reward_gb")
    def referral_reward_traffic_gb(self, obj):
        return obj.referral_reward_gb


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


@admin.register(BotConfiguration)
class BotConfigurationAdmin(ImportExportModelAdmin):
    list_display = (
        "name",
        "provider",
        "store",
        "admin_user_id",
        "additional_admin_user_ids",
        "is_active",
        "force_telegram_channel_join",
        "notify_new_orders",
        "notify_order_updates",
        "send_sales_reports",
        "last_report_sent_at",
        "webhook_path",
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
        "telegram_required_channel_id",
        "telegram_required_channel_username",
        "store__name",
    )
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    readonly_fields = (
        "webhook_secret",
        "webhook_path",
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
                "fields": ("webhook_secret", "webhook_path"),
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

    @admin.display(description=_("Webhook path"))
    def webhook_path(self, obj):
        if not obj.pk:
            return _("Save first to generate webhook path.")
        return reverse("bot_webhook", args=[obj.provider, obj.webhook_secret])

    @admin.display(description=_("Bot event logs"))
    def event_logs_link(self, obj):
        if not obj.pk:
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
        "customer",
        "store",
        "contact_value",
        "status",
        "last_customer_message_at",
        "last_admin_message_at",
        "updated_at",
    )
    list_filter = ("status", "store", "created_at", "updated_at")
    search_fields = ("contact_value", "customer__display_name", "customer__username", "customer__phone_number", "messages__body")
    autocomplete_fields = ("store", "customer")
    readonly_fields = ("created_at", "updated_at", "last_customer_message_at", "last_admin_message_at", "closed_at")
    date_hierarchy = "updated_at"
    list_select_related = ("store", "customer")
    inlines = (SupportMessageInline,)
    actions = ("close_conversations", "mark_waiting_admin")

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


@admin.register(Plan)
class PlanAdmin(ImportExportModelAdmin):
    import_export_change_list_template = "admin/store/plan/change_list.html"
    list_display = (
        "name",
        "store",
        "operator_names",
        "volume_gb",
        "duration_days",
        "price",
        "currency",
        "is_public",
        "is_custom_volume",
        "is_active",
        "sort_order",
    )
    list_filter = ("store", ("operators", admin.RelatedOnlyFieldListFilter), "is_active", "is_public", "is_custom_volume", "currency")
    search_fields = ("name", "description", "operators__name", "operators__slug")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_active", "is_public", "sort_order")
    filter_horizontal = ("operators",)

    @admin.display(description=_("Operators"))
    def operator_names(self, obj):
        names = [operator.name for operator in obj.operators.all()[:4]]
        if not names:
            return "-"
        total = obj.operators.count()
        suffix = f" +{total - len(names)}" if total > len(names) else ""
        return ", ".join(names) + suffix

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("operators")

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


@admin.register(Panel)
class PanelAdmin(ImportExportModelAdmin):
    list_display = ("name", "store", "url", "uses_proxy", "is_active", "inbound_count", "last_sync_at")
    list_filter = ("store", "is_active")
    search_fields = ("name", "url", "username", "proxy_url")
    date_hierarchy = "created_at"
    list_select_related = ("store",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(admin_inbound_count=Count("inbounds"))

    @admin.display(description=_("Inbounds"), ordering="admin_inbound_count")
    def inbound_count(self, obj):
        return getattr(obj, "admin_inbound_count", None) if getattr(obj, "admin_inbound_count", None) is not None else obj.inbounds.count()

    @admin.display(description=_("Proxy"), boolean=True)
    def uses_proxy(self, obj):
        return bool(obj.proxy_url)


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
        "username",
        "customer",
        "plan",
        "operator",
        "quantity",
        "original_amount",
        "discount_badge",
        "discount_source",
        "discount_amount",
        "amount",
        "status",
        "verification_status",
        "sms_match_summary",
        "receipt_analysis_badge",
        "referral_reward_status",
        "sender_card_last4",
        "verified_by",
        "created_at",
    )
    list_filter = (
        OrderSMSMatchStatusFilter,
        "store",
        ("operator", admin.RelatedOnlyFieldListFilter),
        ("customer", admin.RelatedOnlyFieldListFilter),
        "status",
        "verification_status",
        "payment_method",
        "discount_source",
        ("discount_code", admin.RelatedOnlyFieldListFilter),
        "created_at",
    )
    search_fields = (
        "order_tracking_code",
        "bank_tracking_code",
        "sender_card_name",
        "sender_card_last4",
        "username",
        "uuid",
        "operator__name",
        "operator__slug",
        "discount_code__code",
        "discount_code_text",
        "customer__username",
        "customer__phone_number",
        "customer__referral_code",
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
        "verified_at",
    )
    fieldsets = (
        (
            _("Order"),
            {
                "fields": (
                    "public_id",
                    "store",
                    "customer",
                    "plan",
                    "operator",
                    "quantity",
                    "order_tracking_code",
                    "status",
                    "verification_status",
                    "verified_by",
                    "verified_at",
                    "rejection_reason",
                )
            },
        ),
        (
            _("Pricing and discount"),
            {
                "fields": (
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
            _("Manual payment"),
            {
                "fields": (
                    "payment_method",
                    "is_paid",
                    "payment_submitted_at",
                    "admin_notified_at",
                    "admin_receipt_notified_at",
                    "sender_card_name",
                    "sender_card_last4",
                    "payment_date",
                    "payment_time",
                    "payment_receipt_image",
                    "receipt_analysis_summary",
                    "bank_tracking_code",
                    "card_last_four",
                )
            },
        ),
        (
            _("Gateway placeholders"),
            {
                "classes": ("collapse",),
                "fields": (
                    "payment_gateway",
                    "gateway_authority",
                    "gateway_reference_id",
                ),
            },
        ),
        (
            _("VPN provisioning"),
            {
                "fields": (
                    "inbound",
                    "username",
                    "uuid",
                    "sub_link",
                    "direct_link",
                )
            },
        ),
        (
            _("Metadata"),
            {
                "classes": ("collapse",),
                "fields": ("metadata", "created_at", "updated_at"),
            },
        ),
    )
    actions = ("mark_as_confirmed", "verify_and_activate_selected")
    inlines = (MatchedPaymentSMSInline, VPNClientInline, OrderReferralRewardLedgerInline)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(sms_match_count=Count("incoming_payment_sms", distinct=True))

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


@admin.register(Inbound)
class InboundAdmin(ImportExportModelAdmin):
    list_display = (
        "display_name",
        "panel_status",
        "inbound_id",
        "remark",
        "protocol",
        "network_type",
        "security",
        "current_users",
        "max_clients",
        "is_active",
    )
    list_filter = ("panel", "panel__store", "protocol", "network_type", "security", "is_active")
    search_fields = (
        "remark",
        "server_ip",
        "port",
        "=inbound_id",
        "panel__name",
        "panel__url",
        "panel__store__name",
        "panel__store__english_name",
    )
    date_hierarchy = "created_at"
    list_select_related = ("panel", "panel__store")
    autocomplete_fields = ("panel",)
    actions = ["sync_from_panel"]

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
    list_display = (
        "username",
        "store",
        "order",
        "inbound",
        "status",
        "duration_days",
        "used_traffic_bytes",
        "activated_at",
        "expires_at",
        "last_online_at",
        "last_reminder_sent_at",
        "created_at",
    )
    list_filter = ("store", "status", "inbound", "created_at")
    search_fields = ("username", "xui_email", "uuid", "order__order_tracking_code")
    date_hierarchy = "created_at"
    list_select_related = ("store", "order", "plan", "inbound", "inbound__panel")
    readonly_fields = ("public_id", "created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(admin_last_reminder_sent_at=Max("reminder_logs__sent_at"))

    @admin.display(description=_("Last reminder"), ordering="admin_last_reminder_sent_at")
    def last_reminder_sent_at(self, obj):
        return getattr(obj, "admin_last_reminder_sent_at", None) or "-"


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
        "display_name",
        "username",
        "phone_number",
        "is_wholesale",
        "default_discount_percent",
        "referral_code",
        "referred_by",
        "orders_total",
        "invited_total",
        "successful_referrals_total",
        "available_referral_packages_total",
        "available_referral_gb_total",
        "available_referral_duration_total",
        "redeemed_referral_gb_total",
        "last_reminder_sent_at",
        "last_seen_at",
        "created_at",
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
        "referral_code",
        "referral_code_used",
        "referred_at",
        "first_ip",
        "last_ip",
        "user_agent_hash",
        "created_at",
        "updated_at",
        "last_seen_at",
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
            )
        )

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


class BroadcastRecipientInline(admin.TabularInline):
    model = BroadcastRecipient
    extra = 0
    can_delete = False
    fields = ("customer", "channel", "target_identifier", "status", "error_message", "sent_at", "created_at")
    readonly_fields = fields
    ordering = ("created_at", "pk")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BroadcastMessage)
class BroadcastMessageAdmin(ImportExportModelAdmin):
    list_display = (
        "title",
        "audience_type",
        "channel",
        "status",
        "recipients_total",
        "success_count",
        "failed_count",
        "sent_at",
        "created_at",
    )
    list_filter = ("status", "audience_type", "channel", "created_at", "sent_at")
    search_fields = ("title", "message_text", "metadata")
    date_hierarchy = "created_at"
    autocomplete_fields = ("store",)
    readonly_fields = (
        "audience_preview",
        "total_recipients",
        "success_count",
        "failed_count",
        "sent_at",
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
                    "message_text",
                    "audience_type",
                    "channel",
                    "status",
                    "scheduled_at",
                    "audience_preview",
                )
            },
        ),
        (
            _("Delivery"),
            {
                "fields": (
                    "total_recipients",
                    "success_count",
                    "failed_count",
                    "sent_at",
                    "metadata",
                )
            },
        ),
        (
            _("Timestamps"),
            {"classes": ("collapse",), "fields": ("created_at", "updated_at")},
        ),
    )
    inlines = (BroadcastRecipientInline,)
    actions = ("queue_selected", "send_selected", "cancel_selected")

    @admin.display(description=_("Recipients"))
    def recipients_total(self, obj):
        return obj.total_recipients or obj.recipients.count()

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

    @admin.action(description=_("Send selected campaigns"))
    def send_selected(self, request, queryset):
        sent = 0
        for campaign in queryset:
            try:
                campaign.full_clean()
                send_campaign(campaign)
            except ValidationError as exc:
                messages.error(request, _("%(title)s was not sent: %(error)s") % {
                    "title": campaign.title,
                    "error": "; ".join(exc.messages),
                })
                continue
            campaign.refresh_from_db()
            sent += 1
            messages.info(
                request,
                _("%(title)s: %(success)s sent, %(failed)s failed.") % {
                    "title": campaign.title,
                    "success": campaign.success_count,
                    "failed": campaign.failed_count,
                },
            )
        messages.success(request, _("%(count)s campaign(s) processed.") % {"count": sent})

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
        "customer",
        "channel",
        "target_identifier",
        "status",
        "error_summary",
        "sent_at",
        "created_at",
    )
    list_filter = ("status", "channel", "campaign__status", "created_at", "sent_at")
    search_fields = (
        "campaign__title",
        "customer__display_name",
        "customer__username",
        "customer__phone_number",
        "target_identifier",
        "error_message",
    )
    autocomplete_fields = ("campaign", "customer")
    readonly_fields = ("created_at", "updated_at", "sent_at")
    date_hierarchy = "created_at"
    list_select_related = ("campaign", "customer")

    @admin.display(description=_("Error"))
    def error_summary(self, obj):
        if not obj.error_message:
            return "-"
        return obj.error_message[:120]


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
