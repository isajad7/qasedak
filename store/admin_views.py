from collections import defaultdict
from datetime import date, timedelta
import logging
import re

import jdatetime
from django.contrib import admin, messages
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from payments.models import IncomingPaymentSMS

from .admin_dashboard import get_owner_dashboard_context
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
    WIZARD_STEP_BY_SLUG,
    clear_step_skipped,
    get_review_context,
    get_setup_wizard_context,
    get_step_form,
    get_step_page_context,
    mark_step_skipped,
    next_step_slug,
    save_step_form,
    selected_store_from_id,
    wizard_step_url,
)
from .config_lookup import mask_identifier
from .jalali import format_jalali_date
from .models import (
    BotConfiguration,
    BotUser,
    Customer,
    Order,
    Plan,
    Store,
    SupportConversation,
    VPNClient,
    normalize_payment_digits,
)
from .order_actions import activate_order, reject_order
from .vpn_client_management_services import (
    VPNClientManagementError,
    refresh_vpn_client_link_by_admin,
    set_vpn_client_enabled_by_admin,
)
from .xui_api import sync_vpn_client_stats


logger = logging.getLogger(__name__)


DEFAULT_STATUS_FILTER = "not_rejected"
ORDER_WORKBENCH_LIMIT = 20
SERVICE_WORKBENCH_LIMIT = 20
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
        params["section"] = section
    url = reverse("admin_store_service_workbench")
    if params:
        url = f"{url}?{urlencode(params)}"
    if section:
        url = f"{url}#{section}"
    return url


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
        | Q(vpn_clients__status__in=[VPNClient.Status.ERROR, VPNClient.Status.CREATED, VPNClient.Status.INACTIVE])
    ).exclude(status__in=[Order.Status.REJECTED, Order.Status.CANCELLED]).distinct()

    today_orders = base.filter(created_at__gte=today_start, created_at__lt=today_end)

    problematic = base.filter(
        Q(metadata__receipt_analysis__status__in=["mismatch", "not_found", "unsupported_currency", "image_only"])
        | Q(metadata__panel_provisioning_deferred=True)
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


def build_service_workbench_context(selected_store):
    now = timezone.now()
    soon = now + timedelta(days=3)
    base = base_service_clients(selected_store)
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
    return {
        "sections": sections,
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


def service_workbench(request):
    stores, selected_store = selected_store_from_id(request.GET.get("store"))
    workbench_context = build_service_workbench_context(selected_store)
    context = {
        **admin.site.each_context(request),
        **workbench_context,
        "stores": stores,
        "selected_store": selected_store,
        "title": "میز کار سرویس‌ها",
        "subtitle": "مشتری، وضعیت سرویس، مصرف، انقضا و actionهای explicit بدون نمایش secret.",
    }
    return TemplateResponse(request, "admin/store/services/workbench.html", context)


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
    allowed_actions = {"resend_config", "refresh_config_link", "update_usage", "disable_client", "enable_client"}
    if action not in allowed_actions:
        messages.error(request, "Action معتبر نیست.")
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
            "change_url": reverse("admin:store_customer_change", args=[customer.pk]) if customer else "",
        },
        "service_summary": {
            "plan": vpn_client.plan.name if vpn_client.plan_id else "-",
            "order": vpn_client.order,
            "order_url": order_review_url(vpn_client.order) if vpn_client.order_id else "",
            "panel": vpn_client.inbound.panel.name if vpn_client.inbound_id and vpn_client.inbound.panel_id else "-",
            "inbound": vpn_client.inbound.remark or vpn_client.inbound.inbound_id if vpn_client.inbound_id else "-",
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
        },
        "workbench_url": service_workbench_url(store=vpn_client.store),
        "admin_change_url": reverse("admin:store_vpnclient_change", args=[vpn_client.pk]),
    }


def service_review(request, vpn_client_id):
    vpn_client = get_review_vpn_client(vpn_client_id)
    if request.method == "POST":
        return handle_service_review_action(request, vpn_client)

    context = {
        **admin.site.each_context(request),
        **build_service_review_context(vpn_client),
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
            }
            for ticket in support_tickets[:10]
        ],
        "admin_change_url": reverse("admin:store_customer_change", args=[customer.pk]),
        "workbench_url": service_workbench_url(),
    }


def customer_review(request, customer_id):
    customer = get_review_customer(customer_id)
    context = {
        **admin.site.each_context(request),
        **build_customer_review_context(customer),
        "customer": customer,
        "title": f"بررسی مشتری #{customer.pk}",
        "subtitle": "از مشتری به سرویس، usage، expiry و orderهای مرتبط برس.",
    }
    return TemplateResponse(request, "admin/store/customers/review.html", context)


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
        (order.metadata or {}).get("panel_provisioning_reason")
        or (order.metadata or {}).get("delivery_error")
        or ""
    )
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
        "admin_change_url": reverse("admin:store_order_change", args=[order.pk]),
        "workbench_url": order_workbench_url(store=order.store),
    }


def order_review(request, order_id):
    order = get_review_order(order_id)
    if request.method == "POST":
        return handle_order_review_action(request, order)

    context = {
        **admin.site.each_context(request),
        **build_review_context(order),
        "order": order,
        "title": f"بررسی سفارش {order.order_tracking_code}",
        "subtitle": "خلاصه owner-facing، بدون نمایش لینک کانفیگ یا secret کامل.",
    }
    return TemplateResponse(request, "admin/store/orders/review.html", context)


def setup_center(request):
    setup_context = build_setup_center_context(request.GET.get("store"))
    context = {
        **admin.site.each_context(request),
        **setup_context,
        "title": "راه‌اندازی قاصدک",
        "subtitle": "وضعیت نصب و تنظیمات اولیه",
    }
    return TemplateResponse(request, "admin/store/setup_center.html", context)


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


def revenue_control_center(request):
    selected_store_id = request.POST.get("store") if request.method == "POST" else request.GET.get("store")
    stores, selected_store = get_store_options(selected_store_id)
    if request.method == "POST":
        action = request.POST.get("action", "")
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
        "title": "کنترل درآمد هوشمند",
        "subtitle": "Revenue Engine Control Center",
    }
    return TemplateResponse(request, "admin/store/revenue/control_center.html", context)


def setup_wizard_index(request):
    wizard_context = get_setup_wizard_context(request, request.GET.get("store"))
    context = {
        **admin.site.each_context(request),
        **wizard_context,
        "title": "راه‌اندازی مرحله‌ای قاصدک",
        "subtitle": "قدم‌به‌قدم تنظیمات اولیه را کامل کن",
    }
    return TemplateResponse(request, "admin/store/setup_wizard/index.html", context)


def setup_wizard_step(request, step_slug):
    if step_slug not in WIZARD_STEP_BY_SLUG:
        return redirect("admin_store_setup_wizard")

    if step_slug == "review":
        context = {
            **admin.site.each_context(request),
            **get_review_context(request, request.GET.get("store")),
            "title": "بررسی نهایی راه‌اندازی",
        }
        return TemplateResponse(request, "admin/store/setup_wizard/review.html", context)

    _stores, selected_store = selected_store_from_id(request.GET.get("store"))

    if request.method == "POST" and "_skip" in request.POST:
        mark_step_skipped(request, step_slug)
        messages.warning(request, "این مرحله برای بعد علامت‌گذاری شد.")
        next_slug = next_step_slug(step_slug)
        return redirect(wizard_step_url(next_slug, selected_store) if next_slug else "admin_store_owner_dashboard")

    form = get_step_form(step_slug, request=request, store=selected_store, data=request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            saved_object = save_step_form(step_slug, form)
            if isinstance(saved_object, Store):
                selected_store = saved_object
            clear_step_skipped(request, step_slug)
            messages.success(request, "مرحله با موفقیت ذخیره شد.")
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
