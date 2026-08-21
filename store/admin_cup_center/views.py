from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from store.admin_cup_center.forms import (
    INVENTORY_SOURCE_AUTO,
    INVENTORY_SOURCE_MANUAL,
    ExistingConfigLinkFilterForm,
    ManualCupForm,
    ManualLinksForm,
    PanelConfigIntoCupForm,
    QuickSubscriptionBuilderForm,
    inventory_asset_ids_from_data,
    inventory_pool_ids_from_asset_data,
)
from store.admin_cup_center.services import (
    CupCenterError,
    CupCenterRemoteCreateError,
    CupCenterRemoteSaveError,
    CupCenterValidationError,
    add_existing_links_to_cup,
    add_inventory_assets_to_cup,
    add_manual_links_to_cup,
    create_manual_cup,
    create_panel_config_into_cup,
    cup_item_rows,
    cup_list_items,
    cup_queryset,
    mask_link_for_display,
    INVENTORY_NO_CUPITEM_ERROR,
    MANUAL_INVENTORY_NO_CUPITEM_ERROR,
    quick_builder_inventory_pool_rows,
    quick_builder_panel_groups,
    quick_build_subscription_cup,
    rebuild_cup_from_source,
    render_cup_preview,
    remove_cup_item,
    replace_cup_item_config_link,
    set_cup_item_active,
    set_cup_status,
    move_cup_item,
    subscription_url_summary,
    update_cup_item_display_name,
)
from store.config_inventory_services import ConfigInventoryError
from store.models import ConfigInventoryPool, ConfigLink, SubscriptionCup


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


def _quick_panel_generated_count(panel_results):
    total = 0
    for panel in panel_results or []:
        if not isinstance(panel, dict) or not panel.get("success"):
            continue
        try:
            total += int(panel.get("link_count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _quick_status_label(status):
    return {
        "success": "موفق",
        "partial_success": "موفق با هشدار",
        "partial_with_warnings": "موفق با هشدار",
        "failed": "ناموفق",
    }.get(status, status or "-")


def _store_quick_result(request, result):
    panel_generated_count = _quick_panel_generated_count(result.panel_results)
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
        "panel_generated_count": panel_generated_count,
        "inventory_allocation_count": result.inventory_allocation_count,
        "config_link_count": result.config_link_count,
        "panel_results": result.panel_results,
        "inventory_results": result.inventory_results,
        "reconciliation": result.reconciliation,
        "masked_subscription_url": result.masked_subscription_url,
        "email_masked": result.email_masked,
        "warnings": result.warnings,
        "errors": result.errors,
        "structured_errors": result.structured_errors,
    }


def _selected_inventory_ui_state(request):
    source = request.POST if request.method == "POST" else request.GET
    selected_pool_ids = {int(value) for value in source.getlist("inventory_pools") if str(value).isdigit()}
    selected_pool_ids.update(inventory_pool_ids_from_asset_data(source))
    if request.method != "POST":
        for value in request.GET.getlist("inventory_pool"):
            if str(value).isdigit():
                selected_pool_ids.add(int(value))
        pool_id = str(request.GET.get("pool") or "").strip()
        if pool_id.isdigit():
            selected_pool_ids.add(int(pool_id))

    selected_modes = {}
    selected_quantities = {}
    selected_asset_ids_by_pool = {}
    pools_by_id = {
        pool.pk: pool
        for pool in ConfigInventoryPool.objects.filter(pk__in=selected_pool_ids)
    }
    for pool_id in selected_pool_ids:
        mode = str(source.get(f"inventory_mode_{pool_id}") or INVENTORY_SOURCE_AUTO).strip()
        if mode not in {INVENTORY_SOURCE_AUTO, INVENTORY_SOURCE_MANUAL}:
            mode = INVENTORY_SOURCE_AUTO
        selected_modes[pool_id] = mode
        try:
            selected_quantities[pool_id] = int(source.get(f"inventory_quantity_{pool_id}") or source.get("inventory_quantity") or 1)
        except (TypeError, ValueError):
            selected_quantities[pool_id] = 1
        selected_asset_ids_by_pool[pool_id] = set(inventory_asset_ids_from_data(source, pools_by_id.get(pool_id) or pool_id))
    return selected_pool_ids, selected_modes, selected_quantities, selected_asset_ids_by_pool


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
                if result.status in {"partial_success", "partial_with_warnings"}:
                    messages.warning(request, result.reconciliation.get("summary_message") or "Cup ساخته شد، اما بعضی منابع ناموفق بودند.")
                elif result.status == "failed":
                    manual_inventory_failed = any(
                        inventory_result.get("selection_mode") == INVENTORY_SOURCE_MANUAL
                        and inventory_result.get("requested_quantity")
                        and not int(inventory_result.get("cup_item_count") or 0)
                        for inventory_result in (result.inventory_results or [])
                    )
                    inventory_failed = any(
                        inventory_result.get("requested_quantity")
                        and not int(inventory_result.get("cup_item_count") or 0)
                        for inventory_result in (result.inventory_results or [])
                    )
                    messages.error(
                        request,
                        MANUAL_INVENTORY_NO_CUPITEM_ERROR
                        if manual_inventory_failed
                        else INVENTORY_NO_CUPITEM_ERROR
                        if inventory_failed
                        else "Cup ساخته شد، اما هیچ لینک موفقی ذخیره نشد.",
                    )
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
    selected_inventory_pool_ids, selected_inventory_modes, selected_inventory_quantities, selected_inventory_asset_ids_by_pool = _selected_inventory_ui_state(request)
    context = {
        **_base_context(request, "ساخت سریع لینک اشتراک"),
        "form": form,
        "panel_groups": quick_builder_panel_groups(),
        "inventory_pool_rows": quick_builder_inventory_pool_rows(
            selected_asset_ids_by_pool=selected_inventory_asset_ids_by_pool,
            selected_modes=selected_inventory_modes,
            selected_quantities=selected_inventory_quantities,
        ),
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
    panel_results = session_result.get("panel_results") or fallback_panel_results or []
    inventory_results = session_result.get("inventory_results") or (cup.metadata.get("inventory_results") if isinstance(cup.metadata, dict) else "") or []
    reconciliation = session_result.get("reconciliation") or (cup.metadata.get("reconciliation") if isinstance(cup.metadata, dict) else {}) or {}
    status = session_result.get("status") or (cup.metadata.get("status") if isinstance(cup.metadata, dict) else "") or "success"
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
            "status": status,
            "status_label": _quick_status_label(status),
            "protocols": session_result.get("protocols") or render_cup_preview(cup)["protocols"],
            "selected_inbounds": selected_inbounds,
            "selected_panels_count": session_result.get("selected_panels_count") or (cup.metadata.get("selected_panel_count") if isinstance(cup.metadata, dict) else "") or "-",
            "selected_inbounds_count": session_result.get("selected_inbounds_count") or (cup.metadata.get("selected_inbound_count") if isinstance(cup.metadata, dict) else "") or len(selected_inbounds),
            "selected_inventory_pools": session_result.get("selected_inventory_pools") or [],
            "selected_inventory_pools_count": session_result.get("selected_inventory_pools_count") or (cup.metadata.get("selected_inventory_pool_count") if isinstance(cup.metadata, dict) else "") or 0,
            "created_remote_client_groups_count": session_result.get("created_remote_client_groups_count") or (cup.metadata.get("created_remote_client_groups_count") if isinstance(cup.metadata, dict) else "") or 0,
            "panel_generated_count": session_result.get("panel_generated_count") or (cup.metadata.get("panel_generated_count") if isinstance(cup.metadata, dict) else "") or _quick_panel_generated_count(panel_results),
            "inventory_allocation_count": session_result.get("inventory_allocation_count") or (cup.metadata.get("inventory_allocation_count") if isinstance(cup.metadata, dict) else "") or 0,
            "config_link_count": session_result.get("config_link_count") or (cup.metadata.get("config_link_count") if isinstance(cup.metadata, dict) else "") or len(item_rows),
            "panel_results": panel_results,
            "inventory_results": inventory_results,
            "manual_inventory_results": [row for row in inventory_results if row.get("selection_mode") == INVENTORY_SOURCE_MANUAL],
            "auto_inventory_results": [row for row in inventory_results if row.get("selection_mode") != INVENTORY_SOURCE_MANUAL],
            "reconciliation": reconciliation,
            "customer_link_available": reconciliation.get("customer_link_available", bool(item_rows)),
            "customer_link_warning": reconciliation.get("customer_link_warning", ""),
            "summary_message": reconciliation.get("summary_message", ""),
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
            elif action == "remove_item":
                remove_cup_item(cup, request.POST.get("item_id"))
                messages.success(request, "لینک از Cup حذف شد.")
            elif action == "update_item_display_name":
                update_cup_item_display_name(cup, request.POST.get("item_id"), request.POST.get("display_name"))
                messages.success(request, "نام نمایشی لینک به‌روزرسانی شد.")
            elif action == "replace_config_link":
                replace_cup_item_config_link(cup, request.POST.get("item_id"), request.POST.get("config_link_id"))
                messages.success(request, "ConfigLink این آیتم جایگزین شد.")
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
        "add_inventory_url": reverse("admin_store_cup_center_add_inventory", args=[cup.pk]),
        "create_from_inbound_url": reverse("admin_store_cup_center_create_from_inbound", args=[cup.pk]),
        "rebuild_url": reverse("admin_store_cup_center_rebuild", args=[cup.pk]),
        "change_url": reverse("admin:store_subscriptioncup_change", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/detail.html", context)


def cup_center_item_link_secret(request, cup_id, item_id):
    _require_perm(request, "store.view_subscriptioncup")
    _require_perm(request, "store.view_configlink")
    cup = _get_cup(cup_id)
    item = get_object_or_404(cup.items.select_related("config_link"), pk=item_id)
    return JsonResponse(
        {
            "raw_link": item.config_link.raw_link,
            "masked_link": mask_link_for_display(item.config_link),
        }
    )


def cup_center_add_inventory(request, cup_id):
    _require_perm(request, "store.change_subscriptioncup")
    _require_perm(request, "store.view_configinventorypool")
    _require_perm(request, "store.view_configinventoryasset")
    _require_perm(request, "store.add_configlink")
    _require_perm(request, "store.add_cupitem")
    cup = _get_cup(cup_id)
    selected_pool_id = ""
    selected_asset_ids_by_pool = {}
    result = None
    errors = []

    if request.method == "POST":
        selected_pool_id = str(request.POST.get("pool") or "").strip()
        if selected_pool_id.isdigit():
            try:
                pool = ConfigInventoryPool.objects.get(pk=int(selected_pool_id), is_active=True)
                selected_asset_ids = inventory_asset_ids_from_data(request.POST, pool)
                selected_asset_ids_by_pool[int(selected_pool_id)] = set(selected_asset_ids)
                result = add_inventory_assets_to_cup(cup, pool, selected_asset_ids)
                if result.added_count:
                    messages.success(request, f"{result.added_count} کانفیگ از مخزن به Cup اضافه شد.")
                    return redirect("admin_store_cup_center_detail", cup.pk)
                messages.error(request, INVENTORY_NO_CUPITEM_ERROR)
                errors.extend(result.errors or [INVENTORY_NO_CUPITEM_ERROR])
                for warning in result.warnings:
                    messages.warning(request, warning)
            except (ConfigInventoryPool.DoesNotExist, ConfigInventoryError, CupCenterError) as exc:
                safe_message = str(getattr(exc, "safe_message", exc))
                error_code = str(getattr(exc, "code", "") or "").strip()
                errors.append(f"{error_code}: {safe_message}" if error_code else safe_message)
                messages.error(request, INVENTORY_NO_CUPITEM_ERROR)
        else:
            errors.append("یک مخزن کانفیگ انتخاب کنید.")
    else:
        selected_pool_id = str(request.GET.get("pool") or request.GET.get("inventory_pool") or "").strip()

    selected_pool_ids = {int(selected_pool_id)} if selected_pool_id.isdigit() else set()
    context = {
        **_base_context(request, "افزودن کانفیگ از مخزن", cup=cup),
        "pool_rows": quick_builder_inventory_pool_rows(selected_asset_ids_by_pool=selected_asset_ids_by_pool),
        "selected_pool_ids": selected_pool_ids,
        "selected_pool_id": int(selected_pool_id) if selected_pool_id.isdigit() else "",
        "result": result,
        "errors": errors,
        "detail_url": reverse("admin_store_cup_center_detail", args=[cup.pk]),
    }
    return TemplateResponse(request, "admin/store/cup_center/add_inventory.html", context)


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
