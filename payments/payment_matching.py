from datetime import timedelta
from html import escape

from django.db import transaction
from django.utils import timezone

from store.models import BotEventLog, Order

from .models import IncomingPaymentSMS


MATCH_WINDOW_BEFORE = timedelta(minutes=30)
MATCH_WINDOW_AFTER = timedelta(minutes=15)
PENDING_ORDER_STATUSES = (
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    "pending",
)


def find_matching_orders(amount, sms_datetime):
    if timezone.is_naive(sms_datetime):
        sms_datetime = timezone.make_aware(sms_datetime, timezone.get_default_timezone())

    total_field = _order_total_field()
    return (
        Order.objects.select_related("customer", "plan", "store")
        .filter(
            status__in=PENDING_ORDER_STATUSES,
            created_at__gte=sms_datetime - MATCH_WINDOW_BEFORE,
            created_at__lte=sms_datetime + MATCH_WINDOW_AFTER,
            **{total_field: amount},
        )
        .order_by("created_at", "pk")
    )


def _order_total_field():
    order_field_names = {field.name for field in Order._meta.fields}
    return "total_price" if "total_price" in order_field_names else "amount"


def process_incoming_payment_sms(payment_sms, *, notify=True):
    orders = list(find_matching_orders(payment_sms.amount, payment_sms.sms_datetime))
    payment_sms.matched_orders.set(orders)
    payment_sms.status = IncomingPaymentSMS.Status.MATCHED if orders else IncomingPaymentSMS.Status.NO_MATCH
    payment_sms.save(update_fields=["status"])

    if orders and notify:
        notify_admins_about_payment_match(payment_sms, orders)

    return orders


def confirm_incoming_payment_sms(payment_sms, *, order=None, user=None):
    with transaction.atomic():
        payment_sms = IncomingPaymentSMS.objects.select_for_update().get(pk=payment_sms.pk)
        matched_orders = payment_sms.matched_orders.select_for_update()

        if order is None:
            matched_orders = list(matched_orders)
            if len(matched_orders) != 1:
                raise ValueError("Choose exactly one matched order to confirm this SMS.")
            order = matched_orders[0]
        else:
            order = Order.objects.select_for_update().get(pk=order.pk)
            if not payment_sms.matched_orders.filter(pk=order.pk).exists():
                raise ValueError("The selected order is not linked to this SMS.")

        now = timezone.now()
        order.is_paid = True
        order.status = Order.Status.CONFIRMED
        order.verification_status = Order.VerificationStatus.VERIFIED
        order.verified_by = user
        order.verified_at = now
        order.save(
            update_fields=[
                "is_paid",
                "status",
                "verification_status",
                "verified_by",
                "verified_at",
                "updated_at",
            ]
        )

        payment_sms.status = IncomingPaymentSMS.Status.CONFIRMED
        payment_sms.save(update_fields=["status"])
        payment_sms.matched_orders.add(order)

    return order


def dismiss_incoming_payment_sms(payment_sms):
    payment_sms.status = IncomingPaymentSMS.Status.DISMISSED
    payment_sms.save(update_fields=["status"])
    return payment_sms


def notify_admins_about_payment_match(payment_sms, orders):
    from store.bots import active_bot_configs, send_to_config

    text = format_payment_match_message(payment_sms, orders)
    sent_count = 0
    for config in active_bot_configs():
        if send_to_config(config, text=text, event_type=BotEventLog.EventType.WEBHOOK):
            sent_count += 1
    return sent_count


def format_payment_match_message(payment_sms, orders):
    sms_time = timezone.localtime(payment_sms.sms_datetime).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "<b>Bank deposit SMS matched</b>",
        "",
        f"<b>SMS ID:</b> <code>{payment_sms.pk}</code>",
        f"<b>Amount:</b> {payment_sms.amount:,} IRR",
        f"<b>Balance:</b> {payment_sms.balance:,} IRR",
        f"<b>SMS time:</b> {escape(sms_time)}",
        "",
        "<b>Candidate orders:</b>",
    ]

    for order in orders:
        customer = order.customer or "-"
        plan = order.plan.name if order.plan_id else "-"
        lines.extend(
            [
                "",
                f"<code>{escape(order.order_tracking_code)}</code>",
                f"Amount: {order.amount:,} {escape(order.currency)}",
                f"Created: {escape(timezone.localtime(order.created_at).strftime('%Y-%m-%d %H:%M:%S %Z'))}",
                f"Status: {escape(order.get_status_display())}",
                f"Plan: {escape(plan)}",
                f"Customer: {escape(str(customer))}",
            ]
        )

    lines.extend(["", "<b>Raw SMS:</b>", f"<pre>{escape(payment_sms.raw_text)}</pre>"])
    return "\n".join(lines)
