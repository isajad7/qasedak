import re
import time
from dataclasses import dataclass
from datetime import timedelta
from html import escape

import requests
from django.db import transaction
from django.utils import timezone

from .admin_notifications import send_admin_message_to_telegram_admins
from .jalali import format_jalali_datetime, persian_digits
from .models import BotEventLog, Inbound, Panel, PanelHealthCheckLog, PanelHealthStatus, Store
from .telegram_bot.redaction import sanitize_bot_event_log_value
from .xui_api import XUIError, XUIService


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
PROBLEM_STATUSES = {
    PanelHealthStatus.Status.WARNING,
    PanelHealthStatus.Status.ERROR,
}


@dataclass(frozen=True)
class PanelMonitorSettings:
    store: Store | None = None
    enabled: bool = True
    alerts_enabled: bool = True
    timeout_seconds: int = 15
    alert_cooldown_minutes: int = 30
    max_log_age_days: int = 30


def _positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def get_panel_monitor_settings(store=None):
    if store is None:
        store = Store.objects.filter(is_active=True).order_by("pk").first()
    return PanelMonitorSettings(
        store=store,
        enabled=bool(getattr(store, "panel_monitor_enabled", True)),
        alerts_enabled=bool(getattr(store, "panel_monitor_alerts_enabled", True)),
        timeout_seconds=_positive_int(getattr(store, "panel_monitor_check_timeout_seconds", None), 15),
        alert_cooldown_minutes=_positive_int(getattr(store, "panel_monitor_alert_cooldown_minutes", None), 30),
        max_log_age_days=_positive_int(getattr(store, "panel_monitor_max_log_age_days", None), 30),
    )


def get_panels_for_health_check(panel_id=None, limit=None):
    panels = Panel.objects.select_related("store").prefetch_related("inbounds").order_by("pk")
    if panel_id:
        panels = panels.filter(pk=panel_id)
    if limit:
        panels = panels[: max(int(limit), 0)]
    return panels


def sanitize_operational_text(value, *, panel=None, max_length=500):
    text = str(sanitize_bot_event_log_value(value or "") or "")
    for secret in (
        getattr(panel, "password", "") if panel else "",
        getattr(panel, "username", "") if panel else "",
        getattr(panel, "url", "") if panel else "",
        getattr(panel, "proxy_url", "") if panel else "",
    ):
        secret = str(secret or "").strip()
        if secret and len(secret) >= 3:
            text = text.replace(secret, "<redacted>")
    text = URL_RE.sub("<url-redacted>", text)
    if len(text) > max_length:
        text = f"{text[:max_length - 1]}..."
    return text


def _base_result(panel, settings, *, status, summary, login_ok=None, error_code="", error_message="", metadata=None):
    now = timezone.now()
    return {
        "panel_id": panel.pk,
        "panel_name": panel.name,
        "status": status,
        "checked_at": now,
        "response_time_ms": None,
        "login_ok": login_ok,
        "inbounds_checked": 0,
        "inbounds_ok": 0,
        "inbounds_warning": 0,
        "inbounds_error": 0,
        "error_code": error_code,
        "error_message": sanitize_operational_text(error_message, panel=panel),
        "summary": sanitize_operational_text(summary, panel=panel, max_length=1000),
        "metadata": metadata or {},
        "alert_sent": False,
        "alert_sent_count": 0,
        "alert_failed_count": 0,
        "alert_skipped": False,
        "alert_skip_reason": "",
        "dry_run": False,
        "settings": settings,
    }


def _classify_exception(exc):
    if isinstance(exc, requests.Timeout):
        return "timeout", "عدم پاسخ پنل در زمان مجاز"
    if isinstance(exc, requests.ConnectionError):
        return "connection_error", "اتصال به پنل برقرار نشد"
    if isinstance(exc, requests.RequestException):
        return "connection_error", "خطای ارتباط با پنل"
    if isinstance(exc, XUIError):
        message = str(exc).lower()
        if "login" in message or "rejected" in message or "auth" in message:
            return "auth_failed", "ورود به پنل ناموفق بود"
        if "json" in message or "invalid" in message:
            return "malformed_response", "پاسخ پنل قابل خواندن نبود"
        if "not found" in message:
            return "inbound_not_found", "اینباند در پنل پیدا نشد"
        return "xui_api_error", "خطای API پنل"
    return "unexpected_error", "خطای غیرمنتظره هنگام بررسی پنل"


def _xui_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _inbound_issue(inbound, code, message, *, expected="", actual=""):
    return {
        "inbound_id": inbound.inbound_id,
        "remark": inbound.remark or "",
        "code": code,
        "message": message,
        "expected": str(expected or ""),
        "actual": str(actual or ""),
    }


def _ignored_inbound_metadata(ignored_inbounds):
    return {
        "ignored_inbounds": len(ignored_inbounds),
        "ignored_inbound_ids": [inbound.inbound_id for inbound in ignored_inbounds],
        "ignored_inbound_pks": [inbound.pk for inbound in ignored_inbounds],
    }


def _ignored_summary_suffix(ignored_count):
    if not ignored_count:
        return ""
    return f"؛ {persian_digits(ignored_count)} اینباند legacy/ignored نادیده گرفته شد"


def _check_remote_inbound(service, inbound):
    inbound_data = service.get_inbound(inbound.inbound_id, use_cache=False)
    issues = []
    remote_id = inbound_data.get("id")
    if remote_id is not None:
        try:
            remote_id_int = int(remote_id)
        except (TypeError, ValueError):
            remote_id_int = None
        if remote_id_int is not None and remote_id_int != inbound.inbound_id:
            issues.append(
                _inbound_issue(
                    inbound,
                    "inbound_id_mismatch",
                    "شناسه اینباند با مقدار پنل سازگار نیست",
                    expected=inbound.inbound_id,
                    actual=remote_id,
                )
            )

    remote_protocol = str(inbound_data.get("protocol") or "").strip().lower()
    if remote_protocol and remote_protocol != str(inbound.protocol or "").lower():
        issues.append(
            _inbound_issue(
                inbound,
                "protocol_mismatch",
                "پروتکل اینباند با پنل سازگار نیست",
                expected=inbound.protocol,
                actual=remote_protocol,
            )
        )

    remote_remark = str(inbound_data.get("remark") or "").strip()
    local_remark = str(inbound.remark or "").strip()
    if local_remark and remote_remark and local_remark != remote_remark:
        issues.append(
            _inbound_issue(
                inbound,
                "remark_mismatch",
                "remark اینباند با پنل سازگار نیست",
                expected=local_remark,
                actual=remote_remark,
            )
        )

    if not _xui_bool(inbound_data.get("enable"), default=True):
        issues.append(_inbound_issue(inbound, "remote_inbound_disabled", "اینباند در پنل غیرفعال است"))

    return inbound_data, issues


def build_panel_health_result(panel, *, settings=None):
    settings = settings or get_panel_monitor_settings(getattr(panel, "store", None))
    start = time.monotonic()

    if not getattr(panel, "is_active", False):
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.DISABLED,
            summary="پنل غیرفعال است و بررسی نشد.",
            metadata={"reason": "panel_disabled"},
        )
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    if not settings.enabled:
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.DISABLED,
            summary="مانیتورینگ سلامت پنل برای این فروشگاه غیرفعال است.",
            metadata={"reason": "monitor_disabled"},
        )
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    service = XUIService(panel, timeout_seconds=settings.timeout_seconds)
    try:
        service.login()
    except Exception as exc:
        error_code, friendly_message = _classify_exception(exc)
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.ERROR,
            summary=friendly_message,
            login_ok=False,
            error_code=error_code,
            error_message=friendly_message,
            metadata={"exception": sanitize_operational_text(exc, panel=panel)},
        )
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    all_active_inbounds = list(
        Inbound.objects.filter(panel=panel, is_active=True).order_by("inbound_id")
    )
    ignored_inbounds = [inbound for inbound in all_active_inbounds if not inbound.health_monitor_enabled]
    active_inbounds = [inbound for inbound in all_active_inbounds if inbound.health_monitor_enabled]
    ignored_metadata = _ignored_inbound_metadata(ignored_inbounds)

    if not all_active_inbounds:
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.WARNING,
            summary="ورود به پنل موفق بود اما هیچ اینباند فعال محلی برای بررسی وجود ندارد.",
            login_ok=True,
            error_code="no_active_inbounds",
            error_message="هیچ اینباند فعال محلی پیدا نشد.",
            metadata={"inbound_issues": [], "inbound_issue_count": 0, **ignored_metadata},
        )
        result["inbounds_warning"] = 1
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    if not active_inbounds:
        ignored_count = len(ignored_inbounds)
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.OK,
            summary=f"ورود به پنل موفق بود؛ {persian_digits(ignored_count)} اینباند legacy/ignored نادیده گرفته شد.",
            login_ok=True,
            metadata={"inbound_issues": [], "inbound_issue_count": 0, **ignored_metadata},
        )
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    warnings = []
    errors = []
    ok_count = 0
    for inbound in active_inbounds:
        try:
            _inbound_data, issues = _check_remote_inbound(service, inbound)
        except Exception as exc:
            error_code, friendly_message = _classify_exception(exc)
            issue = _inbound_issue(
                inbound,
                error_code,
                friendly_message,
                expected=inbound.inbound_id,
                actual="",
            )
            issue["exception"] = sanitize_operational_text(exc, panel=panel)
            errors.append(issue)
            continue

        if issues:
            warnings.extend(issues)
        else:
            ok_count += 1

    issue_count = len(warnings) + len(errors)
    if issue_count:
        status = PanelHealthStatus.Status.WARNING
        summary = (
            f"ورود به پنل موفق بود؛ {persian_digits(issue_count)} مشکل در اینباندها دیده شد"
            f"{_ignored_summary_suffix(len(ignored_inbounds))}."
        )
    else:
        status = PanelHealthStatus.Status.OK
        summary = (
            f"پنل سالم است و {persian_digits(ok_count)} اینباند فعال بررسی شد"
            f"{_ignored_summary_suffix(len(ignored_inbounds))}."
        )

    result = _base_result(
        panel,
        settings,
        status=status,
        summary=summary,
        login_ok=True,
        error_code="inbound_warning" if warnings else "inbound_error" if errors else "",
        error_message=warnings[0]["message"] if warnings else errors[0]["message"] if errors else "",
        metadata={
            "inbound_issues": [*warnings, *errors][:20],
            "inbound_issue_count": issue_count,
            **ignored_metadata,
        },
    )
    result["inbounds_checked"] = len(active_inbounds)
    result["inbounds_ok"] = ok_count
    result["inbounds_warning"] = len(warnings)
    result["inbounds_error"] = len(errors)
    result["response_time_ms"] = int((time.monotonic() - start) * 1000)
    return result


def update_panel_health_status(panel, result):
    checked_at = result.get("checked_at") or timezone.now()
    status = result["status"]
    metadata = sanitize_bot_event_log_value(result.get("metadata") or {})
    with transaction.atomic():
        health_status, _created = PanelHealthStatus.objects.select_for_update().get_or_create(panel=panel)
        health_status.status = status
        health_status.last_checked_at = checked_at
        health_status.response_time_ms = result.get("response_time_ms")
        health_status.error_code = result.get("error_code") or ""
        health_status.error_message = result.get("error_message") or ""
        health_status.summary = result.get("summary") or ""
        health_status.metadata = metadata

        if status == PanelHealthStatus.Status.OK:
            health_status.last_ok_at = checked_at
            health_status.consecutive_successes += 1
            health_status.consecutive_failures = 0
        elif status in PROBLEM_STATUSES:
            health_status.last_error_at = checked_at
            health_status.consecutive_failures += 1
            health_status.consecutive_successes = 0
        elif status == PanelHealthStatus.Status.DISABLED:
            health_status.consecutive_failures = 0
            health_status.consecutive_successes = 0

        health_status.save()

        log = PanelHealthCheckLog.objects.create(
            panel=panel,
            status=status,
            checked_at=checked_at,
            response_time_ms=result.get("response_time_ms"),
            login_ok=result.get("login_ok"),
            inbounds_checked=result.get("inbounds_checked") or 0,
            inbounds_ok=result.get("inbounds_ok") or 0,
            inbounds_warning=result.get("inbounds_warning") or 0,
            inbounds_error=result.get("inbounds_error") or 0,
            error_code=result.get("error_code") or "",
            error_message=result.get("error_message") or "",
            metadata=metadata,
        )
    return health_status, log


def should_send_panel_alert(previous_status, new_status, settings, *, status_obj=None, force=False, now=None):
    if not settings.alerts_enabled:
        return ""
    now = now or timezone.now()
    previous_status = previous_status or PanelHealthStatus.Status.UNKNOWN

    if new_status == PanelHealthStatus.Status.OK and previous_status in PROBLEM_STATUSES:
        return "recovery"

    if new_status in PROBLEM_STATUSES:
        if force:
            return "problem"
        if previous_status in {
            PanelHealthStatus.Status.UNKNOWN,
            PanelHealthStatus.Status.OK,
            PanelHealthStatus.Status.DISABLED,
        }:
            return "problem"
        if previous_status != new_status and new_status == PanelHealthStatus.Status.ERROR:
            return "problem"

        last_alert = getattr(status_obj, "last_alert_sent_at", None)
        if not last_alert:
            return "problem"
        cooldown = timedelta(minutes=settings.alert_cooldown_minutes)
        if last_alert + cooldown <= now:
            return "problem"
    return ""


def format_panel_health_alert_message(panel, result):
    checked_at = format_jalali_datetime(result.get("checked_at")) or "-"
    status = str(result.get("status") or "").upper()
    error = result.get("error_message") or result.get("summary") or "خطای نامشخص"
    response_time = result.get("response_time_ms")
    response_line = f"\nزمان پاسخ: {persian_digits(response_time)} ms" if response_time is not None else ""
    return "\n".join(
        [
            "⚠️ مشکل در پنل X-UI",
            "",
            f"پنل: {escape(panel.name)}",
            f"وضعیت: {escape(status)}",
            f"خطا: {escape(sanitize_operational_text(error, panel=panel, max_length=300))}",
            f"زمان بررسی: {checked_at}{response_line}",
            "",
            "اقدام پیشنهادی:",
            "لطفاً وضعیت سرور، سرویس X-UI و اینباندهای فعال را بررسی کنید.",
        ]
    )


def format_panel_recovery_message(panel, result):
    checked_at = format_jalali_datetime(result.get("checked_at")) or "-"
    downtime_minutes = result.get("downtime_minutes")
    downtime_label = persian_digits(downtime_minutes) if downtime_minutes is not None else "-"
    return "\n".join(
        [
            "✅ پنل دوباره در دسترس است",
            "",
            f"پنل: {escape(panel.name)}",
            "وضعیت: OK",
            f"مدت تقریبی اختلال: {downtime_label} دقیقه",
            f"زمان بررسی: {checked_at}",
        ]
    )


def send_panel_health_alert(panel, status, result):
    message = format_panel_health_alert_message(panel, result)
    return send_admin_message_to_telegram_admins(
        getattr(panel, "store", None),
        text=message,
        event_type=BotEventLog.EventType.ERROR,
    )


def send_panel_recovery_alert(panel, result):
    message = format_panel_recovery_message(panel, result)
    return send_admin_message_to_telegram_admins(
        getattr(panel, "store", None),
        text=message,
        event_type=BotEventLog.EventType.WEBHOOK,
    )


def _mark_alert_delivery(status_obj, log, result, delivery, *, recovery=False):
    sent_count = int(delivery.get("sent") or 0)
    failed_count = int(delivery.get("failed") or 0)
    result["alert_sent_count"] = sent_count
    result["alert_failed_count"] = failed_count
    result["alert_sent"] = sent_count > 0
    now = timezone.now()
    if sent_count:
        if recovery:
            status_obj.last_recovery_alert_sent_at = now
            status_obj.save(update_fields=["last_recovery_alert_sent_at", "updated_at"])
        else:
            status_obj.last_alert_sent_at = now
            status_obj.save(update_fields=["last_alert_sent_at", "updated_at"])
        log.alert_sent = True
        log.save(update_fields=["alert_sent"])
    return result


def check_panel_health(panel, send_alerts=False, force=False, dry_run=False):
    settings = get_panel_monitor_settings(getattr(panel, "store", None))
    previous_status_obj = PanelHealthStatus.objects.filter(panel=panel).first()
    previous_status = getattr(previous_status_obj, "status", PanelHealthStatus.Status.UNKNOWN)
    result = build_panel_health_result(panel, settings=settings)
    result["previous_status"] = previous_status
    result["dry_run"] = dry_run

    if previous_status_obj and previous_status_obj.last_error_at:
        downtime = result["checked_at"] - previous_status_obj.last_error_at
        result["downtime_minutes"] = max(int(downtime.total_seconds() // 60), 0)

    if dry_run:
        if send_alerts:
            result["alert_skipped"] = True
            result["alert_skip_reason"] = "dry_run"
        return result

    status_obj, log = update_panel_health_status(panel, result)
    if not send_alerts:
        return result

    if not settings.alerts_enabled:
        result["alert_skipped"] = True
        result["alert_skip_reason"] = "alerts_disabled"
        return result

    decision = should_send_panel_alert(
        previous_status,
        result["status"],
        settings,
        status_obj=previous_status_obj,
        force=force,
    )
    if decision == "problem":
        delivery = send_panel_health_alert(panel, result["status"], result)
        return _mark_alert_delivery(status_obj, log, result, delivery)
    if decision == "recovery":
        delivery = send_panel_recovery_alert(panel, result)
        return _mark_alert_delivery(status_obj, log, result, delivery, recovery=True)

    if result["status"] in PROBLEM_STATUSES:
        result["alert_skipped"] = True
        result["alert_skip_reason"] = "cooldown"
    return result


def check_all_panels_health(send_alerts=False, dry_run=False, panel_id=None, limit=None):
    panels = list(get_panels_for_health_check(panel_id=panel_id, limit=limit))
    summary = {
        "total_panels": len(panels),
        "checked": 0,
        "ok": 0,
        "warning": 0,
        "error": 0,
        "disabled": 0,
        "alerts_sent": 0,
        "alerts_skipped": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "results": [],
    }

    for panel in panels:
        try:
            result = check_panel_health(panel, send_alerts=send_alerts, dry_run=dry_run)
        except Exception as exc:
            summary["failed"] += 1
            result = {
                "panel_id": panel.pk,
                "panel_name": panel.name,
                "status": PanelHealthStatus.Status.ERROR,
                "summary": "Panel health check crashed for this panel.",
                "error_message": sanitize_operational_text(exc, panel=panel),
                "alert_sent": False,
                "alert_sent_count": 0,
                "alert_skipped": False,
            }

        status = result.get("status") or PanelHealthStatus.Status.ERROR
        if status in summary:
            summary[status] += 1
        if status != PanelHealthStatus.Status.DISABLED:
            summary["checked"] += 1
        summary["alerts_sent"] += int(result.get("alert_sent_count") or 0)
        if result.get("alert_skipped"):
            summary["alerts_skipped"] += 1
        summary["results"].append(result)

    return summary


def cleanup_old_panel_health_logs(store=None):
    now = timezone.now()
    deleted = 0
    details = []
    stores = [store] if store else list(Store.objects.order_by("pk"))
    seen_store_ids = set()

    for current_store in stores:
        if current_store is None:
            continue
        seen_store_ids.add(current_store.pk)
        settings = get_panel_monitor_settings(current_store)
        cutoff = now - timedelta(days=settings.max_log_age_days)
        count, _ = PanelHealthCheckLog.objects.filter(
            panel__store=current_store,
            checked_at__lt=cutoff,
        ).delete()
        deleted += count
        details.append({"store_id": current_store.pk, "deleted": count, "cutoff": cutoff.isoformat()})

    default_cutoff = now - timedelta(days=30)
    orphan_query = PanelHealthCheckLog.objects.filter(panel__store__isnull=True, checked_at__lt=default_cutoff)
    if store is None or getattr(store, "pk", None) is None:
        count, _ = orphan_query.delete()
        deleted += count
        details.append({"store_id": None, "deleted": count, "cutoff": default_cutoff.isoformat()})

    return {"deleted": deleted, "details": details, "store_ids": sorted(seen_store_ids)}
