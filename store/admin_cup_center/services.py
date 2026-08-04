import base64
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone

from store.config_lookup import mask_identifier
from store.models import ConfigLink, CupItem, Inbound, Panel, SubscriptionCup
from store.panels.factory import get_safe_panel_adapter
from store.panels.xui.adapter import XUIProvisioningRequest
from store.subscription_cups import (
    build_subscription_cup_path,
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
    pass


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


@dataclass
class QuickBuildResult:
    cup: SubscriptionCup
    config_links: list[ConfigLink]
    cup_items: list[CupItem]
    selected_inbounds: list[dict]
    masked_subscription_url: str
    protocols: list[str]
    email_masked: str = ""
    status: str = "success"
    selected_panels_count: int = 0
    selected_inbounds_count: int = 0
    created_remote_client_groups_count: int = 0
    config_link_count: int = 0
    panel_results: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
    raw_url = f"{url}?format=raw"
    return {
        "url": url,
        "raw_url": raw_url,
        "masked_url": mask_subscription_url(url, cup.token),
        "masked_raw_url": mask_subscription_url(raw_url, cup.token),
        "path": build_subscription_cup_path(cup),
        "raw_path": f"{build_subscription_cup_path(cup)}?format=raw",
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
    return sanitize_xui_operational_text(exc, panel=panel, max_length=240) or "panel_operation_failed"


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


def _report_create_error(report):
    details = "; ".join(str(message).strip() for message in (getattr(report, "errors", ()) or getattr(report, "warnings", ())) if str(message).strip())
    if details:
        safe_details = sanitize_xui_operational_text(details, max_length=180)
        return f"این پنل یا خانواده پنل برای ساخت client پشتیبانی نمی‌شود. {safe_details}".strip()
    return "این پنل از ساخت client پشتیبانی نمی‌کند."


def create_panel_config_into_cup(cup, panel, inbound, options, *, adapter=None):
    if not isinstance(panel, Panel) or not isinstance(inbound, Inbound):
        raise CupCenterRemoteCreateError("Panel و Inbound معتبر نیستند.")
    if inbound.panel_id != panel.pk:
        raise CupCenterRemoteCreateError("Inbound انتخاب‌شده به Panel انتخاب‌شده وصل نیست.")
    adapter = adapter or get_safe_panel_adapter(panel)
    try:
        report = adapter.get_capability_report()
    except Exception:
        report = None
    if report is not None and not getattr(report, "supports_create_client", False):
        raise CupCenterRemoteCreateError(_report_create_error(report))

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
        raise CupCenterRemoteCreateError(_safe_panel_error(exc, panel=panel)) from exc

    direct_link = str((remote_result or {}).get("direct_link") or "").strip()
    if not direct_link:
        raise CupCenterRemoteCreateError("پنل client را ساخت اما لینک مستقیم قابل ذخیره برنگرداند.")

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
        raise CupCenterRemoteSaveError(
            "کانفیگ واقعی روی پنل ساخته شد، اما ذخیره local ناموفق بود. لینک را از پنل بازیابی و به صورت manual اضافه کنید."
        ) from exc

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
        raise CupCenterValidationError("Panel فعال و معتبر انتخاب نشده است.")
    if not inbounds:
        raise CupCenterValidationError("حداقل یک inbound انتخاب کنید.")

    seen_remote_ids = set()
    for inbound in inbounds:
        if not isinstance(inbound, Inbound):
            raise CupCenterValidationError("Inbound انتخاب‌شده معتبر نیست.")
        if inbound.panel_id != panel.pk:
            raise CupCenterValidationError("Inbound انتخاب‌شده به Panel خودش وصل نیست.")
        if not inbound.panel.is_active:
            raise CupCenterValidationError("Panel یکی از inboundها فعال نیست.")
        if not inbound.is_active or not inbound.available_for_new_orders:
            raise CupCenterValidationError("Inbound انتخاب‌شده فعال یا قابل فروش نیست.")
        if inbound.protocol not in SUPPORTED_INBOUND_PROTOCOLS:
            raise CupCenterValidationError("Protocol یکی از inboundها پشتیبانی نمی‌شود.")
        remote_id = str(inbound.inbound_id)
        if remote_id in seen_remote_ids:
            raise CupCenterValidationError("Inbound ID تکراری داخل یک Panel برای ساخت remote مجاز نیست.")
        seen_remote_ids.add(remote_id)

    if report is not None:
        if not getattr(report, "supported", True) or not getattr(report, "supports_create_client", False):
            raise CupCenterValidationError(_report_create_error(report))
        if len(inbounds) > 1 and not getattr(report, "supports_multi_inbound_create", False):
            raise CupCenterValidationError("این پنل برای انتخاب چند inbound در یک گروه پشتیبانی نمی‌شود.")
    elif len(inbounds) > 1:
        raise CupCenterValidationError("قابلیت ساخت چند inbound برای این پنل قابل تایید نیست.")


def _validated_quick_build_groups(inbounds, adapter_factory):
    inbounds = list(inbounds or [])
    if not inbounds:
        raise CupCenterValidationError("حداقل یک inbound انتخاب کنید.")
    groups = _group_quick_build_inbounds(inbounds)
    for group in groups:
        panel = group["panel"]
        adapter = adapter_factory(panel)
        try:
            report = adapter.get_capability_report()
        except Exception as exc:
            raise CupCenterValidationError(_safe_panel_error(exc, panel=panel)) from exc
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
        raise CupCenterRemoteCreateError(_safe_panel_error(exc, panel=panel)) from exc


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


def quick_build_multi_panel_subscription_cup(request_data, admin_user=None, *, adapter_factory=None, request=None):
    inbounds = list(request_data.get("inbounds") or [])
    adapter_factory = adapter_factory or get_safe_panel_adapter
    groups = _validated_quick_build_groups(inbounds, adapter_factory)
    selected_inbounds = [_inbound_summary(inbound) for inbound in inbounds]
    panel_results = []
    config_links = []
    cup_items = []
    created_remote_client_groups_count = 0
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
                raise CupCenterRemoteCreateError("پنل client را ساخت اما همه لینک‌های مستقیم قابل ذخیره را برنگرداند.")

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
        except Exception as exc:
            result.errors.append(_safe_panel_error(exc, panel=panel))
        panel_results.append(_safe_quick_panel_result_dict(result))

    errors = [error for panel_result in panel_results for error in panel_result.get("errors", [])]
    warnings = [warning for panel_result in panel_results for warning in panel_result.get("warnings", [])]
    if errors and config_links:
        status = "partial_success"
    elif errors:
        status = "failed"
    else:
        status = "success"

    SubscriptionCup.objects.filter(pk=cup.pk).update(
        metadata={
            **(cup.metadata or {}),
            "remote_created": bool(config_links),
            "status": status,
            "selected_panel_count": len(groups),
            "selected_inbound_count": len(inbounds),
            "created_remote_client_groups_count": created_remote_client_groups_count,
            "config_link_count": len(config_links),
            "panel_results": panel_results,
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
        masked_subscription_url=urls["masked_url"],
        protocols=cup_protocols(cup),
        status=status,
        selected_panels_count=len(groups),
        selected_inbounds_count=len(inbounds),
        created_remote_client_groups_count=created_remote_client_groups_count,
        config_link_count=len(config_links),
        panel_results=panel_results,
        warnings=warnings,
        errors=errors,
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
