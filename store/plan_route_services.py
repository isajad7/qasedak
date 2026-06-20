from copy import copy

from django.core.exceptions import ValidationError
from django.db import models, transaction

from .models import Inbound, Operator, Plan, PlanInboundRoute


BULK_ROUTE_STRATEGY_SKIP_EXISTING = "skip_existing"
BULK_ROUTE_STRATEGY_UPDATE_EXISTING = "update_existing"
BULK_ROUTE_STRATEGY_ADD_NEW = "add_new"
BULK_ROUTE_STRATEGY_REPLACE_ACTIVE = "replace_active"

BULK_ROUTE_STRATEGIES = {
    BULK_ROUTE_STRATEGY_SKIP_EXISTING,
    BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    BULK_ROUTE_STRATEGY_ADD_NEW,
    BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
}


def get_valid_sales_inbounds(store=None):
    inbounds = Inbound.objects.filter(
        is_active=True,
        available_for_new_orders=True,
        panel_id__isnull=False,
        panel__is_active=True,
        legacy_note="",
    )
    if store:
        inbounds = inbounds.filter(models.Q(panel__store=store) | models.Q(panel__store__isnull=True))
    return inbounds.select_related("panel").order_by("panel__name", "inbound_id", "pk")


def get_bulk_route_target_plans(store=None, selected_plan_ids=None, all_active=False):
    plans = Plan.objects.filter(is_active=True)
    if store:
        plans = plans.filter(models.Q(store=store) | models.Q(store__isnull=True))

    selected_ids = normalize_plan_ids(selected_plan_ids)
    if selected_ids:
        plans = plans.filter(pk__in=selected_ids)
    elif not all_active:
        return Plan.objects.none()

    return plans.prefetch_related("operators").order_by("sort_order", "price", "pk").distinct()


def preview_bulk_plan_routes(
    *,
    store=None,
    inbound=None,
    operator=None,
    selected_plan_ids=None,
    all_active=False,
    priority=100,
    weight=1,
    existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    note="",
):
    result = new_bulk_route_result()
    selected_ids = normalize_plan_ids(selected_plan_ids)
    plans = list(get_bulk_route_target_plans(store=store, selected_plan_ids=selected_ids, all_active=all_active))
    result["plans_count"] = len(plans)
    result["selected_plan_ids"] = [plan.pk for plan in plans]

    validate_bulk_route_inputs(
        result,
        store=store,
        inbound=inbound,
        selected_plan_ids=selected_ids,
        all_active=all_active,
        priority=priority,
        weight=weight,
        existing_strategy=existing_strategy,
        plans=plans,
    )
    if result["errors"]:
        return result

    for plan in plans:
        preview_plan_route_operation(
            result,
            store=store,
            plan=plan,
            inbound=inbound,
            operator=operator,
            priority=priority,
            weight=weight,
            existing_strategy=existing_strategy,
            note=note,
        )
    return result


def apply_bulk_plan_routes(
    *,
    store=None,
    inbound=None,
    operator=None,
    selected_plan_ids=None,
    all_active=False,
    priority=100,
    weight=1,
    existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    note="",
):
    preview = preview_bulk_plan_routes(
        store=store,
        inbound=inbound,
        operator=operator,
        selected_plan_ids=selected_plan_ids,
        all_active=all_active,
        priority=priority,
        weight=weight,
        existing_strategy=existing_strategy,
        note=note,
    )
    if preview["errors"]:
        return preview

    result = new_bulk_route_result()
    selected_ids = normalize_plan_ids(selected_plan_ids)
    plans = list(get_bulk_route_target_plans(store=store, selected_plan_ids=selected_ids, all_active=all_active))
    result["plans_count"] = len(plans)
    result["selected_plan_ids"] = [plan.pk for plan in plans]

    with transaction.atomic():
        validate_bulk_route_inputs(
            result,
            store=store,
            inbound=inbound,
            selected_plan_ids=selected_ids,
            all_active=all_active,
            priority=priority,
            weight=weight,
            existing_strategy=existing_strategy,
            plans=plans,
        )
        if result["errors"]:
            raise ValidationError(result["errors"])

        for plan in plans:
            apply_plan_route_operation(
                result,
                store=store,
                plan=plan,
                inbound=inbound,
                operator=operator,
                priority=priority,
                weight=weight,
                existing_strategy=existing_strategy,
                note=note,
            )

    return result


def normalize_plan_ids(selected_plan_ids):
    if not selected_plan_ids:
        return []
    if isinstance(selected_plan_ids, str):
        raw_values = [value.strip() for value in selected_plan_ids.split(",")]
    else:
        raw_values = selected_plan_ids

    normalized = []
    seen = set()
    for value in raw_values:
        pk = getattr(value, "pk", value)
        try:
            pk = int(pk)
        except (TypeError, ValueError):
            continue
        if pk <= 0 or pk in seen:
            continue
        normalized.append(pk)
        seen.add(pk)
    return normalized


def new_bulk_route_result():
    return {
        "plans_count": 0,
        "to_create": 0,
        "to_update": 0,
        "to_skip": 0,
        "to_deactivate": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "deactivated": 0,
        "errors": [],
        "warnings": [],
        "plan_results": [],
        "selected_plan_ids": [],
    }


def validate_bulk_route_inputs(
    result,
    *,
    store,
    inbound,
    selected_plan_ids,
    all_active,
    priority,
    weight,
    existing_strategy,
    plans,
):
    if existing_strategy not in BULK_ROUTE_STRATEGIES:
        result["errors"].append(f"Unknown route strategy: {existing_strategy}")
    if not all_active and not selected_plan_ids:
        result["errors"].append("Select at least one active plan or choose all active plans.")
    if selected_plan_ids and len(plans) != len(selected_plan_ids):
        missing_count = len(selected_plan_ids) - len(plans)
        result["warnings"].append(
            f"{missing_count} selected plan(s) were ignored because they are inactive or outside this store."
        )

    try:
        if int(priority) < 0:
            result["errors"].append("Priority must be zero or greater.")
    except (TypeError, ValueError):
        result["errors"].append("Priority must be a number.")

    try:
        if int(weight) < 1:
            result["errors"].append("Weight must be at least 1.")
    except (TypeError, ValueError):
        result["errors"].append("Weight must be a number.")

    inbound_errors, inbound_warnings = sales_inbound_issues(inbound, store=store)
    result["errors"].extend(inbound_errors)
    result["warnings"].extend(inbound_warnings)


def sales_inbound_issues(inbound, *, store=None):
    errors = []
    warnings = []
    if not inbound:
        return ["Inbound is required."], warnings
    if not inbound.is_active:
        errors.append("Inbound is inactive.")
    if not inbound.available_for_new_orders:
        errors.append("Inbound is not available for new orders.")
    if (inbound.legacy_note or "").strip():
        errors.append("Inbound is marked as legacy.")

    panel = None
    try:
        panel = inbound.panel
    except Exception:
        panel = None

    if not inbound.panel_id or not panel:
        errors.append("Inbound has no panel.")
    elif not panel.is_active:
        errors.append("Inbound panel is inactive.")
    elif store and panel.store_id and panel.store_id != store.pk:
        errors.append("Inbound belongs to a different store.")

    if not getattr(inbound, "health_monitor_enabled", True):
        warnings.append("Selected inbound is excluded from the health monitor.")
    if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
        warnings.append("Selected inbound capacity is currently full.")
    return errors, warnings


def preview_plan_route_operation(
    result,
    *,
    store,
    plan,
    inbound,
    operator,
    priority,
    weight,
    existing_strategy,
    note,
):
    if operator and not plan_accepts_operator(plan, operator, store=store):
        add_plan_result(
            result,
            plan,
            "skipped",
            f"Operator #{operator.pk} is not enabled on this plan.",
        )
        result["to_skip"] += 1
        result["warnings"].append(f"Plan #{plan.pk} skipped: operator is not enabled on the plan.")
        return

    active_routes = list(active_routes_for_plan_operator(plan, operator, store=store))
    same_inbound_route = first_route_for_same_inbound(plan, operator, inbound)

    if existing_strategy == BULK_ROUTE_STRATEGY_SKIP_EXISTING and active_routes:
        add_plan_result(result, plan, "skipped", "Active route already exists.")
        result["to_skip"] += 1
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_ADD_NEW and same_inbound_route:
        add_plan_result(result, plan, "skipped", "A route to the selected inbound already exists.")
        result["to_skip"] += 1
        result["warnings"].append(
            f"Plan #{plan.pk} skipped: a route to inbound #{inbound.pk} already exists and cannot be duplicated."
        )
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_UPDATE_EXISTING and active_routes:
        route = build_updated_route(active_routes[0], inbound=inbound, priority=priority, weight=weight, note=note)
        error_count = len(result["errors"])
        validate_candidate_route(result, route, plan=plan, action="update")
        if len(result["errors"]) == error_count:
            add_plan_result(result, plan, "update", f"Route #{active_routes[0].pk} will be updated.")
            result["to_update"] += 1
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_REPLACE_ACTIVE:
        reusable_route = same_inbound_route
        deactivation_count = len([route for route in active_routes if route.pk != getattr(reusable_route, "pk", None)])
        if reusable_route:
            route = build_updated_route(
                reusable_route,
                inbound=inbound,
                priority=priority,
                weight=weight,
                note=note,
                store=store,
                is_active=True,
            )
            error_count = len(result["errors"])
            validate_candidate_route(result, route, plan=plan, action="update")
            if len(result["errors"]) == error_count:
                add_plan_result(
                    result,
                    plan,
                    "update",
                    f"Route #{reusable_route.pk} will be reused and {deactivation_count} active route(s) will be deactivated.",
                )
                result["to_update"] += 1
                result["to_deactivate"] += deactivation_count
            return
        route = build_new_route(
            store=store,
            plan=plan,
            operator=operator,
            inbound=inbound,
            priority=priority,
            weight=weight,
            note=note,
        )
        error_count = len(result["errors"])
        validate_candidate_route(result, route, plan=plan, action="create")
        if len(result["errors"]) == error_count:
            add_plan_result(result, plan, "replace", f"{len(active_routes)} active route(s) will be deactivated.")
            result["to_create"] += 1
            result["to_deactivate"] += len(active_routes)
        return

    route = build_new_route(
        store=store,
        plan=plan,
        operator=operator,
        inbound=inbound,
        priority=priority,
        weight=weight,
        note=note,
    )
    error_count = len(result["errors"])
    validate_candidate_route(result, route, plan=plan, action="create")
    if len(result["errors"]) == error_count:
        add_plan_result(result, plan, "create", "A new active route will be created.")
        result["to_create"] += 1


def apply_plan_route_operation(
    result,
    *,
    store,
    plan,
    inbound,
    operator,
    priority,
    weight,
    existing_strategy,
    note,
):
    if operator and not plan_accepts_operator(plan, operator, store=store):
        add_plan_result(
            result,
            plan,
            "skipped",
            f"Operator #{operator.pk} is not enabled on this plan.",
        )
        result["skipped"] += 1
        result["warnings"].append(f"Plan #{plan.pk} skipped: operator is not enabled on the plan.")
        return

    active_routes = list(active_routes_for_plan_operator(plan, operator, store=store))
    same_inbound_route = first_route_for_same_inbound(plan, operator, inbound)

    if existing_strategy == BULK_ROUTE_STRATEGY_SKIP_EXISTING and active_routes:
        add_plan_result(result, plan, "skipped", "Active route already exists.")
        result["skipped"] += 1
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_ADD_NEW and same_inbound_route:
        add_plan_result(result, plan, "skipped", "A route to the selected inbound already exists.")
        result["skipped"] += 1
        result["warnings"].append(
            f"Plan #{plan.pk} skipped: a route to inbound #{inbound.pk} already exists and cannot be duplicated."
        )
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_UPDATE_EXISTING and active_routes:
        route = active_routes[0]
        update_route_fields(route, inbound=inbound, priority=priority, weight=weight, note=note)
        route.full_clean()
        route.save()
        add_plan_result(result, plan, "updated", f"Route #{route.pk} updated.")
        result["updated"] += 1
        return

    if existing_strategy == BULK_ROUTE_STRATEGY_REPLACE_ACTIVE:
        reusable_route = same_inbound_route
        deactivation_count = 0
        for route in active_routes:
            if reusable_route and route.pk == reusable_route.pk:
                continue
            route.is_active = False
            route.full_clean()
            route.save(update_fields=["is_active", "updated_at"])
            deactivation_count += 1

        if reusable_route:
            update_route_fields(
                reusable_route,
                store=store,
                inbound=inbound,
                priority=priority,
                weight=weight,
                note=note,
                is_active=True,
            )
            reusable_route.full_clean()
            reusable_route.save()
            add_plan_result(
                result,
                plan,
                "updated",
                f"Route #{reusable_route.pk} reused; {deactivation_count} active route(s) deactivated.",
            )
            result["updated"] += 1
            result["deactivated"] += deactivation_count
            return

        route = build_new_route(
            store=store,
            plan=plan,
            operator=operator,
            inbound=inbound,
            priority=priority,
            weight=weight,
            note=note,
        )
        route.full_clean()
        route.save()
        add_plan_result(result, plan, "created", f"Route #{route.pk} created after replacement.")
        result["created"] += 1
        result["deactivated"] += deactivation_count
        return

    route = build_new_route(
        store=store,
        plan=plan,
        operator=operator,
        inbound=inbound,
        priority=priority,
        weight=weight,
        note=note,
    )
    route.full_clean()
    route.save()
    add_plan_result(result, plan, "created", f"Route #{route.pk} created.")
    result["created"] += 1


def plan_accepts_operator(plan, operator, *, store=None):
    operators = plan.operators.filter(pk=operator.pk, is_active=True)
    if store:
        operators = operators.filter(models.Q(store=store) | models.Q(store__isnull=True))
    return operators.exists()


def active_routes_for_plan_operator(plan, operator, *, store=None):
    routes = PlanInboundRoute.objects.filter(plan=plan, is_active=True)
    routes = filter_routes_for_operator(routes, operator)
    if store:
        routes = routes.filter(
            models.Q(store=store) | models.Q(store__isnull=True),
            models.Q(inbound__panel__store=store) | models.Q(inbound__panel__store__isnull=True),
        ).annotate(
            store_route_priority=models.Case(
                models.When(store=store, then=0),
                default=1,
                output_field=models.IntegerField(),
            )
        )
    else:
        routes = routes.annotate(store_route_priority=models.Value(1, output_field=models.IntegerField()))
    return routes.select_related("store", "plan", "operator", "inbound", "inbound__panel").order_by(
        "store_route_priority",
        "priority",
        "pk",
    )


def first_route_for_same_inbound(plan, operator, inbound):
    routes = PlanInboundRoute.objects.filter(plan=plan, inbound=inbound)
    routes = filter_routes_for_operator(routes, operator)
    return routes.select_related("store", "plan", "operator", "inbound", "inbound__panel").order_by(
        "-is_active",
        "priority",
        "pk",
    ).first()


def filter_routes_for_operator(routes, operator):
    if operator:
        return routes.filter(operator=operator)
    return routes.filter(operator__isnull=True)


def build_new_route(*, store, plan, operator, inbound, priority, weight, note):
    return PlanInboundRoute(
        store=store,
        plan=plan,
        operator=operator,
        inbound=inbound,
        is_active=True,
        priority=priority,
        weight=weight,
        note=note or "",
    )


def build_updated_route(route, *, inbound, priority, weight, note, store=None, is_active=None):
    route = copy(route)
    update_route_fields(
        route,
        store=store,
        inbound=inbound,
        priority=priority,
        weight=weight,
        note=note,
        is_active=is_active,
    )
    return route


def update_route_fields(route, *, inbound, priority, weight, note, store=None, is_active=None):
    if store is not None:
        route.store = store
    route.inbound = inbound
    route.priority = priority
    route.weight = weight
    route.note = note or ""
    if is_active is not None:
        route.is_active = is_active


def validate_candidate_route(result, route, *, plan, action):
    try:
        route.full_clean()
    except ValidationError as exc:
        result["errors"].append(f"Plan #{plan.pk} {plan.name}: cannot {action} route: {format_validation_error(exc)}")


def format_validation_error(exc):
    if hasattr(exc, "message_dict"):
        parts = []
        for field, messages in exc.message_dict.items():
            parts.append(f"{field}: {', '.join(str(message) for message in messages)}")
        return "; ".join(parts)
    return "; ".join(str(message) for message in exc.messages)


def add_plan_result(result, plan, status, message):
    result["plan_results"].append(
        {
            "plan_id": plan.pk,
            "plan": str(plan),
            "status": status,
            "message": message,
        }
    )
