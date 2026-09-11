import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from .models import (
    ConfigInventoryPool,
    Inbound,
    Operator,
    Order,
    Panel,
    Plan,
    PlanDeliveryConfig,
    PlanDeliverySource,
    PlanInboundRoute,
    Store,
    VPNClient,
)
from .external_subscription_sources import (
    ALL_DYNAMIC_PROTOCOLS,
    ALL_DYNAMIC_SECURITY,
    ALL_DYNAMIC_TRANSPORTS,
    default_external_subscription_filter_policy,
    normalize_external_subscription_filter_policy,
)
from .order_services import format_custom_volume_label, sales_mode_requires_operator
from .plan_delivery_services import (
    MODE_CONFLICT,
    MODE_DIRECT_LINKS,
    MODE_GLOBAL_FALLBACK,
    MODE_SUBSCRIPTION,
    active_delivery_config_for_plan,
    active_delivery_sources,
    delivery_mode_choices,
    resolve_plan_delivery_configuration,
    save_delivery_config_sources,
)
from .plan_route_services import (
    get_valid_sales_inbounds,
    sales_inbound_issues,
)
from .source_sellability import source_requires_sellability_verification, source_verification_ui_state


ROUTE_STATUS_READY = "ready"
ROUTE_STATUS_MISSING = "missing"
ROUTE_STATUS_INVALID = "invalid"
ROUTE_STATUS_PANEL_INACTIVE = "panel_inactive"
ROUTE_STATUS_INBOUND_INACTIVE = "inbound_inactive"
ROUTE_STATUS_UNAVAILABLE_FOR_SALES = "unavailable_for_sales"
ROUTE_STATUS_LEGACY_INBOUND = "legacy_inbound"
ROUTE_STATUS_FALLBACK = "fallback"
ROUTE_STATUS_OPERATOR_SPECIFIC = "operator_specific"

READY_ROUTE_STATUSES = {
    ROUTE_STATUS_READY,
    ROUTE_STATUS_OPERATOR_SPECIFIC,
    ROUTE_STATUS_FALLBACK,
}
INVALID_ROUTE_STATUSES = {
    ROUTE_STATUS_INVALID,
    ROUTE_STATUS_PANEL_INACTIVE,
    ROUTE_STATUS_INBOUND_INACTIVE,
    ROUTE_STATUS_UNAVAILABLE_FOR_SALES,
    ROUTE_STATUS_LEGACY_INBOUND,
}

ROUTE_STATUS_LABELS = {
    ROUTE_STATUS_READY: "آماده",
    ROUTE_STATUS_MISSING: "route ندارد",
    ROUTE_STATUS_INVALID: "نامعتبر",
    ROUTE_STATUS_PANEL_INACTIVE: "پنل غیرفعال",
    ROUTE_STATUS_INBOUND_INACTIVE: "Inbound غیرفعال",
    ROUTE_STATUS_UNAVAILABLE_FOR_SALES: "خارج از فروش",
    ROUTE_STATUS_LEGACY_INBOUND: "Legacy",
    ROUTE_STATUS_FALLBACK: "Fallback",
    ROUTE_STATUS_OPERATOR_SPECIFIC: "Route اپراتوری",
}

ROUTE_STATUS_TONES = {
    ROUTE_STATUS_READY: "success",
    ROUTE_STATUS_MISSING: "warning",
    ROUTE_STATUS_INVALID: "danger",
    ROUTE_STATUS_PANEL_INACTIVE: "danger",
    ROUTE_STATUS_INBOUND_INACTIVE: "danger",
    ROUTE_STATUS_UNAVAILABLE_FOR_SALES: "danger",
    ROUTE_STATUS_LEGACY_INBOUND: "danger",
    ROUTE_STATUS_FALLBACK: "warning",
    ROUTE_STATUS_OPERATOR_SPECIFIC: "info",
}

CONFIG_LINK_PATTERN = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
SUB_LINK_PATTERN = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")


def add_query(url, params):
    cleaned = {key: value for key, value in (params or {}).items() if value not in ("", None)}
    if not cleaned:
        return url
    return f"{url}?{urlencode(cleaned)}"


def catalog_url(store=None):
    return add_query(reverse("admin_store_catalog"), {"store": getattr(store, "pk", None)})


def catalog_plan_new_url(store=None):
    return add_query(reverse("admin_store_catalog_plan_new"), {"store": getattr(store, "pk", None)})


def catalog_plan_review_url(plan):
    return reverse("admin_store_catalog_plan_review", args=[plan.pk])


def catalog_plan_edit_url(plan):
    return reverse("admin_store_catalog_plan_edit", args=[plan.pk])


def safe_label(value, limit=120):
    text = str(value or "").strip()
    if not text:
        return "-"
    text = CONFIG_LINK_PATTERN.sub("<config-hidden>", text)
    text = SUB_LINK_PATTERN.sub("<subscription-hidden>", text)
    text = UUID_PATTERN.sub("<identifier-hidden>", text)
    text = LONG_TOKEN_PATTERN.sub("<token-hidden>", text)
    return text[:limit]


def money_label(amount, currency):
    labels = {
        Plan.Currency.TOMAN: "تومان",
        Plan.Currency.IRR: "ریال",
        Plan.Currency.USD: "USD",
    }
    return f"{int(amount or 0):,} {labels.get(currency, currency)}"


def plan_volume_label(plan):
    return format_custom_volume_label(getattr(plan, "volume_gb", ""))


def plan_duration_label(plan):
    return f"{int(getattr(plan, 'duration_days', 0) or 0):,} روز"


def selected_store_from_id(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def store_scoped_plan_query(store):
    queryset = Plan.objects.all()
    if store and store.pk:
        queryset = queryset.filter(models.Q(store=store) | models.Q(store__isnull=True))
    return queryset


def store_scoped_route_query(store):
    queryset = PlanInboundRoute.objects.select_related("store", "plan", "operator", "inbound", "inbound__panel")
    if store and store.pk:
        queryset = queryset.filter(
            models.Q(store=store) | models.Q(store__isnull=True),
            models.Q(inbound__panel__store=store) | models.Q(inbound__panel__store__isnull=True),
        )
    return queryset


def active_operators_for_plan(plan, store=None):
    queryset = plan.operators.filter(is_active=True)
    if store and store.pk:
        queryset = queryset.filter(models.Q(store=store) | models.Q(store__isnull=True))
    return queryset.order_by("sort_order", "name", "pk")


def plan_is_sales_candidate(plan):
    return bool(plan and plan.is_active and (plan.is_public or plan.is_custom_volume))


def status_dict(code, *, warnings=None, routes=None, destination="", operators=None):
    return {
        "code": code,
        "label": ROUTE_STATUS_LABELS.get(code, code),
        "tone": ROUTE_STATUS_TONES.get(code, "secondary"),
        "warnings": list(warnings or []),
        "routes": list(routes or []),
        "destination": destination,
        "operators": list(operators or []),
        "is_ready": code in READY_ROUTE_STATUSES,
        "is_invalid": code in INVALID_ROUTE_STATUSES,
    }


def route_status_from_errors(errors):
    joined = " ".join(str(error) for error in errors)
    if "legacy" in joined.lower() or "Legacy" in joined:
        return ROUTE_STATUS_LEGACY_INBOUND
    if "not available for new orders" in joined or "خارج از فروش" in joined:
        return ROUTE_STATUS_UNAVAILABLE_FOR_SALES
    if "inactive" in joined.lower() and "panel" in joined.lower():
        return ROUTE_STATUS_PANEL_INACTIVE
    if "panel" in joined.lower():
        return ROUTE_STATUS_PANEL_INACTIVE
    if "inactive" in joined.lower():
        return ROUTE_STATUS_INBOUND_INACTIVE
    return ROUTE_STATUS_INVALID


def route_readiness(route, store=None):
    if not route.is_active:
        return status_dict(ROUTE_STATUS_INVALID, warnings=["Route غیرفعال است."])

    errors = []
    warnings = []
    plan = getattr(route, "plan", None)
    operator = getattr(route, "operator", None)
    inbound = getattr(route, "inbound", None)
    panel = getattr(inbound, "panel", None) if inbound else None

    if store and store.pk:
        if route.store_id and route.store_id != store.pk:
            errors.append("Route به فروشگاه دیگری تعلق دارد.")
        if plan and plan.store_id and plan.store_id != store.pk:
            errors.append("پلن به فروشگاه دیگری تعلق دارد.")
        if panel and panel.store_id and panel.store_id != store.pk:
            errors.append("Inbound به فروشگاه دیگری تعلق دارد.")

    inbound_errors, inbound_warnings = sales_inbound_issues(inbound, store=store)
    errors.extend(inbound_errors)
    warnings.extend(inbound_warnings)

    if operator:
        if not operator.is_active:
            errors.append("اپراتور route غیرفعال است.")
        elif plan and plan.pk and not plan.operators.filter(pk=operator.pk).exists():
            errors.append("اپراتور روی پلن فعال نشده است.")

    if errors:
        return status_dict(route_status_from_errors(errors), warnings=errors + warnings, routes=[route])
    return status_dict(
        ROUTE_STATUS_OPERATOR_SPECIFIC if route.operator_id else ROUTE_STATUS_READY,
        warnings=warnings,
        routes=[route],
        destination=inbound_label(inbound),
        operators=[operator.name] if operator else [],
    )


def inbound_label(inbound):
    if not inbound:
        return "-"
    panel_name = safe_label(getattr(getattr(inbound, "panel", None), "name", ""))
    panel = getattr(inbound, "panel", None)
    if str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD:
        group_name = safe_label(inbound.remark or f"Group {inbound.inbound_id}")
        return f"{panel_name} / group #{inbound.inbound_id} / {group_name}"
    remark = safe_label(inbound.remark or f"Inbound {inbound.inbound_id}")
    node_name = safe_label(getattr(inbound, "xui_node_name", "") or getattr(inbound, "xui_node_id", ""))
    node_part = f" / node {node_name}" if node_name != "-" else ""
    return f"{panel_name}{node_part} / #{inbound.inbound_id} / {remark}"


def route_destination_label(route):
    operator = safe_label(getattr(getattr(route, "operator", None), "name", ""))
    label = inbound_label(getattr(route, "inbound", None))
    if operator != "-":
        return f"{operator}: {label}"
    return label


def active_routes_for_catalog_plan(plan, store=None):
    return list(
        store_scoped_route_query(store)
        .filter(plan=plan, is_active=True)
        .order_by("operator_id", "priority", "pk")
    )


def valid_general_routes(plan, store=None):
    return [
        route
        for route in active_routes_for_catalog_plan(plan, store)
        if not route.operator_id and route_readiness(route, store)["is_ready"]
    ]


def get_plan_route_status(plan, store=None):
    effective_store = store or getattr(plan, "store", None)
    routes = active_routes_for_catalog_plan(plan, effective_store)
    route_statuses = [route_readiness(route, effective_store) for route in routes]
    valid_routes = [status["routes"][0] for status in route_statuses if status["is_ready"] and status["routes"]]
    valid_general = [route for route in valid_routes if not route.operator_id]
    valid_operator = [route for route in valid_routes if route.operator_id]
    invalid_statuses = [status for status in route_statuses if status["is_invalid"]]

    if invalid_statuses:
        first = invalid_statuses[0]
        return status_dict(
            first["code"],
            warnings=[warning for status in invalid_statuses for warning in status["warnings"]],
            routes=routes,
            destination=route_destination_label(invalid_statuses[0]["routes"][0]) if invalid_statuses[0]["routes"] else "",
        )

    if valid_general:
        route = valid_general[0]
        return status_dict(
            ROUTE_STATUS_READY,
            warnings=[warning for status in route_statuses for warning in status["warnings"]],
            routes=routes,
            destination=route_destination_label(route),
        )

    if valid_operator:
        operators = {route.operator.name for route in valid_operator if route.operator_id}
        if effective_store and sales_mode_requires_operator(effective_store):
            active_ops = list(active_operators_for_plan(plan, effective_store))
            missing_ops = [
                operator.name
                for operator in active_ops
                if not any(route.operator_id == operator.pk for route in valid_operator)
            ]
            if not missing_ops and active_ops:
                return status_dict(
                    ROUTE_STATUS_OPERATOR_SPECIFIC,
                    warnings=[warning for status in route_statuses for warning in status["warnings"]],
                    routes=routes,
                    destination="، ".join(route_destination_label(route) for route in valid_operator[:3]),
                    operators=sorted(operators),
                )
            if missing_ops:
                warning = "برای این اپراتورها route اختصاصی یا route عمومی معتبر نیست: " + "، ".join(missing_ops[:5])
                if getattr(effective_store, "allow_global_inbound_fallback", True) and get_sales_ready_inbounds(effective_store).exists():
                    return status_dict(
                        ROUTE_STATUS_FALLBACK,
                        warnings=[warning, "برای اپراتورهای بدون route از fallback عمومی استفاده می‌شود."],
                        routes=routes,
                        destination="Fallback عمومی",
                        operators=sorted(operators),
                    )
                return status_dict(
                    ROUTE_STATUS_MISSING,
                    warnings=[warning],
                    routes=routes,
                    destination="، ".join(route_destination_label(route) for route in valid_operator[:3]),
                    operators=sorted(operators),
                )
        return status_dict(
            ROUTE_STATUS_OPERATOR_SPECIFIC,
            warnings=["فقط route اختصاصی اپراتور وجود دارد؛ اگر fallback خاموش باشد route عمومی یا route همه اپراتورها لازم است."],
            routes=routes,
            destination="، ".join(route_destination_label(route) for route in valid_operator[:3]),
            operators=sorted(operators),
        )

    sales_inbound_count = get_sales_ready_inbounds(effective_store).count()
    if effective_store and not getattr(effective_store, "plan_inbound_routing_enabled", True):
        if sales_inbound_count:
            return status_dict(
                ROUTE_STATUS_FALLBACK,
                warnings=["Route explicit خاموش است و انتخاب inbound از fallback عمومی انجام می‌شود."],
                routes=routes,
                destination="Fallback عمومی",
            )
        return status_dict(ROUTE_STATUS_INVALID, warnings=["Route explicit خاموش است ولی inbound آماده فروش وجود ندارد."], routes=routes)

    if effective_store and getattr(effective_store, "allow_global_inbound_fallback", True):
        if sales_inbound_count:
            return status_dict(
                ROUTE_STATUS_FALLBACK,
                warnings=["Route explicit ندارد؛ چون fallback روشن است فروش از انتخاب عمومی inbound انجام می‌شود."],
                routes=routes,
                destination="Fallback عمومی",
            )
        return status_dict(ROUTE_STATUS_INVALID, warnings=["Fallback روشن است ولی inbound آماده فروش وجود ندارد."], routes=routes)

    return status_dict(ROUTE_STATUS_MISSING, warnings=["برای این پلن route معتبر پیدا نشد."], routes=routes)


def validate_plan_sales_readiness(plan, store=None):
    effective_store = store or getattr(plan, "store", None)
    route_status = get_plan_route_status(plan, effective_store)
    warnings = list(route_status["warnings"])

    if not plan.is_active:
        warnings.append("پلن غیرفعال است و فروخته نمی‌شود.")
        return {"ready": False, "status": route_status, "warnings": warnings}
    if not (plan.is_public or plan.is_custom_volume):
        warnings.append("پلن نه عمومی است و نه پلن حجم دلخواه؛ در فروش عادی نمایش داده نمی‌شود.")
        return {"ready": False, "status": route_status, "warnings": warnings}
    if route_status["is_ready"] and route_status["code"] != ROUTE_STATUS_FALLBACK:
        return {"ready": True, "status": route_status, "warnings": warnings}
    if route_status["code"] == ROUTE_STATUS_FALLBACK:
        return {
            "ready": bool(effective_store and getattr(effective_store, "allow_global_inbound_fallback", True)),
            "status": route_status,
            "warnings": warnings,
        }
    return {"ready": False, "status": route_status, "warnings": warnings}


def get_sales_ready_inbounds(store=None):
    return get_valid_sales_inbounds(store)


def get_inbound_sales_readiness(inbound, store=None):
    errors, warnings = sales_inbound_issues(inbound, store=store)
    code = ROUTE_STATUS_READY if not errors else route_status_from_errors(errors)
    return status_dict(code, warnings=errors + warnings)


def get_inbound_catalog_items(store=None):
    items = []
    for inbound in get_sales_ready_inbounds(store).annotate(active_route_count=models.Count("plan_routes", filter=models.Q(plan_routes__is_active=True))):
        readiness = get_inbound_sales_readiness(inbound, store)
        items.append(
            {
                "inbound": inbound,
                "label": inbound_label(inbound),
                "panel": safe_label(inbound.panel.name if inbound.panel_id else ""),
                "xui_inbound_id": inbound.inbound_id,
                "remark": safe_label(inbound.remark or f"Inbound {inbound.inbound_id}"),
                "protocol": inbound.protocol,
                "is_active": inbound.is_active,
                "available_for_new_orders": inbound.available_for_new_orders,
                "health_monitor_enabled": inbound.health_monitor_enabled,
                "active_route_count": getattr(inbound, "active_route_count", 0),
                "readiness": readiness,
                "admin_url": reverse("admin:store_inbound_change", args=[inbound.pk]),
            }
        )
    return items


def get_plan_catalog_items(store=None):
    plans = (
        store_scoped_plan_query(store)
        .select_related("store")
        .prefetch_related("operators", "inbound_routes__operator", "inbound_routes__inbound", "inbound_routes__inbound__panel")
        .order_by("-is_active", "is_custom_volume", "sort_order", "price", "pk")
    )
    items = []
    for plan in plans:
        route_status = get_plan_route_status(plan, store)
        readiness = validate_plan_sales_readiness(plan, store)
        delivery_config = resolve_plan_delivery_configuration(plan, store)
        recent_order_count = Order.objects.filter(plan=plan).order_by().count()
        vpn_client_count = VPNClient.objects.filter(plan=plan).order_by().count()
        preview_url = catalog_plan_review_url(plan)
        test_url = catalog_plan_review_url(plan)
        if delivery_config.active_recipe_id:
            preview_url = reverse("admin_store_plan_fulfillment_recipe_preview", args=[delivery_config.active_recipe_id])
            test_url = reverse("admin_store_plan_fulfillment_recipe_simulate", args=[delivery_config.active_recipe_id])
        items.append(
            {
                "plan": plan,
                "name": safe_label(plan.name),
                "volume": plan_volume_label(plan),
                "duration": plan_duration_label(plan),
                "price": money_label(plan.price, plan.currency),
                "is_active": plan.is_active,
                "is_public": plan.is_public,
                "is_custom_volume": plan.is_custom_volume,
                "route_status": route_status,
                "readiness": readiness,
                "delivery_config": delivery_config,
                "delivery_method": delivery_config.mode_label,
                "delivery_sources": delivery_config.source_summary,
                "delivery_status_label": delivery_config.readiness_label,
                "delivery_status_tone": delivery_config.readiness_tone,
                "destination": route_status["destination"] or "-",
                "operator_names": [operator.name for operator in plan.operators.all()],
                "recent_order_count": recent_order_count,
                "vpn_client_count": vpn_client_count,
                "review_url": catalog_plan_review_url(plan),
                "edit_url": catalog_plan_edit_url(plan),
                "setup_delivery_url": f"{catalog_plan_edit_url(plan)}#delivery",
                "preview_url": preview_url,
                "test_url": test_url,
                "admin_url": reverse("admin:store_plan_change", args=[plan.pk]),
                "bulk_assign_url": add_query(
                    reverse("admin:store_planinboundroute_bulk_assign"),
                    {"store": getattr(store, "pk", None), "plan_ids": plan.pk, "plan_selection_mode": "manual"},
                ),
            }
        )
    return items


def get_route_overview_items(store=None):
    items = []
    routes = store_scoped_route_query(store).order_by("-is_active", "plan__sort_order", "plan__price", "plan_id", "operator_id", "priority", "pk")
    for route in routes:
        readiness = route_readiness(route, store) if route.is_active else status_dict(ROUTE_STATUS_INVALID, warnings=["Route غیرفعال است."])
        items.append(
            {
                "route": route,
                "plan": route.plan,
                "operator": route.operator,
                "inbound": route.inbound,
                "plan_label": safe_label(route.plan.name if route.plan_id else ""),
                "operator_label": safe_label(route.operator.name if route.operator_id else "عمومی"),
                "inbound_label": inbound_label(route.inbound),
                "priority": route.priority,
                "is_active": route.is_active,
                "readiness": readiness,
                "review_url": catalog_plan_review_url(route.plan) if route.plan_id else "",
                "inbound_admin_url": reverse("admin:store_inbound_change", args=[route.inbound_id]) if route.inbound_id else "",
            }
        )
    return items


def get_route_coverage_summary(store=None):
    plan_items = get_plan_catalog_items(store)
    active_items = [item for item in plan_items if item["plan"].is_active]
    sales_candidates = [item for item in plan_items if plan_is_sales_candidate(item["plan"])]
    route_items = get_route_overview_items(store)
    invalid_route_count = sum(1 for item in route_items if item["is_active"] and item["readiness"]["is_invalid"])
    missing_route_count = sum(
        1
        for item in sales_candidates
        if item["route_status"]["code"] in {ROUTE_STATUS_MISSING, ROUTE_STATUS_FALLBACK}
    )
    sales_ready_inbound_count = get_sales_ready_inbounds(store).count()
    fallback_enabled = bool(store and getattr(store, "allow_global_inbound_fallback", True))
    routing_enabled = bool(store and getattr(store, "plan_inbound_routing_enabled", True))

    if not store or not sales_candidates or not sales_ready_inbound_count:
        overall_code = "incomplete"
        overall_label = "تنظیمات ناقص"
        overall_tone = "warning"
    elif invalid_route_count or (missing_route_count and routing_enabled and not fallback_enabled):
        overall_code = "needs_fix"
        overall_label = "نیازمند اصلاح"
        overall_tone = "danger"
    else:
        overall_code = "ready"
        overall_label = "آماده فروش"
        overall_tone = "success"

    return {
        "active_plan_count": len(active_items),
        "inactive_plan_count": sum(1 for item in plan_items if not item["plan"].is_active),
        "sellable_plan_count": len(sales_candidates),
        "missing_route_count": missing_route_count,
        "invalid_route_count": invalid_route_count,
        "sales_ready_inbound_count": sales_ready_inbound_count,
        "fallback_enabled": fallback_enabled,
        "routing_enabled": routing_enabled,
        "overall_code": overall_code,
        "overall_label": overall_label,
        "overall_tone": overall_tone,
    }


def get_catalog_action_items(store=None):
    summary = get_route_coverage_summary(store)
    items = []
    if not store:
        items.append({"title": "Store ساخته نشده", "description": "برای شروع فروش ابتدا Store را بساز.", "tone": "warning", "url": reverse("admin:store_store_add")})
    if not summary["sales_ready_inbound_count"]:
        items.append(
            {
                "title": "Inbound آماده فروش وجود ندارد",
                "description": "Inbound باید فعال، قابل فروش، غیر legacy و روی پنل فعال باشد.",
                "tone": "danger",
                "url": reverse("admin:store_inbound_changelist"),
            }
        )
    if summary["invalid_route_count"]:
        items.append(
            {
                "title": "Route نامعتبر را اصلاح کن",
                "description": f"{summary['invalid_route_count']:,} route فعال به inbound/panel ناسالم اشاره می‌کند.",
                "tone": "danger",
                "url": "#routes",
            }
        )
    if summary["missing_route_count"] and not summary["fallback_enabled"]:
        items.append(
            {
                "title": "پلن‌های بدون route",
                "description": f"{summary['missing_route_count']:,} پلن فعال با fallback خاموش route معتبر ندارد.",
                "tone": "danger",
                "url": reverse("admin:store_planinboundroute_bulk_assign"),
            }
        )
    return items


def _catalog_filter_value(filters, key, default="all"):
    if not filters:
        return default
    value = (filters.get(key) or default).strip()
    return value or default


def filter_plan_catalog_items(items, filters=None):
    filters = filters or {}
    query = (filters.get("q") or "").strip().lower()
    active = _catalog_filter_value(filters, "active")
    visibility = _catalog_filter_value(filters, "visibility")
    route = _catalog_filter_value(filters, "route")

    filtered_items = []
    for item in items:
        plan = item["plan"]
        if query:
            searchable = " ".join(
                [
                    str(item.get("name") or ""),
                    str(getattr(plan, "slug", "") or ""),
                    str(getattr(plan, "description", "") or ""),
                ]
            ).lower()
            if query not in searchable:
                continue
        if active == "active" and not item["is_active"]:
            continue
        if active == "inactive" and item["is_active"]:
            continue
        if visibility == "public" and not item["is_public"]:
            continue
        if visibility == "private" and item["is_public"]:
            continue
        has_explicit_route = item["route_status"]["code"] not in {ROUTE_STATUS_MISSING, ROUTE_STATUS_FALLBACK}
        if route == "has_route" and not has_explicit_route:
            continue
        if route == "missing_route" and has_explicit_route:
            continue
        filtered_items.append(item)
    return filtered_items


def get_catalog_context(store=None, filters=None):
    plan_items = get_plan_catalog_items(store)
    filtered_plan_items = filter_plan_catalog_items(plan_items, filters)
    action_plan_items = [
        item
        for item in plan_items
        if item["plan"].is_active and (item["route_status"]["is_invalid"] or item["route_status"]["code"] in {ROUTE_STATUS_MISSING, ROUTE_STATUS_FALLBACK})
    ]
    return {
        "summary": get_route_coverage_summary(store),
        "plan_items": plan_items,
        "filtered_plan_items": filtered_plan_items,
        "active_plan_items": [item for item in plan_items if item["plan"].is_active],
        "action_plan_items": action_plan_items,
        "inbound_items": get_inbound_catalog_items(store),
        "route_items": get_route_overview_items(store),
        "action_items": get_catalog_action_items(store),
        "catalog_filters": {
            "q": (filters or {}).get("q", ""),
            "active": _catalog_filter_value(filters, "active"),
            "visibility": _catalog_filter_value(filters, "visibility"),
            "route": _catalog_filter_value(filters, "route"),
        },
        "filtered_plan_count": len(filtered_plan_items),
        "new_plan_url": catalog_plan_new_url(store),
        "bulk_assign_url": add_query(reverse("admin:store_planinboundroute_bulk_assign"), {"store": getattr(store, "pk", None)}),
        "inbound_admin_url": reverse("admin:store_inbound_changelist"),
        "panel_admin_url": reverse("admin:store_panel_changelist"),
        "setup_wizard_url": add_query(reverse("admin_store_setup_wizard"), {"store": getattr(store, "pk", None)}),
        "dashboard_url": add_query(reverse("admin_store_owner_dashboard"), {"store": getattr(store, "pk", None)}),
        "audit_command": "python manage.py audit_plan_inbound_routes --dry-run",
    }


def duplicate_plan_for_admin(plan, actor=None):
    with transaction.atomic():
        source = Plan.objects.select_for_update().get(pk=plan.pk)
        copied = Plan.objects.create(
            store=source.store,
            name=(f"کپی {source.name}")[:100],
            slug="",
            description=source.description,
            volume_gb=source.volume_gb,
            duration_days=source.duration_days,
            price=source.price,
            currency=source.currency,
            device_limit=source.device_limit,
            is_active=False,
            sort_order=source.sort_order,
            is_public=source.is_public,
            is_custom_volume=source.is_custom_volume,
        )
        copied.operators.set(source.operators.all())
    return copied


def set_plan_active_state(plan, active, actor=None):
    desired = bool(active)
    if desired:
        store = getattr(plan, "store", None)
        status = get_plan_route_status(plan, store)
        requires_sales_route = bool(plan.is_public or plan.is_custom_volume)
        if requires_sales_route and status["code"] == ROUTE_STATUS_FALLBACK and store and not store.allow_global_inbound_fallback:
            raise ValidationError("Fallback خاموش است؛ قبل از فعال‌سازی پلن route معتبر بساز.")
        if requires_sales_route and not status["is_ready"]:
            raise ValidationError("برای فعال کردن پلن، route معتبر یا fallback ایمن لازم است.")
    if plan.is_active == desired:
        return False
    plan.is_active = desired
    plan.save(update_fields=["is_active", "updated_at"])
    return True


def deactivate_route_for_admin(route):
    if not route.is_active:
        return False
    route.is_active = False
    route.save(update_fields=["is_active", "updated_at"])
    return True


class CatalogPlanForm(forms.Form):
    name = forms.CharField(label=_("نام پلن"), max_length=100)
    volume_gb = forms.DecimalField(
        label=_("حجم (GB)"),
        max_digits=8,
        decimal_places=3,
        min_value=Decimal("0.001"),
    )
    duration_days = forms.IntegerField(label=_("مدت (روز)"), min_value=1)
    price = forms.IntegerField(label=_("قیمت"), min_value=0)
    currency = forms.ChoiceField(label=_("واحد پول"), choices=Plan.Currency.choices)
    is_active = forms.BooleanField(label=_("فعال"), required=False)
    is_public = forms.BooleanField(label=_("نمایش عمومی"), required=False)
    is_custom_volume = forms.BooleanField(label=_("حجم دلخواه"), required=False)
    device_limit = forms.IntegerField(label=_("تعداد دستگاه"), min_value=1)
    sort_order = forms.IntegerField(label=_("ترتیب نمایش"), min_value=0)
    operators = forms.ModelMultipleChoiceField(
        label=_("اپراتورهای مجاز"),
        queryset=Operator.objects.none(),
        required=False,
        help_text=_("فقط در حالت فروش اپراتوری استفاده می‌شود."),
    )
    delivery_mode = forms.ChoiceField(
        label=_("روش تحویل سرویس"),
        choices=delivery_mode_choices(),
        required=False,
        widget=forms.RadioSelect,
    )
    failure_policy = forms.ChoiceField(
        label=_("سیاست خطا و کمبود موجودی"),
        choices=PlanDeliveryConfig.FailurePolicy.choices,
        required=False,
        initial=PlanDeliveryConfig.FailurePolicy.STRICT,
    )

    source_prefix = "source"

    def __init__(self, *args, store=None, plan=None, **kwargs):
        self.store = store or getattr(plan, "store", None)
        self.plan = plan
        self.source_errors = []
        self.cleaned_source_rows = []
        self.active_delivery_config = active_delivery_config_for_plan(plan) if plan else None
        initial = kwargs.pop("initial", {}) or {}
        self.delivery_config = resolve_plan_delivery_configuration(plan, self.store) if plan else resolve_plan_delivery_configuration(None, self.store)
        if plan:
            initial.update(
                {
                    "name": plan.name,
                    "volume_gb": plan.volume_gb,
                    "duration_days": plan.duration_days,
                    "price": plan.price,
                    "currency": plan.currency,
                    "is_active": plan.is_active,
                    "is_public": plan.is_public,
                    "is_custom_volume": plan.is_custom_volume,
                    "device_limit": plan.device_limit,
                    "sort_order": plan.sort_order,
                    "operators": list(plan.operators.values_list("pk", flat=True)),
                    "delivery_mode": "" if self.delivery_config.effective_mode == MODE_CONFLICT else self.delivery_config.effective_mode,
                    "failure_policy": self.delivery_config.failure_policy,
                }
            )
        else:
            initial.setdefault("currency", Plan.Currency.TOMAN)
            initial.setdefault("is_active", False)
            initial.setdefault("is_public", True)
            initial.setdefault("device_limit", 2)
            initial.setdefault("sort_order", 0)
            initial.setdefault("delivery_mode", MODE_GLOBAL_FALLBACK)
            initial.setdefault("failure_policy", PlanDeliveryConfig.FailurePolicy.STRICT)
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

        initial_source_rows = self._posted_source_rows() if self.is_bound else self._config_source_rows()
        selected_inbound_ids = {row.get("inbound_id") for row in initial_source_rows if row.get("inbound_id")}
        selected_pool_ids = {row.get("inventory_pool_id") for row in initial_source_rows if row.get("inventory_pool_id")}

        sales_ready_ids = list(get_sales_ready_inbounds(self.store).values_list("pk", flat=True))
        verifiable_sources = Inbound.objects.select_related("panel").filter(
            panel__family=Panel.Family.PASARGUARD,
            xui_source=Inbound.XUISource.PASARGUARD_GROUP,
            panel__is_active=True,
        )
        if self.store and self.store.pk:
            verifiable_sources = verifiable_sources.filter(
                models.Q(panel__store=self.store) | models.Q(panel__store__isnull=True)
            )
        verifiable_source_ids = [
            inbound.pk for inbound in verifiable_sources if source_requires_sellability_verification(inbound)
        ]
        self.source_inbound_queryset = (
            Inbound.objects.select_related("panel")
            .filter(
                models.Q(pk__in=sales_ready_ids)
                | models.Q(pk__in=selected_inbound_ids)
                | models.Q(pk__in=verifiable_source_ids)
            )
            .order_by("panel__name", "inbound_id", "pk")
        )

        pools = ConfigInventoryPool.objects.filter(models.Q(is_active=True) | models.Q(pk__in=selected_pool_ids))
        if self.store and self.store.pk:
            pools = pools.filter(
                models.Q(connected_plan__isnull=True)
                | models.Q(connected_plan__store=self.store)
                | models.Q(pk__in=selected_pool_ids)
            )
        self.source_pool_queryset = pools.order_by("priority", "title", "pk")

        operators = Operator.objects.filter(is_active=True)
        if self.store and self.store.pk:
            operators = operators.filter(models.Q(store=self.store) | models.Q(store__isnull=True))
        self.fields["operators"].queryset = operators.order_by("sort_order", "name", "pk")
        if not (self.store and sales_mode_requires_operator(self.store)):
            self.fields["operators"].widget = forms.MultipleHiddenInput()

        for field in self.fields.values():
            if not isinstance(field.widget, (forms.CheckboxSelectMultiple, forms.RadioSelect)):
                css_class = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"{css_class} plan-delivery-field".strip()

        self.source_type_options = [
            {"value": value, "label": label}
            for value, label in PlanDeliverySource.SourceType.choices
        ]
        self.source_inbound_options = [
            {
                "value": str(inbound.pk),
                "label": (
                    f"{inbound_label(inbound)} / {source_verification_ui_state(inbound)['label']}"
                    if source_requires_sellability_verification(inbound)
                    else inbound_label(inbound)
                ),
                "dynamic_subscription": bool(
                    getattr(getattr(inbound, "panel", None), "family", "") == Panel.Family.PASARGUARD
                ),
                "verification_state": source_verification_ui_state(inbound)["label"],
                "verification_blocking": source_verification_ui_state(inbound)["blocking"],
            }
            for inbound in self.source_inbound_queryset
        ]
        self.source_pool_options = [
            {"value": str(pool.pk), "label": safe_label(pool.title)}
            for pool in self.source_pool_queryset
        ]
        self.dynamic_protocol_options = [
            {"value": value, "label": str(value).upper() if value not in {"ss"} else "SS"}
            for value in ALL_DYNAMIC_PROTOCOLS
        ]
        self.dynamic_security_options = [
            {"value": value, "label": value}
            for value in ALL_DYNAMIC_SECURITY
        ]
        self.dynamic_transport_options = [
            {"value": value, "label": value}
            for value in ALL_DYNAMIC_TRANSPORTS
        ]
        self.source_rows = self._template_source_rows(initial_source_rows)

    def _as_int(self, value, default=0):
        try:
            if value in ("", None):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _posted_bool(self, name, default=False):
        if not self.is_bound or name not in self.data:
            return default
        return str(self.data.get(name)).lower() in {"1", "true", "on", "yes"}

    def _posted_list(self, name):
        if not self.is_bound:
            return []
        if hasattr(self.data, "getlist"):
            return [str(item or "").strip() for item in self.data.getlist(name) if str(item or "").strip()]
        value = self.data.get(name)
        if isinstance(value, (list, tuple)):
            return [str(item or "").strip() for item in value if str(item or "").strip()]
        return [str(value or "").strip()] if str(value or "").strip() else []

    def _source_field_name(self, index, field):
        return f"{self.source_prefix}-{index}-{field}"

    def _posted_dynamic_policy(self, index):
        return normalize_external_subscription_filter_policy(
            {
                "protocols": self._posted_list(self._source_field_name(index, "dynamic_protocols")),
                "security": self._posted_list(self._source_field_name(index, "dynamic_security")),
                "transport": self._posted_list(self._source_field_name(index, "dynamic_transport")),
                "require_reality_pbk": self._posted_bool(
                    self._source_field_name(index, "dynamic_require_reality_pbk"),
                    True,
                ),
                "remark_include": self.data.get(self._source_field_name(index, "dynamic_remark_include")) or "",
                "remark_exclude": self.data.get(self._source_field_name(index, "dynamic_remark_exclude")) or "",
                "max_configs": self.data.get(self._source_field_name(index, "dynamic_max_configs")) or "",
                "deduplicate_exact": self._posted_bool(
                    self._source_field_name(index, "dynamic_deduplicate_exact"),
                    True,
                ),
                "refresh_interval_hours": self.data.get(self._source_field_name(index, "dynamic_refresh_interval_hours")) or "",
            }
        )

    def _raw_posted_source_row(self, index):
        return {
            "index": index,
            "id": self.data.get(self._source_field_name(index, "id")) or "",
            "source_type": self.data.get(self._source_field_name(index, "source_type")) or PlanDeliverySource.SourceType.PANEL_INBOUND,
            "inbound_id": self.data.get(self._source_field_name(index, "inbound")) or "",
            "inventory_pool_id": self.data.get(self._source_field_name(index, "inventory_pool")) or "",
            "label": self.data.get(self._source_field_name(index, "label")) or "",
            "quantity": self.data.get(self._source_field_name(index, "quantity")) or "1",
            "priority": self.data.get(self._source_field_name(index, "priority")) or str((index + 1) * 10),
            "required": self._posted_bool(self._source_field_name(index, "required")),
            "is_fallback": self._posted_bool(self._source_field_name(index, "is_fallback")),
            "DELETE": self._posted_bool(self._source_field_name(index, "DELETE")),
            "dynamic_policy": self._posted_dynamic_policy(index),
        }

    def _posted_source_rows(self):
        total = self._as_int(self.data.get(f"{self.source_prefix}-TOTAL_FORMS"), 0)
        rows = []
        for index in range(max(total, 0)):
            row = self._raw_posted_source_row(index)
            if row["DELETE"]:
                continue
            has_identity = bool(row["id"] or row["inbound_id"] or row["inventory_pool_id"] or row["label"])
            has_explicit_type = self._source_field_name(index, "source_type") in self.data
            if has_identity or has_explicit_type:
                rows.append(row)
        return rows

    def _config_source_rows(self):
        if not self.active_delivery_config:
            return []
        rows = []
        for index, source in enumerate(active_delivery_sources(self.active_delivery_config)):
            rows.append(
                {
                    "index": index,
                    "id": source.pk,
                    "source_type": source.source_type,
                    "inbound_id": source.inbound_id or "",
                    "inventory_pool_id": source.inventory_pool_id or "",
                    "label": source.label,
                        "quantity": source.quantity,
                        "priority": source.priority,
                        "required": source.required,
                        "is_fallback": source.is_fallback,
                        "dynamic_policy": normalize_external_subscription_filter_policy(
                            (source.metadata or {}).get("dynamic_subscription_policy") or {}
                        ),
                    }
                )
            return rows

    def _blank_source_row(self, index=0):
        return {
            "index": index,
            "id": "",
            "source_type": PlanDeliverySource.SourceType.PANEL_INBOUND,
            "inbound_id": "",
            "inventory_pool_id": "",
            "label": "",
            "quantity": 1,
            "priority": (index + 1) * 10,
            "required": True,
            "is_fallback": False,
            "dynamic_policy": default_external_subscription_filter_policy(),
        }

    def _template_source_rows(self, rows):
        display_rows = rows or [self._blank_source_row(0)]
        decorated = []
        inbound_by_id = {str(inbound.pk): inbound for inbound in self.source_inbound_queryset}
        for index, row in enumerate(display_rows):
            source_type = str(row.get("source_type") or PlanDeliverySource.SourceType.PANEL_INBOUND)
            dynamic_policy = normalize_external_subscription_filter_policy(row.get("dynamic_policy") or {})
            inbound = inbound_by_id.get(str(row.get("inbound_id") or ""))
            supports_dynamic_subscription = bool(
                inbound
                and getattr(getattr(inbound, "panel", None), "family", "") == Panel.Family.PASARGUARD
            )
            verification = source_verification_ui_state(inbound) if inbound else {}
            decorated.append(
                {
                    **row,
                    "index": index,
                    "id": str(row.get("id") or ""),
                    "source_type": source_type,
                    "inbound_id": str(row.get("inbound_id") or ""),
                    "inventory_pool_id": str(row.get("inventory_pool_id") or ""),
                    "quantity": self._as_int(row.get("quantity"), 1) or 1,
                    "priority": self._as_int(row.get("priority"), (index + 1) * 10),
                    "required": bool(row.get("required")),
                    "is_fallback": bool(row.get("is_fallback")),
                    "dynamic_policy": dynamic_policy,
                    "supports_dynamic_subscription": supports_dynamic_subscription,
                    "verification_state": verification.get("label", ""),
                    "verification_tone": verification.get("tone", "slate"),
                    "verification_blocking": verification.get("blocking", False),
                }
            )
        return decorated

    def _source_error(self, row, message):
        label = row.get("label") or row.get("source_type") or _("منبع")
        self.source_errors.append(
            _("ردیف %(row)s (%(label)s): %(message)s")
            % {
                "row": int(row.get("index", 0)) + 1,
                "label": label,
                "message": message,
            }
        )

    def _clean_source_rows(self):
        sources = []
        self.source_errors = []
        inbound_by_id = {str(inbound.pk): inbound for inbound in self.source_inbound_queryset}
        pool_by_id = {str(pool.pk): pool for pool in self.source_pool_queryset}
        valid_source_types = set(PlanDeliverySource.SourceType.values)

        for row in self._posted_source_rows():
            source_type = str(row.get("source_type") or "").strip()
            if source_type not in valid_source_types:
                self._source_error(row, _("نوع منبع نامعتبر است."))
                continue
            quantity = self._as_int(row.get("quantity"), 1)
            priority = self._as_int(row.get("priority"), (int(row.get("index", 0)) + 1) * 10)
            if quantity < 1:
                self._source_error(row, _("تعداد باید حداقل ۱ باشد."))
                continue
            if priority < 0:
                self._source_error(row, _("اولویت نمی‌تواند منفی باشد."))
                continue

            metadata = {"created_from": "canonical_plan_delivery_editor"}
            source = {
                "id": self._as_int(row.get("id"), None),
                "source_type": source_type,
                "label": str(row.get("label") or "").strip()[:120],
                "quantity": quantity,
                "priority": priority,
                "required": bool(row.get("required")),
                "is_fallback": bool(row.get("is_fallback")),
                "metadata": metadata,
            }
            if source_type == PlanDeliverySource.SourceType.PANEL_INBOUND:
                inbound = inbound_by_id.get(str(row.get("inbound_id") or ""))
                if not inbound:
                    self._source_error(row, _("Inbound آماده فروش را انتخاب کنید."))
                    continue
                errors, warnings = sales_inbound_issues(inbound, store=self.store)
                if errors:
                    self._source_error(row, " ".join(str(error) for error in errors + warnings))
                    continue
                if getattr(getattr(inbound, "panel", None), "family", "") == Panel.Family.PASARGUARD:
                    metadata["dynamic_subscription_policy"] = normalize_external_subscription_filter_policy(row.get("dynamic_policy") or {})
                source.update({"panel": inbound.panel, "inbound": inbound, "inventory_pool": None})
            else:
                pool = pool_by_id.get(str(row.get("inventory_pool_id") or ""))
                if not pool:
                    self._source_error(row, _("مخزن کانفیگ را انتخاب کنید."))
                    continue
                if not pool.is_active:
                    self._source_error(row, _("مخزن انتخاب‌شده غیرفعال است."))
                    continue
                source.update({"panel": None, "inbound": None, "inventory_pool": pool})
            sources.append(source)

        if self.source_errors:
            self.add_error("delivery_mode", _("خطاهای منابع سرویس را بررسی کنید."))
        return sources

    def clean(self):
        cleaned_data = super().clean()
        active = bool(cleaned_data.get("is_active"))
        public = bool(cleaned_data.get("is_public"))
        custom = bool(cleaned_data.get("is_custom_volume"))
        delivery_mode = cleaned_data.get("delivery_mode") or MODE_GLOBAL_FALLBACK
        cleaned_data["delivery_mode"] = delivery_mode

        sources = [] if delivery_mode == MODE_GLOBAL_FALLBACK else self._clean_source_rows()
        self.cleaned_source_rows = sources
        if delivery_mode in {MODE_DIRECT_LINKS, MODE_SUBSCRIPTION} and not sources and not self.source_errors:
            self.add_error("delivery_mode", _("برای این روش تحویل حداقل یک منبع سرویس لازم است."))

        if self.store and active and (public or custom):
            fallback_available = bool(getattr(self.store, "allow_global_inbound_fallback", True) and get_sales_ready_inbounds(self.store).exists())
            if delivery_mode == MODE_GLOBAL_FALLBACK and not fallback_available:
                self.add_error("delivery_mode", _("برای پیش‌فرض فروشگاه، fallback باید روشن باشد و حداقل یک inbound آماده فروش وجود داشته باشد."))
        return cleaned_data

    def save(self):
        with transaction.atomic():
            plan = self.plan or Plan(store=self.store, slug="")
            for field in (
                "name",
                "volume_gb",
                "duration_days",
                "price",
                "currency",
                "is_active",
                "is_public",
                "is_custom_volume",
                "device_limit",
                "sort_order",
            ):
                setattr(plan, field, self.cleaned_data[field])
            if not plan.store_id and self.store:
                plan.store = self.store
            plan.full_clean(exclude=["operators"])
            plan.save()
            plan.operators.set(self.cleaned_data.get("operators") or [])
            self.save_delivery(plan, self.cleaned_data.get("delivery_mode") or MODE_GLOBAL_FALLBACK)
        return plan

    def save_delivery(self, plan, delivery_mode):
        save_delivery_config_sources(
            plan,
            delivery_mode=delivery_mode,
            failure_policy=self.cleaned_data.get("failure_policy") or PlanDeliveryConfig.FailurePolicy.STRICT,
            sources=[] if delivery_mode == MODE_GLOBAL_FALLBACK else self.cleaned_source_rows,
        )
