from collections import defaultdict
from datetime import date

import jdatetime
from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import reverse

from .jalali import format_jalali_date
from .models import Order, Plan, Store, normalize_payment_digits


DEFAULT_STATUS_FILTER = "not_rejected"

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
