import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime
from django.conf import settings
from django.db.models import BigIntegerField, Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from payments.models import IncomingPaymentSMS

from .customer_analytics import renewal_order_q
from .jalali import format_jalali_date, persian_digits
from .models import (
    BotConfiguration,
    BotEventLog,
    BroadcastMessage,
    BroadcastRecipient,
    Customer,
    DailyAdminReportLog,
    Order,
    Panel,
    PanelDailyUsage,
    PanelHealthCheckLog,
    PanelHealthStatus,
    Plan,
    RevenueOfferLog,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    VPNClientActionLog,
    VPNClientReminderLog,
    normalize_payment_digits,
)
from .panel_usage_services import format_bytes_fa


DEFAULT_RANGE = "30d"
MAX_REPORT_DAYS = 365
RECENT_LIMIT = 8
SUCCESSFUL_ORDER_STATUSES = (Order.Status.COMPLETED,)
PENDING_ORDER_STATUSES = (
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
)
FAILED_ORDER_STATUSES = (Order.Status.REJECTED, Order.Status.CANCELLED)
VALID_REPORT_EXPORTS = {"sales", "customers", "services", "operations", "revenue", "panel_usage"}
SERVICE_REMOTE_FILTERS = {"remote-active", "remote-disabled", "remote-missing", "remote-problem", "not-checked"}
MONEY_FIELD = BigIntegerField()
DECIMAL_FIELD = DecimalField(max_digits=18, decimal_places=3)
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
CONFIG_LINK_RE = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
SUB_LINK_RE = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?98|0)?9\d{9}(?!\d)")


@dataclass(frozen=True)
class ReportPeriod:
    key: str
    label: str
    start_date: date
    end_date: date
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    previous_start_date: date
    previous_end_date: date
    timezone_name: str
    timezone: ZoneInfo
    errors: list[str] = field(default_factory=list)
    blocked: bool = False
    is_custom: bool = False

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def display(self):
        return f"{format_jalali_date(self.start_date)} تا {format_jalali_date(self.end_date)}"

    @property
    def previous_display(self):
        return f"{format_jalali_date(self.previous_start_date)} تا {format_jalali_date(self.previous_end_date)}"

    @property
    def query_params(self):
        if self.is_custom:
            return {
                "start": self.start_date.isoformat(),
                "end": self.end_date.isoformat(),
            }
        return {"range": self.key}


def _zoneinfo(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ZoneInfo("Asia/Tehran")


def _store_timezone_name(store=None):
    value = (
        getattr(store, "daily_admin_report_timezone", "")
        or getattr(store, "revenue_engine_timezone", "")
        or getattr(settings, "TIME_ZONE", "")
        or "Asia/Tehran"
    )
    return str(value).strip() or "Asia/Tehran"


def _local_day_bounds(day, tz):
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _parse_date(value):
    cleaned = normalize_payment_digits(value).strip().replace("/", "-")
    if not cleaned:
        return None
    parts = cleaned.split("-")
    if len(parts) != 3:
        raise ValueError("تاریخ را با قالب YYYY-MM-DD وارد کن.")
    year, month, day = [int(part) for part in parts]
    if year < 1700:
        return jdatetime.date(year, month, day).togregorian()
    return date(year, month, day)


def _month_bounds(today):
    first = today.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)


def _previous_month_bounds(today):
    first_this_month = today.replace(day=1)
    previous_end = first_this_month - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    return previous_start, previous_end


def resolve_report_period(params, store=None, allow_extended=False):
    params = params or {}
    get_value = params.get if hasattr(params, "get") else lambda key, default=None: params.get(key, default)
    timezone_name = _store_timezone_name(store)
    tz = _zoneinfo(timezone_name)
    today = timezone.localtime(timezone.now(), tz).date()
    range_key = (get_value("range") or DEFAULT_RANGE).strip()
    errors = []
    is_custom = bool(get_value("start") or get_value("end") or range_key == "custom")

    try:
        if is_custom:
            start_date = _parse_date(get_value("start"))
            end_date = _parse_date(get_value("end"))
            if not start_date or not end_date:
                raise ValueError("برای بازه سفارشی، تاریخ شروع و پایان را وارد کن.")
            label = "بازه سفارشی"
            key = "custom"
        elif range_key == "today":
            start_date = end_date = today
            label = "امروز"
            key = range_key
        elif range_key == "7d":
            start_date = today - timedelta(days=6)
            end_date = today
            label = "۷ روز اخیر"
            key = range_key
        elif range_key == "this_month":
            start_date, end_date = _month_bounds(today)
            label = "این ماه"
            key = range_key
        elif range_key == "previous_month":
            start_date, end_date = _previous_month_bounds(today)
            label = "ماه قبل"
            key = range_key
        else:
            if range_key not in {"30d", DEFAULT_RANGE}:
                errors.append("فیلتر زمانی نامعتبر بود؛ بازه ۳۰ روز اخیر نمایش داده شد.")
            start_date = today - timedelta(days=29)
            end_date = today
            label = "۳۰ روز اخیر"
            key = "30d"
    except (TypeError, ValueError):
        errors.append("بازه سفارشی معتبر نیست. تاریخ‌ها را با قالب YYYY-MM-DD وارد کن.")
        start_date = today - timedelta(days=29)
        end_date = today
        label = "۳۰ روز اخیر"
        key = "30d"
        is_custom = False

    blocked = False
    if start_date > end_date:
        errors.append("تاریخ شروع نباید بعد از تاریخ پایان باشد.")
        start_date, end_date = end_date, start_date
    day_count = (end_date - start_date).days + 1
    if day_count > MAX_REPORT_DAYS and not allow_extended:
        errors.append(f"حداکثر بازه مجاز {persian_digits(MAX_REPORT_DAYS)} روز است.")
        blocked = True
        end_date = start_date + timedelta(days=MAX_REPORT_DAYS - 1)

    start, _start_next = _local_day_bounds(start_date, tz)
    _end_start, end = _local_day_bounds(end_date, tz)
    previous_end_date = start_date - timedelta(days=1)
    previous_start_date = previous_end_date - timedelta(days=(end_date - start_date).days)
    previous_start, _ = _local_day_bounds(previous_start_date, tz)
    _, previous_end = _local_day_bounds(previous_end_date, tz)
    return ReportPeriod(
        key=key,
        label=label,
        start_date=start_date,
        end_date=end_date,
        start=start,
        end=end,
        previous_start=previous_start,
        previous_end=previous_end,
        previous_start_date=previous_start_date,
        previous_end_date=previous_end_date,
        timezone_name=timezone_name,
        timezone=tz,
        errors=errors,
        blocked=blocked,
        is_custom=is_custom,
    )


def compare_metric(current, previous):
    current = current or 0
    previous = previous or 0
    delta = current - previous
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    else:
        direction = "flat"
    if previous == 0:
        return {
            "current": current,
            "previous": previous,
            "delta": delta,
            "percent": None,
            "direction": direction,
            "comparable": False,
        }
    return {
        "current": current,
        "previous": previous,
        "delta": delta,
        "percent": round((delta / previous) * 100, 2),
        "direction": direction,
        "comparable": True,
    }


def _comparison_label(comparison, value_formatter=None):
    value_formatter = value_formatter or _format_int
    if not comparison["comparable"]:
        return "داده کافی نیست"
    percent = comparison["percent"] or 0
    sign = "+" if percent > 0 else ""
    delta = value_formatter(comparison["delta"])
    return f"{sign}{persian_digits(f'{percent:.1f}')}٪ ({delta})"


def _comparison_tone(comparison):
    return {"up": "success", "down": "danger", "flat": "secondary"}.get(comparison["direction"], "secondary")


def _comparison_direction_label(comparison):
    if not comparison["comparable"]:
        return "داده کافی نیست"
    return {"up": "رشد", "down": "افت", "flat": "بدون تغییر"}.get(comparison["direction"], "نامشخص")


def _period_filter(queryset, field_name, start, end):
    return queryset.filter(**{f"{field_name}__gte": start, f"{field_name}__lt": end})


def _period_date_filter(queryset, field_name, start_date, end_date):
    return queryset.filter(**{f"{field_name}__gte": start_date, f"{field_name}__lte": end_date})


def _for_store(queryset, store, field="store"):
    if store and getattr(store, "pk", None):
        return queryset.filter(**{f"{field}_id": store.pk})
    return queryset


def _success_at_range_q(prefix="", start=None, end=None):
    query = Q(**{f"{prefix}status__in": SUCCESSFUL_ORDER_STATUSES, f"{prefix}is_paid": True})
    if start:
        query &= (
            Q(**{f"{prefix}verified_at__gte": start})
            | (Q(**{f"{prefix}verified_at__isnull": True}) & Q(**{f"{prefix}created_at__gte": start}))
        )
    if end:
        query &= (
            Q(**{f"{prefix}verified_at__lt": end})
            | (Q(**{f"{prefix}verified_at__isnull": True}) & Q(**{f"{prefix}created_at__lt": end}))
        )
    return query


def _successful_orders(start, end, store=None):
    orders = Order.objects.filter(_success_at_range_q(start=start, end=end)).select_related("plan", "customer", "store")
    return _for_store(orders, store)


def _orders_created(start, end, store=None):
    return _for_store(_period_filter(Order.objects.all(), "created_at", start, end), store)


def _money_rows(queryset):
    return list(queryset.values("currency").annotate(total=Sum("amount"), count=Count("id")).order_by("currency"))


def _format_int(value):
    return persian_digits(f"{int(value or 0):,}")


def _format_decimal(value, places=1):
    if value is None:
        return "داده کافی نیست"
    number = Decimal(value)
    rendered = f"{number:,.{places}f}".rstrip("0").rstrip(".")
    return persian_digits(rendered)


def _currency_label(currency):
    return {
        Plan.Currency.TOMAN: "تومان",
        Plan.Currency.IRR: "ریال",
        Plan.Currency.USD: "USD",
    }.get(currency or Plan.Currency.TOMAN, currency or "")


def _format_money(amount, currency=Plan.Currency.TOMAN):
    return f"{_format_int(amount)} {_currency_label(currency)}"


def _format_money_summary(rows):
    if not rows:
        return "۰ تومان"
    return "، ".join(_format_money(row["total"] or 0, row["currency"]) for row in rows)


def _safe_ratio(numerator, denominator):
    numerator = int(numerator or 0)
    denominator = int(denominator or 0)
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _add_query(url, params=None, fragment=""):
    clean = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    if clean:
        url = f"{url}?{urlencode(clean)}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def reports_center_url(store=None, period=None, fragment=""):
    params = {}
    if period:
        params.update(period.query_params)
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    return _add_query(reverse("admin_store_reports_center"), params, fragment=fragment)


def report_export_url(report_type, store=None, period=None):
    params = {"report": report_type}
    if period:
        params.update(period.query_params)
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    return _add_query(reverse("admin_store_reports_export"), params)


def _admin_url(name, params=None):
    return _add_query(reverse(f"admin:{name}"), params or {})


def _workbench_url(name, store=None, fragment=""):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    if name == "admin_store_service_workbench" and fragment in SERVICE_REMOTE_FILTERS:
        params["remote_status"] = fragment
        fragment = ""
    return _add_query(reverse(name), params, fragment=fragment)


def get_report_store_options(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if selected_store is None:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def get_sales_metrics(period, store=None):
    orders = _orders_created(period.start, period.end, store)
    previous_orders = _orders_created(period.previous_start, period.previous_end, store)
    successful = _successful_orders(period.start, period.end, store)
    previous_successful = _successful_orders(period.previous_start, period.previous_end, store)
    renewal_filter = renewal_order_q()

    revenue_rows = _money_rows(successful)
    total_revenue = successful.aggregate(total=Sum("amount"))["total"] or 0
    previous_revenue = previous_successful.aggregate(total=Sum("amount"))["total"] or 0
    successful_count = successful.count()
    previous_successful_count = previous_successful.count()
    renewal_successful = successful.filter(renewal_filter)
    previous_renewal_successful = previous_successful.filter(renewal_filter)
    renewal_count = renewal_successful.count()
    renewal_revenue = renewal_successful.aggregate(total=Sum("amount"))["total"] or 0
    new_purchase_count = successful.exclude(renewal_filter).count()
    pending_count = orders.filter(status__in=PENDING_ORDER_STATUSES).count()
    failed_count = orders.filter(status__in=FAILED_ORDER_STATUSES).count()
    avg_order_value = int(total_revenue / successful_count) if successful_count else None
    primary_currency = revenue_rows[0]["currency"] if len(revenue_rows) == 1 else Plan.Currency.TOMAN

    volume_expression = ExpressionWrapper(F("plan__volume_gb") * F("quantity"), output_field=DECIMAL_FIELD)
    plan_rows = list(
        successful.values("plan_id", "plan__name", "plan__is_custom_volume", "currency")
        .annotate(count=Count("id"), revenue=Sum("amount"), sold_gb=Sum(volume_expression))
        .order_by("-count", "-revenue", "plan__name")[:5]
    )
    for row in plan_rows:
        row["plan_name"] = redact_sensitive(row["plan__name"] or "پلن حذف‌شده")
        row["type_label"] = "حجم سفارشی" if row["plan__is_custom_volume"] else "پلن ثابت"
        row["revenue_display"] = _format_money(row["revenue"] or 0, row["currency"])
        row["sold_gb_display"] = _format_decimal(row["sold_gb"] or Decimal("0"), places=1)

    custom_orders = successful.filter(plan__is_custom_volume=True)
    fixed_orders = successful.filter(Q(plan__is_custom_volume=False) | Q(plan__is_custom_volume__isnull=True))

    return {
        "revenue": total_revenue,
        "revenue_display": _format_money_summary(revenue_rows),
        "revenue_rows": revenue_rows,
        "revenue_comparison": compare_metric(total_revenue, previous_revenue),
        "successful_orders": successful_count,
        "successful_orders_comparison": compare_metric(successful_count, previous_successful_count),
        "pending_orders": pending_count,
        "failed_orders": failed_count,
        "renewal_successful": renewal_count,
        "renewal_revenue": renewal_revenue,
        "renewal_revenue_display": _format_money(renewal_revenue, primary_currency),
        "renewal_comparison": compare_metric(renewal_count, previous_renewal_successful.count()),
        "new_purchase_successful": new_purchase_count,
        "avg_order_value": avg_order_value,
        "avg_order_value_display": _format_money(avg_order_value, primary_currency) if avg_order_value is not None else "داده کافی نیست",
        "primary_currency": primary_currency,
        "top_plans": plan_rows,
        "custom_volume": {
            "count": custom_orders.count(),
            "revenue": custom_orders.aggregate(total=Sum("amount"))["total"] or 0,
        },
        "fixed_plan": {
            "count": fixed_orders.count(),
            "revenue": fixed_orders.aggregate(total=Sum("amount"))["total"] or 0,
        },
        "links": {
            "orders": _workbench_url("admin_store_order_workbench", store, "completed"),
            "catalog": _workbench_url("admin_store_catalog", store),
        },
    }


def _customers_for_store(store=None):
    customers = Customer.objects.filter(is_active=True)
    if store and getattr(store, "pk", None):
        customers = customers.filter(
            Q(orders__store=store)
            | Q(bot_users__bot_config__store=store)
            | Q(support_conversations__store=store)
            | Q(orders__vpn_clients__store=store)
        ).distinct()
    return customers


def get_customer_metrics(period, store=None):
    customers = _customers_for_store(store)
    new_customers = _period_filter(customers, "created_at", period.start, period.end)
    previous_new_customers = _period_filter(customers, "created_at", period.previous_start, period.previous_end)
    successful_customer_ids = _successful_orders(period.start, period.end, store).exclude(customer__isnull=True).values("customer_id").distinct()
    all_successful_orders = _for_store(
        Order.objects.filter(
            status__in=SUCCESSFUL_ORDER_STATUSES,
            is_paid=True,
        ).filter(
            Q(verified_at__lt=period.end)
            | (Q(verified_at__isnull=True) & Q(created_at__lt=period.end))
        ),
        store,
    )
    all_successful_customer_ids = all_successful_orders.exclude(customer__isnull=True).values("customer_id").distinct()
    active_service_customer_ids = _for_store(
        VPNClient.objects.filter(status=VPNClient.Status.ACTIVE, order__customer__isnull=False),
        store,
    ).values("order__customer_id").distinct()
    linked_customers = customers.annotate(
        telegram_target_count=Count(
            "bot_users",
            filter=Q(
                bot_users__is_active=True,
                bot_users__bot_config__is_active=True,
                bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
            )
            & ~Q(bot_users__chat_id=""),
            distinct=True,
        )
    )
    total_customers = customers.count()
    telegram_linked = linked_customers.filter(telegram_target_count__gt=0).count()
    repeat_customers = customers.annotate(
        successful_orders_total=Count(
            "orders",
            filter=Q(orders__status__in=SUCCESSFUL_ORDER_STATUSES, orders__is_paid=True),
            distinct=True,
        )
    ).filter(successful_orders_total__gte=2)
    returned_customers = customers.filter(pk__in=successful_customer_ids).filter(
        orders__status__in=SUCCESSFUL_ORDER_STATUSES,
        orders__is_paid=True,
        orders__created_at__lt=period.start,
    ).distinct()

    return {
        "new_customers": new_customers.count(),
        "new_customers_comparison": compare_metric(new_customers.count(), previous_new_customers.count()),
        "successful_customers": customers.filter(pk__in=successful_customer_ids).count(),
        "all_successful_customers": customers.filter(pk__in=all_successful_customer_ids).count(),
        "repeat_customers": repeat_customers.count(),
        "active_service_customers": customers.filter(pk__in=active_service_customer_ids).count(),
        "without_telegram_target": linked_customers.filter(telegram_target_count=0).count(),
        "returned_customers": returned_customers.count(),
        "telegram_linked": telegram_linked,
        "telegram_ratio": _safe_ratio(telegram_linked, total_customers),
        "total_customers": total_customers,
        "links": {
            "customers": _admin_url("store_customer_changelist"),
            "services": _workbench_url("admin_store_service_workbench", store, "active"),
        },
    }


def get_service_metrics(period, store=None):
    now = timezone.now()
    soon = now + timedelta(days=3)
    clients = _for_store(VPNClient.objects.select_related("order", "plan", "store"), store)
    current_clients = clients.exclude(status=VPNClient.Status.DELETED).filter(deleted_at__isnull=True)
    active = current_clients.filter(status=VPNClient.Status.ACTIVE)
    expired = current_clients.filter(Q(status=VPNClient.Status.EXPIRED) | Q(status=VPNClient.Status.ACTIVE, expires_at__lt=now))
    expiring = active.filter(expires_at__gte=now, expires_at__lte=soon)
    created = _period_filter(clients, "created_at", period.start, period.end)
    changed_problem = _period_filter(
        clients.filter(status__in=[VPNClient.Status.DELETED, VPNClient.Status.SUSPENDED, VPNClient.Status.ERROR]),
        "updated_at",
        period.start,
        period.end,
    )
    without_telegram = (
        current_clients.filter(order__customer__isnull=False)
        .annotate(
            telegram_target_count=Count(
                "order__customer__bot_users",
                filter=Q(
                    order__customer__bot_users__is_active=True,
                    order__customer__bot_users__bot_config__is_active=True,
                    order__customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                )
                & ~Q(order__customer__bot_users__chat_id=""),
                distinct=True,
            )
        )
        .filter(telegram_target_count=0)
    )
    low_usage_threshold = ExpressionWrapper(F("traffic_limit_bytes") * Value(20) / Value(100), output_field=BigIntegerField())
    low_usage = active.filter(traffic_limit_bytes__gt=0, used_traffic_bytes__lte=low_usage_threshold)
    silent_cutoff = now - timedelta(days=7)
    silent_active = active.filter(last_synced_at__isnull=False).filter(Q(last_online_at__isnull=True) | Q(last_online_at__lt=silent_cutoff))
    limit_totals = active.filter(traffic_limit_bytes__gt=0).aggregate(limit=Sum("traffic_limit_bytes"), used=Sum("used_traffic_bytes"))
    traffic_remaining = None
    usage_percent = None
    if limit_totals["limit"]:
        traffic_remaining = max(int(limit_totals["limit"] or 0) - int(limit_totals["used"] or 0), 0)
        usage_percent = _safe_ratio(limit_totals["used"], limit_totals["limit"])
    sold_volume = _successful_orders(period.start, period.end, store).aggregate(
        total=Sum(ExpressionWrapper(F("plan__volume_gb") * F("quantity"), output_field=DECIMAL_FIELD))
    )["total"]

    remote_missing_current = current_clients.filter(last_remote_check_status=VPNClient.RemoteCheckStatus.REMOTE_MISSING).count()
    remote_disabled_current = current_clients.filter(last_remote_check_status=VPNClient.RemoteCheckStatus.REMOTE_DISABLED).count()
    reconciliation_failures_current = current_clients.filter(
        last_remote_check_status__in=[
            VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE,
            VPNClient.RemoteCheckStatus.INBOUND_MISSING,
            VPNClient.RemoteCheckStatus.AMBIGUOUS,
            VPNClient.RemoteCheckStatus.UNKNOWN,
        ]
    ).count()

    status_rows = list(clients.values("status").annotate(count=Count("id")).order_by("status"))
    for row in status_rows:
        row["label"] = dict(VPNClient.Status.choices).get(row["status"], row["status"])

    return {
        "active_current": active.count(),
        "expired_current": expired.count(),
        "expiring_3d_current": expiring.count(),
        "created_period": created.count(),
        "problem_changed_period": changed_problem.count(),
        "without_telegram_target": without_telegram.count(),
        "low_usage_current": low_usage.count(),
        "silent_active_current": silent_active.count(),
        "traffic_sold_gb": sold_volume or Decimal("0"),
        "traffic_sold_gb_display": _format_decimal(sold_volume or Decimal("0"), places=1),
        "traffic_remaining": traffic_remaining,
        "traffic_remaining_display": format_bytes_fa(traffic_remaining) if traffic_remaining is not None else "نامشخص",
        "average_usage_percent": usage_percent,
        "average_usage_percent_display": f"{persian_digits(f'{usage_percent:.1f}')}٪" if usage_percent is not None else "داده کافی نیست",
        "remote_missing_current": remote_missing_current,
        "remote_disabled_current": remote_disabled_current,
        "reconciliation_failures_current": reconciliation_failures_current,
        "status_rows": status_rows,
        "links": {
            "workbench": _workbench_url("admin_store_service_workbench", store),
            "expiring": _workbench_url("admin_store_service_workbench", store, "expiring"),
            "remote_missing": _workbench_url("admin_store_service_workbench", store, "remote-missing"),
            "remote_disabled": _workbench_url("admin_store_service_workbench", store, "remote-disabled"),
            "remote_problem": _workbench_url("admin_store_service_workbench", store, "remote-problem"),
        },
    }


def _panel_queryset(store=None):
    panels = Panel.objects.filter(is_active=True).select_related("store").order_by("name", "pk")
    return _for_store(panels, store)


def _panel_usage_summary(start_date, end_date, store=None):
    usages = _period_date_filter(PanelDailyUsage.objects.select_related("panel"), "usage_date", start_date, end_date)
    if store and getattr(store, "pk", None):
        usages = usages.filter(panel__store=store)
    valid = usages.exclude(data_quality=PanelDailyUsage.DataQuality.INSUFFICIENT)
    valid_count = valid.count()
    row_count = usages.count()
    panel_count = _panel_queryset(store).count()
    expected_rows = max(panel_count, 1) * ((end_date - start_date).days + 1)
    if row_count == 0:
        quality = "unknown"
    elif valid_count == 0:
        quality = "insufficient"
    elif row_count < expected_rows or usages.exclude(data_quality=PanelDailyUsage.DataQuality.COMPLETE).exists():
        quality = "partial"
    else:
        quality = "complete"
    total_used = valid.aggregate(total=Sum("used_bytes"))["total"] if valid_count else None
    daily_rows = list(valid.values("usage_date").annotate(used=Sum("used_bytes"), active=Sum("active_users_count")).order_by("usage_date"))
    max_daily = max([int(row["used"] or 0) for row in daily_rows], default=None) if valid_count else None
    active_values = [int(row["active"] or 0) for row in daily_rows]
    return {
        "queryset": usages,
        "valid_queryset": valid,
        "quality": quality,
        "total_used": total_used,
        "row_count": row_count,
        "valid_count": valid_count,
        "daily_rows": daily_rows,
        "max_daily": max_daily,
        "avg_daily": int(total_used / max(len(daily_rows), 1)) if total_used is not None else None,
        "avg_active_users": round(sum(active_values) / len(active_values), 1) if active_values else None,
        "max_active_users": max(active_values) if active_values else None,
    }


def get_panel_usage_metrics(period, store=None):
    summary = _panel_usage_summary(period.start_date, period.end_date, store)
    previous_summary = _panel_usage_summary(period.previous_start_date, period.previous_end_date, store)
    panels = _panel_queryset(store)
    health_statuses = PanelHealthStatus.objects.filter(panel__in=panels).select_related("panel")
    latest_status = health_statuses.order_by("-last_checked_at", "-updated_at").first()
    health_logs = _period_filter(PanelHealthCheckLog.objects.filter(panel__in=panels), "checked_at", period.start, period.end)
    warning_error_checks = health_logs.filter(status__in=[PanelHealthStatus.Status.WARNING, PanelHealthStatus.Status.ERROR]).count()
    per_panel = list(
        summary["valid_queryset"]
        .values("panel_id", "panel__name")
        .annotate(
            used=Sum("used_bytes"),
            active=Sum("active_users_count"),
            max_used=Max("used_bytes"),
            days=Count("usage_date", distinct=True),
        )
        .order_by("-used", "panel__name")[:25]
    )
    for row in per_panel:
        row["name"] = redact_sensitive(row["panel__name"] or f"Panel #{row['panel_id']}")
        row["used_display"] = format_bytes_fa(row["used"])
        row["max_used_display"] = format_bytes_fa(row["max_used"])
        row["avg_active_display"] = _format_decimal(Decimal(row["active"] or 0) / Decimal(max(row["days"] or 1, 1)), places=1)

    quality_labels = {
        "complete": "کامل",
        "partial": "ناقص",
        "insufficient": "داده کافی نیست",
        "unknown": "نامشخص",
    }
    return {
        "total_used": summary["total_used"],
        "total_used_display": format_bytes_fa(summary["total_used"]) if summary["total_used"] is not None else "نامشخص",
        "comparison": compare_metric(summary["total_used"] or 0, previous_summary["total_used"] or 0),
        "avg_daily_display": format_bytes_fa(summary["avg_daily"]) if summary["avg_daily"] is not None else "نامشخص",
        "max_daily_display": format_bytes_fa(summary["max_daily"]) if summary["max_daily"] is not None else "نامشخص",
        "avg_active_users": summary["avg_active_users"],
        "max_active_users": summary["max_active_users"],
        "quality": summary["quality"],
        "quality_label": quality_labels.get(summary["quality"], "نامشخص"),
        "latest_health_label": latest_status.get_status_display() if latest_status else "نامشخص",
        "latest_health_panel": redact_sensitive(latest_status.panel.name) if latest_status else "",
        "warning_error_checks": warning_error_checks,
        "per_panel": per_panel,
        "links": {
            "panels": _admin_url("store_panel_changelist"),
            "health": _admin_url("store_panelhealthchecklog_changelist"),
        },
    }


def get_payment_metrics(period, store=None):
    messages = _period_filter(IncomingPaymentSMS.objects.all(), "received_at", period.start, period.end)
    if store and getattr(store, "pk", None):
        messages = messages.filter(Q(matched_orders__store=store) | Q(matched_orders__isnull=True)).distinct()
    return {
        "sms_matched": messages.filter(status__in=[IncomingPaymentSMS.Status.MATCHED, IncomingPaymentSMS.Status.CONFIRMED]).count(),
        "sms_no_match": messages.filter(status=IncomingPaymentSMS.Status.NO_MATCH).count(),
        "sms_new": messages.filter(status=IncomingPaymentSMS.Status.NEW).count(),
    }


def get_operational_metrics(period, store=None):
    orders = _for_store(Order.objects.all(), store)
    reminders = _period_filter(VPNClientReminderLog.objects.select_related("vpn_client"), "created_at", period.start, period.end)
    if store and getattr(store, "pk", None):
        reminders = reminders.filter(vpn_client__store=store)
    bot_events = _period_filter(BotEventLog.objects.select_related("order"), "created_at", period.start, period.end)
    if store and getattr(store, "pk", None):
        bot_events = bot_events.filter(Q(order__store=store) | Q(order__isnull=True))
    xui_actions = _period_filter(VPNClientActionLog.objects.select_related("vpn_client"), "created_at", period.start, period.end)
    if store and getattr(store, "pk", None):
        xui_actions = xui_actions.filter(Q(vpn_client__store=store) | Q(vpn_client__isnull=True))
    reports = _period_filter(_for_store(DailyAdminReportLog.objects.all(), store), "created_at", period.start, period.end)
    payment_metrics = get_payment_metrics(period, store)
    return {
        "pending_receipts": orders.filter(
            payment_method=Order.PaymentMethod.MANUAL_CARD,
            verification_status=Order.VerificationStatus.PENDING,
            status__in=[Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED],
        ).filter(Q(payment_receipt_image__isnull=False) | Q(payment_submitted_at__isnull=False) | Q(is_paid=True)).count(),
        "orders_requiring_action": orders.filter(status__in=[Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED]).count(),
        "sms_matched": payment_metrics["sms_matched"],
        "sms_no_match": payment_metrics["sms_no_match"],
        "reminder_sent": reminders.filter(status=VPNClientReminderLog.Status.SENT).count(),
        "reminder_failed": reminders.filter(status=VPNClientReminderLog.Status.FAILED).count(),
        "reminder_skipped": reminders.filter(status=VPNClientReminderLog.Status.SKIPPED).count(),
        "config_delivery_failed": bot_events.filter(event_type=BotEventLog.EventType.ORDER_APPROVED, status=BotEventLog.Status.FAILED).count(),
        "telegram_send_failed": bot_events.filter(status=BotEventLog.Status.FAILED).count(),
        "xui_operation_failed": xui_actions.filter(status=VPNClientActionLog.Status.FAILED).count(),
        "reconciliation_checks": xui_actions.filter(action=VPNClientActionLog.Action.ADMIN_RECONCILE_CHECK).count(),
        "soft_deleted_after_remote_missing": xui_actions.filter(
            action=VPNClientActionLog.Action.ADMIN_SOFT_DELETE_REMOTE_MISSING,
            status=VPNClientActionLog.Status.SUCCESS,
        ).count(),
        "reconciliation_soft_delete_failed": xui_actions.filter(
            action=VPNClientActionLog.Action.ADMIN_SOFT_DELETE_REMOTE_MISSING,
            status=VPNClientActionLog.Status.FAILED,
        ).count(),
        "daily_report_sent": reports.filter(status=DailyAdminReportLog.Status.SENT).count(),
        "daily_report_failed": reports.filter(status=DailyAdminReportLog.Status.FAILED).count(),
        "links": {
            "orders": _workbench_url("admin_store_order_workbench", store, "needs-review"),
            "services": _workbench_url("admin_store_service_workbench", store, "attention"),
            "support": _workbench_url("admin_store_support_workbench", store, "needs-reply"),
            "revenue": _workbench_url("admin_store_revenue_control", store),
            "health": _admin_url("store_panelhealthchecklog_changelist"),
        },
    }


def get_revenue_metrics(period, store=None):
    logs = _period_filter(RevenueOfferLog.objects.all(), "created_at", period.start, period.end)
    if store and getattr(store, "pk", None):
        logs = logs.filter(Q(store=store) | Q(store__isnull=True))
    status_counts = {row["status"]: row["count"] for row in logs.values("status").annotate(count=Count("id"))}
    sent = status_counts.get(RevenueOfferLog.Status.SENT, 0)
    converted = status_counts.get(RevenueOfferLog.Status.CONVERTED, 0)
    sent_denominator = sent + converted
    engine_rows = list(logs.values("engine_type").annotate(count=Count("id"), converted=Count("id", filter=Q(status=RevenueOfferLog.Status.CONVERTED))).order_by("-count", "engine_type"))
    decision_rows = list(logs.values("decision_source").annotate(count=Count("id"), converted=Count("id", filter=Q(status=RevenueOfferLog.Status.CONVERTED))).order_by("-count", "decision_source"))
    variant_rows = list(
        logs.exclude(variant="")
        .values("variant")
        .annotate(count=Count("id"), converted=Count("id", filter=Q(status=RevenueOfferLog.Status.CONVERTED)))
        .order_by("-converted", "-count", "variant")[:10]
    )
    for row in engine_rows:
        row["label"] = dict(RevenueOfferLog.EngineType.choices).get(row["engine_type"], row["engine_type"])
    for row in decision_rows:
        row["label"] = dict(RevenueOfferLog.DecisionSource.choices).get(row["decision_source"], row["decision_source"])
    for row in variant_rows:
        row["safe_variant"] = redact_sensitive(row["variant"])
        row["conversion_rate"] = _safe_ratio(row["converted"], row["count"])
    top_variant = None
    eligible_variants = [row for row in variant_rows if row["count"] >= 10]
    if eligible_variants:
        top_variant = max(eligible_variants, key=lambda row: (row["conversion_rate"] or 0, row["converted"], row["count"]))
    return {
        "total": logs.count(),
        "dry_run": status_counts.get(RevenueOfferLog.Status.DRY_RUN, 0),
        "sent": sent,
        "skipped_suppressed": status_counts.get(RevenueOfferLog.Status.SKIPPED, 0)
        + status_counts.get(RevenueOfferLog.Status.SUPPRESSED, 0),
        "failed": status_counts.get(RevenueOfferLog.Status.FAILED, 0),
        "converted": converted,
        "conversion_rate": _safe_ratio(converted, sent_denominator),
        "engine_rows": engine_rows,
        "decision_rows": decision_rows,
        "variant_rows": variant_rows,
        "top_variant": top_variant,
        "variant_insufficient": top_variant is None,
        "mode_label": "بدون Store" if not store else ("خاموش" if not store.revenue_engine_enabled else ("Dry-run" if store.revenue_engine_dry_run else "Real-send")),
        "mode_tone": "secondary" if not store else ("warning" if (not store.revenue_engine_enabled or store.revenue_engine_dry_run) else "danger"),
        "links": {
            "control": _workbench_url("admin_store_revenue_control", store),
            "logs": _admin_url("store_revenueofferlog_changelist"),
        },
    }


def get_support_metrics(period, store=None):
    conversations = _for_store(SupportConversation.objects.all(), store)
    messages = SupportMessage.objects.select_related("conversation")
    if store and getattr(store, "pk", None):
        messages = messages.filter(conversation__store=store)
    period_messages = _period_filter(messages, "created_at", period.start, period.end)
    return {
        "open_current": conversations.exclude(status=SupportConversation.Status.CLOSED).count(),
        "waiting_admin_current": conversations.filter(status=SupportConversation.Status.WAITING_ADMIN).count(),
        "created_period": _period_filter(conversations, "created_at", period.start, period.end).count(),
        "closed_period": _period_filter(conversations.filter(status=SupportConversation.Status.CLOSED), "closed_at", period.start, period.end).count(),
        "customer_messages": period_messages.filter(sender_type=SupportMessage.SenderType.CUSTOMER).count(),
        "admin_messages": period_messages.filter(sender_type=SupportMessage.SenderType.ADMIN).count(),
        "links": {
            "workbench": _workbench_url("admin_store_support_workbench", store),
            "needs_reply": _workbench_url("admin_store_support_workbench", store, "needs-reply"),
        },
    }


def _safe_snippet(value, limit=80):
    cleaned = redact_sensitive(value).replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def get_campaign_metrics(period, store=None):
    campaigns = _period_filter(_for_store(BroadcastMessage.objects.all(), store), "created_at", period.start, period.end)
    sent_campaigns = campaigns.filter(status=BroadcastMessage.Status.SENT)
    recipients = BroadcastRecipient.objects.select_related("campaign")
    if store and getattr(store, "pk", None):
        recipients = recipients.filter(campaign__store=store)
    recipients = _period_filter(recipients, "created_at", period.start, period.end)
    sent_recipients = recipients.filter(status=BroadcastRecipient.Status.SENT).count()
    failed_recipients = recipients.filter(status=BroadcastRecipient.Status.FAILED).count()
    skipped_recipients = recipients.filter(status=BroadcastRecipient.Status.SKIPPED).count()
    recent_campaigns = [
        {
            "title": _safe_snippet(campaign.title, 70) or f"Campaign #{campaign.pk}",
            "status": campaign.get_status_display(),
            "created_at": campaign.created_at,
            "sent_at": campaign.sent_at,
            "total_recipients": campaign.total_recipients,
            "success_count": campaign.success_count,
            "failed_count": campaign.failed_count,
            "url": reverse("admin_store_campaign_review", args=[campaign.pk]),
        }
        for campaign in campaigns.order_by("-created_at")[:RECENT_LIMIT]
    ]
    return {
        "campaigns_created": campaigns.count(),
        "campaigns_sent": sent_campaigns.count(),
        "recipients_sent": sent_recipients,
        "recipients_failed": failed_recipients,
        "recipients_skipped": skipped_recipients,
        "success_rate": _safe_ratio(sent_recipients, sent_recipients + failed_recipients),
        "recent_campaigns": recent_campaigns,
        "links": {
            "admin": _admin_url("store_broadcastmessage_changelist"),
            "workbench": _workbench_url("admin_store_campaign_workbench", store),
        },
    }


def _date_series(period):
    return [period.start_date + timedelta(days=offset) for offset in range(period.days)]


def _date_label(day):
    return format_jalali_date(day)


def _rows_by_day(rows, date_key, value_key):
    return {row[date_key]: row[value_key] or 0 for row in rows}


def get_daily_series(period, store=None):
    dates = _date_series(period)
    success_expression = Coalesce("verified_at", "created_at")
    successful = _successful_orders(period.start, period.end, store)
    revenue_rows = list(
        successful.annotate(day=TruncDate(success_expression, tzinfo=period.timezone))
        .values("day")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("day")
    )
    customers = _period_filter(_customers_for_store(store), "created_at", period.start, period.end)
    customer_rows = list(customers.annotate(day=TruncDate("created_at", tzinfo=period.timezone)).values("day").annotate(count=Count("id")).order_by("day"))
    usage = _period_date_filter(PanelDailyUsage.objects.exclude(data_quality=PanelDailyUsage.DataQuality.INSUFFICIENT), "usage_date", period.start_date, period.end_date)
    if store and getattr(store, "pk", None):
        usage = usage.filter(panel__store=store)
    usage_rows = list(usage.values("usage_date").annotate(total=Sum("used_bytes")).order_by("usage_date"))
    revenue_by_day = _rows_by_day(revenue_rows, "day", "total")
    orders_by_day = _rows_by_day(revenue_rows, "day", "count")
    customers_by_day = _rows_by_day(customer_rows, "day", "count")
    usage_by_day = _rows_by_day(usage_rows, "usage_date", "total")
    return {
        "labels": [_date_label(day) for day in dates],
        "revenue": [int(revenue_by_day.get(day, 0) or 0) for day in dates],
        "successful_orders": [int(orders_by_day.get(day, 0) or 0) for day in dates],
        "new_customers": [int(customers_by_day.get(day, 0) or 0) for day in dates],
        "panel_usage_gb": [round((int(usage_by_day.get(day, 0) or 0) / (1024**3)), 2) for day in dates],
        "has_panel_usage": any(int(usage_by_day.get(day, 0) or 0) > 0 for day in dates),
    }


def _metric(label, value, note="", tone="info", comparison=None, badge=""):
    return {
        "label": label,
        "value": value,
        "note": note,
        "tone": tone,
        "comparison": comparison,
        "comparison_label": _comparison_label(comparison) if comparison else "",
        "comparison_tone": _comparison_tone(comparison) if comparison else "",
        "comparison_direction": _comparison_direction_label(comparison) if comparison else "",
        "badge": badge,
    }


def get_reports_center_context(period, store=None):
    sales = get_sales_metrics(period, store)
    customers = get_customer_metrics(period, store)
    services = get_service_metrics(period, store)
    panel_usage = get_panel_usage_metrics(period, store)
    operations = get_operational_metrics(period, store)
    revenue = get_revenue_metrics(period, store)
    support = get_support_metrics(period, store)
    campaigns = get_campaign_metrics(period, store)
    charts = get_daily_series(period, store)
    report_params = period.query_params
    if store and getattr(store, "pk", None):
        report_params = {**report_params, "store": store.pk}

    kpi_cards = [
        _metric("درآمد دوره", sales["revenue_display"], "فقط سفارش‌های paid/completed", "success", sales["revenue_comparison"]),
        _metric("سفارش موفق", _format_int(sales["successful_orders"]), "paid/completed", "success", sales["successful_orders_comparison"]),
        _metric("میانگین ارزش سفارش", sales["avg_order_value_display"], "درآمد تقسیم بر سفارش موفق", "info"),
        _metric("مشتری جدید", _format_int(customers["new_customers"]), "Customerهای ساخته‌شده در دوره", "primary", customers["new_customers_comparison"]),
        _metric("تمدید موفق", _format_int(sales["renewal_successful"]), sales["renewal_revenue_display"], "primary", sales["renewal_comparison"]),
        _metric("سرویس فعال", _format_int(services["active_current"]), "وضعیت فعلی", "safe", badge="وضعیت فعلی"),
        _metric("در حال انقضا", _format_int(services["expiring_3d_current"]), "تا ۳ روز آینده؛ وضعیت فعلی", "warning", badge="وضعیت فعلی"),
        _metric("حذف‌شده از پنل", _format_int(services["remote_missing_current"]), "وضعیت فعلی از آخرین reconciliation", "danger", badge="DB only"),
        _metric("خطای reconciliation", _format_int(services["reconciliation_failures_current"]), "panel/unbound/ambiguous/unknown ذخیره‌شده", "warning", badge="DB only"),
        _metric("مصرف پنل", panel_usage["total_used_display"], panel_usage["quality_label"], "info", panel_usage["comparison"]),
    ]
    range_options = [
        {"key": "today", "label": "امروز"},
        {"key": "7d", "label": "۷ روز اخیر"},
        {"key": "30d", "label": "۳۰ روز اخیر"},
        {"key": "this_month", "label": "این ماه"},
        {"key": "previous_month", "label": "ماه قبل"},
    ]
    export_urls = {report: report_export_url(report, store, period) for report in sorted(VALID_REPORT_EXPORTS)}
    return {
        "period": period,
        "range_options": range_options,
        "report_query_params": report_params,
        "kpi_cards": kpi_cards,
        "sales": sales,
        "customers": customers,
        "services": services,
        "panel_usage": panel_usage,
        "operations": operations,
        "revenue_engine": revenue,
        "support": support,
        "campaigns": campaigns,
        "charts": charts,
        "export_urls": export_urls,
        "links": {
            "dashboard": _workbench_url("admin_store_owner_dashboard", store),
            "orders": _workbench_url("admin_store_order_workbench", store),
            "services": _workbench_url("admin_store_service_workbench", store),
            "support": _workbench_url("admin_store_support_workbench", store),
            "revenue": _workbench_url("admin_store_revenue_control", store),
            "catalog": _workbench_url("admin_store_catalog", store),
        },
    }


def redact_sensitive(value):
    text = str(value or "")
    text = CONFIG_LINK_RE.sub("[redacted-config-link]", text)
    text = SUB_LINK_RE.sub("[redacted-sub-link]", text)
    text = UUID_RE.sub("[redacted-uuid]", text)
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = PHONE_RE.sub("[redacted-phone]", text)
    text = LONG_TOKEN_RE.sub("[redacted-token]", text)
    return text


def _csv_response(rows):
    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    for row in rows:
        writer.writerow([redact_sensitive(cell) for cell in row])
    return output.getvalue()


def _filename(report_type, period):
    safe_report = report_type.replace("_", "-")
    return f"qasedak-{safe_report}-report-{period.start_date.isoformat()}-to-{period.end_date.isoformat()}.csv"


def build_reports_csv(period, report_type, store=None):
    if report_type not in VALID_REPORT_EXPORTS:
        raise ValueError("نوع گزارش نامعتبر است.")
    rows = [["گزارش", report_type], ["بازه", period.display], ["فروشگاه", getattr(store, "name", "همه فروشگاه‌ها") or "همه فروشگاه‌ها"], []]
    if report_type == "sales":
        sales = get_sales_metrics(period, store)
        rows += [
            ["metric", "value"],
            ["revenue", sales["revenue_display"]],
            ["successful_orders", sales["successful_orders"]],
            ["pending_orders", sales["pending_orders"]],
            ["rejected_cancelled_orders", sales["failed_orders"]],
            ["renewal_successful", sales["renewal_successful"]],
            ["new_purchase_successful", sales["new_purchase_successful"]],
            ["avg_order_value", sales["avg_order_value_display"]],
            [],
            ["top_plan", "count", "revenue", "sold_gb", "type"],
        ]
        rows += [[row["plan_name"], row["count"], row["revenue_display"], row["sold_gb_display"], row["type_label"]] for row in sales["top_plans"]]
    elif report_type == "customers":
        customers = get_customer_metrics(period, store)
        rows += [
            ["metric", "value"],
            ["new_customers", customers["new_customers"]],
            ["successful_customers", customers["successful_customers"]],
            ["repeat_customers", customers["repeat_customers"]],
            ["active_service_customers", customers["active_service_customers"]],
            ["without_telegram_target", customers["without_telegram_target"]],
            ["returned_customers", customers["returned_customers"]],
            ["telegram_linked_ratio", customers["telegram_ratio"] if customers["telegram_ratio"] is not None else "insufficient"],
        ]
    elif report_type == "services":
        services = get_service_metrics(period, store)
        rows += [
            ["metric", "value"],
            ["active_current", services["active_current"]],
            ["expired_current", services["expired_current"]],
            ["expiring_3d_current", services["expiring_3d_current"]],
            ["created_period", services["created_period"]],
            ["deleted_suspended_error_period", services["problem_changed_period"]],
            ["without_telegram_target", services["without_telegram_target"]],
            ["traffic_sold_gb", services["traffic_sold_gb_display"]],
            ["traffic_remaining_current", services["traffic_remaining_display"]],
            ["average_usage_percent", services["average_usage_percent_display"]],
            ["remote_missing_current", services["remote_missing_current"]],
            ["remote_disabled_current", services["remote_disabled_current"]],
            ["reconciliation_failures_current", services["reconciliation_failures_current"]],
        ]
    elif report_type == "operations":
        operations = get_operational_metrics(period, store)
        support = get_support_metrics(period, store)
        rows += [["metric", "value"]]
        for key in [
            "pending_receipts",
            "orders_requiring_action",
            "sms_matched",
            "sms_no_match",
            "reminder_sent",
            "reminder_failed",
            "reminder_skipped",
            "config_delivery_failed",
            "telegram_send_failed",
            "xui_operation_failed",
            "reconciliation_checks",
            "soft_deleted_after_remote_missing",
            "reconciliation_soft_delete_failed",
            "daily_report_sent",
            "daily_report_failed",
        ]:
            rows.append([key, operations[key]])
        rows += [
            ["support_open_current", support["open_current"]],
            ["support_waiting_admin_current", support["waiting_admin_current"]],
        ]
    elif report_type == "revenue":
        revenue = get_revenue_metrics(period, store)
        rows += [
            ["metric", "value"],
            ["mode", revenue["mode_label"]],
            ["total", revenue["total"]],
            ["dry_run", revenue["dry_run"]],
            ["sent", revenue["sent"]],
            ["skipped_suppressed", revenue["skipped_suppressed"]],
            ["failed", revenue["failed"]],
            ["converted", revenue["converted"]],
            ["conversion_rate", revenue["conversion_rate"] if revenue["conversion_rate"] is not None else "insufficient"],
            [],
            ["engine_type", "count", "converted"],
        ]
        rows += [[row["label"], row["count"], row["converted"]] for row in revenue["engine_rows"]]
    elif report_type == "panel_usage":
        panel_usage = get_panel_usage_metrics(period, store)
        rows += [
            ["metric", "value"],
            ["total_used", panel_usage["total_used_display"]],
            ["avg_daily", panel_usage["avg_daily_display"]],
            ["max_daily", panel_usage["max_daily_display"]],
            ["avg_active_users", panel_usage["avg_active_users"] if panel_usage["avg_active_users"] is not None else "unknown"],
            ["max_active_users", panel_usage["max_active_users"] if panel_usage["max_active_users"] is not None else "unknown"],
            ["data_quality", panel_usage["quality_label"]],
            ["latest_health", panel_usage["latest_health_label"]],
            ["warning_error_checks", panel_usage["warning_error_checks"]],
            [],
            ["panel", "used", "max_daily", "avg_active_users", "days"],
        ]
        rows += [[row["name"], row["used_display"], row["max_used_display"], row["avg_active_display"], row["days"]] for row in panel_usage["per_panel"]]
    return _filename(report_type, period), _csv_response(rows)
