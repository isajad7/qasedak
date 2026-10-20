from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, Count, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import TruncDate
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from store.admin_access import require_admin_capability, user_has_any_capability
from store.admin_revenue import get_store_options
from store.admin_support_services import customer_message_url
from store.models import BotUser, Customer, Order, normalize_payment_digits
from store.order_revenue import is_admin_direct_order
from .mapping import FULFILLED
from .models import ActivityCollector, ActivityObservation, PurchaseActivity
from .services import REASONS, activity_status, collector_healthy, minimum_bytes

TEHRAN = ZoneInfo("Asia/Tehran")
LABELS = {"active": "فعال · مصرف در ۴۸ ساعت", "inactive": "۴۸ ساعت بدون مصرف", "unknown": "نامشخص",
          "collecting": "نامشخص · در حال جمع‌آوری داده", "conflict": "مغایرت سفارش و سرویس",
          "superseded": "تمدیدشده با خرید بعدی", "closed": "سفارش بسته‌شده", "lost": "تمدیدنکرده / ازدست‌رفته",
          "awaiting_renewal": "در مهلت تمدید",
          "at_risk": "نیازمند پیگیری", "no_purchase": "بدون خرید تجاری تأییدشده", "new": "جدید", "loyal": "وفادار"}
ENTITLEMENTS = {"valid": "قابل استفاده", "expired": "منقضی", "exhausted": "حجم تمام‌شده",
                "disabled": "غیرفعال", "ended": "پایان‌یافته", "unknown": "نیازمند بررسی",
                "closed": "سفارش بسته‌شده", "conflict": "مغایرت", "superseded": "دورهٔ قبلی تمدید"}
VALID_QUALITIES = ("ok", "baseline", "gap", "counter_reset", "mapping_changed", "cycle_changed")
ENDED = {"expired", "exhausted", "disabled", "ended"}


def daily_counts(store, *, now):
    today = now.astimezone(TEHRAN).date()
    since = today - timedelta(days=13)
    query = ActivityObservation.objects.filter(
        purchase__order__store=store, purchase__order__customer__isnull=False,
        observed_at__gte=datetime.combine(since, time.min, tzinfo=TEHRAN), delta_bytes__isnull=False, quality__in=("ok", "partial_ok"),
    ).annotate(day=TruncDate("observed_at", tzinfo=TEHRAN), start_day=TruncDate("interval_start", tzinfo=TEHRAN))
    # CASE keeps absent JSON keys from turning NOT(...) into SQL NULL.
    query = query.annotate(admin_direct=Case(When(
        Q(purchase__order__metadata__admin_direct_purchase=True) |
        Q(purchase__order__metadata__source="admin_direct_purchase"), then=Value(True)),
        default=Value(False), output_field=BooleanField(),
    )).filter(admin_direct=False)
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
    valid = ActivityObservation.objects.filter(
        purchase_id=OuterRef("pk"), observed_at__lte=now,
        quality__in=(*VALID_QUALITIES, *(f"partial_{q}" for q in VALID_QUALITIES)),
    ).order_by("-observed_at")
    proven = valid.filter(quality__in=("ok", "partial_ok"), delta_bytes__gte=minimum_bytes(), interval_start__isnull=False)
    activities = {item.order_id: item for item in PurchaseActivity.objects.filter(order__store=store).annotate(
        last_valid_at=Subquery(valid.values("observed_at")[:1]),
        proven_start=Subquery(proven.values("interval_start")[:1]),
        proven_end=Subquery(proven.values("observed_at")[:1]),
    )}
    orders = Order.objects.filter(store=store, customer__isnull=False).select_related("plan").order_by("-created_at")
    by_customer = defaultdict(list)
    for order in orders:
        if is_admin_direct_order(order):
            continue
        activity = activities.get(order.pk)
        if not activity and order.status not in FULFILLED:
            continue
        state, reason = activity_status(activity, now=now)
        entitlement = activity.entitlement if activity and state != "unknown" else "unknown"
        last_valid = activity.last_valid_at if activity else None
        if activity and activity.reason in VALID_QUALITIES and activity.observed_at and activity.observed_at <= now:
            last_valid = max(filter(None, (last_valid, activity.observed_at)))
        by_customer[order.customer_id].append({
            "order": order, "activity": activity, "state": state, "label": LABELS[state],
            "reason": REASONS.get(reason, reason), "entitlement": entitlement,
            "entitlement_label": ENTITLEMENTS.get(entitlement, entitlement),
            "service_name": f"{order.plan.name} · #{order.pk}", "last_valid_at": last_valid,
        })
    users = list(BotUser.objects.filter(bot_config__store=store).select_related("bot_config").order_by("pk"))
    users_by_customer = defaultdict(list)
    for user in users:
        users_by_customer[user.customer_id].append(user)
    rows = []
    customers = Customer.objects.filter(Q(orders__store=store) | Q(bot_users__bot_config__store=store)).distinct().order_by("pk")
    for customer in customers:
        purchases = by_customer[customer.pk]
        current = [p for p in purchases if p["entitlement"] not in {"superseded", "closed"}]
        successful = [p["order"] for p in purchases if p["order"].status in FULFILLED and p["order"].verification_status == "verified"]
        # Gifts protect entitlement, but are not commercial purchase history unless
        # the admin explicitly recorded revenue. A zero-price ordinary order still is.
        commercial = [order for order in successful if order.payment_method != Order.PaymentMethod.ADMIN_FREE
                      or order.revenue_included is True]
        states = {p["state"] for p in current}
        ended = bool(current) and all(p["entitlement"] in ENDED and p["activity"].ended_at for p in current)
        ends = [p["activity"].ended_at for p in current if p["entitlement"] in ENDED and p["activity"].ended_at]
        last_end = max(ends, default=None)
        followup = [p["service_name"] for p in current if p["state"] == "inactive" and p["entitlement"] == "valid"]
        tags = set()
        if "conflict" in states:
            tags.add("conflict")
        if ended and commercial:
            state = "lost" if last_end <= now - timedelta(days=store.customer_lost_grace_days) else "awaiting_renewal"
        elif "active" in states:
            state = "active"
        elif "unknown" in states or "conflict" in states:
            state = "unknown"
        elif followup:
            state = "inactive"
        elif "collecting" in states:
            state = "collecting"
        elif states and states == {"inactive"}:
            state = "inactive"
        else:
            state = "unknown" if commercial else "no_purchase"
        if followup:
            tags.add("at_risk")
        if not commercial:
            tags.add("no_purchase")
        if state == "collecting":
            tags.add("unknown")
        if commercial:
            first = min(order.verified_at or order.created_at for order in commercial)
            if first >= now - timedelta(days=30):
                tags.add("new")
            if len(commercial) >= 2:
                tags.add("loyal")
        if state == "lost":
            reason = f"همهٔ سرویس‌ها {(now - last_end).days} روز پیش تمام شده‌اند و تمدید یا سرویس جایگزین موفقی ثبت نشده است."
        elif state == "awaiting_renewal":
            reason = f"همهٔ سرویس‌ها پایان یافته‌اند؛ مهلت {store.customer_lost_grace_days} روزهٔ تمدید هنوز تمام نشده است."
        elif state == "active":
            reason = "در بازهٔ ۴۸ ساعت اخیر، افزایش معتبر شمارندهٔ مصرف ثبت شده است."
        elif followup:
            reason = "سرویس معتبر با ۴۸ ساعت مشاهدهٔ پیوسته و بدون مصرف: " + "، ".join(followup)
        elif state == "no_purchase":
            reason = "خرید تجاری تأییدشده‌ای ندارد؛ خرید مستقیم ادمین و هدیهٔ بدون درآمد، سابقهٔ خرید تجاری نیستند."
        else:
            reason = "برای قضاوت دربارهٔ همهٔ سرویس‌های فعلی دادهٔ کافی و پیوسته نداریم."
        issues = [f'{p["service_name"]}: {p["reason"]}' for p in current
                  if p["state"] in {"unknown", "conflict", "collecting"} or
                  (p["activity"] and p["activity"].reason.startswith("partial_"))]
        evidence = [(a.proven_start, a.proven_end) for p in purchases
                    if (a := p["activity"]) and a.proven_start and a.proven_end]
        proven_start, proven_end = max(evidence, key=lambda value: value[1], default=(None, None))
        tags.add(state)
        row = {"customer": customer, "purchases": purchases, "state": state, "label": LABELS[state],
               "tags": tags, "reason": reason, "data_issues": issues, "followup_services": followup,
               "tag_labels": [LABELS[tag] for tag in ("at_risk", "no_purchase", "new", "loyal", "conflict") if tag in tags and tag != state],
               "last_purchase_at": max((o.verified_at or o.created_at for o in commercial), default=None),
               "last_renewal_at": max((o.verified_at or o.created_at for o in commercial
                                       if (o.metadata or {}).get("renewal_client_pk")), default=None),
               "last_service_end": last_end, "proven_start": proven_start, "proven_end": proven_end,
               "last_valid_at": max((p["last_valid_at"] for p in purchases if p["last_valid_at"]), default=None),
               "last_attempt_at": max((p["activity"].observed_at for p in current if p["activity"] and p["activity"].observed_at), default=None),
               "bot_users": users_by_customer[customer.pk],
               "profile_url": reverse("admin_store_customer_review", args=[customer.pk]),
               "message_url": customer_message_url(customer),
               "name": customer.display_name or customer.username or "مشتری"}
        rows.append(row)
    # Opening this GET page must not create Customer records for unlinked users.
    for user in users_by_customer[None]:
        rows.append({"customer": None, "bot_users": [user], "purchases": [], "state": "no_purchase",
                     "label": LABELS["no_purchase"], "tags": {"no_purchase"}, "tag_labels": [],
                     "name": user.display_name or " ".join(filter(None, (user.first_name, user.last_name))) or "کاربر ربات",
                     "reason": "کاربر ثبت‌شدهٔ همین فروشگاه، بدون خرید و پروندهٔ مشتری متصل.",
                     "profile_url": reverse("admin:store_botuser_customer", args=[user.pk])})
    for row in rows:
        row["search_text"] = " ".join([row["name"], str(row["customer"].pk) if row["customer"] else "",
                                         row["customer"].username if row["customer"] else "", *[
                                             f"{u.username} {u.provider_user_id} {u.display_name} {u.first_name} {u.last_name}"
                                             for u in row["bot_users"]]]).casefold()
    return rows


@require_GET
@require_admin_capability("revenue.view")
def activity_dashboard(request):
    stores, store = get_store_options(request.GET.get("store"))
    now = timezone.now()
    rows = customer_rows(store, now=now) if store else []
    counts = {key: sum(key in row["tags"] for row in rows) for key in LABELS}
    segment = request.GET.get("segment", "")
    search = normalize_payment_digits(request.GET.get("q", "")).strip()[:100]
    query = search.lstrip("@").casefold()
    filtered = [row for row in rows if (not segment or segment in row["tags"]) and
                (not query or query in row["search_text"])]
    collector = ActivityCollector.objects.filter(pk=1).first()
    days = daily_counts(store, now=now) if store else []
    context = {
        **admin.site.each_context(request), "title": "فعالیت واقعی مشتری‌ها", "stores": stores,
        "selected_store": store, "rows": Paginator(filtered, 25).get_page(request.GET.get("page")),
        "segments": [(key, LABELS[key], counts[key]) for key in ("active", "at_risk", "lost", "awaiting_renewal", "no_purchase", "new", "loyal", "unknown", "collecting", "conflict")],
        "selected_segment": segment, "search": search, "total": len(rows), "days": days,
        "active_today": days[-1]["active"] if days else 0,
        "measured_today": days[-1]["measured"] if days else 0,
        "collector": collector, "collector_healthy": collector_healthy(collector, now=now),
        "can_review_customer": user_has_any_capability(request.user, "orders.view", "services.view", "support.view"),
        "can_message_customer": user_has_any_capability(request.user, "support.reply"),
        "updated": now, "min_bytes": minimum_bytes(),
    }
    return TemplateResponse(request, "admin/store/customers/activity.html", context)
