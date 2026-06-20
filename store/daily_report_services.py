from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q, Sum
from django.utils import timezone

from .admin_notifications import send_admin_message_to_telegram_admins
from .jalali import format_jalali_date, persian_digits
from .models import (
    BotConfiguration,
    BotEventLog,
    BroadcastMessage,
    BroadcastRecipient,
    DailyAdminReportLog,
    FreeTrialRequest,
    Order,
    Panel,
    PanelHealthCheckLog,
    PanelHealthStatus,
    Referral,
    ReferralRewardLedger,
    RevenueOfferLog,
    Store,
    VPNClientReminderLog,
)
from .panel_usage_services import format_bytes_fa, get_all_panels_usage_comparison


SUCCESSFUL_ORDER_STATUSES = (Order.Status.CONFIRMED, Order.Status.COMPLETED)
PENDING_ORDER_STATUSES = (Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION)


@dataclass(frozen=True)
class DailyReportSettings:
    store: Store | None = None
    enabled: bool = True
    report_time: time = time(9, 0)
    timezone_name: str = "Asia/Tehran"
    include_panel_health: bool = True
    include_financials: bool = True
    include_errors: bool = True

    @property
    def timezone(self):
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Tehran")


def get_daily_report_settings(store=None):
    if store is None:
        store = Store.objects.filter(is_active=True).order_by("pk").first()
    timezone_name = (getattr(store, "daily_admin_report_timezone", "") or "Asia/Tehran").strip()
    return DailyReportSettings(
        store=store,
        enabled=bool(getattr(store, "daily_admin_report_enabled", True)),
        report_time=getattr(store, "daily_admin_report_time", None) or time(9, 0),
        timezone_name=timezone_name,
        include_panel_health=bool(getattr(store, "daily_admin_report_include_panel_health", True)),
        include_financials=bool(getattr(store, "daily_admin_report_include_financials", True)),
        include_errors=bool(getattr(store, "daily_admin_report_include_errors", True)),
    )


def _parse_report_date(value, *, settings):
    if isinstance(value, datetime):
        return timezone.localtime(value, settings.timezone).date()
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return timezone.localtime(timezone.now(), settings.timezone).date() - timedelta(days=1)


def get_report_period(report_date=None, *, store=None, settings=None):
    settings = settings or get_daily_report_settings(store)
    report_date = _parse_report_date(report_date, settings=settings)
    tz = settings.timezone
    period_start = datetime.combine(report_date, time.min, tzinfo=tz)
    period_end = period_start + timedelta(days=1)
    return report_date, period_start, period_end


def _period_filter(queryset, field_name, period_start, period_end):
    return queryset.filter(**{f"{field_name}__gte": period_start, f"{field_name}__lt": period_end})


def _filter_store_orders(queryset, store):
    if store is not None:
        queryset = queryset.filter(store=store)
    return queryset


def _format_int(value):
    return persian_digits(f"{int(value or 0):,}")


def _format_money(value, currency="TOMAN"):
    labels = {
        "TOMAN": "تومان",
        "IRR": "ریال",
        "USD": "دلار",
    }
    return f"{_format_int(value)} {labels.get(currency, currency)}"


def _is_renewal_order(order):
    metadata = order.metadata or {}
    return bool(metadata.get("renewal") or metadata.get("renewal_client_pk"))


def collect_daily_sales_stats(period_start, period_end, *, store=None):
    orders = _filter_store_orders(_period_filter(Order.objects.all(), "created_at", period_start, period_end), store)
    successful = orders.filter(status__in=SUCCESSFUL_ORDER_STATUSES)
    successful_orders = list(successful.only("id", "metadata"))
    currency = (
        successful.exclude(currency="")
        .values_list("currency", flat=True)
        .order_by("currency")
        .first()
        or "TOMAN"
    )
    return {
        "successful_amount": successful.aggregate(total=Sum("amount"))["total"] or 0,
        "successful_count": len(successful_orders),
        "successful_renewals": sum(1 for order in successful_orders if _is_renewal_order(order)),
        "currency": currency,
    }


def collect_daily_order_stats(period_start, period_end, *, store=None):
    orders = _filter_store_orders(_period_filter(Order.objects.all(), "created_at", period_start, period_end), store)
    return {
        "total": orders.count(),
        "successful": orders.filter(status__in=SUCCESSFUL_ORDER_STATUSES).count(),
        "pending": orders.filter(status__in=PENDING_ORDER_STATUSES).count(),
        "rejected": orders.filter(status=Order.Status.REJECTED).count(),
        "cancelled": orders.filter(status=Order.Status.CANCELLED).count(),
    }


def collect_daily_trial_stats(period_start, period_end, *, store=None):
    requests = _period_filter(FreeTrialRequest.objects.select_related("panel"), "created_at", period_start, period_end)
    if store is not None:
        requests = requests.filter(panel__store=store)
    return {
        "total": requests.count(),
        "delivered": requests.filter(status=FreeTrialRequest.Status.DELIVERED).count(),
        "failed": requests.filter(status=FreeTrialRequest.Status.FAILED).count(),
    }


def collect_daily_referral_stats(period_start, period_end, *, store=None):
    referrals = _period_filter(Referral.objects.all(), "purchased_at", period_start, period_end)
    ledgers = _period_filter(ReferralRewardLedger.objects.select_related("order"), "created_at", period_start, period_end)
    redeemed_ledgers = _period_filter(ReferralRewardLedger.objects.select_related("order"), "redeemed_at", period_start, period_end)
    if store is not None:
        referrals = referrals.filter(first_order__store=store)
        ledgers = ledgers.filter(order__store=store)
        redeemed_ledgers = redeemed_ledgers.filter(order__store=store)
    return {
        "successful_invites": referrals.filter(status=Referral.Status.PURCHASED).count(),
        "packages_created": ledgers.count(),
        "packages_redeemed": redeemed_ledgers.filter(status=ReferralRewardLedger.Status.REDEEMED).count(),
    }


def collect_daily_reminder_stats(period_start, period_end, *, store=None):
    logs = _period_filter(VPNClientReminderLog.objects.select_related("vpn_client"), "sent_at", period_start, period_end)
    if store is not None:
        logs = logs.filter(vpn_client__store=store)
    return {
        "sent": logs.filter(status=VPNClientReminderLog.Status.SENT).count(),
        "failed": logs.filter(status=VPNClientReminderLog.Status.FAILED).count(),
        "skipped": logs.filter(status=VPNClientReminderLog.Status.SKIPPED).count(),
    }


def collect_daily_broadcast_stats(period_start, period_end, *, store=None):
    campaigns = _period_filter(BroadcastMessage.objects.all(), "sent_at", period_start, period_end)
    recipients = _period_filter(BroadcastRecipient.objects.select_related("campaign"), "sent_at", period_start, period_end)
    if store is not None:
        campaigns = campaigns.filter(store=store)
        recipients = recipients.filter(campaign__store=store)
    return {
        "campaigns_sent": campaigns.filter(status=BroadcastMessage.Status.SENT).count(),
        "recipients_sent": recipients.filter(status=BroadcastRecipient.Status.SENT).count(),
        "recipients_failed": recipients.filter(status=BroadcastRecipient.Status.FAILED).count(),
    }


def collect_daily_revenue_engine_stats(period_start, period_end, *, store=None):
    logs = _period_filter(RevenueOfferLog.objects.all(), "created_at", period_start, period_end)
    if store is not None:
        logs = logs.filter(Q(store=store) | Q(store__isnull=True))

    sent_statuses = [RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED]
    sent = logs.filter(status__in=sent_statuses).count()
    converted = logs.filter(status=RevenueOfferLog.Status.CONVERTED).count()
    per_engine = {}
    for engine_type in (
        RevenueOfferLog.EngineType.RENEWAL,
        RevenueOfferLog.EngineType.UPSELL,
        RevenueOfferLog.EngineType.RETENTION,
        RevenueOfferLog.EngineType.SILENT_ACTIVE,
    ):
        engine_logs = logs.filter(engine_type=engine_type)
        per_engine[engine_type] = {
            "sent": engine_logs.filter(status__in=sent_statuses).count(),
            "converted": engine_logs.filter(status=RevenueOfferLog.Status.CONVERTED).count(),
        }
    return {
        "enabled": bool(getattr(store, "revenue_engine_enabled", True)),
        "dry_run_enabled": bool(getattr(store, "revenue_engine_dry_run", False)),
        "sent": sent,
        "dry_run": logs.filter(status=RevenueOfferLog.Status.DRY_RUN).count(),
        "suppressed": logs.filter(status=RevenueOfferLog.Status.SUPPRESSED).count(),
        "failed": logs.filter(status=RevenueOfferLog.Status.FAILED).count(),
        "converted": converted,
        "conversion_rate": (converted / sent * 100) if sent else 0,
        "per_engine": per_engine,
    }


def collect_daily_sms_stats(period_start, period_end, *, store=None):
    try:
        from payments.models import IncomingPaymentSMS
    except Exception:
        return {"matched": 0, "no_match": 0, "confirmed": 0}

    messages = _period_filter(IncomingPaymentSMS.objects.all(), "received_at", period_start, period_end)
    if store is not None:
        messages = messages.filter(Q(matched_orders__store=store) | Q(matched_orders__isnull=True)).distinct()
    return {
        "matched": messages.filter(status__in=[IncomingPaymentSMS.Status.MATCHED, IncomingPaymentSMS.Status.CONFIRMED]).count(),
        "confirmed": messages.filter(status=IncomingPaymentSMS.Status.CONFIRMED).count(),
        "no_match": messages.filter(status=IncomingPaymentSMS.Status.NO_MATCH).count(),
    }


def collect_panel_health_summary(*, store=None):
    panels = Panel.objects.filter(is_active=True).select_related("store", "health_status").order_by("name", "pk")
    if store is not None:
        panels = panels.filter(store=store)

    rows = []
    counts = {
        PanelHealthStatus.Status.OK: 0,
        PanelHealthStatus.Status.WARNING: 0,
        PanelHealthStatus.Status.ERROR: 0,
        PanelHealthStatus.Status.DISABLED: 0,
        PanelHealthStatus.Status.UNKNOWN: 0,
    }
    for panel in panels:
        health = getattr(panel, "health_status", None)
        status = getattr(health, "status", PanelHealthStatus.Status.UNKNOWN)
        counts[status] = counts.get(status, 0) + 1
        rows.append(
            {
                "panel_id": panel.pk,
                "panel_name": panel.name,
                "status": status,
                "summary": getattr(health, "summary", ""),
                "last_checked_at": getattr(health, "last_checked_at", None),
            }
        )
    return {"counts": counts, "rows": rows, "total": len(rows)}


def collect_daily_error_stats(period_start, period_end, *, store=None):
    panel_errors = _period_filter(PanelHealthCheckLog.objects.select_related("panel"), "checked_at", period_start, period_end)
    if store is not None:
        panel_errors = panel_errors.filter(panel__store=store)

    telegram_errors = _period_filter(
        BotEventLog.objects.select_related("bot_config"),
        "created_at",
        period_start,
        period_end,
    ).filter(bot_config__provider=BotConfiguration.Provider.TELEGRAM, status=BotEventLog.Status.FAILED)
    if store is not None:
        telegram_errors = telegram_errors.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))

    sms_stats = collect_daily_sms_stats(period_start, period_end, store=store)
    return {
        "xui_failed": panel_errors.filter(status=PanelHealthStatus.Status.ERROR).count(),
        "telegram_failed": telegram_errors.count(),
        "sms_error": sms_stats["no_match"],
    }


def _usage_warning_text(row):
    warnings = list(row.get("warnings") or [])
    current_metadata = getattr(row.get("current"), "metadata", {}) or {}
    warnings.extend(current_metadata.get("warnings") or [])
    return warnings[0] if warnings else "snapshot کافی وجود ندارد."


def collect_panel_usage_report_rows(report_date, *, store=None, timezone_name=None, limit=10, save=True):
    rows = get_all_panels_usage_comparison(report_date, timezone=timezone_name, store=store, save=save)
    return {
        "rows": rows[:limit],
        "total": len(rows),
        "truncated_count": max(len(rows) - limit, 0),
    }


def build_daily_admin_report_message(report_date=None, *, store=None, persist_panel_usage=True):
    settings = get_daily_report_settings(store)
    store = settings.store
    report_date, period_start, period_end = get_report_period(report_date, settings=settings)
    store_name = getattr(store, "name", None) or "فروشگاه"
    lines = [
        f"📊 گزارش روزانه {escape(store_name)}",
        f"تاریخ: {format_jalali_date(report_date) or persian_digits(report_date.isoformat())}",
    ]

    if settings.include_financials:
        sales = collect_daily_sales_stats(period_start, period_end, store=store)
        orders = collect_daily_order_stats(period_start, period_end, store=store)
        lines.extend(
            [
                "",
                "💰 فروش:",
                f"- مبلغ سفارش‌های موفق: {_format_money(sales['successful_amount'], sales['currency'])}",
                f"- سفارش موفق: {_format_int(orders['successful'])}",
                f"- سفارش در انتظار: {_format_int(orders['pending'])}",
                f"- سفارش رد شده: {_format_int(orders['rejected'])}",
                f"- تمدید موفق: {_format_int(sales['successful_renewals'])}",
            ]
        )

    trials = collect_daily_trial_stats(period_start, period_end, store=store)
    referrals = collect_daily_referral_stats(period_start, period_end, store=store)
    lines.extend(
        [
            "",
            "🎁 رشد و جذب:",
            f"- تست رایگان: {_format_int(trials['total'])}",
            f"- دعوت موفق: {_format_int(referrals['successful_invites'])}",
            f"- بسته referral ایجادشده: {_format_int(referrals['packages_created'])}",
            f"- بسته referral دریافت‌شده: {_format_int(referrals['packages_redeemed'])}",
        ]
    )

    reminders = collect_daily_reminder_stats(period_start, period_end, store=store)
    broadcasts = collect_daily_broadcast_stats(period_start, period_end, store=store)
    sms = collect_daily_sms_stats(period_start, period_end, store=store)
    lines.extend(
        [
            "",
            "🔔 عملیات:",
            f"- reminder ارسال‌شده: {_format_int(reminders['sent'])}",
            f"- broadcast ارسال‌شده: {_format_int(broadcasts['campaigns_sent'])}",
            f"- SMS match موفق: {_format_int(sms['matched'])}",
            f"- SMS no-match: {_format_int(sms['no_match'])}",
        ]
    )

    revenue_stats = collect_daily_revenue_engine_stats(period_start, period_end, store=store)
    lines.extend(["", "💰 Revenue Engine:"])
    if not revenue_stats["enabled"]:
        lines.append("- Revenue Engine غیرفعال است.")
    else:
        if revenue_stats["dry_run_enabled"]:
            lines.append("- Revenue Engine در حالت Dry-run است.")
        revenue_conversion_rate = persian_digits(f"{revenue_stats['conversion_rate']:.0f}")
        lines.extend(
            [
                f"- پیشنهادهای ارسال‌شده: {_format_int(revenue_stats['sent'])}",
                f"- dry-run: {_format_int(revenue_stats['dry_run'])}",
                f"- suppress شده: {_format_int(revenue_stats['suppressed'])}",
                f"- conversion: {_format_int(revenue_stats['converted'])}",
                f"- نرخ تبدیل: {revenue_conversion_rate}٪",
                f"- خطاها: {_format_int(revenue_stats['failed'])}",
            ]
        )
        labels = {
            RevenueOfferLog.EngineType.RENEWAL: "Renewal",
            RevenueOfferLog.EngineType.UPSELL: "Upsell",
            RevenueOfferLog.EngineType.RETENTION: "Retention",
            RevenueOfferLog.EngineType.SILENT_ACTIVE: "Silent Active",
        }
        for engine_type, label in labels.items():
            counts = revenue_stats["per_engine"].get(engine_type, {})
            lines.append(
                f"- {label}: {_format_int(counts.get('sent', 0))} sent / "
                f"{_format_int(counts.get('converted', 0))} converted"
            )

    if settings.include_panel_health:
        panel_summary = collect_panel_health_summary(store=store)
        lines.extend(["", "🖥 وضعیت پنل‌ها:"])
        if panel_summary["rows"]:
            for row in panel_summary["rows"][:10]:
                lines.append(f"- {escape(row['panel_name'])}: {str(row['status']).upper()}")
            if len(panel_summary["rows"]) > 10:
                remaining = len(panel_summary["rows"]) - 10
                lines.append(f"- و {_format_int(remaining)} پنل دیگر")
        else:
            lines.append("- پنل فعالی ثبت نشده است: ۰")

    if store is not None and getattr(store, "panel_usage_report_enabled", True):
        usage_summary = collect_panel_usage_report_rows(
            report_date,
            store=store,
            timezone_name=settings.timezone_name,
            limit=10,
            save=persist_panel_usage,
        )
        lines.extend(["", "📈 مصرف پنل‌ها:"])
        if usage_summary["rows"]:
            for row in usage_summary["rows"]:
                panel = row["panel"]
                current = row["current"]
                previous = row["previous"]
                quality = current.data_quality
                warning_suffix = " ⚠️" if quality != "complete" else ""
                if quality == "insufficient":
                    lines.extend(
                        [
                            f"- {escape(panel.name)}:",
                            f"  مصرف دیروز: نامشخص ⚠️",
                            f"  دلیل: {escape(_usage_warning_text(row))}",
                        ]
                    )
                    continue
                lines.extend(
                    [
                        f"- {escape(panel.name)}:",
                        f"  مصرف دیروز: {format_bytes_fa(current.used_bytes)}{warning_suffix}",
                        f"  نسبت به روز قبل: {format_bytes_fa(previous.used_bytes)} {row['previous_change']}",
                        f"  نسبت به میانگین هفته: {format_bytes_fa(row['week_average_used_bytes'])} {row['week_change']}",
                        f"  کاربران فعال: {_format_int(current.active_users_count)}",
                    ]
                )
            if usage_summary["truncated_count"]:
                lines.append(f"- و {_format_int(usage_summary['truncated_count'])} پنل دیگر")
        else:
            lines.append("- پنل فعالی برای گزارش مصرف وجود ندارد.")

    if settings.include_errors:
        errors = collect_daily_error_stats(period_start, period_end, store=store)
        lines.extend(
            [
                "",
                "⚠️ خطاهای مهم:",
                f"- X-UI failed: {_format_int(errors['xui_failed'])}",
                f"- Telegram failed: {_format_int(errors['telegram_failed'])}",
                f"- SMS error: {_format_int(errors['sms_error'])}",
            ]
        )

    return "\n".join(lines)


def _stores_for_daily_report(store=None):
    if store is not None:
        return [store]
    return list(Store.objects.filter(is_active=True, daily_admin_report_enabled=True).order_by("pk"))


def _record_report_result(log, *, status, sent_to_count=0, message_text="", error_message="", metadata=None):
    log.status = status
    log.sent_to_count = sent_to_count
    log.message_text = message_text
    log.error_message = error_message
    log.metadata = metadata or {}
    if status == DailyAdminReportLog.Status.SENT:
        log.sent_at = timezone.now()
    log.save()
    return log


def send_daily_admin_report(report_date=None, dry_run=False, force=False, store=None):
    summary = {
        "total_stores": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "would_send": 0,
        "dry_run": bool(dry_run),
        "force": bool(force),
        "reports": [],
    }

    for current_store in _stores_for_daily_report(store):
        settings = get_daily_report_settings(current_store)
        summary["total_stores"] += 1
        if not settings.enabled:
            summary["skipped"] += 1
            summary["reports"].append({"store_id": current_store.pk, "status": "skipped", "reason": "disabled"})
            continue

        current_report_date, period_start, period_end = get_report_period(report_date, settings=settings)
        existing_sent = DailyAdminReportLog.objects.filter(
            store=current_store,
            report_date=current_report_date,
            status=DailyAdminReportLog.Status.SENT,
        ).first()
        if existing_sent and not force:
            summary["skipped"] += 1
            summary["reports"].append(
                {
                    "store_id": current_store.pk,
                    "report_date": current_report_date.isoformat(),
                    "status": "skipped",
                    "reason": "duplicate",
                    "log_id": existing_sent.pk,
                }
            )
            continue

        message = build_daily_admin_report_message(
            current_report_date,
            store=current_store,
            persist_panel_usage=not dry_run,
        )
        if dry_run:
            summary["would_send"] += 1
            summary["reports"].append(
                {
                    "store_id": current_store.pk,
                    "report_date": current_report_date.isoformat(),
                    "status": "would_send",
                    "message": message,
                }
            )
            continue

        log, _created = DailyAdminReportLog.objects.get_or_create(
            store=current_store,
            report_date=current_report_date,
            defaults={
                "period_start": period_start,
                "period_end": period_end,
                "status": DailyAdminReportLog.Status.SKIPPED,
            },
        )
        metadata = dict(log.metadata or {})
        if force:
            forced_runs = list(metadata.get("forced_runs") or [])
            forced_runs.append(timezone.now().isoformat())
            metadata["forced_runs"] = forced_runs
            metadata["force"] = True
        metadata["period_start"] = period_start.isoformat()
        metadata["period_end"] = period_end.isoformat()

        try:
            delivery = send_admin_message_to_telegram_admins(
                current_store,
                text=message,
                event_type=BotEventLog.EventType.SALES_REPORT,
            )
        except Exception as exc:
            error = str(exc)
            metadata["delivery_error"] = error
            _record_report_result(
                log,
                status=DailyAdminReportLog.Status.FAILED,
                sent_to_count=0,
                message_text=message,
                error_message=error,
                metadata=metadata,
            )
            summary["failed"] += 1
            summary["reports"].append(
                {
                    "store_id": current_store.pk,
                    "report_date": current_report_date.isoformat(),
                    "status": "failed",
                    "error": error,
                    "log_id": log.pk,
                }
            )
            continue

        metadata["delivery"] = delivery
        sent_count = int(delivery.get("sent") or 0)
        failed_count = int(delivery.get("failed") or 0)
        status = DailyAdminReportLog.Status.SENT if sent_count else DailyAdminReportLog.Status.FAILED
        error_message = "" if sent_count else "No Telegram admin report message was delivered."
        if sent_count and failed_count:
            metadata["partial_failure"] = True

        _record_report_result(
            log,
            status=status,
            sent_to_count=sent_count,
            message_text=message,
            error_message=error_message,
            metadata=metadata,
        )
        if sent_count:
            summary["sent"] += 1
        else:
            summary["failed"] += 1
        summary["reports"].append(
            {
                "store_id": current_store.pk,
                "report_date": current_report_date.isoformat(),
                "status": status,
                "sent": sent_count,
                "failed": failed_count,
                "log_id": log.pk,
            }
        )

    return summary
