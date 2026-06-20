import logging
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    CustomerReward,
    DiscountCode,
    Inbound,
    Operator,
    Order,
    Plan,
    PlanInboundRoute,
    Store,
    VPNClient,
    clean_bank_tracking_code,
    clean_manual_payment_submission,
    generate_order_tracking,
    normalize_payment_digits,
)
from .jalali import TEHRAN_TZ
from .naming import build_client_display_name
from .xui_api import create_inactive_client_details


logger = logging.getLogger(__name__)
MAX_ORDER_QUANTITY = 50
CUSTOM_VOLUME_DURATION_DAYS = 30
CUSTOM_VOLUME_MIN_GB = Decimal("1")
CUSTOM_VOLUME_MAX_GB = Decimal("1000")
CUSTOM_VOLUME_QUANT = Decimal("0.001")
CUSTOM_VOLUME_DEFAULT_GB = Decimal("10")
OPERATOR_REQUIRED_MESSAGE = "برای ثبت سفارش ابتدا اپراتور اینترنت را انتخاب کن."
OPERATOR_INVALID_MESSAGE = "اپراتور انتخاب‌شده معتبر یا فعال نیست."
OPERATOR_NO_PLANS_MESSAGE = "برای این اپراتور فعلاً پلن فعالی تعریف نشده است."
PLAN_OPERATOR_MISMATCH_MESSAGE = "این پلن برای اپراتور انتخاب‌شده فعال نیست."
INBOUND_MISSING_PANEL_MESSAGE = "اینباند انتخاب‌شده به هیچ پنل معتبری وصل نیست. لطفاً تنظیمات سرور را بررسی کن."
INBOUND_INACTIVE_MESSAGE = "اینباند انتخاب‌شده غیرفعال است و نمی‌تواند برای سفارش جدید استفاده شود."
INBOUND_NOT_FOR_NEW_ORDERS_MESSAGE = "اینباند انتخاب‌شده برای فروش یا ساخت کانفیگ جدید مجاز نیست."
INBOUND_PANEL_INACTIVE_MESSAGE = "پنل متصل به اینباند غیرفعال است و نمی‌تواند برای سفارش جدید استفاده شود."
INBOUND_STORE_MISMATCH_MESSAGE = "اینباند انتخاب‌شده به فروشگاه دیگری وصل است."
INBOUND_CAPACITY_MESSAGE = "ظرفیت اینباند انتخاب‌شده برای تعداد کانفیگ درخواستی کافی نیست."
PLAN_INBOUND_ROUTE_MISSING_MESSAGE = "برای این پلن مسیر سرور/اینباند تعریف نشده است. لطفاً با پشتیبانی تماس بگیرید."


@dataclass
class ProvisionedOrderResult:
    success: bool
    message: str
    order: Order | None = None
    vpn_client: VPNClient | None = None
    duplicate_detected: bool = False


DUPLICATE_ORDER_WINDOW_SECONDS = 10 * 60
DUPLICATE_ORDER_LOCK_SECONDS = 90


def get_current_store():
    return Store.objects.filter(is_active=True).first() or Store.objects.first()


def sales_mode_for_store(store):
    return getattr(store, "sales_mode", Store.SalesMode.TUNNEL) or Store.SalesMode.TUNNEL


def sales_mode_requires_operator(store):
    return sales_mode_for_store(store) == Store.SalesMode.OPERATOR_BASED


def get_active_operators(store):
    operators = Operator.objects.filter(is_active=True)
    if store:
        operators = operators.filter(models.Q(store=store) | models.Q(store__isnull=True))
    return operators.order_by("sort_order", "name", "id")


def get_active_operator(store, operator_id):
    if not str(operator_id or "").isdigit():
        return None
    return get_active_operators(store).filter(pk=operator_id).first()


def inbound_panel(inbound):
    if not inbound:
        return None
    try:
        return inbound.panel
    except Exception:
        return None


def validate_inbound_for_order(inbound, store=None, *, required_slots=1):
    required_slots = max(int(required_slots or 1), 1)
    if not inbound:
        raise ValidationError("سرور فعالی برای ساخت کانفیگ پیدا نشد.")
    if not getattr(inbound, "is_active", False):
        raise ValidationError(INBOUND_INACTIVE_MESSAGE)
    if not getattr(inbound, "available_for_new_orders", True):
        raise ValidationError(INBOUND_NOT_FOR_NEW_ORDERS_MESSAGE)

    panel = inbound_panel(inbound)
    if not panel or not getattr(inbound, "panel_id", None):
        raise ValidationError(INBOUND_MISSING_PANEL_MESSAGE)
    if not getattr(panel, "is_active", False):
        raise ValidationError(INBOUND_PANEL_INACTIVE_MESSAGE)
    if store and panel.store_id not in (None, store.pk):
        raise ValidationError(INBOUND_STORE_MISMATCH_MESSAGE)
    if inbound.max_clients is not None and inbound.available_capacity < required_slots:
        raise ValidationError(INBOUND_CAPACITY_MESSAGE)
    return inbound


def get_store_plans(store, *, public_only=True, operator=None):
    plans = Plan.objects.filter(is_active=True)
    if public_only:
        plans = plans.filter(is_public=True, is_custom_volume=False)
    if store:
        plans = plans.filter(models.Q(store=store) | models.Q(store__isnull=True))
    if operator:
        plans = plans.filter(operators=operator)
    return plans.distinct()


def operator_has_available_plans(store, operator):
    if not operator:
        return False
    return custom_volume_is_available(store) or get_store_plans(store, public_only=True, operator=operator).exists()


def validate_operator_for_order(store, plan, operator):
    if not sales_mode_requires_operator(store):
        return None
    if not operator:
        raise ValidationError(OPERATOR_REQUIRED_MESSAGE)
    if not get_active_operators(store).filter(pk=operator.pk).exists():
        raise ValidationError(OPERATOR_INVALID_MESSAGE)
    if not plan or not plan.is_active:
        raise ValidationError("پلن انتخابی معتبر نیست.")
    if not plan.is_available_for_operator(operator):
        raise ValidationError(PLAN_OPERATOR_MISMATCH_MESSAGE)
    return operator


def format_custom_volume_label(volume_gb):
    try:
        number = Decimal(str(volume_gb))
    except (InvalidOperation, TypeError, ValueError):
        return f"{volume_gb} GB"
    normalized = format(number.normalize(), "f")
    label = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
    return f"{label} GB"


def normalize_custom_volume_gb(value):
    raw_value = normalize_payment_digits(value).strip()
    raw_value = raw_value.replace("٫", ".").replace("٬", "")
    if "," in raw_value and "." not in raw_value:
        raw_value = raw_value.replace(",", ".")
    else:
        raw_value = raw_value.replace(",", "")
    try:
        volume_gb = Decimal(raw_value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError("حجم دلخواه را به گیگابایت و به صورت عدد وارد کن.") from exc
    if not volume_gb.is_finite():
        raise ValidationError("حجم دلخواه را به گیگابایت و به صورت عدد وارد کن.")
    if volume_gb < CUSTOM_VOLUME_MIN_GB or volume_gb > CUSTOM_VOLUME_MAX_GB:
        raise ValidationError("حجم دلخواه باید بین ۱ تا ۱۰۰۰ گیگابایت باشد.")
    if volume_gb.as_tuple().exponent < -3:
        raise ValidationError("حجم دلخواه حداکثر تا سه رقم اعشار قابل ثبت است.")
    return volume_gb.quantize(CUSTOM_VOLUME_QUANT)


def calculate_custom_volume_price(volume_gb, price_per_gb):
    raw_price = Decimal(volume_gb or 0) * Decimal(price_per_gb or 0)
    return int(raw_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def store_custom_volume_price_per_gb(store):
    if not store:
        return Decimal("0")
    try:
        price_per_gb = Decimal(store.custom_volume_price_per_gb or 0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return price_per_gb if price_per_gb > 0 else Decimal("0")


def custom_volume_is_available(store):
    return bool(store_custom_volume_price_per_gb(store))


def get_custom_volume_currency(store):
    base_plan = get_store_plans(store, public_only=True).order_by("sort_order", "price", "id").first()
    return base_plan.currency if base_plan else Plan.Currency.TOMAN


def get_custom_volume_device_limit(store):
    base_plan = get_store_plans(store, public_only=True).order_by("sort_order", "price", "id").first()
    return base_plan.device_limit if base_plan else 2


def custom_volume_slug(volume_gb, price_per_gb):
    volume_part = format(volume_gb.normalize(), "f").replace(".", "-")
    price_part = format(Decimal(price_per_gb).quantize(CUSTOM_VOLUME_QUANT).normalize(), "f").replace(".", "-")
    return f"custom-volume-{CUSTOM_VOLUME_DURATION_DAYS}d-{volume_part}gb-{price_part}"


def get_or_create_custom_volume_plan(store, volume_gb, *, operator=None):
    if not store:
        raise ValidationError("فروشگاه فعال برای خرید حجم دلخواه پیدا نشد.")
    volume_gb = normalize_custom_volume_gb(volume_gb)
    price_per_gb = store_custom_volume_price_per_gb(store)
    if not price_per_gb:
        raise ValidationError("خرید حجم دلخواه هنوز فعال نشده است.")

    price = calculate_custom_volume_price(volume_gb, price_per_gb)
    currency = get_custom_volume_currency(store)
    device_limit = get_custom_volume_device_limit(store)
    slug = custom_volume_slug(volume_gb, price_per_gb)
    defaults = {
        "name": f"حجم دلخواه {format_custom_volume_label(volume_gb)}",
        "description": "پلن داخلی ساخته‌شده برای خرید حجم دلخواه.",
        "volume_gb": volume_gb,
        "duration_days": CUSTOM_VOLUME_DURATION_DAYS,
        "price": price,
        "currency": currency,
        "device_limit": device_limit,
        "is_active": True,
        "is_public": False,
        "is_custom_volume": True,
        "sort_order": 9999,
    }
    plan, created = Plan.objects.get_or_create(
        store=store,
        slug=slug,
        defaults=defaults,
    )
    if not created:
        changed_fields = []
        for field, value in defaults.items():
            if getattr(plan, field) != value:
                setattr(plan, field, value)
                changed_fields.append(field)
        if changed_fields:
            plan.save(update_fields=[*changed_fields, "updated_at"])
    if operator and not plan.operators.filter(pk=operator.pk).exists():
        plan.operators.add(operator)
    return plan


def get_available_inbound(store, *, required_slots=1):
    required_slots = max(int(required_slots or 1), 1)
    inbound_qs = Inbound.objects.filter(
        is_active=True,
        available_for_new_orders=True,
        panel_id__isnull=False,
        panel__is_active=True,
    ).select_related("panel")

    if store:
        inbound_qs = inbound_qs.filter(
            models.Q(panel__store=store) | models.Q(panel__store__isnull=True)
        )

    for inbound in inbound_qs.order_by("current_users", "id"):
        try:
            validate_inbound_for_order(inbound, store, required_slots=required_slots)
        except ValidationError:
            logger.warning(
                "Skipping unusable inbound during selection inbound_id=%s panel_id=%s store_id=%s",
                getattr(inbound, "pk", None),
                getattr(inbound, "panel_id", None),
                getattr(store, "pk", None),
            )
            continue
        if inbound.max_clients is None or inbound.available_capacity >= required_slots:
            return inbound
    return None


def store_allows_plan_inbound_routing(store):
    return bool(store and getattr(store, "plan_inbound_routing_enabled", True))


def store_allows_global_inbound_fallback(store):
    return not store or bool(getattr(store, "allow_global_inbound_fallback", True))


def _route_queryset_for_store(route_qs, store):
    if not store:
        return route_qs.annotate(
            store_route_priority=models.Value(1, output_field=models.IntegerField())
        )
    return route_qs.filter(
        models.Q(store=store) | models.Q(store__isnull=True),
        models.Q(inbound__panel__store=store) | models.Q(inbound__panel__store__isnull=True),
    ).annotate(
        store_route_priority=models.Case(
            models.When(store=store, then=0),
            default=1,
            output_field=models.IntegerField(),
        )
    )


def _first_valid_route_inbound(route_qs, store, *, required_slots=1):
    route_qs = route_qs.select_related(
        "store",
        "plan",
        "operator",
        "inbound",
        "inbound__panel",
    ).order_by("store_route_priority", "priority", "inbound__current_users", "id")
    for route in route_qs:
        try:
            return validate_inbound_for_order(route.inbound, store, required_slots=required_slots)
        except ValidationError as exc:
            logger.warning(
                "Skipping invalid plan inbound route route_id=%s plan_id=%s operator_id=%s store_id=%s inbound_id=%s: %s",
                route.pk,
                route.plan_id,
                route.operator_id,
                getattr(store, "pk", None),
                route.inbound_id,
                exc.messages[0],
            )
    return None


def select_inbound_for_plan(plan, store=None, operator=None, purpose="new_order", quantity=1):
    required_slots = max(int(quantity or 1), 1)
    effective_store = store or getattr(plan, "store", None)

    if not store_allows_plan_inbound_routing(effective_store):
        return get_available_inbound(effective_store, required_slots=required_slots)

    base_routes = PlanInboundRoute.objects.filter(
        plan=plan,
        is_active=True,
        inbound__is_active=True,
        inbound__available_for_new_orders=True,
        inbound__panel_id__isnull=False,
        inbound__panel__is_active=True,
    )
    base_routes = _route_queryset_for_store(base_routes, effective_store)

    candidate_sets = []
    if operator:
        candidate_sets.append(base_routes.filter(operator=operator))
    candidate_sets.append(base_routes.filter(operator__isnull=True))

    for route_qs in candidate_sets:
        inbound = _first_valid_route_inbound(route_qs, effective_store, required_slots=required_slots)
        if inbound:
            logger.info(
                "Selected plan inbound route plan_id=%s operator_id=%s store_id=%s inbound_pk=%s purpose=%s quantity=%s",
                getattr(plan, "pk", None),
                getattr(operator, "pk", None),
                getattr(effective_store, "pk", None),
                inbound.pk,
                purpose,
                required_slots,
            )
            return inbound

    if store_allows_global_inbound_fallback(effective_store):
        logger.warning(
            "No plan inbound route found; using global fallback plan_id=%s operator_id=%s store_id=%s purpose=%s quantity=%s",
            getattr(plan, "pk", None),
            getattr(operator, "pk", None),
            getattr(effective_store, "pk", None),
            purpose,
            required_slots,
        )
        return get_available_inbound(effective_store, required_slots=required_slots)

    logger.warning(
        "No plan inbound route found and fallback is disabled plan_id=%s operator_id=%s store_id=%s purpose=%s quantity=%s",
        getattr(plan, "pk", None),
        getattr(operator, "pk", None),
        getattr(effective_store, "pk", None),
        purpose,
        required_slots,
    )
    raise ValidationError(PLAN_INBOUND_ROUTE_MISSING_MESSAGE)


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


def build_duplicate_order_key(
    *,
    store,
    customer,
    plan,
    quantity,
    operator=None,
    sender_card_name,
    sender_card_last4,
    payment_time,
    bank_tracking_code,
):
    parts = [
        str(store.pk if store else ""),
        str(customer.pk if customer else ""),
        str(plan.pk),
        str(operator.pk if operator else ""),
        str(quantity),
        (sender_card_name or "").strip().casefold(),
        str(sender_card_last4 or ""),
        payment_time.strftime("%H:%M") if payment_time else "",
        (bank_tracking_code or "").strip().casefold(),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"order:create:{digest}"


def find_similar_pending_order(
    *,
    store,
    customer,
    plan,
    quantity,
    operator=None,
    sender_card_name,
    sender_card_last4,
    payment_time,
    bank_tracking_code,
):
    since = timezone.now() - timedelta(seconds=DUPLICATE_ORDER_WINDOW_SECONDS)
    qs = (
        Order.objects.select_related("plan", "store")
        .filter(
            plan=plan,
            quantity=quantity,
            status__in=[Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION],
            created_at__gte=since,
        )
        .order_by("-created_at")
    )
    if store:
        qs = qs.filter(store=store)
    if operator:
        qs = qs.filter(operator=operator)
    else:
        qs = qs.filter(operator__isnull=True)
    if customer:
        qs = qs.filter(customer=customer)
    else:
        qs = qs.filter(sender_card_name=sender_card_name, sender_card_last4=sender_card_last4)
    if bank_tracking_code:
        qs = qs.filter(bank_tracking_code=bank_tracking_code)
    elif payment_time:
        qs = qs.filter(payment_date=timezone.localdate(timezone.now(), TEHRAN_TZ), payment_time=payment_time)
    return qs.first()


def mark_duplicate_attempt(order, *, source="", reason="similar_order"):
    now = timezone.now()
    metadata = dict(order.metadata or {})
    duplicate = dict(metadata.get("duplicate_warning") or {})
    duplicate["detected"] = True
    duplicate["reason"] = reason
    duplicate["source"] = source or duplicate.get("source") or ""
    duplicate["attempt_count"] = int(duplicate.get("attempt_count") or 0) + 1
    duplicate["last_attempt_at"] = now.isoformat()
    duplicate["window_seconds"] = DUPLICATE_ORDER_WINDOW_SECONDS
    metadata["duplicate_warning"] = duplicate
    order.metadata = metadata
    order.save(update_fields=["metadata", "updated_at"])


def notify_duplicate_attempt(order):
    try:
        from .telegram_bot.notifications import notify_duplicate_order_attempt

        transaction.on_commit(lambda: notify_duplicate_order_attempt(order))
    except Exception:
        logger.exception("Could not schedule duplicate order bot warning for order=%s", order.pk)


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
    operator=None,
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
        operator=operator,
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


def _create_deferred_order_record(
    *,
    store,
    customer,
    plan,
    inbound,
    operator=None,
    tracking_code,
    username,
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
        operator=operator,
        quantity=quantity,
        original_amount=subtotal,
        amount=subtotal,
        currency=plan.currency,
        username=username,
        inbound=inbound,
        order_tracking_code=tracking_code,
        metadata=metadata,
        **order_defaults,
    )
    order.save()
    return order


def create_manual_payment_order(
    *,
    store,
    customer,
    plan=None,
    operator=None,
    custom_volume_gb=None,
    inbound=None,
    sender_card_name="",
    sender_card_last4="",
    payment_time=None,
    receipt_image=None,
    receipt_text="",
    require_receipt=False,
    require_receipt_image=False,
    bank_tracking_code="",
    discount_code="",
    quantity=1,
    metadata=None,
):
    quantity = validate_order_quantity(quantity)
    order_metadata = dict(metadata or {})
    if store:
        order_metadata.update(
            {
                "payment_destination_card_number": store.card_number,
                "payment_destination_card_owner": store.card_owner,
                "payment_destination_bank_name": store.bank_name or "",
                "payment_destination_receipt_image_only": store.receipt_image_only_payment,
            }
        )
    try:
        cleaned_payment = clean_manual_payment_submission(
            sender_card_name=sender_card_name,
            sender_card_last4=sender_card_last4,
            payment_time=payment_time,
            receipt_image=receipt_image,
            receipt_text=receipt_text,
            require_receipt=require_receipt,
            require_receipt_image=require_receipt_image,
        )
        bank_tracking_code = clean_bank_tracking_code(bank_tracking_code)
    except ValidationError as exc:
        return ProvisionedOrderResult(False, exc.messages[0])

    raw_operator_value = getattr(operator, "pk", operator)
    if sales_mode_requires_operator(store):
        operator = get_active_operator(store, raw_operator_value)
        if not operator:
            return ProvisionedOrderResult(
                False,
                OPERATOR_INVALID_MESSAGE if raw_operator_value else OPERATOR_REQUIRED_MESSAGE,
            )

    if custom_volume_gb not in (None, ""):
        try:
            plan = get_or_create_custom_volume_plan(store, custom_volume_gb, operator=operator)
        except ValidationError as exc:
            return ProvisionedOrderResult(False, exc.messages[0])

    if not plan:
        return ProvisionedOrderResult(False, "پلن انتخابی معتبر نیست.")

    try:
        operator = validate_operator_for_order(store, plan, operator)
    except ValidationError as exc:
        return ProvisionedOrderResult(False, exc.messages[0])
    if not sales_mode_requires_operator(store):
        operator = None

    if inbound:
        try:
            inbound = validate_inbound_for_order(inbound, store, required_slots=quantity)
        except ValidationError as exc:
            logger.warning(
                "Rejected order creation with unusable inbound inbound_pk=%s panel_id=%s store_id=%s: %s",
                getattr(inbound, "pk", None),
                getattr(inbound, "panel_id", None),
                getattr(store, "pk", None),
                exc.messages[0],
            )
            return ProvisionedOrderResult(False, exc.messages[0])

    if getattr(plan, "is_custom_volume", False):
        order_metadata.update(
            {
                "custom_volume": True,
                "custom_volume_gb": str(plan.volume_gb),
                "custom_volume_duration_days": plan.duration_days,
                "custom_volume_price_per_gb": str(store_custom_volume_price_per_gb(store)),
            }
        )

    duplicate_source = (order_metadata.get("source") or "manual_payment").strip()
    duplicate_lock_key = build_duplicate_order_key(
        store=store,
        customer=customer,
        plan=plan,
        operator=operator,
        quantity=quantity,
        sender_card_name=cleaned_payment["sender_card_name"],
        sender_card_last4=cleaned_payment["sender_card_last4"],
        payment_time=cleaned_payment["payment_time"],
        bank_tracking_code=bank_tracking_code,
    )
    duplicate_lookup = {
        "store": store,
        "customer": customer,
        "plan": plan,
        "operator": operator,
        "quantity": quantity,
        "sender_card_name": cleaned_payment["sender_card_name"],
        "sender_card_last4": cleaned_payment["sender_card_last4"],
        "payment_time": cleaned_payment["payment_time"],
        "bank_tracking_code": bank_tracking_code,
    }
    existing_order = find_similar_pending_order(**duplicate_lookup)
    if existing_order:
        mark_duplicate_attempt(existing_order, source=duplicate_source)
        notify_duplicate_attempt(existing_order)
        return ProvisionedOrderResult(
            True,
            "A similar order was already submitted recently.",
            existing_order,
            existing_order.vpn_clients.first(),
            duplicate_detected=True,
        )

    lock_acquired = cache.add(duplicate_lock_key, "1", timeout=DUPLICATE_ORDER_LOCK_SECONDS)
    if not lock_acquired:
        existing_order = find_similar_pending_order(**duplicate_lookup)
        if existing_order:
            mark_duplicate_attempt(existing_order, source=duplicate_source, reason="concurrent_duplicate")
            notify_duplicate_attempt(existing_order)
            return ProvisionedOrderResult(
                True,
                "A similar order was already submitted recently.",
                existing_order,
                existing_order.vpn_clients.first(),
                duplicate_detected=True,
            )
        return ProvisionedOrderResult(
            False,
            "درخواست قبلی هنوز در حال ثبت است. چند لحظه صبر کن؛ دوباره زدن دکمه سفارش تکراری می‌سازد.",
        )

    reserved_discount = None
    try:
        tracking_code = generate_order_tracking()
        username = build_client_display_name(
            customer,
            preferred_name=cleaned_payment["sender_card_name"],
            short_id=tracking_code,
            metadata=order_metadata,
        )
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

        try:
            inbound = inbound or select_inbound_for_plan(
                plan,
                store=store,
                operator=operator,
                purpose="manual_payment_order",
                quantity=quantity,
            )
        except ValidationError as exc:
            release_discount_usage(reserved_discount)
            return ProvisionedOrderResult(False, exc.messages[0])
        if not inbound:
            release_discount_usage(reserved_discount)
            return ProvisionedOrderResult(False, "فعلاً سرور VPN فعالی برای ساخت کانفیگ در دسترس نیست. کمی بعد دوباره تلاش کن.")

        client_result = None
        if inbound:
            try:
                inbound = validate_inbound_for_order(inbound, store, required_slots=quantity)
            except ValidationError as exc:
                release_discount_usage(reserved_discount)
                logger.warning(
                    "Selected inbound became unusable before provisioning inbound_pk=%s panel_id=%s store_id=%s: %s",
                    getattr(inbound, "pk", None),
                    getattr(inbound, "panel_id", None),
                    getattr(store, "pk", None),
                    exc.messages[0],
                )
                return ProvisionedOrderResult(False, exc.messages[0])
            client_result = create_inactive_client_details(
                email_prefix=username,
                total_gb=plan.volume_gb,
                expire_days=plan.duration_days,
                panel=inbound.panel,
                inbound=inbound,
                limit_ip=plan.device_limit,
            )

        if not client_result:
            order_metadata.update(
                {
                    "panel_provisioning_deferred": True,
                    "panel_provisioning_deferred_at": timezone.now().isoformat(),
                    "panel_provisioning_reason": (
                        "panel_unavailable_on_checkout" if inbound else "no_active_inbound_on_checkout"
                    ),
                    "deferred_panel_username": username,
                }
            )

        try:
            with transaction.atomic():
                if client_result:
                    order, vpn_client = _create_inactive_order_records(
                        store=store,
                        customer=customer,
                        plan=plan,
                        operator=operator,
                        inbound=inbound,
                        tracking_code=tracking_code,
                        username=username,
                        client_result=client_result,
                        quantity=quantity,
                        metadata=order_metadata,
                        order_defaults={"status": Order.Status.PENDING_PAYMENT},
                    )
                else:
                    order = _create_deferred_order_record(
                        store=store,
                        customer=customer,
                        plan=plan,
                        operator=operator,
                        inbound=inbound,
                        tracking_code=tracking_code,
                        username=username,
                        quantity=quantity,
                        metadata=order_metadata,
                        order_defaults={"status": Order.Status.PENDING_PAYMENT},
                    )
                    vpn_client = None
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
                    require_receipt_image=require_receipt_image,
                )
                order.bank_tracking_code = bank_tracking_code
                order.save()
                mark_customer_reward_used(order.customer, reserved_discount)
                from .admin_notifications import schedule_notify_admins_new_order

                schedule_notify_admins_new_order(order.pk)
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

        return ProvisionedOrderResult(True, "سفارش ثبت شد و منتظر بررسی پرداخت است.", order, vpn_client)
    finally:
        cache.delete(duplicate_lock_key)


def create_renewal_payment_order(*, customer, vpn_client, metadata=None, discount_code=""):
    plan = vpn_client.plan
    if not plan:
        return ProvisionedOrderResult(False, "این کانفیگ پلن قابل تمدید ندارد.")
    if not vpn_client.order_id:
        return ProvisionedOrderResult(False, "سفارش اصلی این کانفیگ پیدا نشد.")
    if customer and vpn_client.order.customer_id != customer.pk:
        return ProvisionedOrderResult(False, "این کانفیگ برای این حساب نیست.")

    store = vpn_client.store or vpn_client.order.store or plan.store or get_current_store()
    operator = vpn_client.order.operator if vpn_client.order_id else None
    subtotal = plan.price
    discount_amount = 0
    discount_source = Order.DiscountSource.NONE
    wholesale_discount_percent = 0
    reserved_discount = None
    normalized_discount_code = DiscountCode.normalize_code(discount_code)
    if normalized_discount_code:
        try:
            reserved_discount, discount_amount = reserve_discount_usage(
                plan,
                normalized_discount_code,
                customer=customer,
                quantity=1,
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

    order_metadata = {
        "source": "web_dashboard_renewal",
        "renewal": True,
        "renewal_client_pk": vpn_client.pk,
        "renewal_client_public_id": str(vpn_client.public_id),
        "renewal_order_pk": vpn_client.order_id,
    }
    if store:
        order_metadata.update(
            {
                "payment_destination_card_number": store.card_number,
                "payment_destination_card_owner": store.card_owner,
                "payment_destination_bank_name": store.bank_name or "",
                "payment_destination_receipt_image_only": store.receipt_image_only_payment,
            }
        )
    order_metadata.update(metadata or {})

    try:
        with transaction.atomic():
            order = Order(
                store=store,
                customer=customer,
                plan=plan,
                operator=operator,
                quantity=1,
                original_amount=subtotal,
                discount_amount=discount_amount,
                amount=max(subtotal - discount_amount, 0),
                currency=plan.currency,
                username=vpn_client.username,
                uuid=vpn_client.uuid,
                inbound=vpn_client.inbound,
                sub_link=vpn_client.sub_link,
                direct_link=vpn_client.direct_link,
                status=Order.Status.PENDING_PAYMENT,
                discount_source=discount_source,
                metadata=order_metadata,
            )
            if reserved_discount:
                order.discount_code = reserved_discount
                order.discount_code_text = reserved_discount.code
            elif discount_source == Order.DiscountSource.WHOLESALE:
                order.discount_code_text = f"WHOLESALE {wholesale_discount_percent}%"
            order.save()
            mark_customer_reward_used(order.customer, reserved_discount)
            from .admin_notifications import schedule_notify_admins_new_order

            schedule_notify_admins_new_order(order.pk)
    except Exception:
        release_discount_usage(reserved_discount)
        raise
    return ProvisionedOrderResult(True, "Renewal order created and is waiting for payment.", order, vpn_client)


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

    try:
        inbound = inbound or select_inbound_for_plan(
            plan,
            store=store,
            purpose="admin_free_subscription",
        )
    except ValidationError as exc:
        return ProvisionedOrderResult(False, exc.messages[0])
    if not inbound:
        return ProvisionedOrderResult(False, "فعلاً سرور VPN فعالی در دسترس نیست. کمی بعد دوباره تلاش کن.")
    try:
        inbound = validate_inbound_for_order(inbound, store)
    except ValidationError as exc:
        logger.warning(
            "Rejected free subscription with unusable inbound inbound_pk=%s panel_id=%s store_id=%s: %s",
            getattr(inbound, "pk", None),
            getattr(inbound, "panel_id", None),
            getattr(store, "pk", None),
            exc.messages[0],
        )
        return ProvisionedOrderResult(False, exc.messages[0])

    tracking_code = generate_order_tracking()
    username = build_client_display_name(
        customer,
        short_id=tracking_code,
        metadata=metadata,
    )
    client_result = create_inactive_client_details(
        email_prefix=username,
        total_gb=plan.volume_gb,
        expire_days=plan.duration_days,
        panel=inbound.panel,
        inbound=inbound,
        limit_ip=plan.device_limit,
    )
    if not client_result:
        return ProvisionedOrderResult(False, "ساخت کانفیگ روی پنل انجام نشد. لطفاً تنظیمات پنل را بررسی کن.")

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
    return ProvisionedOrderResult(True, "اشتراک رایگان ساخته و فعال شد.", order, vpn_client)
