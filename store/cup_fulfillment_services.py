from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .config_inventory_services import (
    ConfigInventoryError,
    allocate_assets_from_pool,
    get_pool_stock_summary,
)
from .models import (
    ConfigAllocation,
    ConfigInventoryPool,
    ConfigLink,
    CupFillerRule,
    CupFulfillmentRecipe,
    CupItem,
    Inbound,
    Order,
    Panel,
    SubscriptionCup,
)
from .naming import build_client_display_name
from .panels import PanelIntegrationError, get_safe_panel_adapter
from .panels.xui.adapter import XUIProvisioningRequest
from .subscription_cups import create_config_link_from_raw
from .xui_api import sanitize_xui_operational_text


FULFILLMENT_STATUS_SUCCESS = "success"
FULFILLMENT_STATUS_PARTIAL = "partial"
FULFILLMENT_STATUS_FAILED = "failed"
FULFILLMENT_STATUS_NOT_CONFIGURED = "not_configured"


@dataclass
class RuleFulfillmentResult:
    rule_id: int | None
    source_type: str
    position: int
    required: bool = True
    success: bool = False
    created_link_count: int = 0
    inventory_allocation_count: int = 0
    panel_generated_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_safe_dict(self):
        return {
            "rule_id": self.rule_id,
            "source_type": self.source_type,
            "position": self.position,
            "required": self.required,
            "success": self.success,
            "created_link_count": self.created_link_count,
            "inventory_allocation_count": self.inventory_allocation_count,
            "panel_generated_count": self.panel_generated_count,
            "errors": self.errors,
            "warnings": self.warnings,
            "metadata": self.metadata,
        }


@dataclass
class CupFulfillmentResult:
    status: str
    cup: SubscriptionCup | None = None
    recipe: CupFulfillmentRecipe | None = None
    created_link_count: int = 0
    inventory_allocation_count: int = 0
    panel_generated_count: int = 0
    rule_results: list[RuleFulfillmentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    intercepted: bool = True

    @property
    def ok(self):
        return self.status in {FULFILLMENT_STATUS_SUCCESS, FULFILLMENT_STATUS_PARTIAL}

    @property
    def subscription_cups(self):
        return [self.cup] if self.cup else []

    def to_safe_dict(self):
        return {
            "status": self.status,
            "cup_id": getattr(self.cup, "pk", None),
            "recipe_id": getattr(self.recipe, "pk", None),
            "created_link_count": self.created_link_count,
            "inventory_allocation_count": self.inventory_allocation_count,
            "panel_generated_count": self.panel_generated_count,
            "errors": self.errors,
            "warnings": self.warnings,
            "rule_results": [result.to_safe_dict() for result in self.rule_results],
        }


def get_active_recipe_for_plan(plan):
    plan_id = getattr(plan, "pk", plan)
    if not plan_id:
        return None
    return (
        CupFulfillmentRecipe.objects.filter(plan_id=plan_id, is_active=True)
        .order_by("priority", "pk")
        .first()
    )


def order_has_active_fulfillment_recipe(order):
    return bool(get_active_recipe_for_plan(getattr(order, "plan_id", None)))


def _safe_error(exc, *, panel=None):
    if isinstance(exc, ConfigInventoryError):
        return exc.safe_message
    if isinstance(exc, PanelIntegrationError):
        return exc.message
    return sanitize_xui_operational_text(exc, panel=panel, max_length=700)


def _safe_email_prefix(order, rule, index):
    metadata = order.metadata or {}
    base = (
        order.username
        or build_client_display_name(
            order.customer,
            order=order,
            preferred_name=order.sender_card_name,
            short_id=order.order_tracking_code,
            metadata=metadata,
        )
        or f"order-{order.pk}"
    )
    value = f"{base}-r{getattr(rule, 'position', 0)}-{index}"
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-")[:80] or f"order-{order.pk}-r{getattr(rule, 'position', 0)}-{index}"


def _cup_expires_at(order):
    try:
        duration_days = int(getattr(order.plan, "duration_days", 0) or 0)
    except Exception:
        duration_days = 0
    if duration_days <= 0:
        return None
    return timezone.now() + timedelta(days=duration_days)


def _create_or_reuse_order_cup(order, recipe):
    cup = (
        SubscriptionCup.objects.select_for_update()
        .filter(order=order, vpn_client__isnull=True)
        .order_by("created_at", "pk")
        .first()
    )
    if not cup:
        cup = SubscriptionCup(order=order)
    metadata = dict(cup.metadata or {})
    metadata.update(
        {
            "source": "cup_fulfillment_recipe",
            "recipe_id": recipe.pk,
            "recipe_title": recipe.title,
            "last_fulfilled_at": timezone.now().isoformat(),
        }
    )
    cup.customer = order.customer
    cup.plan = order.plan
    cup.status = SubscriptionCup.Status.ACTIVE
    cup.title = recipe.title or getattr(order.plan, "name", "")
    cup.traffic_limit_bytes = getattr(order.plan, "traffic_limit_bytes", 0) or 0
    cup.device_limit = getattr(order.plan, "device_limit", None)
    cup.expires_at = cup.expires_at or _cup_expires_at(order)
    cup.metadata = metadata
    cup.save()
    return cup


def _next_position(cup):
    current = cup.items.aggregate(max_position=Max("position")).get("max_position")
    return int(current or 0) + 1


def _direct_link_entries(remote_result, inbounds):
    inbounds = list(inbounds or [])
    if len(inbounds) <= 1:
        return [
            {
                "inbound": inbounds[0] if inbounds else None,
                "remote_result": remote_result or {},
                "direct_link": str((remote_result or {}).get("direct_link") or "").strip(),
            }
        ]
    bundle_results = list((remote_result or {}).get("bundle_inbound_results") or [])
    entries = []
    for index, inbound in enumerate(inbounds):
        item_result = bundle_results[index] if index < len(bundle_results) and isinstance(bundle_results[index], dict) else {}
        entries.append(
            {
                "inbound": inbound,
                "remote_result": item_result or remote_result or {},
                "direct_link": str(item_result.get("direct_link") or "").strip(),
            }
        )
    return entries


def _panel_rule_inbounds(rule):
    inbounds = list(rule.inbounds.select_related("panel").order_by("pk"))
    if not inbounds and rule.panel_id:
        inbounds = list(
            Inbound.objects.select_related("panel")
            .filter(panel=rule.panel, is_active=True, available_for_new_orders=True)
            .order_by("pk")[:1]
        )
    return inbounds


def _inbound_missing_reality_pbk(inbound):
    return inbound.security == Inbound.Security.REALITY and not str(inbound.pbk or "").strip()


def _create_panel_links_for_rule(order, cup, rule, *, adapter_factory):
    panel = rule.panel
    inbounds = _panel_rule_inbounds(rule)
    if not panel and inbounds:
        panel = inbounds[0].panel
    result = RuleFulfillmentResult(
        rule_id=rule.pk,
        source_type=rule.source_type,
        position=rule.position,
        required=rule.required,
        metadata={
            "panel_id": getattr(panel, "pk", None),
            "inbound_pks": [inbound.pk for inbound in inbounds],
        },
    )
    if not panel or not inbounds:
        result.errors.append("Panel rule requires a panel and at least one inbound.")
        return result
    if any(inbound.panel_id != panel.pk for inbound in inbounds):
        result.errors.append("Panel rule contains an inbound from a different panel.")
        return result
    if any(_inbound_missing_reality_pbk(inbound) for inbound in inbounds):
        result.errors.append("Reality inbound is missing public key; source was not provisioned.")
        return result

    adapter = adapter_factory(panel)
    try:
        report = adapter.get_capability_report()
    except Exception:
        report = None
    if report is not None and not getattr(report, "supports_create_client", False):
        result.errors.append("Panel does not currently support create_client.")
        return result
    if len(inbounds) > 1 and report is not None and not getattr(report, "supports_multi_inbound_create", False):
        result.errors.append("Panel does not currently support multi-inbound create.")
        return result

    for index in range(1, int(rule.quantity or 1) + 1):
        request = XUIProvisioningRequest(
            email_prefix=_safe_email_prefix(order, rule, index),
            total_gb=getattr(order.plan, "volume_gb", Decimal("1")) or Decimal("1"),
            duration_days=int(getattr(order.plan, "duration_days", 30) or 30),
            inbound=inbounds[0] if len(inbounds) == 1 else None,
            inbounds=inbounds if len(inbounds) > 1 else None,
            limit_ip=int(getattr(order.plan, "device_limit", 2) or 2),
        )
        try:
            remote_result = (
                adapter.create_enabled_multi_inbound_client(request)
                if len(inbounds) > 1
                else adapter.create_enabled_client(request)
            )
            entries = _direct_link_entries(remote_result, inbounds)
            if any(not entry["direct_link"] for entry in entries):
                result.errors.append("Panel created a client but did not return all direct links.")
                return result
            for entry in entries:
                inbound = entry["inbound"]
                config_link = create_config_link_from_raw(
                    entry["direct_link"],
                    source_type=ConfigLink.SourceType.PANEL_GENERATED,
                    source_panel=panel,
                    source_inbound=inbound,
                    metadata={
                        "source": "cup_fulfillment_recipe",
                        "recipe_id": rule.recipe_id,
                        "rule_id": rule.pk,
                        "panel_id": panel.pk,
                        "inbound_pk": getattr(inbound, "pk", None),
                        "remote_email_saved": bool((entry["remote_result"] or {}).get("email")),
                        "remote_sub_link_saved": bool((entry["remote_result"] or {}).get("sub_link")),
                    },
                )
                CupItem.objects.create(
                    cup=cup,
                    config_link=config_link,
                    position=_next_position(cup),
                    is_active=True,
                    added_reason="fulfillment_recipe_panel",
                    metadata={
                        "source": "cup_fulfillment_recipe",
                        "recipe_id": rule.recipe_id,
                        "rule_id": rule.pk,
                        "panel_id": panel.pk,
                        "inbound_pk": getattr(inbound, "pk", None),
                    },
                )
                result.created_link_count += 1
                result.panel_generated_count += 1
        except Exception as exc:
            result.errors.append(_safe_error(exc, panel=panel))
            return result

    result.success = True
    return result


def _create_inventory_links_for_rule(order, cup, rule):
    pool = rule.inventory_pool
    result = RuleFulfillmentResult(
        rule_id=rule.pk,
        source_type=rule.source_type,
        position=rule.position,
        required=rule.required,
        metadata={"pool_id": getattr(pool, "pk", None)},
    )
    if not pool:
        result.errors.append("Inventory rule requires an inventory pool.")
        return result
    try:
        allocation_result = allocate_assets_from_pool(
            pool,
            int(rule.quantity or 1),
            cup=cup,
            order=order,
            allocation_mode=rule.allocation_mode or None,
        )
    except Exception as exc:
        result.errors.append(_safe_error(exc))
        return result

    for allocation, asset in zip(allocation_result.allocations, allocation_result.assets):
        config_link = create_config_link_from_raw(
            asset.raw_link,
            source_type=ConfigLink.SourceType.IMPORTED_SUBSCRIPTION,
            metadata={
                "source": "config_inventory_pool",
                "pool_id": pool.pk,
                "asset_id": asset.pk,
                "allocation_id": allocation.pk,
                "allocation_mode": allocation.allocation_mode,
            },
        )
        CupItem.objects.create(
            cup=cup,
            config_link=config_link,
            position=_next_position(cup),
            is_active=True,
            added_reason="fulfillment_recipe_inventory",
            metadata={
                "source": "config_inventory_pool",
                "recipe_id": rule.recipe_id,
                "rule_id": rule.pk,
                "pool_id": pool.pk,
                "asset_id": asset.pk,
                "allocation_id": allocation.pk,
            },
        )
        result.created_link_count += 1
        result.inventory_allocation_count += 1

    result.success = True
    return result


def _run_rule(order, cup, rule, *, adapter_factory):
    if rule.source_type == CupFillerRule.SourceType.PANEL_INBOUNDS:
        return _create_panel_links_for_rule(order, cup, rule, adapter_factory=adapter_factory)
    if rule.source_type == CupFillerRule.SourceType.INVENTORY_POOL:
        return _create_inventory_links_for_rule(order, cup, rule)
    return RuleFulfillmentResult(
        rule_id=rule.pk,
        source_type=rule.source_type,
        position=rule.position,
        required=rule.required,
        errors=["Rule source type is not supported."],
    )


def _metadata_for_order(result):
    return {
        "recipe_id": getattr(result.recipe, "pk", None),
        "status": result.status,
        "created_link_count": result.created_link_count,
        "inventory_allocation_count": result.inventory_allocation_count,
        "panel_generated_count": result.panel_generated_count,
        "errors": result.errors,
        "warnings": result.warnings,
        "rule_results": [rule_result.to_safe_dict() for rule_result in result.rule_results],
    }


def fulfill_order_with_recipe(order, *, actor=None, adapter_factory=None):
    adapter_factory = adapter_factory or get_safe_panel_adapter
    order = Order.objects.select_related("customer", "plan", "store").get(pk=getattr(order, "pk", order))
    recipe = get_active_recipe_for_plan(order.plan_id)
    if not recipe:
        return CupFulfillmentResult(status=FULFILLMENT_STATUS_NOT_CONFIGURED, intercepted=False)

    result = CupFulfillmentResult(status=FULFILLMENT_STATUS_FAILED, recipe=recipe, intercepted=True)
    with transaction.atomic():
        order = Order.objects.select_for_update().select_related("customer", "plan", "store").get(pk=order.pk)
        cup = _create_or_reuse_order_cup(order, recipe)
        result.cup = cup
        rules = list(
            recipe.rules.filter(is_active=True)
            .select_related("panel", "inventory_pool")
            .prefetch_related("inbounds")
            .order_by("position", "pk")
        )
        if not rules:
            result.errors.append("Fulfillment recipe has no active rules.")
        required_failed = False

        for rule in rules:
            rule_result = _run_rule(order, cup, rule, adapter_factory=adapter_factory)
            result.rule_results.append(rule_result)
            result.created_link_count += rule_result.created_link_count
            result.inventory_allocation_count += rule_result.inventory_allocation_count
            result.panel_generated_count += rule_result.panel_generated_count
            if rule_result.errors:
                if rule.required:
                    required_failed = True
                    result.errors.extend(rule_result.errors)
                    if recipe.failure_policy == CupFulfillmentRecipe.FailurePolicy.STRICT:
                        break
                else:
                    result.warnings.extend(rule_result.errors)
            result.warnings.extend(rule_result.warnings)

        if result.created_link_count and (
            not required_failed
            or recipe.failure_policy == CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED
        ):
            result.status = FULFILLMENT_STATUS_PARTIAL if required_failed else FULFILLMENT_STATUS_SUCCESS
            order.mark_payment_verified(user=actor)
            order.provisioning_status = Order.ProvisioningStatus.PROVISIONED
            order.last_provisioning_error = ""
            order.provisioned_at = timezone.now()
        else:
            result.status = FULFILLMENT_STATUS_FAILED
            order.is_paid = True
            order.verification_status = Order.VerificationStatus.VERIFIED
            if order.status != Order.Status.COMPLETED:
                order.status = Order.Status.CONFIRMED
            order.provisioning_status = Order.ProvisioningStatus.FAILED
            order.last_provisioning_error = "; ".join(result.errors[:3]) or "Cup fulfillment recipe failed."

        metadata = dict(order.metadata or {})
        metadata["cup_fulfillment_recipe"] = _metadata_for_order(result)
        metadata["panel_provisioning_deferred"] = False
        metadata["panel_provisioning_reason"] = "" if result.ok else "cup_fulfillment_recipe_failed"
        order.metadata = metadata
        order.save(
            update_fields=[
                "is_paid",
                "verification_status",
                "verified_by",
                "verified_at",
                "status",
                "metadata",
                "provisioning_status",
                "last_provisioning_error",
                "provisioned_at",
                "updated_at",
            ]
        )
        ConfigAllocation.objects.filter(cup=cup, order__isnull=True).update(order=order)

    return result


def preview_fulfillment_recipe(recipe):
    recipe = (
        CupFulfillmentRecipe.objects.select_related("plan")
        .prefetch_related("rules__inbounds")
        .get(pk=getattr(recipe, "pk", recipe))
    )
    rules = []
    warnings = []
    for rule in recipe.rules.select_related("panel", "inventory_pool").prefetch_related("inbounds").order_by("position", "pk"):
        inbounds = list(rule.inbounds.select_related("panel").order_by("pk"))
        stock_summary = get_pool_stock_summary(rule.inventory_pool) if rule.inventory_pool_id else None
        rule_summary = {
            "rule_id": rule.pk,
            "position": rule.position,
            "source_type": rule.source_type,
            "quantity": rule.quantity,
            "required": rule.required,
            "panel": str(rule.panel) if rule.panel_id else "",
            "inbounds": [str(inbound) for inbound in inbounds],
            "pool": str(rule.inventory_pool) if rule.inventory_pool_id else "",
            "stock_summary": stock_summary,
        }
        if rule.source_type == CupFillerRule.SourceType.PANEL_INBOUNDS and not inbounds:
            warnings.append(f"Rule {rule.position}: panel rule has no selected inbounds.")
        if rule.source_type == CupFillerRule.SourceType.INVENTORY_POOL:
            capacity = (stock_summary or {}).get("available_capacity")
            if capacity == 0 or (isinstance(capacity, int) and capacity < int(rule.quantity or 1)):
                warnings.append(f"Rule {rule.position}: inventory stock may be insufficient.")
        rules.append(rule_summary)
    return {
        "recipe_id": recipe.pk,
        "title": recipe.title,
        "plan": str(recipe.plan),
        "failure_policy": recipe.failure_policy,
        "rules": rules,
        "warnings": warnings,
    }
