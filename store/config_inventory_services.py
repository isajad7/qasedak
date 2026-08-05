from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import ConfigAllocation, ConfigInventoryAsset, ConfigInventoryPool, ConfigLink
from .subscription_cups import parse_config_link


class ConfigInventoryError(Exception):
    def __init__(self, safe_message, *, code="config_inventory_error"):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.code = code


class InsufficientInventoryStock(ConfigInventoryError):
    def __init__(self, pool, requested, available):
        super().__init__(
            f"Pool '{getattr(pool, 'title', '-')}' has insufficient stock: requested {requested}, available {available}.",
            code="insufficient_stock",
        )
        self.pool = pool
        self.requested = requested
        self.available = available


@dataclass
class InventoryImportResult:
    pool: ConfigInventoryPool
    created_count: int = 0
    skipped_count: int = 0
    duplicate_count: int = 0
    total_count: int = 0
    assets: list[ConfigInventoryAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class InventoryAllocationResult:
    pool: ConfigInventoryPool
    allocation_mode: str
    requested_quantity: int
    assets: list[ConfigInventoryAsset] = field(default_factory=list)
    allocations: list[ConfigAllocation] = field(default_factory=list)

    @property
    def allocated_count(self):
        return len(self.allocations)


def _pool_instance(pool):
    if isinstance(pool, ConfigInventoryPool):
        return pool
    return ConfigInventoryPool.objects.get(pk=pool)


def _not_expired_q():
    return Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())


def import_config_assets(pool, raw_text, source_batch=None):
    pool = _pool_instance(pool)
    result = InventoryImportResult(pool=pool)
    source_batch = str(source_batch or "").strip()

    for line_number, line in enumerate(str(raw_text or "").splitlines(), start=1):
        raw_link = str(line or "").strip()
        if not raw_link:
            result.skipped_count += 1
            continue
        result.total_count += 1
        parsed = parse_config_link(raw_link)
        if parsed.protocol == ConfigLink.Protocol.UNKNOWN:
            result.skipped_count += 1
            result.warnings.append(f"Line {line_number} skipped: unsupported config protocol.")
            continue
        if parsed.normalized_hash and ConfigInventoryAsset.objects.filter(
            pool=pool,
            normalized_hash=parsed.normalized_hash,
        ).exists():
            result.duplicate_count += 1

        asset = ConfigInventoryAsset.objects.create(
            pool=pool,
            raw_link=parsed.raw_link,
            normalized_link=parsed.normalized_link,
            normalized_hash=parsed.normalized_hash,
            protocol=parsed.protocol,
            remark=parsed.remark,
            host=parsed.host,
            port=parsed.port,
            status=ConfigInventoryAsset.Status.AVAILABLE,
            traffic_limit_gb=pool.traffic_limit_gb,
            max_allocations=pool.max_allocations_per_asset,
            source_batch=source_batch,
            metadata={
                "source": "inventory_import",
                "source_batch": source_batch,
                "import_line": line_number,
            },
        )
        result.assets.append(asset)
        result.created_count += 1
    return result


def _asset_allocation_limit(asset, pool):
    limit = asset.max_allocations if asset.max_allocations is not None else pool.max_allocations_per_asset
    if limit is None and pool.allocation_mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        return 1
    return limit


def _shared_candidate_queryset(pool):
    return (
        ConfigInventoryAsset.objects.select_for_update()
        .filter(
            pool=pool,
            status__in=[
                ConfigInventoryAsset.Status.AVAILABLE,
                ConfigInventoryAsset.Status.ASSIGNED,
            ],
        )
        .filter(_not_expired_q())
        .order_by("current_allocations", "created_at", "pk")
    )


def _select_exclusive_assets(pool, quantity):
    assets = list(
        ConfigInventoryAsset.objects.select_for_update()
        .filter(pool=pool, status=ConfigInventoryAsset.Status.AVAILABLE)
        .filter(_not_expired_q())
        .order_by("created_at", "pk")[:quantity]
    )
    if len(assets) < quantity:
        raise InsufficientInventoryStock(pool, quantity, len(assets))
    return assets


def _select_shared_limited_assets(pool, quantity):
    selected = []
    available_slots = 0
    for asset in _shared_candidate_queryset(pool):
        limit = _asset_allocation_limit(asset, pool)
        if limit is None:
            continue
        slots = max(int(limit or 0) - int(asset.current_allocations or 0), 0)
        available_slots += slots
        while slots > 0 and len(selected) < quantity:
            selected.append(asset)
            slots -= 1
        if len(selected) >= quantity:
            return selected
    raise InsufficientInventoryStock(pool, quantity, available_slots)


def _select_shared_unlimited_assets(pool, quantity):
    asset = _shared_candidate_queryset(pool).first()
    if not asset:
        raise InsufficientInventoryStock(pool, quantity, 0)
    return [asset for _index in range(quantity)]


def _selected_asset_counts(assets):
    ids = Counter(asset.pk for asset in assets)
    by_id = {}
    for asset in assets:
        by_id.setdefault(asset.pk, asset)
    return [(by_id[asset_id], count) for asset_id, count in ids.items()]


def allocate_assets_from_pool(pool, quantity, cup=None, order=None, allocation_mode=None):
    pool = _pool_instance(pool)
    quantity = max(int(quantity or 0), 0)
    if quantity < 1:
        raise ConfigInventoryError("Allocation quantity must be at least 1.", code="invalid_quantity")
    mode = allocation_mode or pool.allocation_mode
    if mode not in ConfigInventoryPool.AllocationMode.values:
        raise ConfigInventoryError("Allocation mode is not valid.", code="invalid_allocation_mode")
    if not pool.is_active:
        raise ConfigInventoryError(f"Pool '{pool.title}' is not active.", code="pool_inactive")

    with transaction.atomic():
        pool = ConfigInventoryPool.objects.select_for_update().get(pk=pool.pk)
        if mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
            assets = _select_exclusive_assets(pool, quantity)
        elif mode == ConfigInventoryPool.AllocationMode.SHARED_LIMITED:
            assets = _select_shared_limited_assets(pool, quantity)
        else:
            assets = _select_shared_unlimited_assets(pool, quantity)

        for asset, count in _selected_asset_counts(assets):
            ConfigInventoryAsset.objects.filter(pk=asset.pk).update(
                status=ConfigInventoryAsset.Status.ASSIGNED,
                current_allocations=F("current_allocations") + count,
                updated_at=timezone.now(),
            )
            asset.status = ConfigInventoryAsset.Status.ASSIGNED
            asset.current_allocations = int(asset.current_allocations or 0) + count

        allocations = []
        for asset in assets:
            allocations.append(
                ConfigAllocation.objects.create(
                    asset=asset,
                    cup=cup,
                    order=order,
                    allocation_mode=mode,
                    status=ConfigAllocation.Status.ACTIVE,
                    metadata={
                        "source": "inventory_pool",
                        "pool_id": pool.pk,
                        "asset_hash_suffix": (asset.normalized_hash or "")[-8:],
                    },
                )
            )

    return InventoryAllocationResult(
        pool=pool,
        allocation_mode=mode,
        requested_quantity=quantity,
        assets=assets,
        allocations=allocations,
    )


def get_pool_stock_summary(pool):
    pool = _pool_instance(pool)
    assets = list(ConfigInventoryAsset.objects.filter(pool=pool))
    status_counts = {status: 0 for status in ConfigInventoryAsset.Status.values}
    for asset in assets:
        status_counts[asset.status] = status_counts.get(asset.status, 0) + 1

    usable_assets = [
        asset
        for asset in assets
        if asset.status in {ConfigInventoryAsset.Status.AVAILABLE, ConfigInventoryAsset.Status.ASSIGNED}
        and (not asset.expires_at or asset.expires_at > timezone.now())
    ]
    if pool.allocation_mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        available_capacity = sum(1 for asset in usable_assets if asset.status == ConfigInventoryAsset.Status.AVAILABLE)
    elif pool.allocation_mode == ConfigInventoryPool.AllocationMode.SHARED_LIMITED:
        available_capacity = 0
        for asset in usable_assets:
            limit = _asset_allocation_limit(asset, pool)
            if limit is None:
                continue
            available_capacity += max(int(limit or 0) - int(asset.current_allocations or 0), 0)
    else:
        available_capacity = None if usable_assets else 0

    return {
        "pool_id": pool.pk,
        "title": pool.title,
        "is_active": pool.is_active,
        "allocation_mode": pool.allocation_mode,
        "asset_count": len(assets),
        "status_counts": status_counts,
        "available_capacity": available_capacity,
        "usable_asset_count": len(usable_assets),
    }
