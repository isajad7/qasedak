from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from store.admin_panel_center.routing_forms import PlanRoutingForm, ROUTE_MODE_MULTI, ROUTE_MODE_NONE, ROUTE_MODE_SINGLE
from store.admin_panel_center.routing_services import (
    apply_routing,
    dry_run_test_provisioning,
    inbound_options,
    panel_options,
    plan_index_items,
    plan_summary,
    preview_routing,
    plan_routing_summary,
)
from store.admin_panel_center.routing_test_services import run_safe_route_provisioning_test
from store.models import Plan


def _require_route_permission(request, perm="store.view_plan"):
    if not request.user.has_perm(perm):
        raise PermissionDenied


def _selected_values(form):
    mode = form.cleaned_data["delivery_mode"]
    panel = form.cleaned_data.get("panel")
    inbound = form.cleaned_data.get("inbound") if mode == ROUTE_MODE_SINGLE else None
    inbounds = list(form.cleaned_data.get("inbounds") or []) if mode == ROUTE_MODE_MULTI else []
    return mode, panel, inbound, inbounds


def _initial_for_plan(plan):
    summary = plan_routing_summary(plan)
    routes = summary["routes"]
    panel = summary["panel"]
    mode = summary["mode"]
    initial = {"delivery_mode": mode if mode in {ROUTE_MODE_NONE, ROUTE_MODE_SINGLE, ROUTE_MODE_MULTI} else ROUTE_MODE_NONE}
    if panel:
        initial["panel"] = panel.pk
    if mode == ROUTE_MODE_SINGLE and routes:
        initial["inbound"] = routes[0].inbound_id
    if mode == ROUTE_MODE_MULTI:
        initial["inbounds"] = [route.inbound_id for route in routes]
    return initial, panel


def routing_index(request):
    _require_route_permission(request)
    context = {
        "title": "تنظیم مسیر فروش پلن‌ها",
        "items": plan_index_items(),
    }
    return TemplateResponse(request, "admin/store/panel_center/plan_routing_index.html", context)


def routing_detail(request, plan_id):
    _require_route_permission(request)
    plan = get_object_or_404(Plan.objects.select_related("store"), pk=plan_id)
    initial, selected_panel = _initial_for_plan(plan)
    result = None
    if request.method == "POST":
        form = PlanRoutingForm(request.POST, store=plan.store, initial_panel=request.POST.get("panel"))
        if form.is_valid():
            mode, panel, inbound, inbounds = _selected_values(form)
            action = request.POST.get("action") or "preview"
            if action == "save":
                _require_route_permission(request, "store.change_planinboundroute")
                result = apply_routing(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
                if result.ok:
                    messages.success(request, result.message)
                    return redirect("admin_store_panel_center_routing_detail", plan.pk)
                messages.error(request, result.message)
            elif action == "test":
                result = dry_run_test_provisioning(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
                return _render_routing_result(request, plan, form, result, template="admin/store/panel_center/plan_routing_test_result.html")
            elif action == "live_test":
                _require_route_permission(request, "store.change_planinboundroute")
                result = run_safe_route_provisioning_test(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
                return _render_routing_result(request, plan, form, result, template="admin/store/panel_center/plan_routing_live_test_result.html")
            else:
                result = preview_routing(plan=plan, mode=mode, panel=panel, inbound=inbound, inbounds=inbounds)
                return _render_routing_result(request, plan, form, result, template="admin/store/panel_center/plan_routing_preview.html")
        else:
            messages.error(request, "فرم تنظیم route معتبر نیست.")
    else:
        form = PlanRoutingForm(store=plan.store, initial=initial, initial_panel=selected_panel)

    return _render_routing_detail(request, plan, form, result)


def _render_routing_detail(request, plan, form, result=None):
    selected_panel = form.fields["panel"].queryset.filter(pk=form["panel"].value()).first() if form["panel"].value() else None
    context = {
        "title": "تنظیم مسیر فروش",
        "plan": plan,
        "plan_summary": plan_summary(plan),
        "routing_summary": plan_routing_summary(plan),
        "form": form,
        "panel_options": panel_options(plan.store),
        "inbound_options": inbound_options(selected_panel),
        "result": result,
        "routing_index_url": reverse("admin_store_panel_center_routing"),
    }
    return TemplateResponse(request, "admin/store/panel_center/plan_routing_detail.html", context)


def _render_routing_result(request, plan, form, result, *, template):
    selected_panel = form.cleaned_data.get("panel") if form.is_valid() else None
    context = {
        "title": "تست ساخت واقعی" if "live_test" in template else "پیش‌نمایش ساخت" if "preview" in template else "تست ساخت آزمایشی",
        "plan": plan,
        "plan_summary": plan_summary(plan),
        "routing_summary": plan_routing_summary(plan),
        "form": form,
        "panel_options": panel_options(plan.store),
        "inbound_options": inbound_options(selected_panel),
        "result": result,
        "routing_index_url": reverse("admin_store_panel_center_routing"),
    }
    return TemplateResponse(request, template, context)
