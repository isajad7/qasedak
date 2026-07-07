from collections import defaultdict
from datetime import date, timedelta
import logging
import re
from urllib.parse import urlparse

import jdatetime
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Max, Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from payments.models import IncomingPaymentSMS

from .admin_dashboard import get_owner_dashboard_context
from .admin_access import (
    ensure_admin_capability,
    ensure_any_admin_capability,
    require_admin_capability,
    require_any_admin_capability,
    user_has_capability,
)
from .admin_reports_center import (
    VALID_REPORT_EXPORTS,
    build_reports_csv,
    get_report_store_options,
    get_reports_center_context,
    resolve_report_period,
)
from .admin_catalog import (
    CatalogPlanForm,
    catalog_plan_edit_url,
    catalog_plan_review_url,
    catalog_url,
    deactivate_route_for_admin,
    duplicate_plan_for_admin,
    get_catalog_context,
    get_plan_route_status,
    get_route_overview_items,
    money_label,
    plan_duration_label,
    plan_volume_label,
    selected_store_from_id as selected_catalog_store_from_id,
    set_plan_active_state,
    validate_plan_sales_readiness,
)
from .admin_campaigns import (
    CampaignAudienceForm,
    CampaignMessageForm,
    audience_descriptions,
    build_campaign_export_csv,
    campaign_audience_url,
    campaign_confirm_url,
    campaign_edit_url,
    campaign_export_url,
    campaign_new_url,
    campaign_preview_url,
    campaign_review_url,
    campaign_workbench_url,
    cancel_campaign,
    export_filename,
    get_audience_preview,
    get_review_context as get_campaign_review_context,
    get_workbench_context as get_campaign_workbench_context,
    MESSAGE_TEMPLATE_BODIES,
    queue_campaign_for_processing,
    queue_confirmation_phrase,
    reset_retryable_failed,
    safe_campaign_snippet,
    selected_store_from_id as selected_campaign_store_from_id,
)
from .admin_revenue import (
    REAL_SEND_CONFIRMATION,
    apply_revenue_safe_defaults,
    get_real_send_safety,
    get_revenue_control_context,
    get_store_options,
    revenue_control_url,
    set_revenue_mode,
)
from .admin_setup import build_setup_center_context
from .admin_setup_wizard import (
    WIZARD_ACTIVATE_ACTION,
    WIZARD_ACTIVATION_CONFIRMATION,
    WIZARD_ACTION_FIELD,
    WIZARD_STEP_BY_SLUG,
    WIZARD_TEST_TELEGRAM_ACTION,
    WIZARD_TEST_XUI_ACTION,
    clear_step_skipped,
    get_review_context,
    get_setup_wizard_context,
    get_step_form,
    get_step_page_context,
    is_skip_request,
    is_step_skippable,
    mark_step_skipped,
    next_step_slug,
    save_step_form,
    selected_store_from_id,
    wizard_step_url,
)
from .admin_support_services import (
    customer_message_url,
    get_customer_message_context,
    get_support_review_context,
    get_support_workbench_context,
    send_customer_direct_message,
    send_support_reply,
    support_review_url,
    support_workbench_url,
)
from .config_lookup import mask_identifier
from .deployment.owner_toolkit import OwnerToolkitError, TenantOwnerToolkit
from .jalali import format_jalali_date
from .models import (
    BotConfiguration,
    BotUser,
    BroadcastMessage,
    Customer,
    Panel,
    Order,
    Plan,
    PlanInboundRoute,
    Store,
    SupportConversation,
    VPNClient,
    normalize_payment_digits,
)
from .order_actions import activate_order, reject_order
from .orchestrator_v2.models import TenantInstance
from .vpn_client_management_services import (
    VPNClientManagementError,
    refresh_vpn_client_link_by_admin,
    set_vpn_client_enabled_by_admin,
)
from .vpn_client_reconciliation_services import (
    get_reconciliation_summary,
    reconcile_vpn_clients,
    remote_scope_matches_latest_check,
    remote_status_is_cleanup_eligible,
    remote_status_is_fresh,
    remote_status_label,
    remote_status_tone,
    safe_remote_scope,
    soft_delete_remote_missing_clients,
)
from .setup_readiness import setup_not_ready_details, sync_store_setup_status
from .telegram_bot.client import BotClient, BotDeliveryError
from .xui_api import XUIService, sync_vpn_client_stats


logger = logging.getLogger(__name__)


DEFAULT_STATUS_FILTER = "not_rejected"
ORDER_WORKBENCH_LIMIT = 20
SERVICE_WORKBENCH_LIMIT = 20
REMOTE_FILTER_STATUSES = {
    "remote-active": (VPNClient.RemoteCheckStatus.REMOTE_ACTIVE,),
    "remote-disabled": (VPNClient.RemoteCheckStatus.REMOTE_DISABLED,),
    "remote-missing": (VPNClient.RemoteCheckStatus.REMOTE_MISSING,),
    "remote-problem": (
        VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE,
        VPNClient.RemoteCheckStatus.INBOUND_MISSING,
        VPNClient.RemoteCheckStatus.AMBIGUOUS,
        VPNClient.RemoteCheckStatus.UNKNOWN,
    ),
    "not-checked": (VPNClient.RemoteCheckStatus.NOT_CHECKED,),
}
RECONCILIATION_CONFIRMATION = "SOFT_DELETE_REMOTE_MISSING"
PENDING_REVIEW_STATUSES = (Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED)
PENDING_ORDER_STATUSES = (Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED)
ORDER_TERMINAL_STATUSES = (Order.Status.COMPLETED, Order.Status.REJECTED, Order.Status.CANCELLED)
CONFIG_LINK_MARKERS = ("vless://", "vmess://", "trojan://", "ss://", "/sub/")
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{20,}\b")
CONFIG_LINK_PATTERN = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
SUB_LINK_PATTERN = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)

STATUS_FILTERS = {
    "not_rejected": {
        "label": "رسیدهای قابل اتکا",
        "statuses": [
            Order.Status.PENDING_VERIFICATION,
            Order.Status.CONFIRMED,
            Order.Status.COMPLETED,
        ],
    },
    "pending": {
        "label": "در انتظار بررسی",
        "statuses": [
            Order.Status.PENDING_VERIFICATION,
            Order.Status.CONFIRMED,
        ],
    },
    "completed": {
        "label": "تایید شده",
        "statuses": [Order.Status.COMPLETED],
    },
    "rejected": {
        "label": "رد شده",
        "statuses": [Order.Status.REJECTED],
    },
    "all": {
        "label": "همه رسیدهای ثبت شده",
        "statuses": None,
    },
}


def normalize_card_number(value):
    return "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit())


def parse_report_date(value):
    cleaned = normalize_payment_digits(value).strip().replace("-", "/")
    if not cleaned:
        return None, ""
    parts = cleaned.split("/")
    if len(parts) != 3:
        return None, "تاریخ را به شکل 1405/02/16 یا 2026-05-05 وارد کن."
    try:
        year, month, day = [int(part) for part in parts]
        if year < 1700:
            return jdatetime.date(year, month, day).togregorian(), ""
        return date(year, month, day), ""
    except ValueError:
        return None, "تاریخ وارد شده معتبر نیست."


def payment_card_snapshot(order):
    metadata = order.metadata or {}
    return {
        "card_number": normalize_card_number(
            metadata.get("payment_destination_card_number")
            or getattr(order.store, "card_number", "")
        ),
        "card_owner": (
            metadata.get("payment_destination_card_owner")
            or getattr(order.store, "card_owner", "")
            or "بدون نام"
        ),
        "bank_name": (
            metadata.get("payment_destination_bank_name")
            or getattr(order.store, "bank_name", "")
            or ""
        ),
    }


def order_amount_as_rial(order):
    if order.currency == Plan.Currency.TOMAN:
        return order.amount * 10
    if order.currency == Plan.Currency.IRR:
        return order.amount
    return 0


def empty_summary(card_number, card_owner="", bank_name=""):
    return {
        "card_number": card_number,
        "card_owner": card_owner or "بدون نام",
        "bank_name": bank_name or "",
        "order_count": 0,
        "total_irr": 0,
        "pending_count": 0,
        "completed_count": 0,
        "rejected_count": 0,
        "latest_payment_at": None,
        "totals_by_currency": defaultdict(int),
    }


def build_order_row(order, card_info):
    analysis = (order.metadata or {}).get("receipt_analysis") or {}
    return {
        "order": order,
        "card_number": card_info["card_number"],
        "card_owner": card_info["card_owner"],
        "bank_name": card_info["bank_name"],
        "amount_irr": order_amount_as_rial(order),
        "analysis_status": analysis.get("status") or "ثبت نشده",
        "requires_admin_review": analysis.get("requires_admin_review"),
        "admin_url": reverse("admin:store_order_change", args=[order.pk]),
        "receipt_url": order.payment_receipt_image.url if order.payment_receipt_image else "",
    }


@require_admin_capability("payments.review")
def card_receipts_report(request):
    selected_status = request.GET.get("status") or DEFAULT_STATUS_FILTER
    if selected_status not in STATUS_FILTERS:
        selected_status = DEFAULT_STATUS_FILTER

    date_from_raw = request.GET.get("date_from", "").strip()
    date_to_raw = request.GET.get("date_to", "").strip()
    selected_card = normalize_card_number(request.GET.get("card", ""))
    date_from, date_from_error = parse_report_date(date_from_raw)
    date_to, date_to_error = parse_report_date(date_to_raw)
    filter_errors = [error for error in (date_from_error, date_to_error) if error]

    orders = Order.objects.filter(
        payment_method=Order.PaymentMethod.MANUAL_CARD,
        is_paid=True,
        payment_submitted_at__isnull=False,
    ).select_related("store", "plan", "customer")

    statuses = STATUS_FILTERS[selected_status]["statuses"]
    if statuses is not None:
        orders = orders.filter(status__in=statuses)
    if date_from:
        orders = orders.filter(payment_submitted_at__date__gte=date_from)
    if date_to:
        orders = orders.filter(payment_submitted_at__date__lte=date_to)

    summaries = {}
    selected_order_rows = []
    recent_order_rows = []

    for order in orders.order_by("-payment_submitted_at", "-created_at"):
        card_info = payment_card_snapshot(order)
        card_number = card_info["card_number"] or "بدون شماره"
        if selected_card and selected_card != card_number:
            continue

        if card_number not in summaries:
            summaries[card_number] = empty_summary(
                card_number,
                card_info["card_owner"],
                card_info["bank_name"],
            )

        summary = summaries[card_number]
        summary["order_count"] += 1
        summary["total_irr"] += order_amount_as_rial(order)
        summary["totals_by_currency"][order.currency] += order.amount
        summary["latest_payment_at"] = summary["latest_payment_at"] or order.payment_submitted_at
        if order.status == Order.Status.COMPLETED:
            summary["completed_count"] += 1
        elif order.status == Order.Status.REJECTED:
            summary["rejected_count"] += 1
        else:
            summary["pending_count"] += 1

        row = build_order_row(order, card_info)
        if selected_card:
            selected_order_rows.append(row)
        elif len(recent_order_rows) < 20:
            recent_order_rows.append(row)

    card_summaries = sorted(
        summaries.values(),
        key=lambda item: item["total_irr"],
        reverse=True,
    )
    for summary in card_summaries:
        summary["totals_by_currency"] = dict(summary["totals_by_currency"])

    card_options = {
        normalize_card_number(store.card_number): {
            "card_number": normalize_card_number(store.card_number),
            "card_owner": store.card_owner,
            "bank_name": store.bank_name or "",
        }
        for store in Store.objects.exclude(card_number="")
    }
    for summary in card_summaries:
        card_options[summary["card_number"]] = {
            "card_number": summary["card_number"],
            "card_owner": summary["card_owner"],
            "bank_name": summary["bank_name"],
        }

    context = {
        **admin.site.each_context(request),
        "title": "گزارش دریافتی کارت‌ها",
        "card_summaries": card_summaries,
        "card_options": sorted(card_options.values(), key=lambda item: item["card_number"]),
        "selected_card": selected_card,
        "selected_status": selected_status,
        "status_filters": STATUS_FILTERS,
        "date_from": date_from_raw,
        "date_to": date_to_raw,
        "date_from_label": format_jalali_date(date_from) if date_from else "",
        "date_to_label": format_jalali_date(date_to) if date_to else "",
        "filter_errors": filter_errors,
        "order_rows": selected_order_rows[:200] if selected_card else recent_order_rows,
        "grand_total_irr": sum(item["total_irr"] for item in card_summaries),
        "grand_order_count": sum(item["order_count"] for item in card_summaries),
    }
    return TemplateResponse(request, "admin/card_receipts_report.html", context)


def admin_order_changelist(params=None, *, store=None):
    query = dict(params or {})
    if store and getattr(store, "pk", None):
        query.setdefault("store__id__exact", store.pk)
    url = reverse("admin:store_order_changelist")
    if query:
        return f"{url}?{urlencode(query)}"
    return url


def admin_sms_changelist(params=None):
    query = urlencode(params or {})
    url = reverse("admin:payments_incomingpaymentsms_changelist")
    return f"{url}?{query}" if query else url


def order_review_url(order):
    return reverse("admin_store_order_review", args=[order.pk])


def order_workbench_url(section="", *, store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    if section:
        params["section"] = section
    url = reverse("admin_store_order_workbench")
    if params:
        url = f"{url}?{urlencode(params)}"
    if section:
        url = f"{url}#{section}"
    return url


def mask_phone(value):
    cleaned = "".join(ch for ch in normalize_payment_digits(value) if ch.isdigit() or ch == "+")
    if not cleaned:
        return ""
    prefix = cleaned[:4] if cleaned.startswith("+") else cleaned[:3]
    suffix = cleaned[-2:] if len(cleaned) > 5 else ""
    return f"{prefix}***{suffix}" if suffix else "***"


def safe_customer_name(customer):
    if not customer:
        return "مشتری ثبت نشده"
    phone = getattr(customer, "phone_number", "") or ""
    name = (getattr(customer, "display_name", "") or getattr(customer, "username", "") or "").strip()
    if phone and normalize_payment_digits(name) == normalize_payment_digits(phone):
        return mask_phone(phone)
    if name:
        return name
    return f"Customer {str(customer.public_id)[:8]}"


def money_label(amount, currency):
    labels = {
        Plan.Currency.TOMAN: "تومان",
        Plan.Currency.IRR: "ریال",
        Plan.Currency.USD: "USD",
    }
    return f"{int(amount or 0):,} {labels.get(currency, currency)}"


def order_status_label(status):
    return {
        Order.Status.PENDING_PAYMENT: "در انتظار پرداخت",
        Order.Status.PENDING_VERIFICATION: "در انتظار بررسی",
        Order.Status.CONFIRMED: "تایید پرداخت",
        Order.Status.COMPLETED: "تکمیل شده",
        Order.Status.REJECTED: "رد شده",
        Order.Status.CANCELLED: "لغو شده",
    }.get(status, status or "-")


def order_status_tone(status):
    return {
        Order.Status.PENDING_PAYMENT: "warning",
        Order.Status.PENDING_VERIFICATION: "warning",
        Order.Status.CONFIRMED: "info",
        Order.Status.COMPLETED: "success",
        Order.Status.REJECTED: "danger",
        Order.Status.CANCELLED: "secondary",
    }.get(status, "secondary")


def payment_status(order):
    if order.verification_status == Order.VerificationStatus.REJECTED or order.status == Order.Status.REJECTED:
        return "رد پرداخت", "danger"
    if order.verification_status == Order.VerificationStatus.VERIFIED:
        return "تایید شده", "success"
    if order.is_paid or order.payment_submitted_at or order.payment_receipt_image:
        return "منتظر بررسی", "warning"
    if order.status == Order.Status.PENDING_PAYMENT:
        return "منتظر پرداخت", "secondary"
    return "نامشخص", "secondary"


def prefetched_clients(order):
    return list(order.vpn_clients.all())


def delivery_status(order):
    metadata = order.metadata or {}
    if order.status in {Order.Status.REJECTED, Order.Status.CANCELLED}:
        return "متوقف", "secondary"
    if metadata.get("panel_provisioning_deferred") or metadata.get("panel_provisioning_last_failed_at"):
        return "خطای ساخت/تحویل", "danger"

    clients = prefetched_clients(order)
    if not clients:
        if order.status in {Order.Status.CONFIRMED, Order.Status.COMPLETED} or order.verification_status == Order.VerificationStatus.VERIFIED:
            return "کانفیگ ندارد", "danger"
        if order.is_paid:
            return "آماده بررسی", "warning"
        return "شروع نشده", "secondary"

    if any(client.status == VPNClient.Status.ERROR for client in clients):
        return "خطای کانفیگ", "danger"
    active_count = sum(1 for client in clients if client.status == VPNClient.Status.ACTIVE)
    if active_count == len(clients):
        return "فعال/تحویل شده", "success"
    if active_count:
        return "بخشی فعال", "warning"
    return "کانفیگ آماده فعال‌سازی", "info"


def route_status(order):
    inbound = getattr(order, "inbound", None)
    if not inbound:
        return "Route/Inbound ثبت نشده", "danger"
    panel = getattr(inbound, "panel", None)
    if not panel:
        return "Inbound بدون پنل", "danger"
    if not panel.is_active:
        return "پنل غیرفعال", "warning"
    if not inbound.is_active or not inbound.available_for_new_orders:
        return "Inbound نیازمند بررسی", "warning"
    return "Route آماده", "success"


def receipt_analysis(order):
    return (order.metadata or {}).get("receipt_analysis") or {}


def safe_action_message(message):
    text = str(message or "").strip()
    if not text:
        return "عملیات انجام نشد."
    if CONFIG_LINK_PATTERN.search(text) or SUB_LINK_PATTERN.search(text) or any(marker in text for marker in CONFIG_LINK_MARKERS):
        return "عملیات پیام حساسی برگرداند؛ جزئیات در UI مخفی شد."
    text = CONFIG_LINK_PATTERN.sub("<config-link-hidden>", text)
    text = SUB_LINK_PATTERN.sub("<subscription-link-hidden>", text)
    text = UUID_PATTERN.sub("<identifier-hidden>", text)
    text = EMAIL_PATTERN.sub("<email-hidden>", text)
    text = LONG_TOKEN_PATTERN.sub("<token-hidden>", text)
    if any(marker in text for marker in CONFIG_LINK_MARKERS):
        return "عملیات پیام حساسی برگرداند؛ جزئیات در UI مخفی شد."
    return text[:300]


def safe_short_text(value, limit=120):
    text = safe_action_message(value)
    return text[:limit] if text else ""


def mask_email(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return mask_identifier(text)
    local, domain = text.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[:2]}***@{domain}"


def safe_identifier_label(value):
    text = str(value or "").strip()
    if not text:
        return "-"
    if "@" in text:
        return mask_email(text)
    return mask_identifier(text)


def vpn_client_customer(vpn_client):
    if vpn_client.order_id and getattr(vpn_client.order, "customer_id", None):
        return vpn_client.order.customer
    trial = vpn_client.free_trial_requests.select_related("customer").filter(customer__isnull=False).first()
    return trial.customer if trial else None


def vpn_client_identifier(vpn_client):
    return str(vpn_client.uuid or vpn_client.xui_email or vpn_client.username or "").strip()


def vpn_client_action_payload(vpn_client):
    identifier = vpn_client_identifier(vpn_client)
    if not vpn_client.inbound_id or not getattr(vpn_client.inbound, "panel_id", None) or not identifier:
        return None
    return {
        "panel_id": vpn_client.inbound.panel_id,
        "inbound_id": vpn_client.inbound.inbound_id,
        "identifier": identifier,
        "vpn_client_id": vpn_client.pk,
    }


def service_review_url(vpn_client):
    return reverse("admin_store_service_review", args=[vpn_client.pk])


def customer_review_url(customer):
    return reverse("admin_store_customer_review", args=[customer.pk])


def service_workbench_url(section="", *, store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    if section:
        if section in REMOTE_FILTER_STATUSES:
            params["remote_status"] = section
        else:
            params["section"] = section
    url = reverse("admin_store_service_workbench")
    if params:
        url = f"{url}?{urlencode(params)}"
    if section and section not in REMOTE_FILTER_STATUSES:
        url = f"{url}#{section}"
    return url


def service_reconcile_url(store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    url = reverse("admin_store_services_reconcile")
    return f"{url}?{urlencode(params)}" if params else url


def service_bulk_cleanup_url(store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    url = reverse("admin_store_services_reconciliation_soft_delete_missing")
    return f"{url}?{urlencode(params)}" if params else url


def service_soft_delete_missing_url(vpn_client):
    return reverse("admin_store_service_soft_delete_missing", args=[vpn_client.pk])


def admin_vpn_client_changelist(params=None, *, store=None):
    query = dict(params or {})
    if store and getattr(store, "pk", None):
        query.setdefault("store__id__exact", store.pk)
    url = reverse("admin:store_vpnclient_changelist")
    if query:
        return f"{url}?{urlencode(query)}"
    return url


def admin_customer_changelist(params=None):
    query = urlencode(params or {})
    url = reverse("admin:store_customer_changelist")
    return f"{url}?{query}" if query else url


def gb_label_from_bytes(value):
    try:
        number = int(value or 0) / (1024 ** 3)
    except (TypeError, ValueError):
        number = 0
    if number >= 10:
        return f"{number:,.0f} GB"
    return f"{number:,.2f}".rstrip("0").rstrip(".") + " GB"


def days_left(expires_at):
    if not expires_at:
        return None
    delta = expires_at - timezone.now()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return 0
    return (seconds + 86_399) // 86_400


def days_left_label(expires_at):
    value = days_left(expires_at)
    return f"{value} روز باقی‌مانده" if value is not None else "نامشخص"


def status_tone(status):
    return {
        VPNClient.Status.ACTIVE: "success",
        VPNClient.Status.INACTIVE: "secondary",
        VPNClient.Status.CREATED: "info",
        VPNClient.Status.SUSPENDED: "warning",
        VPNClient.Status.EXPIRED: "warning",
        VPNClient.Status.DELETED: "secondary",
        VPNClient.Status.ERROR: "danger",
    }.get(status, "secondary")


def local_usage_summary(vpn_client):
    latest = vpn_client.usage_snapshots.order_by("-recorded_at").first()
    total = latest.total_traffic_bytes if latest else vpn_client.traffic_limit_bytes
    used = latest.used_traffic_bytes if latest else vpn_client.used_traffic_bytes
    remaining = latest.remaining_traffic_bytes if latest else vpn_client.remaining_traffic_bytes
    percent = 0
    if total:
        percent = min(100, max(0, int((int(used or 0) * 100) / int(total))))
    return {
        "total": total,
        "used": used,
        "remaining": remaining,
        "total_label": gb_label_from_bytes(total) if total else "نامشخص",
        "used_label": gb_label_from_bytes(used),
        "remaining_label": gb_label_from_bytes(remaining) if total else "نامشخص",
        "percent": percent,
        "snapshot": latest,
        "known": bool(total or used or latest),
    }


def local_bot_stats(vpn_client):
    usage = local_usage_summary(vpn_client)
    return {
        "panel_available": None,
        "total_traffic_bytes": usage["total"],
        "used_traffic_bytes": usage["used"],
        "remaining_traffic_bytes": usage["remaining"],
        "expiry_at": vpn_client.expires_at,
    }


def client_route_status(vpn_client):
    inbound = getattr(vpn_client, "inbound", None)
    if not inbound:
        return "Inbound ثبت نشده", "danger"
    panel = getattr(inbound, "panel", None)
    if not panel:
        return "Inbound بدون پنل", "danger"
    if not panel.is_active:
        return "پنل غیرفعال", "warning"
    if not inbound.is_active:
        return "Inbound غیرفعال", "warning"
    return "مسیر آماده", "success"


def customer_telegram_target_status(customer, store=None):
    if not customer:
        return "بدون مشتری", "danger", 0
    targets = telegram_targets_for_customer(customer, store=store)
    return ("متصل", "success", len(targets)) if targets else ("بدون مقصد تلگرام", "warning", 0)


def telegram_targets_for_customer(customer, store=None):
    if not customer:
        return []
    queryset = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=customer,
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(chat_id="")
    )
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))
    return list(queryset.order_by("-last_seen_at", "-created_at")[:5])


def service_row(vpn_client):
    customer = vpn_client_customer(vpn_client)
    usage = local_usage_summary(vpn_client)
    route_label, route_tone = client_route_status(vpn_client)
    telegram_label, telegram_tone, _target_count = customer_telegram_target_status(customer, vpn_client.store)
    service_label = safe_identifier_label(vpn_client.xui_email or vpn_client.username or vpn_client.public_id)
    remote_status = vpn_client.last_remote_check_status or VPNClient.RemoteCheckStatus.NOT_CHECKED
    remote_fresh = remote_status_is_fresh(vpn_client)
    remote_scope_current = remote_scope_matches_latest_check(vpn_client)
    remote_cleanup_eligible = remote_status_is_cleanup_eligible(vpn_client)
    return {
        "client": vpn_client,
        "service_label": service_label,
        "review_url": service_review_url(vpn_client),
        "change_url": reverse("admin:store_vpnclient_change", args=[vpn_client.pk]),
        "customer": customer,
        "customer_name": safe_customer_name(customer),
        "customer_url": customer_review_url(customer) if customer else "",
        "phone_masked": mask_phone(getattr(customer, "phone_number", "")) if customer else "",
        "plan": vpn_client.plan.name if vpn_client.plan_id else "-",
        "order_code": vpn_client.order.order_tracking_code if vpn_client.order_id else "",
        "panel": vpn_client.inbound.panel.name if vpn_client.inbound_id and vpn_client.inbound.panel_id else "",
        "inbound": vpn_client.inbound.remark or str(vpn_client.inbound.inbound_id) if vpn_client.inbound_id else "",
        "protocol": vpn_client.inbound.get_protocol_display() if vpn_client.inbound_id else "-",
        "status_label": vpn_client.get_status_display(),
        "status_tone": status_tone(vpn_client.status),
        "active_label": "فعال" if vpn_client.status == VPNClient.Status.ACTIVE else "غیرفعال",
        "remote_status": remote_status,
        "remote_status_label": "نیازمند بررسی مجدد"
        if vpn_client.last_remote_check_at and not remote_fresh
        else remote_status_label(remote_status),
        "remote_status_tone": "warning" if vpn_client.last_remote_check_at and not remote_fresh else remote_status_tone(remote_status),
        "last_remote_check_at": vpn_client.last_remote_check_at,
        "remote_check_error": safe_short_text(vpn_client.last_remote_check_error),
        "remote_check_fresh": remote_fresh,
        "remote_scope_current": remote_scope_current,
        "remote_cleanup_eligible": remote_cleanup_eligible,
        "soft_delete_missing_url": service_soft_delete_missing_url(vpn_client),
        "expires_at": vpn_client.expires_at,
        "days_left": days_left(vpn_client.expires_at),
        "days_left_label": days_left_label(vpn_client.expires_at),
        "usage": usage,
        "telegram_label": telegram_label,
        "telegram_tone": telegram_tone,
        "route_label": route_label,
        "route_tone": route_tone,
        "has_subscription_link": bool(vpn_client.sub_link),
        "has_direct_link": bool(vpn_client.direct_link),
        "created_at": vpn_client.created_at,
    }


class AdminServiceActionError(Exception):
    pass


def order_row(order):
    payment_label, payment_tone = payment_status(order)
    delivery_label, delivery_tone = delivery_status(order)
    phone = getattr(order.customer, "phone_number", "") if order.customer_id else ""
    bot_users = list(order.customer.bot_users.all()) if order.customer_id else []
    return {
        "order": order,
        "tracking_code": order.order_tracking_code,
        "review_url": order_review_url(order),
        "change_url": reverse("admin:store_order_change", args=[order.pk]),
        "customer_name": safe_customer_name(order.customer),
        "phone_masked": mask_phone(phone),
        "telegram_status": "متصل" if bot_users else "بدون مقصد تلگرام",
        "plan": order.plan.name if order.plan_id else "-",
        "plan_review_url": catalog_plan_review_url(order.plan) if order.plan_id else "",
        "operator": order.operator.name if order.operator_id else "",
        "amount": money_label(order.amount, order.currency),
        "status_label": order_status_label(order.status),
        "status_tone": order_status_tone(order.status),
        "payment_label": payment_label,
        "payment_tone": payment_tone,
        "delivery_label": delivery_label,
        "delivery_tone": delivery_tone,
        "has_receipt": bool(order.payment_receipt_image),
        "created_at": order.created_at,
    }


def order_list(queryset, limit=ORDER_WORKBENCH_LIMIT):
    orders = (
        queryset.select_related("store", "customer", "plan", "operator", "inbound", "inbound__panel")
        .prefetch_related("vpn_clients", "incoming_payment_sms", "customer__bot_users")
        .order_by("-created_at", "-pk")[:limit]
    )
    return [order_row(order) for order in orders]


def base_workbench_orders(store=None):
    queryset = Order.objects.all()
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(store=store)
    return queryset


def build_workbench_context(selected_store):
    now = timezone.now()
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    base = base_workbench_orders(selected_store)

    needs_review = base.filter(
        payment_method=Order.PaymentMethod.MANUAL_CARD,
        verification_status=Order.VerificationStatus.PENDING,
        status__in=PENDING_REVIEW_STATUSES,
    ).filter(Q(payment_receipt_image__isnull=False) | Q(payment_submitted_at__isnull=False) | Q(is_paid=True))

    ready_action = base.filter(
        Q(status=Order.Status.CONFIRMED)
        | Q(is_paid=True, verification_status=Order.VerificationStatus.VERIFIED, status__in=PENDING_REVIEW_STATUSES)
        | Q(metadata__panel_provisioning_deferred=True)
        | Q(provisioning_status__in=[Order.ProvisioningStatus.PENDING, Order.ProvisioningStatus.PROVISIONING, Order.ProvisioningStatus.FAILED])
        | Q(vpn_clients__status__in=[VPNClient.Status.ERROR, VPNClient.Status.CREATED, VPNClient.Status.INACTIVE])
    ).exclude(status__in=[Order.Status.REJECTED, Order.Status.CANCELLED]).distinct()

    today_orders = base.filter(created_at__gte=today_start, created_at__lt=today_end)

    problematic = base.filter(
        Q(metadata__receipt_analysis__status__in=["mismatch", "not_found", "unsupported_currency", "image_only"])
        | Q(metadata__panel_provisioning_deferred=True)
        | Q(provisioning_status=Order.ProvisioningStatus.FAILED)
        | Q(vpn_clients__status=VPNClient.Status.ERROR)
        | Q(status__in=[Order.Status.CONFIRMED, Order.Status.COMPLETED], inbound__isnull=True)
        | Q(customer__isnull=True)
        | Q(customer__bot_users__isnull=True, status__in=[Order.Status.CONFIRMED, Order.Status.COMPLETED])
    ).distinct()

    completed = base.filter(status=Order.Status.COMPLETED)
    rejected_today = today_orders.filter(status__in=[Order.Status.REJECTED, Order.Status.CANCELLED]).count()
    completed_today = today_orders.filter(status=Order.Status.COMPLETED).count()
    sms_no_match_count = IncomingPaymentSMS.objects.filter(status=IncomingPaymentSMS.Status.NO_MATCH).count()

    sections = [
        {
            "key": "needs-review",
            "title": "نیازمند بررسی",
            "description": "رسید یا پرداختی که owner باید تایید یا رد کند.",
            "count": needs_review.count(),
            "tone": "warning",
            "items": order_list(needs_review),
            "link": admin_order_changelist(
                {
                    "status__exact": Order.Status.PENDING_VERIFICATION,
                    "verification_status__exact": Order.VerificationStatus.PENDING,
                },
                store=selected_store,
            ),
        },
        {
            "key": "ready-action",
            "title": "آماده اقدام",
            "description": "پرداخت تایید شده یا کانفیگ آماده‌ای که هنوز نیاز به اقدام دستی دارد.",
            "count": ready_action.count(),
            "tone": "info",
            "items": order_list(ready_action),
            "link": admin_order_changelist({"status__exact": Order.Status.CONFIRMED}, store=selected_store),
        },
        {
            "key": "today",
            "title": "امروز",
            "description": f"تکمیل‌شده امروز: {completed_today:,} | رد/لغو امروز: {rejected_today:,}",
            "count": today_orders.count(),
            "tone": "primary",
            "items": order_list(today_orders),
            "link": admin_order_changelist(store=selected_store),
        },
        {
            "key": "problematic",
            "title": "مشکل‌دار",
            "description": f"خطای route/delivery/payment matching. SMS بدون match: {sms_no_match_count:,}",
            "count": problematic.count() + sms_no_match_count,
            "tone": "danger",
            "items": order_list(problematic),
            "link": admin_order_changelist({"delivery_status": "failed"}, store=selected_store),
            "extra_links": [
                {"label": "SMSهای بدون match", "url": admin_sms_changelist({"status__exact": IncomingPaymentSMS.Status.NO_MATCH})}
            ],
        },
        {
            "key": "completed",
            "title": "تکمیل‌شده",
            "description": "آخرین سفارش‌های تکمیل‌شده برای پیگیری سریع.",
            "count": completed.count(),
            "tone": "success",
            "items": order_list(completed),
            "link": admin_order_changelist({"status__exact": Order.Status.COMPLETED}, store=selected_store),
        },
    ]
    return {
        "sections": sections,
        "summary_counts": {
            "needs_review": sections[0]["count"],
            "ready_action": sections[1]["count"],
            "today_total": sections[2]["count"],
            "problematic": sections[3]["count"],
            "completed": sections[4]["count"],
            "completed_today": completed_today,
            "rejected_today": rejected_today,
            "sms_no_match": sms_no_match_count,
        },
        "section_limit": ORDER_WORKBENCH_LIMIT,
    }


@require_admin_capability("orders.view")
def order_workbench(request):
    stores, selected_store = selected_store_from_id(request.GET.get("store"))
    workbench_context = build_workbench_context(selected_store)
    context = {
        **admin.site.each_context(request),
        **workbench_context,
        "stores": stores,
        "selected_store": selected_store,
        "title": "میز کار سفارش‌ها",
        "subtitle": "رسید، تایید پرداخت، ساخت/تحویل کانفیگ و خطاهای روزانه owner.",
    }
    return TemplateResponse(request, "admin/store/orders/workbench.html", context)


@require_admin_capability("support.view")
def support_workbench(request):
    stores, selected_store = selected_store_from_id(request.GET.get("store"))
    workbench_context = get_support_workbench_context(selected_store)
    context = {
        **admin.site.each_context(request),
        **workbench_context,
        "stores": stores,
        "selected_store": selected_store,
        "title": "میز کار پشتیبانی",
        "subtitle": "پیام‌ها، گفتگوها، مشتری و مسیرهای مرتبط بدون نمایش داده حساس.",
    }
    return TemplateResponse(request, "admin/store/support/workbench.html", context)


def handle_support_review_action(request, conversation):
    action = request.POST.get("action", "")
    review_url = support_review_url(conversation)
    if action not in {"reply", "close", "reopen", "mark_followup"}:
        messages.error(request, "Action معتبر نیست.")
        return redirect(review_url)
    ensure_admin_capability(request.user, "support.reply")

    if action == "reply":
        if request.POST.get("confirm_customer") != "1" and request.POST.get("confirm_external") != "1":
            messages.error(request, "برای ارسال پاسخ باید تایید صریح owner ثبت شود.")
            return redirect(review_url)
        result = send_support_reply(conversation, request.POST.get("message", ""), request.user)
        if result.duplicate:
            messages.success(request, "این پاسخ قبلاً ثبت شده بود و دوباره ارسال نشد.")
        elif result.ok:
            messages.success(request, f"پاسخ برای {result.sent_count} مقصد تلگرام همین مشتری ارسال شد.")
        else:
            messages.error(request, safe_action_message(result.safe_error))
        return redirect(review_url)

    if request.POST.get("confirm_action") != "1" and request.POST.get("confirm_external") != "1":
        messages.error(request, "برای تغییر وضعیت تیکت باید تایید صریح ثبت شود.")
        return redirect(review_url)

    if action == "close":
        if conversation.status == SupportConversation.Status.CLOSED:
            messages.success(request, "گفتگو قبلاً بسته شده است.")
        else:
            conversation.close()
            messages.success(request, "گفتگوی پشتیبانی بسته شد.")
        return redirect(review_url)

    if action == "reopen":
        if conversation.status != SupportConversation.Status.CLOSED:
            messages.success(request, "گفتگو قبلاً باز است.")
        else:
            conversation.status = SupportConversation.Status.OPEN
            conversation.closed_at = None
            conversation.save(update_fields=["status", "closed_at", "updated_at"])
            messages.success(request, "گفتگوی پشتیبانی دوباره باز شد.")
        return redirect(review_url)

    if conversation.status == SupportConversation.Status.WAITING_ADMIN:
        messages.success(request, "گفتگو قبلاً نیازمند پیگیری است.")
    else:
        conversation.status = SupportConversation.Status.WAITING_ADMIN
        conversation.closed_at = None
        conversation.save(update_fields=["status", "closed_at", "updated_at"])
        messages.success(request, "گفتگو به وضعیت نیازمند پیگیری منتقل شد.")
    return redirect(review_url)


@require_admin_capability("support.view")
def support_review(request, support_id):
    conversation = get_object_or_404(
        SupportConversation.objects.select_related("store", "customer"),
        pk=support_id,
    )
    if request.method == "POST":
        return handle_support_review_action(request, conversation)

    review_context = get_support_review_context(conversation.pk)
    if not user_has_capability(request.user, "support.reply"):
        for key in ("can_reply", "can_close", "can_reopen", "can_mark_followup"):
            review_context["actions"][key] = False
        review_context["customer_summary"]["message_url"] = ""
    context = {
        **admin.site.each_context(request),
        **review_context,
        "title": f"بررسی پشتیبانی #{conversation.pk}",
        "subtitle": "Timeline و پاسخ owner-facing فقط برای همین گفتگو.",
    }
    return TemplateResponse(request, "admin/store/support/review.html", context)


def base_service_clients(store=None):
    queryset = (
        VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .prefetch_related("free_trial_requests__customer")
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
    )
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(store=store)
    return queryset


def service_list(queryset, limit=SERVICE_WORKBENCH_LIMIT):
    return [service_row(client) for client in queryset.order_by("-created_at", "-pk")[:limit]]


def service_section(key, title, description, queryset, *, tone="info", link="", limit=SERVICE_WORKBENCH_LIMIT):
    return {
        "key": key,
        "title": title,
        "description": description,
        "count": queryset.count(),
        "tone": tone,
        "items": service_list(queryset, limit=limit),
        "link": link,
    }


def _apply_remote_status_filter(queryset, remote_filter):
    statuses = REMOTE_FILTER_STATUSES.get(remote_filter)
    if not statuses:
        return queryset
    if remote_filter == "not-checked":
        return queryset.filter(Q(last_remote_check_status__in=statuses) | Q(last_remote_check_at__isnull=True))
    return queryset.filter(last_remote_check_status__in=statuses)


def build_service_workbench_context(selected_store, remote_filter=""):
    now = timezone.now()
    soon = now + timedelta(days=3)
    unfiltered_base = base_service_clients(selected_store)
    reconciliation_summary = get_reconciliation_summary(unfiltered_base)
    base = _apply_remote_status_filter(unfiltered_base, remote_filter)
    active = base.filter(status=VPNClient.Status.ACTIVE)
    expiring = active.filter(expires_at__gte=now, expires_at__lte=soon)
    expired = base.filter(Q(status=VPNClient.Status.EXPIRED) | Q(expires_at__lt=now)).distinct()
    suspicious_low_usage = active.filter(created_at__lte=now - timedelta(days=1), used_traffic_bytes=0)
    needs_telegram = (
        base.filter(order__customer__isnull=False)
        .annotate(
            telegram_target_count=Count(
                "order__customer__bot_users",
                filter=Q(
                    order__customer__bot_users__is_active=True,
                    order__customer__bot_users__bot_config__is_active=True,
                    order__customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                ),
                distinct=True,
            )
        )
        .filter(telegram_target_count=0)
    )
    needs_attention = base.filter(
        Q(status__in=[VPNClient.Status.ERROR, VPNClient.Status.CREATED])
        | Q(inbound__isnull=True)
        | Q(inbound__panel__isnull=True)
        | Q(inbound__is_active=False)
        | Q(inbound__panel__is_active=False)
        | Q(order__metadata__panel_provisioning_deferred=True)
    ).distinct()
    recent_customers = Customer.objects.annotate(
        admin_orders_count=Count("orders", distinct=True),
        admin_active_clients_count=Count(
            "orders__vpn_clients",
            filter=Q(orders__vpn_clients__status=VPNClient.Status.ACTIVE),
            distinct=True,
        ),
        admin_last_order_at=Max("orders__created_at"),
        admin_telegram_count=Count(
            "bot_users",
            filter=Q(
                bot_users__is_active=True,
                bot_users__bot_config__is_active=True,
                bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
            ),
            distinct=True,
        ),
    )
    if selected_store and getattr(selected_store, "pk", None):
        recent_customers = recent_customers.filter(Q(orders__store=selected_store) | Q(orders__isnull=True)).distinct()
    recent_customers = recent_customers.order_by("-created_at", "-pk")[:SERVICE_WORKBENCH_LIMIT]

    sections = [
        service_section(
            "active",
            "سرویس‌های فعال",
            "سرویس‌هایی که status فعال دارند.",
            active,
            tone="success",
            link=admin_vpn_client_changelist({"status__exact": VPNClient.Status.ACTIVE}, store=selected_store),
        ),
        service_section(
            "expiring",
            "در حال انقضا",
            "سرویس‌های فعال که تا ۳ روز آینده منقضی می‌شوند.",
            expiring,
            tone="warning",
            link=admin_vpn_client_changelist({"status__exact": VPNClient.Status.ACTIVE}, store=selected_store),
        ),
        service_section(
            "expired",
            "منقضی‌شده",
            "سرویس‌هایی که منقضی شده‌اند یا تاریخ پایان گذشته دارند.",
            expired,
            tone="warning",
            link=admin_vpn_client_changelist({"status__exact": VPNClient.Status.EXPIRED}, store=selected_store),
        ),
        service_section(
            "low-usage",
            "کم‌مصرف / مشکوک",
            "فعال است اما بعد از ۲۴ ساعت مصرفی در DB دیده نمی‌شود.",
            suspicious_low_usage,
            tone="info",
            link=admin_vpn_client_changelist({"status__exact": VPNClient.Status.ACTIVE}, store=selected_store),
        ),
        service_section(
            "needs-telegram",
            "نیازمند لینک تلگرام",
            "مشتری مقصد تلگرام فعال ندارد؛ resend از admin ممکن نیست.",
            needs_telegram,
            tone="warning",
            link=admin_vpn_client_changelist(store=selected_store),
        ),
        service_section(
            "attention",
            "خطادار / نیازمند بررسی",
            "missing panel/inbound/route یا خطای provisioning ذخیره‌شده در DB.",
            needs_attention,
            tone="danger",
            link=admin_vpn_client_changelist({"status__exact": VPNClient.Status.ERROR}, store=selected_store),
        ),
    ]
    remote_filters = [
        {
            "key": "",
            "label": "همه",
            "url": service_workbench_url(store=selected_store),
            "active": not remote_filter,
            "count": unfiltered_base.count(),
        },
        {
            "key": "remote-active",
            "label": "فعال در پنل",
            "url": service_workbench_url("remote-active", store=selected_store),
            "active": remote_filter == "remote-active",
            "count": reconciliation_summary["remote_active"],
        },
        {
            "key": "remote-disabled",
            "label": "غیرفعال در پنل",
            "url": service_workbench_url("remote-disabled", store=selected_store),
            "active": remote_filter == "remote-disabled",
            "count": reconciliation_summary["remote_disabled"],
        },
        {
            "key": "remote-missing",
            "label": "حذف‌شده از پنل",
            "url": service_workbench_url("remote-missing", store=selected_store),
            "active": remote_filter == "remote-missing",
            "count": reconciliation_summary["remote_missing"],
        },
        {
            "key": "remote-problem",
            "label": "خطادار / نامشخص",
            "url": service_workbench_url("remote-problem", store=selected_store),
            "active": remote_filter == "remote-problem",
            "count": reconciliation_summary["unknown_or_error"] + reconciliation_summary["ambiguous"],
        },
        {
            "key": "not-checked",
            "label": "بررسی‌نشده",
            "url": service_workbench_url("not-checked", store=selected_store),
            "active": remote_filter == "not-checked",
            "count": reconciliation_summary["not_checked"],
        },
    ]
    return {
        "sections": sections,
        "reconciliation_summary": reconciliation_summary,
        "remote_filters": remote_filters,
        "current_remote_filter": remote_filter,
        "reconcile_url": service_reconcile_url(selected_store),
        "bulk_cleanup_url": service_bulk_cleanup_url(selected_store),
        "bulk_cleanup_enabled": reconciliation_summary["cleanup_candidate_count"] > 0,
        "bulk_cleanup_confirmation": RECONCILIATION_CONFIRMATION,
        "summary_counts": {
            "active": sections[0]["count"],
            "expiring": sections[1]["count"],
            "expired": sections[2]["count"],
            "low_usage": sections[3]["count"],
            "needs_telegram": sections[4]["count"],
            "attention": sections[5]["count"],
            "recent_customers": len(recent_customers),
        },
        "recent_customers": [
            {
                "customer": customer,
                "name": safe_customer_name(customer),
                "phone_masked": mask_phone(customer.phone_number),
                "telegram_label": "متصل" if getattr(customer, "admin_telegram_count", 0) else "بدون مقصد تلگرام",
                "orders_count": getattr(customer, "admin_orders_count", 0) or 0,
                "active_clients_count": getattr(customer, "admin_active_clients_count", 0) or 0,
                "last_order_at": getattr(customer, "admin_last_order_at", None),
                "review_url": customer_review_url(customer),
                "change_url": reverse("admin:store_customer_change", args=[customer.pk]),
            }
            for customer in recent_customers
        ],
        "section_limit": SERVICE_WORKBENCH_LIMIT,
    }


@require_admin_capability("services.view")
def service_workbench(request):
    stores, selected_store = selected_store_from_id(request.GET.get("store"))
    remote_filter = request.GET.get("remote_status") or ""
    if remote_filter not in REMOTE_FILTER_STATUSES:
        remote_filter = ""
    workbench_context = build_service_workbench_context(selected_store, remote_filter=remote_filter)
    context = {
        **admin.site.each_context(request),
        **workbench_context,
        "stores": stores,
        "selected_store": selected_store,
        "can_modify_services": user_has_capability(request.user, "services.modify"),
        "title": "میز کار سرویس‌ها",
        "subtitle": "مشتری، وضعیت سرویس، مصرف، انقضا و actionهای explicit بدون نمایش secret.",
    }
    return TemplateResponse(request, "admin/store/services/workbench.html", context)


@require_admin_capability("services.modify")
def service_reconcile(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    stores, selected_store = selected_store_from_id(request.GET.get("store") or request.POST.get("store"))
    if request.POST.get("confirm_reconcile") != "1":
        messages.error(request, "برای بررسی سرویس‌ها باید تایید owner ثبت شود.")
        return redirect(service_workbench_url(store=selected_store))
    queryset = base_service_clients(selected_store)
    client_ids = [value for value in request.POST.getlist("client_ids") if str(value).strip()]
    if client_ids:
        queryset = queryset.filter(pk__in=client_ids)
    try:
        result = reconcile_vpn_clients(queryset, actor=request.user)
    except Exception:
        logger.exception("Service reconciliation failed")
        messages.error(request, "بررسی سرویس‌ها ناموفق بود. جزئیات امن در لاگ سرور ثبت شد.")
        return redirect(service_workbench_url(store=selected_store))
    request.session["service_reconciliation_last_batch_id"] = result.batch_id
    messages.success(
        request,
        (
            f"بررسی سرویس‌ها انجام شد: {result.checked} سرویس در {result.scopes} scope "
            f"با {result.api_calls} درخواست پنل. Batch: {result.batch_id[:8]}"
        ),
    )
    url = service_workbench_url(store=selected_store)
    separator = "&" if "?" in url else "?"
    return redirect(f"{url}{separator}batch={result.batch_id}")


@require_admin_capability("services.modify")
def service_soft_delete_missing(request, vpn_client_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    vpn_client = get_object_or_404(
        VPNClient.objects.select_related("store", "inbound", "inbound__panel"),
        pk=vpn_client_id,
    )
    if request.POST.get("confirm_soft_delete") != RECONCILIATION_CONFIRMATION:
        messages.error(request, "عبارت تایید حذف نرم معتبر نیست.")
        return redirect(service_review_url(vpn_client))
    result = soft_delete_remote_missing_clients([vpn_client.pk], actor=request.user)
    if result["deleted"]:
        messages.success(request, "سرویس به صورت حذف نرم از قاصدک خارج شد؛ سفارش، مشتری و سوابق حفظ شدند.")
        return redirect(service_workbench_url("remote-missing", store=vpn_client.store))
    messages.error(request, "حذف نرم انجام نشد؛ نتیجه revalidation تازه یا scope دقیق برای حذف معتبر نبود.")
    return redirect(service_review_url(vpn_client))


@require_admin_capability("services.modify")
def service_bulk_soft_delete_missing(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    _stores, selected_store = selected_store_from_id(request.GET.get("store") or request.POST.get("store"))
    if request.POST.get("confirm_bulk_soft_delete") != RECONCILIATION_CONFIRMATION:
        messages.error(request, "عبارت تایید پاک‌سازی گروهی معتبر نیست.")
        return redirect(service_workbench_url("remote-missing", store=selected_store))
    summary = get_reconciliation_summary(base_service_clients(selected_store))
    candidate_ids = summary["cleanup_candidate_ids"]
    result = soft_delete_remote_missing_clients(candidate_ids, actor=request.user)
    if result["deleted"]:
        messages.success(
            request,
            f"{result['deleted']} سرویس حذف نرم شد. {result['blocked']} مورد به دلیل revalidation/scope حذف نشد.",
        )
    else:
        messages.error(request, "هیچ سرویسی حذف نرم نشد؛ ممکن است نتیجه check قدیمی شده یا revalidation ناموفق بوده باشد.")
    return redirect(service_workbench_url("remote-missing", store=selected_store))


def get_review_vpn_client(vpn_client_id):
    return get_object_or_404(
        VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .prefetch_related("free_trial_requests__customer", "usage_snapshots", "reminder_logs", "action_logs"),
        pk=vpn_client_id,
    )


def latest_failed_action_error(vpn_client):
    log = vpn_client.action_logs.filter(status="failed").order_by("-created_at").first()
    return safe_action_message(log.error_message) if log and log.error_message else ""


def resend_vpn_client_config_to_telegram(vpn_client):
    customer = vpn_client_customer(vpn_client)
    if not customer:
        raise AdminServiceActionError("برای این سرویس مشتری مشخص ثبت نشده است.")
    if not (vpn_client.sub_link or vpn_client.direct_link):
        raise AdminServiceActionError("برای این سرویس لینک کانفیگ ذخیره نشده است.")
    targets = telegram_targets_for_customer(customer, store=vpn_client.store)
    if not targets:
        raise AdminServiceActionError("برای این مشتری مقصد تلگرام فعال ثبت نشده است.")

    from .telegram_bot.client import BotClient, BotDeliveryError
    from .telegram_bot.services_flow import send_client_config_messages

    sent = 0
    last_error = ""
    for bot_user in targets:
        try:
            send_client_config_messages(
                BotClient(bot_user.bot_config),
                vpn_client,
                chat_id=bot_user.chat_id,
                bot_user=bot_user,
                sync_stats=local_bot_stats,
            )
            sent += 1
        except BotDeliveryError as exc:
            last_error = safe_action_message(exc)
            logger.warning(
                "Admin service resend failed vpn_client=%s bot_user=%s error=%s",
                vpn_client.pk,
                bot_user.pk,
                last_error,
            )
    if not sent:
        raise AdminServiceActionError(last_error or "ارسال تلگرام انجام نشد.")
    return sent


def handle_service_review_action(request, vpn_client):
    action = request.POST.get("action", "")
    review_url = service_review_url(vpn_client)
    allowed_actions = {
        "resend_config",
        "refresh_config_link",
        "update_usage",
        "disable_client",
        "enable_client",
        "reconcile_client",
        "soft_delete_remote_missing",
    }
    if action not in allowed_actions:
        messages.error(request, "Action معتبر نیست.")
        return redirect(review_url)
    if action == "resend_config":
        ensure_admin_capability(request.user, "services.send_config")
    else:
        ensure_admin_capability(request.user, "services.modify")
    if action == "soft_delete_remote_missing":
        if request.POST.get("confirm_soft_delete") != RECONCILIATION_CONFIRMATION:
            messages.error(request, "عبارت تایید حذف نرم معتبر نیست.")
            return redirect(review_url)
        result = soft_delete_remote_missing_clients([vpn_client.pk], actor=request.user)
        if result["deleted"]:
            messages.success(request, "سرویس به صورت حذف نرم از قاصدک خارج شد؛ سفارش، مشتری و سوابق حفظ شدند.")
            return redirect(service_workbench_url("remote-missing", store=vpn_client.store))
        messages.error(request, "حذف نرم انجام نشد؛ نتیجه revalidation تازه یا scope دقیق برای حذف معتبر نبود.")
        return redirect(review_url)

    if action == "reconcile_client":
        if request.POST.get("confirm_reconcile") != "1":
            messages.error(request, "برای بررسی این سرویس باید تایید owner ثبت شود.")
            return redirect(review_url)
        try:
            result = reconcile_vpn_clients(VPNClient.objects.filter(pk=vpn_client.pk), actor=request.user)
        except Exception:
            logger.exception("Single service reconciliation failed vpn_client=%s", vpn_client.pk)
            messages.error(request, "بررسی پنل ناموفق بود. جزئیات امن در لاگ سرور ثبت شد.")
            return redirect(review_url)
        messages.success(request, f"بررسی همین سرویس انجام شد. Batch: {result.batch_id[:8]}")
        return redirect(review_url)

    if request.POST.get("confirm_external") != "1":
        messages.error(request, "برای اجرای این action باید تایید صریح owner ثبت شود.")
        return redirect(review_url)

    try:
        if action == "resend_config":
            sent = resend_vpn_client_config_to_telegram(vpn_client)
            messages.success(request, f"کانفیگ بدون نمایش لینک در UI برای {sent} مقصد تلگرام ارسال شد.")
            return redirect(review_url)

        if action == "refresh_config_link":
            payload = vpn_client_action_payload(vpn_client)
            if not payload:
                messages.error(request, "این سرویس به panel/inbound/identifier معتبر وصل نیست.")
                return redirect(review_url)
            refresh_vpn_client_link_by_admin(f"web-admin:{request.user.pk}", payload)
            messages.success(request, "لینک کانفیگ refresh شد؛ مقدار کامل در UI نمایش داده نمی‌شود.")
            return redirect(review_url)

        if action == "update_usage":
            stats = sync_vpn_client_stats(vpn_client, force=True, create_snapshot=True)
            if stats.get("panel_available") is False:
                messages.error(request, safe_action_message(stats.get("error") or "به‌روزرسانی usage از پنل انجام نشد."))
            else:
                messages.success(request, "Usage از پنل به‌روزرسانی شد.")
            return redirect(review_url)

        payload = vpn_client_action_payload(vpn_client)
        if not payload:
            messages.error(request, "این سرویس به panel/inbound/identifier معتبر وصل نیست.")
            return redirect(review_url)
        set_vpn_client_enabled_by_admin(
            f"web-admin:{request.user.pk}",
            payload,
            enabled=(action == "enable_client"),
        )
        if action == "enable_client":
            messages.success(request, "سرویس فعال شد.")
        else:
            messages.success(request, "سرویس غیرفعال شد.")
        return redirect(review_url)
    except (AdminServiceActionError, VPNClientManagementError) as exc:
        messages.error(request, safe_action_message(exc))
        return redirect(review_url)
    except Exception:
        logger.exception("Admin service review action failed action=%s vpn_client=%s", action, vpn_client.pk)
        messages.error(request, "عملیات ناموفق بود. جزئیات امن در لاگ سرور ثبت شد.")
        return redirect(review_url)


def build_service_review_context(vpn_client):
    customer = vpn_client_customer(vpn_client)
    usage = local_usage_summary(vpn_client)
    route_label, route_tone = client_route_status(vpn_client)
    telegram_label, telegram_tone, telegram_target_count = customer_telegram_target_status(customer, vpn_client.store)
    last_order = (
        Order.objects.filter(customer=customer).order_by("-created_at").first()
        if customer
        else None
    )
    latest_reminder = vpn_client.reminder_logs.order_by("-created_at").first()
    payload = vpn_client_action_payload(vpn_client)
    delivery_error = latest_failed_action_error(vpn_client)
    if not delivery_error and vpn_client.order_id:
        delivery_error = safe_action_message((vpn_client.order.metadata or {}).get("delivery_error", ""))
    remote_status = vpn_client.last_remote_check_status or VPNClient.RemoteCheckStatus.NOT_CHECKED
    remote_fresh = remote_status_is_fresh(vpn_client)
    remote_scope_current = remote_scope_matches_latest_check(vpn_client)
    remote_warning = ""
    if vpn_client.last_remote_check_at and not remote_fresh:
        remote_warning = "نتیجه بررسی قدیمی است؛ قبل از پاک‌سازی دوباره بررسی کن."
    elif vpn_client.last_remote_check_at and not remote_scope_current:
        remote_warning = "scope سرویس بعد از آخرین بررسی تغییر کرده است؛ نتیجه قبلی معتبر نیست."
    elif remote_status == VPNClient.RemoteCheckStatus.REMOTE_MISSING:
        remote_warning = "این سرویس در آخرین بررسی معتبر روی پنل پیدا نشده است."
    return {
        "row": service_row(vpn_client),
        "customer_summary": {
            "customer": customer,
            "name": safe_customer_name(customer),
            "phone_masked": mask_phone(getattr(customer, "phone_number", "")) if customer else "",
            "username_masked": safe_identifier_label(getattr(customer, "username", "")) if customer else "",
            "telegram_label": telegram_label,
            "telegram_tone": telegram_tone,
            "telegram_target_count": telegram_target_count,
            "last_order": last_order,
            "last_order_url": order_review_url(last_order) if last_order else "",
            "review_url": customer_review_url(customer) if customer else "",
            "message_url": customer_message_url(customer) if customer else "",
            "support_history_url": customer_review_url(customer) if customer else "",
            "change_url": reverse("admin:store_customer_change", args=[customer.pk]) if customer else "",
        },
        "service_summary": {
            "plan": vpn_client.plan.name if vpn_client.plan_id else "-",
            "order": vpn_client.order,
            "order_url": order_review_url(vpn_client.order) if vpn_client.order_id else "",
            "panel": vpn_client.inbound.panel.name if vpn_client.inbound_id and vpn_client.inbound.panel_id else "-",
            "inbound": vpn_client.inbound.remark or vpn_client.inbound.inbound_id if vpn_client.inbound_id else "-",
            "safe_scope": safe_remote_scope(vpn_client),
            "protocol": vpn_client.inbound.get_protocol_display() if vpn_client.inbound_id else "-",
            "status_label": vpn_client.get_status_display(),
            "status_tone": status_tone(vpn_client.status),
            "created_at": vpn_client.created_at,
            "expires_at": vpn_client.expires_at,
            "days_left": days_left(vpn_client.expires_at),
            "days_left_label": days_left_label(vpn_client.expires_at),
            "activated_at": vpn_client.activated_at,
            "last_synced_at": vpn_client.last_synced_at,
            "last_online_at": vpn_client.last_online_at,
            "usage": usage,
        },
        "reconciliation_summary": {
            "status": remote_status,
            "label": "نیازمند بررسی مجدد" if vpn_client.last_remote_check_at and not remote_fresh else remote_status_label(remote_status),
            "tone": "warning" if vpn_client.last_remote_check_at and not remote_fresh else remote_status_tone(remote_status),
            "checked_at": vpn_client.last_remote_check_at,
            "batch_id": vpn_client.last_remote_check_batch_id[:8] if vpn_client.last_remote_check_batch_id else "",
            "error": safe_short_text(vpn_client.last_remote_check_error),
            "fresh": remote_fresh,
            "scope_current": remote_scope_current,
            "warning": remote_warning,
            "confirmation": RECONCILIATION_CONFIRMATION,
        },
        "delivery_summary": {
            "config_exists": bool(vpn_client.uuid or vpn_client.xui_email or vpn_client.username),
            "subscription_status": "ذخیره شده و مخفی" if vpn_client.sub_link else "ثبت نشده",
            "direct_status": "ذخیره شده و مخفی" if vpn_client.direct_link else "ثبت نشده",
            "delivered_to_telegram": "ثبت جداگانه ندارد",
            "last_error": delivery_error,
        },
        "health_summary": {
            "route_label": route_label,
            "route_tone": route_tone,
            "telegram_label": telegram_label,
            "telegram_tone": telegram_tone,
            "reminder_label": latest_reminder.get_status_display() if latest_reminder else "ثبت نشده",
            "reminder_at": latest_reminder.sent_at or latest_reminder.created_at if latest_reminder else None,
        },
        "actions": {
            "can_resend_config": bool(customer and (vpn_client.sub_link or vpn_client.direct_link)),
            "can_refresh_config": bool(payload),
            "can_update_usage": bool(payload),
            "can_disable": bool(payload and vpn_client.status != VPNClient.Status.DELETED),
            "can_enable": bool(payload and vpn_client.status != VPNClient.Status.DELETED),
            "can_reconcile": bool(vpn_client.status != VPNClient.Status.DELETED),
            "can_soft_delete_remote_missing": remote_status_is_cleanup_eligible(vpn_client),
        },
        "workbench_url": service_workbench_url(store=vpn_client.store),
        "admin_change_url": reverse("admin:store_vpnclient_change", args=[vpn_client.pk]),
    }


@require_admin_capability("services.view")
def service_review(request, vpn_client_id):
    vpn_client = get_review_vpn_client(vpn_client_id)
    if request.method == "POST":
        return handle_service_review_action(request, vpn_client)

    review_context = build_service_review_context(vpn_client)
    review_context["actions"]["can_resend_config"] = review_context["actions"]["can_resend_config"] and user_has_capability(
        request.user, "services.send_config"
    )
    for key in ("can_refresh_config", "can_update_usage", "can_disable", "can_enable", "can_reconcile", "can_soft_delete_remote_missing"):
        review_context["actions"][key] = review_context["actions"][key] and user_has_capability(request.user, "services.modify")
    if not user_has_capability(request.user, "support.reply"):
        review_context["customer_summary"]["message_url"] = ""
    if not user_has_capability(request.user, "support.view"):
        review_context["customer_summary"]["support_history_url"] = ""
    context = {
        **admin.site.each_context(request),
        **review_context,
        "vpn_client": vpn_client,
        "title": f"بررسی سرویس #{vpn_client.pk}",
        "subtitle": "نمای owner-facing بدون نمایش لینک کانفیگ، UUID، token یا شماره کامل.",
    }
    return TemplateResponse(request, "admin/store/services/review.html", context)


def customer_clients(customer):
    return (
        VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .prefetch_related("free_trial_requests__customer")
        .filter(Q(order__customer=customer) | Q(free_trial_requests__customer=customer))
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
        .distinct()
    )


def get_review_customer(customer_id):
    return get_object_or_404(
        Customer.objects.prefetch_related("bot_users__bot_config", "orders", "support_conversations"),
        pk=customer_id,
    )


def build_customer_review_context(customer):
    clients = customer_clients(customer)
    active_clients = clients.filter(status=VPNClient.Status.ACTIVE)
    expired_clients = clients.filter(Q(status=VPNClient.Status.EXPIRED) | Q(expires_at__lt=timezone.now())).distinct()
    orders = customer.orders.select_related("store", "plan", "operator").order_by("-created_at")
    support_tickets = customer.support_conversations.order_by("-updated_at")
    telegram_label, telegram_tone, telegram_target_count = customer_telegram_target_status(customer)
    return {
        "customer_summary": {
            "name": safe_customer_name(customer),
            "phone_masked": mask_phone(customer.phone_number),
            "username_masked": safe_identifier_label(customer.username) if customer.username else "",
            "telegram_label": telegram_label,
            "telegram_tone": telegram_tone,
            "telegram_target_count": telegram_target_count,
            "created_at": customer.created_at,
            "last_seen_at": customer.last_seen_at,
        },
        "orders_summary": {
            "total": orders.count(),
            "completed": orders.filter(status=Order.Status.COMPLETED).count(),
            "pending": orders.filter(status__in=PENDING_ORDER_STATUSES).count(),
            "recent": [
                {
                    "order": order,
                    "tracking_code": order.order_tracking_code,
                    "plan": order.plan.name if order.plan_id else "-",
                    "status_label": order_status_label(order.status),
                    "status_tone": order_status_tone(order.status),
                    "created_at": order.created_at,
                    "review_url": order_review_url(order),
                }
                for order in orders[:10]
            ],
        },
        "active_clients": [service_row(client) for client in active_clients.order_by("-created_at")[:10]],
        "expired_clients": [service_row(client) for client in expired_clients.order_by("-expires_at", "-created_at")[:10]],
        "support_tickets": [
            {
                "ticket": ticket,
                "subject": safe_short_text(ticket.subject),
                "status": ticket.get_status_display(),
                "updated_at": ticket.updated_at,
                "change_url": reverse("admin:store_supportconversation_change", args=[ticket.pk]),
                "review_url": support_review_url(ticket),
            }
            for ticket in support_tickets[:10]
        ],
        "admin_change_url": reverse("admin:store_customer_change", args=[customer.pk]),
        "message_url": customer_message_url(customer),
        "support_workbench_url": support_workbench_url(),
        "workbench_url": service_workbench_url(),
    }


@require_any_admin_capability("orders.view", "services.view", "support.view")
def customer_review(request, customer_id):
    customer = get_review_customer(customer_id)
    review_context = build_customer_review_context(customer)
    if not user_has_capability(request.user, "support.reply"):
        review_context["message_url"] = ""
    context = {
        **admin.site.each_context(request),
        **review_context,
        "customer": customer,
        "title": f"بررسی مشتری #{customer.pk}",
        "subtitle": "از مشتری به سرویس، usage، expiry و orderهای مرتبط برس.",
    }
    return TemplateResponse(request, "admin/store/customers/review.html", context)


@require_admin_capability("support.reply")
def customer_message(request, customer_id):
    customer = get_object_or_404(Customer, pk=customer_id)
    message_url = customer_message_url(customer)
    if request.method == "POST":
        if request.POST.get("action") != "send":
            messages.error(request, "Action معتبر نیست.")
            return redirect(message_url)
        if request.POST.get("confirm_customer") != "1" and request.POST.get("confirm_external") != "1":
            messages.error(request, "برای ارسال پیام باید تایید صریح owner ثبت شود.")
            return redirect(message_url)
        result = send_customer_direct_message(customer, request.POST.get("message", ""), request.user)
        if result.duplicate:
            messages.success(request, "این پیام قبلاً ثبت شده بود و دوباره ارسال نشد.")
        elif result.ok:
            messages.success(request, f"پیام شخصی برای {result.sent_count} مقصد تلگرام همین مشتری ارسال شد.")
        else:
            messages.error(request, safe_action_message(result.safe_error))
        return redirect(message_url)

    context = {
        **admin.site.each_context(request),
        **get_customer_message_context(customer.pk),
        "title": f"پیام به مشتری #{customer.pk}",
        "subtitle": "ارسال پیام شخصی و موردی فقط برای همین مشتری.",
    }
    return TemplateResponse(request, "admin/store/customers/message.html", context)


def get_admin_campaign(campaign_id):
    return get_object_or_404(
        BroadcastMessage.objects.select_related("store"),
        pk=campaign_id,
    )


@require_admin_capability("campaigns.view")
def campaign_workbench(request):
    stores, selected_store = selected_campaign_store_from_id(request.GET.get("store"))
    workbench_context = get_campaign_workbench_context(selected_store)
    if not user_has_capability(request.user, "campaigns.create"):
        workbench_context["quick_actions"] = [
            action for action in workbench_context["quick_actions"] if action["url"] != campaign_new_url(selected_store)
        ]
    context = {
        **admin.site.each_context(request),
        **workbench_context,
        "stores": stores,
        "selected_store": selected_store,
        "can_create_campaign": user_has_capability(request.user, "campaigns.create"),
        "title": "کمپین‌ها و پیام‌رسانی",
        "subtitle": "ساخت، preview، queue و پایش کمپین‌های Broadcast بدون ارسال تصادفی.",
    }
    return TemplateResponse(request, "admin/store/campaigns/workbench.html", context)


@require_admin_capability("campaigns.create")
def campaign_message_form(request, campaign_id=None):
    campaign = None
    stores, selected_store = selected_campaign_store_from_id(request.GET.get("store"))
    if campaign_id:
        campaign = get_admin_campaign(campaign_id)
        selected_store = campaign.store or selected_store
    else:
        audience_type = request.GET.get("audience")
        if audience_type not in BroadcastMessage.AudienceType.values:
            audience_type = BroadcastMessage.AudienceType.ALL
        campaign = BroadcastMessage(
            store=selected_store,
            status=BroadcastMessage.Status.DRAFT,
            audience_type=audience_type,
            channel=BroadcastMessage.Channel.TELEGRAM,
        )

    if request.method == "POST" and campaign.pk and campaign.status != BroadcastMessage.Status.DRAFT:
        messages.error(request, "فقط کمپین draft قابل ویرایش است.")
        return redirect(campaign_review_url(campaign))

    form = CampaignMessageForm(request.POST or None, instance=campaign, selected_store=selected_store)
    if request.method == "POST":
        if form.is_valid():
            saved = form.save(commit=False)
            metadata = dict(saved.metadata or {})
            metadata.setdefault("created_by_user_id", request.user.pk)
            metadata["last_edited_by_user_id"] = request.user.pk
            saved.metadata = metadata
            saved.status = saved.status or BroadcastMessage.Status.DRAFT
            saved.save()
            messages.success(request, "متن کمپین ذخیره شد. حالا مخاطبان را بررسی کن.")
            return redirect(campaign_audience_url(saved))
        messages.error(request, "لطفاً خطاهای فرم پیام را بررسی کن.")

    context = {
        **admin.site.each_context(request),
        "title": "ساخت کمپین جدید" if not campaign.pk else f"ویرایش کمپین #{campaign.pk}",
        "subtitle": "مرحله Message؛ این فرم هیچ پیام واقعی ارسال نمی‌کند.",
        "campaign": campaign if campaign.pk else None,
        "form": form,
        "stores": stores,
        "selected_store": selected_store,
        "message_templates": MESSAGE_TEMPLATE_BODIES,
        "workbench_url": campaign_workbench_url(selected_store),
        "review_url": campaign_review_url(campaign) if campaign.pk else "",
        "audience_url": campaign_audience_url(campaign) if campaign.pk else "",
        "preview_url": campaign_preview_url(campaign) if campaign.pk else "",
        "confirm_url": campaign_confirm_url(campaign) if campaign.pk else "",
        "current_step": "message",
    }
    return TemplateResponse(request, "admin/store/campaigns/form.html", context)


@require_admin_capability("campaigns.create")
def campaign_audience(request, campaign_id):
    campaign = get_admin_campaign(campaign_id)
    if request.method == "POST" and campaign.status != BroadcastMessage.Status.DRAFT:
        messages.error(request, "Audience فقط برای کمپین draft قابل تغییر است.")
        return redirect(campaign_review_url(campaign))

    form = CampaignAudienceForm(request.POST or None, instance=campaign)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Audience ذخیره شد. Preview را قبل از queue بررسی کن.")
            return redirect(campaign_preview_url(campaign))
        messages.error(request, "لطفاً خطاهای audience را بررسی کن.")

    preview = get_audience_preview(campaign)
    context = {
        **admin.site.each_context(request),
        "title": f"Audience کمپین #{campaign.pk}",
        "subtitle": "مرحله Audience؛ فقط شمارش DB انجام می‌شود و recipient دائمی ساخته نمی‌شود.",
        "campaign": campaign,
        "form": form,
        "preview": preview,
        "audience_descriptions": audience_descriptions(),
        "workbench_url": campaign_workbench_url(campaign.store),
        "review_url": campaign_review_url(campaign),
        "edit_url": campaign_edit_url(campaign),
        "preview_url": campaign_preview_url(campaign),
        "confirm_url": campaign_confirm_url(campaign),
        "current_step": "audience",
    }
    return TemplateResponse(request, "admin/store/campaigns/audience.html", context)


@require_admin_capability("campaigns.view")
def campaign_preview(request, campaign_id):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    campaign = get_admin_campaign(campaign_id)
    preview = get_audience_preview(campaign)
    context = {
        **admin.site.each_context(request),
        "title": f"Preview کمپین #{campaign.pk}",
        "subtitle": "مرحله Preview؛ هیچ recipient، send یا live validation ساخته/اجرا نمی‌شود.",
        "campaign": campaign,
        "preview": preview,
        "message_preview": safe_campaign_message_preview(campaign.message_text),
        "workbench_url": campaign_workbench_url(campaign.store),
        "review_url": campaign_review_url(campaign),
        "edit_url": campaign_edit_url(campaign),
        "audience_url": campaign_audience_url(campaign),
        "confirm_url": campaign_confirm_url(campaign),
        "current_step": "preview",
    }
    return TemplateResponse(request, "admin/store/campaigns/preview.html", context)


def safe_campaign_message_preview(message):
    return safe_campaign_snippet(message, limit=4096)


@require_admin_capability("campaigns.queue")
def campaign_confirm(request, campaign_id):
    campaign = get_admin_campaign(campaign_id)
    phrase = queue_confirmation_phrase(campaign)
    preview = get_audience_preview(campaign)
    if request.method == "POST":
        if request.POST.get("confirmation", "").strip() != phrase:
            messages.error(request, f"برای queue کردن باید دقیقاً {phrase} را وارد کنی.")
            return redirect(campaign_confirm_url(campaign))
        try:
            counts = queue_campaign_for_processing(campaign, request.user)
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
            return redirect(campaign_confirm_url(campaign))
        messages.success(
            request,
            f"کمپین queue شد: total={counts.get('total', 0):,} sent=0. ارسال واقعی فقط با process_broadcast_queue انجام می‌شود.",
        )
        return redirect(campaign_review_url(campaign))
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    context = {
        **admin.site.each_context(request),
        "title": f"تایید queue کمپین #{campaign.pk}",
        "subtitle": "مرحله Confirm؛ این صفحه فقط با POST و phrase دقیق کمپین را queue می‌کند.",
        "campaign": campaign,
        "preview": preview,
        "confirmation_phrase": phrase,
        "workbench_url": campaign_workbench_url(campaign.store),
        "review_url": campaign_review_url(campaign),
        "edit_url": campaign_edit_url(campaign),
        "audience_url": campaign_audience_url(campaign),
        "preview_url": campaign_preview_url(campaign),
        "current_step": "confirm",
    }
    return TemplateResponse(request, "admin/store/campaigns/confirm.html", context)


@require_admin_capability("campaigns.view")
def campaign_review(request, campaign_id):
    campaign = get_admin_campaign(campaign_id)
    review_url = campaign_review_url(campaign)
    if request.method == "POST":
        ensure_admin_capability(request.user, "campaigns.queue")
        action = request.POST.get("action", "")
        try:
            if action == "cancel":
                phrase = f"CANCEL_CAMPAIGN_{campaign.pk}"
                if request.POST.get("confirmation", "").strip() != phrase:
                    messages.error(request, f"برای cancel باید دقیقاً {phrase} را وارد کنی.")
                else:
                    cancel_campaign(campaign, request.user)
                    messages.warning(request, "کمپین لغو شد. recipientهای sent برگشت داده نمی‌شوند.")
                return redirect(review_url)

            if action == "retry":
                phrase = f"RETRY_CAMPAIGN_{campaign.pk}"
                if request.POST.get("confirmation", "").strip() != phrase:
                    messages.error(request, f"برای retry باید دقیقاً {phrase} را وارد کنی.")
                else:
                    count = reset_retryable_failed(campaign, request.user)
                    if count:
                        messages.success(request, f"{count:,} failed retryable دوباره pending شد و کمپین queue شد.")
                    else:
                        messages.info(request, "failed retryable برای retry پیدا نشد.")
                return redirect(review_url)
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
            return redirect(review_url)

        messages.error(request, "Action معتبر نیست.")
        return redirect(review_url)

    review_context = get_campaign_review_context(campaign)
    review_context["actions"]["can_edit"] = review_context["actions"]["can_edit"] and user_has_capability(
        request.user, "campaigns.create"
    )
    for key in ("can_queue", "can_cancel", "can_retry"):
        review_context["actions"][key] = review_context["actions"][key] and user_has_capability(request.user, "campaigns.queue")
    context = {
        **admin.site.each_context(request),
        **review_context,
        "campaign": campaign,
        "title": f"Campaign Review #{campaign.pk}",
        "subtitle": "وضعیت ارسال، خطاها و actionهای امن کمپین.",
    }
    return TemplateResponse(request, "admin/store/campaigns/review.html", context)


@require_admin_capability("campaigns.view")
def campaign_export(request, campaign_id):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    campaign = get_admin_campaign(campaign_id)
    response = HttpResponse(build_campaign_export_csv(campaign), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{export_filename(campaign)}"'
    return response


def get_review_order(order_id):
    return get_object_or_404(
        Order.objects.select_related("store", "customer", "plan", "operator", "inbound", "inbound__panel", "verified_by")
        .prefetch_related("vpn_clients", "vpn_clients__inbound__panel", "incoming_payment_sms", "customer__bot_users"),
        pk=order_id,
    )


def can_approve_order(order):
    return order.status not in {Order.Status.REJECTED, Order.Status.CANCELLED}


def can_reject_order(order):
    return order.status not in {Order.Status.COMPLETED, Order.Status.REJECTED, Order.Status.CANCELLED}


def can_retry_delivery(order):
    if order.status in {Order.Status.COMPLETED, Order.Status.REJECTED, Order.Status.CANCELLED}:
        return False
    return bool(prefetched_clients(order))


def handle_order_review_action(request, order):
    action = request.POST.get("action", "")
    review_url = order_review_url(order)

    if action not in {"approve", "reject", "retry_delivery"}:
        messages.error(request, "Action معتبر نیست.")
        return redirect(review_url)
    if action == "reject":
        ensure_admin_capability(request.user, "orders.reject")
    else:
        ensure_admin_capability(request.user, "orders.approve")

    if request.POST.get("confirm_external") != "1":
        messages.error(request, "برای اجرای این action باید تایید صریح owner ثبت شود.")
        return redirect(review_url)

    try:
        if action == "approve":
            if not can_approve_order(order):
                messages.error(request, "این سفارش در وضعیت قابل تایید نیست.")
                return redirect(review_url)
            if order.status == Order.Status.COMPLETED and order.verification_status == Order.VerificationStatus.VERIFIED:
                messages.success(request, "سفارش قبلاً تکمیل شده است.")
                return redirect(review_url)
            result = activate_order(order, user=request.user, notify=True)
            if result.success:
                messages.success(request, safe_action_message(result.message))
            else:
                messages.error(request, safe_action_message(result.message))
            return redirect(review_url)

        if action == "reject":
            reason = (request.POST.get("reason") or "").strip()
            if not reason:
                messages.error(request, "برای رد سفارش، reason لازم است.")
                return redirect(review_url)
            if not can_reject_order(order):
                messages.error(request, "این سفارش در وضعیت قابل رد نیست.")
                return redirect(review_url)
            result = reject_order(order, reason=reason, user=request.user, notify=True)
            if result.success:
                messages.success(request, safe_action_message(result.message))
            else:
                messages.error(request, safe_action_message(result.message))
            return redirect(review_url)

        if not can_retry_delivery(order):
            messages.error(request, "Retry delivery برای این سفارش امن یا در دسترس نیست.")
            return redirect(review_url)
        result = activate_order(order, user=request.user, notify=True)
        if result.success:
            messages.success(request, safe_action_message(result.message))
        else:
            messages.error(request, safe_action_message(result.message))
        return redirect(review_url)
    except Exception:
        logger.exception("Admin order review action failed action=%s order_id=%s", action, order.pk)
        messages.error(request, "عملیات ناموفق بود. جزئیات در لاگ سرور ثبت شد.")
        return redirect(review_url)


def build_review_context(order):
    payment_label, payment_tone = payment_status(order)
    delivery_label, delivery_tone = delivery_status(order)
    route_label, route_tone = route_status(order)
    analysis = receipt_analysis(order)
    sms_messages = list(order.incoming_payment_sms.all())
    clients = prefetched_clients(order)
    first_client = clients[0] if clients else None
    last_error = (
        order.last_provisioning_error
        or (order.metadata or {}).get("panel_provisioning_reason")
        or (order.metadata or {}).get("delivery_error")
        or ""
    )
    provisioning_scope = (order.metadata or {}).get("provisioning_scope") or {}
    provisioning_strategy = (order.metadata or {}).get("provisioning_strategy") or ""
    if not provisioning_strategy:
        provisioning_strategy = "renew_existing_client" if (order.metadata or {}).get("renewal_client_pk") else ""
    panel_profile = ""
    if order.inbound_id and order.inbound and order.inbound.panel_id:
        panel_profile = order.inbound.panel.capability_profile or "legacy_single_node"
    return {
        "row": order_row(order),
        "payment": {
            "label": payment_label,
            "tone": payment_tone,
            "expected_amount": money_label(order.amount, order.currency),
            "detected_amount": f"{analysis.get('matched_amount_irr'):,} ریال" if analysis.get("matched_amount_irr") else "-",
            "analysis_status": analysis.get("status") or "-",
            "analysis_warning": analysis.get("warning") or "",
            "submitted_at": order.payment_submitted_at,
            "sender_card_last4": order.sender_card_last4 or order.card_last_four or "",
            "receipt_url": order.payment_receipt_image.url if order.payment_receipt_image else "",
            "sms_messages": sms_messages,
        },
        "delivery": {
            "label": delivery_label,
            "tone": delivery_tone,
            "route_label": route_label,
            "route_tone": route_tone,
            "panel": order.inbound.panel.name if order.inbound_id and order.inbound and order.inbound.panel_id else "",
            "inbound": str(order.inbound) if order.inbound_id else "",
            "config_created": bool(clients),
            "config_sent": "ثبت جداگانه ندارد",
            "client_count": len(clients),
            "active_client_count": sum(1 for client in clients if client.status == VPNClient.Status.ACTIVE),
            "last_error": safe_action_message(last_error) if last_error else "",
            "primary_client_status": first_client.get_status_display() if first_client else "",
            "provisioning_strategy": provisioning_strategy,
            "compatibility_profile": panel_profile,
            "provisioning_status": order.get_provisioning_status_display(),
            "provisioning_attempts": order.provisioning_attempts,
            "provisioned_at": order.provisioned_at,
            "scope_panel_id": provisioning_scope.get("panel_id") or "",
            "scope_node_id": provisioning_scope.get("node_id") or "",
            "scope_xui_inbound_id": provisioning_scope.get("xui_inbound_id") or "",
        },
        "can_approve": can_approve_order(order),
        "can_reject": can_reject_order(order),
        "can_retry_delivery": can_retry_delivery(order),
        "service_reviews": [
            {
                "label": f"Service #{client.pk}",
                "status": client.get_status_display(),
                "url": service_review_url(client),
            }
            for client in clients
        ],
        "customer_message_url": customer_message_url(order.customer) if order.customer_id else "",
        "support_workbench_url": support_workbench_url(store=order.store),
        "suggested_message_template": "payment_review" if order.status == Order.Status.REJECTED else "",
        "admin_change_url": reverse("admin:store_order_change", args=[order.pk]),
        "workbench_url": order_workbench_url(store=order.store),
    }


@require_admin_capability("orders.view")
def order_review(request, order_id):
    order = get_review_order(order_id)
    if request.method == "POST":
        return handle_order_review_action(request, order)

    review_context = build_review_context(order)
    review_context["can_approve"] = review_context["can_approve"] and user_has_capability(request.user, "orders.approve")
    review_context["can_reject"] = review_context["can_reject"] and user_has_capability(request.user, "orders.reject")
    review_context["can_retry_delivery"] = review_context["can_retry_delivery"] and user_has_capability(request.user, "orders.approve")
    if not user_has_capability(request.user, "support.reply"):
        review_context["customer_message_url"] = ""
    if not user_has_capability(request.user, "support.view"):
        review_context["support_workbench_url"] = ""
    context = {
        **admin.site.each_context(request),
        **review_context,
        "order": order,
        "title": f"بررسی سفارش {order.order_tracking_code}",
        "subtitle": "خلاصه owner-facing، بدون نمایش لینک کانفیگ یا secret کامل.",
    }
    return TemplateResponse(request, "admin/store/orders/review.html", context)


def handle_catalog_post_action(request, *, selected_store=None, plan=None):
    action = request.POST.get("action", "")
    route_id = request.POST.get("route_id")
    target_plan = plan
    if not target_plan and request.POST.get("plan_id"):
        target_plan = get_object_or_404(Plan.objects.select_related("store"), pk=request.POST.get("plan_id"))

    if action not in {"activate", "deactivate", "duplicate", "deactivate_route"}:
        messages.error(request, "Action معتبر نیست.")
        return None
    ensure_admin_capability(request.user, "catalog.manage")
    if request.POST.get("confirm_action") != "1":
        messages.error(request, "برای انجام این عملیات باید تایید صریح ثبت شود.")
        return None

    if action == "deactivate_route":
        route = get_object_or_404(PlanInboundRoute.objects.select_related("plan", "inbound", "inbound__panel"), pk=route_id)
        changed = deactivate_route_for_admin(route)
        if changed:
            messages.success(request, "Route انتخاب‌شده غیرفعال شد.")
        else:
            messages.success(request, "Route قبلاً غیرفعال بود.")
        return route.plan

    if not target_plan:
        messages.error(request, "پلن انتخاب نشده است.")
        return None

    if action == "activate":
        try:
            changed = set_plan_active_state(target_plan, True, actor=request.user)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "پلن فعال شد." if changed else "پلن از قبل فعال بود.")
        return target_plan

    if action == "deactivate":
        changed = set_plan_active_state(target_plan, False, actor=request.user)
        messages.success(request, "پلن غیرفعال شد." if changed else "پلن از قبل غیرفعال بود.")
        return target_plan

    copied = duplicate_plan_for_admin(target_plan, actor=request.user)
    messages.success(request, "کپی پلن به‌صورت غیرفعال و بدون route فعال ساخته شد.")
    return copied


@require_admin_capability("catalog.view")
def product_catalog(request):
    stores, selected_store = selected_catalog_store_from_id(request.GET.get("store"))
    if request.method == "POST":
        target = handle_catalog_post_action(request, selected_store=selected_store)
        if target and request.POST.get("action") == "duplicate":
            return redirect(catalog_plan_review_url(target))
        return redirect(catalog_url(selected_store))

    context = {
        **admin.site.each_context(request),
        **get_catalog_context(selected_store, request.GET),
        "stores": stores,
        "selected_store": selected_store,
        "can_manage_catalog": user_has_capability(request.user, "catalog.manage"),
        "title": "محصولات / پلن‌ها",
        "subtitle": "مدیریت پلن‌های فروش، قیمت، نمایش و آمادگی route بدون اجرای live call.",
    }
    return TemplateResponse(request, "admin/store/catalog/index.html", context)


@require_admin_capability("catalog.manage")
def catalog_plan_form(request, plan_id=None):
    plan = None
    if plan_id:
        plan = get_object_or_404(Plan.objects.select_related("store").prefetch_related("operators"), pk=plan_id)
        selected_store = plan.store
    else:
        _stores, selected_store = selected_catalog_store_from_id(request.GET.get("store"))

    form = CatalogPlanForm(request.POST or None, store=selected_store, plan=plan)
    if request.method == "POST":
        if form.is_valid():
            saved_plan = form.save()
            messages.success(request, "پلن و تنظیمات مسیر فروش ذخیره شد.")
            return redirect(catalog_plan_review_url(saved_plan))
        messages.error(request, "لطفاً خطاهای فرم پلن را بررسی کن.")

    context = {
        **admin.site.each_context(request),
        "title": "ویرایش پلن" if plan else "ساخت پلن جدید",
        "subtitle": "فرم کوتاه owner-facing برای قیمت، حجم، مدت و route فروش.",
        "form": form,
        "plan": plan,
        "selected_store": selected_store,
        "catalog_url": catalog_url(selected_store),
        "review_url": catalog_plan_review_url(plan) if plan else "",
    }
    return TemplateResponse(request, "admin/store/catalog/plan_form.html", context)


@require_admin_capability("catalog.view")
def catalog_plan_review(request, plan_id):
    plan = get_object_or_404(Plan.objects.select_related("store").prefetch_related("operators"), pk=plan_id)
    selected_store = plan.store
    if request.method == "POST":
        target = handle_catalog_post_action(request, selected_store=selected_store, plan=plan)
        if target:
            return redirect(catalog_plan_review_url(target))
        return redirect(catalog_plan_review_url(plan))

    route_status = get_plan_route_status(plan, selected_store)
    readiness = validate_plan_sales_readiness(plan, selected_store)
    route_items = [item for item in get_route_overview_items(selected_store) if item["route"].plan_id == plan.pk]
    recent_orders = Order.objects.filter(plan=plan).order_by("-created_at", "-pk")
    vpn_clients = VPNClient.objects.filter(plan=plan)
    context = {
        **admin.site.each_context(request),
        "title": f"بررسی پلن {plan.name}",
        "subtitle": "نمای owner-facing پلن، routeها و آمادگی فروش.",
        "plan": plan,
        "selected_store": selected_store,
        "route_status": route_status,
        "readiness": readiness,
        "route_items": route_items,
        "recent_order_count": recent_orders.count(),
        "recent_order_url": f"{reverse('admin:store_order_changelist')}?{urlencode({'plan__id__exact': plan.pk})}",
        "vpn_client_count": vpn_clients.count(),
        "vpn_client_url": f"{reverse('admin:store_vpnclient_changelist')}?{urlencode({'plan__id__exact': plan.pk})}",
        "volume_label": plan_volume_label(plan),
        "duration_label": plan_duration_label(plan),
        "price_label": money_label(plan.price, plan.currency),
        "bot_preview": f"{plan.name} - {plan_volume_label(plan)} - {plan_duration_label(plan)} - {money_label(plan.price, plan.currency)}",
        "edit_url": catalog_plan_edit_url(plan),
        "catalog_url": catalog_url(selected_store),
        "bulk_assign_url": f"{reverse('admin:store_planinboundroute_bulk_assign')}?{urlencode({'store': getattr(selected_store, 'pk', ''), 'plan_ids': plan.pk, 'plan_selection_mode': 'manual'})}",
        "admin_change_url": reverse("admin:store_plan_change", args=[plan.pk]),
        "can_manage_catalog": user_has_capability(request.user, "catalog.manage"),
    }
    return TemplateResponse(request, "admin/store/catalog/plan_review.html", context)


@require_admin_capability("setup.manage")
def setup_center(request):
    setup_context = build_setup_center_context(request.GET.get("store"))
    context = {
        **admin.site.each_context(request),
        **setup_context,
        "title": "راه‌اندازی قاصدک",
        "subtitle": "وضعیت نصب و تنظیمات اولیه",
    }
    return TemplateResponse(request, "admin/store/setup_center.html", context)


@require_admin_capability("dashboard.view")
def owner_dashboard(request):
    dashboard_context = get_owner_dashboard_context(
        user=request.user,
        selected_store_id=request.GET.get("store"),
    )
    context = {
        **admin.site.each_context(request),
        **dashboard_context,
        "title": "داشبورد قاصدک",
        "subtitle": "امروز چه خبر است؟",
    }
    return TemplateResponse(request, "admin/store/owner_dashboard.html", context)


@require_admin_capability("reports.view")
def reports_center(request):
    stores, selected_store = get_report_store_options(request.GET.get("store"))
    allow_extended = bool(request.user.is_superuser and request.GET.get("extended_reason"))
    period = resolve_report_period(request.GET, store=selected_store, allow_extended=allow_extended)
    reports_context = get_reports_center_context(period, selected_store)
    context = {
        **admin.site.each_context(request),
        **reports_context,
        "stores": stores,
        "selected_store": selected_store,
        "can_export_reports": user_has_capability(request.user, "reports.export"),
        "title": "گزارش‌ها و تحلیل کسب‌وکار",
        "subtitle": "فروش، مشتری، سرویس، مصرف پنل و عملیات از داده‌های موجود DB",
    }
    return TemplateResponse(request, "admin/store/reports/index.html", context)


@require_admin_capability("reports.export")
def reports_export(request):
    report_type = (request.GET.get("report") or "sales").strip()
    if report_type not in VALID_REPORT_EXPORTS:
        return HttpResponseBadRequest("نوع گزارش نامعتبر است.")
    _stores, selected_store = get_report_store_options(request.GET.get("store"))
    allow_extended = bool(request.user.is_superuser and request.GET.get("extended_reason"))
    period = resolve_report_period(request.GET, store=selected_store, allow_extended=allow_extended)
    if period.errors or period.blocked:
        return HttpResponseBadRequest("بازه گزارش معتبر نیست یا از حد مجاز طولانی‌تر است.")
    filename, body = build_reports_csv(period, report_type, selected_store)
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@require_admin_capability("revenue.view")
def revenue_control_center(request):
    selected_store_id = request.POST.get("store") if request.method == "POST" else request.GET.get("store")
    stores, selected_store = get_store_options(selected_store_id)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "enable_real_send":
            ensure_admin_capability(request.user, "revenue.enable_real_send")
        else:
            ensure_admin_capability(request.user, "revenue.manage_safe")
        if not selected_store:
            messages.error(request, "برای Revenue Control Center ابتدا یک Store بساز.")
        elif action == "enable_dry_run":
            selected_store.revenue_engine_dry_run = True
            selected_store.save(update_fields=["revenue_engine_dry_run"])
            messages.success(request, "Dry-run mode فعال شد؛ پیام واقعی ارسال نمی‌شود.")
        elif action == "disable_revenue":
            if request.POST.get("confirmation") != "DISABLE_REVENUE_ENGINE":
                messages.error(request, "برای خاموش کردن Revenue Engine متن تایید را دقیق وارد کن.")
            else:
                set_revenue_mode(selected_store, enabled=False, dry_run=True)
                messages.warning(request, "Revenue Engine خاموش شد و Store در حالت امن dry-run باقی ماند.")
        elif action == "enable_engine_dry_run":
            set_revenue_mode(selected_store, enabled=True, dry_run=True)
            messages.success(request, "Revenue Engine روشن شد، اما همچنان در dry-run است.")
        elif action == "enable_real_send":
            if request.POST.get("confirmation") != REAL_SEND_CONFIRMATION:
                messages.error(request, f"برای real-send باید دقیقاً {REAL_SEND_CONFIRMATION} را وارد کنی.")
            else:
                safety = get_real_send_safety(selected_store)
                if not safety["safe"]:
                    messages.error(request, "Real-send فعال نشد؛ شرط‌های ایمنی هنوز کامل نیستند.")
                    for issue in safety["blocking"][:5]:
                        messages.warning(request, issue)
                else:
                    set_revenue_mode(selected_store, enabled=True, dry_run=False)
                    messages.warning(request, "Real-send فعال شد. ارسال واقعی Revenue Engine روشن است.")
        elif action == "reset_safe_defaults":
            if request.POST.get("confirmation") != "RESET_REVENUE_SAFE_DEFAULTS":
                messages.error(request, "برای reset safe defaults متن تایید را دقیق وارد کن.")
            else:
                apply_revenue_safe_defaults(selected_store)
                messages.success(request, "Revenue Engine به safe defaults برگشت و dry-run=True شد.")
        else:
            messages.error(request, "Action نامعتبر است.")
        return redirect(revenue_control_url(selected_store))

    control_context = get_revenue_control_context(selected_store)
    context = {
        **admin.site.each_context(request),
        **control_context,
        "stores": stores,
        "selected_store": selected_store,
        "can_manage_revenue": user_has_capability(request.user, "revenue.manage_safe"),
        "can_enable_real_send": user_has_capability(request.user, "revenue.enable_real_send"),
        "title": "کنترل درآمد هوشمند",
        "subtitle": "Revenue Engine Control Center",
    }
    return TemplateResponse(request, "admin/store/revenue/control_center.html", context)


def _safe_setup_test_error(exc, *sensitive_values):
    message = str(exc or "").strip()
    for value in sensitive_values:
        value = str(value or "").strip()
        if value:
            message = message.replace(value, "<redacted>")
    return safe_action_message(message)


def _test_telegram_setup_form(form):
    token = (form.cleaned_data.get("bot_token") or "").strip()
    if not token:
        raise ValidationError("برای بررسی اتصال، Bot token لازم است.")
    config = BotConfiguration(
        provider=BotConfiguration.Provider.TELEGRAM,
        store=form.store,
        name=form.cleaned_data.get("name") or "Telegram bot",
        telegram_bot_username=form.cleaned_data.get("telegram_bot_username") or "",
        bot_token=token,
        admin_user_id=form.cleaned_data.get("admin_user_id") or "",
        additional_admin_user_ids=form.cleaned_data.get("additional_admin_user_ids") or "",
        is_active=False,
    )
    client = BotClient(config)
    client.bot_token = token
    client.base_url = client.BASE_URLS[BotConfiguration.Provider.TELEGRAM].format(token=token).rstrip("/")
    return client.get_me()


def _test_xui_setup_form(form, store):
    password = (form.cleaned_data.get("password") or "").strip()
    panel = Panel(
        store=store,
        name=form.cleaned_data.get("name") or "Primary X-UI panel",
        url=form.cleaned_data.get("url") or "",
        username=form.cleaned_data.get("username") or "",
        password=password,
        proxy_url=form.cleaned_data.get("proxy_url") or None,
        is_active=bool(form.cleaned_data.get("is_active")),
    )
    service = XUIService(panel, timeout_seconds=8)
    return service.authenticated_json("GET", "/panel/api/inbounds/list")


def _store_domain_host(store):
    text = str(getattr(store, "domain", "") or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname or parsed.path.split("/", 1)[0]
    return host.strip().strip(".").lower()


def _tenant_instance_for_store(store):
    host = _store_domain_host(store)
    if not host:
        return None
    return (
        TenantInstance.objects.exclude(status=TenantInstance.Status.DELETED)
        .filter(
            Q(subdomain__iexact=host)
            | Q(domain__iexact=host)
            | Q(customer_domain__iexact=host)
            | Q(domain__iexact=f"https://{host}")
            | Q(domain__iexact=f"http://{host}")
            | Q(instance_url__iexact=f"https://{host}")
            | Q(instance_url__iexact=f"http://{host}")
        )
        .order_by("id")
        .first()
    )


def _store_has_active_telegram_bot(store):
    if not store:
        return False
    return (
        BotConfiguration.objects.filter(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        )
        .exclude(bot_token="")
        .exists()
    )


def _ensure_tenant_bot_worker_after_setup(request, store):
    if not _store_has_active_telegram_bot(store):
        return
    instance = _tenant_instance_for_store(store)
    if not instance:
        return
    try:
        TenantOwnerToolkit().start_or_restart_bot_worker(tenant_id=instance.tenant_id)
    except OwnerToolkitError as exc:
        messages.warning(
            request,
            f"فروشگاه ذخیره شد، اما راه‌اندازی worker ربات نیاز به بررسی دارد: {safe_action_message(exc)}",
        )
    else:
        messages.success(request, "worker ربات تلگرام فعال شد.")


def _handle_setup_test_action(request, step_slug, form, selected_store):
    action = request.POST.get(WIZARD_ACTION_FIELD)
    if action == WIZARD_TEST_TELEGRAM_ACTION:
        if step_slug != "telegram":
            return False
        if form.is_valid():
            try:
                _test_telegram_setup_form(form)
            except (ValidationError, BotDeliveryError) as exc:
                messages.error(
                    request,
                    f"بررسی ربات ناموفق بود: {_safe_setup_test_error(exc, form.cleaned_data.get('bot_token'))}",
                )
            else:
                messages.success(request, "اتصال ربات با getMe بررسی شد. هیچ پیام تلگرامی ارسال نشد.")
        else:
            messages.error(request, "برای بررسی اتصال، ابتدا خطاهای فرم را اصلاح کن.")
        return True

    if action == WIZARD_TEST_XUI_ACTION:
        if step_slug != "panel":
            return False
        if form.is_valid():
            try:
                _test_xui_setup_form(form, selected_store)
            except Exception as exc:
                safe_error = _safe_setup_test_error(
                    exc,
                    form.cleaned_data.get("password"),
                    form.cleaned_data.get("proxy_url"),
                )
                logger.warning("Setup wizard X-UI read test failed: %s", safe_error)
                messages.error(
                    request,
                    f"تست خواندن پنل ناموفق بود: {safe_error}",
                )
            else:
                messages.success(request, "اتصال پنل با یک خواندن read-only بررسی شد.")
        else:
            messages.error(request, "برای تست پنل، ابتدا خطاهای فرم را اصلاح کن.")
        return True

    return False


@require_admin_capability("setup.manage")
def setup_wizard_index(request):
    wizard_context = get_setup_wizard_context(request, request.GET.get("store"))
    context = {
        **admin.site.each_context(request),
        **wizard_context,
        "title": "راه‌اندازی مرحله‌ای قاصدک",
        "subtitle": "قدم‌به‌قدم تنظیمات اولیه را کامل کن",
    }
    return TemplateResponse(request, "admin/store/setup_wizard/index.html", context)


@require_admin_capability("setup.manage")
def setup_wizard_step(request, step_slug):
    if step_slug not in WIZARD_STEP_BY_SLUG:
        return redirect("admin_store_setup_wizard")

    if step_slug == "review":
        _stores, selected_store = selected_store_from_id(request.GET.get("store"))
        if request.method == "POST":
            if request.POST.get(WIZARD_ACTION_FIELD) != WIZARD_ACTIVATE_ACTION:
                messages.error(request, "Action نامعتبر است.")
                return redirect(wizard_step_url("review", selected_store))
            if request.POST.get("activation_confirmation", "").strip() != WIZARD_ACTIVATION_CONFIRMATION:
                messages.error(request, f"برای فعال‌سازی باید دقیقاً {WIZARD_ACTIVATION_CONFIRMATION} را وارد کنی.")
                return redirect(wizard_step_url("review", selected_store))
            status = sync_store_setup_status(selected_store, allow_ready=True)
            if status == Store.SetupStatus.READY:
                _ensure_tenant_bot_worker_after_setup(request, selected_store)
                messages.success(request, "فروشگاه آماده شد و فروش عمومی فعال است.")
            else:
                details = setup_not_ready_details(selected_store)
                blockers = ", ".join(details["pending"] or details["reasons"] or ["unknown"])
                messages.error(
                    request,
                    f"هنوز چند مورد setup کامل نیست؛ تا رفع همه موارد فروش عمومی فعال نمی‌شود. موارد باقی‌مانده: {blockers}",
                )
            return redirect(wizard_step_url("review", selected_store))

        context = {
            **admin.site.each_context(request),
            **get_review_context(request, getattr(selected_store, "pk", None)),
            "title": "بررسی نهایی راه‌اندازی",
        }
        return TemplateResponse(request, "admin/store/setup_wizard/review.html", context)

    _stores, selected_store = selected_store_from_id(request.GET.get("store"))

    if request.method == "POST" and is_skip_request(request.POST):
        if not is_step_skippable(step_slug):
            messages.error(request, "این مرحله قابل رد کردن نیست.")
            return redirect(wizard_step_url(step_slug, selected_store))
        mark_step_skipped(request, step_slug)
        messages.warning(request, "این مرحله برای بعد علامت‌گذاری شد.")
        next_slug = next_step_slug(step_slug)
        return redirect(wizard_step_url(next_slug, selected_store) if next_slug else "admin_store_owner_dashboard")

    form = get_step_form(step_slug, request=request, store=selected_store, data=request.POST or None)
    if request.method == "POST":
        if request.POST.get(WIZARD_ACTION_FIELD) in {WIZARD_TEST_TELEGRAM_ACTION, WIZARD_TEST_XUI_ACTION}:
            if not _handle_setup_test_action(request, step_slug, form, selected_store):
                messages.error(request, "Action تست برای این مرحله معتبر نیست.")
                return redirect(wizard_step_url(step_slug, selected_store))
            context = {
                **admin.site.each_context(request),
                **get_step_page_context(
                    request,
                    step_slug,
                    selected_store_id=getattr(selected_store, "pk", None),
                    form=form,
                ),
                "title": WIZARD_STEP_BY_SLUG[step_slug].title,
            }
            return TemplateResponse(request, WIZARD_STEP_BY_SLUG[step_slug].template, context)

        if form.is_valid():
            saved_object = save_step_form(step_slug, form)
            if isinstance(saved_object, Store):
                selected_store = saved_object
            sync_store_setup_status(selected_store, allow_ready=False)
            clear_step_skipped(request, step_slug)
            if step_slug == "telegram":
                _ensure_tenant_bot_worker_after_setup(request, selected_store)
            messages.success(request, "مرحله با موفقیت ذخیره شد.")
            if step_slug == "plans" and isinstance(saved_object, Plan):
                return redirect(catalog_plan_review_url(saved_object))
            if step_slug == "routes":
                return redirect(catalog_url(selected_store))
            next_slug = next_step_slug(step_slug)
            return redirect(wizard_step_url(next_slug, selected_store) if next_slug else "admin_store_owner_dashboard")
        messages.error(request, "لطفاً خطاهای فرم را بررسی کن.")

    context = {
        **admin.site.each_context(request),
        **get_step_page_context(
            request,
            step_slug,
            selected_store_id=getattr(selected_store, "pk", None),
            form=form,
        ),
        "title": WIZARD_STEP_BY_SLUG[step_slug].title,
    }
    return TemplateResponse(request, WIZARD_STEP_BY_SLUG[step_slug].template, context)
