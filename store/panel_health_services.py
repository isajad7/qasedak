import hashlib
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from html import escape

import requests
from django.db import transaction
from django.utils import timezone

from .admin_notifications import send_admin_message_to_telegram_admins
from .jalali import TEHRAN_TZ, format_jalali_datetime, persian_digits
from .models import BotEventLog, Inbound, Panel, PanelHealthCheckLog, PanelHealthStatus, Store
from .panels.errors import PanelIntegrationError
from .telegram_bot.redaction import sanitize_bot_event_log_value
from .xui_api import XUIError, XUIService, classify_xui_exception
from .xui_compat import discover_xui_capabilities


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
    alert_check_interval_minutes: int = 15
    alert_repeat_interval_minutes: int = 60
    failure_threshold_count: int = 2
    recovery_alert_enabled: bool = True
    quiet_hours_enabled: bool = False
    quiet_hours_start: object | None = None
    quiet_hours_end: object | None = None
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
    repeat_interval = _positive_int(getattr(store, "panel_health_alert_repeat_interval_minutes", None), 60)
    return PanelMonitorSettings(
        store=store,
        enabled=bool(getattr(store, "panel_monitor_enabled", True)),
        alerts_enabled=bool(
            getattr(store, "panel_monitor_alerts_enabled", True)
            and getattr(store, "panel_health_alerts_enabled", False)
        ),
        timeout_seconds=_positive_int(getattr(store, "panel_monitor_check_timeout_seconds", None), 15),
        alert_cooldown_minutes=repeat_interval,
        alert_check_interval_minutes=_positive_int(
            getattr(store, "panel_health_alert_check_interval_minutes", None),
            15,
        ),
        alert_repeat_interval_minutes=repeat_interval,
        failure_threshold_count=_positive_int(getattr(store, "panel_health_alert_failure_threshold_count", None), 2),
        recovery_alert_enabled=bool(getattr(store, "panel_health_recovery_alert_enabled", True)),
        quiet_hours_enabled=bool(getattr(store, "panel_health_quiet_hours_enabled", False)),
        quiet_hours_start=getattr(store, "panel_health_quiet_hours_start", None),
        quiet_hours_end=getattr(store, "panel_health_quiet_hours_end", None),
        max_log_age_days=_positive_int(getattr(store, "panel_monitor_max_log_age_days", None), 30),
    )


def get_panels_for_health_check(panel_id=None, limit=None):
    panels = Panel.objects.select_related("store").prefetch_related("inbounds").order_by("pk")
    if panel_id:
        panels = panels.filter(pk=panel_id)
    if limit:
        panels = panels[: max(int(limit), 0)]
    return panels


def mask_alert_recipient(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return f"{text[:2]}***"
    return f"{text[:4]}...{text[-4:]}"


def get_panel_health_alert_recipient_summary(store=None):
    from .models import BotConfiguration
    from .telegram_bot.notifications import active_bot_configs

    recipients = []
    configs = active_bot_configs(store=store).filter(provider=BotConfiguration.Provider.TELEGRAM)
    for config in configs:
        for admin_id in config.get_admin_user_ids():
            if admin_id not in recipients:
                recipients.append(admin_id)
    return {
        "count": len(recipients),
        "masked": [mask_alert_recipient(admin_id) for admin_id in recipients],
        "config_count": configs.count(),
    }


def safe_panel_alert_label(panel):
    name = sanitize_operational_text(getattr(panel, "name", "") or "", panel=panel, max_length=80)
    if name:
        return f"{name} (ID {getattr(panel, 'pk', '-')})"
    return f"Panel ID {getattr(panel, 'pk', '-')}"


def panel_alert_repeat_interval_minutes(panel, settings):
    return _positive_int(getattr(panel, "alert_repeat_interval_minutes", None), settings.alert_repeat_interval_minutes)


def panel_alert_failure_threshold_count(panel, settings):
    return _positive_int(getattr(panel, "failure_threshold_count", None), settings.failure_threshold_count)


def panel_health_alerts_quiet_now(settings, *, now=None):
    if not settings.quiet_hours_enabled or not settings.quiet_hours_start or not settings.quiet_hours_end:
        return False
    now = now or timezone.now()
    local_time = timezone.localtime(now, TEHRAN_TZ).time()
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


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
    text = re.sub(r"(?i)(?:^|\s)/?sub/[^\s<>()]+", " <subscription-link-redacted>", text)
    text = re.sub(r"(?i)(csrf[-_ ]?token|session|cookie)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)\bcsrf[-_a-z0-9]{6,}\b", "<csrf-redacted>", text)
    text = re.sub(r"(?i)\b(?:session|cookie)[-_a-z0-9]{8,}\b", "<session-redacted>", text)
    if len(text) > max_length:
        text = f"{text[:max_length - 1]}..."
    return text


def sanitize_operational_metadata(value, *, panel=None):
    value = sanitize_bot_event_log_value(value)
    if isinstance(value, dict):
        return {
            sanitize_operational_text(key, panel=panel): sanitize_operational_metadata(item, panel=panel)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_operational_metadata(item, panel=panel) for item in value]
    if isinstance(value, str):
        return sanitize_operational_text(value, panel=panel)
    return value


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
    if isinstance(exc, PanelIntegrationError):
        if exc.__cause__:
            return _classify_exception(exc.__cause__)
        return exc.error_code or "panel_integration_error", exc.message or "خطای عملیات پنل"
    if isinstance(exc, XUIError):
        if exc.category == "http_403_csrf_required":
            return (
                "http_403_csrf_required",
                "ورود به پنل با خطای HTTP 403 ناموفق شد. احتمالاً CSRF یا تنظیمات امنیتی پنل نیاز به سازگاری دارد.",
            )
        if exc.category == "http_403":
            return "http_403", "درخواست پنل با خطای HTTP 403 ناموفق شد."
        if exc.category == "http_401":
            return "http_401", "ورود به پنل با خطای HTTP 401 ناموفق شد. نام کاربری یا رمز پنل را بررسی کنید."
        if exc.category == "two_factor_required":
            return (
                "two_factor_required",
                "ورود دو مرحله‌ای برای این پنل فعال است و اتصال خودکار پشتیبانی نمی‌شود.",
            )
        if exc.category == "auth_failed":
            return "auth_failed", "ورود به پنل ناموفق بود"
        if exc.category == "unexpected_response":
            return "unexpected_response", "پاسخ پنل قابل خواندن یا قابل انتظار نبود"
        if exc.category == "unsupported_version":
            return "unsupported_version", "نسخه یا پروفایل پنل پشتیبانی نمی‌شود"
        message = str(exc).lower()
        if "login" in message or "rejected" in message or "auth" in message:
            return "auth_failed", "ورود به پنل ناموفق بود"
        if "json" in message or "invalid" in message:
            return "unexpected_response", "پاسخ پنل قابل خواندن یا قابل انتظار نبود"
        if "not found" in message:
            return "inbound_not_found", "اینباند در پنل پیدا نشد"
        return exc.category or "unknown", "خطای API پنل"
    category, _message, _metadata = classify_xui_exception(exc)
    if category == "network_timeout":
        return "network_timeout", "عدم پاسخ پنل در زمان مجاز"
    if category == "connection_refused":
        return "connection_refused", "اتصال به پنل برقرار نشد"
    if category == "dns_error":
        return "dns_error", "نام دامنه پنل قابل resolve نبود"
    if category == "tls_error":
        return "tls_error", "خطای TLS/SSL هنگام اتصال به پنل"
    if category == "invalid_url":
        return "invalid_url", "آدرس پنل نامعتبر است"
    if isinstance(exc, requests.RequestException):
        return category, "خطای ارتباط با پنل"
    return "unexpected_error", "خطای غیرمنتظره هنگام بررسی پنل"


def _exception_metadata(exc, *, panel):
    if isinstance(exc, PanelIntegrationError):
        metadata = exc.to_safe_dict()
        if exc.__cause__:
            _category, _message, cause_metadata = classify_xui_exception(exc.__cause__)
            metadata["cause"] = sanitize_operational_metadata(dict(cause_metadata or {}), panel=panel)
        return metadata
    _category, _message, metadata = classify_xui_exception(exc)
    safe_metadata = sanitize_operational_metadata(dict(metadata or {}), panel=panel)
    safe_metadata["exception"] = sanitize_operational_text(exc, panel=panel)
    return safe_metadata


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
    inbound_data = service.get_inbound(inbound, use_cache=False)
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


def _compatibility_health_metadata(profile=None, error=""):
    metadata = {
        "compatibility": {
            "profile": getattr(profile, "profile", "") or "unknown",
            "version": getattr(profile, "version", "") or "",
            "node_count": 0,
            "host_count": 0,
            "source": "",
            "error": error,
        },
        "node_issues": [],
        "node_issue_count": 0,
    }
    if not profile:
        return metadata, []
    profile_metadata = profile.metadata or {}
    nodes = profile_metadata.get("nodes") or []
    node_issues = []
    healthy_statuses = {"", "ok", "online", "running", "active", "connected", "healthy"}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        status = str(node.get("status") or "").strip().lower()
        enabled = node.get("enabled")
        if enabled is False:
            continue
        if status not in healthy_statuses:
            node_issues.append(
                {
                    "node_id": node.get("external_node_id") or "",
                    "node_name": node.get("name") or "",
                    "status": status or "unknown",
                    "message": "Node is not reporting an online/healthy status.",
                }
            )
    metadata["compatibility"] = {
        "profile": profile.profile,
        "version": profile.version or "",
        "node_count": profile_metadata.get("node_count", 0),
        "host_count": profile_metadata.get("host_count", 0),
        "source": profile_metadata.get("source", ""),
        "error": error,
    }
    metadata["node_issues"] = node_issues[:20]
    metadata["node_issue_count"] = len(node_issues)
    return metadata, node_issues


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

    from .panels import get_safe_panel_adapter

    adapter = get_safe_panel_adapter(panel)
    capability_report = adapter.get_capability_report()
    if getattr(adapter, "family", "") == Panel.Family.PASARGUARD:
        from .external_subscription_sources import external_feed_health_summary_for_panel

        try:
            adapter.test_connection()
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
                metadata=_exception_metadata(exc, panel=panel),
            )
            result["response_time_ms"] = int((time.monotonic() - start) * 1000)
            return result

        all_active_inbounds = list(
            Inbound.objects.filter(panel=panel, is_active=True).order_by("inbound_id")
        )
        ignored_inbounds = [inbound for inbound in all_active_inbounds if not inbound.health_monitor_enabled]
        active_inbounds = [inbound for inbound in all_active_inbounds if inbound.health_monitor_enabled]
        ignored_metadata = _ignored_inbound_metadata(ignored_inbounds)
        metadata_base = {
            "capability_report": capability_report.to_dict(),
            "native_raw_delivery": True,
            "external_subscription_feeds": external_feed_health_summary_for_panel(panel),
            **ignored_metadata,
        }
        if not all_active_inbounds:
            result = _base_result(
                panel,
                settings,
                status=PanelHealthStatus.Status.WARNING,
                summary="اتصال PasarGuard موفق بود اما هیچ گروه فعال محلی برای بررسی وجود ندارد.",
                login_ok=True,
                error_code="no_active_pasarguard_groups",
                error_message="هیچ گروه فعال محلی پیدا نشد.",
                metadata={"inbound_issues": [], "inbound_issue_count": 0, **metadata_base},
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
                summary=f"اتصال PasarGuard موفق بود؛ {persian_digits(ignored_count)} گروه ignored نادیده گرفته شد.",
                login_ok=True,
                metadata={"inbound_issues": [], "inbound_issue_count": 0, **metadata_base},
            )
            result["response_time_ms"] = int((time.monotonic() - start) * 1000)
            return result

        warnings = []
        errors = []
        ok_count = 0
        feed_health = metadata_base["external_subscription_feeds"]
        for issue in feed_health.get("issues") or []:
            target = errors if issue.get("status") == "error" else warnings
            target.append(
                {
                    "inbound_id": "",
                    "remark": f"External feed {issue.get('feed_id') or '-'}",
                    "code": issue.get("last_error_code") or "external_subscription_feed_unhealthy",
                    "message": "Dynamic subscription feed refresh is not healthy.",
                    "expected": "healthy",
                    "actual": issue.get("status") or "unknown",
                    "metadata": issue,
                }
            )
        for inbound in active_inbounds:
            check = adapter.check_inbound(inbound)
            if check.ok:
                ok_count += 1
                continue
            issue = _inbound_issue(
                inbound,
                check.status or "pasarguard_group_warning",
                check.message or "گروه PasarGuard قابل استفاده نیست.",
                expected="active",
                actual=check.status,
            )
            issue["metadata"] = sanitize_operational_metadata(check.metadata, panel=panel)
            if check.status == "error":
                errors.append(issue)
            else:
                warnings.append(issue)

        issue_count = len(warnings) + len(errors)
        status = PanelHealthStatus.Status.WARNING if issue_count else PanelHealthStatus.Status.OK
        summary = (
            f"اتصال PasarGuard موفق بود؛ {persian_digits(issue_count)} مشکل در گروه‌ها دیده شد"
            if issue_count
            else f"PasarGuard سالم است و {persian_digits(ok_count)} گروه فعال بررسی شد"
        )
        result = _base_result(
            panel,
            settings,
            status=status,
            summary=f"{summary}{_ignored_summary_suffix(len(ignored_inbounds))}.",
            login_ok=True,
            error_code="pasarguard_group_warning" if warnings else "pasarguard_group_error" if errors else "",
            error_message=warnings[0]["message"] if warnings else errors[0]["message"] if errors else "",
            metadata={
                "inbound_issues": [*warnings, *errors][:20],
                "inbound_issue_count": issue_count,
                **metadata_base,
            },
        )
        result["inbounds_checked"] = len(active_inbounds)
        result["inbounds_ok"] = ok_count
        result["inbounds_warning"] = len(warnings)
        result["inbounds_error"] = len(errors)
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    if getattr(adapter, "family", "") != "xui":
        structured_error = PanelIntegrationError(
            "این خانواده پنل هنوز در مانیتورینگ سلامت پیاده‌سازی نشده است.",
            error_code="unsupported_panel_family",
            layer="adapter_factory",
            action="panel_health_check",
            technical_detail="Panel family is not supported by health monitoring yet.",
            remediation="برای health monitoring فعلاً پنل X-UI انتخاب کنید یا adapter خانواده پنل را تکمیل کنید.",
            panel=panel,
            panel_family=getattr(capability_report, "family", "") or "",
            capability_profile=getattr(capability_report, "capability_profile", "") or "",
            safe_context={"capability_report": capability_report.to_dict()},
        ).to_safe_dict()
        result = _base_result(
            panel,
            settings,
            status=PanelHealthStatus.Status.WARNING,
            summary="این خانواده پنل هنوز در مانیتورینگ سلامت پیاده‌سازی نشده است.",
            login_ok=None,
            error_code="unsupported_panel_family",
            error_message="این خانواده پنل هنوز در مانیتورینگ سلامت پیاده‌سازی نشده است.",
            metadata={"capability_report": capability_report.to_dict(), "structured_error": structured_error},
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
            metadata=_exception_metadata(exc, panel=panel),
        )
        result["response_time_ms"] = int((time.monotonic() - start) * 1000)
        return result

    try:
        compatibility_profile = discover_xui_capabilities(panel, live=True, service=service, write=True, use_cache=False)
        compatibility_metadata, node_issues = _compatibility_health_metadata(compatibility_profile)
        compatibility_metadata["capability_report"] = adapter.detect_capabilities(live=False).to_dict()
    except Exception as exc:
        compatibility_profile = None
        compatibility_metadata, node_issues = _compatibility_health_metadata(
            None,
            error=sanitize_operational_text(exc, panel=panel),
        )

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
            metadata={"inbound_issues": [], "inbound_issue_count": 0, **ignored_metadata, **compatibility_metadata},
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
            metadata={"inbound_issues": [], "inbound_issue_count": 0, **ignored_metadata, **compatibility_metadata},
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
            issue.update(_exception_metadata(exc, panel=panel))
            errors.append(issue)
            continue

        if issues:
            warnings.extend(issues)
        else:
            ok_count += 1

    issue_count = len(warnings) + len(errors)
    if node_issues:
        warnings.extend(
            {
                "inbound_id": "",
                "remark": item.get("node_name") or item.get("node_id") or "",
                "code": "node_unhealthy",
                "message": item.get("message") or "Node health warning.",
                "expected": "online",
                "actual": item.get("status") or "unknown",
            }
            for item in node_issues[:20]
        )
        issue_count = len(warnings) + len(errors)
    if issue_count:
        status = PanelHealthStatus.Status.WARNING
        summary = (
            f"ورود به پنل موفق بود؛ {persian_digits(issue_count)} مشکل در node/اینباندها دیده شد"
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
            **compatibility_metadata,
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
    metadata = sanitize_operational_metadata(result.get("metadata") or {}, panel=panel)
    with transaction.atomic():
        health_status, _created = PanelHealthStatus.objects.select_for_update().get_or_create(panel=panel)
        previous_status = health_status.status
        health_status.status = status
        health_status.last_checked_at = checked_at
        health_status.response_time_ms = result.get("response_time_ms")
        health_status.error_code = result.get("error_code") or ""
        health_status.error_message = result.get("error_message") or ""
        health_status.summary = result.get("summary") or ""
        health_status.metadata = metadata

        if status == PanelHealthStatus.Status.OK:
            health_status.last_ok_at = checked_at
            if previous_status in PROBLEM_STATUSES:
                health_status.last_recovery_at = checked_at
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


def _message_hash(message):
    return hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()


def should_send_panel_alert(previous_status, new_status, settings, *, panel=None, status_obj=None, force=False, now=None):
    decision = PanelHealthAlertService(now=now).decide_alert(
        panel,
        previous_status,
        {"status": new_status},
        settings,
        status_obj=status_obj,
        force=force,
    )
    return decision["action"]


def format_panel_health_alert_message(panel, result):
    checked_at = format_jalali_datetime(result.get("checked_at")) or "-"
    status = str(result.get("status") or "").upper()
    step = result.get("error_code") or status or "-"
    error = result.get("error_message") or result.get("summary") or "خطای نامشخص"
    consecutive_failures = result.get("consecutive_failure_count")
    if consecutive_failures is None:
        consecutive_failures = result.get("consecutive_failures") or 0
    return "\n".join(
        [
            "🚨 هشدار خرابی پنل",
            "",
            f"پنل: {escape(safe_panel_alert_label(panel))}",
            "وضعیت: خراب",
            f"مرحله: {escape(str(step))}",
            f"خطا: {escape(sanitize_operational_text(error, panel=panel, max_length=300))}",
            f"تعداد خطاهای پشت‌سرهم: {persian_digits(consecutive_failures)}",
            f"زمان: {checked_at}",
            "",
            "اقدام پیشنهادی:",
            "- credential پنل را بررسی کنید",
            "- وضعیت API پنل را بررسی کنید",
            "- اینباندها و ظرفیت را بررسی کنید",
        ]
    )


def format_panel_recovery_message(panel, result):
    checked_at = format_jalali_datetime(result.get("checked_at")) or "-"
    downtime_minutes = result.get("downtime_minutes")
    downtime_label = persian_digits(downtime_minutes) if downtime_minutes is not None else "-"
    return "\n".join(
        [
            "✅ پنل دوباره سالم شد",
            "",
            f"پنل: {escape(safe_panel_alert_label(panel))}",
            f"زمان قطعی تقریبی: {downtime_label} دقیقه",
            f"زمان بازیابی: {checked_at}",
        ]
    )


def send_panel_health_alert(panel, status, result):
    message = format_panel_health_alert_message(panel, result)
    result["alert_message_hash"] = _message_hash(message)
    return send_admin_message_to_telegram_admins(
        getattr(panel, "store", None),
        text=message,
        event_type=BotEventLog.EventType.ERROR,
    )


def send_panel_recovery_alert(panel, result):
    message = format_panel_recovery_message(panel, result)
    result["alert_message_hash"] = _message_hash(message)
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
            status_obj.last_alert_error_code = result.get("error_code") or ""
            status_obj.last_alert_message_hash = result.get("alert_message_hash") or ""
            status_obj.save(
                update_fields=[
                    "last_alert_sent_at",
                    "last_alert_error_code",
                    "last_alert_message_hash",
                    "updated_at",
                ]
            )
        log.alert_sent = True
        log.save(update_fields=["alert_sent"])
    return result


class PanelHealthAlertService:
    def __init__(self, *, store=None, now=None):
        self.store = store
        self.now = now

    def get_settings(self, panel=None):
        return get_panel_monitor_settings(getattr(panel, "store", None) or self.store)

    def simulate_consecutive_failure_count(self, previous_status_obj, result):
        status = result.get("status")
        if status not in PROBLEM_STATUSES:
            return 0
        previous_status = getattr(previous_status_obj, "status", PanelHealthStatus.Status.UNKNOWN)
        previous_count = int(getattr(previous_status_obj, "consecutive_failures", 0) or 0)
        return previous_count + 1 if previous_status in PROBLEM_STATUSES else 1

    def decide_alert(self, panel, previous_status, result, settings, *, status_obj=None, force=False):
        now = self.now or timezone.now()
        new_status = result.get("status") or PanelHealthStatus.Status.UNKNOWN
        previous_status = previous_status or PanelHealthStatus.Status.UNKNOWN
        if panel is None:
            return {"action": "", "reason": "panel_missing", "would_send": False}
        if not settings.alerts_enabled:
            return {"action": "", "reason": "alerts_disabled", "would_send": False}
        if not getattr(panel, "health_alert_enabled", True):
            return {"action": "", "reason": "panel_alert_disabled", "would_send": False}
        if panel_health_alerts_quiet_now(settings, now=now) and not force:
            return {"action": "", "reason": "quiet_hours", "would_send": False}

        if new_status == PanelHealthStatus.Status.OK and previous_status in PROBLEM_STATUSES:
            if settings.recovery_alert_enabled:
                return {"action": "recovery", "reason": "", "would_send": True}
            return {"action": "", "reason": "recovery_disabled", "would_send": False}

        if new_status not in PROBLEM_STATUSES:
            return {"action": "", "reason": "healthy", "would_send": False}

        if force:
            return {"action": "problem", "reason": "", "would_send": True}

        consecutive_failures = result.get("consecutive_failure_count")
        if consecutive_failures is None:
            consecutive_failures = int(getattr(status_obj, "consecutive_failures", 0) or 0)
        threshold = panel_alert_failure_threshold_count(panel, settings)
        if consecutive_failures < threshold:
            return {"action": "", "reason": "failure_threshold", "would_send": False}

        last_alert = getattr(status_obj, "last_alert_sent_at", None)
        repeat_interval = panel_alert_repeat_interval_minutes(panel, settings)
        if last_alert and last_alert + timedelta(minutes=repeat_interval) > now:
            return {"action": "", "reason": "repeat_interval", "would_send": False}

        return {"action": "problem", "reason": "", "would_send": True}

    def check_panel(self, panel, *, send_alerts=False, force=False, dry_run=False, no_send=False):
        settings = self.get_settings(panel)
        previous_status_obj = PanelHealthStatus.objects.filter(panel=panel).first()
        previous_status = getattr(previous_status_obj, "status", PanelHealthStatus.Status.UNKNOWN)
        result = build_panel_health_result(panel, settings=settings)
        result["previous_status"] = previous_status
        result["dry_run"] = dry_run
        result["would_send_alert"] = False
        result["alert_decision"] = ""

        if previous_status_obj and previous_status_obj.last_error_at:
            downtime = result["checked_at"] - previous_status_obj.last_error_at
            result["downtime_minutes"] = max(int(downtime.total_seconds() // 60), 0)

        if dry_run:
            result["consecutive_failure_count"] = self.simulate_consecutive_failure_count(previous_status_obj, result)
            if send_alerts:
                decision = self.decide_alert(
                    panel,
                    previous_status,
                    result,
                    settings,
                    status_obj=previous_status_obj,
                    force=force,
                )
                result["would_send_alert"] = bool(decision["would_send"])
                result["alert_decision"] = decision["action"]
                if not decision["would_send"]:
                    result["alert_skipped"] = True
                    result["alert_skip_reason"] = decision["reason"] or "dry_run"
            return result

        status_obj, log = update_panel_health_status(panel, result)
        result["consecutive_failure_count"] = status_obj.consecutive_failures
        result["last_health_status"] = status_obj.status
        if not send_alerts:
            return result

        decision = self.decide_alert(
            panel,
            previous_status,
            result,
            settings,
            status_obj=status_obj,
            force=force,
        )
        result["would_send_alert"] = bool(decision["would_send"])
        result["alert_decision"] = decision["action"]
        if no_send and decision["would_send"]:
            result["alert_skipped"] = True
            result["alert_skip_reason"] = "no_send"
            return result
        if decision["action"] == "problem":
            delivery = send_panel_health_alert(panel, result["status"], result)
            return _mark_alert_delivery(status_obj, log, result, delivery)
        if decision["action"] == "recovery":
            delivery = send_panel_recovery_alert(panel, result)
            return _mark_alert_delivery(status_obj, log, result, delivery, recovery=True)

        if result["status"] in PROBLEM_STATUSES or previous_status in PROBLEM_STATUSES:
            result["alert_skipped"] = True
            result["alert_skip_reason"] = decision["reason"]
        return result

    def send_test_message(self, panel, *, dry_run=False, no_send=False):
        result = {
            "panel_id": getattr(panel, "pk", None),
            "panel_name": getattr(panel, "name", ""),
            "status": "test",
            "checked_at": self.now or timezone.now(),
            "consecutive_failure_count": 0,
            "error_code": "test_message",
            "error_message": "پیام تست هشدار سلامت پنل.",
            "summary": "پیام تست هشدار سلامت پنل.",
        }
        message = "\n".join(
            [
                "🧪 پیام تست هشدار سلامت پنل",
                "",
                f"پنل: {escape(safe_panel_alert_label(panel))}",
                f"زمان: {format_jalali_datetime(result['checked_at']) or '-'}",
                "",
                "این پیام فقط برای ادمین‌های تنظیم‌شده ارسال شده است.",
            ]
        )
        result["alert_message_hash"] = _message_hash(message)
        result["would_send_alert"] = True
        if dry_run or no_send:
            result["alert_skipped"] = True
            result["alert_skip_reason"] = "dry_run" if dry_run else "no_send"
            result["alert_sent_count"] = 0
            result["alert_failed_count"] = 0
            return result
        delivery = send_admin_message_to_telegram_admins(
            getattr(panel, "store", None),
            text=message,
            event_type=BotEventLog.EventType.WEBHOOK,
        )
        result["alert_sent_count"] = int(delivery.get("sent") or 0)
        result["alert_failed_count"] = int(delivery.get("failed") or 0)
        result["alert_sent"] = result["alert_sent_count"] > 0
        return result


def check_panel_health(panel, send_alerts=False, force=False, dry_run=False, no_send=False):
    return PanelHealthAlertService().check_panel(
        panel,
        send_alerts=send_alerts,
        force=force,
        dry_run=dry_run,
        no_send=no_send,
    )


def check_all_panels_health(send_alerts=False, dry_run=False, panel_id=None, limit=None, force=False, no_send=False, active_only=False):
    panels = list(get_panels_for_health_check(panel_id=panel_id, limit=limit))
    if active_only and panel_id is None:
        panels = [panel for panel in panels if panel.is_active]
    service = PanelHealthAlertService()
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
        "would_send": 0,
        "dry_run": bool(dry_run),
        "results": [],
    }

    for panel in panels:
        try:
            result = service.check_panel(
                panel,
                send_alerts=send_alerts,
                force=force,
                dry_run=dry_run,
                no_send=no_send,
            )
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
                "would_send_alert": False,
            }

        status = result.get("status") or PanelHealthStatus.Status.ERROR
        if status in summary:
            summary[status] += 1
        if status != PanelHealthStatus.Status.DISABLED:
            summary["checked"] += 1
        summary["alerts_sent"] += int(result.get("alert_sent_count") or 0)
        summary["would_send"] += int(bool(result.get("would_send_alert")))
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
