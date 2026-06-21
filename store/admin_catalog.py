import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from .models import Inbound, Operator, Order, Plan, PlanInboundRoute, Store, VPNClient
from .order_services import format_custom_volume_label, sales_mode_requires_operator
from .plan_route_services import (
    active_routes_for_plan_operator,
    get_valid_sales_inbounds,
    sales_inbound_issues,
)


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
    remark = safe_label(inbound.remark or f"Inbound {inbound.inbound_id}")
    return f"{panel_name} / #{inbound.inbound_id} / {remark}"


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
        recent_order_count = Order.objects.filter(plan=plan).order_by().count()
        vpn_client_count = VPNClient.objects.filter(plan=plan).order_by().count()
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
                "destination": route_status["destination"] or "-",
                "operator_names": [operator.name for operator in plan.operators.all()],
                "recent_order_count": recent_order_count,
                "vpn_client_count": vpn_client_count,
                "review_url": catalog_plan_review_url(plan),
                "edit_url": catalog_plan_edit_url(plan),
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


def get_catalog_context(store=None):
    plan_items = get_plan_catalog_items(store)
    action_plan_items = [
        item
        for item in plan_items
        if item["plan"].is_active and (item["route_status"]["is_invalid"] or item["route_status"]["code"] in {ROUTE_STATUS_MISSING, ROUTE_STATUS_FALLBACK})
    ]
    return {
        "summary": get_route_coverage_summary(store),
        "plan_items": plan_items,
        "active_plan_items": [item for item in plan_items if item["plan"].is_active],
        "action_plan_items": action_plan_items,
        "inbound_items": get_inbound_catalog_items(store),
        "route_items": get_route_overview_items(store),
        "action_items": get_catalog_action_items(store),
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


class SalesReadyInboundChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return inbound_label(obj)


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
    inbound = SalesReadyInboundChoiceField(
        label=_("Inbound مقصد"),
        queryset=Inbound.objects.none(),
        required=False,
        help_text=_("فقط inboundهای فعال، قابل فروش، غیر legacy و متصل به پنل فعال نمایش داده می‌شوند."),
    )
    operator = forms.ModelChoiceField(
        label=_("اپراتور route"),
        queryset=Operator.objects.none(),
        required=False,
        help_text=_("خالی یعنی route عمومی. در حالت فروش اپراتوری می‌توان route اختصاصی ساخت."),
    )
    priority = forms.IntegerField(label=_("اولویت route"), min_value=0, initial=100)
    route_active = forms.BooleanField(label=_("route فعال باشد"), required=False, initial=True)

    def __init__(self, *args, store=None, plan=None, **kwargs):
        self.store = store or getattr(plan, "store", None)
        self.plan = plan
        initial = kwargs.pop("initial", {}) or {}
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
                }
            )
            route = store_scoped_route_query(self.store).filter(plan=plan, is_active=True).order_by("operator_id", "priority", "pk").first()
            if route:
                initial.update(
                    {
                        "inbound": route.inbound_id,
                        "operator": route.operator_id,
                        "priority": route.priority,
                        "route_active": route.is_active,
                    }
                )
        else:
            initial.setdefault("currency", Plan.Currency.TOMAN)
            initial.setdefault("is_active", False)
            initial.setdefault("is_public", True)
            initial.setdefault("device_limit", 2)
            initial.setdefault("sort_order", 0)
            initial.setdefault("priority", 100)
            initial.setdefault("route_active", True)
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

        self.fields["inbound"].queryset = get_sales_ready_inbounds(self.store)
        operators = Operator.objects.filter(is_active=True)
        if self.store and self.store.pk:
            operators = operators.filter(models.Q(store=self.store) | models.Q(store__isnull=True))
        operators = operators.order_by("sort_order", "name", "pk")
        self.fields["operator"].queryset = operators
        self.fields["operators"].queryset = operators

        if not (self.store and sales_mode_requires_operator(self.store)):
            self.fields["operator"].widget = forms.HiddenInput()
            self.fields["operators"].widget = forms.MultipleHiddenInput()

    def clean(self):
        cleaned_data = super().clean()
        active = bool(cleaned_data.get("is_active"))
        public = bool(cleaned_data.get("is_public"))
        custom = bool(cleaned_data.get("is_custom_volume"))
        inbound = cleaned_data.get("inbound")
        operator = cleaned_data.get("operator")
        selected_operators = cleaned_data.get("operators")

        if operator and selected_operators is not None and operator not in selected_operators:
            self.add_error("operator", _("برای route اختصاصی، همان اپراتور باید در اپراتورهای مجاز پلن هم انتخاب شود."))

        if self.store and active and (public or custom):
            fallback_enabled = bool(getattr(self.store, "allow_global_inbound_fallback", True))
            routing_enabled = bool(getattr(self.store, "plan_inbound_routing_enabled", True))
            route_selected = bool(inbound and cleaned_data.get("route_active", True))
            existing_ready = False
            if self.plan:
                status = get_plan_route_status(self.plan, self.store)
                existing_ready = status["is_ready"] and status["code"] != ROUTE_STATUS_FALLBACK
            if routing_enabled and not fallback_enabled and not route_selected and not existing_ready:
                self.add_error("inbound", _("Fallback خاموش است؛ پلن فعال قابل فروش باید route معتبر داشته باشد."))
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

            inbound = self.cleaned_data.get("inbound")
            if inbound:
                self.save_route(plan, inbound)
        return plan

    def save_route(self, plan, inbound):
        operator = self.cleaned_data.get("operator")
        is_active = bool(self.cleaned_data.get("route_active"))
        priority = self.cleaned_data.get("priority") or 100
        route_filter = {"plan": plan, "inbound": inbound}
        if operator:
            route_filter["operator"] = operator
        else:
            route_filter["operator__isnull"] = True

        route = PlanInboundRoute.objects.filter(**route_filter).order_by("-is_active", "priority", "pk").first()
        if route is None:
            route = PlanInboundRoute(plan=plan, inbound=inbound, operator=operator)

        if is_active:
            for active_route in active_routes_for_plan_operator(plan, operator, store=self.store):
                if active_route.pk != route.pk:
                    active_route.is_active = False
                    active_route.full_clean()
                    active_route.save(update_fields=["is_active", "updated_at"])

        route.store = self.store
        route.inbound = inbound
        route.operator = operator
        route.priority = priority
        route.weight = getattr(route, "weight", 1) or 1
        route.is_active = is_active
        if not route.note:
            route.note = "Updated from product catalog."
        route.full_clean()
        route.save()
