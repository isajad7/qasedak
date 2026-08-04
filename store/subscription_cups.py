import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.db import transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import ConfigLink, CupItem, Inbound, Order, SubscriptionCup, VPNClient


logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOLS = {
    ConfigLink.Protocol.VLESS,
    ConfigLink.Protocol.VMESS,
    ConfigLink.Protocol.TROJAN,
    ConfigLink.Protocol.SS,
}


@dataclass(frozen=True)
class ParsedConfigLink:
    raw_link: str
    normalized_link: str
    normalized_hash: str
    protocol: str
    remark: str = ""
    host: str = ""
    port: int | None = None


def _safe_port(parts):
    try:
        return parts.port
    except ValueError:
        return None


def _decode_vmess_payload(raw_link):
    payload = raw_link.split("://", 1)[1].split("#", 1)[0].strip()
    if not payload:
        return {}
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}").decode("utf-8", "ignore")
        data = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_int(value):
    try:
        if value in (None, ""):
            return None
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_standard_url(raw_link):
    try:
        parts = urlsplit(raw_link)
    except ValueError:
        return "", None, ""
    return parts.hostname or "", _safe_port(parts), unquote(parts.fragment or "")


def _parse_shadowsocks(raw_link):
    host, port, remark = _parse_standard_url(raw_link)
    if host:
        return host, port, remark

    body = raw_link.split("://", 1)[1].split("#", 1)[0].strip() if "://" in raw_link else ""
    if "@" not in body:
        return host, port, remark
    server = body.rsplit("@", 1)[1]
    if ":" not in server:
        return host, port, remark
    server_host, server_port = server.rsplit(":", 1)
    return server_host.strip("[]"), _safe_int(server_port), remark


def parse_config_link(raw_link):
    raw_link = str(raw_link or "").strip()
    protocol = ConfigLink.Protocol.UNKNOWN
    if "://" in raw_link:
        prefix = raw_link.split("://", 1)[0].strip().lower()
        if prefix in SUPPORTED_PROTOCOLS:
            protocol = prefix

    normalized_link = raw_link
    normalized_hash = hashlib.sha256((normalized_link or raw_link).encode("utf-8")).hexdigest() if raw_link else ""
    host = ""
    port = None
    remark = ""

    try:
        if protocol in {ConfigLink.Protocol.VLESS, ConfigLink.Protocol.TROJAN}:
            host, port, remark = _parse_standard_url(raw_link)
        elif protocol == ConfigLink.Protocol.SS:
            host, port, remark = _parse_shadowsocks(raw_link)
        elif protocol == ConfigLink.Protocol.VMESS:
            data = _decode_vmess_payload(raw_link)
            host = str(data.get("add") or "").strip()
            port = _safe_int(data.get("port"))
            remark = str(data.get("ps") or "").strip()
    except Exception:
        host = ""
        port = None
        remark = ""

    return ParsedConfigLink(
        raw_link=raw_link,
        normalized_link=normalized_link,
        normalized_hash=normalized_hash,
        protocol=protocol,
        remark=remark[:255],
        host=host[:255],
        port=port,
    )


def apply_config_link_parse(config_link, raw_link, *, source_type=None, source_panel=None, source_inbound=None, vpn_client=None, metadata=None):
    parsed = parse_config_link(raw_link)
    config_link.raw_link = parsed.raw_link
    config_link.normalized_link = parsed.normalized_link
    config_link.normalized_hash = parsed.normalized_hash
    config_link.protocol = parsed.protocol
    config_link.remark = parsed.remark
    config_link.host = parsed.host
    config_link.port = parsed.port
    if source_type:
        config_link.source_type = source_type
    config_link.source_panel = source_panel
    config_link.source_inbound = source_inbound
    config_link.vpn_client = vpn_client
    config_link.is_active = True
    if metadata is not None:
        config_link.metadata = metadata
    return config_link


def create_config_link_from_raw(raw_link, *, source_type=ConfigLink.SourceType.UNKNOWN, source_panel=None, source_inbound=None, vpn_client=None, metadata=None):
    config_link = ConfigLink()
    apply_config_link_parse(
        config_link,
        raw_link,
        source_type=source_type,
        source_panel=source_panel,
        source_inbound=source_inbound,
        vpn_client=vpn_client,
        metadata=metadata or {},
    )
    config_link.save()
    return config_link


def _panel_for_inbound(inbound):
    if not inbound:
        return None
    try:
        return inbound.panel
    except Exception:
        return None


def _bundle_inbounds(vpn_client, bundle_results):
    raw = vpn_client.xui_raw or {}
    inbound_pks = raw.get("bundle_inbound_pks") or []
    explicit_pks = [
        result.get("source_inbound_pk") or result.get("inbound_pk")
        for result in bundle_results
        if isinstance(result, dict)
    ]
    pks = [pk for pk in [*inbound_pks, *explicit_pks] if pk]
    if not pks:
        return {}
    return {inbound.pk: inbound for inbound in Inbound.objects.select_related("panel").filter(pk__in=pks)}


def config_entries_for_vpn_client(vpn_client):
    entries = []
    raw = vpn_client.xui_raw or {}
    bundle_results = raw.get("bundle_inbound_results") or []
    if isinstance(bundle_results, list) and bundle_results:
        inbounds_by_pk = _bundle_inbounds(vpn_client, bundle_results)
        inbound_pks = raw.get("bundle_inbound_pks") or []
        for index, result in enumerate(bundle_results, start=1):
            if not isinstance(result, dict):
                continue
            direct_link = str(result.get("direct_link") or "").strip()
            if not direct_link:
                continue
            inbound_pk = result.get("source_inbound_pk") or result.get("inbound_pk")
            if not inbound_pk and index <= len(inbound_pks):
                inbound_pk = inbound_pks[index - 1]
            source_inbound = inbounds_by_pk.get(inbound_pk) or vpn_client.inbound
            entries.append(
                {
                    "raw_link": direct_link,
                    "source_inbound": source_inbound,
                    "source_panel": _panel_for_inbound(source_inbound),
                    "metadata": {
                        "source": "vpn_client_bundle",
                        "bundle_position": index,
                    },
                }
            )

    if not entries and vpn_client.direct_link:
        entries.append(
            {
                "raw_link": vpn_client.direct_link,
                "source_inbound": vpn_client.inbound,
                "source_panel": _panel_for_inbound(vpn_client.inbound),
                "metadata": {"source": "vpn_client"},
            }
        )
    return entries


def config_entries_for_order(order):
    if not order.direct_link:
        return []
    return [
        {
            "raw_link": order.direct_link,
            "source_inbound": order.inbound,
            "source_panel": _panel_for_inbound(order.inbound),
            "metadata": {"source": "order"},
        }
    ]


def _cup_title(order=None, vpn_client=None):
    if vpn_client and vpn_client.username:
        return vpn_client.username
    if order and order.plan_id:
        return order.plan.name
    return ""


def _external_subscription_link(order=None, vpn_client=None):
    return str(getattr(vpn_client, "sub_link", "") or getattr(order, "sub_link", "") or "").strip()


def _cup_identity(order=None, vpn_client=None):
    order = order or getattr(vpn_client, "order", None)
    plan = getattr(vpn_client, "plan", None) or getattr(order, "plan", None)
    customer = getattr(order, "customer", None)
    return {
        "customer": customer,
        "order": order,
        "plan": plan,
        "vpn_client": vpn_client,
        "expires_at": getattr(vpn_client, "expires_at", None),
        "traffic_limit_bytes": int(getattr(vpn_client, "traffic_limit_bytes", 0) or getattr(plan, "traffic_limit_bytes", 0) or 0),
        "device_limit": getattr(vpn_client, "device_limit", None) or getattr(plan, "device_limit", None),
        "title": _cup_title(order=order, vpn_client=vpn_client),
    }


def _get_cup_for_target(order=None, vpn_client=None):
    queryset = SubscriptionCup.objects.select_for_update().order_by("created_at", "pk")
    if vpn_client:
        return queryset.filter(vpn_client=vpn_client).first()
    return queryset.filter(order=order, vpn_client__isnull=True).first()


def _update_cup(cup, *, order=None, vpn_client=None, force_active=False):
    identity = _cup_identity(order=order, vpn_client=vpn_client)
    for field, value in identity.items():
        setattr(cup, field, value)
    if force_active:
        cup.status = SubscriptionCup.Status.ACTIVE
    metadata = dict(cup.metadata or {})
    external_sub = _external_subscription_link(order=identity["order"], vpn_client=vpn_client)
    metadata.update(
        {
            "external_panel_subscription_link": external_sub,
            "external_panel_subscription_link_saved": bool(external_sub),
            "last_rebuilt_at": timezone.now().isoformat(),
        }
    )
    cup.metadata = metadata
    cup.save()
    return cup


def _create_or_update_item(cup, entry, *, position, reusable_items, vpn_client=None, added_reason="rebuild"):
    raw_link = entry["raw_link"]
    item = None
    for candidate in list(reusable_items):
        if candidate.config_link.raw_link == raw_link:
            item = candidate
            reusable_items.remove(candidate)
            break

    config_metadata = {
        **(entry.get("metadata") or {}),
        "cup_id": cup.pk,
    }
    if item:
        config_link = item.config_link
        apply_config_link_parse(
            config_link,
            raw_link,
            source_type=ConfigLink.SourceType.PANEL_GENERATED,
            source_panel=entry.get("source_panel"),
            source_inbound=entry.get("source_inbound"),
            vpn_client=vpn_client,
            metadata=config_metadata,
        )
        config_link.save()
        item.position = position
        item.is_active = True
        item.added_reason = added_reason
        item.metadata = entry.get("metadata") or {}
        item.save(update_fields=["position", "is_active", "added_reason", "metadata", "updated_at"])
        return item

    config_link = create_config_link_from_raw(
        raw_link,
        source_type=ConfigLink.SourceType.PANEL_GENERATED,
        source_panel=entry.get("source_panel"),
        source_inbound=entry.get("source_inbound"),
        vpn_client=vpn_client,
        metadata=config_metadata,
    )
    return CupItem.objects.create(
        cup=cup,
        config_link=config_link,
        position=position,
        is_active=True,
        added_reason=added_reason,
        metadata=entry.get("metadata") or {},
    )


def _rebuild_cup(order=None, vpn_client=None, *, entries, force_active=False, added_reason="rebuild"):
    if not order and vpn_client:
        order = vpn_client.order
    with transaction.atomic():
        cup = _get_cup_for_target(order=order, vpn_client=vpn_client)
        if not cup:
            cup = SubscriptionCup()
            if not force_active:
                cup.status = SubscriptionCup.Status.ACTIVE
        cup = _update_cup(cup, order=order, vpn_client=vpn_client, force_active=force_active)
        reusable_items = list(cup.items.select_related("config_link").order_by("position", "pk"))
        active_item_ids = []
        for position, entry in enumerate(entries, start=1):
            item = _create_or_update_item(
                cup,
                entry,
                position=position,
                reusable_items=reusable_items,
                vpn_client=vpn_client,
                added_reason=added_reason,
            )
            active_item_ids.append(item.pk)
        stale_items = cup.items.exclude(pk__in=active_item_ids) if active_item_ids else cup.items.all()
        stale_items.update(is_active=False, updated_at=timezone.now())
    return cup


def rebuild_subscription_cup_for_vpn_client(vpn_client, *, force_active=False, added_reason="rebuild"):
    if not isinstance(vpn_client, VPNClient):
        vpn_client = VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel").get(pk=vpn_client)
    entries = config_entries_for_vpn_client(vpn_client)
    return _rebuild_cup(
        order=vpn_client.order,
        vpn_client=vpn_client,
        entries=entries,
        force_active=force_active,
        added_reason=added_reason,
    )


def rebuild_subscription_cups_for_order(order, *, force_active=False, added_reason="rebuild"):
    if not isinstance(order, Order):
        order = Order.objects.select_related("store", "customer", "plan", "inbound", "inbound__panel").get(pk=order)
    clients = list(
        order.get_vpn_clients()
        .select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .order_by("created_at", "pk")
    )
    if clients:
        return [
            rebuild_subscription_cup_for_vpn_client(
                vpn_client,
                force_active=force_active,
                added_reason=added_reason,
            )
            for vpn_client in clients
        ]
    entries = config_entries_for_order(order)
    if not entries:
        return []
    return [
        _rebuild_cup(
            order=order,
            entries=entries,
            force_active=force_active,
            added_reason=added_reason,
        )
    ]


def get_subscription_cup_for_vpn_client(vpn_client):
    if not vpn_client:
        return None
    return SubscriptionCup.objects.filter(vpn_client=vpn_client).order_by("created_at", "pk").first()


def get_subscription_cup_for_order(order):
    if not order:
        return None
    return SubscriptionCup.objects.filter(order=order, vpn_client__isnull=True).order_by("created_at", "pk").first()


def active_cup_links(cup):
    return list(
        CupItem.objects.filter(cup=cup, is_active=True, config_link__is_active=True)
        .select_related("config_link")
        .order_by("position", "pk")
        .values_list("config_link__raw_link", flat=True)
    )


def render_subscription_cup_raw(cup):
    return "\n".join(active_cup_links(cup))


def render_subscription_cup_base64(cup):
    raw_text = render_subscription_cup_raw(cup)
    return base64.b64encode(raw_text.encode("utf-8")).decode("ascii")


def render_subscription_cup(cup, *, output_format="base64"):
    if str(output_format or "").lower() == "raw":
        return render_subscription_cup_raw(cup)
    return render_subscription_cup_base64(cup)


def cup_protocols(cup):
    protocols = []
    for protocol in (
        CupItem.objects.filter(cup=cup, is_active=True, config_link__is_active=True)
        .order_by("position", "pk")
        .values_list("config_link__protocol", flat=True)
    ):
        if protocol not in protocols:
            protocols.append(protocol)
    return protocols


def build_subscription_cup_path(cup):
    try:
        return reverse("subscription_cup", args=[cup.token])
    except NoReverseMatch:
        return f"/sub/{cup.token}"


def public_base_url_for_store(store=None):
    domain = str(getattr(store, "domain", "") or "").strip().rstrip("/")
    if not domain:
        return ""
    parts = urlsplit(domain)
    if parts.scheme and parts.netloc:
        return domain
    scheme = "http" if settings.DEBUG else "https"
    return f"{scheme}://{domain}"


def build_subscription_cup_url(cup, *, request=None, store=None):
    path = build_subscription_cup_path(cup)
    if request:
        return request.build_absolute_uri(path)
    base_url = public_base_url_for_store(
        store
        or getattr(getattr(cup, "order", None), "store", None)
        or getattr(getattr(cup, "vpn_client", None), "store", None)
        or getattr(getattr(cup, "plan", None), "store", None)
    )
    return f"{base_url}{path}" if base_url else path


def mask_subscription_url(url, token):
    token = str(token or "")
    url = str(url or "")
    if not token or token not in url:
        return url
    if len(token) <= 8:
        masked = "***"
    else:
        masked = f"{token[:4]}...{token[-4:]}"
    return url.replace(token, masked)
