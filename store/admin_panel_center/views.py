from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from store.admin_panel_center.forms import PanelCenterForm
from store.admin_panel_center.services import (
    build_test_connection_result,
    capability_items,
    get_panel_queryset,
    inbound_rows,
    panel_action_urls,
    panel_health_alert_detail,
    panel_health_alert_summary,
    panel_list_items,
    safe_capability_report,
    structured_errors_for_capability_report,
    sync_panel_inbounds,
)
from store.models import Panel
from store.panel_health_services import check_all_panels_health, check_panel_health


def _require_panel_permission(request, perm="store.view_panel"):
    if not request.user.has_perm(perm):
        raise PermissionDenied


def _base_context(request, title):
    return {
        "title": title,
        "site_title": "VPN Store Admin",
        "site_header": "VPN Store Administration",
    }


def _panel_result_payload(result):
    return {
        "ok": result.ok,
        "title": result.title,
        "message": result.message,
        "details": result.details,
        "warnings": result.warnings,
        "errors": result.errors,
        "structured_errors": getattr(result, "structured_errors", []) or [],
    }


def panel_center_index(request):
    _require_panel_permission(request)
    context = {
        **_base_context(request, "مرکز اتصال پنل‌ها"),
        "items": panel_list_items(),
        "alert_summary": panel_health_alert_summary(),
        "add_url": reverse("admin_store_panel_center_new"),
        "panel_admin_url": reverse("admin:store_panel_changelist"),
    }
    return TemplateResponse(request, "admin/store/panel_center/index.html", context)


def panel_center_form(request, panel_id=None):
    perm = "store.change_panel" if panel_id else "store.add_panel"
    _require_panel_permission(request, perm)
    panel = get_object_or_404(Panel, pk=panel_id) if panel_id else None
    if request.method == "POST":
        form = PanelCenterForm(request.POST, instance=panel)
        if form.is_valid():
            saved = form.save()
            if saved.family == Panel.Family.XUI and saved.is_active:
                result = sync_panel_inbounds(saved, create_missing=True, available_for_new_orders=True, active_only=True)
                if result.ok:
                    created = int(result.details.get("created") or 0)
                    updated = int(result.details.get("updated") or 0)
                    messages.success(request, f"پنل ذخیره شد و اینباندها همگام شدند. جدید: {created}، به‌روزرسانی: {updated}.")
                    request.session["panel_center_last_result"] = _panel_result_payload(result)
                else:
                    messages.warning(request, f"پنل ذخیره شد، اما دریافت اینباندها ناموفق بود: {result.message}")
                    request.session["panel_center_last_result"] = _panel_result_payload(result)
            else:
                messages.success(request, "پنل ذخیره شد.")
            return redirect("admin_store_panel_center_detail", saved.pk)
    else:
        form = PanelCenterForm(instance=panel)

    context = {
        **_base_context(request, "ویرایش پنل" if panel else "افزودن پنل"),
        "form": form,
        "panel": panel,
        "cancel_url": reverse("admin_store_panel_center_detail", args=[panel.pk]) if panel else reverse("admin_store_panel_center"),
    }
    return TemplateResponse(request, "admin/store/panel_center/panel_form.html", context)


def panel_center_detail(request, panel_id):
    _require_panel_permission(request)
    panel = get_object_or_404(get_panel_queryset(), pk=panel_id)
    adapter, report, report_dict = safe_capability_report(panel)
    rows = inbound_rows(panel)
    last_result = request.session.pop("panel_center_last_result", None)
    context = {
        **_base_context(request, "جزئیات پنل"),
        "panel": panel,
        "actions": panel_action_urls(panel),
        "adapter_family": getattr(adapter, "family", report.family),
        "report": report,
        "report_dict": report_dict,
        "report_structured_errors": structured_errors_for_capability_report(panel, report),
        "capability_items": capability_items(report),
        "alert_detail": panel_health_alert_detail(panel),
        "inbound_rows": rows[:8],
        "inbound_count": len(rows),
        "last_result": last_result,
    }
    return TemplateResponse(request, "admin/store/panel_center/panel_detail.html", context)


def panel_center_capabilities(request, panel_id):
    _require_panel_permission(request)
    panel = get_object_or_404(get_panel_queryset(), pk=panel_id)
    _adapter, report, report_dict = safe_capability_report(panel)
    context = {
        **_base_context(request, "گزارش قابلیت پنل"),
        "panel": panel,
        "actions": panel_action_urls(panel),
        "report": report,
        "report_dict": report_dict,
        "report_structured_errors": structured_errors_for_capability_report(panel, report),
        "capability_items": capability_items(report),
    }
    return TemplateResponse(request, "admin/store/panel_center/capability_report.html", context)


def panel_center_inbounds(request, panel_id):
    _require_panel_permission(request)
    panel = get_object_or_404(get_panel_queryset(), pk=panel_id)
    context = {
        **_base_context(request, "کاوشگر اینباندها"),
        "panel": panel,
        "actions": panel_action_urls(panel),
        "inbound_rows": inbound_rows(panel),
    }
    return TemplateResponse(request, "admin/store/panel_center/inbound_explorer.html", context)


@require_POST
def panel_center_alerts_dry_run(request):
    _require_panel_permission(request, "store.view_panel")
    summary = check_all_panels_health(send_alerts=True, dry_run=True, active_only=True)
    messages.info(
        request,
        (
            "Dry-run هشدار سلامت پنل‌ها انجام شد: "
            f"checked={summary['checked']} ok={summary['ok']} warning={summary['warning']} "
            f"error={summary['error']} would_send={summary['would_send']} alerts_sent=0"
        ),
    )
    return redirect("admin_store_panel_center")


@require_POST
def panel_center_panel_alerts_dry_run(request, panel_id):
    _require_panel_permission(request, "store.view_panel")
    panel = get_object_or_404(Panel, pk=panel_id)
    result = check_panel_health(panel, send_alerts=True, dry_run=True)
    messages.info(
        request,
        (
            "Dry-run هشدار این پنل انجام شد: "
            f"status={result.get('status') or '-'} "
            f"would_send={bool(result.get('would_send_alert'))} "
            f"skip_reason={result.get('alert_skip_reason') or '-'} "
            f"alerts_sent=0"
        ),
    )
    return redirect("admin_store_panel_center_detail", panel.pk)


@require_POST
def panel_center_panel_alert_enable(request, panel_id):
    _require_panel_permission(request, "store.change_panel")
    panel = get_object_or_404(Panel, pk=panel_id)
    panel.health_alert_enabled = True
    panel.save(update_fields=["health_alert_enabled", "updated_at"])
    messages.success(request, "هشدار سلامت برای این پنل فعال شد.")
    return redirect("admin_store_panel_center_detail", panel.pk)


@require_POST
def panel_center_panel_alert_disable(request, panel_id):
    _require_panel_permission(request, "store.change_panel")
    panel = get_object_or_404(Panel, pk=panel_id)
    panel.health_alert_enabled = False
    panel.save(update_fields=["health_alert_enabled", "updated_at"])
    messages.success(request, "هشدار سلامت برای این پنل غیرفعال شد.")
    return redirect("admin_store_panel_center_detail", panel.pk)


@require_POST
def panel_center_test(request, panel_id):
    _require_panel_permission(request, "store.view_panel")
    panel = get_object_or_404(Panel, pk=panel_id)
    result = build_test_connection_result(panel)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.warning(request, result.message)
    request.session["panel_center_last_result"] = _panel_result_payload(result)
    return redirect("admin_store_panel_center_detail", panel.pk)


@require_POST
def panel_center_sync(request, panel_id):
    _require_panel_permission(request, "store.change_inbound")
    panel = get_object_or_404(Panel, pk=panel_id)
    result = sync_panel_inbounds(panel)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.warning(request, result.message)
    request.session["panel_center_last_result"] = _panel_result_payload(result)
    return redirect("admin_store_panel_center_detail", panel.pk)
