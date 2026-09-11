from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .config_inventory_services import ConfigInventoryError, allocate_assets_from_pool
from .db_locking import select_for_update_self
from .external_subscription_sources import (
    preview_external_subscription_filter,
    register_external_subscription_feed_snapshot,
)
from .models import ConfigAllocation, ConfigLink, CupItem, Inbound, Order, Panel, PlanDeliveryConfig, PlanDeliverySource, SubscriptionCup
from .panels import get_safe_panel_adapter
from .panels.xui.adapter import XUIProvisioningRequest
from .plan_delivery_services import MODE_DIRECT_LINKS, MODE_GLOBAL_FALLBACK, MODE_SUBSCRIPTION, active_delivery_sources
from .subscription_cups import build_subscription_cup_url, create_config_link_from_raw
from .xui_api import XUIError


logger = logging.getLogger(__name__)


@dataclass
class DynamicFeedInitialSnapshot:
    provider: str
    panel: Panel
    delivery_source: PlanDeliverySource
    vpn_client: object | None
    protected_subscription_url: str
    remote_identity_ref: str
    raw_links: list[str] = field(default_factory=list)
    config_links: list[ConfigLink] = field(default_factory=list)
    filter_result: object | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class SourceExecutionResult:
    source_ids: list[int] = field(default_factory=list)
    source_type: str = ""
    required: bool = True
    is_fallback: bool = False
    ok: bool = False
    output_count: int = 0
    config_links: list[ConfigLink] = field(default_factory=list)
    vpn_clients: list = field(default_factory=list)
    raw_links: list[str] = field(default_factory=list)
    dynamic_feeds: list[DynamicFeedInitialSnapshot] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PlanDeliveryResult:
    intercepted: bool
    ok: bool
    mode: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_results: list[SourceExecutionResult] = field(default_factory=list)
    direct_links: list[str] = field(default_factory=list)
    config_links: list[ConfigLink] = field(default_factory=list)
    vpn_clients: list = field(default_factory=list)
    subscription_cups: list[SubscriptionCup] = field(default_factory=list)
    customer_subscription_url: str = ""
    customer_config_count: int = 0
    protected_upstream_subscription_urls: list[str] = field(default_factory=list)


def preview_plan_delivery(delivery_config):
    sources = list(active_delivery_sources(delivery_config))
    return {
        "delivery_config_id": delivery_config.pk,
        "delivery_mode": delivery_config.delivery_mode,
        "source_count": len(sources),
        "panel_source_count": sum(1 for source in sources if source.source_type == PlanDeliverySource.SourceType.PANEL_INBOUND),
        "inventory_source_count": sum(1 for source in sources if source.source_type == PlanDeliverySource.SourceType.INVENTORY_POOL),
        "expected_output_count": sum(int(source.quantity or 1) for source in sources),
    }


def _stable_idempotency_key(order, delivery_config):
    base = f"plan-delivery-v2:{order.public_id}:{order.order_tracking_code}:{delivery_config.pk}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _source_safe_error(exc, panel=None):
    from . import provisioning_services

    if isinstance(exc, ConfigInventoryError):
        return exc.safe_message
    return provisioning_services._safe_error(exc, panel=panel)


def _mark_order_provisioning(order, *, idempotency_key):
    from . import provisioning_services

    order.is_paid = True
    order.verification_status = Order.VerificationStatus.VERIFIED
    order.status = Order.Status.CONFIRMED
    provisioning_services._mark_provisioning_state(
        order,
        Order.ProvisioningStatus.PROVISIONING,
        idempotency_key=idempotency_key,
        increment=True,
    )
    order.save(
        update_fields=[
            "is_paid",
            "verification_status",
            "status",
            "provisioning_status",
            "provisioning_attempts",
            "provisioning_idempotency_key",
            "updated_at",
        ]
    )


def _finish_failed_order(order, *, safe_error, idempotency_key):
    from . import provisioning_services

    metadata = dict(order.metadata or {})
    plan_delivery = dict(metadata.get("plan_delivery_v2") or {})
    plan_delivery.update(
        {
            "status": "failed",
            "failed_at": timezone.now().isoformat(),
        }
    )
    metadata["plan_delivery_v2"] = plan_delivery
    order.metadata = metadata
    order.is_paid = True
    order.verification_status = Order.VerificationStatus.VERIFIED
    if order.status != Order.Status.COMPLETED:
        order.status = Order.Status.CONFIRMED
    provisioning_services._mark_provisioning_state(
        order,
        Order.ProvisioningStatus.FAILED,
        error=safe_error,
        idempotency_key=idempotency_key,
    )
    order.save(
        update_fields=[
            "is_paid",
            "verification_status",
            "status",
            "metadata",
            "provisioning_status",
            "last_provisioning_error",
            "provisioning_idempotency_key",
            "updated_at",
        ]
    )


def _panel_group_key(source):
    inbound = source.inbound
    panel = source.panel or (inbound.panel if inbound and inbound.panel_id else None)
    return getattr(panel, "pk", None)


def _validate_panel_inbound(inbound):
    if not inbound or not inbound.panel_id:
        raise XUIError("missing_panel_inbound")
    if inbound.security == Inbound.Security.REALITY and not inbound.pbk:
        raise XUIError("reality_missing_public_key")


def _is_pasarguard_panel(panel):
    return str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD


def _panel_config_link(raw_link, *, source, vpn_client=None, metadata=None):
    return create_config_link_from_raw(
        raw_link,
        source_type=ConfigLink.SourceType.PANEL_GENERATED,
        source_panel=source.panel or source.inbound.panel,
        source_inbound=source.inbound,
        vpn_client=vpn_client,
        metadata=metadata or {},
    )


def _execute_single_panel_source(order, source):
    from . import provisioning_services

    result = SourceExecutionResult(
        source_ids=[source.pk],
        source_type=source.source_type,
        required=source.required,
        is_fallback=source.is_fallback,
    )
    inbound = source.inbound
    panel = source.panel or (inbound.panel if inbound and inbound.panel_id else None)
    try:
        _validate_panel_inbound(inbound)
        quantity = max(int(source.quantity or 1), 1)
        identity_offset = int(source.pk or 0) * 1000
        for index in range(1, quantity + 1):
            identity = provisioning_services.order_identity(order, inbound, index=identity_offset + index)
            client_result = provisioning_services.lookup_existing_remote_client(panel, inbound, identity)
            if not client_result:
                client_result = provisioning_services.create_enabled_client_details(
                    email_prefix=identity["email_prefix"],
                    total_gb=order.plan.volume_gb,
                    duration_days=order.plan.duration_days,
                    panel=panel,
                    inbound=inbound,
                    limit_ip=order.plan.device_limit,
                    client_uuid=identity["uuid"],
                    sub_id=identity["sub_id"],
                    email=identity["email"],
                )
            if not client_result:
                raise XUIError("remote_create_failed")
            verified = provisioning_services.lookup_existing_remote_client(panel, inbound, identity)
            if not verified:
                raise XUIError("remote_verify_failed")
            client_result = {**client_result, **verified}
            vpn_client, _created = provisioning_services._upsert_local_client(order, inbound, client_result)
            direct_link = str(client_result.get("direct_link") or "").strip()
            if direct_link:
                config_link = _panel_config_link(
                    direct_link,
                    source=source,
                    vpn_client=vpn_client,
                    metadata={
                        "source": "plan_delivery_v2_panel",
                        "plan_delivery_source_id": source.pk,
                    },
                )
                result.raw_links.append(direct_link)
                result.config_links.append(config_link)
            result.vpn_clients.append(vpn_client)
        result.output_count = len(result.raw_links)
        result.ok = bool(result.output_count)
    except Exception as exc:
        result.errors.append(_source_safe_error(exc, panel=panel))
    return result


def _execute_pasarguard_panel_group(order, sources, *, dynamic_subscription=False):
    from . import provisioning_services
    from django.db.models import F

    sources = list(sources)
    first_source = sources[0]
    inbounds = [source.inbound for source in sources if source.inbound_id]
    panel = first_source.panel or (inbounds[0].panel if inbounds else None)
    result = SourceExecutionResult(
        source_ids=[source.pk for source in sources],
        source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
        required=any(source.required for source in sources),
        is_fallback=any(source.is_fallback for source in sources),
    )
    try:
        if not panel or not _is_pasarguard_panel(panel):
            raise XUIError("pasarguard_panel_required")
        if not inbounds:
            raise XUIError("pasarguard_group_required")
        if any(inbound.panel_id != panel.pk for inbound in inbounds):
            raise XUIError("pasarguard_cross_panel_group_selection")
        adapter = get_safe_panel_adapter(panel)
        report = adapter.get_capability_report()
        if not report.supports_create_client:
            raise XUIError("pasarguard_panel_does_not_support_create")
        quantity = max(max(int(source.quantity or 1) for source in sources), 1)
        for index in range(1, quantity + 1):
            identity = provisioning_services.multi_inbound_order_identity(order, inbounds, index=index)
            request = XUIProvisioningRequest(
                email_prefix=identity["email_prefix"],
                total_gb=order.plan.volume_gb,
                duration_days=order.plan.duration_days,
                inbound=inbounds[0] if len(inbounds) == 1 else None,
                inbounds=inbounds,
                limit_ip=order.plan.device_limit,
                client_uuid=identity["uuid"],
                sub_id=identity["sub_id"],
                email=identity["email"],
            )
            client_result = adapter.create_enabled_multi_inbound_client(request)
            raw_links = [str(link).strip() for link in (client_result.get("raw_links") or []) if str(link).strip()]
            if not raw_links and str(client_result.get("direct_link") or "").strip():
                raw_links = [str(client_result.get("direct_link")).strip()]
            if not raw_links:
                raise XUIError("pasarguard_raw_subscription_empty")
            client_result = {
                **client_result,
                "direct_link": raw_links[0],
                "raw": {
                    **(client_result.get("raw") or {}),
                    "pasarguard_group_pks": [inbound.pk for inbound in inbounds],
                    "pasarguard_group_ids": [inbound.inbound_id for inbound in inbounds],
                    "native_raw_delivery": True,
                },
            }
            vpn_client, created = provisioning_services._upsert_local_client(order, inbounds[0], client_result)
            if created and len(inbounds) > 1:
                Inbound.objects.filter(pk__in=[inbound.pk for inbound in inbounds[1:]]).update(
                    current_users=F("current_users") + 1,
                    updated_at=timezone.now(),
                )
            result.vpn_clients.append(vpn_client)
            selected_raw_links = raw_links
            filter_result = None
            if dynamic_subscription:
                filter_result = preview_external_subscription_filter(raw_links, source=first_source)
                if filter_result.selected_count <= 0:
                    raise XUIError("pasarguard_dynamic_subscription_filter_empty")
                selected_raw_links = [item.raw_link for item in filter_result.selected_configs]
                result.warnings.extend(
                    [
                        f"pasarguard_dynamic_subscription_invalid_configs={filter_result.invalid_count}"
                    ]
                    if filter_result.invalid_count
                    else []
                )

            created_config_links = []
            for raw_index, raw_link in enumerate(selected_raw_links, start=1):
                config_link = _panel_config_link(
                    raw_link,
                    source=first_source,
                    vpn_client=vpn_client,
                    metadata={
                        "source": "plan_delivery_v2_pasarguard_raw",
                        "plan_delivery_source_ids": [source.pk for source in sources],
                        "panel_id": panel.pk,
                        "pasarguard_group_pks": [inbound.pk for inbound in inbounds],
                        "pasarguard_group_ids": [inbound.inbound_id for inbound in inbounds],
                        "raw_index": raw_index,
                        "native_raw_delivery": True,
                        "external_subscription_initial": bool(dynamic_subscription),
                    },
                )
                result.raw_links.append(raw_link)
                result.config_links.append(config_link)
                created_config_links.append(config_link)

            if dynamic_subscription:
                result.dynamic_feeds.append(
                    DynamicFeedInitialSnapshot(
                        provider=Panel.Family.PASARGUARD,
                        panel=panel,
                        delivery_source=first_source,
                        vpn_client=vpn_client,
                        protected_subscription_url=str(client_result.get("sub_link") or ""),
                        remote_identity_ref=str(client_result.get("email") or ""),
                        raw_links=raw_links,
                        config_links=created_config_links,
                        filter_result=filter_result,
                        metadata={
                            "source": "plan_delivery_v2_pasarguard_dynamic_subscription",
                            "plan_delivery_source_ids": [source.pk for source in sources],
                            "pasarguard_group_pks": [inbound.pk for inbound in inbounds],
                            "pasarguard_group_ids": [inbound.inbound_id for inbound in inbounds],
                        },
                    )
                )
        result.output_count = len(result.raw_links)
        result.ok = bool(result.output_count)
    except Exception as exc:
        result.errors.append(_source_safe_error(exc, panel=panel))
    return result


def _execute_multi_panel_group(order, sources):
    from . import provisioning_services

    sources = list(sources)
    first_source = sources[0]
    inbounds = [source.inbound for source in sources]
    panel = first_source.panel or inbounds[0].panel
    result = SourceExecutionResult(
        source_ids=[source.pk for source in sources],
        source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
        required=any(source.required for source in sources),
        is_fallback=any(source.is_fallback for source in sources),
    )
    try:
        for inbound in inbounds:
            _validate_panel_inbound(inbound)
        quantity = max(max(int(source.quantity or 1) for source in sources), 1)
        for index in range(1, quantity + 1):
            identity = provisioning_services.multi_inbound_order_identity(order, inbounds, index=index)
            existing_results = [
                provisioning_services.lookup_existing_remote_client(panel, inbound, identity)
                for inbound in inbounds
            ]
            existing_count = sum(1 for item in existing_results if item)
            if existing_count and existing_count != len(inbounds):
                raise XUIError("partial_multi_inbound_remote_client_exists")
            if existing_count == len(inbounds):
                per_inbound_results = existing_results
            else:
                client_result = provisioning_services.create_enabled_multi_inbound_client_details(
                    email_prefix=identity["email_prefix"],
                    total_gb=order.plan.volume_gb,
                    duration_days=order.plan.duration_days,
                    panel=panel,
                    inbounds=inbounds,
                    limit_ip=order.plan.device_limit,
                    client_uuid=identity["uuid"],
                    sub_id=identity["sub_id"],
                    email=identity["email"],
                )
                if not client_result:
                    raise XUIError("remote_multi_inbound_create_failed")
                per_inbound_results = client_result.get("bundle_inbound_results") or []
            if len(per_inbound_results) != len(inbounds):
                raise XUIError("remote_multi_inbound_verify_failed")

            verified_results = []
            for inbound, candidate in zip(inbounds, per_inbound_results):
                verified = provisioning_services.lookup_existing_remote_client(panel, inbound, identity)
                if not verified:
                    raise XUIError("remote_multi_inbound_verify_failed")
                verified_results.append({**candidate, **verified})
            primary_result = {
                **verified_results[0],
                "bundle_inbound_results": verified_results,
                "raw": {
                    **(verified_results[0].get("raw") or {}),
                    "bundle_inbound_results": verified_results,
                    "bundle_inbound_pks": [inbound.pk for inbound in inbounds],
                },
            }
            vpn_client, _created = provisioning_services._upsert_local_client(order, inbounds[0], primary_result)
            result.vpn_clients.append(vpn_client)
            for source, inbound, verified in zip(sources, inbounds, verified_results):
                direct_link = str(verified.get("direct_link") or "").strip()
                if not direct_link:
                    continue
                config_link = _panel_config_link(
                    direct_link,
                    source=source,
                    vpn_client=vpn_client,
                    metadata={
                        "source": "plan_delivery_v2_panel_bundle",
                        "plan_delivery_source_id": source.pk,
                        "source_inbound_pk": inbound.pk,
                    },
                )
                result.raw_links.append(direct_link)
                result.config_links.append(config_link)
        result.output_count = len(result.raw_links)
        result.ok = bool(result.output_count)
    except Exception as exc:
        result.errors.append(_source_safe_error(exc, panel=panel))
    return result


def _execute_panel_sources(order, sources, *, dynamic_subscription=False):
    grouped = {}
    for source in sources:
        grouped.setdefault(_panel_group_key(source), []).append(source)
    results = []
    for group_sources in grouped.values():
        group_sources = sorted(group_sources, key=lambda source: (source.priority, source.pk))
        panel = group_sources[0].panel or group_sources[0].inbound.panel
        if _is_pasarguard_panel(panel):
            results.append(_execute_pasarguard_panel_group(order, group_sources, dynamic_subscription=dynamic_subscription))
        elif len(group_sources) > 1 and panel.capability_profile == Panel.CapabilityProfile.MODERN_MULTI_NODE:
            results.append(_execute_multi_panel_group(order, group_sources))
        else:
            for source in group_sources:
                results.append(_execute_single_panel_source(order, source))
    return results


def _execute_inventory_source(order, source):
    result = SourceExecutionResult(
        source_ids=[source.pk],
        source_type=source.source_type,
        required=source.required,
        is_fallback=source.is_fallback,
    )
    try:
        allocation_result = allocate_assets_from_pool(
            source.inventory_pool,
            source.quantity,
            order=order,
        )
        for asset, allocation in zip(allocation_result.assets, allocation_result.allocations):
            raw_link = str(asset.raw_link or "").strip()
            if not raw_link:
                continue
            config_link = create_config_link_from_raw(
                raw_link,
                source_type=ConfigLink.SourceType.IMPORTED_SUBSCRIPTION,
                metadata={
                    "source": "plan_delivery_v2_inventory",
                    "plan_delivery_source_id": source.pk,
                    "inventory_pool_id": source.inventory_pool_id,
                    "allocation_id": allocation.pk,
                },
            )
            result.raw_links.append(raw_link)
            result.config_links.append(config_link)
        result.output_count = len(result.raw_links)
        result.ok = bool(result.output_count)
    except Exception as exc:
        result.errors.append(_source_safe_error(exc))
    return result


def _execute_sources(order, sources, *, dynamic_subscription=False):
    panel_sources = [
        source
        for source in sources
        if source.source_type == PlanDeliverySource.SourceType.PANEL_INBOUND
    ]
    inventory_sources = [
        source
        for source in sources
        if source.source_type == PlanDeliverySource.SourceType.INVENTORY_POOL
    ]
    results = []
    if panel_sources:
        results.extend(_execute_panel_sources(order, panel_sources, dynamic_subscription=dynamic_subscription))
    for source in inventory_sources:
        results.append(_execute_inventory_source(order, source))
    return results


def _ensure_subscription_cup(order, delivery_config, config_links):
    cup = (
        SubscriptionCup.objects.select_for_update()
        .filter(order=order, vpn_client__isnull=True)
        .order_by("created_at", "pk")
        .first()
    )
    if not cup:
        cup = SubscriptionCup(order=order)
    cup.customer = order.customer
    cup.plan = order.plan
    cup.vpn_client = None
    cup.status = SubscriptionCup.Status.ACTIVE
    cup.expires_at = timezone.now() + timedelta(days=order.plan.duration_days)
    cup.traffic_limit_bytes = order.plan.traffic_limit_bytes
    cup.device_limit = order.plan.device_limit
    cup.title = order.plan.name
    metadata = dict(cup.metadata or {})
    metadata.update(
        {
            "source": "plan_delivery_v2",
            "delivery_config_id": delivery_config.pk,
            "last_built_at": timezone.now().isoformat(),
        }
    )
    cup.metadata = metadata
    cup.save()

    cup.items.update(is_active=False, updated_at=timezone.now())
    for position, config_link in enumerate(config_links, start=1):
        CupItem.objects.create(
            cup=cup,
            config_link=config_link,
            position=position,
            is_active=True,
            added_reason="plan_delivery_v2",
            metadata={"source": "plan_delivery_v2"},
        )
    allocation_ids = [
        (link.metadata or {}).get("allocation_id")
        for link in config_links
        if (link.metadata or {}).get("allocation_id")
    ]
    if allocation_ids:
        ConfigAllocation.objects.filter(pk__in=allocation_ids, order=order).update(cup=cup, updated_at=timezone.now())
    return cup


def _cup_active_item_count(cup):
    if not cup:
        return 0
    return CupItem.objects.filter(cup=cup, is_active=True, config_link__is_active=True).count()


def _successful_outputs(results):
    raw_links = []
    config_links = []
    vpn_clients = []
    dynamic_feeds = []
    for result in results:
        if not result.ok:
            continue
        raw_links.extend(result.raw_links)
        config_links.extend(result.config_links)
        vpn_clients.extend(result.vpn_clients)
        dynamic_feeds.extend(result.dynamic_feeds)
    return raw_links, config_links, vpn_clients, dynamic_feeds


def _should_use_fallback(results):
    if not results:
        return True
    if not any(result.ok for result in results):
        return True
    return any(result.errors for result in results)


def _finish_success_order(order, delivery_config, *, mode, raw_links, config_links, vpn_clients, source_results, dynamic_feeds=None, actor=None):
    from . import provisioning_services

    primary_client = vpn_clients[0] if vpn_clients else None
    subscription_cups = []
    registered_feeds = []
    if mode == MODE_SUBSCRIPTION:
        cup = _ensure_subscription_cup(order, delivery_config, config_links)
        for snapshot in dynamic_feeds or []:
            registered_feeds.append(
                register_external_subscription_feed_snapshot(
                    cup=cup,
                    source=snapshot.delivery_source,
                    panel=snapshot.panel,
                    vpn_client=snapshot.vpn_client,
                    protected_subscription_url=snapshot.protected_subscription_url,
                    remote_identity_ref=snapshot.remote_identity_ref,
                    raw_links=snapshot.raw_links,
                    config_links=snapshot.config_links,
                    filter_result=snapshot.filter_result,
                    provider=snapshot.provider,
                    metadata=snapshot.metadata,
                )
            )
        subscription_cups = [cup]
        order.sub_link = build_subscription_cup_url(cup, store=order.store)
        order.direct_link = ""
    else:
        order.sub_link = ""
        order.direct_link = raw_links[0] if raw_links else ""

    order.uuid = getattr(primary_client, "uuid", None) or order.uuid
    order.username = order.username or getattr(primary_client, "username", "") or ""
    order.mark_payment_verified(user=actor)
    metadata = dict(order.metadata or {})
    metadata["panel_provisioning_deferred"] = False
    metadata["panel_provisioning_reason"] = ""
    metadata["panel_provisioned_at"] = timezone.now().isoformat()
    metadata["plan_delivery_v2"] = {
        "status": "provisioned",
        "delivery_config_id": delivery_config.pk,
        "delivery_mode": mode,
        "source_result_count": len(source_results),
        "successful_source_result_count": sum(1 for result in source_results if result.ok),
        "failed_source_result_count": sum(1 for result in source_results if not result.ok),
        "config_link_count": len(config_links),
        "vpn_client_count": len(vpn_clients),
        "subscription_cup_ids": [cup.pk for cup in subscription_cups],
        "external_feed_ids": [feed.pk for feed in registered_feeds] if mode == MODE_SUBSCRIPTION else [],
    }
    if mode == MODE_DIRECT_LINKS:
        metadata["direct_delivery_links"] = raw_links
    else:
        metadata.pop("direct_delivery_links", None)
    order.metadata = metadata
    provisioning_services._mark_provisioning_state(
        order,
        Order.ProvisioningStatus.PROVISIONED,
        error="",
        provisioned_at=timezone.now(),
    )
    order.save(
        update_fields=[
            "uuid",
            "sub_link",
            "direct_link",
            "username",
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
    return subscription_cups


def execute_plan_delivery(order, delivery_config, actor=None, dry_run=False):
    if delivery_config.delivery_mode == MODE_GLOBAL_FALLBACK:
        return PlanDeliveryResult(intercepted=False, ok=False, mode=delivery_config.delivery_mode)
    if delivery_config.delivery_mode not in {MODE_DIRECT_LINKS, MODE_SUBSCRIPTION}:
        return PlanDeliveryResult(
            intercepted=True,
            ok=False,
            mode=delivery_config.delivery_mode,
            errors=["unsupported_delivery_mode"],
        )
    if dry_run:
        preview = preview_plan_delivery(delivery_config)
        return PlanDeliveryResult(
            intercepted=True,
            ok=bool(preview["source_count"]),
            mode=delivery_config.delivery_mode,
            warnings=[] if preview["source_count"] else ["no_active_sources"],
        )

    idempotency_key = _stable_idempotency_key(order, delivery_config)
    with transaction.atomic():
        delivery_config = (
            select_for_update_self(
                PlanDeliveryConfig.objects.select_related("plan", "plan__store")
            ).get(pk=delivery_config.pk)
        )
        sources = list(active_delivery_sources(delivery_config))
        if not sources:
            _finish_failed_order(order, safe_error="no_active_plan_delivery_sources", idempotency_key=idempotency_key)
            return PlanDeliveryResult(
                intercepted=True,
                ok=False,
                mode=delivery_config.delivery_mode,
                errors=["no_active_plan_delivery_sources"],
            )

        _mark_order_provisioning(order, idempotency_key=idempotency_key)
        primary_sources = [source for source in sources if not source.is_fallback]
        fallback_sources = [source for source in sources if source.is_fallback]
        source_results = _execute_sources(
            order,
            primary_sources,
            dynamic_subscription=delivery_config.delivery_mode == MODE_SUBSCRIPTION,
        )
        if fallback_sources and _should_use_fallback(source_results):
            source_results.extend(
                _execute_sources(
                    order,
                    fallback_sources,
                    dynamic_subscription=delivery_config.delivery_mode == MODE_SUBSCRIPTION,
                )
            )

        raw_links, config_links, vpn_clients, dynamic_feeds = _successful_outputs(source_results)
        required_failures = [
            result
            for result in source_results
            if result.required and not result.ok
        ]
        errors = [error for result in source_results for error in result.errors]
        warnings = [warning for result in source_results for warning in result.warnings]
        strict = delivery_config.failure_policy == PlanDeliveryConfig.FailurePolicy.STRICT
        ok = bool(raw_links) and not (strict and required_failures)
        if not ok:
            safe_error = "; ".join(errors[:3]) or "plan_delivery_v2_failed"
            _finish_failed_order(order, safe_error=safe_error, idempotency_key=idempotency_key)
            return PlanDeliveryResult(
                intercepted=True,
                ok=False,
                mode=delivery_config.delivery_mode,
                errors=errors or ["plan_delivery_v2_failed"],
                warnings=warnings,
                source_results=source_results,
                direct_links=raw_links,
                config_links=config_links,
                vpn_clients=vpn_clients,
            )

        subscription_cups = _finish_success_order(
            order,
            delivery_config,
            mode=delivery_config.delivery_mode,
            raw_links=raw_links,
            config_links=config_links,
            vpn_clients=vpn_clients,
            source_results=source_results,
            dynamic_feeds=dynamic_feeds,
            actor=actor,
        )
        customer_subscription_url = order.sub_link if delivery_config.delivery_mode == MODE_SUBSCRIPTION else ""
        customer_config_count = _cup_active_item_count(subscription_cups[0]) if subscription_cups else 0
        protected_upstream_subscription_urls = [
            str(snapshot.protected_subscription_url or "")
            for snapshot in dynamic_feeds
            if str(snapshot.protected_subscription_url or "").strip()
        ]
    return PlanDeliveryResult(
        intercepted=True,
        ok=True,
        mode=delivery_config.delivery_mode,
        warnings=warnings,
        source_results=source_results,
        direct_links=raw_links,
        config_links=config_links,
        vpn_clients=vpn_clients,
        subscription_cups=subscription_cups,
        customer_subscription_url=customer_subscription_url,
        customer_config_count=customer_config_count,
        protected_upstream_subscription_urls=protected_upstream_subscription_urls,
    )
