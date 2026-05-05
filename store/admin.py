from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.db.models import Count, Max, Min, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin
from import_export.widgets import ForeignKeyWidget

from payments.models import IncomingPaymentSMS

from .models import (
    BotConfiguration,
    BotEventLog,
    BotPendingAction,
    BotUser,
    Customer,
    CustomerReward,
    DiscountCode,
    Inbound,
    Order,
    Panel,
    Plan,
    Referral,
    Store,
    VPNClient,
    VPNClientUsageSnapshot,
)
from .bots import build_sales_report, send_to_config
from .order_actions import activate_order
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
    list_display = ("name", "english_name", "domain", "telegram_support", "bale_support", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "english_name", "domain", "telegram_support", "bale_support")
    date_hierarchy = "created_at"
    prepopulated_fields = {"slug": ("english_name",)}


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
        "is_active",
        "notify_new_orders",
        "notify_order_updates",
        "send_sales_reports",
        "last_report_sent_at",
        "webhook_path",
    )
    list_filter = ("provider", "store", "is_active", "notify_new_orders", "notify_order_updates", "send_sales_reports")
    search_fields = ("name", "admin_user_id", "store__name")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    readonly_fields = ("webhook_secret", "webhook_path", "last_error", "created_at", "updated_at", "last_report_sent_at")
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
                    "is_active",
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
                "fields": ("last_error", "created_at", "updated_at"),
            },
        ),
    )
    actions = ("send_test_message", "send_sales_report_now")
    inlines = (BotEventLogInline,)

    @admin.display(description=_("Webhook path"))
    def webhook_path(self, obj):
        if not obj.pk:
            return _("Save first to generate webhook path.")
        return reverse("bot_webhook", args=[obj.provider, obj.webhook_secret])

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


@admin.register(BotPendingAction)
class BotPendingActionAdmin(ImportExportModelAdmin):
    list_display = ("bot_config", "order", "admin_user_id", "action", "status", "created_at", "resolved_at")
    list_filter = ("bot_config", "action", "status", "created_at")
    search_fields = ("order__order_tracking_code", "admin_user_id")
    autocomplete_fields = ("bot_config", "order")
    readonly_fields = ("created_at", "updated_at", "resolved_at")
    date_hierarchy = "created_at"
    list_select_related = ("bot_config", "order")


@admin.register(BotEventLog)
class BotEventLogAdmin(ImportExportModelAdmin):
    list_display = ("event_type", "status", "bot_config", "order", "created_at")
    list_filter = ("event_type", "status", "bot_config", "created_at")
    search_fields = ("message", "order__order_tracking_code", "bot_config__name")
    autocomplete_fields = ("bot_config", "order")
    readonly_fields = ("created_at", "updated_at", "raw_payload")
    date_hierarchy = "created_at"
    list_select_related = ("bot_config", "order")


@admin.register(Plan)
class PlanAdmin(ImportExportModelAdmin):
    list_display = (
        "name",
        "store",
        "volume_gb",
        "duration_days",
        "price",
        "currency",
        "is_public",
        "is_active",
        "sort_order",
    )
    list_filter = ("store", "is_active", "is_public", "currency")
    search_fields = ("name", "description")
    date_hierarchy = "created_at"
    list_select_related = ("store",)
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_active", "is_public", "sort_order")


@admin.register(Panel)
class PanelAdmin(ImportExportModelAdmin):
    list_display = ("name", "store", "url", "is_active", "last_sync_at")
    list_filter = ("store", "is_active")
    search_fields = ("name", "url", "username")
    date_hierarchy = "created_at"
    list_select_related = ("store",)


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


@admin.register(Order)
class OrderAdmin(ImportExportModelAdmin):
    resource_class = OrderResource
    list_display = (
        "order_tracking_code",
        "username",
        "customer",
        "plan",
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
        "sender_card_last4",
        "verified_by",
        "created_at",
    )
    list_filter = (
        OrderSMSMatchStatusFilter,
        "store",
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
        "discount_code__code",
        "discount_code_text",
        "customer__username",
        "customer__phone_number",
        "customer__referral_code",
    )
    autocomplete_fields = ("store", "customer", "plan", "discount_code", "inbound", "verified_by")
    date_hierarchy = "created_at"
    list_select_related = ("store", "customer", "plan", "discount_code", "inbound", "verified_by")
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
    inlines = (MatchedPaymentSMSInline, VPNClientInline)

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

        if activation_requested:
            self.activate_order(request, obj, old_obj=old_obj, save_order=False)

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
        "panel",
        "inbound_id",
        "protocol",
        "network_type",
        "security",
        "current_users",
        "max_clients",
        "is_active",
    )
    list_filter = ("panel", "protocol", "network_type", "security", "is_active")
    search_fields = ("remark", "server_ip", "panel__name")
    date_hierarchy = "created_at"
    list_select_related = ("panel", "panel__store")
    actions = ["sync_from_panel"]

    def display_name(self, obj):
        return str(obj)

    display_name.short_description = _("Name")

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
        "created_at",
    )
    list_filter = ("store", "status", "inbound", "created_at")
    search_fields = ("username", "xui_email", "uuid", "order__order_tracking_code")
    date_hierarchy = "created_at"
    list_select_related = ("store", "order", "plan", "inbound", "inbound__panel")
    readonly_fields = ("public_id", "created_at", "updated_at")


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
    inlines = (ReferralInline, CustomerRewardInline)

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
            )
        )

    @admin.display(description=_("Orders"))
    def orders_total(self, obj):
        value = getattr(obj, "admin_orders_count", None)
        return value if value is not None else obj.orders.count()

    @admin.display(description=_("Invited"))
    def invited_total(self, obj):
        value = getattr(obj, "admin_invited_count", None)
        return value if value is not None else obj.referrals_made.count()

    @admin.display(description=_("Successful"))
    def successful_referrals_total(self, obj):
        value = getattr(obj, "admin_successful_referrals_count", None)
        return value if value is not None else obj.referrals_made.filter(status=Referral.Status.PURCHASED).count()


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
