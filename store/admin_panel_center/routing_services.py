from __future__ import annotations

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse

from store.admin_catalog import get_plan_route_status, money_label, plan_duration_label, plan_volume_label
from store.admin_panel_center.routing_forms import ROUTE_MODE_MULTI, ROUTE_MODE_NONE, ROUTE_MODE_SINGLE
from store.models import Inbound, Order, Panel, Plan, PlanInboundRoute, VPNClient
from store.panels import get_safe_panel_adapter
from store.panels.errors import RoutingValidationError
from store.source_sellability import source_sellability_issues
from store.plan_route_services import (
    BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
    active_routes_for_plan_operator,
    apply_bulk_plan_routes,
    preview_bulk_plan_routes,
    set_plan_single_inbound_mode,
)


SUPPORTED_ROUTE_PROTOCOLS = {"vless", "vmess", "trojan"}


def _is_pasarguard_panel(panel):
    return str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD


@dataclass
class RoutingOperationResult:
    ok: bool
    mode: str
    message: str
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    structured_errors: list[dict] = field(default_factory=list)


def plan_routing_url(plan):
    return reverse("admin_store_panel_center_routing_detail", args=[plan.pk])


def route_mode_for_plan(plan, routes=None):
    routes = list(routes if routes is not None else current_active_routes(plan))
    if not routes:
        return ROUTE_MODE_NONE
    if getattr(plan, "multi_inbound_bundle", False):
        return ROUTE_MODE_MULTI if len(routes) > 1 else "invalid"
    return ROUTE_MODE_SINGLE if len(routes) == 1 else "mixed"


def current_active_routes(plan):
    return list(active_routes_for_plan_operator(plan, None, store=getattr(plan, "store", None)))


def panel_report(panel):
    adapter = get_safe_panel_adapter(panel)
    return adapter.get_capability_report()


def route_panel_for_routes(routes):
    panel_ids = {route.inbound.panel_id for route in routes if route.inbound_id and route.inbound.panel_id}
    if len(panel_ids) == 1:
        return routes[0].inbound.panel
    return None


def plan_routing_summary(plan):
    routes = current_active_routes(plan)
    panel = route_panel_for_routes(routes)
    report = panel_report(panel) if panel else None
    status = get_plan_route_status(plan, getattr(plan, "store", None))
    return {
        "plan": plan,
        "routes": routes,
        "route_count": len(routes),
        "mode": route_mode_for_plan(plan, routes),
        "status": status,
        "panel": panel,
        "report": report,
        "routing_url": plan_routing_url(plan),
        "inbound_ids": [route.inbound_id for route in routes],
        "remote_inbound_ids": [route.inbound.inbound_id for route in routes if route.inbound_id],
    }


def plan_index_items():
    plans = (
        Plan.objects.select_related("store")
        .prefetch_related("inbound_routes__inbound__panel")
        .order_by("store__name", "sort_order", "price", "pk")
    )
    return [plan_routing_summary(plan) for plan in plans]


def plan_summary(plan):
    return {
        "title": plan.name,
        "volume": plan_volume_label(plan),
        "duration": plan_duration_label(plan),
        "price": money_label(plan.price, plan.currency),
        "is_active": plan.is_active,
        "is_public": plan.is_public,
        "device_limit": plan.device_limit,
        "multi_inbound_bundle": plan.multi_inbound_bundle,
    }


def panel_options(store=None):
    panels = Panel.objects.filter(is_active=True).select_related("store").order_by("store__name", "name", "pk")
    if store:
        panels = panels.filter(Q(store=store) | Q(store__isnull=True))
    options = []
    for panel in panels:
        report = panel_report(panel)
        options.append(
            {
                "panel": panel,
                "report": report,
                "supported": report.supports_create_client,
                "label": f"{panel.name} / {report.family} / {report.capability_profile or '-'} / {report.detected_version or '-'}",
            }
        )
    return options


def inbound_options(panel):
    if not panel:
        return []
    rows = []
    for inbound in Inbound.objects.filter(panel=panel).order_by("inbound_id", "pk"):
        errors, warnings = validate_inbound_for_routing(inbound, panel=panel, require_panel_capability=False)
        rows.append(
            {
                "inbound": inbound,
                "errors": errors,
                "warnings": warnings,
                "usable": not errors,
                "active_plan_routes": list(
                    inbound.plan_routes.filter(is_active=True).select_related("plan").order_by("plan__sort_order", "plan__price", "pk")[:8]
                ),
            }
        )
    return rows


def validate_inbound_for_routing(inbound, *, panel=None, require_panel_capability=True):
    errors = []
    warnings = []
    if not inbound:
        return ["اینباند انتخاب نشده است. Inbound is required."], warnings
    if panel and inbound.panel_id != panel.pk:
        errors.append("اینباند انتخاب‌شده به پنل انتخاب‌شده وصل نیست. Inbound does not belong to the selected panel.")
    if not inbound.is_active:
        errors.append("اینباند غیرفعال است. Inbound is inactive.")
    if not inbound.available_for_new_orders:
        errors.append("اینباند برای فروش جدید فعال نیست. Inbound is not sellable.")
    if inbound.legacy_note:
        errors.append("اینباند legacy علامت‌گذاری شده است. Inbound is marked as legacy.")
    if not inbound.panel_id:
        errors.append("اینباند پنل ندارد. Inbound has no panel.")
    elif not inbound.panel.is_active:
        errors.append("پنل اینباند غیرفعال است. Inbound panel is inactive.")
    protocol = str(inbound.protocol or "").lower()
    if not _is_pasarguard_panel(getattr(inbound, "panel", None)) and protocol not in SUPPORTED_ROUTE_PROTOCOLS:
        errors.append("پروتکل اینباند در Routing Builder پشتیبانی نمی‌شود. Inbound protocol is not supported by the routing builder.")
    verification_errors, verification_warnings = source_sellability_issues(inbound)
    errors.extend(verification_errors)
    warnings.extend(verification_warnings)
    if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
        warnings.append("Inbound capacity is currently full.")
    if require_panel_capability and inbound.panel_id:
        report = panel_report(inbound.panel)
        if not report.supports_create_client:
            errors.append("پنل قابلیت ساخت client ندارد. Panel does not support client creation.")
    return errors, warnings


def validate_routing_selection(*, plan, mode, panel=None, inbound=None, inbounds=None):
    errors = []
    warnings = []
    selected = list(inbounds or [])

    if mode == ROUTE_MODE_NONE:
        return errors, warnings

    if not panel:
        errors.append("پنل مقصد انتخاب نشده است. Panel is required.")
        return errors, warnings
    report = panel_report(panel)
    if not report.supports_create_client:
        errors.append("پنل انتخاب‌شده قابلیت supports_create_client ندارد. Selected panel does not support client creation.")

    if mode == ROUTE_MODE_SINGLE:
        if not inbound:
            errors.append("برای حالت single دقیقاً یک اینباند انتخاب کنید. Exactly one inbound is required for single mode.")
        if selected:
            errors.append("در حالت single از فیلد تک‌اینباندی استفاده کنید. Use the single inbound field for single mode.")
        if inbound:
            inbound_errors, inbound_warnings = validate_inbound_for_routing(inbound, panel=panel)
            errors.extend(inbound_errors)
            warnings.extend(inbound_warnings)
        return errors, warnings

    if mode == ROUTE_MODE_MULTI:
        if inbound:
            errors.append("در حالت multi فیلد تک‌اینباندی باید خالی باشد. Single inbound field must be empty for multi mode.")
        if len(selected) < 2:
            errors.append("برای حالت multi حداقل دو اینباند انتخاب کنید. At least two inbounds are required for multi mode.")
        panel_ids = {item.panel_id for item in selected}
        if len(panel_ids) > 1:
            errors.append("اینباندهای انتخاب‌شده از چند پنل هستند. این مورد فقط در Multi-panel Builder مجاز است، نه در single-panel route builder. All selected inbounds must belong to the same panel.")
        if panel.pk not in panel_ids and selected:
            errors.append("اینباندهای انتخاب‌شده به پنل انتخاب‌شده وصل نیستند. Selected inbounds do not belong to the selected panel.")
        supports_multi_group = _is_pasarguard_panel(panel) and getattr(report, "supports_multi_group_users", False)
        if not report.supports_multi_inbound_create and not supports_multi_group:
            errors.append("پنل انتخاب‌شده قابلیت supports_multi_inbound_create ندارد. Selected panel does not support multi-inbound create.")
        if not supports_multi_group and panel.capability_profile != Panel.CapabilityProfile.MODERN_MULTI_NODE:
            errors.append("حالت multi به capability profile modern_multi_node نیاز دارد. Multi mode requires modern_multi_node capability profile.")
        remote_ids = [str(item.inbound_id) for item in selected]
        if len(set(remote_ids)) != len(remote_ids):
            errors.append("Inbound ID ریموت تکراری داخل این builder مجاز نیست. Duplicate remote inbound IDs are not allowed in this builder.")
        for item in selected:
            inbound_errors, inbound_warnings = validate_inbound_for_routing(item, panel=panel)
            errors.extend(f"Inbound #{item.pk}: {message}" for message in inbound_errors)
            warnings.extend(f"Inbound #{item.pk}: {message}" for message in inbound_warnings)
        return errors, warnings

    errors.append("حالت تحویل شناخته نشد. Unknown delivery mode.")
    return errors, warnings


def _routing_error_code(message):
    text = str(message or "").lower()
    if "supports_create_client" in text or "client creation" in text:
        return "panel_capability_missing"
    if "supports_multi_inbound_create" in text or "multi-inbound" in text or "modern_multi_node" in text:
        return "panel_capability_missing"
    if "same panel" in text or "پنل" in text and "چند" in text:
        return "mixed_panel_selection"
    if "inbound" in text or "اینباند" in text:
        return "inbound_validation_failed"
    if "panel is required" in text or "پنل مقصد" in text:
        return "panel_required"
    return "routing_validation_failed"


def _routing_remediation(message):
    text = str(message or "").lower()
    if "sync capabilities" in text or "supports_" in text or "client creation" in text:
        return "از Panel Center گزینه Test connection / Sync capabilities را اجرا کنید یا پنل X-UI با قابلیت لازم انتخاب کنید."
    if "same panel" in text or "چند پنل" in text:
        return "برای route تک‌پنلی فقط اینباندهای همان پنل را انتخاب کنید؛ برای چند پنل از Multi-panel Builder استفاده کنید."
    if "inbound" in text or "اینباند" in text:
        return "وضعیت active، available_for_new_orders، protocol و اتصال اینباند به پنل را بررسی کنید."
    return "انتخاب‌های route را اصلاح و دوباره Preview یا Save را اجرا کنید."


def _structured_routing_errors(errors, *, plan, mode, panel=None, inbound=None, inbounds=None):
    selected = [inbound] if inbound else list(inbounds or [])
    fallback_inbound = selected[0] if selected else None
    report = panel_report(panel) if panel else None
    return [
        RoutingValidationError(
            str(message),
            error_code=_routing_error_code(message),
            action="validate_route",
            technical_detail=str(message),
            remediation=_routing_remediation(message),
            panel=panel,
            panel_family=getattr(report, "family", "") if report else "",
            capability_profile=getattr(report, "capability_profile", "") if report else "",
            inbound=fallback_inbound,
            safe_context={
                "plan_id": getattr(plan, "pk", None),
                "plan_name": getattr(plan, "name", "") or "",
                "mode": mode,
                "selected_inbound_pks": [getattr(item, "pk", None) for item in selected if item],
                "remote_inbound_ids": [getattr(item, "inbound_id", None) for item in selected if item],
            },
        ).to_safe_dict()
        for message in errors
    ]


def preview_routing(*, plan, mode, panel=None, inbound=None, inbounds=None):
    errors, warnings = validate_routing_selection(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
    selected_inbounds = [inbound] if mode == ROUTE_MODE_SINGLE and inbound else list(inbounds or [])
    report = panel_report(panel) if panel else None
    details = {
        "selected_inbound_pks": [item.pk for item in selected_inbounds],
        "remote_inbound_ids": [item.inbound_id for item in selected_inbounds],
        "panel_family": report.family if report else "",
        "capability_profile": report.capability_profile if report else "",
        "endpoint_category": "preview-only",
        "expected_mode": "multi group_ids create" if mode == ROUTE_MODE_MULTI and _is_pasarguard_panel(panel) else "multi inboundIds create" if mode == ROUTE_MODE_MULTI else "single create" if mode == ROUTE_MODE_SINGLE else "no route",
        "totalGB": str(plan.volume_gb),
        "duration_days": plan.duration_days,
        "limitIp": plan.device_limit,
        "subscription": bool(report and report.supports_subscription),
        "direct_links_expected": len(selected_inbounds),
        "order_created": Order.objects.count(),
        "vpn_clients_created": VPNClient.objects.count(),
    }
    if errors:
        return RoutingOperationResult(
            False,
            mode,
            "Preview has validation errors.",
            details=details,
            warnings=warnings,
            errors=errors,
            structured_errors=_structured_routing_errors(errors, plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds),
        )
    return RoutingOperationResult(True, mode, "Preview is ready. No remote write was performed.", details=details, warnings=warnings)


def apply_routing(*, plan, mode, panel=None, inbound=None, inbounds=None):
    errors, warnings = validate_routing_selection(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
    if errors:
        return RoutingOperationResult(
            False,
            mode,
            "Route was not saved.",
            warnings=warnings,
            errors=errors,
            structured_errors=_structured_routing_errors(errors, plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds),
        )
    if mode == ROUTE_MODE_NONE:
        return deactivate_plan_routes(plan)
    if mode == ROUTE_MODE_SINGLE:
        result = apply_bulk_plan_routes(
            store=plan.store,
            inbound=inbound,
            selected_plan_ids=[plan.pk],
            all_active=False,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
            note="Updated from Panel Integration Center routing builder.",
        )
    else:
        result = apply_bulk_plan_routes(
            store=plan.store,
            inbounds=list(inbounds or []),
            multi_inbound_bundle=True,
            selected_plan_ids=[plan.pk],
            all_active=False,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
            note="Updated from Panel Integration Center routing builder.",
        )
    if result.get("errors"):
        return RoutingOperationResult(
            False,
            mode,
            "Route was not saved.",
            details=result,
            warnings=result.get("warnings", []),
            errors=result["errors"],
            structured_errors=_structured_routing_errors(result["errors"], plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds),
        )
    return RoutingOperationResult(True, mode, "Route saved.", details=result, warnings=result.get("warnings", []))


def deactivate_plan_routes(plan):
    with transaction.atomic():
        set_plan_single_inbound_mode(plan)
        routes = list(active_routes_for_plan_operator(plan, None, store=plan.store))
        deactivated = 0
        for route in routes:
            route.is_active = False
            route.full_clean()
            route.save(update_fields=["is_active", "updated_at"])
            deactivated += 1
    return RoutingOperationResult(
        True,
        ROUTE_MODE_NONE,
        "Routes deactivated.",
        details={"created": 0, "updated": 0, "deactivated": deactivated, "skipped": 0},
    )


def dry_run_test_provisioning(*, plan, mode, panel=None, inbound=None, inbounds=None):
    preview = preview_routing(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
    preview.details.update(
        {
            "live_test": "deferred",
            "test_client_prefix": f"qasedak-route-test-{plan.pk}-<timestamp>",
            "cleanup_required": False,
            "remote_write_called": False,
            "uuid": "<masked>",
            "sub_id": "<masked>",
            "subscription_link": "<redacted>",
            "direct_link": "<redacted>",
        }
    )
    preview.message = "Dry-run test only. Live remote create/cleanup is intentionally deferred."
    return preview
