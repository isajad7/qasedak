import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .config_lookup import mask_identifier
from .jalali import format_jalali_datetime, persian_digits
from .models import Customer, FreeTrialRequest, Inbound, Order, Panel, VPNClient, VPNClientActionLog
from .xui_api import (
    XUIError,
    build_config_link_for_identifier,
    bytes_from_gb,
    delete_client_from_inbound,
    find_client_by_identifier,
    update_client_traffic_and_expiry,
)

logger = logging.getLogger(__name__)

CONFIG_LINK_RE = re.compile(r"\b(?:vless|vmess|trojan|ss)://\S+", re.IGNORECASE)
SUB_LINK_RE = re.compile(r"\bhttps?://[^\s<>'\"]*/sub/[^\s<>'\"]*", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
PENDING_RENEWAL_STATUSES = {
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
}
BYTES_PER_GB = Decimal(1024 ** 3)
ADMIN_ENABLE_ACTION = "admin_enable"
ADMIN_DISABLE_ACTION = "admin_disable"


class VPNClientManagementError(Exception):
    pass


class VPNClientPermissionError(VPNClientManagementError):
    pass


class VPNClientPendingRenewalError(VPNClientManagementError):
    pass


@dataclass
class LocalClientMatch:
    vpn_client: VPNClient | None = None
    status: str = "not_found"
    count: int = 0


def _safe_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VPNClientManagementError("عدد وارد شده معتبر نیست.") from exc


def gb_to_bytes(value):
    number = _safe_decimal(value)
    if number <= 0:
        raise VPNClientManagementError("عدد وارد شده باید مثبت باشد.")
    return int(number * BYTES_PER_GB)


def _safe_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise VPNClientManagementError("عدد وارد شده معتبر نیست.") from exc
    if number <= 0:
        raise VPNClientManagementError("عدد وارد شده باید مثبت باشد.")
    return number


def _format_gb_from_bytes(value):
    try:
        number = Decimal(int(value or 0)) / BYTES_PER_GB
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal("0")
    label = f"{number.quantize(Decimal('0.01')):f}".rstrip("0").rstrip(".")
    return persian_digits(label or "0")


def _mask_string_value(value):
    value = str(value or "")
    value = CONFIG_LINK_RE.sub("<config-link-redacted>", value)
    value = SUB_LINK_RE.sub("<config-link-redacted>", value)
    return UUID_RE.sub(lambda match: f"<identifier:{mask_identifier(match.group(0))}>", value)


def sanitize_audit_metadata(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if any(part in lowered for part in ("link", "config", "uuid", "password", "token")):
                sanitized[key_str] = _mask_string_value(item) if isinstance(item, str) else "<redacted>"
            else:
                sanitized[key_str] = sanitize_audit_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_audit_metadata(item) for item in value]
    if isinstance(value, str):
        return _mask_string_value(value)
    return value


def _client_customer(vpn_client):
    if vpn_client.order_id and vpn_client.order.customer_id:
        return vpn_client.order.customer
    trial = vpn_client.free_trial_requests.select_related("customer").filter(customer__isnull=False).first()
    return trial.customer if trial else None


def _client_identifier(vpn_client):
    return str(vpn_client.uuid or vpn_client.xui_email or vpn_client.username or "").strip()


def _create_action_log(
    *,
    vpn_client=None,
    customer=None,
    actor_type,
    actor_telegram_id="",
    action,
    panel=None,
    inbound=None,
    identifier="",
    old_total_bytes=None,
    new_total_bytes=None,
    old_expiry_time=None,
    new_expiry_time=None,
    metadata=None,
):
    return VPNClientActionLog.objects.create(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=actor_type,
        actor_telegram_id=str(actor_telegram_id or ""),
        action=action,
        panel=panel,
        inbound=inbound,
        xui_identifier_masked=mask_identifier(identifier),
        old_total_bytes=old_total_bytes,
        new_total_bytes=new_total_bytes,
        old_expiry_time=old_expiry_time,
        new_expiry_time=new_expiry_time,
        metadata=sanitize_audit_metadata(metadata or {}),
    )


def _complete_action_log(log, *, status, error_message="", metadata=None, **fields):
    log.status = status
    log.error_message = _mask_string_value(error_message or "")
    log.completed_at = timezone.now()
    if metadata:
        current = dict(log.metadata or {})
        current.update(sanitize_audit_metadata(metadata))
        log.metadata = current
    for field, value in fields.items():
        setattr(log, field, value)
    log.save(
        update_fields=[
            "status",
            "error_message",
            "completed_at",
            "metadata",
            *fields.keys(),
            "updated_at",
        ]
    )


def get_manageable_vpn_client_for_user(customer, vpn_client_id):
    if not customer:
        return None
    lookup = Q(public_id=vpn_client_id)
    try:
        lookup |= Q(pk=int(vpn_client_id))
    except (TypeError, ValueError):
        pass
    return (
        VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .filter(lookup)
        .filter(Q(order__customer=customer) | Q(free_trial_requests__customer=customer))
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
        .distinct()
        .first()
    )


def _pending_renewal_for_client(customer, vpn_client):
    if not customer or not vpn_client:
        return None
    return (
        Order.objects.filter(
            customer=customer,
            metadata__renewal_client_pk=vpn_client.pk,
            status__in=PENDING_RENEWAL_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )


def _require_user_can_manage(customer, vpn_client):
    if not customer or not vpn_client:
        raise VPNClientPermissionError("این کانفیگ پیدا نشد.")
    owner = _client_customer(vpn_client)
    if not owner or owner.pk != customer.pk:
        raise VPNClientPermissionError("شما اجازه مدیریت این کانفیگ را ندارید.")
    if vpn_client.is_deleted:
        raise VPNClientManagementError("این کانفیگ قبلا حذف شده است.")
    if _pending_renewal_for_client(customer, vpn_client):
        raise VPNClientPendingRenewalError("برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید وجود دارد.")


def cleanup_vpn_client_related_records(vpn_client, *, old_direct_link="", old_sub_link="", old_uuid=""):
    FreeTrialRequest.objects.filter(vpn_client=vpn_client).exclude(config_link="").update(
        config_link="",
        updated_at=timezone.now(),
    )
    if vpn_client.order_id:
        order = vpn_client.order
        changed = []
        if old_uuid and order.uuid == old_uuid:
            order.uuid = ""
            changed.append("uuid")
        if old_direct_link and order.direct_link == old_direct_link:
            order.direct_link = ""
            changed.append("direct_link")
        if old_sub_link and order.sub_link == old_sub_link:
            order.sub_link = ""
            changed.append("sub_link")
        if changed:
            metadata = dict(order.metadata or {})
            metadata["vpn_client_deleted"] = True
            metadata["deleted_vpn_client_id"] = vpn_client.pk
            order.metadata = metadata
            order.save(update_fields=[*changed, "metadata", "updated_at"])


def mark_local_vpn_client_deleted(
    vpn_client,
    *,
    customer=None,
    admin_telegram_id="",
    reason="",
    remote_deleted_at=None,
    remote_result=None,
):
    with transaction.atomic():
        locked = (
            VPNClient.objects.select_for_update()
            .select_related("order", "order__customer", "inbound", "inbound__panel")
            .get(pk=vpn_client.pk)
        )
        old_direct_link = locked.direct_link or ""
        old_sub_link = locked.sub_link or ""
        old_uuid = locked.uuid or ""
        locked.mark_deleted(
            customer=customer,
            admin_telegram_id=admin_telegram_id,
            reason=reason,
            remote_deleted_at=remote_deleted_at,
        )
        raw = dict(locked.xui_raw or {})
        raw["deleted"] = True
        raw["deleted_at"] = locked.deleted_at.isoformat() if locked.deleted_at else ""
        raw["remote_deleted_at"] = locked.remote_deleted_at.isoformat() if locked.remote_deleted_at else ""
        if remote_result:
            raw["delete_result"] = {
                "stats_deleted": bool(remote_result.get("stats_deleted")),
                "stats_remaining": bool(remote_result.get("stats_remaining")),
                "matched_field": remote_result.get("matched_field") or "",
            }
        locked.xui_raw = raw
        locked.save(
            update_fields=[
                "status",
                "deleted_at",
                "deleted_by_customer",
                "deleted_by_admin_telegram_id",
                "delete_reason",
                "remote_deleted_at",
                "disabled_at",
                "sub_link",
                "direct_link",
                "xui_raw",
                "updated_at",
            ]
        )
        cleanup_vpn_client_related_records(
            locked,
            old_direct_link=old_direct_link,
            old_sub_link=old_sub_link,
            old_uuid=old_uuid,
        )
    return locked


def delete_vpn_client_for_user(customer, vpn_client, reason="", actor_telegram_id=""):
    _require_user_can_manage(customer, vpn_client)
    if not vpn_client.inbound_id or not vpn_client.inbound.panel_id:
        raise VPNClientManagementError("این کانفیگ به پنل معتبر وصل نیست.")

    identifier = _client_identifier(vpn_client)
    if not identifier:
        raise VPNClientManagementError("شناسه قابل حذف برای این کانفیگ پیدا نشد.")

    log = _create_action_log(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=VPNClientActionLog.ActorType.USER,
        actor_telegram_id=actor_telegram_id,
        action=VPNClientActionLog.Action.USER_DELETE,
        panel=vpn_client.inbound.panel,
        inbound=vpn_client.inbound,
        identifier=identifier,
        old_total_bytes=vpn_client.traffic_limit_bytes,
        old_expiry_time=vpn_client.expires_at,
        metadata={"source": "bot_user_delete", "vpn_client_id": vpn_client.pk},
    )
    try:
        remote_result = delete_client_from_inbound(vpn_client.inbound.panel, vpn_client.inbound, identifier)
    except Exception as exc:
        logger.warning(
            "User VPN client delete failed customer=%s vpn_client=%s identifier=%s error=%s",
            customer.pk,
            vpn_client.pk,
            mask_identifier(identifier),
            exc,
        )
        _complete_action_log(log, status=VPNClientActionLog.Status.FAILED, error_message=str(exc))
        raise VPNClientManagementError("حذف کانفیگ انجام نشد.") from exc

    try:
        deleted_client = mark_local_vpn_client_deleted(
            vpn_client,
            customer=customer,
            reason=reason or "Deleted by user from bot",
            remote_deleted_at=timezone.now(),
            remote_result=remote_result,
        )
    except Exception as exc:
        logger.exception("Remote delete succeeded but local delete failed vpn_client=%s", vpn_client.pk)
        _complete_action_log(
            log,
            status=VPNClientActionLog.Status.FAILED,
            error_message=str(exc),
            metadata={"remote_deleted": True, "local_sync_failed": True},
        )
        raise

    _complete_action_log(
        log,
        status=VPNClientActionLog.Status.SUCCESS,
        metadata={
            "remote_deleted": True,
            "stats_deleted": bool(remote_result.get("stats_deleted")),
            "stats_remaining": bool(remote_result.get("stats_remaining")),
        },
    )
    return {"success": True, "vpn_client": deleted_client, "remote": remote_result, "audit_log": log}


def _lookup_panel_inbound(payload):
    panel = Panel.objects.filter(pk=payload.get("panel_id"), is_active=True).first()
    if not panel:
        raise VPNClientManagementError("پنل این کانفیگ در دسترس نیست.")
    inbound = Inbound.objects.filter(panel=panel, inbound_id=payload.get("inbound_id")).first()
    if not inbound:
        raise VPNClientManagementError("Inbound این کانفیگ پیدا نشد.")
    identifier = str(payload.get("identifier") or "").strip()
    if not identifier:
        raise VPNClientManagementError("شناسه کانفیگ پیدا نشد.")
    return panel, inbound, identifier


def _candidate_values(identifier, result=None):
    result = result or {}
    values = {str(identifier or "").strip()}
    for key in ("email", "remark"):
        value = str(result.get(key) or "").strip()
        if value:
            values.add(value)
    for source_key in ("client", "client_stats"):
        source = result.get(source_key) or {}
        if isinstance(source, dict):
            for key in ("id", "email", "password", "subId", "sub_id", "remark", "name"):
                value = str(source.get(key) or "").strip()
                if value:
                    values.add(value)
    return {value for value in values if value}


def find_local_vpn_client_for_lookup(panel, inbound, identifier, *, result=None, vpn_client_id=None):
    queryset = (
        VPNClient.objects.select_related("store", "order", "order__customer", "plan", "inbound", "inbound__panel")
        .filter(inbound=inbound)
        .exclude(status=VPNClient.Status.DELETED)
        .filter(deleted_at__isnull=True)
    )
    if vpn_client_id:
        client = queryset.filter(pk=vpn_client_id).first()
        return LocalClientMatch(client, "matched" if client else "not_found", 1 if client else 0)

    values = _candidate_values(identifier, result=result)
    lookup = Q()
    for value in values:
        lookup |= Q(uuid__iexact=value) | Q(xui_email__iexact=value) | Q(username__iexact=value) | Q(sub_id__iexact=value)
    if not lookup:
        return LocalClientMatch(None, "not_found", 0)
    matches = list(queryset.filter(lookup).distinct()[:2])
    if len(matches) == 1:
        return LocalClientMatch(matches[0], "matched", 1)
    if len(matches) > 1:
        return LocalClientMatch(None, "multiple", len(matches))
    return LocalClientMatch(None, "not_found", 0)


def delete_vpn_client_by_admin_lookup(admin_telegram_id, lookup_token_or_result, reason=""):
    payload = dict(lookup_token_or_result or {})
    panel, inbound, identifier = _lookup_panel_inbound(payload)
    local_match = find_local_vpn_client_for_lookup(
        panel,
        inbound,
        identifier,
        result=payload,
        vpn_client_id=payload.get("vpn_client_id"),
    )
    vpn_client = local_match.vpn_client
    customer = _client_customer(vpn_client) if vpn_client else None
    log = _create_action_log(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=VPNClientActionLog.ActorType.ADMIN,
        actor_telegram_id=admin_telegram_id,
        action=VPNClientActionLog.Action.ADMIN_DELETE,
        panel=panel,
        inbound=inbound,
        identifier=identifier,
        old_total_bytes=payload.get("total_bytes"),
        old_expiry_time=payload.get("expiry_time"),
        metadata={
            "source": "admin_lookup_delete",
            "local_match_status": local_match.status,
            "local_match_count": local_match.count,
        },
    )
    try:
        remote_result = delete_client_from_inbound(panel, inbound, identifier)
    except Exception as exc:
        logger.warning(
            "Admin VPN client delete failed admin=%s panel=%s inbound=%s identifier=%s error=%s",
            admin_telegram_id,
            panel.pk,
            inbound.inbound_id,
            mask_identifier(identifier),
            exc,
        )
        _complete_action_log(log, status=VPNClientActionLog.Status.FAILED, error_message=str(exc))
        raise VPNClientManagementError("حذف کانفیگ از پنل انجام نشد.") from exc

    if vpn_client:
        try:
            mark_local_vpn_client_deleted(
                vpn_client,
                admin_telegram_id=admin_telegram_id,
                reason=reason or "Deleted by admin lookup",
                remote_deleted_at=timezone.now(),
                remote_result=remote_result,
            )
        except Exception as exc:
            logger.exception("Remote admin delete succeeded but local delete failed vpn_client=%s", vpn_client.pk)
            _complete_action_log(
                log,
                status=VPNClientActionLog.Status.FAILED,
                error_message=str(exc),
                metadata={"remote_deleted": True, "local_sync_failed": True},
            )
            raise

    _complete_action_log(
        log,
        status=VPNClientActionLog.Status.SUCCESS,
        metadata={
            "remote_deleted": True,
            "local_updated": bool(vpn_client),
            "local_match_status": local_match.status,
            "stats_deleted": bool(remote_result.get("stats_deleted")),
            "stats_remaining": bool(remote_result.get("stats_remaining")),
        },
    )
    return {
        "success": True,
        "vpn_client": vpn_client,
        "remote": remote_result,
        "local_match_status": local_match.status,
        "audit_log": log,
    }


def _live_lookup(panel, identifier):
    result = find_client_by_identifier(panel, identifier)
    if not result:
        raise VPNClientManagementError("این کانفیگ روی پنل پیدا نشد.")
    return result


def sync_local_vpn_client_after_remote_update(vpn_client, remote_result):
    if not vpn_client:
        return None
    with transaction.atomic():
        locked = VPNClient.objects.select_for_update().get(pk=vpn_client.pk)
        if locked.is_deleted:
            return locked
        locked.traffic_limit_bytes = int(remote_result.get("new_total_bytes") or locked.traffic_limit_bytes or 0)
        locked.expires_at = remote_result.get("new_expiry_time") or locked.expires_at
        if remote_result.get("enabled") is True:
            locked.status = VPNClient.Status.ACTIVE
            locked.disabled_at = None
        elif remote_result.get("enabled") is False and locked.status == VPNClient.Status.ACTIVE:
            locked.status = VPNClient.Status.INACTIVE
        locked.last_synced_at = timezone.now()
        locked.xui_raw = remote_result.get("raw", locked.xui_raw)
        locked.save(
            update_fields=[
                "traffic_limit_bytes",
                "expires_at",
                "status",
                "disabled_at",
                "last_synced_at",
                "xui_raw",
                "updated_at",
            ]
        )
    return locked


def _sync_local_enabled_state(vpn_client, remote_result, enabled):
    if not vpn_client:
        return None
    with transaction.atomic():
        locked = VPNClient.objects.select_for_update().get(pk=vpn_client.pk)
        if locked.is_deleted:
            return locked
        locked.traffic_limit_bytes = int(remote_result.get("new_total_bytes") or locked.traffic_limit_bytes or 0)
        locked.expires_at = remote_result.get("new_expiry_time") or locked.expires_at
        locked.last_synced_at = timezone.now()
        locked.xui_raw = remote_result.get("raw", locked.xui_raw)
        if enabled:
            locked.status = VPNClient.Status.ACTIVE
            locked.disabled_at = None
        else:
            locked.status = VPNClient.Status.INACTIVE
            locked.disabled_at = locked.disabled_at or timezone.now()
        locked.save(
            update_fields=[
                "traffic_limit_bytes",
                "expires_at",
                "status",
                "disabled_at",
                "last_synced_at",
                "xui_raw",
                "updated_at",
            ]
        )
    return locked


def set_vpn_client_enabled_by_admin(admin_telegram_id, lookup_token_or_result, *, enabled):
    payload = dict(lookup_token_or_result or {})
    panel, inbound, identifier = _lookup_panel_inbound(payload)
    local_match = find_local_vpn_client_for_lookup(
        panel,
        inbound,
        identifier,
        result=payload,
        vpn_client_id=payload.get("vpn_client_id"),
    )
    vpn_client = local_match.vpn_client
    customer = _client_customer(vpn_client) if vpn_client else None
    log = _create_action_log(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=VPNClientActionLog.ActorType.ADMIN,
        actor_telegram_id=admin_telegram_id,
        action=ADMIN_ENABLE_ACTION if enabled else ADMIN_DISABLE_ACTION,
        panel=panel,
        inbound=inbound,
        identifier=identifier,
        old_total_bytes=getattr(vpn_client, "traffic_limit_bytes", None),
        old_expiry_time=getattr(vpn_client, "expires_at", None),
        metadata={
            "source": "admin_service_review",
            "local_match_status": local_match.status,
            "enabled": bool(enabled),
        },
    )
    try:
        remote_result = update_client_traffic_and_expiry(
            panel,
            inbound,
            identifier,
            enable=bool(enabled),
        )
    except Exception as exc:
        logger.warning(
            "Admin VPN client enabled-state update failed admin=%s panel=%s inbound=%s identifier=%s enabled=%s error=%s",
            admin_telegram_id,
            panel.pk,
            inbound.inbound_id,
            mask_identifier(identifier),
            bool(enabled),
            exc,
        )
        _complete_action_log(log, status=VPNClientActionLog.Status.FAILED, error_message=str(exc))
        raise VPNClientManagementError("تغییر وضعیت سرویس روی پنل انجام نشد.") from exc

    if vpn_client:
        _sync_local_enabled_state(vpn_client, remote_result, bool(enabled))

    _complete_action_log(
        log,
        status=VPNClientActionLog.Status.SUCCESS,
        new_total_bytes=remote_result.get("new_total_bytes"),
        new_expiry_time=remote_result.get("new_expiry_time"),
        metadata={
            "local_updated": bool(vpn_client),
            "local_match_status": local_match.status,
            "enabled": bool(remote_result.get("enabled")) if remote_result.get("enabled") is not None else bool(enabled),
        },
    )
    remote_result["audit_log"] = log
    remote_result["vpn_client"] = vpn_client
    remote_result["local_match_status"] = local_match.status
    return remote_result


def update_vpn_client_limits_by_admin(
    admin_telegram_id,
    lookup_token_or_result,
    *,
    traffic_gb=None,
    expiry_days=None,
    set_expiry_at=None,
    set_total_gb=None,
    mode="add",
):
    payload = dict(lookup_token_or_result or {})
    panel, inbound, identifier = _lookup_panel_inbound(payload)
    live = _live_lookup(panel, identifier)
    local_match = find_local_vpn_client_for_lookup(
        panel,
        inbound,
        identifier,
        result=live,
        vpn_client_id=payload.get("vpn_client_id"),
    )
    vpn_client = local_match.vpn_client
    customer = _client_customer(vpn_client) if vpn_client else None

    current_total = int(live.get("total_bytes") or live.get("total_traffic_bytes") or 0)
    current_expiry = live.get("expiry_time") or live.get("expiry_at")
    new_total = None
    new_expiry = None
    action = None

    if set_total_gb is not None:
        new_total = gb_to_bytes(set_total_gb)
    elif traffic_gb is not None:
        if mode == "set":
            new_total = gb_to_bytes(traffic_gb)
        else:
            if not current_total:
                raise VPNClientManagementError("این کانفیگ حجم نامحدود دارد. ابتدا حجم کل را تنظیم کنید.")
            new_total = current_total + gb_to_bytes(traffic_gb)

    if set_expiry_at is not None:
        new_expiry = set_expiry_at
    elif expiry_days is not None:
        days = _safe_int(expiry_days)
        base = current_expiry if current_expiry and current_expiry > timezone.now() else timezone.now()
        new_expiry = base + timedelta(days=days)

    if new_total is not None and new_expiry is not None:
        action = VPNClientActionLog.Action.ADMIN_UPDATE_TRAFFIC_AND_EXPIRY
    elif new_total is not None:
        action = VPNClientActionLog.Action.ADMIN_UPDATE_TRAFFIC
    elif new_expiry is not None:
        action = VPNClientActionLog.Action.ADMIN_UPDATE_EXPIRY
    else:
        raise VPNClientManagementError("هیچ تغییری برای اعمال انتخاب نشده است.")

    log = _create_action_log(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=VPNClientActionLog.ActorType.ADMIN,
        actor_telegram_id=admin_telegram_id,
        action=action,
        panel=panel,
        inbound=inbound,
        identifier=identifier,
        old_total_bytes=current_total,
        new_total_bytes=new_total,
        old_expiry_time=current_expiry,
        new_expiry_time=new_expiry,
        metadata={
            "source": "admin_lookup_update",
            "local_match_status": local_match.status,
            "mode": mode,
        },
    )

    try:
        remote_result = update_client_traffic_and_expiry(
            panel,
            inbound,
            identifier,
            total_bytes=new_total,
            expiry_time=new_expiry,
            enable=True,
        )
    except Exception as exc:
        logger.warning(
            "Admin VPN client update failed admin=%s panel=%s inbound=%s identifier=%s error=%s",
            admin_telegram_id,
            panel.pk,
            inbound.inbound_id,
            mask_identifier(identifier),
            exc,
        )
        _complete_action_log(log, status=VPNClientActionLog.Status.FAILED, error_message=str(exc))
        raise VPNClientManagementError("ویرایش کانفیگ روی پنل انجام نشد.") from exc

    if vpn_client:
        sync_local_vpn_client_after_remote_update(vpn_client, remote_result)

    _complete_action_log(
        log,
        status=VPNClientActionLog.Status.SUCCESS,
        new_total_bytes=remote_result.get("new_total_bytes"),
        new_expiry_time=remote_result.get("new_expiry_time"),
        metadata={"local_updated": bool(vpn_client), "local_match_status": local_match.status},
    )
    remote_result["audit_log"] = log
    remote_result["vpn_client"] = vpn_client
    remote_result["local_match_status"] = local_match.status
    return remote_result


def refresh_vpn_client_link_by_admin(admin_telegram_id, lookup_token_or_result):
    payload = dict(lookup_token_or_result or {})
    panel, inbound, identifier = _lookup_panel_inbound(payload)
    local_match = find_local_vpn_client_for_lookup(
        panel,
        inbound,
        identifier,
        result=payload,
        vpn_client_id=payload.get("vpn_client_id"),
    )
    vpn_client = local_match.vpn_client
    customer = _client_customer(vpn_client) if vpn_client else None
    log = _create_action_log(
        vpn_client=vpn_client,
        customer=customer,
        actor_type=VPNClientActionLog.ActorType.ADMIN,
        actor_telegram_id=admin_telegram_id,
        action=VPNClientActionLog.Action.ADMIN_REFRESH_LINK,
        panel=panel,
        inbound=inbound,
        identifier=identifier,
        metadata={"source": "admin_lookup_refresh_link", "local_match_status": local_match.status},
    )
    try:
        details = build_config_link_for_identifier(panel, inbound.inbound_id, identifier)
    except Exception as exc:
        _complete_action_log(log, status=VPNClientActionLog.Status.FAILED, error_message=str(exc))
        raise VPNClientManagementError("دریافت لینک به‌روز انجام نشد.") from exc

    updated_link = details.get("updated_config_link") or details.get("direct_link") or ""
    if vpn_client and updated_link:
        vpn_client.direct_link = updated_link
        vpn_client.xui_email = details.get("email") or vpn_client.xui_email
        vpn_client.save(update_fields=["direct_link", "xui_email", "updated_at"])

    _complete_action_log(
        log,
        status=VPNClientActionLog.Status.SUCCESS,
        metadata={"local_updated": bool(vpn_client), "link_returned": bool(details.get("updated_config_link"))},
    )
    details["audit_log"] = log
    details["vpn_client"] = vpn_client
    return details


def build_delete_confirmation_text(vpn_client):
    label = vpn_client.xui_email or vpn_client.username or "کانفیگ"
    plan = vpn_client.plan.name if vpn_client.plan_id else "بدون پلن"
    expiry = format_jalali_datetime(vpn_client.expires_at, default="نامحدود") if vpn_client.expires_at else "نامحدود"
    remaining = _format_gb_from_bytes(vpn_client.remaining_traffic_bytes) if vpn_client.traffic_limit_bytes else "نامحدود"
    return (
        "⚠️ حذف کانفیگ\n\n"
        "این کار کانفیگ شما را از پنل حذف می‌کند و دیگر قابل استفاده نیست.\n\n"
        f"نام سرویس: {label}\n"
        f"پلن: {plan}\n"
        f"انقضا: {expiry}\n"
        f"حجم باقی‌مانده: {remaining} گیگ\n\n"
        "آیا مطمئن هستید؟"
    )


def build_admin_delete_confirmation_text(payload):
    panel_name = payload.get("panel_name") or "-"
    inbound_label = payload.get("inbound_remark") or payload.get("inbound_id") or "-"
    enabled = payload.get("enabled")
    status = "فعال" if enabled is not False else "غیرفعال"
    return (
        "⚠️ حذف کانفیگ از پنل\n\n"
        f"نام/ایمیل: {payload.get('masked_identifier') or mask_identifier(payload.get('identifier'))}\n"
        f"پنل: {panel_name}\n"
        f"Inbound: {inbound_label}\n"
        f"وضعیت: {status}\n\n"
        "این عملیات کانفیگ را از Sanaei/X-UI حذف می‌کند."
    )


def build_admin_edit_summary(result):
    total = result.get("new_total_bytes")
    expiry = result.get("new_expiry_time")
    lines = ["✅ تغییرات کانفیگ اعمال شد."]
    if total is not None:
        lines.append(f"حجم کل جدید: {_format_gb_from_bytes(total)} گیگ")
    if expiry:
        lines.append(f"انقضای جدید: {format_jalali_datetime(expiry, default='نامحدود')}")
    if result.get("local_match_status") == "multiple":
        lines.append("به دلیل چند match محلی، فقط پنل آپدیت شد و دیتابیس local تغییر نکرد.")
    elif result.get("local_match_status") == "not_found":
        lines.append("رکورد local متناظر پیدا نشد؛ فقط پنل آپدیت شد.")
    return "\n".join(lines)
