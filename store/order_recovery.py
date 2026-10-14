"""Manual order recovery shared by the admin panel and bot."""
from dataclasses import dataclass

from django.db import transaction
from django.utils.dateparse import parse_datetime

from .db_locking import select_for_update_self
from .models import Order


def record_order_metadata(order, key, value):
    # Merge with the latest row so notification bookkeeping preserves provisioning data.
    with transaction.atomic():
        current = select_for_update_self(Order.objects.all()).get(pk=order.pk)
        metadata = dict(current.metadata or {})
        metadata[key] = value
        current.metadata = metadata
        current.save(update_fields=["metadata", "updated_at"])
    order.metadata = metadata


def order_has_partial_delivery(order):
    metadata = order.metadata or {}
    if metadata.get("renewal_client_pk"):
        return False
    return bool(
        (metadata.get("cup_fulfillment_recipe") or {}).get("status") == "partial"
        or (metadata.get("plan_delivery_v2") or {}).get("failed_source_result_count")
    )


def can_retry_order(order):
    automation = getattr(order, "automation", None)
    if automation and automation.review_status in {"cancelling", "cancel_failed", "cancelled"}:
        return False
    return (
        order.status not in {Order.Status.REJECTED, Order.Status.CANCELLED}
        and (order.status != Order.Status.COMPLETED or order_has_partial_delivery(order))
        and order.verification_status != Order.VerificationStatus.REJECTED
        and bool(
            order.is_paid
            or order.payment_submitted_at
            or order.provisioning_status in {Order.ProvisioningStatus.FAILED, Order.ProvisioningStatus.PROVISIONING}
            or (order.metadata or {}).get("panel_provisioning_deferred")
        )
    )


def can_resend_order(order):
    return order.status == Order.Status.COMPLETED and order.verification_status == Order.VerificationStatus.VERIFIED


def customer_notification_summary(order):
    state = dict((order.metadata or {}).get("customer_notification") or {})
    state["label"] = {
        "sent": "ارسال شد",
        "partial": "ارسال ناقص؛ تلاش مجدد لازم است",
        "failed": "ارسال ناموفق",
        "sending": "نتیجه ارسال نامشخص",
        "no_target": "مقصد فعال برای مشتری پیدا نشد",
        "skipped": "اطلاع‌رسانی این سفارش غیرفعال است",
    }.get(state.get("status"), "هنوز نتیجه ارسال ثبت نشده")
    state["attempted_at"] = parse_datetime(state["attempted_at"]) if state.get("attempted_at") else None
    return state


@dataclass
class OrderMessageResult:
    success: bool
    message: str


def retry_order_completion(order, *, actor=None):
    from .provisioning_services import ProvisioningResult, approve_and_provision_order

    order.refresh_from_db()
    if not can_retry_order(order):
        return ProvisioningResult(False, "تلاش مجدد برای این سفارش در دسترس نیست.")
    return approve_and_provision_order(order, actor=actor, source="manual_retry", notify=True)


def resend_order_result(order, *, target_bot_user=None):
    order.refresh_from_db()
    if not can_resend_order(order):
        return OrderMessageResult(False, "ارسال مجدد فقط برای سفارش تکمیل‌شده در دسترس است.")
    from .telegram_bot.notifications import notify_order_event

    notify_order_event(order, event_type="approved", force_customer=True, target_bot_user=target_bot_user)
    order.refresh_from_db()
    summary = customer_notification_summary(order)
    return OrderMessageResult(summary.get("status") == "sent", summary["label"])
