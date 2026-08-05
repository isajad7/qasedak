from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from store.admin_cup_center.forms import ExistingConfigLinkFilterForm, ManualCupForm, ManualLinksForm, PanelConfigIntoCupForm, QuickSubscriptionBuilderForm
from store.admin_cup_center.services import (
    CupCenterError,
    CupCenterRemoteCreateError,
    CupCenterRemoteSaveError,
    CupCenterValidationError,
    add_existing_links_to_cup,
    add_manual_links_to_cup,
    create_manual_cup,
    create_panel_config_into_cup,
    cup_item_rows,
    cup_list_items,
    cup_queryset,
    mask_link_for_display,
    quick_builder_inventory_pool_rows,
    quick_builder_panel_groups,
    quick_build_subscription_cup,
    rebuild_cup_from_source,
    render_cup_preview,
    set_cup_item_active,
    set_cup_status,
    move_cup_item,
    subscription_url_summary,
)
from store.models import ConfigLink, SubscriptionCup


def _require_perm(request, perm):
    if not request.user.has_perm(perm):
        raise PermissionDenied


def _base_context(request, title, cup=None):
    context = {
        **admin.site.each_context(request),
        "title": title,
        "cup": cup,
        "cup_center_url": reverse("admin_store_cup_center"),
        "new_cup_url": reverse("admin_store_cup_center_new"),
        "quick_build_url": reverse("admin_store_cup_center_quick_build"),
        "subscription_cup_admin_url": reverse("admin:store_subscriptioncup_changelist"),
    }
    return context


def _get_cup(cup_id):
    return get_object_or_404(cup_queryset(), pk=cup_id)


def cup_center_index(request):
    _require_perm(request, "store.view_subscriptioncup")
    cups = cup_queryset()
    status = str(request.GET.get("status") or "").strip()
    query = str(request.GET.get("q") or "").strip()
    if status:
        cups = cups.filter(status=status)
    if query:
        cups = cups.filter(title__icontains=query)
    context = {
        **_base_context(request, "مدیریت لینک‌های اشتراک"),
        "items": cup_list_items(cups[:100], request=request),
        "status": status,
        "query": query,
        "status_choices": SubscriptionCup.Status.choices,
    }
    return TemplateResponse(request, "admin/store/cup_center/index.html", context)


def _quick_result_session_key(cup_id):
    return f"cup_center_quick_result_{cup_id}"


def _store_quick_result(request, result):
    request.session[_quick_result_session_key(result.cup.pk)] = {
        "cup_id": result.cup.pk,
        "title": result.cup.title,
        "item_count": result.item_count,
        "status": result.status,
        "protocols": result.protocols,
        "selected_inbounds": result.selected_inbounds,
        "selected_inventory_pools": result.selected_inventory_pools,
        "selected_panels_count": result.selected_panels_count,
        "selected_inbounds_count": result.selected_inbounds_count,
        "selected_inventory_pools_count": result.selected_inventory_pools_count,
        "created_remote_client_groups_count": result.created_remote_client_groups_count,
        "inventory_allocation_count": result.inventory_allocation_count,
        "config_link_count": result.config_link_count,
        "panel_results": result.panel_results,
        "inventory_results": result.inventory_results,
        "masked_subscription_url": result.masked_subscription_url,
        "email_masked": result.email_masked,
        "warnings": result.warnings,
        "errors": result.errors,
        "structured_errors": result.structured_errors,
    }


def cup_center_quick_build(request):
    _require_perm(request, "store.add_subscriptioncup")
    _require_perm(request, "store.add_configlink")
    _require_perm(request, "store.add_cupitem")
    _require_perm(request, "store.view_panel")
    _require_perm(request, "store.view_inbound")
    if request.method == "POST":
        form = QuickSubscriptionBuilderForm(request.POST)
        structured_errors = []
        if form.is_valid():
            try:
                result = quick_build_subscription_cup(form.cleaned_data, admin_user=request.user, request=request)
                _store_quick_result(request, result)
                if result.status == "partial_success":
                    messages.warning(request, "Cup ساخته شد، اما بعضی پنل‌ها ناموفق بودند.")
                elif result.status == "failed":
                    messages.error(request, "Cup ساخته شد، اما هیچ لینک موفقی ذخیره نشد.")
                else:
                    messages.success(request, "لینک اشتراک سریع ساخته شد.")
                return redirect("admin_store_cup_center_quick_result", result.cup.pk)
            except (CupCenterValidationError, CupCenterRemoteCreateError, CupCenterRemoteSaveError) as exc:
                form.add_error(None, str(exc))
                if getattr(exc, "structured_error", None):
                    structured_errors.append(exc.structured_error)
    else:
        initial = {}
        selected_pool_ids = [value for value in request.GET.getlist("inventory_pool") if str(value).isdigit()]
        if not selected_pool_ids:
            pool_id = str(request.GET.get("pool") or "").strip()
            selected_pool_ids = [pool_id] if pool_id.isdigit() else []
        if selected_pool_ids:
            initial["inventory_pools"] = selected_pool_ids
        form = QuickSubscriptionBuilderForm(initial=initial)
        structured_errors = []
    if request.method == "POST":
        selected_inventory_pool_ids = {int(value) for value in request.POST.getlist("inventory_pools") if str(value).isdigit()}
    else:
        selected_inventory_pool_ids = {int(value) for value in request.GET.getlist("inventory_pool") if str(value).isdigit()}
        pool_id = str(request.GET.get("pool") or "").strip()
        if pool_id.isdigit():
            selected_inventory_pool_ids.add(int(pool_id))
    context = {
        **_base_context(request, "ساخت سریع لینک اشتراک"),
        "form": form,
        "panel_groups": quick_builder_panel_groups(),
        "inventory_pool_rows": quick_builder_inventory_pool_rows(),
        "selected_inbound_ids": {int(value) for value in request.POST.getlist("inbounds") if str(value).isdigit()},
        "selected_inventory_pool_ids": selected_inventory_pool_ids,
        "structured_errors": structured_errors,
        "cancel_url": reverse("admin_store_cup_center"),
    }
    return TemplateResponse(request, "admin/store/cup_center/quick_build.html", context)


def cup_center_quick_result(request, cup_id):
    _require_perm(request, "store.view_subscriptioncup")
    cup = _get_cup(cup_id)
    session_result = request.session.get(_quick_result_session_key(cup.pk)) or {}
    item_rows = cup_item_rows(cup)
    selected_inbounds = session_result.get("selected_inbounds") or [
        {
            "id": row["source_inbound"].pk if row["source_inbound"] else "",
            "label": str(row["source_inbound"] or "-"),
            "panel_name": str(row["source_panel"] or "-"),
            "remote_inbound_id": getattr(row["source_inbound"], "inbound_id", "") if row["source_inbound"] else "",
            "protocol": row["protocol"],
            "host": row["host"],
            "port": row["port"],
            "remark": row["remark"],
        }
        for row in item_rows
    ]
    fallback_panel_results = cup.metadata.get("panel_results") if isinstance(cup.metadata, dict) else None
    fallback_structured_errors = [
        error
        for panel_result in (fallback_panel_results or [])
        for error in ((panel_result.get("structured_errors") or []) if isinstance(panel_result, dict) else [])
    ]
    context = {
        **_base_context(request, "نتیجه ساخت سریع لینک اشتراک", cup=cup),
        "subscription": subscription_url_summary(cup, request=request),
        "result": {
            "title": session_result.get("title") or cup.title,
            "item_count": session_result.get("item_count") or len(item_rows),
            "status": session_result.get("status") or (cup.metadata.get("status") if isinstance(cup.metadata, dict) else "") or "success",
            "protocols": session_result.get("protocols") or render_cup_preview(cup)["protocols"],
            "selected_inbounds": selected_inbounds,
            "selected_panels_count": session_result.get("selected_panels_count") or (cup.metadata.get("selected_panel_count") if isinstance(cup.metadata, dict) else "") or "-",
            "selected_inbounds_count": session_result.get("selected_inbounds_count") or (cup.metadata.get("selected_inbound_count") if isinstance(cup.metadata, dict) else "") or len(selected_inbounds),
            "selected_inventory_pools": session_result.get("selected_inventory_pools") or [],
            "selected_inventory_pools_count": session_result.get("selected_inventory_pools_count") or (cup.metadata.get("selected_inventory_pool_count") if isinstance(cup.metadata, dict) else "") or 0,
            "created_remote_client_groups_count": session_result.get("created_remote_client_groups_count") or (cup.metadata.get("created_remote_client_groups_count") if isinstance(cup.metadata, dict) else "") or 0,
            "inventory_allocation_count": session_result.get("inventory_allocation_count") or (cup.metadata.get("inventory_allocation_count") if isinstance(cup.metadata, dict) else "") or 0,
            "config_link_count": session_result.get("config_link_count") or (cup.metadata.get("config_link_count") if isinstance(cup.metadata, dict) else "") or len(item_rows),
            "panel_results": session_result.get("panel_results") or fallback_panel_results or [],
            "inventory_results": session_result.get("inventory_results") or (cup.metadata.get("inventory_results") if isinstance(cup.metadata, dict) else "") or [],
            "masked_subscription_url": session_result.get("masked_subscription_url") or subscription_url_summary(cup, request=request)["masked_client_url"],
            "email_masked": session_result.get("email_masked") or "",
            "warnings": session_result.get("warnings") or [],
            "errors": session_result.get("errors") or [],
            "structured_errors": session_result.get("structured_errors") or fallback_structured_errors,
        },
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
        "preview_url": reverse("admin_store_cup_center_preview", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/quick_result.html", context)


def cup_center_new(request):
    _require_perm(request, "store.add_subscriptioncup")
    if request.method == "POST":
        form = ManualCupForm(request.POST)
        if form.is_valid():
            cup = create_manual_cup(**form.cleaned_data)
            messages.success(request, "Cup ساخته شد. لینک اشتراک در صفحه جزئیات به صورت masked نمایش داده می‌شود.")
            return redirect("admin_store_cup_center_detail", cup.pk)
    else:
        form = ManualCupForm()
    context = {
        **_base_context(request, "ساخت Subscription Cup"),
        "form": form,
        "cancel_url": reverse("admin_store_cup_center"),
    }
    return TemplateResponse(request, "admin/store/cup_center/form.html", context)


def cup_center_detail(request, cup_id):
    _require_perm(request, "store.view_subscriptioncup")
    cup = _get_cup(cup_id)
    if request.method == "POST":
        _require_perm(request, "store.change_subscriptioncup")
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "enable_cup":
                set_cup_status(cup, SubscriptionCup.Status.ACTIVE)
                messages.success(request, "Cup فعال شد.")
            elif action == "disable_cup":
                set_cup_status(cup, SubscriptionCup.Status.DISABLED)
                messages.success(request, "Cup غیرفعال شد.")
            elif action == "enable_item":
                set_cup_item_active(cup, request.POST.get("item_id"), True)
                messages.success(request, "لینک داخل Cup فعال شد.")
            elif action == "disable_item":
                set_cup_item_active(cup, request.POST.get("item_id"), False)
                messages.success(request, "لینک داخل Cup غیرفعال شد.")
            elif action == "move_item_up":
                move_cup_item(cup, request.POST.get("item_id"), "up")
            elif action == "move_item_down":
                move_cup_item(cup, request.POST.get("item_id"), "down")
            else:
                messages.warning(request, "Action معتبر نبود.")
        except Exception:
            messages.error(request, "Action انجام نشد.")
        return redirect("admin_store_cup_center_detail", cup.pk)

    context = {
        **_base_context(request, f"Cup #{cup.pk}", cup=cup),
        "subscription": subscription_url_summary(cup, request=request),
        "item_rows": cup_item_rows(cup),
        "active_item_count": cup.items.filter(is_active=True, config_link__is_active=True).count(),
        "inactive_item_count": cup.items.filter(is_active=False).count() + cup.items.filter(is_active=True, config_link__is_active=False).count(),
        "preview_url": reverse("admin_store_cup_center_preview", args=[cup.pk]),
        "add_existing_url": reverse("admin_store_cup_center_add_existing", args=[cup.pk]),
        "add_manual_url": reverse("admin_store_cup_center_add_manual", args=[cup.pk]),
        "create_from_inbound_url": reverse("admin_store_cup_center_create_from_inbound", args=[cup.pk]),
        "rebuild_url": reverse("admin_store_cup_center_rebuild", args=[cup.pk]),
        "change_url": reverse("admin:store_subscriptioncup_change", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/detail.html", context)


def cup_center_add_existing(request, cup_id):
    _require_perm(request, "store.change_subscriptioncup")
    _require_perm(request, "store.view_configlink")
    _require_perm(request, "store.add_cupitem")
    cup = _get_cup(cup_id)
    result = None
    if request.method == "POST":
        result = add_existing_links_to_cup(cup, request.POST.getlist("config_link_ids"))
        if result.added_count:
            messages.success(request, f"{result.added_count} لینک به Cup اضافه شد.")
        for warning in result.warnings:
            messages.warning(request, warning)
    form = ExistingConfigLinkFilterForm(request.GET)
    links = form.filter_queryset(
        ConfigLink.objects.select_related("source_panel", "source_inbound", "vpn_client").order_by("-created_at", "-pk")
    )[:100]
    existing_hashes = set(
        cup.items.select_related("config_link")
        .exclude(config_link__normalized_hash="")
        .values_list("config_link__normalized_hash", flat=True)
    )
    rows = [
        {
            "link": link,
            "masked_link": mask_link_for_display(link),
            "already_exists": bool(link.normalized_hash and link.normalized_hash in existing_hashes),
        }
        for link in links
    ]
    context = {
        **_base_context(request, "افزودن لینک‌های موجود", cup=cup),
        "form": form,
        "rows": rows,
        "result": result,
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/add_existing.html", context)


def cup_center_add_manual(request, cup_id):
    _require_perm(request, "store.change_subscriptioncup")
    _require_perm(request, "store.add_configlink")
    _require_perm(request, "store.add_cupitem")
    cup = _get_cup(cup_id)
    result = None
    if request.method == "POST":
        form = ManualLinksForm(request.POST)
        if form.is_valid():
            result = add_manual_links_to_cup(cup, form.cleaned_data["raw_links"])
            if result.added_count:
                messages.success(request, f"{result.added_count} لینک manual به Cup اضافه شد.")
            if result.skipped_invalid_count:
                messages.warning(request, f"{result.skipped_invalid_count} خط نامعتبر اضافه نشد.")
            for warning in result.warnings[:5]:
                messages.warning(request, warning)
            form = ManualLinksForm()
    else:
        form = ManualLinksForm()
    context = {
        **_base_context(request, "افزودن لینک دستی", cup=cup),
        "form": form,
        "result": result,
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/add_manual.html", context)


def cup_center_create_from_inbound(request, cup_id):
    _require_perm(request, "store.change_subscriptioncup")
    _require_perm(request, "store.add_configlink")
    _require_perm(request, "store.add_cupitem")
    _require_perm(request, "store.view_panel")
    _require_perm(request, "store.view_inbound")
    cup = _get_cup(cup_id)
    created_result = None
    if request.method == "POST":
        form = PanelConfigIntoCupForm(request.POST, cup=cup)
        structured_errors = []
        if form.is_valid():
            try:
                created_result = create_panel_config_into_cup(cup, form.cleaned_data["panel"], form.cleaned_data["inbound"], form.cleaned_data)
                messages.success(request, "کانفیگ واقعی ساخته و به Cup اضافه شد. خروجی فقط به صورت masked نمایش داده شد.")
                return redirect("admin_store_cup_center_detail", cup.pk)
            except CupCenterRemoteSaveError as exc:
                form.add_error(None, str(exc))
                if getattr(exc, "structured_error", None):
                    structured_errors.append(exc.structured_error)
            except CupCenterRemoteCreateError as exc:
                form.add_error(None, str(exc))
                if getattr(exc, "structured_error", None):
                    structured_errors.append(exc.structured_error)
    else:
        form = PanelConfigIntoCupForm(cup=cup)
        structured_errors = []
    context = {
        **_base_context(request, "ساخت کانفیگ از Panel/Inbound", cup=cup),
        "form": form,
        "created_result": created_result,
        "structured_errors": structured_errors,
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/create_from_inbound.html", context)


def cup_center_preview(request, cup_id):
    _require_perm(request, "store.view_subscriptioncup")
    cup = _get_cup(cup_id)
    context = {
        **_base_context(request, "Preview Cup output", cup=cup),
        "preview": render_cup_preview(cup),
        "subscription": subscription_url_summary(cup, request=request),
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/preview.html", context)


@require_POST
def cup_center_rebuild(request, cup_id):
    _require_perm(request, "store.change_subscriptioncup")
    cup = _get_cup(cup_id)
    try:
        rebuilt = rebuild_cup_from_source(cup)
    except CupCenterError:
        rebuilt = None
    if rebuilt:
        messages.success(request, "Cup از source خود rebuild شد.")
        return redirect("admin_store_cup_center_detail", rebuilt.pk)
    messages.warning(request, "برای این Cup source خودکار قابل rebuild پیدا نشد.")
    return redirect("admin_store_cup_center_detail", cup.pk)
