from __future__ import annotations

import base64
import binascii
import html
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import ConfigAllocation, ConfigInventoryAsset, ConfigInventoryPool, ConfigLink
from .subscription_cups import parse_config_link


ALLOCATION_MODE_DISPLAY_LABELS = {
    ConfigInventoryPool.AllocationMode.EXCLUSIVE: "اختصاصی",
    ConfigInventoryPool.AllocationMode.SHARED_LIMITED: "اشتراکی محدود",
    ConfigInventoryPool.AllocationMode.SHARED_UNLIMITED: "اشتراکی نامحدود",
}

ASSET_STATUS_DISPLAY_LABELS = {
    ConfigInventoryAsset.Status.AVAILABLE: "آماده",
    ConfigInventoryAsset.Status.RESERVED: "رزرو شده",
    ConfigInventoryAsset.Status.ASSIGNED: "تخصیص‌داده‌شده",
    ConfigInventoryAsset.Status.DISABLED: "غیرفعال",
    ConfigInventoryAsset.Status.EXPIRED: "منقضی‌شده",
    ConfigInventoryAsset.Status.BURNED: "سوخته",
}

ALLOCATION_STATUS_DISPLAY_LABELS = {
    ConfigAllocation.Status.ACTIVE: "فعال",
    ConfigAllocation.Status.RELEASED: "آزادشده",
    ConfigAllocation.Status.CANCELLED: "لغوشده",
}

DIRECT_LINKS_IMPORT_MODE = "direct_links"
SUBSCRIPTION_URL_IMPORT_MODE = "subscription_url"
DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT = 10
MAX_SUBSCRIPTION_IMPORT_BYTES = 2 * 1024 * 1024
COMMENT_LINE_PREFIXES = ("#", ";", "//")
SUBSCRIPTION_CONFIG_PROTOCOL_RE = re.compile(
    r"(?i)(?:vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\"'<>]+"
)
SUBSCRIPTION_CLIENT_HEADERS = {
    "Accept": "text/plain,*/*",
    "User-Agent": "v2rayNG/1.8.38",
}
SUBSCRIPTION_STRONG_CLIENT_HEADERS = {
    "Accept": "text/plain,application/octet-stream,*/*;q=0.8",
    "User-Agent": "Hiddify/2.0 v2rayNG/1.8.38 sing-box/1.10",
}


def inventory_allocation_mode_label(value):
    return ALLOCATION_MODE_DISPLAY_LABELS.get(value, value or "-")


def inventory_asset_status_label(value):
    return ASSET_STATUS_DISPLAY_LABELS.get(value, value or "-")


def config_allocation_status_label(value):
    return ALLOCATION_STATUS_DISPLAY_LABELS.get(value, value or "-")


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


class SubscriptionImportFetchError(ConfigInventoryError):
    pass


@dataclass
class InventoryImportResult:
    pool: ConfigInventoryPool
    created_count: int = 0
    skipped_count: int = 0
    duplicate_count: int = 0
    total_count: int = 0
    import_mode: str = DIRECT_LINKS_IMPORT_MODE
    fetched: bool | None = None
    decoded_as_base64: bool = False
    response_type: str = ""
    configs_found: int = 0
    source_url_masked: str = ""
    fetch_error: str = ""
    assets: list[ConfigInventoryAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubscriptionContentParseResult:
    import_text: str
    decoded_as_base64: bool = False
    response_type: str = "unknown"
    configs_found: int = 0


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


def mask_subscription_source_url(url):
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    path_segments = []
    mask_next_segment = False
    for segment in (parts.path or "").split("/"):
        lowered = segment.lower()
        should_mask = bool(segment) and (
            mask_next_segment
            or len(segment) > 12
            or bool(re.search(r"[A-Za-z0-9_-]{8,}", segment))
        )
        if should_mask:
            path_segments.append("***" if len(segment) <= 8 else f"{segment[:4]}...{segment[-4:]}")
        else:
            path_segments.append(segment)
        mask_next_segment = lowered in {"sub", "subs", "subscription", "subscribe"}
    safe_query = ""
    if parts.query:
        safe_query = "&".join(f"{key}=***" if value else key for key, value in parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, parts.netloc, "/".join(path_segments), safe_query, ""))


def _is_comment_line(raw_link):
    stripped = str(raw_link or "").lstrip()
    return bool(stripped) and stripped.startswith(COMMENT_LINE_PREFIXES)


def _importable_subscription_lines(text):
    lines = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw_link = str(line or "").strip()
        if not raw_link or _is_comment_line(raw_link):
            continue
        lines.append(raw_link)
    return lines


def _has_supported_config_line(lines):
    return any(parse_config_link(line).protocol != ConfigLink.Protocol.UNKNOWN for line in lines)


def _looks_like_html(text):
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("<html", "<!doctype", "<body", "<head", "</a>", "<div", "<script"))


def _dedupe_preserving_order(values):
    seen = set()
    deduped = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def default_asset_max_allocations_for_pool(pool):
    pool = _pool_instance(pool)
    if pool.allocation_mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        return 1
    if pool.allocation_mode == ConfigInventoryPool.AllocationMode.SHARED_LIMITED:
        return pool.max_allocations_per_asset
    return None


def _extract_protocol_links_from_text(text):
    text = str(text or "")
    variants = [text]
    unescaped = html.unescape(text)
    if unescaped != text:
        variants.append(unescaped)
    decoded = unquote(unescaped)
    if decoded != unescaped:
        variants.append(decoded)

    links = []
    for variant in variants:
        for match in SUBSCRIPTION_CONFIG_PROTOCOL_RE.finditer(variant):
            raw_link = html.unescape(match.group(0)).strip()
            if raw_link:
                links.append(raw_link)
    return _dedupe_preserving_order(links)


def _decode_subscription_base64(text):
    compact = "".join(str(text or "").split())
    if not compact:
        return "", ""
    padded = f"{compact}{'=' * (-len(compact) % 4)}"
    try:
        payload = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return "", ""
    response_type = "base64_urlsafe" if "-" in compact or "_" in compact else "base64"
    return payload.decode("utf-8-sig", "replace"), response_type


def _subscription_content_to_import_text(content):
    raw_lines = _importable_subscription_lines(content)
    raw_links = _extract_protocol_links_from_text(content)
    if _has_supported_config_line(raw_lines):
        return SubscriptionContentParseResult(
            "\n".join(raw_lines),
            decoded_as_base64=False,
            response_type="raw",
            configs_found=len(raw_links),
        )

    decoded, decoded_type = _decode_subscription_base64(content)
    decoded_lines = _importable_subscription_lines(decoded)
    decoded_links = _extract_protocol_links_from_text(decoded)
    if _has_supported_config_line(decoded_lines):
        return SubscriptionContentParseResult(
            "\n".join(decoded_lines),
            decoded_as_base64=True,
            response_type=decoded_type,
            configs_found=len(decoded_links),
        )
    if decoded_links:
        return SubscriptionContentParseResult(
            "\n".join(decoded_links),
            decoded_as_base64=True,
            response_type=f"{decoded_type}_mixed",
            configs_found=len(decoded_links),
        )
    if raw_links:
        return SubscriptionContentParseResult(
            "\n".join(raw_links),
            decoded_as_base64=False,
            response_type="html_embedded" if _looks_like_html(content) else "mixed",
            configs_found=len(raw_links),
        )

    response_type = "html_no_configs" if _looks_like_html(content) else (decoded_type or "unknown")
    return SubscriptionContentParseResult(
        "\n".join(raw_lines or decoded_lines),
        decoded_as_base64=bool(decoded_lines and not raw_lines),
        response_type=response_type,
        configs_found=0,
    )


def _validated_subscription_url(subscription_url):
    subscription_url = str(subscription_url or "").strip()
    try:
        parts = urlsplit(subscription_url)
    except ValueError as exc:
        raise SubscriptionImportFetchError("Subscription URL is not valid.", code="invalid_subscription_url") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SubscriptionImportFetchError("Subscription URL must use http or https.", code="invalid_subscription_url")
    return subscription_url


def _fetch_subscription_once(subscription_url, *, timeout, headers):
    try:
        timeout = max(1, min(int(timeout or DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT), 60))
    except (TypeError, ValueError):
        timeout = DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT
    request = Request(
        subscription_url,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_SUBSCRIPTION_IMPORT_BYTES + 1)
    except HTTPError as exc:
        raise SubscriptionImportFetchError(f"Supplier subscription fetch failed with HTTP {exc.code}.", code="fetch_http_error") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SubscriptionImportFetchError("Supplier subscription fetch failed.", code="fetch_failed") from exc
    if len(payload) > MAX_SUBSCRIPTION_IMPORT_BYTES:
        raise SubscriptionImportFetchError("Supplier subscription response is too large.", code="response_too_large")
    return payload.decode("utf-8-sig", "replace")


def _fetch_subscription_content(subscription_url, *, timeout=DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT):
    subscription_url = _validated_subscription_url(subscription_url)
    content = _fetch_subscription_once(subscription_url, timeout=timeout, headers=SUBSCRIPTION_CLIENT_HEADERS)
    parsed = _subscription_content_to_import_text(content)
    if parsed.configs_found == 0 and _looks_like_html(content):
        retry_content = _fetch_subscription_once(subscription_url, timeout=timeout, headers=SUBSCRIPTION_STRONG_CLIENT_HEADERS)
        if retry_content:
            return retry_content
    return content


def _not_expired_q():
    return Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())


def import_config_assets(pool, raw_text, source_batch=None, *, import_mode=DIRECT_LINKS_IMPORT_MODE, metadata=None):
    pool = _pool_instance(pool)
    result = InventoryImportResult(pool=pool, import_mode=import_mode or DIRECT_LINKS_IMPORT_MODE)
    source_batch = str(source_batch or "").strip()
    metadata = dict(metadata or {})

    for line_number, line in enumerate(str(raw_text or "").splitlines(), start=1):
        raw_link = str(line or "").strip()
        if not raw_link or _is_comment_line(raw_link):
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
            max_allocations=default_asset_max_allocations_for_pool(pool),
            source_batch=source_batch,
            metadata={
                "source": import_mode or DIRECT_LINKS_IMPORT_MODE,
                "source_batch": source_batch,
                "import_line": line_number,
                **metadata,
            },
        )
        result.assets.append(asset)
        result.created_count += 1
    return result


def import_config_assets_from_subscription_url(pool, subscription_url, *, source_batch=None, timeout=DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT):
    pool = _pool_instance(pool)
    source_url_masked = mask_subscription_source_url(subscription_url)
    try:
        content = _fetch_subscription_content(subscription_url, timeout=timeout)
    except ConfigInventoryError as exc:
        return InventoryImportResult(
            pool=pool,
            import_mode=SUBSCRIPTION_URL_IMPORT_MODE,
            fetched=False,
            source_url_masked=source_url_masked,
            fetch_error=exc.safe_message,
            errors=[exc.safe_message],
        )
    parsed_content = _subscription_content_to_import_text(content)
    result = import_config_assets(
        pool,
        parsed_content.import_text,
        source_batch=source_batch,
        import_mode=SUBSCRIPTION_URL_IMPORT_MODE,
        metadata={
            "source_url_masked": source_url_masked,
            "decoded_as_base64": parsed_content.decoded_as_base64,
            "response_type": parsed_content.response_type,
            "configs_found": parsed_content.configs_found,
        },
    )
    result.fetched = True
    result.decoded_as_base64 = parsed_content.decoded_as_base64
    result.response_type = parsed_content.response_type
    result.configs_found = parsed_content.configs_found
    result.source_url_masked = source_url_masked
    if parsed_content.configs_found == 0:
        if parsed_content.response_type == "html_no_configs":
            result.warnings.insert(0, "Supplier subscription returned HTML but did not contain supported config links.")
        else:
            result.warnings.append("Supplier subscription did not contain supported config links.")
    return result


def _asset_allocation_limit(asset, pool):
    if pool is None:
        pool = getattr(asset, "pool", None)
    if pool is None:
        return getattr(asset, "max_allocations", None)
    limit = asset.max_allocations if asset.max_allocations is not None else pool.max_allocations_per_asset
    if limit is None and pool.allocation_mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        return 1
    return limit


def asset_allocation_limit(asset, pool=None):
    pool = pool or getattr(asset, "pool", None)
    return _asset_allocation_limit(asset, pool)


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
            while len(selected) < quantity:
                selected.append(asset)
            return selected
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


def _coerce_asset_ids(asset_ids):
    coerced_ids = []
    for value in asset_ids or []:
        try:
            asset_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigInventoryError("Selected inventory asset id is not valid.", code="invalid_asset_id") from exc
        if asset_id < 1:
            raise ConfigInventoryError("Selected inventory asset id is not valid.", code="invalid_asset_id")
        coerced_ids.append(asset_id)
    return coerced_ids


def _validate_selected_asset(asset, *, pool, mode, requested_count):
    if asset.pool_id != pool.pk:
        raise ConfigInventoryError("Selected inventory asset does not belong to the selected pool.", code="asset_pool_mismatch")
    if asset.status in {
        ConfigInventoryAsset.Status.DISABLED,
        ConfigInventoryAsset.Status.EXPIRED,
        ConfigInventoryAsset.Status.BURNED,
        ConfigInventoryAsset.Status.RESERVED,
    }:
        raise ConfigInventoryError("Selected inventory asset is not usable.", code="asset_not_usable")
    if asset.expires_at and asset.expires_at <= timezone.now():
        raise ConfigInventoryError("Selected inventory asset is expired.", code="asset_expired")

    if mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        if requested_count != 1 or asset.status != ConfigInventoryAsset.Status.AVAILABLE or int(asset.current_allocations or 0) > 0:
            raise ConfigInventoryError("Selected exclusive inventory asset is already assigned.", code="exclusive_asset_assigned")
        return

    if asset.status not in {ConfigInventoryAsset.Status.AVAILABLE, ConfigInventoryAsset.Status.ASSIGNED}:
        raise ConfigInventoryError("Selected inventory asset is not available for shared allocation.", code="asset_not_available")

    if mode == ConfigInventoryPool.AllocationMode.SHARED_LIMITED:
        limit = _asset_allocation_limit(asset, pool)
        if limit is None:
            return
        remaining = max(int(limit or 0) - int(asset.current_allocations or 0), 0)
        if remaining < requested_count:
            raise ConfigInventoryError("Selected shared inventory asset has no remaining allocation capacity.", code="asset_allocation_limit_reached")


def allocate_selected_assets_from_pool(pool, asset_ids, cup=None, order=None, allocation_mode=None):
    pool = _pool_instance(pool)
    selected_ids = _coerce_asset_ids(asset_ids)
    if not selected_ids:
        raise ConfigInventoryError("Select at least one inventory asset.", code="asset_required")
    mode = allocation_mode or pool.allocation_mode
    if mode not in ConfigInventoryPool.AllocationMode.values:
        raise ConfigInventoryError("Allocation mode is not valid.", code="invalid_allocation_mode")
    if not pool.is_active:
        raise ConfigInventoryError(f"Pool '{pool.title}' is not active.", code="pool_inactive")

    selected_counts = Counter(selected_ids)
    with transaction.atomic():
        pool = ConfigInventoryPool.objects.select_for_update().get(pk=pool.pk)
        assets_by_id = {
            asset.pk: asset
            for asset in ConfigInventoryAsset.objects.select_for_update().filter(pk__in=selected_counts.keys()).select_related("pool")
        }
        missing_ids = [asset_id for asset_id in selected_counts if asset_id not in assets_by_id]
        if missing_ids:
            raise ConfigInventoryError("Selected inventory asset was not found.", code="asset_not_found")

        for asset_id, requested_count in selected_counts.items():
            _validate_selected_asset(assets_by_id[asset_id], pool=pool, mode=mode, requested_count=requested_count)

        selected_assets = [assets_by_id[asset_id] for asset_id in selected_ids]
        for asset, count in _selected_asset_counts(selected_assets):
            ConfigInventoryAsset.objects.filter(pk=asset.pk).update(
                status=ConfigInventoryAsset.Status.ASSIGNED,
                current_allocations=F("current_allocations") + count,
                updated_at=timezone.now(),
            )
            asset.status = ConfigInventoryAsset.Status.ASSIGNED
            asset.current_allocations = int(asset.current_allocations or 0) + count

        allocations = []
        for asset in selected_assets:
            allocations.append(
                ConfigAllocation.objects.create(
                    asset=asset,
                    cup=cup,
                    order=order,
                    allocation_mode=mode,
                    status=ConfigAllocation.Status.ACTIVE,
                    metadata={
                        "source": "inventory_pool_manual_selection",
                        "pool_id": pool.pk,
                        "asset_hash_suffix": (asset.normalized_hash or "")[-8:],
                    },
                )
            )

    return InventoryAllocationResult(
        pool=pool,
        allocation_mode=mode,
        requested_quantity=len(selected_assets),
        assets=selected_assets,
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
        has_unlimited_asset = False
        for asset in usable_assets:
            limit = _asset_allocation_limit(asset, pool)
            if limit is None:
                has_unlimited_asset = True
                continue
            available_capacity += max(int(limit or 0) - int(asset.current_allocations or 0), 0)
        if has_unlimited_asset:
            available_capacity = None
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
