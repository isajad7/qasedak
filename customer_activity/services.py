"""Counter evidence and a fenced, read-only panel collector for Revenue Engine stage 1."""
from collections import Counter
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from store.models import Order, Panel
from .mapping import FULFILLED, load_purchase_maps, resolve_maps
from .models import ActivityCollector, ActivityObservation, PurchaseActivity
from .panels import read_panel

WINDOW = timedelta(hours=48)
MAX_GAP = timedelta(minutes=45)
LEASE = timedelta(minutes=10)
def minimum_bytes():
    return max(1, getattr(settings, "CUSTOMER_ACTIVITY_MIN_BYTES", 1))

REASONS = {
    "not_observed": "هنوز پایش نشده",
    "baseline": "نمونهٔ اولیه؛ منتظر تغییر مصرف",
    "ok": "مصرف از پنل با هویت اختصاصی خوانده شده",
    "unmapped_source": "منبع مصرف این خرید کامل یا یکتا شناسایی نشده",
    "shared_identity": "کانفیگ مشترک؛ مصرف این خرید قابل تفکیک نیست",
    "ownership_conflict": "مالک مشتری و سرویس هم‌خوان نیست",
    "missing_counters": "پنل شمارندهٔ معتبر برنگردانده",
    "panel_unreachable": "پنل در دسترس نیست",
    "partial": "بخشی از اطلاعات پنل قابل دریافت نیست",
    "tracking_disabled": "پایش این پنل غیرفعال است",
    "unsupported_panel": "پایش این نوع پنل هنوز پشتیبانی نمی‌شود",
    "counter_reset": "شمارنده ریست شده؛ شروع بازهٔ مشاهدهٔ جدید",
    "mapping_changed": "کانفیگ‌های خرید تغییر کرده؛ شروع مشاهدهٔ جدید",
    "cycle_changed": "سقف حجم یا انقضا تغییر کرده؛ شروع دورهٔ جدید",
    "gap": "وقفه در داده‌ها؛ برای تشخیص عدم مصرف باید دوباره داده جمع شود",
    "stale": "دادهٔ تازه در دسترس نیست",
    "closed_order_active": "سفارش بسته شده ولی سرویس روی پنل فعال است",
    "closed_cup_active": "ساب بسته شده ولی کانفیگ روی پنل هنوز قابل استفاده است",
    "unverified_service": "سرویس دارد ولی سفارش تأییدشده نیست",
    "superseded": "این سرویس با خرید بعدی تمدید شده",
}


def entitlement(mapping, rows, now):
    if mapping.superseded:
        return "superseded", "superseded", None
    order = mapping.order
    states, ends = [], []
    for row in rows.values():
        expiry = row.get("expiry_time")
        total = row.get("total_bytes")
        if expiry and expiry <= now:
            states.append("expired")
            ends.append(expiry)
        elif total and total > 0 and row["used_bytes"] >= total:
            states.append("exhausted")
        elif row.get("enabled") is False:
            states.append("disabled")
        elif row.get("enabled") is True and total is not None:
            states.append("valid")
        else:
            states.append("unknown")
    if order.status in {Order.Status.CANCELLED, Order.Status.REJECTED}:
        if "valid" in states:
            return "conflict", "closed_order_active", None
        return "closed", "", None
    if order.status not in FULFILLED or order.verification_status != Order.VerificationStatus.VERIFIED:
        return "conflict", "unverified_service", None
    if not states:
        return "unknown", "", None
    cups = mapping.cups
    if cups and all(cup.status != "active" or (cup.expires_at and cup.expires_at <= now) for cup in cups):
        if "valid" in states:
            return "conflict", "closed_cup_active", None
        return "disabled", "", None
    unique = set(states)
    if unique == {"valid"}:
        return "valid", "", None
    if "valid" in unique or "unknown" in unique:
        return "unknown", "", None
    # All delivered sources have ended; a customer with another valid purchase is not lost.
    return (states[0] if len(unique) == 1 else "ended"), "", max(ends) if len(ends) == len(states) else None


@transaction.atomic
def record_purchase(mapping, rows, reason, *, now):
    activity, _ = PurchaseActivity.objects.get_or_create(order=mapping.order)
    activity = PurchaseActivity.objects.select_for_update().get(pk=activity.pk)
    if activity.observed_at and now <= activity.observed_at:
        return activity
    state, state_reason, ended = entitlement(mapping, rows, now)
    reason = state_reason or reason
    if reason and reason != "superseded":
        if state not in {"closed", "conflict"}:
            state = "unknown"
    current = {}
    if not reason:
        for key, row in rows.items():
            values = [row.get(name) for name in ("used_bytes", "upload_bytes", "download_bytes")]
            if any(not isinstance(value, int) or value < 0 for value in values):
                reason = "missing_counters"
                break
            current[key] = {
                "used": values[0], "up": values[1], "down": values[2],
                "at": row["captured_at"].isoformat(), "quota": row.get("total_bytes"),
                "expiry": row["expiry_time"].isoformat() if row.get("expiry_time") else None,
            }
    previous = activity.counters
    delta, start = None, None
    quality = reason or "ok"
    if not reason:
        times = [parse_datetime(value["at"]) for value in current.values()]
        if not times or any(value > now or now - value > MAX_GAP for value in times):
            quality = "stale"
        elif not previous:
            quality = "baseline"
        elif previous.keys() != current.keys():
            quality = "mapping_changed"
        elif not activity.observed_at or now - activity.observed_at > MAX_GAP:
            quality = "gap"
        else:
            starts = [parse_datetime(value["at"]) for value in previous.values()]
            if any(not (timedelta(0) < parse_datetime(current[key]["at"]) - parse_datetime(previous[key]["at"]) <= MAX_GAP) for key in current):
                quality = "gap"
            elif any(current[key][field] != previous[key][field] for key in current for field in ("quota", "expiry")):
                quality = "cycle_changed"
            elif any(current[key][field] < previous[key][field] for key in current for field in ("used", "up", "down")):
                quality = "counter_reset"
            else:
                delta = sum(value["used"] - previous[key]["used"] for key, value in current.items())
                start = min(starts)
        if quality != "ok":
            activity.continuous_since = max(times) if times and quality != "stale" else None
            # A different service/cycle cannot inherit the old service's activity.
            activity.last_activity_start = activity.last_activity_at = None
        if delta is not None and delta >= minimum_bytes():
            activity.last_activity_start = start
            activity.last_activity_at = max(times)
    else:
        activity.continuous_since = None
        current = {}
        activity.last_activity_start = activity.last_activity_at = None
    if quality in {"stale", "missing_counters"}:
        current = {}
        activity.continuous_since = None
        if state not in {"closed", "conflict", "superseded"}:
            state = "unknown"
    activity.observed_at = now
    activity.entitlement = state
    activity.reason = quality
    activity.counters = current
    activity.source_count = len(rows)
    activity.ended_at = (ended or activity.ended_at or now) if state in {"expired", "exhausted", "disabled", "ended"} else None
    activity.save()
    ActivityObservation.objects.create(purchase=activity, observed_at=now, interval_start=start, delta_bytes=delta, quality=quality)
    return activity


def activity_status(activity, *, now=None):
    now = now or timezone.now()
    if not activity or not activity.observed_at:
        return "unknown", "not_observed"
    if now - activity.observed_at > MAX_GAP:
        return "unknown", "stale"
    if activity.entitlement in {"conflict", "superseded", "closed"}:
        return activity.entitlement, activity.reason
    if activity.reason not in {"ok", "baseline", "gap", "counter_reset", "mapping_changed", "cycle_changed"}:
        return "unknown", activity.reason
    cutoff = now - WINDOW
    if activity.last_activity_start and activity.last_activity_start >= cutoff:
        return "active", activity.reason
    if (activity.continuous_since and activity.continuous_since <= cutoff
            and (not activity.last_activity_at or activity.last_activity_at <= cutoff)):
        return "inactive", activity.reason
    return "collecting", activity.reason


def _claim(now):
    ActivityCollector.objects.get_or_create(pk=1)
    token = uuid4().hex
    updated = ActivityCollector.objects.filter(pk=1).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now),
    ).update(token=token, lease_until=now + LEASE, heartbeat_at=now)
    return token if updated else None


def _heartbeat(token):
    now = timezone.now()
    if not ActivityCollector.objects.filter(pk=1, token=token, lease_until__gt=now).update(
        heartbeat_at=now, lease_until=now + LEASE,
    ):
        raise RuntimeError("Activity collector lease lost")


def collect_activity():
    token = _claim(timezone.now())
    if not token:
        return {"skipped": "already_running"}
    try:
        maps, orphans = load_purchase_maps()
        panel_ids = {source.panel_id for mapping in maps for source in mapping.sources if source.panel_id}
        rows, panels = [], {}
        for panel in Panel.objects.filter(pk__in=panel_ids).select_related("store").order_by("pk"):
            _heartbeat(token)
            samples, status = read_panel(panel)
            rows.extend(samples)
            panels[panel.pk] = status
        _heartbeat(token)
        resolved = resolve_maps(maps, orphans, rows)
        counts = Counter()
        for mapping in maps:
            matched, reason = resolved[mapping.order.pk]
            if reason == "unmapped_source":
                reason = next((panels[source.panel_id] for source in mapping.sources
                               if panels.get(source.panel_id, "ok") != "ok"), reason)
            with transaction.atomic():
                lock = ActivityCollector.objects.select_for_update().get(pk=1)
                now = timezone.now()
                if lock.token != token or not lock.lease_until or lock.lease_until <= now:
                    raise RuntimeError("Activity collector lease lost")
                activity = record_purchase(mapping, matched, reason, now=now)
                counts[activity.reason] += 1
            _heartbeat(token)
        summary = {"purchases": len(maps), "panels": len(panels), "panel_statuses": dict(Counter(panels.values())), "quality": dict(counts)}
        ActivityCollector.objects.filter(pk=1, token=token).update(completed_at=timezone.now(), summary=summary)
        return summary
    except Exception as exc:
        ActivityCollector.objects.filter(pk=1, token=token).update(summary={"error": type(exc).__name__})
        raise
    finally:
        ActivityCollector.objects.filter(pk=1, token=token).update(lease_until=None)


def collector_healthy(collector, *, now=None):
    now = now or timezone.now()
    return bool(collector and collector.heartbeat_at and now - collector.heartbeat_at <= MAX_GAP
                and not collector.summary.get("error")
                and (collector.completed_at or (collector.lease_until and collector.lease_until > now)))
