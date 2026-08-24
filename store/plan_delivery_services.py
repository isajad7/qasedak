from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import models
from django.utils.translation import gettext as _

from .config_inventory_services import get_pool_stock_summary
from .models import (
    CupFulfillmentRecipe,
    Plan,
    PlanDeliveryConfig,
    PlanDeliverySource,
    PlanInboundRoute,
)
from .plan_route_services import get_valid_sales_inbounds, sales_inbound_issues


logger = logging.getLogger(__name__)


MODE_GLOBAL_FALLBACK = PlanDeliveryConfig.DeliveryMode.GLOBAL_FALLBACK
MODE_DIRECT_LINKS = PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS
MODE_SUBSCRIPTION = PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION
MODE_CONFLICT = "CONFLICT"

SOURCE_V2_CONFIG = "plan_delivery_config"
SOURCE_STORE_FALLBACK = "store_global_fallback"
SOURCE_PLAN_ROUTES = "legacy_plan_inbound_routes"
SOURCE_CUP_RECIPE = "legacy_cup_fulfillment_recipe"
SOURCE_CONFLICT = "legacy_conflict"

READINESS_READY = "ready"
READINESS_WARNING = "warning"
READINESS_INCOMPLETE = "incomplete"
READINESS_CONFLICT = "conflict"


MODE_LABELS = {
    MODE_GLOBAL_FALLBACK: _("پیش‌فرض فروشگاه"),
    MODE_DIRECT_LINKS: _("لینک مستقیم"),
    MODE_SUBSCRIPTION: _("ساب اختصاصی قاصدک"),
    MODE_CONFLICT: _("تداخل تنظیمات"),
}

READINESS_LABELS = {
    READINESS_READY: _("آماده"),
    READINESS_WARNING: _("هشدار"),
    READINESS_INCOMPLETE: _("ناقص"),
    READINESS_CONFLICT: _("تداخل تنظیمات"),
}

READINESS_TONES = {
    READINESS_READY: "success",
    READINESS_WARNING: "warning",
    READINESS_INCOMPLETE: "danger",
    READINESS_CONFLICT: "danger",
}


@dataclass
class PlanDeliveryConfiguration:
    plan_id: int | None
    effective_mode: str
    source_of_truth: str
    delivery_config_id: int | None = None
    failure_policy: str = PlanDeliveryConfig.FailurePolicy.STRICT
    active_recipe_id: int | None = None
    active_route_ids: list[int] = field(default_factory=list)
    active_route_count: int = 0
    source_count: int = 0
    panel_source_count: int = 0
    inventory_source_count: int = 0
    required_source_count: int = 0
    fallback_source_count: int = 0
    expected_output_count: int = 0
    global_fallback_available: bool = False
    sales_ready_inbound_count: int = 0
    readiness_status: str = READINESS_INCOMPLETE
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    legacy_source_used: bool = False
    legacy_ignored: bool = False

    @property
    def mode_label(self):
        return MODE_LABELS.get(self.effective_mode, self.effective_mode)

    @property
    def readiness_label(self):
        return READINESS_LABELS.get(self.readiness_status, self.readiness_status)

    @property
    def readiness_tone(self):
        return READINESS_TONES.get(self.readiness_status, "slate")

    @property
    def has_conflict(self):
        return self.effective_mode == MODE_CONFLICT

    @property
    def source_summary(self):
        if self.effective_mode == MODE_SUBSCRIPTION:
            parts = []
            if self.panel_source_count:
                parts.append(_("%(count)s پنل") % {"count": self.panel_source_count})
            if self.inventory_source_count:
                parts.append(_("%(count)s مخزن") % {"count": self.inventory_source_count})
            return " + ".join(parts) or "-"
        if self.effective_mode == MODE_DIRECT_LINKS:
            return _("لینک مستقیم — %(count)s منبع") % {"count": self.source_count}
        if self.effective_mode == MODE_CONFLICT:
            return _("%(routes)s route legacy / %(panels)s پنل") % {
                "routes": self.active_route_count,
                "panels": self.panel_source_count,
            }
        if self.global_fallback_available:
            return _("Fallback فعال / %(count)s اینباند آماده فروش") % {"count": self.sales_ready_inbound_count}
        return _("Fallback بدون منبع آماده")


def delivery_mode_choices():
    return (
        (MODE_GLOBAL_FALLBACK, MODE_LABELS[MODE_GLOBAL_FALLBACK]),
        (MODE_DIRECT_LINKS, MODE_LABELS[MODE_DIRECT_LINKS]),
        (MODE_SUBSCRIPTION, MODE_LABELS[MODE_SUBSCRIPTION]),
    )


def active_delivery_config_for_plan(plan):
    plan_id = getattr(plan, "pk", plan)
    if not plan_id:
        return None
    return (
        PlanDeliveryConfig.objects.filter(plan_id=plan_id, active=True)
        .select_related("plan", "plan__store")
        .first()
    )


def active_delivery_sources(delivery_config):
    return (
        PlanDeliverySource.objects.filter(delivery_config=delivery_config, active=True)
        .select_related("panel", "inbound", "inbound__panel", "inventory_pool")
        .order_by("priority", "pk")
    )


def active_plan_routes(plan, store=None):
    routes = PlanInboundRoute.objects.filter(plan=plan, is_active=True)
    if store and getattr(store, "pk", None):
        routes = routes.filter(
            models.Q(store=store) | models.Q(store__isnull=True),
            models.Q(inbound__panel__store=store) | models.Q(inbound__panel__store__isnull=True),
        )
    return routes.select_related("store", "operator", "inbound", "inbound__panel").order_by("operator_id", "priority", "pk")


def active_plan_recipes(plan):
    return CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).order_by("priority", "pk")


def _fallback_state(store):
    sales_ready_count = get_valid_sales_inbounds(store).count()
    fallback_enabled = bool(store and getattr(store, "allow_global_inbound_fallback", True))
    return sales_ready_count, bool(fallback_enabled and sales_ready_count)


def _legacy_warning(plan):
    has_routes = PlanInboundRoute.objects.filter(plan=plan, is_active=True).exists()
    has_recipe = CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).exists()
    if has_routes or has_recipe:
        return _("Legacy route/recipe configuration exists but is ignored because PlanDeliveryConfig is active.")
    return ""


def _source_readiness(source, store=None):
    if source.source_type == PlanDeliverySource.SourceType.PANEL_INBOUND:
        errors, warnings = sales_inbound_issues(source.inbound, store=store)
        return errors, warnings, 1 if source.inbound_id else 0
    if source.source_type == PlanDeliverySource.SourceType.INVENTORY_POOL:
        pool = source.inventory_pool
        errors = []
        warnings = []
        if not pool:
            errors.append(_("Inventory source requires a pool."))
            return errors, warnings, 0
        if not pool.is_active:
            errors.append(_("Inventory pool is inactive."))
        summary = get_pool_stock_summary(pool)
        capacity = summary.get("available_capacity")
        if capacity is not None and int(capacity or 0) < int(source.quantity or 1):
            errors.append(_("Inventory pool capacity is lower than requested quantity."))
        return errors, warnings, int(source.quantity or 1)
    return [_("Unsupported delivery source type.")], [], 0


def _v2_readiness(delivery_config, store=None):
    sources = list(active_delivery_sources(delivery_config))
    warnings = []
    errors = []
    panel_source_count = 0
    inventory_source_count = 0
    required_source_count = 0
    fallback_source_count = 0
    expected_output_count = 0

    for source in sources:
        if source.source_type == PlanDeliverySource.SourceType.PANEL_INBOUND:
            panel_source_count += 1
        elif source.source_type == PlanDeliverySource.SourceType.INVENTORY_POOL:
            inventory_source_count += 1
        if source.required:
            required_source_count += 1
        if source.is_fallback:
            fallback_source_count += 1
        source_errors, source_warnings, expected = _source_readiness(source, store=store)
        errors.extend(source_errors)
        warnings.extend(source_warnings)
        expected_output_count += expected

    if delivery_config.delivery_mode == MODE_GLOBAL_FALLBACK:
        sales_ready_count, fallback_available = _fallback_state(store)
        return {
            "source_count": 0,
            "panel_source_count": 0,
            "inventory_source_count": 0,
            "required_source_count": 0,
            "fallback_source_count": 0,
            "expected_output_count": 1 if fallback_available else 0,
            "readiness_status": READINESS_READY if fallback_available else READINESS_INCOMPLETE,
            "warnings": [] if fallback_available else [_("هیچ اینباند آماده فروش برای fallback فروشگاه وجود ندارد.")],
        }

    if not sources:
        errors.append(_("برای این روش تحویل حداقل یک منبع سرویس لازم است."))

    if errors:
        readiness_status = READINESS_INCOMPLETE
    elif warnings:
        readiness_status = READINESS_WARNING
    else:
        readiness_status = READINESS_READY
    return {
        "source_count": len(sources),
        "panel_source_count": panel_source_count,
        "inventory_source_count": inventory_source_count,
        "required_source_count": required_source_count,
        "fallback_source_count": fallback_source_count,
        "expected_output_count": expected_output_count,
        "readiness_status": readiness_status,
        "warnings": list(dict.fromkeys(errors + warnings)),
    }


def _route_readiness(routes, store=None):
    warnings = []
    errors = []
    panel_ids = set()
    for route in routes:
        inbound = route.inbound
        if getattr(inbound, "panel_id", None):
            panel_ids.add(inbound.panel_id)
        inbound_errors, inbound_warnings = sales_inbound_issues(inbound, store=store)
        errors.extend(inbound_errors)
        warnings.extend(inbound_warnings)
    if errors:
        return READINESS_INCOMPLETE, list(dict.fromkeys(errors + warnings)), len(panel_ids)
    if warnings:
        return READINESS_WARNING, list(dict.fromkeys(warnings)), len(panel_ids)
    return READINESS_READY, [], len(panel_ids)


def _recipe_readiness(recipe):
    from .plan_fulfillment_services import ERROR_STATUS, WARNING_STATUS, get_recipe_readiness

    readiness = get_recipe_readiness(recipe)
    status_code = readiness.get("status_code")
    if status_code == ERROR_STATUS:
        readiness_status = READINESS_INCOMPLETE
    elif status_code == WARNING_STATUS:
        readiness_status = READINESS_WARNING
    else:
        readiness_status = READINESS_READY
    warnings = list(readiness.get("errors") or []) + list(readiness.get("warnings") or [])
    return readiness, readiness_status, list(dict.fromkeys(warnings))


def _resolve_legacy(plan, store=None):
    effective_store = store or getattr(plan, "store", None)
    routes = list(active_plan_routes(plan, effective_store))
    recipes = list(active_plan_recipes(plan))
    active_recipe = recipes[0] if recipes else None
    sales_ready_count, fallback_available = _fallback_state(effective_store)
    route_status, route_warnings, route_panel_count = _route_readiness(routes, effective_store)
    active_route_ids = [route.pk for route in routes]

    if active_recipe and routes:
        readiness = None
        recipe_warnings = []
        try:
            readiness, _recipe_status, recipe_warnings = _recipe_readiness(active_recipe)
        except Exception as exc:
            recipe_warnings = [str(exc)]
        logger.warning(
            "Legacy plan delivery conflict detected plan_id=%s recipe_id=%s active_route_ids=%s",
            plan.pk,
            active_recipe.pk,
            active_route_ids,
        )
        return PlanDeliveryConfiguration(
            plan_id=plan.pk,
            effective_mode=MODE_CONFLICT,
            source_of_truth=SOURCE_CONFLICT,
            active_recipe_id=active_recipe.pk,
            active_route_ids=active_route_ids,
            active_route_count=len(routes),
            source_count=len(routes),
            panel_source_count=max(route_panel_count, (readiness or {}).get("panel_source_count", 0)),
            inventory_source_count=(readiness or {}).get("inventory_pool_count", 0),
            required_source_count=len(routes),
            expected_output_count=(readiness or {}).get("expected_config_count", len(routes)),
            global_fallback_available=fallback_available,
            sales_ready_inbound_count=sales_ready_count,
            readiness_status=READINESS_CONFLICT,
            warnings=list(dict.fromkeys(route_warnings + recipe_warnings)),
            conflicts=[_("این پلن هم‌زمان دارای مسیر مستقیم legacy و Recipe فعال است.")],
            legacy_source_used=True,
        )

    if active_recipe:
        readiness, readiness_status, warnings = _recipe_readiness(active_recipe)
        return PlanDeliveryConfiguration(
            plan_id=plan.pk,
            effective_mode=MODE_SUBSCRIPTION,
            source_of_truth=SOURCE_CUP_RECIPE,
            active_recipe_id=active_recipe.pk,
            source_count=int(readiness.get("required_rule_count", 0) or 0) + int(readiness.get("optional_rule_count", 0) or 0),
            panel_source_count=readiness.get("panel_source_count", 0),
            inventory_source_count=readiness.get("inventory_pool_count", 0),
            required_source_count=readiness.get("required_rule_count", 0),
            expected_output_count=readiness.get("expected_config_count", 0),
            global_fallback_available=fallback_available,
            sales_ready_inbound_count=sales_ready_count,
            readiness_status=readiness_status,
            warnings=warnings,
            legacy_source_used=True,
        )

    if routes:
        return PlanDeliveryConfiguration(
            plan_id=plan.pk,
            effective_mode=MODE_DIRECT_LINKS,
            source_of_truth=SOURCE_PLAN_ROUTES,
            active_route_ids=active_route_ids,
            active_route_count=len(routes),
            source_count=len(routes),
            panel_source_count=route_panel_count,
            required_source_count=len(routes),
            expected_output_count=len(routes),
            global_fallback_available=fallback_available,
            sales_ready_inbound_count=sales_ready_count,
            readiness_status=route_status,
            warnings=route_warnings,
            legacy_source_used=True,
        )

    return PlanDeliveryConfiguration(
        plan_id=plan.pk,
        effective_mode=MODE_GLOBAL_FALLBACK,
        source_of_truth=SOURCE_STORE_FALLBACK,
        global_fallback_available=fallback_available,
        sales_ready_inbound_count=sales_ready_count,
        readiness_status=READINESS_READY if fallback_available else READINESS_INCOMPLETE,
        expected_output_count=1 if fallback_available else 0,
        warnings=[] if fallback_available else [_("هیچ اینباند آماده فروش برای fallback فروشگاه وجود ندارد.")],
    )


def resolve_plan_delivery_configuration(plan, store=None):
    plan_id = getattr(plan, "pk", plan)
    if not plan_id:
        sales_ready_count, fallback_available = _fallback_state(store)
        return PlanDeliveryConfiguration(
            plan_id=None,
            effective_mode=MODE_GLOBAL_FALLBACK,
            source_of_truth=SOURCE_STORE_FALLBACK,
            global_fallback_available=fallback_available,
            sales_ready_inbound_count=sales_ready_count,
            readiness_status=READINESS_READY if fallback_available else READINESS_INCOMPLETE,
            expected_output_count=1 if fallback_available else 0,
            warnings=[] if fallback_available else [_("هیچ اینباند آماده فروش برای fallback فروشگاه وجود ندارد.")],
        )

    if not isinstance(plan, Plan):
        plan = Plan.objects.select_related("store").get(pk=plan_id)
    effective_store = store or getattr(plan, "store", None)
    delivery_config = active_delivery_config_for_plan(plan)
    if delivery_config:
        readiness = _v2_readiness(delivery_config, store=effective_store)
        warnings = list(readiness["warnings"])
        legacy_warning = _legacy_warning(plan)
        legacy_ignored = bool(legacy_warning)
        if legacy_warning:
            warnings.append(legacy_warning)
        return PlanDeliveryConfiguration(
            plan_id=plan.pk,
            effective_mode=delivery_config.delivery_mode,
            source_of_truth=SOURCE_V2_CONFIG,
            delivery_config_id=delivery_config.pk,
            failure_policy=delivery_config.failure_policy,
            global_fallback_available=_fallback_state(effective_store)[1],
            sales_ready_inbound_count=_fallback_state(effective_store)[0],
            legacy_ignored=legacy_ignored,
            warnings=list(dict.fromkeys(warnings)),
            **{key: value for key, value in readiness.items() if key != "warnings"},
        )

    return _resolve_legacy(plan, effective_store)


def get_or_create_delivery_config(plan, *, delivery_mode=None):
    config, _created = PlanDeliveryConfig.objects.get_or_create(
        plan=plan,
        defaults={
            "delivery_mode": delivery_mode or MODE_GLOBAL_FALLBACK,
            "failure_policy": PlanDeliveryConfig.FailurePolicy.STRICT,
            "active": True,
        },
    )
    if not config.active:
        config.active = True
        config.save(update_fields=["active", "updated_at"])
    return config


def save_delivery_config_sources(plan, *, delivery_mode, failure_policy, sources):
    config = get_or_create_delivery_config(plan, delivery_mode=delivery_mode)
    config.delivery_mode = delivery_mode
    config.failure_policy = failure_policy or PlanDeliveryConfig.FailurePolicy.STRICT
    config.active = True
    config.full_clean()
    config.save()

    existing = {source.pk: source for source in config.sources.all()}
    kept_ids = []
    for index, source_data in enumerate(sources, start=1):
        source_id = source_data.pop("id", None)
        source = existing.get(source_id) if source_id else None
        if not source:
            source = PlanDeliverySource(delivery_config=config)
        for field, value in source_data.items():
            setattr(source, field, value)
        if source.priority is None:
            source.priority = index * 10
        source.active = True
        source.full_clean()
        source.save()
        kept_ids.append(source.pk)

    config.sources.exclude(pk__in=kept_ids).update(active=False)
    return config

