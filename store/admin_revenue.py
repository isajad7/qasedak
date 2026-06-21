from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from .admin_support_services import customer_message_url, support_workbench_url
from .models import BotConfiguration, Customer, Order, RevenueOfferLog, Store


REAL_SEND_CONFIRMATION = "ENABLE_REAL_REVENUE_SEND"
RECENT_LOG_LIMIT = 20
SAFE_TOTAL_DAILY_CAP = 100
MAX_RECENT_FAILURES_FOR_REAL_SEND = 5
MAX_RECENT_FAILURE_RATE = Decimal("0.20")
MIN_TARGET_COVERAGE = Decimal("0.50")
MIN_TARGET_SAMPLE_SIZE = 5

SAFE_REVENUE_DEFAULTS = {
    "revenue_engine_enabled": True,
    "revenue_engine_dry_run": True,
    "renewal_engine_enabled": True,
    "upsell_engine_enabled": True,
    "retention_engine_enabled": True,
    "ai_revenue_optimizer_enabled": True,
    "revenue_optimization_enabled": True,
    "revenue_max_offers_per_user_per_day": 1,
    "revenue_max_offers_per_user_per_week": 3,
    "revenue_max_total_offers_per_day": SAFE_TOTAL_DAILY_CAP,
    "revenue_offer_cooldown_hours": 24,
    "retention_offer_cooldown_hours": 72,
    "revenue_min_ai_confidence": Decimal("0.50"),
}


def add_query(url, params):
    cleaned = {key: value for key, value in params.items() if value not in ("", None)}
    if not cleaned:
        return url
    return f"{url}?{urlencode(cleaned)}"


def get_store_options(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def revenue_control_url(store=None):
    return add_query(reverse("admin_store_revenue_control"), {"store": getattr(store, "pk", None)})


def revenue_logs_url(store=None, params=None):
    merged = dict(params or {})
    if store and store.pk:
        merged.setdefault("store__id__exact", store.pk)
    return add_query(reverse("admin:store_revenueofferlog_changelist"), merged)


def store_admin_url(store=None):
    if store and store.pk:
        return reverse("admin:store_store_change", args=[store.pk])
    return reverse("admin:store_store_add")


def _logs_for_period(store=None, days=7):
    since = timezone.now() - timedelta(days=days)
    queryset = RevenueOfferLog.objects.filter(created_at__gte=since)
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset


def _choice_label(choices, key):
    labels = {value: str(label) for value, label in choices}
    return labels.get(key, key or "-")


def _status_counts(queryset):
    return {row["status"]: int(row["count"] or 0) for row in queryset.values("status").annotate(count=Count("id"))}


def get_revenue_metrics(store=None, days=7):
    logs = _logs_for_period(store, days=days)
    counts = _status_counts(logs)
    converted = counts.get(RevenueOfferLog.Status.CONVERTED, 0)
    sent_or_converted = counts.get(RevenueOfferLog.Status.SENT, 0) + converted
    skipped_or_suppressed = counts.get(RevenueOfferLog.Status.SKIPPED, 0) + counts.get(
        RevenueOfferLog.Status.SUPPRESSED, 0
    )
    conversion_rate = Decimal("0")
    if sent_or_converted:
        conversion_rate = (Decimal(converted) / Decimal(sent_or_converted) * Decimal("100")).quantize(Decimal("0.1"))
    total = sum(counts.values())
    return {
        "days": days,
        "total": total,
        "dry_run": counts.get(RevenueOfferLog.Status.DRY_RUN, 0),
        "sent": sent_or_converted,
        "failed": counts.get(RevenueOfferLog.Status.FAILED, 0),
        "skipped_suppressed": skipped_or_suppressed,
        "conversions": converted,
        "conversion_rate": conversion_rate,
        "status_counts": counts,
    }


def _breakdown_rows(queryset, field_name, choices):
    rows = queryset.values(field_name, "status").annotate(count=Count("id")).order_by(field_name)
    grouped = {}
    for row in rows:
        key = row[field_name] or ""
        grouped.setdefault(
            key,
            {
                "key": key,
                "label": _choice_label(choices, key),
                "total": 0,
                "dry_run": 0,
                "sent": 0,
                "failed": 0,
                "skipped_suppressed": 0,
                "conversions": 0,
            },
        )
        count = int(row["count"] or 0)
        status = row["status"]
        grouped[key]["total"] += count
        if status == RevenueOfferLog.Status.DRY_RUN:
            grouped[key]["dry_run"] += count
        elif status == RevenueOfferLog.Status.FAILED:
            grouped[key]["failed"] += count
        elif status == RevenueOfferLog.Status.CONVERTED:
            grouped[key]["sent"] += count
            grouped[key]["conversions"] += count
        elif status == RevenueOfferLog.Status.SENT:
            grouped[key]["sent"] += count
        elif status in {RevenueOfferLog.Status.SKIPPED, RevenueOfferLog.Status.SUPPRESSED}:
            grouped[key]["skipped_suppressed"] += count
    return sorted(grouped.values(), key=lambda item: (-item["total"], item["label"]))


def get_revenue_engine_breakdown(store=None, days=7):
    logs = _logs_for_period(store, days=days)
    return _breakdown_rows(logs, "engine_type", RevenueOfferLog.EngineType.choices)


def get_revenue_decision_source_breakdown(store=None, days=7):
    logs = _logs_for_period(store, days=days)
    return _breakdown_rows(logs, "decision_source", RevenueOfferLog.DecisionSource.choices)


def _status_tone(status):
    return {
        RevenueOfferLog.Status.DRY_RUN: "safe",
        RevenueOfferLog.Status.SENT: "success",
        RevenueOfferLog.Status.CONVERTED: "success",
        RevenueOfferLog.Status.FAILED: "danger",
        RevenueOfferLog.Status.SKIPPED: "skipped",
        RevenueOfferLog.Status.SUPPRESSED: "warning",
    }.get(status, "secondary")


def _target_label(log):
    if log.customer_id:
        return f"Customer #{log.customer_id}"
    if log.bot_user_id:
        return f"BotUser #{log.bot_user_id}"
    if log.vpn_client_id:
        return f"VPNClient #{log.vpn_client_id}"
    return "-"


def get_recent_revenue_events(store=None, limit=RECENT_LOG_LIMIT):
    queryset = RevenueOfferLog.objects.order_by("-created_at", "-pk")
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    queryset = queryset.only(
        "id",
        "customer_id",
        "bot_user_id",
        "vpn_client_id",
        "engine_type",
        "event_type",
        "decision_source",
        "status",
        "skip_reason",
        "created_at",
    )
    rows = []
    for log in queryset[:limit]:
        customer_url = reverse("admin_store_customer_review", args=[log.customer_id]) if log.customer_id else ""
        message_url = customer_message_url(Customer(pk=log.customer_id)) if log.customer_id else ""
        rows.append(
            {
                "id": log.pk,
                "status": log.status,
                "status_label": log.get_status_display(),
                "status_tone": _status_tone(log.status),
                "engine_type": log.engine_type,
                "engine_label": log.get_engine_type_display(),
                "event_type": log.event_type,
                "target_label": _target_label(log),
                "decision_source": log.decision_source,
                "decision_source_label": log.get_decision_source_display(),
                "skip_reason": log.skip_reason,
                "customer_url": customer_url,
                "message_url": message_url,
                "created_at": log.created_at,
            }
        )
    return rows


def get_engine_switches(store=None):
    fields = (
        ("renewal_engine_enabled", "Renewal", "یادآوری تمدید و پیشنهاد تمدید سرویس‌های نزدیک انقضا."),
        ("upsell_engine_enabled", "Upsell", "پیشنهاد ارتقا یا خرید حجم/مدت بیشتر بعد از سیگنال خرید."),
        ("retention_engine_enabled", "Retention", "بازگردانی مشتری‌های غیرفعال یا از دست رفته."),
        ("ai_revenue_optimizer_enabled", "AI optimizer", "امتیازدهی و انتخاب پیشنهاد با کمک AI داخلی/قواعد fallback."),
        ("revenue_optimization_enabled", "Optimization / A-B", "انتخاب variant و آزمایش کنترل‌شده پیشنهادها."),
    )
    advanced_url = store_admin_url(store)
    return [
        {
            "field": field,
            "label": label,
            "description": description,
            "enabled": bool(store and getattr(store, field, False)),
            "advanced_url": advanced_url,
        }
        for field, label, description in fields
    ]


def get_safety_limits(store=None):
    if not store:
        return []
    return [
        {
            "label": "max offers per user per day",
            "value": store.revenue_max_offers_per_user_per_day,
            "safe": store.revenue_max_offers_per_user_per_day <= 1,
        },
        {
            "label": "max offers per user per week",
            "value": store.revenue_max_offers_per_user_per_week,
            "safe": store.revenue_max_offers_per_user_per_week <= 3,
        },
        {
            "label": "total daily cap",
            "value": store.revenue_max_total_offers_per_day,
            "safe": store.revenue_max_total_offers_per_day <= SAFE_TOTAL_DAILY_CAP,
        },
        {
            "label": "cooldown",
            "value": f"{store.revenue_offer_cooldown_hours}h",
            "safe": store.revenue_offer_cooldown_hours >= 24,
        },
        {
            "label": "retention cooldown",
            "value": f"{store.retention_offer_cooldown_hours}h",
            "safe": store.retention_offer_cooldown_hours >= 72,
        },
        {
            "label": "min AI confidence",
            "value": f"{store.revenue_min_ai_confidence:.2f}",
            "safe": store.revenue_min_ai_confidence >= Decimal("0.50"),
        },
    ]


def _active_telegram_bot_count(store=None):
    queryset = BotConfiguration.objects.filter(
        provider=BotConfiguration.Provider.TELEGRAM,
        is_active=True,
    ).exclude(bot_token="")
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.count()


def _recent_customer_ids_for_target_coverage(store=None):
    recent_logs = _logs_for_period(store, days=7).exclude(customer_id__isnull=True)
    ids = list(recent_logs.values_list("customer_id", flat=True).distinct()[:500])
    if ids:
        return ids

    since = timezone.now() - timedelta(days=30)
    orders = Order.objects.filter(
        created_at__gte=since,
        status=Order.Status.COMPLETED,
        customer_id__isnull=False,
    )
    if store and store.pk:
        orders = orders.filter(store=store)
    return list(orders.values_list("customer_id", flat=True).distinct()[:500])


def get_telegram_target_coverage(store=None):
    customer_ids = _recent_customer_ids_for_target_coverage(store)
    total = len(customer_ids)
    if not total:
        return {
            "known": False,
            "total": 0,
            "with_target": 0,
            "coverage": None,
            "coverage_percent": None,
            "label": "داده کافی برای محاسبه coverage نیست",
        }
    with_target = (
        Customer.objects.filter(pk__in=customer_ids)
        .filter(
            bot_users__is_active=True,
            bot_users__bot_config__is_active=True,
            bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .exclude(bot_users__chat_id="")
        .exclude(bot_users__bot_config__bot_token="")
    )
    if store and store.pk:
        with_target = with_target.filter(Q(bot_users__bot_config__store=store) | Q(bot_users__bot_config__store__isnull=True))
    with_target_count = with_target.distinct().count()
    coverage = (Decimal(with_target_count) / Decimal(total)).quantize(Decimal("0.01"))
    return {
        "known": True,
        "total": total,
        "with_target": with_target_count,
        "coverage": coverage,
        "coverage_percent": int(coverage * 100),
        "label": f"{with_target_count:,}/{total:,} مشتری recent target تلگرام دارند",
    }


def get_real_send_safety(store=None):
    blocking = []
    warnings = []
    if not store:
        blocking.append("هیچ Store فعالی برای Revenue Engine انتخاب نشده است.")
        return {
            "safe": False,
            "blocking": blocking,
            "warnings": warnings,
            "target_coverage": get_telegram_target_coverage(store),
        }

    try:
        store.full_clean()
    except ValidationError as exc:
        blocking.append(f"تنظیمات Store معتبر نیست: {exc.messages[0] if exc.messages else exc}")

    if _active_telegram_bot_count(store) == 0:
        blocking.append("BotConfiguration تلگرام فعال با token ذخیره‌شده پیدا نشد.")

    recent = get_revenue_metrics(store=store, days=7)
    recent_total = recent["total"]
    recent_failures = recent["failed"]
    if recent_failures > MAX_RECENT_FAILURES_FOR_REAL_SEND:
        blocking.append(f"تعداد failهای ۷ روز اخیر زیاد است: {recent_failures:,}.")
    if recent_total >= 5:
        failure_rate = Decimal(recent_failures) / Decimal(recent_total)
        if failure_rate > MAX_RECENT_FAILURE_RATE:
            blocking.append("نرخ fail در ۷ روز اخیر بالاتر از حد امن است.")

    no_target_count = _logs_for_period(store, days=7).filter(skip_reason="no_personal_telegram_target").count()
    if no_target_count >= MIN_TARGET_SAMPLE_SIZE:
        blocking.append(f"{no_target_count:,} لاگ اخیر target تلگرام نداشته‌اند.")
    elif no_target_count:
        warnings.append(f"{no_target_count:,} لاگ اخیر target تلگرام نداشته‌اند.")

    target_coverage = get_telegram_target_coverage(store)
    if target_coverage["known"] and target_coverage["total"] >= MIN_TARGET_SAMPLE_SIZE:
        if target_coverage["coverage"] < MIN_TARGET_COVERAGE:
            blocking.append("Telegram target coverage برای مشتری‌های recent پایین است.")
    elif not target_coverage["known"]:
        warnings.append("برای محاسبه Telegram target coverage هنوز داده کافی وجود ندارد.")

    if store.revenue_max_offers_per_user_per_day > 1:
        blocking.append("max offers per user per day باید ۱ یا کمتر باشد.")
    if store.revenue_max_offers_per_user_per_week > 3:
        blocking.append("max offers per user per week باید ۳ یا کمتر باشد.")
    if store.revenue_max_total_offers_per_day > SAFE_TOTAL_DAILY_CAP:
        blocking.append(f"total daily cap باید {SAFE_TOTAL_DAILY_CAP:,} یا کمتر باشد.")
    if store.revenue_offer_cooldown_hours < 24:
        blocking.append("cooldown عمومی باید حداقل ۲۴ ساعت باشد.")
    if store.retention_offer_cooldown_hours < 72:
        blocking.append("retention cooldown باید حداقل ۷۲ ساعت باشد.")
    if store.revenue_min_ai_confidence < Decimal("0.50"):
        blocking.append("min AI confidence باید حداقل 0.50 باشد.")

    return {
        "safe": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "target_coverage": target_coverage,
    }


def is_real_send_safe_to_enable(store):
    return get_real_send_safety(store).get("safe", False)


def get_revenue_action_items(store=None, metrics=None, safety=None):
    metrics = metrics or get_revenue_metrics(store=store, days=7)
    safety = safety or get_real_send_safety(store)
    items = []
    if metrics["failed"]:
        items.append(
            {
                "title": "failed logs نیاز به بررسی دارند",
                "description": f"{metrics['failed']:,} fail در ۷ روز اخیر ثبت شده است.",
                "tone": "danger",
                "url": revenue_logs_url(store, {"status__exact": RevenueOfferLog.Status.FAILED}),
            }
        )
    no_target_count = _logs_for_period(store, days=7).filter(skip_reason="no_personal_telegram_target").count()
    if no_target_count:
        items.append(
            {
                "title": "بعضی پیشنهادها target تلگرام ندارند",
                "description": f"{no_target_count:,} مورد recent به دلیل نبود target تلگرام skip شده‌اند.",
                "tone": "warning",
                "url": support_workbench_url("problematic", store),
            }
        )
    if metrics["dry_run"] >= 10 and metrics["sent"] == 0:
        items.append(
            {
                "title": "Dry-run زیاد است ولی sent صفر مانده",
                "description": "گزارش dry-run را بررسی کن و فقط بعد از رفع warningها real-send را فعال کن.",
                "tone": "warning",
                "command": "python manage.py revenue_report --days 7 --verbose",
            }
        )
    if store and store.revenue_engine_enabled and not store.revenue_engine_dry_run:
        items.append(
            {
                "title": "Real-send فعال است",
                "description": "ارسال واقعی Revenue Engine روشن است؛ caps و failها را روزانه بررسی کن.",
                "tone": "danger",
                "url": revenue_logs_url(store),
            }
        )
    for issue in safety["blocking"]:
        items.append(
            {
                "title": "real-send فعلاً امن نیست",
                "description": issue,
                "tone": "danger",
                "url": revenue_control_url(store),
            }
        )
    if store and store.revenue_engine_enabled and metrics["total"] == 0:
        items.append(
            {
                "title": "RevenueOfferLog اخیر وجود ندارد",
                "description": "اگر dry-run timer را نصب کرده‌ای، command و timer status را بررسی کن.",
                "tone": "warning",
                "command": "python manage.py run_revenue_scan --dry-run --engine all --limit 100 --verbose",
            }
        )
    return items


def apply_revenue_safe_defaults(store):
    for field, value in SAFE_REVENUE_DEFAULTS.items():
        setattr(store, field, value)
    store.save(update_fields=list(SAFE_REVENUE_DEFAULTS.keys()))
    return store


def set_revenue_mode(store, enabled, dry_run):
    store.revenue_engine_enabled = bool(enabled)
    store.revenue_engine_dry_run = bool(dry_run)
    store.save(update_fields=["revenue_engine_enabled", "revenue_engine_dry_run"])
    return store


def get_revenue_status(store=None):
    if not store:
        return {
            "label": "Store ندارد",
            "mode": "unknown",
            "tone": "missing",
            "description": "ابتدا Store بساز یا انتخاب کن.",
        }
    if not store.revenue_engine_enabled:
        return {
            "label": "خاموش",
            "mode": "disabled",
            "tone": "warning",
            "description": "Revenue Engine غیرفعال است.",
        }
    if store.revenue_engine_dry_run:
        return {
            "label": "امن: Dry-run",
            "mode": "dry-run",
            "tone": "safe",
            "description": "پیشنهادها log می‌شوند، اما پیام واقعی ارسال نمی‌شود.",
        }
    return {
        "label": "فعال واقعی: Real Send Active",
        "mode": "real-send",
        "tone": "danger",
        "description": "ارسال واقعی فعال است؛ guardrailها و failها را نزدیک بررسی کن.",
    }


def get_command_hints():
    return [
        {
            "label": "run dry-run revenue scan",
            "command": "python manage.py run_revenue_scan --dry-run --engine all --limit 100 --verbose",
        },
        {
            "label": "revenue report",
            "command": "python manage.py revenue_report --days 7 --verbose",
        },
        {
            "label": "doctor",
            "command": "scripts/doctor.sh --live-bot --live-xui",
        },
    ]


def get_revenue_control_context(store=None, days=7):
    metrics = get_revenue_metrics(store=store, days=days)
    safety = get_real_send_safety(store)
    return {
        "status": get_revenue_status(store),
        "metrics": metrics,
        "engine_switches": get_engine_switches(store),
        "safety_limits": get_safety_limits(store),
        "engine_breakdown": get_revenue_engine_breakdown(store, days=days),
        "decision_source_breakdown": get_revenue_decision_source_breakdown(store, days=days),
        "recent_events": get_recent_revenue_events(store),
        "action_items": get_revenue_action_items(store, metrics=metrics, safety=safety),
        "safety": safety,
        "commands": get_command_hints(),
        "logs_url": revenue_logs_url(store),
        "store_admin_url": store_admin_url(store),
        "real_send_confirmation": REAL_SEND_CONFIRMATION,
    }
