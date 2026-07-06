import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import requests
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .config_lookup import mask_identifier
from .models import Inbound, VPNClient, VPNClientActionLog
from .vpn_client_management_services import (
    mark_local_vpn_client_deleted,
    sanitize_audit_metadata,
)
from .xui_api import (
    XUIError,
    XUIService,
    first_xui_value,
    lookup_values,
    parse_xui_client_stats,
    parse_xui_json_object,
    related_client_stats,
    sanitize_xui_operational_text,
    xui_bool,
)
from .xui_compat import assert_precise_client_scope, build_remote_client_key, inbound_remote_key
from .xui_compat.errors import XUIAmbiguousScopeError, XUIUnknownSafeModeError
from .xui_compat.normalizers import normalize_client


FRESH_RESULT_TTL = timedelta(minutes=30)
REMOTE_STATUS_LABELS = {
    VPNClient.RemoteCheckStatus.NOT_CHECKED: "بررسی نشده",
    VPNClient.RemoteCheckStatus.REMOTE_ACTIVE: "فعال در پنل",
    VPNClient.RemoteCheckStatus.REMOTE_DISABLED: "غیرفعال در پنل",
    VPNClient.RemoteCheckStatus.REMOTE_MISSING: "حذف‌شده از پنل",
    VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE: "پنل در دسترس نیست",
    VPNClient.RemoteCheckStatus.INBOUND_MISSING: "اینباند پیدا نشد",
    VPNClient.RemoteCheckStatus.AMBIGUOUS: "تطبیق مبهم",
    VPNClient.RemoteCheckStatus.UNKNOWN: "نامشخص",
}
REMOTE_STATUS_TONES = {
    VPNClient.RemoteCheckStatus.NOT_CHECKED: "secondary",
    VPNClient.RemoteCheckStatus.REMOTE_ACTIVE: "success",
    VPNClient.RemoteCheckStatus.REMOTE_DISABLED: "warning",
    VPNClient.RemoteCheckStatus.REMOTE_MISSING: "danger",
    VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE: "danger",
    VPNClient.RemoteCheckStatus.INBOUND_MISSING: "warning",
    VPNClient.RemoteCheckStatus.AMBIGUOUS: "warning",
    VPNClient.RemoteCheckStatus.UNKNOWN: "secondary",
}
ERROR_STATUSES = {
    VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE,
    VPNClient.RemoteCheckStatus.INBOUND_MISSING,
    VPNClient.RemoteCheckStatus.UNKNOWN,
}


@dataclass(frozen=True)
class ScopeKey:
    panel_id: int
    node_id: str
    inbound_id: int
    inbound_pk: int


@dataclass
class ScopeFetchResult:
    status: str
    remote_clients: list[dict] = field(default_factory=list)
    error: str = ""
    complete: bool = False


@dataclass
class ReconciliationResult:
    batch_id: str
    checked: int = 0
    updated: int = 0
    api_calls: int = 0
    scopes: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add_status(self, status):
        self.statuses[status] = self.statuses.get(status, 0) + 1


def remote_status_label(status):
    return REMOTE_STATUS_LABELS.get(status or VPNClient.RemoteCheckStatus.NOT_CHECKED, "نامشخص")


def remote_status_tone(status):
    return REMOTE_STATUS_TONES.get(status or VPNClient.RemoteCheckStatus.NOT_CHECKED, "secondary")


def remote_status_is_fresh(vpn_client, now=None):
    checked_at = getattr(vpn_client, "last_remote_check_at", None)
    if not checked_at:
        return False
    now = now or timezone.now()
    return checked_at >= now - FRESH_RESULT_TTL


def safe_remote_scope(vpn_client):
    inbound = getattr(vpn_client, "inbound", None)
    panel = getattr(inbound, "panel", None) if inbound else None
    if not inbound or not panel:
        return {}
    node_id = (getattr(vpn_client, "xui_node_id", "") or getattr(inbound, "xui_node_id", "") or "").strip()
    return {
        "panel_id": panel.pk,
        "inbound_pk": inbound.pk,
        "inbound_id": inbound.inbound_id,
        "node_id": node_id,
        "remote_scope_key": inbound_remote_key(inbound, panel=panel),
    }


def remote_scope_matches_latest_check(vpn_client):
    return bool(safe_remote_scope(vpn_client)) and dict(vpn_client.last_remote_check_scope or {}) == safe_remote_scope(vpn_client)


def remote_status_is_cleanup_eligible(vpn_client, now=None):
    return (
        vpn_client.last_remote_check_status == VPNClient.RemoteCheckStatus.REMOTE_MISSING
        and remote_status_is_fresh(vpn_client, now=now)
        and remote_scope_matches_latest_check(vpn_client)
    )


def build_reconciliation_candidates(queryset):
    return list(
        queryset.select_related("store", "order", "order__customer", "inbound", "inbound__panel")
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
        .order_by("inbound__panel_id", "inbound__xui_node_id", "inbound__inbound_id", "pk")
    )


def _scope_key_for_client(vpn_client):
    inbound = getattr(vpn_client, "inbound", None)
    panel = getattr(inbound, "panel", None) if inbound else None
    if not inbound or not panel or not getattr(inbound, "inbound_id", None):
        return None
    node_id = (getattr(vpn_client, "xui_node_id", "") or getattr(inbound, "xui_node_id", "") or "").strip()
    return ScopeKey(panel.pk, node_id, int(inbound.inbound_id), inbound.pk)


def group_clients_by_remote_scope(clients):
    grouped = {}
    missing_scope = []
    for client in clients:
        key = _scope_key_for_client(client)
        if key is None:
            missing_scope.append(client)
            continue
        grouped.setdefault(key, []).append(client)
    return grouped, missing_scope


def _safe_error(exc, *, panel=None):
    return sanitize_xui_operational_text(exc, panel=panel, max_length=280)


def _status_from_exception(exc, *, panel=None):
    if isinstance(exc, XUIAmbiguousScopeError):
        return VPNClient.RemoteCheckStatus.AMBIGUOUS, _safe_error(exc, panel=panel)
    if isinstance(exc, XUIUnknownSafeModeError):
        return VPNClient.RemoteCheckStatus.UNKNOWN, _safe_error(exc, panel=panel)
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ProxyError)):
        return VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE, "Panel/network connection failed."
    text = str(exc or "").lower()
    if "not found" in text or "record not found" in text or "404" in text:
        return VPNClient.RemoteCheckStatus.INBOUND_MISSING, _safe_error(exc, panel=panel)
    if "timeout" in text or "connection" in text or "proxy" in text or "login" in text or "auth" in text:
        return VPNClient.RemoteCheckStatus.PANEL_UNREACHABLE, _safe_error(exc, panel=panel)
    return VPNClient.RemoteCheckStatus.UNKNOWN, _safe_error(exc, panel=panel)


def _remote_values_from_sources(*sources):
    values = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        values.update(str(value).strip() for value in lookup_values(source) if str(value).strip())
        for key in ("remark", "name"):
            value = str(source.get(key) or "").strip()
            if value:
                values.add(value)
    return values


def _remote_keys_for_values(panel, inbound, values):
    return {
        build_remote_client_key(panel, inbound, value)
        for value in values
        if str(value or "").strip()
    }


def _normalize_remote_clients(panel, inbound, inbound_data):
    settings = parse_xui_json_object((inbound_data or {}).get("settings"))
    if "settings" in (inbound_data or {}) and not isinstance(settings, dict):
        return [], False
    clients = settings.get("clients") or []
    if not isinstance(clients, list):
        return [], False
    client_stats = parse_xui_client_stats(inbound_data)
    rows = []
    consumed_stats = set()

    for index, client in enumerate(clients):
        if not isinstance(client, dict):
            continue
        stats = related_client_stats(client_stats, client)
        if stats:
            for stats_index, candidate in enumerate(client_stats):
                if candidate is stats:
                    consumed_stats.add(stats_index)
                    break
        normalized = normalize_client(
            client,
            context={
                "panel_id": panel.pk,
                "node_external_id": getattr(inbound, "xui_node_id", "") or "",
                "inbound_external_id": inbound.inbound_id,
                "source": "inbound_clients",
            },
        )
        values = _remote_values_from_sources(client, stats, {"id": normalized.remote_client_id})
        rows.append(
            {
                "index": f"client:{index}",
                "values": values,
                "values_lower": {value.lower() for value in values},
                "remote_keys": _remote_keys_for_values(panel, inbound, values),
                "enabled": normalized.enabled,
                "source": "client",
            }
        )

    for stats_index, stats in enumerate(client_stats):
        if stats_index in consumed_stats or not isinstance(stats, dict):
            continue
        normalized = normalize_client(
            stats,
            context={
                "panel_id": panel.pk,
                "node_external_id": getattr(inbound, "xui_node_id", "") or "",
                "inbound_external_id": inbound.inbound_id,
                "source": "client_stats",
            },
        )
        values = _remote_values_from_sources(stats, {"id": normalized.remote_client_id})
        rows.append(
            {
                "index": f"stats:{stats_index}",
                "values": values,
                "values_lower": {value.lower() for value in values},
                "remote_keys": _remote_keys_for_values(panel, inbound, values),
                "enabled": normalized.enabled,
                "source": "client_stats",
            }
        )
    return rows, True


def fetch_remote_scope_clients(panel, node, inbound):
    try:
        assert_precise_client_scope(panel, inbound, operation="read", allow_multi_scope=False)
        service = XUIService(panel)
        inbound_data = service.get_inbound(inbound, use_cache=False)
    except Exception as exc:
        status, error = _status_from_exception(exc, panel=panel)
        return ScopeFetchResult(status=status, error=error, complete=False)

    remote_clients, complete = _normalize_remote_clients(panel, inbound, inbound_data)
    if not complete:
        return ScopeFetchResult(
            status=VPNClient.RemoteCheckStatus.UNKNOWN,
            error="Panel response did not include a complete client list.",
            complete=False,
        )
    return ScopeFetchResult(
        status=VPNClient.RemoteCheckStatus.REMOTE_ACTIVE,
        remote_clients=remote_clients,
        complete=True,
    )


def _local_identity_values(vpn_client):
    values = set()
    for value in (vpn_client.uuid, vpn_client.xui_email, vpn_client.username, vpn_client.sub_id):
        value = str(value or "").strip()
        if value:
            values.add(value)
    return values


def match_local_client_to_remote(local_client, remote_clients):
    identities = _local_identity_values(local_client)
    remote_key = str(local_client.remote_client_key or "").strip()
    if not identities and not remote_key:
        return VPNClient.RemoteCheckStatus.UNKNOWN, "Local client has no safe remote identity."

    identity_lowers = {value.lower() for value in identities}
    matches = []
    for remote in remote_clients:
        if remote_key and remote_key in remote.get("remote_keys", set()):
            matches.append(remote)
            continue
        if identity_lowers.intersection(remote.get("values_lower", set())):
            matches.append(remote)

    if not matches:
        return VPNClient.RemoteCheckStatus.REMOTE_MISSING, ""
    unique = {match["index"]: match for match in matches}
    if len(unique) > 1:
        return VPNClient.RemoteCheckStatus.AMBIGUOUS, "Multiple remote clients matched the same local identity."
    remote = next(iter(unique.values()))
    enabled = remote.get("enabled")
    if enabled is False or first_xui_value(enabled) is False:
        return VPNClient.RemoteCheckStatus.REMOTE_DISABLED, ""
    if enabled is not None and not xui_bool(enabled):
        return VPNClient.RemoteCheckStatus.REMOTE_DISABLED, ""
    return VPNClient.RemoteCheckStatus.REMOTE_ACTIVE, ""


def _persist_check_result(vpn_client, *, status, batch_id, checked_at, error=""):
    VPNClient.objects.filter(pk=vpn_client.pk).update(
        last_remote_check_status=status,
        last_remote_check_at=checked_at,
        last_remote_check_error=str(error or "")[:300],
        last_remote_check_scope=safe_remote_scope(vpn_client),
        last_remote_check_batch_id=batch_id,
        updated_at=timezone.now(),
    )


def reconcile_vpn_clients(queryset, actor=None, *, persist=True):
    batch_id = uuid.uuid4().hex
    checked_at = timezone.now()
    result = ReconciliationResult(batch_id=batch_id)
    clients = build_reconciliation_candidates(queryset)
    grouped, missing_scope = group_clients_by_remote_scope(clients)

    for client in missing_scope:
        if persist:
            _persist_check_result(
                client,
                status=VPNClient.RemoteCheckStatus.UNKNOWN,
                batch_id=batch_id,
                checked_at=checked_at,
                error="Local client is missing exact panel/inbound scope.",
            )
        result.checked += 1
        result.updated += 1
        result.add_status(VPNClient.RemoteCheckStatus.UNKNOWN)

    for _key, scope_clients in grouped.items():
        representative = scope_clients[0]
        inbound = representative.inbound
        panel = inbound.panel
        scope_result = fetch_remote_scope_clients(panel, getattr(inbound, "xui_node_id", "") or "", inbound)
        result.scopes += 1
        result.api_calls += 1
        if scope_result.status != VPNClient.RemoteCheckStatus.REMOTE_ACTIVE or not scope_result.complete:
            for client in scope_clients:
                if persist:
                    _persist_check_result(
                        client,
                        status=scope_result.status,
                        batch_id=batch_id,
                        checked_at=checked_at,
                        error=scope_result.error,
                    )
                result.checked += 1
                result.updated += 1
                result.add_status(scope_result.status)
            if scope_result.error:
                result.errors.append(scope_result.error)
            continue

        for client in scope_clients:
            status, error = match_local_client_to_remote(client, scope_result.remote_clients)
            if persist:
                _persist_check_result(
                    client,
                    status=status,
                    batch_id=batch_id,
                    checked_at=checked_at,
                    error=error,
                )
            result.checked += 1
            result.updated += 1
            result.add_status(status)
    return result


def revalidate_missing_clients(client_ids, actor=None):
    queryset = VPNClient.objects.filter(pk__in=client_ids)
    return reconcile_vpn_clients(queryset, actor=actor)


def _client_customer(vpn_client):
    if vpn_client.order_id and getattr(vpn_client.order, "customer_id", None):
        return vpn_client.order.customer
    trial = vpn_client.free_trial_requests.select_related("customer").filter(customer__isnull=False).first()
    return trial.customer if trial else None


def _client_identifier(vpn_client):
    return str(vpn_client.uuid or vpn_client.xui_email or vpn_client.username or "").strip()


def _actor_label(actor):
    if not actor:
        return ""
    if isinstance(actor, str):
        return actor[:80]
    if getattr(actor, "pk", None):
        return f"web-admin:{actor.pk}"
    return str(actor)[:80]


def _create_soft_delete_log(vpn_client, actor=None):
    identifier = _client_identifier(vpn_client)
    return VPNClientActionLog.objects.create(
        vpn_client=vpn_client,
        customer=_client_customer(vpn_client),
        actor_type=VPNClientActionLog.ActorType.ADMIN if getattr(actor, "pk", None) else VPNClientActionLog.ActorType.SYSTEM,
        actor_telegram_id=_actor_label(actor),
        action=VPNClientActionLog.Action.ADMIN_SOFT_DELETE_REMOTE_MISSING,
        panel=vpn_client.inbound.panel if vpn_client.inbound_id and vpn_client.inbound.panel_id else None,
        inbound=vpn_client.inbound if vpn_client.inbound_id else None,
        xui_identifier_masked=mask_identifier(identifier),
        old_total_bytes=vpn_client.traffic_limit_bytes,
        old_expiry_time=vpn_client.expires_at,
        metadata=sanitize_audit_metadata(
            {
                "source": "service_reconciliation",
                "last_remote_check_status": vpn_client.last_remote_check_status,
                "last_remote_check_batch_id": vpn_client.last_remote_check_batch_id,
                "scope": safe_remote_scope(vpn_client),
            }
        ),
    )


def _complete_log(log, *, status, error_message="", metadata=None):
    log.status = status
    log.error_message = str(error_message or "")[:300]
    log.completed_at = timezone.now()
    if metadata:
        current = dict(log.metadata or {})
        current.update(sanitize_audit_metadata(metadata))
        log.metadata = current
    log.save(update_fields=["status", "error_message", "completed_at", "metadata", "updated_at"])


def soft_delete_remote_missing_clients(client_ids, actor=None):
    client_ids = [int(client_id) for client_id in client_ids if str(client_id).strip()]
    if not client_ids:
        return {"requested": 0, "deleted": 0, "blocked": 0, "revalidated": None, "errors": []}

    now = timezone.now()
    initial_clients = list(
        VPNClient.objects.select_related("store", "order", "order__customer", "inbound", "inbound__panel")
        .prefetch_related("free_trial_requests__customer")
        .filter(pk__in=client_ids)
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
    )
    eligible_ids = [client.pk for client in initial_clients if remote_status_is_cleanup_eligible(client, now=now)]
    initially_blocked = len(client_ids) - len(eligible_ids)
    if not eligible_ids:
        return {"requested": len(client_ids), "deleted": 0, "blocked": initially_blocked, "revalidated": None, "errors": []}

    revalidated = revalidate_missing_clients(eligible_ids, actor=actor)
    now = timezone.now()
    candidates = list(
        VPNClient.objects.select_related("store", "order", "order__customer", "inbound", "inbound__panel")
        .prefetch_related("free_trial_requests__customer")
        .filter(pk__in=eligible_ids)
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
    )
    deleted = 0
    blocked = initially_blocked
    errors = []
    for client in candidates:
        if not remote_status_is_cleanup_eligible(client, now=now):
            blocked += 1
            continue
        log = _create_soft_delete_log(client, actor=actor)
        try:
            with transaction.atomic():
                mark_local_vpn_client_deleted(
                    client,
                    admin_telegram_id=_actor_label(actor),
                    reason="remote_client_missing_confirmed",
                    remote_deleted_at=now,
                    remote_result={"matched_field": "reconciliation_zero_exact_match"},
                )
            _complete_log(log, status=VPNClientActionLog.Status.SUCCESS, metadata={"local_soft_deleted": True})
            deleted += 1
        except Exception as exc:
            blocked += 1
            safe_error = _safe_error(exc, panel=getattr(getattr(client, "inbound", None), "panel", None))
            _complete_log(log, status=VPNClientActionLog.Status.FAILED, error_message=safe_error)
            errors.append(safe_error)
    return {
        "requested": len(client_ids),
        "deleted": deleted,
        "blocked": blocked,
        "revalidated": revalidated,
        "errors": errors,
    }


def get_reconciliation_summary(queryset=None):
    queryset = queryset or VPNClient.objects.all()
    base = queryset.exclude(status=VPNClient.Status.DELETED).filter(deleted_at__isnull=True)
    status_counts = {
        row["last_remote_check_status"]: row["count"]
        for row in base.values("last_remote_check_status").annotate(count=Count("id"))
    }
    now = timezone.now()
    checked = base.exclude(last_remote_check_status=VPNClient.RemoteCheckStatus.NOT_CHECKED).filter(
        last_remote_check_at__isnull=False
    )
    stale_cutoff = now - FRESH_RESULT_TTL
    stale_count = checked.filter(last_remote_check_at__lt=stale_cutoff).count()
    remote_missing_fresh = [
        client.pk
        for client in checked.filter(last_remote_check_status=VPNClient.RemoteCheckStatus.REMOTE_MISSING)
        .select_related("inbound", "inbound__panel")
        if remote_status_is_cleanup_eligible(client, now=now)
    ]
    return {
        "statuses": {
            status: {
                "key": status,
                "label": remote_status_label(status),
                "tone": remote_status_tone(status),
                "count": status_counts.get(status, 0),
            }
            for status, _label in VPNClient.RemoteCheckStatus.choices
        },
        "remote_active": status_counts.get(VPNClient.RemoteCheckStatus.REMOTE_ACTIVE, 0),
        "remote_disabled": status_counts.get(VPNClient.RemoteCheckStatus.REMOTE_DISABLED, 0),
        "remote_missing": status_counts.get(VPNClient.RemoteCheckStatus.REMOTE_MISSING, 0),
        "unknown_or_error": sum(status_counts.get(status, 0) for status in ERROR_STATUSES),
        "ambiguous": status_counts.get(VPNClient.RemoteCheckStatus.AMBIGUOUS, 0),
        "checked": checked.count(),
        "not_checked": base.filter(
            Q(last_remote_check_status=VPNClient.RemoteCheckStatus.NOT_CHECKED)
            | Q(last_remote_check_at__isnull=True)
        ).count(),
        "stale": stale_count,
        "cleanup_candidate_ids": remote_missing_fresh,
        "cleanup_candidate_count": len(remote_missing_fresh),
        "latest_check_at": checked.order_by("-last_remote_check_at").values_list("last_remote_check_at", flat=True).first(),
        "fresh_ttl_minutes": int(FRESH_RESULT_TTL.total_seconds() // 60),
    }
