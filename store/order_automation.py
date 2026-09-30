"""Order receipt follow-up, timed approval and its subsequent human reconciliation."""
import logging
from datetime import timedelta
from html import escape

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .db_locking import select_for_update_self
from .models import BotConfiguration, BotEventLog, BotUser, Order, OrderAutomation, Store, SubscriptionCup, VPNClient

logger = logging.getLogger(__name__)
AUTO_APPROVE_DELAY = timedelta(minutes=5)
REMINDER_MINUTES = (5, 15, 30)


class OrderAutomationSettingsForm(forms.ModelForm):
    class Meta:
        model = Store
        fields = ("order_review_reminders_enabled", "order_auto_approve_enabled")


def pending_receipts(store=None):
    qs = Order.objects.filter(
        payment_method=Order.PaymentMethod.MANUAL_CARD,
        payment_submitted_at__isnull=False,
        verification_status=Order.VerificationStatus.PENDING,
        status__in=(Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION),
        store__is_active=True,
    ).filter(
        (Q(payment_receipt_image__isnull=False) & ~Q(payment_receipt_image=""))
        | (Q(bank_tracking_code__isnull=False) & ~Q(bank_tracking_code=""))
        | (Q(metadata__has_key="receipt_text") & ~Q(metadata__receipt_text="") & ~Q(metadata__receipt_text=None))
        | (Q(metadata__receipt__file_id__isnull=False) & ~Q(metadata__receipt__file_id="") & ~Q(metadata__receipt__file_id=None))
    )
    return qs.filter(store=store) if store else qs


def auto_approve_order(order_id, *, now=None):
    from .provisioning_services import approve_and_provision_order
    from .telegram_bot.notifications import notify_order_event

    now = now or timezone.now()
    with transaction.atomic():
        order = select_for_update_self(Order.objects.select_related("plan", "store")).get(pk=order_id)
        store = Store.objects.get(pk=order.store_id)
        if not store.order_auto_approve_enabled or not store.order_auto_approve_enabled_at:
            return False
        if not pending_receipts(store).filter(pk=order.pk).exists():
            return False
        # Turning the feature on gives existing receipts a fresh five-minute review window.
        due = max(order.payment_submitted_at, store.order_auto_approve_enabled_at) + AUTO_APPROVE_DELAY
        if now < due or (order.metadata or {}).get("suppress_new_order_notification"):
            return False
        audit, _ = OrderAutomation.objects.get_or_create(order=order)
        if audit.auto_approved_at:
            return False
        audit.auto_approved_at = now
        audit.review_status = OrderAutomation.ReviewStatus.PENDING
        audit.save()
    # Commit the claim BEFORE remote side effects: crashes/timeouts must not repeat a renewal automatically.
    try:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order_id)
            result = approve_and_provision_order(order, source="timed_receipt_approval", notify=False)
            order.refresh_from_db()
            if result.ok and not getattr(result, "already_provisioned", False):
                try:
                    notify_order_event(order, event_type="approved")
                except Exception:
                    logger.exception("Timed approval delivery notification failed order_id=%s", order_id)
    except Exception:
        logger.exception("Timed provisioning requires manual reconciliation order_id=%s", order_id)
        OrderAutomation.objects.filter(pk=audit.pk).update(last_error="نتیجه تحویل نامشخص است؛ پیش از تلاش مجدد سرویس روی پنل را بررسی کنید.")
        return True
    if not result.ok:
        OrderAutomation.objects.filter(pk=audit.pk).update(last_error="تحویل خودکار کامل نشد؛ نتیجه و خطای تحویل سفارش را بررسی کنید.")
    return True


def reminder_due(order, audit, now):
    stage = audit.reminder_stage
    if stage < len(REMINDER_MINUTES):
        return now >= order.payment_submitted_at + timedelta(minutes=REMINDER_MINUTES[stage])
    return not audit.last_reminded_at or now >= audit.last_reminded_at + timedelta(hours=1)


def send_receipt_reminders(store_id, *, now=None):
    from .telegram_bot.notifications import active_bot_configs, send_to_config

    now = now or timezone.now()
    with transaction.atomic():
        store = Store.objects.select_for_update().get(pk=store_id)
        if not store.is_active or not store.order_review_reminders_enabled:
            return 0
        due = []
        failed_auto = Order.objects.filter(store=store, automation__review_status="pending", automation__last_error__gt="", payment_submitted_at__isnull=False).exclude(status__in=(Order.Status.COMPLETED, Order.Status.REJECTED, Order.Status.CANCELLED))
        orders = select_for_update_self(pending_receipts(store) | failed_auto).order_by("automation__last_reminded_at", "payment_submitted_at", "pk")[:100]
        for order in orders:
            if (order.metadata or {}).get("suppress_new_order_notification"):
                continue
            audit, _ = OrderAutomation.objects.get_or_create(order=order)
            if (not audit.auto_approved_at or audit.last_error) and reminder_due(order, audit, now):
                due.append((order, audit))
            if len(due) >= 10:
                break
        if not due:
            return 0
        lines = ["⏳ <b>سفارش‌های منتظر بررسی پرداخت / تحویل</b>"]
        keyboard = []
        for order, audit in due:
            minutes = max(0, int((now - order.payment_submitted_at).total_seconds() // 60))
            lines.append(f"• {escape(order.order_tracking_code)} — {minutes} دقیقه انتظار")
            keyboard.append([{"text": f"بررسی سفارش #{order.pk}", "callback_data": f"order:detail:{order.order_tracking_code}"}])
        sent = 0
        for config in active_bot_configs(store=store).filter(notify_new_orders=True):
            sent += send_to_config(config, text="\n".join(lines), event_type=BotEventLog.EventType.WEBHOOK, reply_markup={"inline_keyboard": keyboard})
        if not sent:
            return 0
        for order, audit in due:
            elapsed = (now - order.payment_submitted_at).total_seconds() / 60
            audit.reminder_stage = max(audit.reminder_stage, sum(elapsed >= value for value in REMINDER_MINUTES))
            audit.last_reminded_at = now
            audit.save(update_fields=["reminder_stage", "last_reminded_at", "updated_at"])
        return len(due)


def confirm_auto_payment(order_id, *, user, note=""):
    from .referral_services import create_referral_reward_for_order

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        audit = OrderAutomation.objects.select_for_update().get(order=order)
        if not audit.auto_approved_at or audit.review_status not in (OrderAutomation.ReviewStatus.PENDING, OrderAutomation.ReviewStatus.VERIFIED):
            raise ValidationError("این تأیید خودکار در وضعیت قابل تطبیق نیست.")
        if order.status in (Order.Status.REJECTED, Order.Status.CANCELLED):
            raise ValidationError("سفارش رد یا لغو شده است.")
        if audit.review_status == OrderAutomation.ReviewStatus.VERIFIED:
            return
        audit.review_status = OrderAutomation.ReviewStatus.VERIFIED
        audit.reviewed_at = timezone.now()
        audit.reviewed_by = user
        audit.review_note = note.strip()
        audit.save()
        order.verification_status = Order.VerificationStatus.VERIFIED
        order.is_paid = True
        order.verified_by = user
        # Preserve the original activation time; reconciliation has its own audit timestamp.
        order.verified_at = order.verified_at or audit.reviewed_at
        order.save(update_fields=["verification_status", "is_paid", "verified_by", "verified_at", "updated_at"])
        create_referral_reward_for_order(order)


def cancellation_clients(order):
    # Renewals also lock the VPNClient: wait for any in-flight renewal before checking its order.
    clients = list(select_for_update_self(order.vpn_clients.select_related("inbound__panel")).order_by("pk"))
    audit = getattr(order, "automation", None)
    approved_at = (audit.auto_approved_at if audit else None) or order.verified_at or order.created_at
    renewal_id = (order.metadata or {}).get("renewal_client_pk")
    if renewal_id:
        client = select_for_update_self(VPNClient.objects.select_related("order", "inbound__panel")).filter(pk=renewal_id).first()
        if not client or client.store_id != order.store_id or not client.order_id or client.order.customer_id != order.customer_id:
            raise ValidationError("سرویس مقصد تمدید با مشتری و فروشگاه سفارش مطابقت ندارد.")
        if client.pk not in [item.pk for item in clients]:
            clients.append(client)
    for client in clients:
        newer = Order.objects.filter(
            Q(metadata__renewal_client_pk=client.pk) | Q(metadata__renewal_client_pk=str(client.pk)),
            status=Order.Status.COMPLETED,
            verified_at__gt=approved_at,
        ).exclude(pk=order.pk)
        if newer.exists():
            raise ValidationError("این سرویس بعداً دوباره تمدید شده؛ توقف آن نیازمند بررسی خرید بعدی است.")
    return clients


def cancel_auto_approved_order(order_id, *, user, reason):
    from .vpn_client_management_services import set_vpn_client_enabled_by_admin

    if not reason.strip():
        raise ValidationError("دلیل لغو را وارد کنید؛ این متن برای مشتری ارسال می‌شود.")
    with transaction.atomic():
        order = select_for_update_self(Order.objects.select_related("store", "plan")).get(pk=order_id)
        audit = OrderAutomation.objects.select_for_update().get(order=order)
        if not audit.auto_approved_at:
            raise ValidationError("این سفارش تأیید خودکار ندارد.")
        if audit.review_status == OrderAutomation.ReviewStatus.CANCELLED:
            return True
        clients = cancellation_clients(order)
        audit.review_status = OrderAutomation.ReviewStatus.CANCELLING
        audit.review_note = reason.strip()
        audit.reviewed_by = user
        audit.reviewed_at = timezone.now()
        audit.last_error = ""
        audit.save()
        # Close project subscriptions immediately; do not edit shared upstream configs.
        cup_query = Q(order=order)
        if clients:
            cup_query |= Q(vpn_client_id__in=[client.pk for client in clients])
        SubscriptionCup.objects.filter(cup_query).update(status=SubscriptionCup.Status.DISABLED, updated_at=timezone.now())
        for client in clients:
            if client.pk in audit.revoked_client_ids:
                continue
            try:
                if not client.inbound_id or not client.inbound.panel_id:
                    raise ValidationError("سرویس پنل معتبر ندارد.")
                panel = client.inbound.panel
                identifier = (client.xui_email or client.username) if str(panel.family).lower() == "pasarguard" else (client.uuid or client.xui_email or client.username)
                payload = {"panel_id": panel.pk, "inbound_id": client.inbound.inbound_id,
                           "node_id": client.xui_node_id or client.inbound.xui_node_id,
                           "identifier": str(identifier or ""), "vpn_client_id": client.pk}
                set_vpn_client_enabled_by_admin(f"web-admin:{user.pk}", payload, enabled=False)
                audit.revoked_client_ids.append(client.pk)
                audit.save(update_fields=["revoked_client_ids", "updated_at"])
            except Exception:
                logger.exception("Auto-approved order cancellation incomplete order_id=%s client_id=%s", order.pk, client.pk)
                audit.review_status = OrderAutomation.ReviewStatus.CANCEL_FAILED
                audit.last_error = "ساب متوقف شد، ولی غیرفعال‌سازی همه سرویس‌ها روی پنل کامل نشد. پس از رفع خطا دوباره لغو را بزنید."
                audit.save(update_fields=["review_status", "last_error", "updated_at"])
                return False
        order.status = Order.Status.CANCELLED
        order.verification_status = Order.VerificationStatus.REJECTED
        order.is_paid = False
        order.rejection_reason = reason.strip()
        order.save(update_fields=["status", "verification_status", "is_paid", "rejection_reason", "updated_at"])
        audit.review_status = OrderAutomation.ReviewStatus.CANCELLED
        audit.save(update_fields=["review_status", "updated_at"])
    send_cancellation_notice(order_id)
    return True


def send_cancellation_notice(order_id, *, now=None):
    from .telegram_bot.notifications import notify_order_event

    now = now or timezone.now()
    with transaction.atomic():
        audit = OrderAutomation.objects.select_for_update().get(order_id=order_id)
        if audit.review_status != OrderAutomation.ReviewStatus.CANCELLED or audit.cancellation_notification_status in ("sent", "no_target"):
            return False
        if audit.cancellation_notification_attempted_at and now < audit.cancellation_notification_attempted_at + timedelta(minutes=5):
            return False
        order = Order.objects.select_related("plan", "store", "customer").get(pk=order_id)
        targets = BotUser.objects.filter(customer_id=order.customer_id, is_active=True,
                                        bot_config__is_active=True, bot_config__provider=BotConfiguration.Provider.TELEGRAM).exclude(chat_id="")
        if order.store_id:
            targets = targets.filter(Q(bot_config__store_id=order.store_id) | Q(bot_config__store__isnull=True))
        if not order.customer_id or not targets.exists():
            audit.cancellation_notification_status = "no_target"
            audit.save(update_fields=["cancellation_notification_status", "updated_at"])
            return False
        audit.cancellation_notification_attempted_at = now
        audit.save(update_fields=["cancellation_notification_attempted_at", "updated_at"])
    try:
        sent = notify_order_event(order, event_type="cancelled")
    except Exception:
        logger.exception("Cancellation message failed order_id=%s", order_id)
        sent = 0
    OrderAutomation.objects.filter(pk=audit.pk).update(
        cancellation_notification_status="sent" if sent else "failed",
        cancellation_notified_at=now if sent else None,
    )
    return bool(sent)


def process_order_automation(*, now=None, limit=100):
    now = now or timezone.now()
    summary = {"approved": 0, "reminded": 0, "notified": 0, "failed": 0}
    # The timestamp is a claim as well as an audit: a failed provisioning attempt requires human review.
    candidates = pending_receipts().filter(store__order_auto_approve_enabled=True, automation__auto_approved_at__isnull=True)
    for order_id in candidates.order_by("payment_submitted_at", "pk").values_list("pk", flat=True)[:limit]:
        try:
            summary["approved"] += int(auto_approve_order(order_id, now=now))
        except Exception:
            logger.exception("Timed order approval failed order_id=%s", order_id)
            summary["failed"] += 1
    for store_id in Store.objects.filter(is_active=True).values_list("pk", flat=True):
        try:
            summary["reminded"] += send_receipt_reminders(store_id, now=now)
            Store.objects.filter(pk=store_id).update(order_automation_last_run_at=now)
        except Exception:
            logger.exception("Order follow-up failed store_id=%s", store_id)
            summary["failed"] += 1
    cancelled = OrderAutomation.objects.filter(review_status=OrderAutomation.ReviewStatus.CANCELLED).exclude(cancellation_notification_status__in=("sent", "no_target"))
    for order_id in cancelled.values_list("order_id", flat=True)[:limit]:
        summary["notified"] += int(send_cancellation_notice(order_id, now=now))
    return summary
