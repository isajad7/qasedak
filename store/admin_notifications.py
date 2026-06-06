import logging

from django.db import transaction
from django.utils import timezone

from .models import BotEventLog, Order

logger = logging.getLogger(__name__)


def get_admin_telegram_ids(store=None):
    from .bots import active_bot_configs

    admin_ids = []
    for config in active_bot_configs(store=store):
        if not config.notify_new_orders:
            continue
        for admin_id in config.get_admin_user_ids():
            if admin_id not in admin_ids:
                admin_ids.append(admin_id)
    return admin_ids


def build_admin_order_message(order):
    from .bots import format_order_message

    return format_order_message(order)


def build_admin_order_keyboard(order):
    from .bots import order_admin_keyboard

    return order_admin_keyboard(order)


def _order_id(order_or_id):
    return getattr(order_or_id, "pk", order_or_id)


def _order_queryset():
    return Order.objects.select_related(
        "store",
        "customer",
        "plan",
        "operator",
        "inbound",
        "inbound__panel",
    )


def _order_has_payment_evidence(order):
    metadata = order.metadata or {}
    return bool(
        order.payment_submitted_at
        or order.payment_receipt_image
        or (metadata.get("receipt_text") or "").strip()
        or metadata.get("receipt")
        or order.bank_tracking_code
    )


def _notification_configs(order):
    from .bots import active_bot_configs

    return [config for config in active_bot_configs(order) if config.notify_new_orders]


def _claim_notification(order_id, field_name, *, also_claim_receipt=False, require_payment_evidence=False):
    with transaction.atomic():
        order = _order_queryset().select_for_update().filter(pk=order_id).first()
        if not order:
            logger.warning("Admin notification skipped because order_id=%s was not found.", order_id)
            return None
        if (order.metadata or {}).get("suppress_new_order_notification"):
            return None
        if require_payment_evidence and not _order_has_payment_evidence(order):
            return None
        if getattr(order, field_name):
            return None

        now = timezone.now()
        setattr(order, field_name, now)
        update_fields = [field_name]
        if also_claim_receipt and _order_has_payment_evidence(order) and not order.admin_receipt_notified_at:
            order.admin_receipt_notified_at = now
            update_fields.append("admin_receipt_notified_at")
        order.save(update_fields=[*update_fields, "updated_at"])
        return order


def _send_order_notification(order, *, title):
    from .bots import send_new_order_to_config

    sent_count = 0
    for config in _notification_configs(order):
        try:
            if send_new_order_to_config(config, order, title=title):
                sent_count += 1
        except Exception as exc:
            logger.exception(
                "Admin order notification failed config_id=%s order_id=%s: %s",
                config.pk,
                order.pk,
                exc,
            )
            try:
                from .bots import log_event

                log_event(
                    config,
                    event_type=BotEventLog.EventType.ERROR,
                    status=BotEventLog.Status.FAILED,
                    order=order,
                    message=f"Admin order notification failed: {exc}",
                )
            except Exception:
                logger.exception("Could not log admin notification failure for config_id=%s.", config.pk)
    return sent_count


def notify_admins_new_order(order_or_id):
    order_id = _order_id(order_or_id)
    order = _order_queryset().filter(pk=order_id).first()
    if not order or not _notification_configs(order):
        return 0

    claimed_order = _claim_notification(
        order_id,
        "admin_notified_at",
        also_claim_receipt=True,
    )
    if not claimed_order:
        return 0
    return _send_order_notification(claimed_order, title="سفارش جدید VPN")


def notify_admins_payment_receipt(order_or_id):
    order_id = _order_id(order_or_id)
    order = _order_queryset().filter(pk=order_id).first()
    if not order or not _notification_configs(order):
        return 0

    claimed_order = _claim_notification(
        order_id,
        "admin_receipt_notified_at",
        require_payment_evidence=True,
    )
    if not claimed_order:
        return 0
    return _send_order_notification(claimed_order, title="رسید پرداخت برای سفارش ثبت شد")


def notify_admins_order_needs_review(order_or_id):
    order_id = _order_id(order_or_id)
    order = _order_queryset().filter(pk=order_id).first()
    if not order or not _notification_configs(order):
        return 0

    claimed_order = _claim_notification(order_id, "admin_receipt_notified_at")
    if not claimed_order:
        return 0
    return _send_order_notification(claimed_order, title="سفارش نیازمند بررسی پرداخت")


def schedule_notify_admins_new_order(order_or_id):
    order_id = _order_id(order_or_id)
    transaction.on_commit(lambda: notify_admins_new_order(order_id))


def schedule_notify_admins_payment_receipt(order_or_id):
    order_id = _order_id(order_or_id)
    transaction.on_commit(lambda: notify_admins_payment_receipt(order_id))


def schedule_notify_admins_order_needs_review(order_or_id):
    order_id = _order_id(order_or_id)
    transaction.on_commit(lambda: notify_admins_order_needs_review(order_id))
