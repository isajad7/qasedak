from dataclasses import dataclass
import logging

from django.db import transaction

from .models import Order
from .xui_api import enable_client

logger = logging.getLogger(__name__)


@dataclass
class OrderActionResult:
    success: bool
    message: str


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
            return OrderActionResult(True, "Order is already completed.")

        if not order.can_be_activated:
            logger.warning("activate_order failed missing inbound/uuid order_id=%s tracking=%s", order.pk, order.order_tracking_code)
            return OrderActionResult(False, "This order has no valid inbound or VPN UUID.")

        logger.info("Calling 3xUI enable_client order_id=%s tracking=%s uuid=%s", order.pk, order.order_tracking_code, order.uuid)
        if not enable_client(order):
            logger.warning("3xUI enable_client failed order_id=%s tracking=%s uuid=%s", order.pk, order.order_tracking_code, order.uuid)
            return OrderActionResult(False, "Sanaei/X-UI activation failed.")
        logger.info("3xUI enable_client succeeded order_id=%s tracking=%s uuid=%s", order.pk, order.order_tracking_code, order.uuid)

        order.mark_payment_verified(user=user)
        order.save(
            update_fields=[
                "is_paid",
                "verification_status",
                "verified_by",
                "verified_at",
                "status",
                "updated_at",
            ]
        )

        vpn_client = order.vpn_clients.first()
        if vpn_client:
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

    if notify:
        from .bots import notify_order_event

        notify_order_event(order, event_type="approved")

    logger.info("activate_order completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    return OrderActionResult(True, "Order approved and VPN client activated.")


def reject_order(order, *, reason="", user=None, notify=True):
    logger.info("reject_order started order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    with transaction.atomic():
        order = Order.objects.select_for_update().select_related("plan", "store").get(pk=order.pk)
        if order.status == Order.Status.COMPLETED:
            logger.warning("reject_order denied completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
            return OrderActionResult(False, "Completed orders cannot be rejected.")

        order.mark_payment_rejected(reason=reason, user=user)
        order.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "status",
                "updated_at",
            ]
        )

    if notify:
        from .bots import notify_order_event

        notify_order_event(order, event_type="rejected")

    logger.info("reject_order completed order_id=%s tracking=%s", order.pk, order.order_tracking_code)
    return OrderActionResult(True, "Order rejected.")
