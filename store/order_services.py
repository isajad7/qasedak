import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    CustomerReward,
    DiscountCode,
    Inbound,
    Order,
    Plan,
    Store,
    VPNClient,
    clean_bank_tracking_code,
    clean_manual_payment_submission,
    generate_order_tracking,
)
from .jalali import TEHRAN_TZ
from .xui_api import build_config_email_prefix, create_inactive_client_details


logger = logging.getLogger(__name__)
MAX_ORDER_QUANTITY = 50


@dataclass
class ProvisionedOrderResult:
    success: bool
    message: str
    order: Order | None = None
    vpn_client: VPNClient | None = None


def get_current_store():
    return Store.objects.filter(is_active=True).first() or Store.objects.first()


def get_store_plans(store, *, public_only=True):
    plans = Plan.objects.filter(is_active=True)
    if public_only:
        plans = plans.filter(is_public=True)
    if store:
        plans = plans.filter(models.Q(store=store) | models.Q(store__isnull=True))
    return plans


def get_available_inbound(store):
    inbound_qs = Inbound.objects.filter(
        is_active=True,
        panel__is_active=True,
    ).select_related("panel")

    if store:
        inbound_qs = inbound_qs.filter(
            models.Q(panel__store=store) | models.Q(panel__store__isnull=True)
        )

    for inbound in inbound_qs.order_by("current_users", "id"):
        if inbound.has_capacity:
            return inbound
    return None


def validate_order_quantity(quantity=1):
    if quantity in (None, ""):
        return 1
    try:
        numeric_quantity = Decimal(str(quantity).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("Quantity must be a whole number.") from exc
    if not numeric_quantity.is_finite() or numeric_quantity != numeric_quantity.to_integral_value():
        raise ValidationError("Quantity must be a whole number.")
    quantity = int(numeric_quantity)
    if quantity <= 0 or quantity > MAX_ORDER_QUANTITY:
        raise ValidationError("Quantity must be between 1 and 50.")
    return quantity


def calculate_percentage_discount(amount, percent):
    if amount <= 0 or not percent:
        return 0
    percent = max(min(int(percent), 100), 0)
    return min((amount * percent) // 100, amount)


def get_customer_wholesale_discount_percent(customer):
    if not customer:
        return 0
    if hasattr(customer, "wholesale_discount_percent"):
        return customer.wholesale_discount_percent
    if not getattr(customer, "is_wholesale", False):
        return 0
    return max(min(int(getattr(customer, "default_discount_percent", 0) or 0), 100), 0)


def validate_reward_discount_owner(discount, customer, *, lock=False):
    reward_qs = CustomerReward.objects.filter(discount_code=discount)
    if lock:
        reward_qs = reward_qs.select_for_update()
    if not reward_qs.exists():
        return
    if not customer or not reward_qs.filter(
        customer=customer,
        status=CustomerReward.Status.AVAILABLE,
    ).exists():
        raise ValidationError("This reward code is not available for this account.")


def mark_customer_reward_used(customer, discount):
    if not customer or not discount:
        return
    CustomerReward.objects.filter(
        customer=customer,
        discount_code=discount,
        status=CustomerReward.Status.AVAILABLE,
    ).update(
        status=CustomerReward.Status.USED,
        used_at=timezone.now(),
        updated_at=timezone.now(),
    )


def reserve_discount_usage(plan, code, *, customer=None, quantity=1):
    quantity = validate_order_quantity(quantity)
    subtotal = plan.price * quantity
    normalized_code = DiscountCode.normalize_code(code)
    if not normalized_code:
        return None, 0

    with transaction.atomic():
        discount = (
            DiscountCode.objects.select_for_update()
            .filter(code=normalized_code)
            .first()
        )
        if not discount:
            raise ValidationError("Discount code is invalid.")
        validate_reward_discount_owner(discount, customer, lock=True)
        discount.validate_for_plan(plan)
        discount_amount = discount.calculate_discount(subtotal)
        DiscountCode.objects.filter(pk=discount.pk).update(
            used_count=F("used_count") + 1,
            updated_at=timezone.now(),
        )
        discount.used_count += 1
        return discount, discount_amount


def release_discount_usage(discount):
    if discount:
        DiscountCode.objects.filter(pk=discount.pk, used_count__gt=0).update(
            used_count=F("used_count") - 1,
            updated_at=timezone.now(),
        )


def preview_discount(plan, code, *, customer=None, quantity=1):
    quantity = validate_order_quantity(quantity)
    subtotal = plan.price * quantity
    normalized_code = DiscountCode.normalize_code(code)
    if not normalized_code:
        return None, 0, subtotal
    discount = DiscountCode.objects.filter(code=normalized_code).first()
    if not discount:
        raise ValidationError("Discount code is invalid.")
    validate_reward_discount_owner(discount, customer)
    discount.validate_for_plan(plan)
    discount_amount = discount.calculate_discount(subtotal)
    return discount, discount_amount, max(subtotal - discount_amount, 0)


def _create_inactive_order_records(
    *,
    store,
    customer,
    plan,
    inbound,
    tracking_code,
    username,
    client_result,
    quantity=1,
    metadata=None,
    order_defaults=None,
):
    order_defaults = order_defaults or {}
    metadata = metadata or {}
    subtotal = plan.price * quantity
    order = Order(
        store=store,
        customer=customer,
        plan=plan,
        quantity=quantity,
        original_amount=subtotal,
        amount=subtotal,
        currency=plan.currency,
        username=username,
        uuid=client_result["uuid"],
        inbound=inbound,
        sub_link=client_result["sub_link"],
        direct_link=client_result["direct_link"],
        order_tracking_code=tracking_code,
        metadata=metadata,
        **order_defaults,
    )
    order.save()

    vpn_client = VPNClient.objects.create(
        store=store,
        order=order,
        plan=plan,
        inbound=inbound,
        username=username,
        xui_email=client_result["email"],
        uuid=client_result["uuid"],
        sub_id=client_result["sub_id"],
        sub_link=client_result["sub_link"],
        direct_link=client_result["direct_link"],
        status=VPNClient.Status.INACTIVE,
        traffic_limit_bytes=plan.traffic_limit_bytes,
        duration_days=plan.duration_days,
        device_limit=plan.device_limit,
        xui_raw=client_result.get("raw", {}),
    )
    Inbound.objects.filter(pk=inbound.pk).update(current_users=F("current_users") + 1)
    return order, vpn_client


def create_manual_payment_order(
    *,
    store,
    customer,
    plan,
    inbound=None,
    sender_card_name="",
    sender_card_last4="",
    payment_time=None,
    receipt_image=None,
    receipt_text="",
    require_receipt=False,
    bank_tracking_code="",
    discount_code="",
    quantity=1,
    metadata=None,
):
    quantity = validate_order_quantity(quantity)
    try:
        cleaned_payment = clean_manual_payment_submission(
            sender_card_name=sender_card_name,
            sender_card_last4=sender_card_last4,
            payment_time=payment_time,
            receipt_image=receipt_image,
            receipt_text=receipt_text,
            require_receipt=require_receipt,
        )
        bank_tracking_code = clean_bank_tracking_code(bank_tracking_code)
    except ValidationError as exc:
        return ProvisionedOrderResult(False, exc.messages[0])

    inbound = inbound or get_available_inbound(store)
    if not inbound:
        return ProvisionedOrderResult(False, "No active VPN server is available right now. Please try again later.")

    tracking_code = generate_order_tracking()
    username = build_config_email_prefix(cleaned_payment["sender_card_name"], plan.volume_gb, tracking_code)
    reserved_discount = None
    discount_amount = 0
    discount_source = Order.DiscountSource.NONE
    wholesale_discount_percent = 0
    normalized_discount_code = DiscountCode.normalize_code(discount_code)
    subtotal = plan.price * quantity

    if normalized_discount_code:
        try:
            reserved_discount, discount_amount = reserve_discount_usage(
                plan,
                normalized_discount_code,
                customer=customer,
                quantity=quantity,
            )
        except ValidationError as exc:
            return ProvisionedOrderResult(False, exc.messages[0])
        if reserved_discount:
            discount_source = Order.DiscountSource.MANUAL
    else:
        wholesale_discount_percent = get_customer_wholesale_discount_percent(customer)
        if wholesale_discount_percent:
            discount_amount = calculate_percentage_discount(subtotal, wholesale_discount_percent)
            if discount_amount:
                discount_source = Order.DiscountSource.WHOLESALE

    client_result = create_inactive_client_details(
        email_prefix=username,
        total_gb=plan.volume_gb,
        expire_days=plan.duration_days,
        panel=inbound.panel,
        inbound=inbound,
        limit_ip=plan.device_limit,
    )
    if not client_result:
        release_discount_usage(reserved_discount)
        return ProvisionedOrderResult(False, "Could not create the VPN client on the panel. Please contact support.")

    try:
        with transaction.atomic():
            order, vpn_client = _create_inactive_order_records(
                store=store,
                customer=customer,
                plan=plan,
                inbound=inbound,
                tracking_code=tracking_code,
                username=username,
                client_result=client_result,
                quantity=quantity,
                metadata=dict(metadata or {}),
                order_defaults={"status": Order.Status.PENDING_PAYMENT},
            )
            if reserved_discount:
                order.apply_discount(
                    reserved_discount,
                    discount_amount,
                    source=discount_source,
                )
            elif discount_source == Order.DiscountSource.WHOLESALE:
                order.apply_wholesale_discount(wholesale_discount_percent)
            order.submit_manual_payment(
                sender_card_name=cleaned_payment["sender_card_name"],
                sender_card_last4=cleaned_payment["sender_card_last4"],
                payment_time=cleaned_payment["payment_time"],
                receipt_image=cleaned_payment["receipt_image"],
                receipt_text=cleaned_payment["receipt_text"],
                require_receipt=require_receipt,
            )
            order.bank_tracking_code = bank_tracking_code
            order.save()
            mark_customer_reward_used(order.customer, reserved_discount)
    except ValidationError as exc:
        release_discount_usage(reserved_discount)
        return ProvisionedOrderResult(False, exc.messages[0])
    except OSError:
        logger.exception("Could not save manual payment receipt image.")
        release_discount_usage(reserved_discount)
        return ProvisionedOrderResult(
            False,
            "Could not save the receipt image. Please try a smaller JPG or PNG image, or contact support.",
        )
    except Exception:
        release_discount_usage(reserved_discount)
        raise

    return ProvisionedOrderResult(True, "Order created and is waiting for verification.", order, vpn_client)


def grant_free_subscription(
    *,
    store,
    customer,
    plan,
    inbound=None,
    granted_by=None,
    reason="",
    metadata=None,
    notify_customer=True,
):
    from .order_actions import activate_order

    inbound = inbound or get_available_inbound(store)
    if not inbound:
        return ProvisionedOrderResult(False, "No active VPN server is available right now. Please try again later.")

    tracking_code = generate_order_tracking()
    payer_label = customer.display_name if customer else "free"
    username = build_config_email_prefix(payer_label, plan.volume_gb, tracking_code)
    client_result = create_inactive_client_details(
        email_prefix=username,
        total_gb=plan.volume_gb,
        expire_days=plan.duration_days,
        panel=inbound.panel,
        inbound=inbound,
        limit_ip=plan.device_limit,
    )
    if not client_result:
        return ProvisionedOrderResult(False, "Could not create the VPN client on the panel. Please contact support.")

    now = timezone.now()
    grant_metadata = {
        "source": "admin_free",
        "free_grant": True,
        "free_grant_reason": reason,
        "suppress_new_order_notification": True,
        "suppress_admin_order_updates": True,
        "suppress_customer_notification": not notify_customer,
    }
    grant_metadata.update(metadata or {})

    with transaction.atomic():
        order, vpn_client = _create_inactive_order_records(
            store=store,
            customer=customer,
            plan=plan,
            inbound=inbound,
            tracking_code=tracking_code,
            username=username,
            client_result=client_result,
            metadata=grant_metadata,
            order_defaults={
                "status": Order.Status.PENDING_PAYMENT,
                "payment_method": Order.PaymentMethod.ADMIN_FREE,
                "is_paid": True,
                "payment_submitted_at": now,
                "payment_date": timezone.localdate(now, TEHRAN_TZ),
            },
        )
        order.discount_amount = plan.price
        order.amount = 0
        order.save(update_fields=["discount_amount", "amount", "metadata", "updated_at"])

    result = activate_order(order, user=granted_by, notify=True)
    order.refresh_from_db()
    vpn_client.refresh_from_db()
    if not result.success:
        return ProvisionedOrderResult(False, result.message, order, vpn_client)
    return ProvisionedOrderResult(True, "Free subscription granted and activated.", order, vpn_client)
