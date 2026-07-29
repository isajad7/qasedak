import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .db_locking import select_for_update_self
from .models import Inbound, Order, Panel, VPNClient
from .naming import build_client_display_name, build_xui_client_email
from .referral_services import create_referral_reward_for_order
from .xui_api import (
    XUIError,
    XUIService,
    bytes_from_gb,
    create_enabled_client_details,
    create_enabled_multi_inbound_client_details,
    find_xui_client_and_stats,
    first_xui_value,
    parse_xui_datetime,
    sanitize_xui_operational_text,
    xui_bool,
)
from .xui_compat import build_remote_client_key, inbound_remote_key


logger = logging.getLogger(__name__)

STRATEGY_LEGACY_PRECREATE_INACTIVE = "legacy_precreate_inactive"
STRATEGY_DEFERRED_CREATE_ENABLED = "deferred_create_enabled"
STRATEGY_DIRECT_CREATE_ENABLED = "direct_create_enabled"
STRATEGY_RENEW_EXISTING_CLIENT = "renew_existing_client"

MODERN_PROFILES = {
    Panel.CapabilityProfile.MODERN_SINGLE_NODE,
    Panel.CapabilityProfile.MODERN_MULTI_NODE,
}


@dataclass
class ProvisioningResult:
    ok: bool
    message: str
    already_provisioned: bool = False
    order_status: str = ""
    provisioning_status: str = ""
    client: VPNClient | None = None
    delivery_attempted: bool = False
    safe_error: str = ""

    @property
    def success(self):
        return self.ok


def panel_profile(panel):
    return getattr(panel, "capability_profile", "") or Panel.CapabilityProfile.LEGACY_SINGLE_NODE


def is_modern_panel(panel):
    return panel_profile(panel) in MODERN_PROFILES


def order_is_renewal(order):
    return bool((order.metadata or {}).get("renewal_client_pk"))


def order_is_admin_direct(order):
    metadata = order.metadata or {}
    return bool(metadata.get("admin_direct_purchase") or metadata.get("free_grant") or order.payment_method == Order.PaymentMethod.ADMIN_FREE)


def resolve_provisioning_strategy(panel, order_type="paid_purchase", payment_state="pending"):
    if order_type == "renewal":
        return STRATEGY_RENEW_EXISTING_CLIENT
    if order_type in {"free_trial", "admin_direct_paid_grant", "admin_direct_purchase"}:
        return STRATEGY_DIRECT_CREATE_ENABLED if is_modern_panel(panel) else STRATEGY_LEGACY_PRECREATE_INACTIVE
    if is_modern_panel(panel):
        return STRATEGY_DEFERRED_CREATE_ENABLED
    return STRATEGY_LEGACY_PRECREATE_INACTIVE


def resolve_order_provisioning_strategy(order):
    if order_is_renewal(order):
        return STRATEGY_RENEW_EXISTING_CLIENT
    if not order.inbound_id or not getattr(order.inbound, "panel_id", None):
        return STRATEGY_LEGACY_PRECREATE_INACTIVE
    if order_is_admin_direct(order):
        return resolve_provisioning_strategy(
            order.inbound.panel,
            order_type="admin_direct_purchase",
            payment_state="verified",
        )
    return resolve_provisioning_strategy(
        order.inbound.panel,
        order_type="paid_purchase",
        payment_state="verified" if order.is_paid else "pending",
    )


def provisioning_scope(inbound):
    panel = inbound.panel if inbound and inbound.panel_id else None
    return {
        "panel_id": getattr(panel, "pk", None),
        "panel_profile": panel_profile(panel) if panel else "",
        "inbound_pk": getattr(inbound, "pk", None),
        "xui_inbound_id": getattr(inbound, "inbound_id", None),
        "node_id": getattr(inbound, "xui_node_id", "") or "",
        "node_name": getattr(inbound, "xui_node_name", "") or "",
        "remote_key": inbound_remote_key(inbound, panel=panel) if inbound and panel else "",
    }


def _scope_value(scope, key):
    value = (scope or {}).get(key)
    return "" if value is None else str(value).strip()


def _scope_matches_inbound(scope, inbound):
    if not scope or not inbound:
        return False
    checks = (
        ("panel_id", getattr(inbound, "panel_id", None)),
        ("inbound_pk", getattr(inbound, "pk", None)),
        ("xui_inbound_id", getattr(inbound, "inbound_id", None)),
        ("node_id", getattr(inbound, "xui_node_id", "") or ""),
    )
    for key, current_value in checks:
        expected = _scope_value(scope, key)
        if expected and expected != str(current_value or "").strip():
            return False
    return True


def resolve_frozen_order_inbound(order):
    metadata = order.metadata or {}
    scope = metadata.get("provisioning_scope") or {}
    current_inbound = getattr(order, "inbound", None)
    if not scope:
        return current_inbound
    if current_inbound and _scope_matches_inbound(scope, current_inbound):
        return current_inbound

    inbound_pk = _scope_value(scope, "inbound_pk")
    if inbound_pk:
        inbound = Inbound.objects.select_related("panel").filter(pk=inbound_pk).first()
        if inbound and _scope_matches_inbound(scope, inbound):
            return inbound

    panel_id = _scope_value(scope, "panel_id")
    xui_inbound_id = _scope_value(scope, "xui_inbound_id")
    node_id = _scope_value(scope, "node_id")
    if panel_id and xui_inbound_id:
        matches = list(
            Inbound.objects.select_related("panel")
            .filter(panel_id=panel_id, inbound_id=xui_inbound_id, xui_node_id=node_id)
            .order_by("pk")[:2]
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise XUIError("frozen_provisioning_scope_ambiguous")

    raise XUIError("frozen_provisioning_scope_mismatch")


def resolve_frozen_order_inbounds(order):
    metadata = order.metadata or {}
    scopes = metadata.get("provisioning_scopes") or []
    if not metadata.get("multi_inbound_bundle") or not scopes:
        inbound = resolve_frozen_order_inbound(order)
        return [inbound] if inbound else []

    inbounds = []
    seen = set()
    for scope in scopes:
        inbound = None
        inbound_pk = _scope_value(scope, "inbound_pk")
        if inbound_pk:
            inbound = Inbound.objects.select_related("panel").filter(pk=inbound_pk).first()
        if not inbound or not _scope_matches_inbound(scope, inbound):
            panel_id = _scope_value(scope, "panel_id")
            xui_inbound_id = _scope_value(scope, "xui_inbound_id")
            node_id = _scope_value(scope, "node_id")
            matches = list(
                Inbound.objects.select_related("panel")
                .filter(panel_id=panel_id, inbound_id=xui_inbound_id, xui_node_id=node_id)
                .order_by("pk")[:2]
            )
            inbound = matches[0] if len(matches) == 1 else None
        if not inbound or not _scope_matches_inbound(scope, inbound):
            raise XUIError("frozen_multi_provisioning_scope_mismatch")
        if inbound.pk in seen:
            continue
        seen.add(inbound.pk)
        inbounds.append(inbound)

    if not inbounds:
        raise XUIError("frozen_multi_provisioning_scope_empty")
    panel_ids = {inbound.panel_id for inbound in inbounds}
    if len(panel_ids) > 1:
        raise XUIError("frozen_multi_provisioning_scope_cross_panel")
    panel = inbounds[0].panel
    if panel_profile(panel) != Panel.CapabilityProfile.MODERN_MULTI_NODE:
        raise XUIError("frozen_multi_provisioning_requires_modern_multi_node")
    return inbounds


def freeze_order_provisioning_metadata(order, inbound, *, strategy):
    metadata = dict(order.metadata or {})
    metadata["provisioning_strategy"] = strategy
    metadata.setdefault("provisioning_scope", provisioning_scope(inbound))
    return metadata


def order_identity(order, inbound, *, index=1):
    scope = provisioning_scope(inbound)
    base = "|".join(
        [
            "qasedak",
            "paid-provisioning",
            str(order.public_id),
            str(order.order_tracking_code),
            str(scope.get("panel_id") or ""),
            str(scope.get("node_id") or "local"),
            str(scope.get("xui_inbound_id") or ""),
            str(index),
        ]
    )
    client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, base))
    sub_id = hashlib.sha256(f"sub:{base}".encode("utf-8")).hexdigest()[:16]
    email_prefix = order.username or build_client_display_name(
        order.customer,
        order=order,
        preferred_name=order.sender_card_name,
        short_id=order.order_tracking_code,
        metadata=order.metadata,
    )
    email = build_xui_client_email(email_prefix, client_uuid)
    idempotency_key = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return {
        "uuid": client_uuid,
        "sub_id": sub_id,
        "email": email,
        "email_prefix": email_prefix,
        "idempotency_key": idempotency_key,
        "scope": scope,
    }


def multi_inbound_order_identity(order, inbounds, *, index=1):
    inbounds = list(inbounds or [])
    if not inbounds:
        raise XUIError("multi_inbound_identity_requires_inbounds")
    panel = inbounds[0].panel
    remote_keys = sorted(inbound_remote_key(inbound, panel=panel) for inbound in inbounds)
    base = "|".join(
        [
            "qasedak",
            "paid-multi-inbound-provisioning",
            str(order.public_id),
            str(order.order_tracking_code),
            str(getattr(panel, "pk", "") or ""),
            ",".join(remote_keys),
            str(index),
        ]
    )
    client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, base))
    sub_id = hashlib.sha256(f"sub:{base}".encode("utf-8")).hexdigest()[:16]
    email_prefix = order.username or build_client_display_name(
        order.customer,
        order=order,
        preferred_name=order.sender_card_name,
        short_id=order.order_tracking_code,
        metadata=order.metadata,
    )
    email = build_xui_client_email(email_prefix, client_uuid)
    idempotency_key = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return {
        "uuid": client_uuid,
        "sub_id": sub_id,
        "email": email,
        "email_prefix": email_prefix,
        "idempotency_key": idempotency_key,
        "scope": {
            "panel_id": getattr(panel, "pk", None),
            "bundle_remote_keys": remote_keys,
            "inbound_count": len(inbounds),
        },
    }


def _safe_error(exc, panel=None):
    return sanitize_xui_operational_text(exc, panel=panel, max_length=700)


def _json_safe_value(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _mark_provisioning_state(order, status, *, error="", provisioned_at=None, idempotency_key="", increment=False):
    order.provisioning_status = status
    if increment:
        order.provisioning_attempts = int(order.provisioning_attempts or 0) + 1
    if error or status != Order.ProvisioningStatus.FAILED:
        order.last_provisioning_error = error
    if provisioned_at is not None:
        order.provisioned_at = provisioned_at
    if idempotency_key:
        order.provisioning_idempotency_key = idempotency_key


def _finish_failed_modern_order(order, *, safe_error, idempotency_key="", strategy=""):
    metadata = dict(order.metadata or {})
    metadata["panel_provisioning_deferred"] = True
    metadata["panel_provisioning_last_failed_at"] = timezone.now().isoformat()
    metadata["panel_provisioning_reason"] = safe_error or "provisioning_failed"
    if strategy:
        metadata["provisioning_strategy"] = strategy
    order.metadata = metadata
    order.is_paid = True
    order.verification_status = Order.VerificationStatus.VERIFIED
    if order.status != Order.Status.COMPLETED:
        order.status = Order.Status.CONFIRMED
    _mark_provisioning_state(
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


def _provisioning_block_reason(order):
    if order.status in {Order.Status.REJECTED, Order.Status.CANCELLED}:
        return "order_status_not_provisionable"
    if order.verification_status == Order.VerificationStatus.REJECTED:
        return "payment_rejected"
    if order.status == Order.Status.COMPLETED:
        return "completed_order_not_verified"
    return ""


def _remote_candidate_values(identity):
    return {
        str(identity.get("uuid") or "").strip(),
        str(identity.get("email") or "").strip(),
        str(identity.get("sub_id") or "").strip(),
    } - {""}


def _matches_remote_client(data, values):
    matches = []
    inbound_data = data or {}
    seen = set()
    for value in values:
        target_client, target_stats, matched_field, _clients, _stats = find_xui_client_and_stats(inbound_data, value)
        if not target_client and not target_stats:
            continue
        client = target_client or {}
        stats = target_stats or {}
        key = (
            str(client.get("id") or stats.get("id") or "").strip(),
            str(client.get("email") or stats.get("email") or "").strip(),
            str(client.get("subId") or client.get("sub_id") or stats.get("subId") or stats.get("sub_id") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        matches.append({"client": client, "stats": stats, "matched_field": matched_field})
    return matches


def _client_result_from_remote(service, inbound, inbound_data, identity, match):
    client = dict(match.get("client") or {})
    stats = dict(match.get("stats") or {})
    client_uuid = str(client.get("id") or stats.get("id") or identity["uuid"]).strip()
    email = str(client.get("email") or stats.get("email") or identity["email"]).strip()
    sub_id = str(client.get("subId") or client.get("sub_id") or stats.get("subId") or identity["sub_id"]).strip()
    enabled_value = first_xui_value(client.get("enable"), stats.get("enable"))
    if enabled_value is not None and not xui_bool(enabled_value):
        raise XUIError("Remote client exists but is not enabled.")
    hosts = service.get_hosts_for_inbound(inbound.inbound_id)
    direct_link = service.build_direct_link(
        inbound=inbound,
        inbound_data=inbound_data,
        client_uuid=client_uuid,
        client_data={**client, "email": email, "subId": sub_id},
        email=email,
        hosts=hosts,
    )
    sub_link = f"{service.build_sub_base_url(inbound_data)}/sub/{sub_id}" if sub_id else ""
    return {
        "uuid": client_uuid,
        "email": email,
        "sub_id": sub_id,
        "sub_link": sub_link,
        "direct_link": direct_link,
        "expires_at": parse_xui_datetime(first_xui_value(client.get("expiryTime"), stats.get("expiryTime"))),
        "xui_node_id": getattr(inbound, "xui_node_id", "") or "",
        "remote_client_key": build_remote_client_key(service.panel, inbound, client_uuid),
        "remote_scope": identity["scope"],
        "raw": {**client, **({"clientStats": stats} if stats else {})},
    }


def lookup_existing_remote_client(panel, inbound, identity):
    service = XUIService(panel)
    inbound_data = service.get_inbound(inbound, use_cache=False)
    matches = _matches_remote_client(inbound_data, _remote_candidate_values(identity))
    if len(matches) > 1:
        raise XUIError("ambiguous_remote_client")
    if not matches:
        return None
    return _client_result_from_remote(service, inbound, inbound_data, identity, matches[0])


def _local_client_matches(order, inbound, client_result):
    identifiers = {
        str(client_result.get("uuid") or "").strip(),
        str(client_result.get("email") or "").strip(),
        str(client_result.get("remote_client_key") or "").strip(),
    } - {""}
    query = Q(order=order, inbound=inbound)
    identifier_query = Q()
    for value in identifiers:
        identifier_query |= Q(uuid__iexact=value) | Q(xui_email__iexact=value) | Q(remote_client_key__iexact=value)
    if not identifier_query:
        return VPNClient.objects.none()
    return VPNClient.objects.select_for_update().filter(query & identifier_query).exclude(status=VPNClient.Status.DELETED)


def _upsert_local_client(order, inbound, client_result):
    from . import order_actions as order_actions_module

    matches = list(_local_client_matches(order, inbound, client_result).order_by("created_at", "pk")[:3])
    if len(matches) > 1:
        raise XUIError("ambiguous_local_client")
    created = False
    if matches:
        vpn_client = matches[0]
    else:
        vpn_client = order_actions_module.create_order_vpn_client(
            order,
            inbound=inbound,
            username=client_result.get("email") or order.username,
            client_result=client_result,
            status=VPNClient.Status.ACTIVE,
        )
        created = True
        Inbound.objects.filter(pk=inbound.pk).update(current_users=F("current_users") + 1, updated_at=timezone.now())

    vpn_client.store = order.store
    vpn_client.plan = order.plan
    vpn_client.username = vpn_client.username or client_result.get("email") or order.username
    vpn_client.xui_email = client_result.get("email") or vpn_client.xui_email
    vpn_client.uuid = client_result.get("uuid") or vpn_client.uuid
    vpn_client.sub_id = client_result.get("sub_id") or vpn_client.sub_id
    vpn_client.sub_link = client_result.get("sub_link") or vpn_client.sub_link
    vpn_client.direct_link = client_result.get("direct_link") or vpn_client.direct_link
    vpn_client.status = VPNClient.Status.ACTIVE
    vpn_client.traffic_limit_bytes = order.plan.traffic_limit_bytes
    vpn_client.duration_days = order.plan.duration_days
    vpn_client.device_limit = order.plan.device_limit
    vpn_client.xui_node_id = client_result.get("xui_node_id") or getattr(inbound, "xui_node_id", "") or ""
    vpn_client.remote_client_key = client_result.get("remote_client_key") or build_remote_client_key(
        inbound.panel,
        inbound,
        vpn_client.uuid,
    )
    vpn_client.xui_raw = _json_safe_value(client_result.get("raw", vpn_client.xui_raw))
    vpn_client.mark_active(duration_days=order.plan.duration_days)
    if client_result.get("expires_at"):
        vpn_client.expires_at = client_result["expires_at"]
    vpn_client.save()
    return vpn_client, created


def _activate_modern_enabled_order_locked(order, *, actor=None, strategy=STRATEGY_DEFERRED_CREATE_ENABLED):
    from . import order_actions as order_actions_module

    try:
        inbounds = resolve_frozen_order_inbounds(order)
        inbound = inbounds[0] if inbounds else None
    except Exception as exc:
        panel = getattr(getattr(order, "inbound", None), "panel", None)
        safe_error = _safe_error(exc, panel=panel)
        _finish_failed_modern_order(order, safe_error=safe_error, strategy=strategy)
        return ProvisioningResult(
            False,
            "scope ذخیره‌شده سفارش با route فعلی سازگار نیست. provisioning برای جلوگیری از ساخت اشتباه متوقف شد.",
            order_status=order.status,
            provisioning_status=order.provisioning_status,
            safe_error=safe_error,
        )
    if not inbound or not inbound.panel_id:
        return ProvisioningResult(False, "سفارش route دقیق panel/inbound ندارد.", safe_error="missing_scope")
    panel = inbound.panel
    multi_bundle = bool((order.metadata or {}).get("multi_inbound_bundle") and len(inbounds) > 1)
    identity = multi_inbound_order_identity(order, inbounds) if multi_bundle else order_identity(order, inbound)
    metadata = freeze_order_provisioning_metadata(order, inbound, strategy=strategy)
    if multi_bundle:
        metadata["multi_inbound_bundle"] = True
        metadata["provisioning_scopes"] = [provisioning_scope(bundle_inbound) for bundle_inbound in inbounds]
        metadata["bundle_inbound_pks"] = [bundle_inbound.pk for bundle_inbound in inbounds]
        metadata["bundle_inbound_ids"] = [bundle_inbound.inbound_id for bundle_inbound in inbounds]
    order.inbound = inbound
    order.metadata = metadata
    order.is_paid = True
    order.verification_status = Order.VerificationStatus.VERIFIED
    order.status = Order.Status.CONFIRMED
    _mark_provisioning_state(
        order,
        Order.ProvisioningStatus.PROVISIONING,
        idempotency_key=identity["idempotency_key"],
        increment=True,
    )
    order.save(
        update_fields=[
            "is_paid",
            "verification_status",
            "status",
            "inbound",
            "metadata",
            "provisioning_status",
            "provisioning_attempts",
            "provisioning_idempotency_key",
            "updated_at",
        ]
    )

    try:
        clients = []
        reused_any_remote = False
        for index in range(1, order_actions_module.required_client_count(order) + 1):
            indexed_identity = (
                identity
                if index == 1
                else multi_inbound_order_identity(order, inbounds, index=index)
                if multi_bundle
                else order_identity(order, inbound, index=index)
            )
            if multi_bundle:
                existing_results = [
                    lookup_existing_remote_client(panel, bundle_inbound, indexed_identity)
                    for bundle_inbound in inbounds
                ]
                existing_count = sum(1 for item in existing_results if item)
                if existing_count and existing_count != len(inbounds):
                    raise XUIError("partial_multi_inbound_remote_client_exists")
                already_remote = existing_count == len(inbounds)
                if already_remote:
                    per_inbound_results = existing_results
                else:
                    client_result = create_enabled_multi_inbound_client_details(
                        email_prefix=indexed_identity["email_prefix"],
                        total_gb=order.plan.volume_gb,
                        duration_days=order.plan.duration_days,
                        panel=panel,
                        inbounds=inbounds,
                        limit_ip=order.plan.device_limit,
                        client_uuid=indexed_identity["uuid"],
                        sub_id=indexed_identity["sub_id"],
                        email=indexed_identity["email"],
                    )
                    if not client_result:
                        raise XUIError("remote_multi_inbound_create_failed")
                    per_inbound_results = client_result.get("bundle_inbound_results") or []
                if len(per_inbound_results) != len(inbounds):
                    raise XUIError("remote_multi_inbound_verify_failed")
                verified_results = []
                for bundle_inbound, client_result in zip(inbounds, per_inbound_results):
                    verified = lookup_existing_remote_client(panel, bundle_inbound, indexed_identity)
                    if not verified:
                        raise XUIError("remote_multi_inbound_verify_failed")
                    verified_results.append({**client_result, **verified})
                primary_result = {
                    **verified_results[0],
                    "bundle_inbound_results": verified_results,
                    "raw": {
                        **(verified_results[0].get("raw") or {}),
                        "bundle_inbound_results": _json_safe_value(verified_results),
                        "bundle_inbound_pks": [bundle_inbound.pk for bundle_inbound in inbounds],
                    },
                }
                vpn_client, _created = _upsert_local_client(order, inbound, primary_result)
                clients.append(vpn_client)
                reused_any_remote = reused_any_remote or already_remote
                continue

            client_result = lookup_existing_remote_client(panel, inbound, indexed_identity)
            already_remote = bool(client_result)
            if not client_result:
                client_result = create_enabled_client_details(
                    email_prefix=indexed_identity["email_prefix"],
                    total_gb=order.plan.volume_gb,
                    duration_days=order.plan.duration_days,
                    panel=panel,
                    inbound=inbound,
                    limit_ip=order.plan.device_limit,
                    client_uuid=indexed_identity["uuid"],
                    sub_id=indexed_identity["sub_id"],
                    email=indexed_identity["email"],
                )
                already_remote = False
            if not client_result:
                raise XUIError("remote_create_failed")
            verified = lookup_existing_remote_client(panel, inbound, indexed_identity)
            if not verified:
                raise XUIError("remote_verify_failed")
            client_result = {**client_result, **verified}
            vpn_client, _created = _upsert_local_client(order, inbound, client_result)
            clients.append(vpn_client)
            reused_any_remote = reused_any_remote or already_remote
    except Exception as exc:
        safe_error = _safe_error(exc, panel=panel)
        logger.warning(
            "Modern paid provisioning failed order_id=%s tracking=%s panel=%s inbound=%s node=%s error=%s",
            order.pk,
            order.order_tracking_code,
            panel.pk,
            inbound.pk,
            identity["scope"].get("node_id") or "local",
            safe_error,
        )
        _finish_failed_modern_order(
            order,
            safe_error=safe_error,
            idempotency_key=identity["idempotency_key"],
            strategy=strategy,
        )
        return ProvisioningResult(
            False,
            "ساخت و تایید کانفیگ روی پنل X-UI ناموفق بود. سفارش تکمیل نشد و قابل retry است.",
            order_status=order.status,
            provisioning_status=order.provisioning_status,
            safe_error=safe_error,
        )

    primary_client = clients[0] if clients else None
    order.uuid = primary_client.uuid if primary_client else order.uuid
    order.sub_link = primary_client.sub_link if primary_client else order.sub_link
    order.direct_link = primary_client.direct_link if primary_client else order.direct_link
    order.username = order.username or (primary_client.username if primary_client else "")
    order.mark_payment_verified(user=actor)
    metadata = dict(order.metadata or {})
    metadata["panel_provisioning_deferred"] = False
    metadata["panel_provisioning_reason"] = ""
    metadata["panel_provisioned_at"] = timezone.now().isoformat()
    metadata["remote_reused_on_retry"] = bool(reused_any_remote)
    order.metadata = metadata
    _mark_provisioning_state(
        order,
        Order.ProvisioningStatus.PROVISIONED,
        error="",
        provisioned_at=timezone.now(),
        idempotency_key=identity["idempotency_key"],
    )
    order.save(
        update_fields=[
            "uuid",
            "sub_link",
            "direct_link",
            "username",
            "inbound",
            "is_paid",
            "verification_status",
            "verified_by",
            "verified_at",
            "status",
            "metadata",
            "provisioning_status",
            "last_provisioning_error",
            "provisioned_at",
            "provisioning_idempotency_key",
            "updated_at",
        ]
    )
    return ProvisioningResult(
        True,
        "سفارش تایید شد و کانفیگ VPN فعال شد.",
        already_provisioned=False,
        order_status=order.status,
        provisioning_status=order.provisioning_status,
        client=primary_client,
    )


def _activate_legacy_order_locked(order, *, actor=None):
    from . import order_actions as order_actions_module

    order_actions_module.ensure_legacy_order_client(order)
    missing_count = order_actions_module.required_client_count(order) - order.vpn_clients.count()
    if missing_count > 0:
        logger.info(
            "activate_order provisioning missing panel clients order_id=%s tracking=%s missing=%s",
            order.pk,
            order.order_tracking_code,
            missing_count,
        )
        provision_result = order_actions_module.provision_missing_panel_client(order)
        if not provision_result.success:
            logger.warning(
                "activate_order failed provisioning order_id=%s tracking=%s message=%s",
                order.pk,
                order.order_tracking_code,
                provision_result.message,
            )
            return ProvisioningResult(False, provision_result.message)

    clients = list(
        select_for_update_self(order.vpn_clients.select_related("plan", "inbound", "inbound__panel")).order_by(
            "created_at",
            "pk",
        )
    )
    if len(clients) < order_actions_module.required_client_count(order):
        return ProvisioningResult(False, "همه کانفیگ‌های VPN هنوز آماده نیستند. فعال‌سازی را دوباره امتحان کن.")

    for vpn_client in clients:
        if vpn_client.status == VPNClient.Status.ACTIVE:
            continue
        panel_error = order_actions_module.vpn_client_panel_error(vpn_client)
        if panel_error:
            order_actions_module.log_panel_link_error(
                "activate_order invalid VPN client panel link",
                order=order,
                vpn_client=vpn_client,
                reason=panel_error,
            )
            return ProvisioningResult(False, order_actions_module.PANEL_LINK_ADMIN_MESSAGE)
        logger.info(
            "Calling 3xUI enable_client order_id=%s tracking=%s client_id=%s uuid=%s",
            order.pk,
            order.order_tracking_code,
            vpn_client.pk,
            order_actions_module.mask_xui_value(vpn_client.uuid),
        )
        if not order_actions_module.enable_client(vpn_client):
            logger.warning(
                "3xUI enable_client failed order_id=%s tracking=%s client_id=%s uuid=%s",
                order.pk,
                order.order_tracking_code,
                vpn_client.pk,
                order_actions_module.mask_xui_value(vpn_client.uuid),
            )
            return ProvisioningResult(False, "فعال‌سازی روی پنل X-UI ناموفق بود.")

    order.mark_payment_verified(user=actor)
    changed_fields = order_actions_module.sync_order_primary_client_fields(order, clients[0] if clients else None)
    metadata = dict(order.metadata or {})
    if order.inbound_id:
        metadata = freeze_order_provisioning_metadata(
            order,
            order.inbound,
            strategy=STRATEGY_LEGACY_PRECREATE_INACTIVE,
        )
    order.metadata = metadata
    _mark_provisioning_state(order, Order.ProvisioningStatus.PROVISIONED, error="", provisioned_at=timezone.now())
    order.save(
        update_fields=[
            "is_paid",
            "verification_status",
            "verified_by",
            "verified_at",
            "status",
            *changed_fields,
            "metadata",
            "provisioning_status",
            "last_provisioning_error",
            "provisioned_at",
            "updated_at",
        ]
    )

    for vpn_client in clients:
        vpn_client.mark_active(duration_days=order.plan.duration_days)
        vpn_client.save(
            update_fields=[
                "status",
                "activated_at",
                "expires_at",
                "disabled_at",
                "updated_at",
            ]
        )
    return ProvisioningResult(True, "سفارش تایید شد و کانفیگ VPN فعال شد.", client=clients[0] if clients else None)


def _activate_renewal_order_locked(order, *, actor=None):
    from . import order_actions as order_actions_module

    result = order_actions_module.activate_renewal_order(order, user=actor)
    order.refresh_from_db()
    if result.success:
        _mark_provisioning_state(order, Order.ProvisioningStatus.PROVISIONED, error="", provisioned_at=timezone.now())
        metadata = dict(order.metadata or {})
        metadata["provisioning_strategy"] = STRATEGY_RENEW_EXISTING_CLIENT
        order.metadata = metadata
        order.save(
            update_fields=[
                "metadata",
                "provisioning_status",
                "last_provisioning_error",
                "provisioned_at",
                "updated_at",
            ]
        )
    return ProvisioningResult(result.success, result.message, order_status=order.status, provisioning_status=order.provisioning_status)


def approve_and_provision_order(order, actor=None, source=None, notify=True):
    logger.info("approve_and_provision_order started order_id=%s tracking=%s source=%s", order.pk, order.order_tracking_code, source or "")
    with transaction.atomic():
        order = (
            select_for_update_self(Order.objects.select_related("plan", "store", "inbound", "inbound__panel"))
            .get(pk=order.pk)
        )
        if order.status == Order.Status.COMPLETED and order.verification_status == Order.VerificationStatus.VERIFIED:
            return ProvisioningResult(
                True,
                "سفارش قبلاً تکمیل شده است.",
                already_provisioned=True,
                order_status=order.status,
                provisioning_status=order.provisioning_status,
            )
        block_reason = _provisioning_block_reason(order)
        if block_reason:
            logger.warning(
                "approve_and_provision_order refused order_id=%s tracking=%s status=%s verification=%s reason=%s",
                order.pk,
                order.order_tracking_code,
                order.status,
                order.verification_status,
                block_reason,
            )
            return ProvisioningResult(
                False,
                "این سفارش در وضعیت قابل تایید یا فعال‌سازی نیست.",
                order_status=order.status,
                provisioning_status=order.provisioning_status,
                safe_error=block_reason,
            )

        strategy = resolve_order_provisioning_strategy(order)
        if strategy == STRATEGY_RENEW_EXISTING_CLIENT:
            result = _activate_renewal_order_locked(order, actor=actor)
        elif strategy in {STRATEGY_DEFERRED_CREATE_ENABLED, STRATEGY_DIRECT_CREATE_ENABLED} and order.inbound_id and is_modern_panel(order.inbound.panel):
            result = _activate_modern_enabled_order_locked(order, actor=actor, strategy=strategy)
        else:
            result = _activate_legacy_order_locked(order, actor=actor)

    if result.ok:
        try:
            create_referral_reward_for_order(order)
        except Exception:
            logger.exception("Could not create referral GB reward for order_id=%s", order.pk)

        if notify and not result.already_provisioned:
            from .telegram_bot.notifications import notify_order_event

            notify_order_event(order, event_type="approved")
            result.delivery_attempted = True

    logger.info(
        "approve_and_provision_order finished order_id=%s tracking=%s ok=%s provisioning=%s",
        order.pk,
        order.order_tracking_code,
        result.ok,
        result.provisioning_status,
    )
    return result
