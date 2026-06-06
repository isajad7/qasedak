from dataclasses import dataclass
import logging

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from .models import Inbound, Order, VPNClient
from .naming import build_client_display_name
from .referral_services import create_referral_reward_for_order
from .xui_api import delete_client, enable_client, renew_client

logger = logging.getLogger(__name__)
PANEL_LINK_ADMIN_MESSAGE = "این کانفیگ به اینباند و پنل معتبر وصل نیست. لطفاً تنظیمات اینباند و پنل را در ادمین بررسی کن."


@dataclass
class OrderActionResult:
    success: bool
    message: str


def required_client_count(order):
    return max(int(order.quantity or 1), 1)


def client_display_username(base_username, client_result, *, order, index):
    if required_client_count(order) > 1:
        return client_result.get("email") or f"{base_username}_{index}"
    return base_username


def create_order_vpn_client(order, *, inbound, username, client_result, status=VPNClient.Status.INACTIVE):
    return VPNClient.objects.create(
        store=order.store,
        order=order,
        plan=order.plan,
        inbound=inbound,
        username=username,
        xui_email=client_result["email"],
        uuid=client_result["uuid"],
        sub_id=client_result["sub_id"],
        sub_link=client_result["sub_link"],
        direct_link=client_result["direct_link"],
        status=status,
        traffic_limit_bytes=order.plan.traffic_limit_bytes,
        duration_days=order.plan.duration_days,
        device_limit=order.plan.device_limit,
        xui_raw=client_result.get("raw", {}),
    )


def vpn_client_panel_error(vpn_client):
    if not getattr(vpn_client, "uuid", None):
        return "UUID کانفیگ ثبت نشده است."
    if not getattr(vpn_client, "inbound_id", None):
        return "کانفیگ به هیچ inboundی وصل نیست."

    inbound = getattr(vpn_client, "inbound", None)
    if not inbound:
        return "Inbound کانفیگ در دیتابیس پیدا نشد."
    if not getattr(inbound, "panel_id", None):
        return "Inbound کانفیگ به هیچ پنلی وصل نیست."
    if not getattr(inbound, "is_active", False):
        return "Inbound کانفیگ غیرفعال است."

    panel = getattr(inbound, "panel", None)
    if not panel:
        return "پنل متصل به inbound در دیتابیس پیدا نشد."
    if not getattr(panel, "is_active", False):
        return "پنل متصل به inbound غیرفعال است."
    return ""


def log_panel_link_error(prefix, *, order=None, vpn_client=None, reason=""):
    logger.warning(
        "%s order_id=%s tracking=%s client_id=%s inbound_pk=%s inbound_panel_id=%s reason=%s",
        prefix,
        getattr(order, "pk", None),
        getattr(order, "order_tracking_code", None),
        getattr(vpn_client, "pk", None),
        getattr(vpn_client, "inbound_id", None),
        getattr(getattr(vpn_client, "inbound", None), "panel_id", None),
        reason,
    )


def ensure_legacy_order_client(order):
    if order.vpn_clients.exists() or not order.can_be_activated:
        return None
    client_result = {
        "email": order.username,
        "uuid": order.uuid,
        "sub_id": "",
        "sub_link": order.sub_link,
        "direct_link": order.direct_link,
        "raw": {"id": order.uuid, "email": order.username},
    }
    return create_order_vpn_client(
        order,
        inbound=order.inbound,
        username=order.username,
        client_result=client_result,
    )


def sync_order_primary_client_fields(order, client):
    changed_fields = []
    if not client:
        return changed_fields
    if not order.inbound_id and client.inbound_id:
        order.inbound = client.inbound
        changed_fields.append("inbound")
    if not order.username:
        order.username = client.username
        changed_fields.append("username")
    if not order.uuid and client.uuid:
        order.uuid = client.uuid
        changed_fields.append("uuid")
    if not order.sub_link and client.sub_link:
        order.sub_link = client.sub_link
        changed_fields.append("sub_link")
    if not order.direct_link and client.direct_link:
        order.direct_link = client.direct_link
        changed_fields.append("direct_link")
    return changed_fields


def provision_missing_panel_client(order):
    from .order_services import get_available_inbound, validate_inbound_for_order
    from .xui_api import create_inactive_client_details

    ensure_legacy_order_client(order)
    missing_count = required_client_count(order) - order.vpn_clients.count()
    if missing_count <= 0:
        return OrderActionResult(True, "کلاینت‌های پنل از قبل آماده هستند.")

    if order.inbound_id:
        inbound = order.inbound
        try:
            inbound = validate_inbound_for_order(inbound, order.store, required_slots=missing_count)
        except ValidationError as exc:
            logger.warning(
                "activate_order refused to provision on invalid order inbound order_id=%s tracking=%s inbound_pk=%s panel_id=%s: %s",
                order.pk,
                order.order_tracking_code,
                getattr(inbound, "pk", None),
                getattr(inbound, "panel_id", None),
                exc.messages[0],
            )
            return OrderActionResult(False, exc.messages[0])
    else:
        inbound = get_available_inbound(order.store, required_slots=missing_count)

    if not inbound:
        return OrderActionResult(False, "فعلاً سرور VPN فعالی برای ساخت کانفیگ در دسترس نیست.")

    metadata = dict(order.metadata or {})
    username = (
        metadata.get("deferred_panel_username")
        or build_client_display_name(
            order.customer,
            order=order,
            preferred_name=order.sender_card_name,
            short_id=order.order_tracking_code,
            metadata=metadata,
        )
    )
    for index in range(1, missing_count + 1):
        client_result = create_inactive_client_details(
            email_prefix=username,
            total_gb=order.plan.volume_gb,
            expire_days=order.plan.duration_days,
            panel=inbound.panel,
            inbound=inbound,
            limit_ip=order.plan.device_limit,
        )
        if not client_result:
            metadata["panel_provisioning_deferred"] = True
            metadata["panel_provisioning_last_failed_at"] = timezone.now().isoformat()
            metadata["panel_provisioning_reason"] = "panel_unavailable_on_activation"
            metadata["panel_provisioning_missing_clients"] = required_client_count(order) - order.vpn_clients.count()
            order.metadata = metadata
            order.save(update_fields=["metadata", "updated_at"])
            return OrderActionResult(
                False,
                "ساخت کلاینت در پنل X-UI ناموفق بود. بعد از بررسی دسترسی پنل دوباره تلاش کن.",
            )

        create_order_vpn_client(
            order,
            inbound=inbound,
            username=client_display_username(username, client_result, order=order, index=index),
            client_result=client_result,
        )
        Inbound.objects.filter(pk=inbound.pk).update(current_users=F("current_users") + 1)

    primary_client = order.vpn_clients.order_by("created_at", "pk").first()
    changed_fields = sync_order_primary_client_fields(order, primary_client)
    metadata["panel_provisioning_deferred"] = False
    metadata["panel_provisioned_at"] = timezone.now().isoformat()
    metadata["panel_provisioning_reason"] = ""
    metadata["panel_provisioning_missing_clients"] = 0
    order.metadata = metadata
    order.save(update_fields=[*changed_fields, "metadata", "updated_at"])
    return OrderActionResult(True, "کلاینت‌های پنل ساخته شدند.")


def activate_order(order, *, user=None, notify=True):
    logger.info("activate_order started order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .select_related("plan", "store", "inbound", "inbound__panel")
            .get(pk=order.pk)
        )
        if order.status == Order.Status.COMPLETED and order.verification_status == Order.VerificationStatus.VERIFIED:
            logger.info("activate_order skipped already completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
            return OrderActionResult(True, "سفارش قبلاً تکمیل شده است.")

        if (order.metadata or {}).get("renewal_client_pk"):
            renewal_result = activate_renewal_order(order, user=user)
            if not renewal_result.success:
                return renewal_result
            action_message = renewal_result.message
        else:
            ensure_legacy_order_client(order)
            missing_count = required_client_count(order) - order.vpn_clients.count()
            if missing_count > 0:
                logger.info(
                    "activate_order provisioning missing panel clients order_id=%s tracking=%s missing=%s",
                    order.pk,
                    order.order_tracking_code,
                    missing_count,
                )
                provision_result = provision_missing_panel_client(order)
                if not provision_result.success:
                    logger.warning(
                        "activate_order failed provisioning order_id=%s tracking=%s message=%s",
                        order.pk,
                        order.order_tracking_code,
                        provision_result.message,
                    )
                    return provision_result

            clients = list(
                order.vpn_clients.select_for_update().select_related("plan", "inbound", "inbound__panel").order_by("created_at", "pk")
            )
            if len(clients) < required_client_count(order):
                return OrderActionResult(False, "همه کانفیگ‌های VPN هنوز آماده نیستند. فعال‌سازی را دوباره امتحان کن.")

            for vpn_client in clients:
                if vpn_client.status == VPNClient.Status.ACTIVE:
                    continue
                panel_error = vpn_client_panel_error(vpn_client)
                if panel_error:
                    log_panel_link_error(
                        "activate_order invalid VPN client panel link",
                        order=order,
                        vpn_client=vpn_client,
                        reason=panel_error,
                    )
                    return OrderActionResult(False, PANEL_LINK_ADMIN_MESSAGE)
                logger.info(
                    "Calling 3xUI enable_client order_id=%s tracking=%s client_id=%s uuid=%s",
                    order.pk,
                    order.order_tracking_code,
                    vpn_client.pk,
                    vpn_client.uuid,
                )
                if not enable_client(vpn_client):
                    logger.warning(
                        "3xUI enable_client failed order_id=%s tracking=%s client_id=%s uuid=%s",
                        order.pk,
                        order.order_tracking_code,
                        vpn_client.pk,
                        vpn_client.uuid,
                    )
                    return OrderActionResult(False, "فعال‌سازی روی پنل X-UI ناموفق بود.")
                logger.info(
                    "3xUI enable_client succeeded order_id=%s tracking=%s client_id=%s uuid=%s",
                    order.pk,
                    order.order_tracking_code,
                    vpn_client.pk,
                    vpn_client.uuid,
                )

            order.mark_payment_verified(user=user)
            changed_fields = sync_order_primary_client_fields(order, clients[0] if clients else None)
            order.save(
                update_fields=[
                    "is_paid",
                    "verification_status",
                    "verified_by",
                    "verified_at",
                    "status",
                    *changed_fields,
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
            action_message = "سفارش تایید شد و کانفیگ VPN فعال شد."

    try:
        create_referral_reward_for_order(order)
    except Exception:
        logger.exception("Could not create referral GB reward for order_id=%s", order.pk)

    if notify:
        from .bots import notify_order_event

        notify_order_event(order, event_type="approved")

    logger.info("activate_order completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    return OrderActionResult(True, action_message)


def activate_renewal_order(order, *, user=None):
    client_pk = (order.metadata or {}).get("renewal_client_pk")
    if not client_pk:
        return OrderActionResult(False, "سفارش تمدید به کانفیگ مقصد وصل نیست.")

    vpn_client = (
        VPNClient.objects.select_for_update()
        .select_related("plan", "order", "inbound", "inbound__panel")
        .filter(pk=client_pk)
        .first()
    )
    if not vpn_client:
        return OrderActionResult(False, "کانفیگ مقصد برای تمدید پیدا نشد.")
    if order.customer_id and vpn_client.order_id and vpn_client.order.customer_id != order.customer_id:
        return OrderActionResult(False, "کانفیگ مقصد متعلق به این مشتری نیست.")
    panel_error = vpn_client_panel_error(vpn_client)
    if panel_error:
        log_panel_link_error(
            "activate_renewal_order invalid VPN client panel link",
            order=order,
            vpn_client=vpn_client,
            reason=panel_error,
        )
        return OrderActionResult(False, PANEL_LINK_ADMIN_MESSAGE)

    renewed = renew_client(vpn_client, order.plan)
    if not renewed:
        return OrderActionResult(False, "تمدید روی پنل X-UI ناموفق بود.")

    order.mark_payment_verified(user=user)
    metadata = dict(order.metadata or {})
    metadata["renewed_at"] = timezone.now().isoformat()
    order.metadata = metadata
    order.save(
        update_fields=[
            "is_paid",
            "verification_status",
            "verified_by",
            "verified_at",
            "status",
            "metadata",
            "updated_at",
        ]
    )

    vpn_client.plan = order.plan
    vpn_client.status = VPNClient.Status.ACTIVE
    vpn_client.traffic_limit_bytes = order.plan.traffic_limit_bytes
    vpn_client.used_upload_bytes = 0
    vpn_client.used_download_bytes = 0
    vpn_client.used_traffic_bytes = 0
    vpn_client.duration_days = order.plan.duration_days
    vpn_client.device_limit = order.plan.device_limit
    vpn_client.expires_at = renewed.get("expiry_at") or vpn_client.expires_at
    vpn_client.disabled_at = None
    vpn_client.xui_raw = renewed.get("raw", vpn_client.xui_raw)
    vpn_client.save(
        update_fields=[
            "plan",
            "status",
            "traffic_limit_bytes",
            "used_upload_bytes",
            "used_download_bytes",
            "used_traffic_bytes",
            "duration_days",
            "device_limit",
            "expires_at",
            "disabled_at",
            "xui_raw",
            "updated_at",
        ]
    )
    return OrderActionResult(True, "سفارش تایید شد و اشتراک VPN تمدید شد.")


def deletion_targets_for_order(order):
    clients = list(order.vpn_clients.select_related("inbound", "inbound__panel").all())
    if clients:
        return clients
    if order.inbound_id and order.uuid:
        return [order]
    return []


def decrement_inbound_users_for_targets(targets):
    counts_by_inbound = {}
    for target in targets:
        if not getattr(target, "inbound_id", None):
            continue
        counts_by_inbound[target.inbound_id] = counts_by_inbound.get(target.inbound_id, 0) + 1
    for inbound_id, count in counts_by_inbound.items():
        Inbound.objects.filter(pk=inbound_id, current_users__gt=0).update(
            current_users=models.Case(
                models.When(current_users__lt=count, then=0),
                default=F("current_users") - count,
                output_field=models.PositiveIntegerField(),
            ),
            updated_at=timezone.now(),
        )


def reject_order(order, *, reason="", user=None, notify=True):
    logger.info("reject_order started order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .select_related("plan", "store", "inbound", "inbound__panel")
            .prefetch_related("vpn_clients")
            .get(pk=order.pk)
        )
        if order.status == Order.Status.COMPLETED:
            logger.warning("reject_order denied completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
            return OrderActionResult(False, "Completed orders cannot be rejected.")

        delete_targets = deletion_targets_for_order(order)
        for target in delete_targets:
            logger.info(
                "Calling 3xUI delete_client order_id=%s tracking=%s uuid=%s",
                order.pk,
                order.order_tracking_code,
                target.uuid,
            )
            if not delete_client(target):
                logger.warning(
                    "3xUI delete_client failed order_id=%s tracking=%s uuid=%s",
                    order.pk,
                    order.order_tracking_code,
                    target.uuid,
                )
                return OrderActionResult(False, "Sanaei/X-UI client deletion failed.")
            logger.info(
                "3xUI delete_client succeeded order_id=%s tracking=%s uuid=%s",
                order.pk,
                order.order_tracking_code,
                target.uuid,
            )

        order.mark_payment_rejected(reason=reason, user=user)
        metadata = dict(order.metadata or {})
        metadata["panel_client_deleted_on_reject"] = bool(delete_targets)
        metadata["panel_clients_deleted_on_reject"] = len(delete_targets)
        order.metadata = metadata
        order.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "status",
                "metadata",
                "updated_at",
            ]
        )

        for vpn_client in order.vpn_clients.all():
            vpn_client.mark_suspended()
            raw = dict(vpn_client.xui_raw or {})
            raw["deleted_on_reject"] = True
            vpn_client.xui_raw = raw
            vpn_client.save(update_fields=["status", "disabled_at", "xui_raw", "updated_at"])

        decrement_inbound_users_for_targets(delete_targets)

    if notify:
        from .bots import notify_order_event

        notify_order_event(order, event_type="rejected")

    logger.info("reject_order completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    return OrderActionResult(True, "Order rejected.")


def cancel_order(order, *, user=None, hide_from_customer=True):
    logger.info("cancel_order started order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .select_related("plan", "store", "inbound", "inbound__panel")
            .prefetch_related("vpn_clients")
            .get(pk=order.pk)
        )
        delete_targets = deletion_targets_for_order(order)
        should_delete_panel_client = bool(delete_targets)

        for target in delete_targets:
            logger.info(
                "Calling 3xUI delete_client for cancel order_id=%s tracking=%s uuid=%s",
                order.pk,
                order.order_tracking_code,
                target.uuid,
            )
            if not delete_client(target):
                logger.warning(
                    "3xUI delete_client failed for cancel order_id=%s tracking=%s uuid=%s",
                    order.pk,
                    order.order_tracking_code,
                    target.uuid,
                )
                return OrderActionResult(False, "Sanaei/X-UI client deletion failed.")

        metadata = dict(order.metadata or {})
        metadata["customer_hidden"] = bool(hide_from_customer)
        metadata["cancelled_by_customer"] = True
        metadata["panel_client_deleted_on_cancel"] = should_delete_panel_client
        metadata["panel_clients_deleted_on_cancel"] = len(delete_targets)
        order.metadata = metadata
        order.status = Order.Status.CANCELLED
        order.save(update_fields=["status", "metadata", "updated_at"])

        for vpn_client in order.vpn_clients.all():
            vpn_client.mark_suspended()
            raw = dict(vpn_client.xui_raw or {})
            raw["deleted_on_cancel"] = should_delete_panel_client
            vpn_client.xui_raw = raw
            vpn_client.save(update_fields=["status", "disabled_at", "xui_raw", "updated_at"])

        decrement_inbound_users_for_targets(delete_targets)

    logger.info("cancel_order completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    return OrderActionResult(True, "Order deleted from dashboard.")
