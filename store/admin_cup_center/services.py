import base64
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone

from store.config_lookup import mask_identifier
from store.config_inventory_services import ConfigInventoryError, allocate_assets_from_pool, get_pool_stock_summary
from store.models import ConfigInventoryPool, ConfigLink, CupItem, Inbound, Panel, SubscriptionCup
from store.panels.errors import (
    CupBuildValidationError,
    CupRemoteCreateFailedError,
    PanelCapabilityMissingError,
    PanelIntegrationError,
    safe_error_dict,
    sanitize_error_value,
)
from store.panels.factory import get_safe_panel_adapter
from store.panels.xui.adapter import XUIProvisioningRequest
from store.subscription_cups import (
    build_subscription_cup_client_path,
    build_subscription_cup_dashboard_path,
    build_subscription_cup_path,
    build_subscription_cup_raw_path,
    build_subscription_cup_url,
    create_config_link_from_raw,
    cup_protocols,
    mask_subscription_url,
    parse_config_link,
    rebuild_subscription_cup_for_vpn_client,
    rebuild_subscription_cups_for_order,
    render_subscription_cup_raw,
)
from store.xui_api import sanitize_xui_operational_text


class CupCenterError(Exception):
    def __init__(self, message="", *, structured_error=None):
        if isinstance(structured_error, PanelIntegrationError):
            self.structured_error = structured_error.to_safe_dict()
            message = message or structured_error.message
        elif isinstance(structured_error, dict):
            self.structured_error = structured_error
            message = message or structured_error.get("message") or ""
        else:
            self.structured_error = None
        super().__init__(message)


class CupCenterRemoteCreateError(CupCenterError):
    pass


class CupCenterRemoteSaveError(CupCenterError):
    pass


class CupCenterValidationError(CupCenterError):
    pass


@dataclass
class AddLinksResult:
    requested_count: int = 0
    found_count: int = 0
    created_count: int = 0
    added_count: int = 0
    skipped_empty_count: int = 0
    skipped_invalid_count: int = 0
    duplicate_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class PanelConfigResult:
    config_link: ConfigLink
    cup_item: CupItem
    masked_link: str
    panel_name: str
    inbound_label: str
    email_masked: str = ""


@dataclass
class QuickPanelBuildResult:
    panel_id: int | None
    panel_name: str
    family: str
    capability_profile: str
    detected_version: str
    health_status: str
    selected_inbounds: list[dict]
    group_index: int
    success: bool = False
    create_success: bool = False
    create_mode: str = ""
    link_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    structured_errors: list[dict] = field(default_factory=list)


@dataclass
class QuickBuildResult:
    cup: SubscriptionCup
    config_links: list[ConfigLink]
    cup_items: list[CupItem]
    selected_inbounds: list[dict]
    masked_subscription_url: str
    protocols: list[str]
    selected_inventory_pools: list[dict] = field(default_factory=list)
    email_masked: str = ""
    status: str = "success"
    selected_panels_count: int = 0
    selected_inbounds_count: int = 0
    selected_inventory_pools_count: int = 0
    created_remote_client_groups_count: int = 0
    inventory_allocation_count: int = 0
    config_link_count: int = 0
    panel_results: list[dict] = field(default_factory=list)
    inventory_results: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    structured_errors: list[dict] = field(default_factory=list)

    @property
    def item_count(self):
        return len(self.cup_items)


SUPPORTED_INBOUND_PROTOCOLS = {
    Inbound.Protocol.VLESS,
    Inbound.Protocol.VMESS,
    Inbound.Protocol.TROJAN,
}


def cup_center_url(name, *args):
    from django.urls import reverse

    return reverse(name, args=args)


def _next_position(cup):
    current = cup.items.aggregate(max_position=Max("position")).get("max_position") or 0
    return int(current) + 1


def _active_item_filter():
    return Q(items__is_active=True, items__config_link__is_active=True)


def cup_queryset():
    return (
        SubscriptionCup.objects.select_related("customer", "order", "plan", "vpn_client")
        .annotate(active_item_count=Count("items", filter=_active_item_filter(), distinct=True))
        .annotate(inactive_item_count=Count("items", filter=Q(items__is_active=False) | Q(items__config_link__is_active=False), distinct=True))
        .order_by("-created_at", "-pk")
    )


def cup_item_queryset(cup):
    return (
        CupItem.objects.filter(cup=cup)
        .select_related("config_link", "config_link__source_panel", "config_link__source_inbound")
        .order_by("position", "pk")
    )


def cup_item_rows(cup):
    rows = []
    for item in cup_item_queryset(cup):
        config_link = item.config_link
        rows.append(
            {
                "item": item,
                "config_link": config_link,
                "masked_link": mask_link_for_display(config_link),
                "protocol": config_link.protocol or ConfigLink.Protocol.UNKNOWN,
                "host": config_link.host or "-",
                "port": config_link.port or "-",
                "remark": config_link.remark or "-",
                "source_type": config_link.source_type or ConfigLink.SourceType.UNKNOWN,
                "source_panel": config_link.source_panel or None,
                "source_inbound": config_link.source_inbound or None,
                "is_effective_active": bool(item.is_active and config_link.is_active),
            }
        )
    return rows


def supported_inbound_queryset():
    return (
        Inbound.objects.select_related("panel")
        .filter(
            is_active=True,
            available_for_new_orders=True,
            panel__is_active=True,
            protocol__in=SUPPORTED_INBOUND_PROTOCOLS,
        )
        .order_by("panel__name", "inbound_id", "pk")
    )


def _panel_health_status(panel):
    try:
        return getattr(panel.health_status, "status", "") or "unknown"
    except Exception:
        return "unknown"


def _inbound_summary(inbound):
    panel = getattr(inbound, "panel", None)
    return {
        "id": inbound.pk,
        "label": str(inbound),
        "panel_id": getattr(panel, "pk", None) or inbound.panel_id,
        "panel_name": getattr(panel, "name", "") or "-",
        "remote_inbound_id": inbound.inbound_id,
        "protocol": inbound.protocol,
        "host": inbound.server_ip or "-",
        "port": inbound.port or "-",
        "remark": inbound.remark or f"Inbound {inbound.inbound_id}",
    }


def quick_builder_panel_groups():
    inbound_queryset = (
        Inbound.objects.filter(
            is_active=True,
            available_for_new_orders=True,
            protocol__in=SUPPORTED_INBOUND_PROTOCOLS,
        )
        .order_by("inbound_id", "pk")
    )
    panels = (
        Panel.objects.filter(is_active=True)
        .select_related("health_status")
        .prefetch_related(Prefetch("inbounds", queryset=inbound_queryset, to_attr="quick_builder_inbounds"))
        .order_by("name", "pk")
    )
    groups = []
    for panel in panels:
        inbounds = list(getattr(panel, "quick_builder_inbounds", []) or [])
        if not inbounds:
            continue
        groups.append(
            {
                "panel": panel,
                "panel_id": panel.pk,
                "panel_name": panel.name,
                "family": panel.family or "-",
                "family_display": panel.get_family_display() if panel.family else "-",
                "capability_profile": panel.capability_profile or "-",
                "capability_profile_display": panel.get_capability_profile_display() if panel.capability_profile else "-",
                "detected_version": panel.detected_xui_version or "-",
                "health_status": _panel_health_status(panel),
                "inbounds": [_inbound_summary(inbound) | {"inbound": inbound} for inbound in inbounds],
            }
        )
    return groups


def _inventory_pool_summary(pool):
    summary = get_pool_stock_summary(pool)
    capacity = summary["available_capacity"]
    return {
        "id": pool.pk,
        "title": pool.title,
        "allocation_mode": summary["allocation_mode"],
        "allocation_mode_display": pool.get_allocation_mode_display(),
        "available_stock": "Unlimited" if capacity is None else capacity,
        "asset_count": summary["asset_count"],
        "usable_asset_count": summary["usable_asset_count"],
        "connected_plan": str(pool.connected_plan) if pool.connected_plan_id else "",
        "priority": pool.priority,
    }


def quick_builder_inventory_pool_rows():
    pools = ConfigInventoryPool.objects.filter(is_active=True).select_related("connected_plan").order_by("priority", "title", "pk")
    return [_inventory_pool_summary(pool) for pool in pools]


def inbound_selection_rows():
    rows = []
    for group in quick_builder_panel_groups():
        for row in group["inbounds"]:
            rows.append(
                {
                    "inbound": row["inbound"],
                    "panel": group["panel"],
                    "panel_id": group["panel_id"],
                    "protocol": row["protocol"],
                    "host": row["host"],
                    "port": row["port"],
                    "remark": row["remark"],
                }
            )
    return rows


def mask_link_for_display(link_or_config):
    config_link = link_or_config if isinstance(link_or_config, ConfigLink) else None
    raw_link = getattr(config_link, "raw_link", link_or_config) or ""
    parsed = parse_config_link(raw_link)
    protocol = getattr(config_link, "protocol", "") or parsed.protocol or ConfigLink.Protocol.UNKNOWN
    suffix = (getattr(config_link, "normalized_hash", "") or parsed.normalized_hash or "")[-8:] or "-"
    remark = getattr(config_link, "remark", "") or parsed.remark
    remark_part = f" · {remark[:48]}" if remark else ""
    return f"{protocol}://<hidden> · hash ending {suffix}{remark_part}"


def subscription_url_summary(cup, *, request=None):
    url = build_subscription_cup_url(cup, request=request)
    path = build_subscription_cup_path(cup)
    dashboard_path = build_subscription_cup_dashboard_path(cup)
    client_path = build_subscription_cup_client_path(cup)
    raw_path = build_subscription_cup_raw_path(cup)
    if request:
        dashboard_url = request.build_absolute_uri(dashboard_path)
        client_url = request.build_absolute_uri(client_path)
        raw_url = request.build_absolute_uri(raw_path)
    else:
        base_url = build_subscription_cup_url(cup)
        base_prefix = base_url[: -len(path)] if path and base_url.endswith(path) else ""
        dashboard_url = f"{base_prefix}{dashboard_path}" if base_prefix else dashboard_path
        client_url = f"{base_prefix}{client_path}" if base_prefix else client_path
        raw_url = f"{base_prefix}{raw_path}" if base_prefix else raw_path
    return {
        "url": url,
        "dashboard_url": dashboard_url,
        "client_url": client_url,
        "raw_url": raw_url,
        "masked_url": mask_subscription_url(url, cup.token),
        "masked_dashboard_url": mask_subscription_url(dashboard_url, cup.token),
        "masked_client_url": mask_subscription_url(client_url, cup.token),
        "masked_raw_url": mask_subscription_url(raw_url, cup.token),
        "path": path,
        "dashboard_path": dashboard_path,
        "client_path": client_path,
        "raw_path": raw_path,
    }


def cup_list_items(cups, *, request=None):
    items = []
    for cup in cups:
        urls = subscription_url_summary(cup, request=request)
        items.append(
            {
                "cup": cup,
                "active_item_count": getattr(cup, "active_item_count", None) if getattr(cup, "active_item_count", None) is not None else cup.items.filter(is_active=True, config_link__is_active=True).count(),
                "inactive_item_count": getattr(cup, "inactive_item_count", None) if getattr(cup, "inactive_item_count", None) is not None else cup.items.filter(Q(is_active=False) | Q(config_link__is_active=False)).count(),
                "subscription": urls,
                "detail_url": cup_center_url("admin_store_cup_center_detail", cup.pk),
                "preview_url": cup_center_url("admin_store_cup_center_preview", cup.pk),
                "add_existing_url": cup_center_url("admin_store_cup_center_add_existing", cup.pk),
                "add_manual_url": cup_center_url("admin_store_cup_center_add_manual", cup.pk),
                "create_from_inbound_url": cup_center_url("admin_store_cup_center_create_from_inbound", cup.pk),
                "rebuild_url": cup_center_url("admin_store_cup_center_rebuild", cup.pk),
                "status_tone": cup_status_tone(cup),
            }
        )
    return items


def cup_status_tone(cup):
    if cup.status == SubscriptionCup.Status.ACTIVE and not cup.is_expired:
        return "emerald"
    if cup.status == SubscriptionCup.Status.DISABLED:
        return "rose"
    return "amber"


def create_manual_cup(*, title="", customer=None, order=None, plan=None, status=SubscriptionCup.Status.ACTIVE, expires_at=None, metadata=None):
    if order:
        customer = customer or getattr(order, "customer", None)
        plan = plan or getattr(order, "plan", None)
    cup = SubscriptionCup.objects.create(
        title=title or "",
        customer=customer,
        order=order,
        plan=plan,
        status=status or SubscriptionCup.Status.ACTIVE,
        expires_at=expires_at,
        traffic_limit_bytes=int(getattr(plan, "traffic_limit_bytes", 0) or 0),
        device_limit=getattr(plan, "device_limit", None),
        metadata=metadata or {},
    )
    return cup


def add_existing_links_to_cup(cup, config_link_ids):
    ids = [value for value in config_link_ids if str(value).strip()]
    result = AddLinksResult(requested_count=len(ids))
    if not ids:
        result.warnings.append("هیچ لینکی انتخاب نشده بود.")
        return result

    config_links = list(
        ConfigLink.objects.filter(pk__in=ids)
        .select_related("source_panel", "source_inbound", "vpn_client")
        .order_by("pk")
    )
    result.found_count = len(config_links)
    existing_hashes = set(
        cup.items.select_related("config_link")
        .exclude(config_link__normalized_hash="")
        .values_list("config_link__normalized_hash", flat=True)
    )
    position = _next_position(cup)
    with transaction.atomic():
        for config_link in config_links:
            if config_link.normalized_hash and config_link.normalized_hash in existing_hashes:
                result.duplicate_count += 1
            CupItem.objects.create(
                cup=cup,
                config_link=config_link,
                position=position,
                is_active=True,
                added_reason="admin_existing",
                metadata={"source": "cup_center_existing_link"},
            )
            existing_hashes.add(config_link.normalized_hash)
            position += 1
            result.added_count += 1
    if result.duplicate_count:
        result.warnings.append(f"{result.duplicate_count} لینک مشابه از قبل داخل این Cup وجود داشت؛ برای MVP دوباره اضافه شد.")
    return result


def add_manual_links_to_cup(cup, raw_text):
    result = AddLinksResult()
    lines = str(raw_text or "").splitlines()
    result.requested_count = len(lines)
    existing_hashes = set(
        cup.items.select_related("config_link")
        .exclude(config_link__normalized_hash="")
        .values_list("config_link__normalized_hash", flat=True)
    )
    position = _next_position(cup)
    with transaction.atomic():
        for line_number, raw_link in enumerate(lines, start=1):
            raw_link = str(raw_link or "").strip()
            if not raw_link:
                result.skipped_empty_count += 1
                continue
            parsed = parse_config_link(raw_link)
            if parsed.protocol == ConfigLink.Protocol.UNKNOWN:
                result.skipped_invalid_count += 1
                result.warnings.append(f"خط {line_number}: protocol پشتیبانی‌شده نبود و اضافه نشد.")
                continue
            if parsed.normalized_hash in existing_hashes:
                result.duplicate_count += 1
            config_link = create_config_link_from_raw(
                raw_link,
                source_type=ConfigLink.SourceType.MANUAL,
                metadata={"source": "cup_center_manual"},
            )
            CupItem.objects.create(
                cup=cup,
                config_link=config_link,
                position=position,
                is_active=True,
                added_reason="admin_manual",
                metadata={"source": "cup_center_manual"},
            )
            existing_hashes.add(parsed.normalized_hash)
            position += 1
            result.created_count += 1
            result.added_count += 1
    if result.duplicate_count:
        result.warnings.append(f"{result.duplicate_count} لینک مشابه از قبل داخل این Cup وجود داشت؛ برای MVP دوباره اضافه شد.")
    return result


def _safe_panel_error(exc, panel=None):
    if isinstance(exc, PanelIntegrationError):
        return exc.message
    return sanitize_error_value(sanitize_xui_operational_text(exc, panel=panel, max_length=240) or "panel_operation_failed")


def _cup_structured_error(
    exc=None,
    *,
    error_code="cup_build_validation_failed",
    layer="cup_builder",
    action="build_subscription_cup",
    message="خطا در ساخت کانفیگ",
    technical_detail="",
    remediation="انتخاب پنل و اینباند را بررسی کنید.",
    panel=None,
    inbound=None,
    safe_context=None,
):
    if isinstance(exc, PanelIntegrationError):
        return exc.to_safe_dict()
    return CupBuildValidationError(
        message,
        error_code=error_code,
        layer=layer,
        action=action,
        technical_detail=technical_detail or str(exc or ""),
        remediation=remediation,
        panel=panel,
        inbound=inbound,
        safe_context=safe_context or {},
    ).to_safe_dict()


def _raise_cup_error(error_class, message, *, structured_error):
    raise error_class(message, structured_error=structured_error)


def _redacted_panel_config_metadata(remote_result, *, panel, inbound):
    return {
        "source": "cup_center_panel_generated",
        "remote_created": True,
        "panel_id": getattr(panel, "pk", None),
        "inbound_pk": getattr(inbound, "pk", None),
        "inbound_id": getattr(inbound, "inbound_id", None),
        "node_id": getattr(inbound, "xui_node_id", "") or "",
        "email_masked": mask_identifier((remote_result or {}).get("email")),
        "sub_link_saved": bool((remote_result or {}).get("sub_link")),
        "remote_scope_saved": bool((remote_result or {}).get("remote_scope")),
        "remote_client_key_saved": bool((remote_result or {}).get("remote_client_key")),
        "created_at": timezone.now().isoformat(),
    }


def _report_create_error(report, *, panel=None, inbound=None, action="create_client", missing_capability="supports_create_client"):
    details = "; ".join(str(message).strip() for message in (getattr(report, "errors", ()) or getattr(report, "warnings", ())) if str(message).strip())
    message = "این پنل هنوز برای ساخت کانفیگ قابل استفاده نیست."
    if missing_capability == "supports_multi_inbound_create":
        message = "پنل انتخاب‌شده قابلیت supports_multi_inbound_create ندارد."
    elif getattr(report, "family", "") == Panel.Family.MARZBAN:
        message = "پنل Marzban هنوز برای ساخت مستقیم کانفیگ در این بخش پیاده‌سازی نشده است."
    error_code = "unsupported_panel_family" if not getattr(report, "supported", True) else "panel_capability_missing"
    structured = PanelCapabilityMissingError(
        message,
        error_code=error_code,
        action=action,
        technical_detail=details or f"Missing capability: {missing_capability}",
        remediation="از Panel Center گزینه Test connection / Sync capabilities را اجرا کنید، یا family پنل را روی X-UI تنظیم کنید.",
        panel=panel,
        panel_family=getattr(report, "family", "") or "",
        capability_profile=getattr(report, "capability_profile", "") or "",
        inbound=inbound,
        safe_context={
            "required_capability": missing_capability,
            "supported": getattr(report, "supported", None),
            "capability_profile": getattr(report, "capability_profile", "") or "",
        },
        warnings=list(getattr(report, "warnings", ()) or []),
    )
    return message, structured.to_safe_dict()


def create_panel_config_into_cup(cup, panel, inbound, options, *, adapter=None):
    if not isinstance(panel, Panel) or not isinstance(inbound, Inbound):
        structured = _cup_structured_error(
            error_code="inbound_validation_failed",
            layer="inbound_validation",
            action="create_client",
            message="Panel و Inbound معتبر نیستند.",
            remediation="یک پنل فعال و یک اینباند فعال همان پنل انتخاب کنید.",
            panel=panel if isinstance(panel, Panel) else None,
            inbound=inbound if isinstance(inbound, Inbound) else None,
        )
        _raise_cup_error(CupCenterRemoteCreateError, "Panel و Inbound معتبر نیستند.", structured_error=structured)
    if inbound.panel_id != panel.pk:
        structured = _cup_structured_error(
            error_code="inbound_panel_mismatch",
            layer="inbound_validation",
            action="create_client",
            message="Inbound انتخاب‌شده به Panel انتخاب‌شده وصل نیست.",
            remediation="اینباندی را انتخاب کنید که متعلق به همین پنل باشد.",
            panel=panel,
            inbound=inbound,
        )
        _raise_cup_error(CupCenterRemoteCreateError, "Inbound انتخاب‌شده به Panel انتخاب‌شده وصل نیست.", structured_error=structured)
    adapter = adapter or get_safe_panel_adapter(panel)
    try:
        report = adapter.get_capability_report()
    except Exception:
        report = None
    if report is not None and not getattr(report, "supports_create_client", False):
        message, structured = _report_create_error(report, panel=panel, inbound=inbound, action="create_client")
        _raise_cup_error(CupCenterRemoteCreateError, message, structured_error=structured)

    request = XUIProvisioningRequest(
        email_prefix=str(options.get("email_prefix") or "").strip(),
        total_gb=options.get("total_gb") or Decimal("1"),
        duration_days=int(options.get("duration_days") or 30),
        inbound=inbound,
        limit_ip=int(options.get("device_limit") or 2),
    )
    try:
        remote_result = adapter.create_enabled_client(request)
    except Exception as exc:
        structured = CupRemoteCreateFailedError(
            "ساخت کانفیگ روی پنل برای Cup ناموفق بود.",
            action="create_client",
            technical_detail=str(exc or ""),
            remediation="credentialها، قابلیت supports_create_client، ظرفیت اینباند و وضعیت پنل را بررسی کنید.",
            panel=panel,
            inbound=inbound,
            safe_context={"panel_error": exc.to_safe_dict() if isinstance(exc, PanelIntegrationError) else ""},
        ).to_safe_dict()
        raise CupCenterRemoteCreateError(_safe_panel_error(exc, panel=panel), structured_error=structured) from exc

    direct_link = str((remote_result or {}).get("direct_link") or "").strip()
    if not direct_link:
        structured = _cup_structured_error(
            error_code="cup_remote_create_missing_link",
            layer="subscription_render",
            action="create_client",
            message="پنل client را ساخت اما لینک مستقیم قابل ذخیره برنگرداند.",
            remediation="خروجی پنل و تنظیمات host/subscription را بررسی کنید؛ در صورت نیاز لینک را از پنل بازیابی و manual اضافه کنید.",
            panel=panel,
            inbound=inbound,
            safe_context={"remote_created": True, "direct_link_returned": False},
        )
        _raise_cup_error(CupCenterRemoteCreateError, "پنل client را ساخت اما لینک مستقیم قابل ذخیره برنگرداند.", structured_error=structured)

    try:
        with transaction.atomic():
            config_link = create_config_link_from_raw(
                direct_link,
                source_type=ConfigLink.SourceType.PANEL_GENERATED,
                source_panel=panel,
                source_inbound=inbound,
                metadata=_redacted_panel_config_metadata(remote_result, panel=panel, inbound=inbound),
            )
            cup_item = CupItem.objects.create(
                cup=cup,
                config_link=config_link,
                position=_next_position(cup),
                is_active=True,
                added_reason="admin_panel_generated",
                metadata={
                    "source": "cup_center_panel_generated",
                    "panel_id": panel.pk,
                    "inbound_pk": inbound.pk,
                },
            )
    except Exception as exc:
        message = "کانفیگ واقعی روی پنل ساخته شد، اما ذخیره local ناموفق بود. لینک را از پنل بازیابی و به صورت manual اضافه کنید."
        structured = _cup_structured_error(
            exc,
            error_code="cup_local_save_failed",
            layer="cup_builder",
            action="save_config_link",
            message=message,
            remediation="لینک ساخته‌شده را از پنل بازیابی و به صورت manual داخل Cup اضافه کنید.",
            panel=panel,
            inbound=inbound,
            safe_context={"remote_created": True},
        )
        raise CupCenterRemoteSaveError(message, structured_error=structured) from exc

    return PanelConfigResult(
        config_link=config_link,
        cup_item=cup_item,
        masked_link=mask_link_for_display(config_link),
        panel_name=str(panel),
        inbound_label=str(inbound),
        email_masked=mask_identifier((remote_result or {}).get("email")),
    )


def _group_quick_build_inbounds(inbounds):
    grouped = {}
    for inbound in inbounds:
        grouped.setdefault(inbound.panel_id, {"panel": inbound.panel, "inbounds": []})
        grouped[inbound.panel_id]["inbounds"].append(inbound)
    return list(grouped.values())


def _validate_quick_build_panel_group(panel, inbounds, report=None):
    if not isinstance(panel, Panel) or not getattr(panel, "is_active", False):
        structured = _cup_structured_error(
            error_code="panel_required",
            layer="cup_builder",
            action="validate_quick_builder",
            message="Panel فعال و معتبر انتخاب نشده است.",
            remediation="یک پنل فعال انتخاب کنید.",
            panel=panel if isinstance(panel, Panel) else None,
        )
        _raise_cup_error(CupCenterValidationError, "Panel فعال و معتبر انتخاب نشده است.", structured_error=structured)
    if not inbounds:
        message = "هیچ اینباندی انتخاب نشده است. حداقل یک inbound انتخاب کنید."
        structured = _cup_structured_error(
            error_code="inbound_required",
            layer="inbound_validation",
            action="validate_quick_builder",
            message=message,
            remediation="حداقل یک اینباند فعال و قابل فروش انتخاب کنید.",
            panel=panel,
        )
        _raise_cup_error(CupCenterValidationError, message, structured_error=structured)

    seen_remote_ids = set()
    for inbound in inbounds:
        if not isinstance(inbound, Inbound):
            message = "Inbound انتخاب‌شده معتبر نیست."
            structured = _cup_structured_error(
                error_code="inbound_validation_failed",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                remediation="فقط اینباندهای معتبر موجود در لیست Quick Builder را انتخاب کنید.",
                panel=panel,
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        if inbound.panel_id != panel.pk:
            message = "Inbound انتخاب‌شده به Panel خودش وصل نیست."
            structured = _cup_structured_error(
                error_code="inbound_panel_mismatch",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                remediation="اینباندهای هر گروه باید متعلق به همان پنل باشند.",
                panel=panel,
                inbound=inbound,
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        if not inbound.panel.is_active:
            message = "Panel یکی از inboundها فعال نیست."
            structured = _cup_structured_error(
                error_code="panel_inactive",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                remediation="پنل اینباند را فعال کنید یا اینباند دیگری انتخاب کنید.",
                panel=inbound.panel,
                inbound=inbound,
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        if not inbound.is_active or not inbound.available_for_new_orders:
            message = "Inbound انتخاب‌شده فعال یا قابل فروش نیست."
            structured = _cup_structured_error(
                error_code="inbound_not_sellable",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                remediation="گزینه‌های is_active و available_for_new_orders اینباند را بررسی کنید.",
                panel=panel,
                inbound=inbound,
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        if inbound.protocol not in SUPPORTED_INBOUND_PROTOCOLS:
            message = "Protocol یکی از inboundها پشتیبانی نمی‌شود."
            structured = _cup_structured_error(
                error_code="inbound_unsupported_protocol",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                technical_detail=f"protocol={inbound.protocol}",
                remediation="فقط VLESS، VMess یا Trojan را برای Quick Builder انتخاب کنید.",
                panel=panel,
                inbound=inbound,
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        remote_id = str(inbound.inbound_id)
        if remote_id in seen_remote_ids:
            message = "Inbound ID تکراری داخل یک Panel برای ساخت remote مجاز نیست."
            structured = _cup_structured_error(
                error_code="duplicate_remote_inbound_id",
                layer="inbound_validation",
                action="validate_quick_builder",
                message=message,
                remediation="برای هر پنل فقط یک رکورد با هر remote inbound ID انتخاب کنید.",
                panel=panel,
                inbound=inbound,
                safe_context={"remote_inbound_id": remote_id},
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        seen_remote_ids.add(remote_id)

    if report is not None:
        if not getattr(report, "supported", True) or not getattr(report, "supports_create_client", False):
            message, structured = _report_create_error(report, panel=panel, inbound=inbounds[0], action="create_client")
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
        if len(inbounds) > 1 and not getattr(report, "supports_multi_inbound_create", False):
            message = "این پنل فقط ساخت تک‌اینباندی را پشتیبانی می‌کند. برای این پنل فقط یک اینباند انتخاب کنید. انتخاب چند inbound برای این پنل مجاز نیست."
            structured = _cup_structured_error(
                error_code="panel_capability_missing",
                layer="capability_detection",
                action="create_multi_inbound_client",
                message=message,
                technical_detail="Missing capability: supports_multi_inbound_create",
                remediation="فقط یک اینباند از این پنل انتخاب کنید یا پنل modern_multi_node با قابلیت supports_multi_inbound_create انتخاب کنید.",
                panel=panel,
                inbound=inbounds[0],
                safe_context={"required_capability": "supports_multi_inbound_create"},
            )
            _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
    elif len(inbounds) > 1:
        message = "قابلیت ساخت چند inbound برای این پنل قابل تایید نیست."
        structured = _cup_structured_error(
            error_code="panel_capability_unknown",
            layer="capability_detection",
            action="create_multi_inbound_client",
            message=message,
            remediation="ابتدا Test connection / Sync capabilities را برای پنل اجرا کنید.",
            panel=panel,
            inbound=inbounds[0],
            safe_context={"required_capability": "supports_multi_inbound_create"},
        )
        _raise_cup_error(CupCenterValidationError, message, structured_error=structured)


def _validated_quick_build_groups(inbounds, adapter_factory):
    inbounds = list(inbounds or [])
    if not inbounds:
        message = "هیچ اینباندی انتخاب نشده است. حداقل یک inbound انتخاب کنید."
        structured = _cup_structured_error(
            error_code="inbound_required",
            layer="inbound_validation",
            action="validate_quick_builder",
            message=message,
            remediation="حداقل یک اینباند فعال و قابل فروش انتخاب کنید.",
        )
        _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
    groups = _group_quick_build_inbounds(inbounds)
    for group in groups:
        panel = group["panel"]
        adapter = adapter_factory(panel)
        try:
            report = adapter.get_capability_report()
        except Exception as exc:
            structured = safe_error_dict(
                exc,
                error_code="capability_detection_failed",
                layer="capability_detection",
                action="detect_capabilities",
                message="خواندن قابلیت‌های پنل برای Quick Builder ناموفق بود.",
                remediation="از Panel Center گزینه Test connection / Sync capabilities را اجرا کنید.",
                panel=panel,
            )
            raise CupCenterValidationError(_safe_panel_error(exc, panel=panel), structured_error=structured) from exc
        _validate_quick_build_panel_group(panel, group["inbounds"], report=report)
        group["adapter"] = adapter
        group["report"] = report
    return groups


def _direct_link_entries_from_remote(remote_result, inbounds):
    if len(inbounds) == 1:
        return [{"inbound": inbounds[0], "remote_result": remote_result, "direct_link": str((remote_result or {}).get("direct_link") or "").strip()}]
    bundle_results = list((remote_result or {}).get("bundle_inbound_results") or [])
    entries = []
    for index, inbound in enumerate(inbounds):
        item_result = bundle_results[index] if index < len(bundle_results) and isinstance(bundle_results[index], dict) else {}
        direct_link = str(item_result.get("direct_link") or "").strip()
        entries.append({"inbound": inbound, "remote_result": item_result or remote_result, "direct_link": direct_link})
    return entries


def _remote_create_for_quick_build(panel, inbounds, options, adapter):
    request = XUIProvisioningRequest(
        email_prefix=str(options.get("remark_prefix") or "").strip(),
        total_gb=options.get("volume_gb") or Decimal("10"),
        duration_days=int(options.get("duration_days") or 30),
        inbound=inbounds[0] if len(inbounds) == 1 else None,
        inbounds=inbounds if len(inbounds) > 1 else None,
        limit_ip=int(options.get("device_limit") or 2),
    )
    try:
        if len(inbounds) > 1:
            return adapter.create_enabled_multi_inbound_client(request)
        return adapter.create_enabled_client(request)
    except Exception as exc:
        structured = CupRemoteCreateFailedError(
            "ساخت کانفیگ روی پنل برای Quick Builder ناموفق بود.",
            action="create_multi_inbound_client" if len(inbounds) > 1 else "create_client",
            technical_detail=str(exc or ""),
            remediation="credentialها، قابلیت‌های پنل، ظرفیت اینباند و وضعیت API نوشتن را بررسی کنید.",
            panel=panel,
            inbound=inbounds[0] if inbounds else None,
            safe_context={
                "selected_inbound_pks": [getattr(inbound, "pk", None) for inbound in inbounds],
                "remote_inbound_ids": [getattr(inbound, "inbound_id", None) for inbound in inbounds],
                "panel_error": exc.to_safe_dict() if isinstance(exc, PanelIntegrationError) else "",
            },
        ).to_safe_dict()
        raise CupCenterRemoteCreateError(_safe_panel_error(exc, panel=panel), structured_error=structured) from exc


def _quick_panel_result(panel, inbounds, group_index):
    return QuickPanelBuildResult(
        panel_id=getattr(panel, "pk", None),
        panel_name=getattr(panel, "name", "") or "-",
        family=getattr(panel, "family", "") or "-",
        capability_profile=getattr(panel, "capability_profile", "") or "-",
        detected_version=getattr(panel, "detected_xui_version", "") or "-",
        health_status=_panel_health_status(panel),
        selected_inbounds=[_inbound_summary(inbound) for inbound in inbounds],
        group_index=group_index,
    )


def _safe_quick_panel_result_dict(result):
    return {
        "panel_id": result.panel_id,
        "panel_name": result.panel_name,
        "family": result.family,
        "capability_profile": result.capability_profile,
        "detected_version": result.detected_version,
        "health_status": result.health_status,
        "selected_inbounds": result.selected_inbounds,
        "group_index": result.group_index,
        "success": result.success,
        "create_success": result.create_success,
        "create_mode": result.create_mode,
        "link_count": result.link_count,
        "warnings": result.warnings,
        "errors": result.errors,
        "structured_errors": result.structured_errors,
    }


def _quick_config_link_metadata(entry_remote, *, panel, inbound, group_index):
    base = _redacted_panel_config_metadata(entry_remote, panel=panel, inbound=inbound)
    return {
        **base,
        "source": "quick_builder_multi_panel",
        "generated_by": "quick_builder_multi_panel",
        "panel_id": getattr(panel, "pk", None),
        "panel_name": getattr(panel, "name", "") or "",
        "inbound_pk": getattr(inbound, "pk", None),
        "remote_inbound_id": getattr(inbound, "inbound_id", None),
        "protocol": getattr(inbound, "protocol", "") or "",
        "host": getattr(inbound, "server_ip", "") or "",
        "port": getattr(inbound, "port", "") or "",
        "group_index": group_index,
    }


def _traffic_limit_bytes_from_gb(value):
    try:
        return int((value or Decimal("0")) * Decimal(1024 ** 3))
    except Exception:
        return 0


def _quick_inventory_link_metadata(asset, allocation, *, pool):
    return {
        "source": "quick_builder_inventory_pool",
        "generated_by": "quick_builder_multi_panel",
        "pool_id": getattr(pool, "pk", None),
        "pool_title": sanitize_error_value(getattr(pool, "title", "") or ""),
        "asset_id": getattr(asset, "pk", None),
        "allocation_id": getattr(allocation, "pk", None),
        "allocation_mode": getattr(allocation, "allocation_mode", "") or getattr(pool, "allocation_mode", ""),
        "asset_hash_suffix": (getattr(asset, "normalized_hash", "") or "")[-8:],
        "created_at": timezone.now().isoformat(),
    }


def _quick_inventory_result(pool, quantity, group_index):
    summary = get_pool_stock_summary(pool)
    return {
        "pool_id": pool.pk,
        "pool_title": sanitize_error_value(pool.title),
        "allocation_mode": pool.allocation_mode,
        "allocation_mode_display": pool.get_allocation_mode_display(),
        "requested_quantity": quantity,
        "available_before": "Unlimited" if summary["available_capacity"] is None else summary["available_capacity"],
        "asset_count_before": summary["asset_count"],
        "usable_asset_count_before": summary["usable_asset_count"],
        "group_index": group_index,
        "success": False,
        "allocated_count": 0,
        "link_count": 0,
        "warnings": [],
        "errors": [],
        "structured_errors": [],
    }


def quick_build_multi_panel_subscription_cup(request_data, admin_user=None, *, adapter_factory=None, request=None):
    inbounds = list(request_data.get("inbounds") or [])
    inventory_pools = list(request_data.get("inventory_pools") or [])
    inventory_quantity = int(request_data.get("inventory_quantity") or 1)
    if not inbounds and not inventory_pools:
        message = "حداقل یک Inbound یا یک استخر کانفیگ آماده انتخاب کنید."
        structured = _cup_structured_error(
            error_code="quick_builder_source_required",
            layer="cup_builder",
            action="validate_quick_builder",
            message=message,
            remediation="از بخش منابع پنل یک Inbound یا از بخش استخرها یک Pool انتخاب کنید.",
        )
        _raise_cup_error(CupCenterValidationError, message, structured_error=structured)
    adapter_factory = adapter_factory or get_safe_panel_adapter
    groups = _validated_quick_build_groups(inbounds, adapter_factory) if inbounds else []
    selected_inbounds = [_inbound_summary(inbound) for inbound in inbounds]
    selected_inventory_pools = [_inventory_pool_summary(pool) for pool in inventory_pools]
    panel_results = []
    inventory_results = []
    config_links = []
    cup_items = []
    created_remote_client_groups_count = 0
    inventory_allocation_count = 0
    position = 1

    with transaction.atomic():
        cup = SubscriptionCup.objects.create(
            title=str(request_data.get("title") or "").strip(),
            status=SubscriptionCup.Status.ACTIVE,
            expires_at=request_data.get("expires_at"),
            traffic_limit_bytes=_traffic_limit_bytes_from_gb(request_data.get("volume_gb")),
            device_limit=int(request_data.get("device_limit") or 2),
            metadata={
                "source": "quick_subscription_builder_multi_panel",
                "generated_by": "quick_builder_multi_panel",
                "panel_ids": [group["panel"].pk for group in groups],
                "inbound_pks": [inbound.pk for inbound in inbounds],
                "inventory_pool_ids": [pool.pk for pool in inventory_pools],
                "inventory_quantity_per_pool": inventory_quantity,
                "volume_gb": str(request_data.get("volume_gb") or ""),
                "duration_days": int(request_data.get("duration_days") or 30),
                "admin_user_id": getattr(admin_user, "pk", None),
                "remote_created": False,
                "created_at": timezone.now().isoformat(),
            },
        )

    for group_index, group in enumerate(groups, start=1):
        panel = group["panel"]
        group_inbounds = group["inbounds"]
        adapter = group["adapter"]
        result = _quick_panel_result(panel, group_inbounds, group_index)
        result.create_mode = "multi_inbound" if len(group_inbounds) > 1 else "single_inbound"
        try:
            remote_result = _remote_create_for_quick_build(panel, group_inbounds, request_data, adapter)
            result.create_success = True
            entries = _direct_link_entries_from_remote(remote_result, group_inbounds)
            missing_links = [entry for entry in entries if not entry["direct_link"]]
            if missing_links:
                message = "پنل client را ساخت اما همه لینک‌های مستقیم قابل ذخیره را برنگرداند."
                structured = _cup_structured_error(
                    error_code="cup_remote_create_missing_link",
                    layer="subscription_render",
                    action=result.create_mode,
                    message=message,
                    remediation="خروجی direct_link پنل را بررسی کنید و در صورت نیاز لینک‌ها را از پنل بازیابی و manual اضافه کنید.",
                    panel=panel,
                    inbound=group_inbounds[0] if group_inbounds else None,
                    safe_context={
                        "selected_inbound_pks": [getattr(inbound, "pk", None) for inbound in group_inbounds],
                        "missing_link_count": len(missing_links),
                    },
                )
                raise CupCenterRemoteCreateError(message, structured_error=structured)

            with transaction.atomic():
                for entry in entries:
                    inbound = entry["inbound"]
                    entry_remote = entry["remote_result"]
                    config_link = create_config_link_from_raw(
                        entry["direct_link"],
                        source_type=ConfigLink.SourceType.PANEL_GENERATED,
                        source_panel=panel,
                        source_inbound=inbound,
                        metadata={**_quick_config_link_metadata(entry_remote, panel=panel, inbound=inbound, group_index=group_index), "cup_id": cup.pk},
                    )
                    cup_item = CupItem.objects.create(
                        cup=cup,
                        config_link=config_link,
                        position=position,
                        is_active=True,
                        added_reason="quick_builder",
                        metadata={
                            "source": "quick_builder_multi_panel",
                            "generated_by": "quick_builder_multi_panel",
                            "panel_id": panel.pk,
                            "inbound_pk": inbound.pk,
                            "group_index": group_index,
                        },
                    )
                    config_links.append(config_link)
                    cup_items.append(cup_item)
                    position += 1
            result.success = True
            result.link_count = len(entries)
            created_remote_client_groups_count += 1
        except (CupCenterRemoteCreateError, CupCenterRemoteSaveError) as exc:
            result.errors.append(str(exc))
            if getattr(exc, "structured_error", None):
                result.structured_errors.append(exc.structured_error)
        except Exception as exc:
            result.errors.append(_safe_panel_error(exc, panel=panel))
            result.structured_errors.append(
                safe_error_dict(
                    exc,
                    error_code="cup_remote_create_failed",
                    layer="cup_builder",
                    action=result.create_mode or "create_client",
                    message="ساخت کانفیگ روی پنل برای Quick Builder ناموفق بود.",
                    remediation="خطای پنل را بررسی و پس از رفع مشکل دوباره تلاش کنید.",
                    panel=panel,
                    inbound=group_inbounds[0] if group_inbounds else None,
                )
            )
        panel_results.append(_safe_quick_panel_result_dict(result))

    for pool_index, pool in enumerate(inventory_pools, start=1):
        result = _quick_inventory_result(pool, inventory_quantity, pool_index)
        try:
            with transaction.atomic():
                allocation_result = allocate_assets_from_pool(pool, inventory_quantity, cup=cup)
                for asset, allocation in zip(allocation_result.assets, allocation_result.allocations, strict=False):
                    config_link = create_config_link_from_raw(
                        asset.raw_link,
                        source_type=ConfigLink.SourceType.IMPORTED_SUBSCRIPTION,
                        metadata={**_quick_inventory_link_metadata(asset, allocation, pool=pool), "cup_id": cup.pk},
                    )
                    cup_item = CupItem.objects.create(
                        cup=cup,
                        config_link=config_link,
                        position=position,
                        is_active=True,
                        added_reason="quick_builder_inventory",
                        metadata={
                            "source": "quick_builder_inventory_pool",
                            "generated_by": "quick_builder_multi_panel",
                            "pool_id": pool.pk,
                            "asset_id": asset.pk,
                            "allocation_id": allocation.pk,
                            "allocation_mode": allocation.allocation_mode,
                            "group_index": pool_index,
                        },
                    )
                    config_links.append(config_link)
                    cup_items.append(cup_item)
                    position += 1
                result["success"] = True
                result["allocated_count"] = allocation_result.allocated_count
                result["link_count"] = allocation_result.allocated_count
                inventory_allocation_count += allocation_result.allocated_count
        except ConfigInventoryError as exc:
            message = sanitize_error_value(getattr(exc, "safe_message", str(exc)))
            result["errors"].append(message)
            result["structured_errors"].append(
                _cup_structured_error(
                    exc,
                    error_code=f"config_inventory_{getattr(exc, 'code', 'allocation_failed')}",
                    layer="inventory_allocation",
                    action="allocate_inventory_pool",
                    message=message,
                    remediation="موجودی استخر، فعال بودن Pool و مقدار درخواستی را بررسی کنید.",
                    safe_context={
                        "pool_id": pool.pk,
                        "pool_title": sanitize_error_value(pool.title),
                        "requested_quantity": inventory_quantity,
                    },
                )
            )
        except Exception as exc:
            message = sanitize_error_value(exc)
            result["errors"].append(message)
            result["structured_errors"].append(
                safe_error_dict(
                    exc,
                    error_code="config_inventory_allocation_failed",
                    layer="inventory_allocation",
                    action="allocate_inventory_pool",
                    message="تخصیص کانفیگ از استخر برای Quick Builder ناموفق بود.",
                    remediation="موجودی استخر و سلامت لینک‌های واردشده را بررسی کنید و دوباره تلاش کنید.",
                )
            )
        inventory_results.append(result)

    panel_errors = [error for panel_result in panel_results for error in panel_result.get("errors", [])]
    inventory_errors = [error for inventory_result in inventory_results for error in inventory_result.get("errors", [])]
    errors = panel_errors + inventory_errors
    structured_errors = [
        error
        for panel_result in panel_results
        for error in panel_result.get("structured_errors", [])
    ] + [
        error
        for inventory_result in inventory_results
        for error in inventory_result.get("structured_errors", [])
    ]
    warnings = [warning for panel_result in panel_results for warning in panel_result.get("warnings", [])] + [
        warning for inventory_result in inventory_results for warning in inventory_result.get("warnings", [])
    ]
    if errors and config_links:
        status = "partial_success"
    elif errors:
        status = "failed"
    else:
        status = "success"

    SubscriptionCup.objects.filter(pk=cup.pk).update(
        metadata={
            **(cup.metadata or {}),
            "remote_created": bool(created_remote_client_groups_count),
            "status": status,
            "selected_panel_count": len(groups),
            "selected_inbound_count": len(inbounds),
            "selected_inventory_pool_count": len(inventory_pools),
            "created_remote_client_groups_count": created_remote_client_groups_count,
            "inventory_allocation_count": inventory_allocation_count,
            "config_link_count": len(config_links),
            "panel_results": panel_results,
            "inventory_results": inventory_results,
            "updated_at": timezone.now().isoformat(),
        }
    )
    cup.refresh_from_db()

    urls = subscription_url_summary(cup, request=request)
    return QuickBuildResult(
        cup=cup,
        config_links=config_links,
        cup_items=cup_items,
        selected_inbounds=selected_inbounds,
        masked_subscription_url=urls["masked_client_url"],
        protocols=cup_protocols(cup),
        selected_inventory_pools=selected_inventory_pools,
        status=status,
        selected_panels_count=len(groups),
        selected_inbounds_count=len(inbounds),
        selected_inventory_pools_count=len(inventory_pools),
        created_remote_client_groups_count=created_remote_client_groups_count,
        inventory_allocation_count=inventory_allocation_count,
        config_link_count=len(config_links),
        panel_results=panel_results,
        inventory_results=inventory_results,
        warnings=warnings,
        errors=errors,
        structured_errors=structured_errors,
    )


def quick_build_subscription_cup(request_data, admin_user=None, *, adapter=None, request=None):
    adapter_factory = (lambda panel: adapter) if adapter is not None else get_safe_panel_adapter
    return quick_build_multi_panel_subscription_cup(request_data, admin_user=admin_user, adapter_factory=adapter_factory, request=request)


def render_cup_preview(cup):
    active_items = list(cup_item_queryset(cup).filter(is_active=True, config_link__is_active=True))
    inactive_count = cup.items.filter(Q(is_active=False) | Q(config_link__is_active=False)).count()
    raw_text = render_subscription_cup_raw(cup) if cup.is_accessible else ""
    encoded = base64.b64encode(raw_text.encode("utf-8")).decode("ascii") if raw_text else ""
    protocol_counts = {}
    for item in active_items:
        protocol = item.config_link.protocol or ConfigLink.Protocol.UNKNOWN
        protocol_counts[protocol] = protocol_counts.get(protocol, 0) + 1
    return {
        "is_accessible": cup.is_accessible,
        "endpoint_status": "ok" if cup.is_accessible else "blocked",
        "active_count": len(active_items),
        "inactive_count": inactive_count,
        "base64_length": len(encoded),
        "raw_line_count": len(raw_text.splitlines()) if raw_text else 0,
        "protocol_counts": protocol_counts,
        "protocols": cup_protocols(cup),
        "masked_lines": [mask_link_for_display(item.config_link) for item in active_items[:50]],
    }


def set_cup_status(cup, status):
    if status not in SubscriptionCup.Status.values:
        raise CupCenterError("وضعیت Cup معتبر نیست.")
    cup.status = status
    cup.save(update_fields=["status", "updated_at"])
    return cup


def set_cup_item_active(cup, item_id, is_active):
    item = CupItem.objects.get(cup=cup, pk=item_id)
    item.is_active = bool(is_active)
    item.save(update_fields=["is_active", "updated_at"])
    return item


def move_cup_item(cup, item_id, direction):
    items = list(cup.items.order_by("position", "pk"))
    index = next((idx for idx, item in enumerate(items) if item.pk == int(item_id)), None)
    if index is None:
        raise CupItem.DoesNotExist
    swap_index = index - 1 if direction == "up" else index + 1
    if swap_index < 0 or swap_index >= len(items):
        return items[index]
    current = items[index]
    other = items[swap_index]
    current.position, other.position = other.position, current.position
    current.save(update_fields=["position", "updated_at"])
    other.save(update_fields=["position", "updated_at"])
    return current


def rebuild_cup_from_source(cup):
    if cup.vpn_client_id:
        return rebuild_subscription_cup_for_vpn_client(cup.vpn_client, force_active=True, added_reason="admin_rebuild")
    if cup.order_id:
        cups = rebuild_subscription_cups_for_order(cup.order, force_active=True, added_reason="admin_rebuild")
        return cups[0] if cups else None
    return None
