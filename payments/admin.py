from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _
from import_export import fields, resources
from import_export.admin import ImportExportModelAdmin

from store.models import normalize_payment_digits

from .models import IncomingPaymentSMS
from .payment_matching import (
    confirm_incoming_payment_sms,
    dismiss_incoming_payment_sms,
    process_incoming_payment_sms,
)


class IncomingPaymentSMSResource(resources.ModelResource):
    matched_order_codes = fields.Field(column_name="matched_order_codes")

    class Meta:
        model = IncomingPaymentSMS
        import_id_fields = ("id",)
        fields = (
            "id",
            "raw_text",
            "amount",
            "balance",
            "sms_datetime",
            "received_at",
            "status",
            "matched_order_codes",
        )
        export_order = fields
        skip_unchanged = True
        report_skipped = True

    def dehydrate_matched_order_codes(self, obj):
        return ", ".join(obj.matched_orders.values_list("order_tracking_code", flat=True))


class MatchedOrderCountFilter(admin.SimpleListFilter):
    title = _("matched order count")
    parameter_name = "matched_orders_count"

    def lookups(self, request, model_admin):
        return (
            ("none", _("No matched orders")),
            ("single", _("Exactly one matched order")),
            ("multiple", _("Multiple matched orders")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "none":
            return queryset.annotate(order_match_count=Count("matched_orders", distinct=True)).filter(
                order_match_count=0
            )
        if value == "single":
            return queryset.annotate(order_match_count=Count("matched_orders", distinct=True)).filter(
                order_match_count=1
            )
        if value == "multiple":
            return queryset.annotate(order_match_count=Count("matched_orders", distinct=True)).filter(
                order_match_count__gt=1
            )
        return queryset


@admin.register(IncomingPaymentSMS)
class IncomingPaymentSMSAdmin(ImportExportModelAdmin):
    resource_class = IncomingPaymentSMSResource
    list_display = (
        "id",
        "matched_order_customer",
        "amount",
        "status",
        "matched_orders_summary",
        "order_review_links",
        "sms_datetime",
        "received_at",
    )
    list_filter = ("status", MatchedOrderCountFilter, "sms_datetime", "received_at")
    search_fields = ("raw_text", "matched_orders__order_tracking_code", "matched_orders__customer__phone_number")
    readonly_fields = ("received_at", "matched_orders_summary", "raw_preview")
    filter_horizontal = ("matched_orders",)
    date_hierarchy = "sms_datetime"
    actions = ("re_evaluate_sms_match", "confirm_single_matched_order", "dismiss_selected")
    ordering = ("-received_at",)

    fieldsets = (
        (
            _("Payment SMS"),
            {
                "fields": (
                    "raw_text",
                    "raw_preview",
                    "amount",
                    "balance",
                    "sms_datetime",
                    "received_at",
                    "status",
                )
            },
        ),
        (
            _("Matching"),
            {
                "fields": ("matched_orders", "matched_orders_summary"),
            },
        ),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("matched_orders__customer")
            .annotate(order_match_count=Count("matched_orders", distinct=True))
        )

    @admin.display(description=_("Raw preview"))
    def raw_preview(self, obj):
        return Truncator(obj.raw_text).chars(90)

    def mask_phone(self, value):
        cleaned = "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit() or ch == "+")
        if not cleaned:
            return ""
        prefix = cleaned[:4] if cleaned.startswith("+") else cleaned[:3]
        suffix = cleaned[-2:] if len(cleaned) > 5 else ""
        return f"{prefix}***{suffix}" if suffix else "***"

    def safe_customer_label(self, customer):
        if not customer:
            return "-"
        phone = getattr(customer, "phone_number", "") or ""
        label = (getattr(customer, "display_name", "") or getattr(customer, "username", "") or "").strip()
        if phone and normalize_payment_digits(label) == normalize_payment_digits(phone):
            return self.mask_phone(phone)
        return label or f"Customer {str(customer.public_id)[:8]}"

    @admin.display(description=_("Customer"))
    def matched_order_customer(self, obj):
        order = next(iter(obj.matched_orders.all()), None)
        return self.safe_customer_label(order.customer) if order else "-"

    @admin.display(description=_("Matched orders"), ordering="order_match_count")
    def matched_orders_summary(self, obj):
        count = getattr(obj, "order_match_count", None)
        if count is None:
            count = obj.matched_orders.count()
        if not count:
            return format_html('<span class="badge bg-secondary">{}</span>', _("None"))

        links = []
        for order in obj.matched_orders.all()[:5]:
            url = reverse("admin:store_order_change", args=[order.pk])
            links.append((url, order.order_tracking_code))
        suffix = "" if count <= 5 else _(" +%(count)s more") % {"count": count - 5}
        order_links = format_html_join(", ", '<a href="{}">{}</a>', links)
        return format_html("{}{}", order_links, suffix)

    @admin.display(description=_("Review"))
    def order_review_links(self, obj):
        links = []
        for order in obj.matched_orders.all()[:3]:
            links.append((reverse("admin_store_order_review", args=[order.pk]), _("Review %(code)s") % {"code": order.order_tracking_code}))
        if not links:
            return "-"
        return format_html_join(", ", '<a href="{}">{}</a>', links)

    @admin.action(description=_("Re-evaluate SMS matching for selected messages"))
    def re_evaluate_sms_match(self, request, queryset):
        processed = 0
        total_matches = 0
        failed = 0

        for payment_sms in queryset:
            try:
                matches = process_incoming_payment_sms(payment_sms, notify=False)
            except Exception as exc:  # pragma: no cover - defensive admin feedback
                failed += 1
                messages.error(
                    request,
                    _("SMS #%(sms_id)s could not be re-evaluated: %(error)s")
                    % {"sms_id": payment_sms.pk, "error": exc},
                )
                continue
            processed += 1
            total_matches += len(matches)

        if processed:
            messages.success(
                request,
                _("%(processed)s SMS message(s) re-evaluated with %(matches)s total matched order(s).")
                % {"processed": processed, "matches": total_matches},
            )
        if failed:
            messages.warning(request, _("%(count)s SMS message(s) failed during re-evaluation.") % {"count": failed})

    @admin.action(description=_("Confirm SMS with exactly one matched order"))
    def confirm_single_matched_order(self, request, queryset):
        confirmed = 0
        skipped = 0
        failed = 0

        for payment_sms in queryset.prefetch_related("matched_orders"):
            if payment_sms.matched_orders.count() != 1:
                skipped += 1
                continue
            try:
                confirm_incoming_payment_sms(payment_sms, user=request.user)
            except ValueError as exc:
                failed += 1
                messages.error(request, _("SMS #%(sms_id)s could not be confirmed: %(error)s") % {
                    "sms_id": payment_sms.pk,
                    "error": exc,
                })
                continue
            confirmed += 1

        if confirmed:
            messages.success(request, _("%(count)s SMS payment(s) confirmed.") % {"count": confirmed})
        if skipped:
            messages.warning(
                request,
                _("%(count)s SMS payment(s) skipped because they do not have exactly one match.")
                % {"count": skipped},
            )
        if failed:
            messages.error(request, _("%(count)s SMS payment(s) failed to confirm.") % {"count": failed})

    @admin.action(description=_("Dismiss selected SMS payments"))
    def dismiss_selected(self, request, queryset):
        updated = 0
        for payment_sms in queryset:
            dismiss_incoming_payment_sms(payment_sms)
            updated += 1
        messages.success(request, _("%(count)s SMS payment(s) dismissed.") % {"count": updated})
