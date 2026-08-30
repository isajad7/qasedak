from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.urls import reverse
from django.utils import timezone

from store.models import Inbound, Panel, PanelHealthStatus, Store
from store.panels import get_safe_panel_adapter
from store.panels.capabilities import sanitize_capability_metadata
from store.panels.errors import PanelIntegrationError, safe_error_dict
from store.panel_health_services import (
    PanelHealthAlertService,
    get_panel_health_alert_recipient_summary,
    panel_alert_failure_threshold_count,
    panel_alert_repeat_interval_minutes,
)
from store.xui_api import XUIService, classify_xui_exception, sanitize_xui_operational_text
from store.xui_compat import discover_xui_capabilities
from store.management.commands.sync_xui_topology import Command as SyncXUITopologyCommand


SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan"}


def is_pasarguard_panel(panel):
    return str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD


def target_labels_for_panel(panel):
    if is_pasarguard_panel(panel):
        return {
            "singular": "گروه",
            "plural": "گروه‌ها",
            "explorer": "کاوشگر گروه‌ها",
            "recent": "گروه‌های اخیر",
            "local_empty": "گروه local برای این پنل ثبت نشده است.",
            "remote_id": "Group ID",
            "name": "نام گروه",
            "protocol": "Delivery",
            "host_port": "Tags",
            "node": "وضعیت",
            "link": "Raw native",
            "sync": "همگام‌سازی گروه‌ها",
            "inbounds": "گروه‌ها",
        }
    return {
        "singular": "اینباند",
        "plural": "اینباندها",
            "explorer": "کاوشگر اینباندها",
            "recent": "اینباندهای اخیر",
        "local_empty": "اینباند local برای این پنل ثبت نشده است.",
        "remote_id": "Remote ID",
        "name": "نام",
        "protocol": "پروتکل",
        "host_port": "Host / Port",
        "node": "Node",
        "link": "لینک",
        "sync": "همگام‌سازی اینباندها",
        "inbounds": "اینباندها",
    }


@dataclass
class PanelActionResult:
    ok: bool
    title: str
    message: str
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    structured_errors: list[dict] = field(default_factory=list)


def mask_panel_url(value):
    parsed = urlsplit(str(value or ""))
    if not parsed.netloc:
        return "-"
    host = parsed.hostname or parsed.netloc.split("@")[-1]
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    if path and path != "/":
        return f"{parsed.scheme}://{host}{path}"
    return f"{parsed.scheme}://{host}"


def panel_center_url(name, *args):
    return reverse(name, args=args)


def panel_action_urls(panel):
    return {
        "detail": panel_center_url("admin_store_panel_center_detail", panel.pk),
        "edit": panel_center_url("admin_store_panel_center_edit", panel.pk),
        "test": panel_center_url("admin_store_panel_center_test", panel.pk),
        "alert_dry_run": panel_center_url("admin_store_panel_center_panel_alerts_dry_run", panel.pk),
        "alert_enable": panel_center_url("admin_store_panel_center_panel_alert_enable", panel.pk),
        "alert_disable": panel_center_url("admin_store_panel_center_panel_alert_disable", panel.pk),
        "sync": panel_center_url("admin_store_panel_center_sync", panel.pk),
        "inbounds": panel_center_url("admin_store_panel_center_inbounds", panel.pk),
        "capabilities": panel_center_url("admin_store_panel_center_capabilities", panel.pk),
        "django_admin": reverse("admin:store_panel_change", args=[panel.pk]),
        "target_plural": target_labels_for_panel(panel)["plural"],
        "target_sync_label": target_labels_for_panel(panel)["sync"],
        "target_inbounds_label": target_labels_for_panel(panel)["inbounds"],
    }


def badge_tone_for_report(report):
    if not report.supported:
        return "slate"
    if report.errors:
        return "rose"
    if report.warnings:
        return "amber"
    return "emerald"


def boolean_badge(value):
    if value is True:
        return {"label": "پشتیبانی می‌شود", "tone": "emerald"}
    if value is False:
        return {"label": "پشتیبانی نمی‌شود", "tone": "slate"}
    return {"label": "نامشخص", "tone": "amber"}


def capability_items(report):
    items = [
        ("ورود", boolean_badge(report.supports_login)),
        ("خواندن اینباندها", boolean_badge(report.supports_read_inbounds)),
        ("ساخت کلاینت", boolean_badge(report.supports_create_client)),
        ("حذف کلاینت", boolean_badge(report.supports_delete_client)),
        ("ساخت چند اینباندی", boolean_badge(report.supports_multi_inbound_create)),
        ("لینک subscription", boolean_badge(report.supports_subscription)),
        ("CSRF در login", boolean_badge(report.requires_csrf_for_login)),
        ("CSRF در write", boolean_badge(report.requires_csrf_for_write)),
    ]
    if getattr(report, "family", "") == Panel.Family.PASARGUARD:
        items = [
            ("API key auth", boolean_badge(report.uses_api_key_auth)),
            ("خواندن گروه‌ها", boolean_badge(report.supports_read_inbounds)),
            ("ساخت/ویرایش کاربر", boolean_badge(report.supports_create_client and report.supports_update_client)),
            ("Disable کاربر", boolean_badge(report.supports_disable_client)),
            ("Reset usage", boolean_badge(report.supports_reset_usage)),
            ("چندگروهی با group_ids", boolean_badge(report.supports_multi_group_users)),
            ("subscription_url", boolean_badge(report.supports_subscription)),
            ("raw native configs", boolean_badge(report.supports_native_raw_configs)),
        ]
    return items


def structured_errors_for_capability_report(panel, report):
    if not report or (getattr(report, "supported", True) and not getattr(report, "errors", ())):
        return []
    return [
        PanelIntegrationError(
            "این پنل هنوز برای ساخت کانفیگ قابل استفاده نیست.",
            error_code="panel_capability_missing" if getattr(report, "supported", False) else "unsupported_panel_family",
            layer="capability_detection",
            action="detect_capabilities",
            technical_detail="; ".join(getattr(report, "errors", ()) or getattr(report, "warnings", ())),
            remediation="از Panel Center گزینه Test connection / Sync capabilities را اجرا کنید، یا family پنل را روی X-UI تنظیم کنید.",
            panel=panel,
            panel_family=getattr(report, "family", "") or "",
            capability_profile=getattr(report, "capability_profile", "") or "",
            safe_context={"capability_report": report.to_dict()},
            warnings=list(getattr(report, "warnings", ()) or []),
        ).to_safe_dict()
    ]


def safe_capability_report(panel):
    adapter = get_safe_panel_adapter(panel)
    report = adapter.get_capability_report()
    report_dict = report.to_dict()
    return adapter, report, report_dict


def panel_summary(panel):
    _adapter, report, _report_dict = safe_capability_report(panel)
    health = getattr(panel, "health_status", None)
    return {
        "panel": panel,
        "family": panel.get_family_display() if hasattr(panel, "get_family_display") else report.family,
        "masked_url": mask_panel_url(panel.url),
        "report": report,
        "report_tone": badge_tone_for_report(report),
        "health": health,
        "health_label": health.get_status_display() if health else "بدون بررسی",
        "health_checked_at": timezone.localtime(health.last_checked_at).strftime("%Y-%m-%d %H:%M") if health and health.last_checked_at else "-",
        "actions": panel_action_urls(panel),
        "target_labels": target_labels_for_panel(panel),
    }


def panel_list_items():
    panels = get_panel_queryset()
    return [panel_summary(panel) for panel in panels.order_by("store__name", "name", "pk")]


def get_panel_queryset():
    return Panel.objects.select_related("store", "health_status").order_by("store__name", "name", "pk")


def _alert_settings_url(store=None):
    if store and getattr(store, "pk", None):
        return reverse("admin:store_panelhealthalertsettings_change", args=[store.pk])
    first_store = Store.objects.order_by("pk").first()
    if first_store:
        return reverse("admin:store_panelhealthalertsettings_change", args=[first_store.pk])
    return reverse("admin:store_panelhealthalertsettings_changelist")


def _alert_status_label(enabled):
    return {"label": "فعال", "tone": "emerald"} if enabled else {"label": "غیرفعال", "tone": "slate"}


def panel_health_alert_summary(store=None):
    store = store or Store.objects.filter(is_active=True).order_by("pk").first() or Store.objects.order_by("pk").first()
    settings = PanelHealthAlertService(store=store).get_settings()
    recipients = get_panel_health_alert_recipient_summary(store)
    panels = Panel.objects.all()
    if store:
        panels = panels.filter(store=store)
    alert_enabled_panel_count = panels.filter(is_active=True, health_alert_enabled=True).count()
    enabled = bool(store and store.panel_health_alerts_enabled and store.panel_monitor_alerts_enabled)
    return {
        "store": store,
        "enabled": enabled,
        "status_badge": _alert_status_label(enabled),
        "check_interval": settings.alert_check_interval_minutes,
        "failure_threshold": settings.failure_threshold_count,
        "repeat_interval": settings.alert_repeat_interval_minutes,
        "recipients": recipients,
        "recipients_label": ", ".join(recipients["masked"]) if recipients["masked"] else "گیرنده ادمین تنظیم نشده",
        "alert_enabled_panel_count": alert_enabled_panel_count,
        "settings_url": _alert_settings_url(store),
        "dry_run_url": reverse("admin_store_panel_center_alerts_dry_run"),
        "panel_status_url": reverse("admin:store_panel_changelist"),
    }


def panel_health_alert_detail(panel):
    settings = PanelHealthAlertService().get_settings(panel)
    try:
        health = panel.health_status
    except PanelHealthStatus.DoesNotExist:
        health = None
    repeat_interval = panel_alert_repeat_interval_minutes(panel, settings)
    threshold = panel_alert_failure_threshold_count(panel, settings)
    override_enabled = bool(panel.alert_repeat_interval_minutes or panel.failure_threshold_count)
    return {
        "enabled": bool(panel.health_alert_enabled),
        "status_badge": _alert_status_label(bool(panel.health_alert_enabled)),
        "health": health,
        "health_label": health.get_status_display() if health else "بدون بررسی",
        "last_error": (health.error_message or health.summary) if health else "-",
        "consecutive_failures": health.consecutive_failures if health else 0,
        "last_alert_sent_at": timezone.localtime(health.last_alert_sent_at).strftime("%Y-%m-%d %H:%M") if health and health.last_alert_sent_at else "-",
        "last_recovery_at": timezone.localtime(health.last_recovery_at).strftime("%Y-%m-%d %H:%M") if health and health.last_recovery_at else "-",
        "override_enabled": override_enabled,
        "override_label": "فعال" if override_enabled else "استفاده از تنظیمات کلی",
        "repeat_interval": repeat_interval,
        "failure_threshold": threshold,
        "settings_url": _alert_settings_url(getattr(panel, "store", None)),
        "dry_run_url": panel_center_url("admin_store_panel_center_panel_alerts_dry_run", panel.pk),
        "admin_change_url": reverse("admin:store_panel_change", args=[panel.pk]),
        "test_message_url": reverse("admin:store_panel_health_alert_test", args=[panel.pk]),
    }


def build_test_connection_result(panel):
    adapter = get_safe_panel_adapter(panel)
    if getattr(adapter, "family", "") == Panel.Family.PASARGUARD:
        try:
            api_ok = adapter.test_connection()
            groups = adapter.list_inbounds()
            report = adapter.detect_capabilities(live=True, write=False)
        except Exception as exc:
            structured = safe_error_dict(
                exc,
                error_code="pasarguard_test_connection_failed",
                layer="pasarguard_api",
                action="test_connection",
                message="تست اتصال PasarGuard ناموفق بود.",
                remediation="آدرس پایه پنل، API key و دسترسی /api/system و /api/groups را بررسی کنید.",
                panel=panel,
            )
            safe_message = structured.get("message") or "تست اتصال PasarGuard ناموفق بود."
            return PanelActionResult(
                False,
                "تست اتصال ناموفق بود",
                safe_message,
                details=structured,
                errors=[safe_message],
                structured_errors=[structured],
            )
        return PanelActionResult(
            True,
            "تست اتصال موفق بود",
            "API key و خواندن گروه‌های PasarGuard با موفقیت انجام شد.",
            details={
                "api_ok": bool(api_ok),
                "read_api_ok": True,
                "remote_groups": len(groups),
                "capability_profile": report.capability_profile or "-",
                "native_raw_delivery": True,
            },
            warnings=list(report.warnings),
            errors=list(report.errors),
        )

    if getattr(adapter, "family", "") != Panel.Family.XUI:
        report = adapter.get_capability_report()
        message = "این پنل هنوز برای تست اتصال عملیاتی قابل استفاده نیست."
        structured = safe_error_dict(
            PanelIntegrationError(
                message,
                error_code="unsupported_panel_family",
                layer="adapter_factory",
                action="test_connection",
                technical_detail="; ".join(report.errors or report.warnings),
                remediation="family پنل را بررسی کنید یا برای عملیات remote از پنل X-UI استفاده کنید.",
                panel=panel,
                panel_family=report.family,
                capability_profile=report.capability_profile,
                safe_context={"capability_report": report.to_dict()},
            )
        )
        return PanelActionResult(
            False,
            "تست اتصال انجام نشد",
            message,
            details=report.to_dict(),
            errors=[message],
            structured_errors=[structured],
        )

    try:
        login_ok = adapter.test_connection()
        read_inbounds = adapter.list_inbounds()
        report = adapter.detect_capabilities(live=True, write=False)
    except Exception as exc:
        source_exc = exc.__cause__ if isinstance(exc, PanelIntegrationError) and exc.__cause__ else exc
        category, message, metadata = classify_xui_exception(source_exc)
        safe_message = sanitize_xui_operational_text(message or exc, panel=panel)
        structured = safe_error_dict(
            exc,
            error_code=category or "panel_test_connection_failed",
            layer="panel_login",
            action="test_connection",
            message="تست اتصال پنل ناموفق بود.",
            remediation=(metadata or {}).get("remediation_hint") or "credentialها، CSRF/2FA و دسترسی شبکه پنل را بررسی کنید.",
            panel=panel,
            safe_context=sanitize_capability_metadata({"error_code": category, **(metadata or {})}),
        )
        return PanelActionResult(
            False,
            "تست اتصال ناموفق بود",
            safe_message,
            details=sanitize_capability_metadata({"error_code": category, **(metadata or {})}),
            errors=[safe_message],
            structured_errors=[structured],
        )

    return PanelActionResult(
        True,
        "تست اتصال موفق بود",
        "ورود و خواندن API با موفقیت انجام شد.",
        details={
            "login_ok": bool(login_ok),
            "read_api_ok": True,
            "remote_inbounds": len(read_inbounds),
            "detected_version": report.detected_version or "-",
            "capability_profile": report.capability_profile or "-",
        },
        warnings=list(report.warnings),
        errors=list(report.errors),
    )


def sync_panel_inbounds(panel, *, create_missing=True, available_for_new_orders=True, active_only=True):
    adapter = get_safe_panel_adapter(panel)
    if getattr(adapter, "family", "") == Panel.Family.PASARGUARD:
        try:
            report = adapter.detect_capabilities(live=True, write=False)
            remote_groups = adapter.list_inbounds()
            created = 0
            updated = 0
            skipped = 0
            remote_group_ids = []
            for group in remote_groups:
                group_id = group.get("id")
                if group_id is None:
                    skipped += 1
                    continue
                remote_group_ids.append(group_id)
                exists = Inbound.objects.filter(panel=panel, xui_node_id="", inbound_id=group_id).exists()
                if not exists and not create_missing:
                    skipped += 1
                    continue
                disabled_known = group.get("is_disabled") is not None
                group_disabled = group.get("is_disabled") is True
                group_sellable = bool(available_for_new_orders and disabled_known and not group_disabled)
                defaults = {
                    "remark": group.get("name") or f"PasarGuard group {group_id}",
                    "protocol": Inbound.Protocol.VLESS,
                    "server_ip": "pasarguard-native",
                    "port": "0",
                    "config_params": "{}",
                    "is_active": bool(disabled_known and not group_disabled),
                    "available_for_new_orders": group_sellable,
                    "health_monitor_enabled": True,
                    "last_synced_at": timezone.now(),
                    "xui_node_id": "",
                    "xui_node_name": "",
                    "xui_source": Inbound.XUISource.PASARGUARD_GROUP,
                    "xui_remote_key": f"pasarguard_group:{group_id}",
                    "is_synced_from_node": False,
                    "metadata": {
                        "remote_kind": "pasarguard_group",
                        "group_id": group_id,
                        "group_name": group.get("name") or "",
                        "inbound_tags": group.get("inbound_tags") or [],
                        "inbound_tag_count": len(group.get("inbound_tags") or []),
                        "is_disabled": group.get("is_disabled"),
                        "disabled_known": disabled_known,
                        "inbound_tags_known": bool(group.get("inbound_tags_known", True)),
                        "remote_source": group.get("source") or "",
                        "native_raw_delivery": True,
                    },
                }
                _inbound, was_created = Inbound.objects.update_or_create(
                    panel=panel,
                    xui_node_id="",
                    inbound_id=group_id,
                    defaults=defaults,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
            stale_count = 0
            stale_qs = Inbound.objects.filter(panel=panel, xui_source=Inbound.XUISource.PASARGUARD_GROUP)
            if remote_group_ids:
                stale_qs = stale_qs.exclude(inbound_id__in=remote_group_ids)
            for stale in stale_qs:
                metadata = dict(stale.metadata or {})
                metadata.update(
                    {
                        "remote_kind": "pasarguard_group",
                        "stale": True,
                        "stale_marked_at": timezone.now().isoformat(),
                        "native_raw_delivery": True,
                    }
                )
                stale.is_active = False
                stale.available_for_new_orders = False
                stale.metadata = metadata
                stale.last_synced_at = timezone.now()
                stale.save(update_fields=["is_active", "available_for_new_orders", "metadata", "last_synced_at", "updated_at"])
                stale_count += 1
            panel.capability_profile = Panel.CapabilityProfile.PASARGUARD_GROUPS
            panel.detected_xui_version = ""
            panel.last_sync_at = timezone.now()
            panel.capability_metadata = {
                "family": Panel.Family.PASARGUARD,
                "group_count": len(remote_groups),
                "native_raw_delivery": True,
                "report": report.to_dict(),
            }
            panel.save(
                update_fields=[
                    "capability_profile",
                    "detected_xui_version",
                    "last_sync_at",
                    "capability_metadata",
                    "updated_at",
                ]
            )
        except Exception as exc:
            structured = safe_error_dict(
                exc,
                error_code="pasarguard_group_sync_failed",
                layer="pasarguard_read",
                action="sync_groups",
                message="همگام‌سازی گروه‌های PasarGuard ناموفق بود.",
                remediation="Test connection را اجرا کنید و دسترسی API key به /api/groups را بررسی کنید.",
                panel=panel,
            )
            safe_message = structured.get("message") or "همگام‌سازی گروه‌های PasarGuard ناموفق بود."
            return PanelActionResult(
                False,
                "همگام‌سازی ناموفق بود",
                safe_message,
                errors=[safe_message],
                structured_errors=[structured],
            )

        return PanelActionResult(
            True,
            "همگام‌سازی انجام شد",
            "گروه‌های PasarGuard در منابع local ذخیره شدند.",
            details={
                "profile": Panel.CapabilityProfile.PASARGUARD_GROUPS,
                "remote_groups": len(remote_groups),
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "stale": stale_count,
                "native_raw_delivery": True,
            },
            warnings=list(report.warnings),
            errors=list(report.errors),
        )

    if getattr(adapter, "family", "") != Panel.Family.XUI:
        report = adapter.get_capability_report()
        message = "همگام‌سازی برای این خانواده پنل هنوز پشتیبانی نمی‌شود."
        structured = safe_error_dict(
            PanelIntegrationError(
                message,
                error_code="unsupported_panel_family",
                layer="adapter_factory",
                action="sync_inbounds",
                technical_detail="; ".join(report.errors or report.warnings),
                remediation="برای Sync inbounds فعلاً پنل X-UI انتخاب کنید یا adapter خانواده پنل را تکمیل کنید.",
                panel=panel,
                panel_family=report.family,
                capability_profile=report.capability_profile,
                safe_context={"capability_report": report.to_dict()},
            )
        )
        return PanelActionResult(
            False,
            "همگام‌سازی پشتیبانی نمی‌شود",
            message,
            details=report.to_dict(),
            errors=[message],
            structured_errors=[structured],
        )

    command = SyncXUITopologyCommand()
    service = XUIService(panel)
    try:
        profile = discover_xui_capabilities(panel, live=True, service=service, write=True, use_cache=False)
        remote_inbounds, remote_nodes = command.fetch_topology(service, panel)
        if active_only:
            remote_inbounds = [item for item in remote_inbounds if item.get("active") is not False]
        planned = command.plan_updates(panel, remote_inbounds)
        updated = command.apply_updates(planned)
        created = 0
        if create_missing:
            created = command.create_missing_inbounds(
                panel,
                planned,
                available_for_new_orders=available_for_new_orders,
                active_only=active_only,
            )
            if created:
                planned = command.plan_updates(panel, remote_inbounds)
        panel.last_sync_at = timezone.now()
        panel.save(update_fields=["last_sync_at", "updated_at"])
    except Exception as exc:
        safe_error = sanitize_xui_operational_text(exc, panel=panel)
        return PanelActionResult(
            False,
            "همگام‌سازی ناموفق بود",
            safe_error,
            errors=[safe_error],
            structured_errors=[
                safe_error_dict(
                    exc,
                    error_code="panel_read_failed",
                    layer="panel_read",
                    action="sync_inbounds",
                    message="همگام‌سازی اینباندهای پنل ناموفق بود.",
                    remediation="Test connection را اجرا کنید و دسترسی API خواندنی پنل را بررسی کنید.",
                    panel=panel,
                )
            ],
        )

    protocols = {
        str(item.get("protocol") or "").lower()
        for item in remote_inbounds
        if str(item.get("protocol") or "").strip()
    }
    unsupported_protocols = sorted(protocol for protocol in protocols if protocol not in SUPPORTED_PROTOCOLS)
    skipped = planned.get("skipped") or []
    return PanelActionResult(
        True,
        "همگام‌سازی انجام شد",
        "اطلاعات local inboundها با API خواندنی پنل به‌روزرسانی شد.",
        details={
            "profile": profile.profile,
            "version": profile.version or "-",
            "remote_inbounds": len(remote_inbounds),
            "remote_nodes": len(remote_nodes),
            "created": created,
            "updated": updated,
            "skipped": len(skipped),
            "unsupported_protocols": unsupported_protocols,
        },
        warnings=[str(item.get("reason") or "") for item in skipped[:10] if item.get("reason")],
    )


def inbound_status(inbound):
    warnings = []
    panel = getattr(inbound, "panel", None)
    is_pasarguard = is_pasarguard_panel(panel)
    if not inbound.is_active:
        warnings.append("گروه غیرفعال است." if is_pasarguard else "اینباند غیرفعال است.")
    if not inbound.available_for_new_orders:
        warnings.append("برای فروش جدید فعال نیست.")
    if panel and not panel.is_active:
        warnings.append("پنل متصل غیرفعال است.")
    if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
        warnings.append("ظرفیت تکمیل شده است.")
    protocol = str(inbound.protocol or "").lower()
    if not is_pasarguard and protocol and protocol not in SUPPORTED_PROTOCOLS:
        warnings.append("پروتکل برای لینک مستقیم استاندارد پشتیبانی نشده است.")
    return warnings


def inbound_rows(panel):
    rows = []
    labels = target_labels_for_panel(panel)
    for inbound in Inbound.objects.filter(panel=panel).order_by("-is_active", "inbound_id", "pk"):
        warnings = inbound_status(inbound)
        metadata = inbound.metadata or {}
        is_pasarguard = is_pasarguard_panel(panel)
        rows.append(
            {
                "inbound": inbound,
                "target_labels": labels,
                "target_kind": "pasarguard_group" if is_pasarguard else "xui_inbound",
                "warnings": warnings,
                "tone": "amber" if warnings else "emerald",
                "sellable": bool(inbound.is_active and inbound.available_for_new_orders and not warnings),
                "remote_id_label": metadata.get("group_id") if is_pasarguard else inbound.inbound_id,
                "protocol_label": "native raw" if is_pasarguard else inbound.protocol,
                "host_port_label": f"{metadata.get('inbound_tag_count', 0)} tag" if is_pasarguard else f"{inbound.server_ip}:{inbound.port}",
                "node_label": "disabled" if metadata.get("is_disabled") else "unknown" if is_pasarguard and metadata.get("disabled_known") is False else "active" if is_pasarguard else inbound.xui_node_name or inbound.xui_node_id or "local",
                "link_support": "native raw" if is_pasarguard else "پشتیبانی می‌شود" if str(inbound.protocol or "").lower() in SUPPORTED_PROTOCOLS else "نامشخص",
            }
        )
    return rows
