from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Count, F
from django.db.models.functions import TruncDate
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from store.admin_access import require_admin_capability, user_has_any_capability
from store.admin_revenue import get_store_options
from store.models import Customer, Order
from .mapping import FULFILLED
from .models import ActivityCollector, ActivityObservation, PurchaseActivity
from .services import REASONS, activity_status, collector_healthy, minimum_bytes

TEHRAN = ZoneInfo("Asia/Tehran")
LABELS = {"active": "مصرف در ۴۸ ساعت", "inactive": "۴۸ ساعت بدون مصرف", "unknown": "نامشخص",
          "collecting": "در حال جمع‌آوری داده", "conflict": "مغایرت سفارش و سرویس",
          "superseded": "تمدیدشده با خرید بعدی", "closed": "سفارش بسته‌شده", "lost": "ازدست‌رفته",
          "at_risk": "نیازمند پیگیری", "no_purchase": "بدون خرید تأییدشده", "new": "جدید", "loyal": "وفادار"}
ENTITLEMENTS = {"valid": "قابل استفاده", "expired": "منقضی", "exhausted": "حجم تمام‌شده",
                "disabled": "غیرفعال", "ended": "پایان‌یافته", "unknown": "نیازمند بررسی",
                "closed": "سفارش بسته‌شده", "conflict": "مغایرت", "superseded": "دورهٔ قبلی تمدید"}


def daily_counts(store, *, now):
    today = now.astimezone(TEHRAN).date()
    since = today - timedelta(days=13)
    query = ActivityObservation.objects.filter(
        purchase__order__store=store, purchase__order__customer__isnull=False,
        observed_at__gte=datetime.combine(since, time.min, tzinfo=TEHRAN), delta_bytes__isnull=False, quality__in=("ok", "partial_ok"),
    ).annotate(day=TruncDate("observed_at", tzinfo=TEHRAN), start_day=TruncDate("interval_start", tzinfo=TEHRAN))
    # Crossing midnight cannot prove on which day bytes flowed; omit that interval.
    query = query.filter(day=F("start_day"))
    def counts(qs):
        return {row["day"]: row["count"] for row in qs.values("day").annotate(count=Count("purchase__order__customer", distinct=True))}
    active = counts(query.filter(delta_bytes__gte=minimum_bytes()))
    measured = counts(query)
    maximum = max(active.values(), default=1) or 1
    return [{"day": since + timedelta(days=day), "active": active.get(since + timedelta(days=day), 0),
             "measured": measured.get(since + timedelta(days=day), 0),
             "width": round(100 * active.get(since + timedelta(days=day), 0) / maximum)} for day in range(14)]


def customer_rows(store, *, now):
    activities = {item.order_id: item for item in PurchaseActivity.objects.filter(order__store=store)}
    orders = Order.objects.filter(store=store, customer__isnull=False).select_related("plan").order_by("-created_at")
    by_customer = defaultdict(list)
    for order in orders:
        activity = activities.get(order.pk)
        if not activity and order.status not in FULFILLED:
            continue
        state, reason = activity_status(activity, now=now)
        entitlement = activity.entitlement if activity and reason != "stale" else "unknown"
        by_customer[order.customer_id].append({
            "order": order, "activity": activity, "state": state, "label": LABELS[state],
            "reason": REASONS.get(reason, reason), "entitlement": entitlement,
            "entitlement_label": ENTITLEMENTS.get(entitlement, entitlement),
        })
    rows = []
    for customer in Customer.objects.filter(orders__store=store).distinct().order_by("pk"):
        purchases = by_customer[customer.pk]
        current = [p for p in purchases if p["entitlement"] not in {"superseded", "closed"}]
        successful = [p["order"] for p in purchases if p["order"].status in FULFILLED and p["order"].verification_status == "verified"]
        states = {p["state"] for p in current}
        ended = current and all(p["entitlement"] in {"expired", "exhausted", "disabled", "ended"} for p in current)
        tags = set()
        if "conflict" in states:
            tags.add("conflict")
        if "active" in states:
            state = "active"
        elif "unknown" in states or "conflict" in states:
            state = "unknown"
        elif ended and all(p["activity"].ended_at and p["activity"].ended_at <= now - timedelta(days=7) for p in current):
            state = "lost"
        elif "collecting" in states:
            state = "collecting"
        elif states and states == {"inactive"}:
            state = "inactive"
        else:
            state = "unknown" if successful else "no_purchase"
        if any(p["state"] == "inactive" and p["entitlement"] == "valid" for p in current):
            tags.add("at_risk")
        if successful:
            first = min(order.verified_at or order.created_at for order in successful)
            if first >= now - timedelta(days=30):
                tags.add("new")
            if len(successful) >= 2:
                tags.add("loyal")
        tags.add(state)
        rows.append({"customer": customer, "purchases": purchases, "state": state, "label": LABELS[state],
                     "tags": tags, "tag_labels": [LABELS[tag] for tag in ("at_risk", "new", "loyal", "conflict") if tag in tags]})
    return rows


@require_GET
@require_admin_capability("revenue.view")
def activity_dashboard(request):
    stores, store = get_store_options(request.GET.get("store"))
    now = timezone.now()
    rows = customer_rows(store, now=now) if store else []
    counts = {key: sum(key in row["tags"] for row in rows) for key in LABELS}
    segment = request.GET.get("segment", "")
    search = request.GET.get("q", "").strip()[:100]
    filtered = [row for row in rows if (not segment or segment in row["tags"]) and
                (not search or search.casefold() in f'{row["customer"].display_name} {row["customer"].username} {row["customer"].pk}'.casefold())]
    collector = ActivityCollector.objects.filter(pk=1).first()
    days = daily_counts(store, now=now) if store else []
    context = {
        **admin.site.each_context(request), "title": "فعالیت واقعی مشتری‌ها", "stores": stores,
        "selected_store": store, "rows": Paginator(filtered, 25).get_page(request.GET.get("page")),
        "segments": [(key, LABELS[key], counts[key]) for key in ("active", "inactive", "at_risk", "lost", "new", "loyal", "unknown", "collecting", "conflict")],
        "selected_segment": segment, "search": search, "total": len(rows), "days": days,
        "active_today": days[-1]["active"] if days else 0,
        "measured_today": days[-1]["measured"] if days else 0,
        "collector": collector, "collector_healthy": collector_healthy(collector, now=now),
        "can_review_customer": user_has_any_capability(request.user, "orders.view", "services.view", "support.view"),
        "updated": now, "min_bytes": minimum_bytes(),
    }
    return TemplateResponse(request, "admin/store/customers/activity.html", context)
