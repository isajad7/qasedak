import logging
import re
from datetime import timedelta

from django.contrib import messages
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.signing import BadSignature
from django.db import models, transaction
from django.db.models import F
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    CustomerReward,
    DiscountCode,
    Order,
    Plan,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    parse_payment_time,
)
from .order_services import (
    CUSTOM_VOLUME_DEFAULT_GB,
    CUSTOM_VOLUME_DURATION_DAYS,
    CUSTOM_VOLUME_MAX_GB,
    CUSTOM_VOLUME_MIN_GB,
    OPERATOR_NO_PLANS_MESSAGE,
    calculate_custom_volume_price,
    calculate_percentage_discount,
    create_manual_payment_order,
    create_renewal_payment_order,
    custom_volume_is_available,
    format_custom_volume_label,
    get_customer_wholesale_discount_percent,
    get_custom_volume_currency,
    get_active_operator,
    get_active_operators,
    get_available_inbound as service_get_available_inbound,
    get_store_plans as service_get_store_plans,
    normalize_custom_volume_gb,
    operator_has_available_plans,
    sales_mode_requires_operator,
    store_custom_volume_price_per_gb,
    validate_order_quantity,
)
from .jalali import TEHRAN_TZ, format_jalali_chart_label
from .referrals import (
    assign_referrer,
    build_referral_link,
    get_referral_code_from_request,
)
from .referral_services import (
    apply_referral_code,
    get_active_referral_configs,
    get_referral_summary,
    redeem_referral_rewards,
)
from .xui_api import sync_vpn_client_stats


TRACKING_COOKIE_NAME = "order_lookup_key"
TRACKING_COOKIE_SALT = "order-lookup"
LEGACY_TRACKING_COOKIE_NAME = "vpn_tracking_code"
LEGACY_TRACKING_COOKIE_SALT = "vpn-config-lookup"
TRACKING_CODE_RE = re.compile(r"^([A-Za-z0-9]{8}|[0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$")
ORDER_TRACKING_RECOVERY_WINDOW = timedelta(minutes=15)
SUPPORT_MESSAGE_MAX_LENGTH = 2000
SUPPORT_CONTACT_MAX_LENGTH = 120
logger = logging.getLogger(__name__)


def get_current_store():
    return Store.objects.filter(is_active=True).first() or Store.objects.first()


def get_store_plans(store, operator=None):
    return service_get_store_plans(store, public_only=True, operator=operator)


def get_custom_volume_offer(store, plans=None):
    if not custom_volume_is_available(store):
        return None
    price_per_gb = store_custom_volume_price_per_gb(store)
    default_volume = CUSTOM_VOLUME_DEFAULT_GB
    currency = get_custom_volume_currency(store)
    return {
        "price_per_gb": price_per_gb,
        "price_per_gb_raw": format(price_per_gb.normalize(), "f"),
        "default_volume": format(default_volume.normalize(), "f"),
        "default_volume_label": format_custom_volume_label(default_volume),
        "default_price": calculate_custom_volume_price(default_volume, price_per_gb),
        "duration_days": CUSTOM_VOLUME_DURATION_DAYS,
        "currency": currency,
        "min_gb": format(CUSTOM_VOLUME_MIN_GB.normalize(), "f"),
        "max_gb": format(CUSTOM_VOLUME_MAX_GB.normalize(), "f"),
        "card_count": 1,
    }


def plan_card_count(plans, custom_volume_offer=None):
    try:
        count = plans.count()
    except (AttributeError, TypeError):
        count = len(plans or [])
    return count + (1 if custom_volume_offer else 0)


def current_payment_time_value():
    return timezone.localtime(timezone.now(), TEHRAN_TZ).strftime("%H:%M")


def get_available_inbound(store):
    return service_get_available_inbound(store)


def get_current_customer(request):
    return getattr(request, "customer", None)


def customer_has_orders(customer):
    return bool(customer and customer.orders.exists())


def first_time_redirect():
    return redirect(f"{reverse('home')}#plans")


def apply_referral_from_request(request):
    customer = get_current_customer(request)
    referral_code = get_referral_code_from_request(request)
    if customer and referral_code:
        apply_referral_code(customer, referral_code)
    return referral_code


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def is_rate_limited(request, *, scope, limit=10, window=60):
    cache_key = f"rate:{scope}:{get_client_ip(request)}"
    if cache.add(cache_key, 1, timeout=window):
        return False
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=window)
        return False
    return count > limit


def normalize_tracking_code(value):
    code = (value or "").strip()
    if not TRACKING_CODE_RE.fullmatch(code):
        return ""
    if len(code) == 8:
        return code.upper()
    return code.lower()


def get_order_by_tracking_code(tracking_code):
    normalized = normalize_tracking_code(tracking_code)
    if not normalized:
        return None
    return (
        Order.objects.select_related("store", "plan", "operator", "customer")
        .filter(order_tracking_code=normalized)
        .first()
    )


def set_tracking_cookie(response, tracking_code):
    response.set_signed_cookie(
        TRACKING_COOKIE_NAME,
        tracking_code,
        salt=TRACKING_COOKIE_SALT,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
    )
    response.delete_cookie(
        LEGACY_TRACKING_COOKIE_NAME,
        samesite="Lax",
    )
    return response


def get_tracking_code_from_cookie(request):
    cookie_candidates = (
        (TRACKING_COOKIE_NAME, TRACKING_COOKIE_SALT),
        (LEGACY_TRACKING_COOKIE_NAME, LEGACY_TRACKING_COOKIE_SALT),
    )
    for cookie_name, cookie_salt in cookie_candidates:
        try:
            return normalize_tracking_code(
                request.get_signed_cookie(
                    cookie_name,
                    salt=cookie_salt,
                )
            )
        except (KeyError, BadSignature):
            continue
    return ""


def recover_customer_from_tracked_order(request, order):
    if not order:
        return None

    customer = get_current_customer(request)
    if not order.customer_id or (customer and order.customer_id == customer.pk):
        return order

    if customer_has_orders(customer):
        return None

    tracking_cookie = get_tracking_code_from_cookie(request)
    source = str((order.metadata or {}).get("source") or "")
    is_recent_web_order = (
        source.startswith("web_")
        and order.created_at >= timezone.now() - ORDER_TRACKING_RECOVERY_WINDOW
    )
    if tracking_cookie != normalize_tracking_code(order.order_tracking_code) and not is_recent_web_order:
        return None

    request.customer = order.customer
    logger.info(
        "Recovered customer cookie from order tracking code order=%s previous_customer=%s recovered_customer=%s",
        order.pk,
        customer.pk if customer else None,
        order.customer_id,
    )
    return order


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
        raise ValidationError("این کد جایزه برای این حساب در دسترس نیست.")


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
            raise ValidationError("کد تخفیف معتبر نیست.")
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
        raise ValidationError("کد تخفیف معتبر نیست.")
    validate_reward_discount_owner(discount, customer)
    discount.validate_for_plan(plan)
    discount_amount = discount.calculate_discount(subtotal)
    return discount, discount_amount, max(subtotal - discount_amount, 0)


def build_home_context(
    *,
    store,
    plans,
    operators=None,
    selected_operator=None,
    operator_error="",
    selected_plan_id=None,
    order=None,
    checkout_error="",
    payment_time="",
    payment_receipt_text="",
    discount_code="",
    quantity="1",
    custom_volume_gb="",
    referral_code="",
    order_update_error="",
    order_update_saved=False,
    scroll_to="",
):
    if not payment_time:
        payment_time = current_payment_time_value()
    operator_based = sales_mode_requires_operator(store)
    operators = operators if operators is not None else get_active_operators(store) if operator_based else []
    custom_volume_offer = (
        get_custom_volume_offer(store, plans)
        if not operator_based or selected_operator
        else None
    )
    if custom_volume_offer and not custom_volume_gb:
        custom_volume_gb = custom_volume_offer["default_volume"]
    return {
        "store": store,
        "plans": plans,
        "operators": operators,
        "sales_mode": getattr(store, "sales_mode", "tunnel") if store else "tunnel",
        "sales_mode_operator_based": operator_based,
        "selected_operator": selected_operator,
        "selected_operator_id": str(selected_operator.pk if selected_operator else ""),
        "operator_error": operator_error,
        "custom_volume_offer": custom_volume_offer,
        "plan_card_count": plan_card_count(plans, custom_volume_offer),
        "selected_plan_id": str(selected_plan_id or ""),
        "order": order,
        "order_clients": order.get_vpn_clients() if order else [],
        "checkout_error": checkout_error,
        "payment_time": payment_time,
        "payment_receipt_text": payment_receipt_text,
        "discount_code": discount_code,
        "quantity": quantity or "1",
        "custom_volume_gb": custom_volume_gb,
        "referral_code": referral_code,
        "order_update_error": order_update_error,
        "order_update_saved": order_update_saved,
        "scroll_to": scroll_to,
    }


def create_order_from_checkout(
    request,
    *,
    store,
    inbound,
    payment_time,
    plan=None,
    operator=None,
    custom_volume_gb=None,
    discount_code="",
):
    sender_card_name = request.POST.get("sender_card_name", "").strip() or "رسید تصویری"
    try:
        result = create_manual_payment_order(
            store=store,
            customer=get_current_customer(request),
            plan=plan,
            operator=operator,
            custom_volume_gb=custom_volume_gb,
            inbound=inbound,
            sender_card_name=sender_card_name,
            sender_card_last4="",
            payment_time=payment_time,
            receipt_image=request.FILES.get("payment_receipt_image"),
            receipt_text="",
            require_receipt=True,
            require_receipt_image=True,
            discount_code=discount_code,
            quantity="1" if custom_volume_gb not in (None, "") else request.POST.get("quantity", "1"),
            metadata={
                "source": "web_checkout",
                "payment_capture_mode": "receipt_image_only",
            },
        )
    except ValidationError as exc:
        return None, exc.messages[0]
    if not result.success:
        return None, result.message
    return result.order, ""


def home(request):
    referral_code = apply_referral_from_request(request)
    store = get_current_store()
    operator_based = sales_mode_requires_operator(store)
    operators = get_active_operators(store) if operator_based else []
    selected_operator_id = request.GET.get("operator", "")
    selected_operator = get_active_operator(store, selected_operator_id) if operator_based else None
    plans = get_store_plans(store, selected_operator if operator_based else None)
    if operator_based and not selected_operator:
        plans = Plan.objects.none()
    order = None
    scroll_to = ""
    selected_plan_id = request.GET.get("plan", "")

    tracking_code = request.GET.get("order")
    order_update_saved = request.GET.get("updated") == "1"
    if tracking_code:
        order = recover_customer_from_tracked_order(
            request,
            get_order_by_tracking_code(tracking_code),
        )
        if order:
            scroll_to = "confirmation"

    if request.method == "POST":
        selected_operator_id = request.POST.get("operator_id", "")
        selected_operator = get_active_operator(store, selected_operator_id) if operator_based else None
        plans = get_store_plans(store, selected_operator if operator_based else None)
        if operator_based and not selected_operator:
            plans = Plan.objects.none()
        selected_plan_id = request.POST.get("plan_id", "")
        payment_time_value = current_payment_time_value()
        payment_receipt_text_value = ""
        discount_code_value = request.POST.get("discount_code", "")
        quantity_value = request.POST.get("quantity", "1")
        custom_volume_selected = request.POST.get("custom_volume_selected") == "1"
        custom_volume_value = request.POST.get("custom_volume_gb", "").strip()
        referral_code_value = request.POST.get("referral_code", "")
        if referral_code_value:
            assign_referrer(get_current_customer(request), referral_code_value)
        if operator_based and not selected_operator:
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    operators=operators,
                    selected_operator=selected_operator,
                    selected_plan_id=selected_plan_id,
                    checkout_error="برای خرید ابتدا اپراتور اینترنت خود را انتخاب کن.",
                    payment_time=payment_time_value,
                    payment_receipt_text=payment_receipt_text_value,
                    discount_code=discount_code_value,
                    quantity=quantity_value,
                    custom_volume_gb=custom_volume_value,
                    referral_code=referral_code_value or referral_code,
                    scroll_to="plans",
                ),
            )
        if operator_based and selected_operator and not operator_has_available_plans(store, selected_operator):
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    operators=operators,
                    selected_operator=selected_operator,
                    selected_plan_id=selected_plan_id,
                    checkout_error=OPERATOR_NO_PLANS_MESSAGE,
                    payment_time=payment_time_value,
                    payment_receipt_text=payment_receipt_text_value,
                    discount_code=discount_code_value,
                    quantity=quantity_value,
                    custom_volume_gb=custom_volume_value,
                    referral_code=referral_code_value or referral_code,
                    scroll_to="plans",
                ),
            )
        plan = None
        custom_volume_gb = None
        if custom_volume_selected:
            selected_plan_id = "custom"
            quantity_value = "1"
            try:
                custom_volume_gb = normalize_custom_volume_gb(custom_volume_value)
            except ValidationError as exc:
                return render(
                    request,
                    "home.html",
                    build_home_context(
                        store=store,
                        plans=plans,
                        operators=operators,
                        selected_operator=selected_operator,
                        selected_plan_id=selected_plan_id,
                        checkout_error=exc.messages[0],
                        payment_time=payment_time_value,
                        payment_receipt_text=payment_receipt_text_value,
                        discount_code=discount_code_value,
                        quantity=quantity_value,
                        custom_volume_gb=custom_volume_value,
                        referral_code=referral_code_value or referral_code,
                        scroll_to="checkout",
                    ),
                )
        else:
            plan = get_object_or_404(plans, id=selected_plan_id)
        inbound = get_available_inbound(store)

        try:
            payment_time = parse_payment_time(payment_time_value)
        except ValidationError as exc:
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    operators=operators,
                    selected_operator=selected_operator,
                    selected_plan_id=selected_plan_id,
                    checkout_error=exc.messages[0],
                    payment_time=payment_time_value,
                    payment_receipt_text=payment_receipt_text_value,
                    discount_code=discount_code_value,
                    quantity=quantity_value,
                    custom_volume_gb=custom_volume_value,
                    referral_code=referral_code_value or referral_code,
                    scroll_to="checkout",
                ),
            )

        if not inbound:
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    operators=operators,
                    selected_operator=selected_operator,
                    selected_plan_id=selected_plan_id,
                    checkout_error="فعلا ظرفیت جدیدی آزاد نیست. چند دقیقه دیگر دوباره امتحان کن یا به پشتیبانی پیام بده.",
                    payment_time=payment_time_value,
                    payment_receipt_text=payment_receipt_text_value,
                    discount_code=discount_code_value,
                    quantity=quantity_value,
                    custom_volume_gb=custom_volume_value,
                    referral_code=referral_code_value or referral_code,
                    scroll_to="checkout",
                ),
            )

        order, create_error = create_order_from_checkout(
            request,
            store=store,
            inbound=inbound,
            payment_time=payment_time,
            plan=plan,
            operator=selected_operator,
            custom_volume_gb=custom_volume_gb,
            discount_code=discount_code_value,
        )
        if not order:
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    operators=operators,
                    selected_operator=selected_operator,
                    selected_plan_id=selected_plan_id,
                    checkout_error=create_error,
                    payment_time=payment_time_value,
                    payment_receipt_text=payment_receipt_text_value,
                    discount_code=discount_code_value,
                    quantity=quantity_value,
                    custom_volume_gb=custom_volume_value,
                    referral_code=referral_code_value or referral_code,
                    scroll_to="checkout",
                ),
            )

        response = redirect(f"{reverse('order_detail', kwargs={'order_id': order.public_id})}?created=1")
        set_tracking_cookie(response, order.order_tracking_code)
        return response

    response = render(
        request,
        "home.html",
        build_home_context(
            store=store,
            plans=plans,
            operators=operators,
            selected_operator=selected_operator,
            selected_plan_id=selected_plan_id,
            order=order,
            order_update_saved=order_update_saved,
            payment_time=request.GET.get("payment_time", ""),
            discount_code="",
            referral_code=referral_code,
            scroll_to=scroll_to or ("checkout" if selected_plan_id else ""),
        ),
    )
    if order:
        set_tracking_cookie(response, order.order_tracking_code)
    return response


def create_order(request, plan_id):
    return redirect(f"{reverse('home')}?plan={plan_id}#checkout")


def checkout(request, tracking_code):
    customer = get_current_customer(request)
    if not customer_has_orders(customer):
        return first_time_redirect()
    order = get_object_or_404(
        Order.objects.select_related("store", "operator"),
        order_tracking_code=tracking_code,
        customer=customer,
    )

    if request.method == "POST":
        if order.status == Order.Status.COMPLETED:
            return redirect(reverse("order_detail", kwargs={"order_id": order.public_id}))
        try:
            receipt_image_only = bool(order.store and order.store.receipt_image_only_payment)
            existing_receipt_text = (order.metadata or {}).get("receipt_text")
            order.submit_manual_payment(
                sender_card_name="رسید تصویری" if receipt_image_only else request.POST.get("sender_card_name", ""),
                sender_card_last4="",
                payment_time=current_payment_time_value() if receipt_image_only else request.POST.get("payment_time", ""),
                receipt_image=request.FILES.get("payment_receipt_image"),
                receipt_text="" if receipt_image_only else request.POST.get("payment_receipt_text", "").strip(),
                require_receipt=not receipt_image_only and not bool(order.payment_receipt_image or existing_receipt_text),
                require_receipt_image=receipt_image_only and not bool(order.payment_receipt_image),
            )
            order.save()
            from .admin_notifications import schedule_notify_admins_payment_receipt

            schedule_notify_admins_payment_receipt(order.pk)
        except (ValidationError, SuspiciousFileOperation, OSError) as exc:
            if not isinstance(exc, ValidationError):
                logger.exception("Could not update manual payment details for order=%s", order.order_tracking_code)
            store = get_current_store()
            selected_operator = order.operator if sales_mode_requires_operator(store) else None
            plans = get_store_plans(store, selected_operator)
            message = (
                exc.messages[0]
                if isinstance(exc, ValidationError)
                else "تصویر رسید ذخیره نشد. یک تصویر JPG یا PNG سبک‌تر بفرست یا به پشتیبانی پیام بده."
            )
            return render(
                request,
                "home.html",
                build_home_context(
                    store=store,
                    plans=plans,
                    selected_operator=selected_operator,
                    selected_plan_id=order.plan_id,
                    order=order,
                    order_update_error=message,
                    payment_time=request.POST.get("payment_time", ""),
                    payment_receipt_text=request.POST.get("payment_receipt_text", "").strip(),
                    scroll_to="confirmation",
                ),
            )

    return redirect(reverse("order_detail", kwargs={"order_id": order.public_id}))


def build_config_context(order, *, lookup_error="", tracking_code=""):
    config_cards = []
    if order:
        for client in order.get_vpn_clients():
            stats = sync_vpn_client_stats(client)
            config_cards.append({"client": client, "stats": stats})

    return {
        "order": order,
        "tracking_code": tracking_code or (order.order_tracking_code if order else ""),
        "config_cards": config_cards,
        "lookup_error": lookup_error,
    }


def visible_customer_orders(customer):
    return customer.orders.select_related("plan", "operator", "store", "discount_code")


def renewal_state_for(stats):
    if stats.get("is_expired"):
        return True, "تمام شده و آماده تمدید است."

    expiry_at = stats.get("expiry_at")
    if expiry_at:
        remaining_seconds = (expiry_at - timezone.now()).total_seconds()
        if 0 < remaining_seconds <= 7 * 24 * 60 * 60:
            remaining_days = max(int(remaining_seconds // (24 * 60 * 60)), 0)
            return True, f"{remaining_days} روز تا پایان مانده است."

    total = stats.get("total_traffic_bytes", 0) or 0
    remaining = stats.get("remaining_traffic_bytes", 0) or 0
    if total and remaining <= total * 0.15:
        return True, "حجم باقی‌مانده رو به پایان است."

    return False, ""


def my_configurations(request):
    if not customer_has_orders(get_current_customer(request)):
        return first_time_redirect()
    return redirect(f"{reverse('dashboard')}#access")


def latest_support_conversation(customer, store, *, include_closed=True):
    if not customer:
        return None
    conversations = SupportConversation.objects.filter(customer=customer).select_related("store", "customer")
    if store:
        conversations = conversations.filter(models.Q(store=store) | models.Q(store__isnull=True))
    else:
        conversations = conversations.filter(store__isnull=True)
    if not include_closed:
        conversations = conversations.exclude(status=SupportConversation.Status.CLOSED)
    return conversations.order_by("-updated_at", "-created_at").first()


def support_contact_initial_value(customer, conversation=None):
    if conversation and conversation.contact_value:
        return conversation.contact_value
    if not customer:
        return ""
    return customer.phone_number or (f"@{customer.username.lstrip('@')}" if customer.username else "")


def serialize_support_message(message):
    created_at = timezone.localtime(message.created_at).strftime("%Y-%m-%d %H:%M")
    return {
        "id": message.pk,
        "sender": message.sender_type,
        "sender_label": message.get_sender_type_display(),
        "body": message.body,
        "created_at": created_at,
    }


def serialize_support_conversation(conversation):
    if not conversation:
        return None
    return {
        "id": conversation.pk,
        "status": conversation.status,
        "status_label": conversation.get_status_display(),
        "contact_value": conversation.contact_value,
        "updated_at": timezone.localtime(conversation.updated_at).strftime("%Y-%m-%d %H:%M"),
    }


def support(request):
    store = get_current_store()
    customer = get_current_customer(request)
    conversation = latest_support_conversation(customer, store)
    return render(
        request,
        "support.html",
        {
            "customer": customer,
            "conversation": conversation,
            "support_contact_value": support_contact_initial_value(customer, conversation),
        },
    )


@require_GET
def support_messages(request):
    customer = get_current_customer(request)
    if not customer:
        return JsonResponse({"ok": False, "message": "حساب مرورگر پیدا نشد."}, status=403)
    conversation = latest_support_conversation(customer, get_current_store())
    messages_qs = (
        conversation.messages.order_by("created_at", "id")
        if conversation
        else SupportMessage.objects.none()
    )
    return JsonResponse(
        {
            "ok": True,
            "conversation": serialize_support_conversation(conversation),
            "messages": [serialize_support_message(item) for item in messages_qs],
        }
    )


@require_POST
def support_send_message(request):
    customer = get_current_customer(request)
    if not customer:
        return JsonResponse({"ok": False, "message": "حساب مرورگر پیدا نشد."}, status=403)
    if is_rate_limited(request, scope="support_message", limit=8, window=60):
        return JsonResponse({"ok": False, "message": "پیام‌ها زیاد شد. کمی بعد دوباره امتحان کن."}, status=429)

    contact_value = request.POST.get("contact_value", "").strip()
    body = request.POST.get("body", "").strip()
    if not contact_value:
        return JsonResponse({"ok": False, "message": "شماره موبایل یا آیدی تلگرام را وارد کن."}, status=400)
    if len(contact_value) > SUPPORT_CONTACT_MAX_LENGTH:
        return JsonResponse({"ok": False, "message": "فیلد تماس بیش از حد طولانی است."}, status=400)
    if not body:
        return JsonResponse({"ok": False, "message": "متن پیام را وارد کن."}, status=400)
    if len(body) > SUPPORT_MESSAGE_MAX_LENGTH:
        return JsonResponse({"ok": False, "message": "متن پیام باید حداکثر ۲۰۰۰ کاراکتر باشد."}, status=400)

    store = get_current_store()
    with transaction.atomic():
        conversation = latest_support_conversation(customer, store, include_closed=False)
        if not conversation:
            conversation = SupportConversation.objects.create(
                store=store,
                customer=customer,
                contact_value=contact_value,
                status=SupportConversation.Status.OPEN,
            )
        support_message = SupportMessage.objects.create(
            conversation=conversation,
            sender_type=SupportMessage.SenderType.CUSTOMER,
            customer=customer,
            body=body,
            metadata={
                "ip": get_client_ip(request),
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
            },
        )
        conversation.contact_value = contact_value
        conversation.status = SupportConversation.Status.WAITING_ADMIN
        conversation.last_customer_message_at = timezone.now()
        conversation.closed_at = None
        conversation.save(update_fields=["contact_value", "status", "last_customer_message_at", "closed_at", "updated_at"])

    try:
        from .bots import notify_support_message

        notified_count = notify_support_message(conversation, support_message)
    except Exception as exc:
        notified_count = 0
        logger.exception("Could not notify admins about support conversation=%s: %s", conversation.pk, exc)

    messages_qs = conversation.messages.order_by("created_at", "id")
    return JsonResponse(
        {
            "ok": True,
            "message": "پیام ارسال شد.",
            "admin_notified": notified_count > 0,
            "conversation": serialize_support_conversation(conversation),
            "messages": [serialize_support_message(item) for item in messages_qs],
        }
    )


def dashboard(request):
    customer = get_current_customer(request)
    if not customer_has_orders(customer):
        return first_time_redirect()

    orders = [
        order
        for order in visible_customer_orders(customer)
        .prefetch_related("vpn_clients")
        .order_by("-created_at")
        if not (order.metadata or {}).get("customer_hidden")
    ]
    pending_renewals_by_client_pk = {
        int((order.metadata or {}).get("renewal_client_pk"))
        for order in orders
        if (order.metadata or {}).get("renewal_client_pk")
        and order.status
        in {
            Order.Status.PENDING_PAYMENT,
            Order.Status.PENDING_VERIFICATION,
            Order.Status.CONFIRMED,
        }
    }
    access_cards = []
    dashboard_cards = []
    for order in orders:
        if order.status == Order.Status.CANCELLED:
            continue
        clients = list(order.get_vpn_clients())
        if not clients:
            total = order.plan.traffic_limit_bytes if order.plan_id else 0
            dashboard_cards.append(
                {
                    "order": order,
                    "client": None,
                    "stats": {
                        "total_traffic_bytes": total,
                        "used_traffic_bytes": 0,
                        "remaining_traffic_bytes": 0,
                        "is_enabled": False,
                        "is_expired": False,
                        "panel_available": bool(order.inbound_id),
                    },
                    "remaining_percent": 0,
                    "status_label": "در انتظار ساخت",
                    "is_pending": True,
                    "is_expired": False,
                    "panel_available": bool(order.inbound_id),
                }
            )
            continue

        for client in clients:
            stats = sync_vpn_client_stats(client)
            can_renew, renewal_reason = renewal_state_for(stats)
            total = stats.get("total_traffic_bytes", 0) or 0
            remaining = stats.get("remaining_traffic_bytes", 0) or 0
            remaining_percent = min(round((remaining / total) * 100), 100) if total else 0
            card = {
                "order": order,
                "client": client,
                "stats": stats,
                "can_renew": can_renew,
                "renewal_reason": renewal_reason,
                "pending_renewal": client.pk in pending_renewals_by_client_pk,
                "remaining_percent": remaining_percent,
                "status_label": (
                    "تمام شده"
                    if stats.get("is_expired")
                    else "فعال"
                    if stats.get("is_enabled")
                    else "در انتظار تایید"
                ),
                "is_pending": not stats.get("is_enabled") and not stats.get("is_expired"),
                "is_expired": stats.get("is_expired"),
                "panel_available": stats.get("panel_available", True),
            }
            access_cards.append(card)
            dashboard_cards.append(card)

    active_access_cards = [
        item
        for item in access_cards
        if item["stats"].get("is_enabled") and not item["stats"].get("is_expired")
    ]
    total_traffic_bytes = sum(
        item["stats"].get("total_traffic_bytes", 0) or 0 for item in access_cards
    )
    used_traffic_bytes = sum(
        item["stats"].get("used_traffic_bytes", 0) or 0 for item in access_cards
    )
    remaining_traffic_bytes = sum(
        item["stats"].get("remaining_traffic_bytes", 0) or 0 for item in access_cards
    )
    rewards = (
        customer.rewards.select_related("discount_code", "plan", "referral").order_by("-earned_at")
    )
    referral_summary = get_referral_summary(customer, request=request, store=get_current_store())
    referral_active_configs = list(get_active_referral_configs(customer))
    return render(
        request,
        "dashboard.html",
        {
            "customer": customer,
            "orders": orders,
            "access_cards": access_cards,
            "dashboard_cards": dashboard_cards,
            "active_access_cards": active_access_cards,
            "total_traffic_bytes": total_traffic_bytes,
            "used_traffic_bytes": used_traffic_bytes,
            "remaining_traffic_bytes": remaining_traffic_bytes,
            "pending_orders_count": sum(
                1 for order in orders if order.status != Order.Status.COMPLETED
            ),
            "rewards": rewards[:5],
            "referral_link": build_referral_link(request, customer),
            "referral_summary": referral_summary,
            "referral_active_configs": referral_active_configs,
        },
    )


def order_detail(request, order_id):
    order = recover_customer_from_tracked_order(
        request,
        Order.objects.select_related("customer", "plan", "operator", "store").filter(public_id=order_id).first(),
    )
    if not order:
        raise Http404("Order not found.")
    order = get_object_or_404(
        Order.objects.select_related("plan", "operator", "store").prefetch_related("vpn_clients"),
        pk=order.pk,
    )
    response = render(
        request,
        "order_detail.html",
        {
            "order": order,
            "access_clients": order.get_vpn_clients(),
            "show_checkout_notice": request.GET.get("created") == "1",
        },
    )
    set_tracking_cookie(response, order.order_tracking_code)
    return response


@require_POST
def renew_config(request, config_id):
    customer = get_current_customer(request)
    if not customer_has_orders(customer):
        return first_time_redirect()

    vpn_client = get_object_or_404(
        VPNClient.objects.select_related("plan", "store", "order", "order__store", "inbound", "inbound__panel"),
        public_id=config_id,
        order__customer=customer,
    )
    if Order.objects.filter(
        customer=customer,
        metadata__renewal_client_pk=vpn_client.pk,
        status__in=[
            Order.Status.PENDING_PAYMENT,
            Order.Status.PENDING_VERIFICATION,
            Order.Status.CONFIRMED,
        ],
    ).exists():
        messages.info(request, "برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید داری.")
        return redirect(f"{reverse('dashboard')}#access")

    result = create_renewal_payment_order(customer=customer, vpn_client=vpn_client)
    if not result.success:
        messages.error(request, result.message)
        return redirect(f"{reverse('dashboard')}#access")

    messages.success(request, "سفارش تمدید ساخته شد. اطلاعات پرداخت را ثبت کن تا برای تایید ارسال شود.")
    return redirect(f"{reverse('home')}?order={result.order.order_tracking_code}#confirmation")


@require_POST
def redeem_referral_reward(request):
    customer = get_current_customer(request)
    if not customer_has_orders(customer):
        return first_time_redirect()

    active_configs = list(get_active_referral_configs(customer))
    if not active_configs:
        messages.error(request, "برای دریافت هدیه، ابتدا باید یک کانفیگ فعال داشته باشی.")
        return redirect(f"{reverse('dashboard')}#referrals")

    config_id = request.POST.get("config_id", "").strip()
    if config_id:
        vpn_config = next((item for item in active_configs if str(item.public_id) == config_id), None)
        if not vpn_config:
            messages.error(request, "کانفیگ انتخاب‌شده فعال نیست یا پیدا نشد.")
            return redirect(f"{reverse('dashboard')}#referrals")
    elif len(active_configs) == 1:
        vpn_config = active_configs[0]
    else:
        messages.info(request, "برای دریافت هدیه، کانفیگ موردنظر را انتخاب کن.")
        return redirect(f"{reverse('dashboard')}#referrals")

    result = redeem_referral_rewards(customer, vpn_config)
    if result.success:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect(f"{reverse('dashboard')}#referrals")


@require_POST
def delete_order(request, order_id):
    customer = get_current_customer(request)
    if not customer_has_orders(customer):
        return first_time_redirect()

    order = get_object_or_404(
        customer.orders.select_related("plan", "store", "inbound", "inbound__panel").prefetch_related("vpn_clients"),
        public_id=order_id,
    )
    from .order_actions import cancel_order

    result = cancel_order(order)
    if result.success:
        messages.success(request, "سفارش از داشبورد حذف شد.")
    else:
        messages.error(request, result.message)
    return redirect(f"{reverse('dashboard')}#access")


def account(request):
    customer = get_current_customer(request)
    if not customer:
        raise Http404("Account not found.")
    if not customer_has_orders(customer):
        return first_time_redirect()

    referral_code = apply_referral_from_request(request)

    if request.method != "POST":
        return redirect(f"{reverse('dashboard')}#profile")

    username = request.POST.get("username", "").strip()
    phone_number = request.POST.get("phone_number", "").strip()
    referral_code = request.POST.get("referral_code", "").strip()

    if referral_code:
        assign_referrer(customer, referral_code)

    duplicate_username = (
        username
        and customer.__class__.objects.exclude(pk=customer.pk).filter(username=username).exists()
    )
    duplicate_phone = (
        phone_number
        and customer.__class__.objects.exclude(pk=customer.pk).filter(phone_number=phone_number).exists()
    )
    if duplicate_username:
        messages.error(request, "این نام کاربری قبلا ثبت شده است.")
        return redirect(f"{reverse('dashboard')}#profile")
    if duplicate_phone:
        messages.error(request, "این شماره موبایل قبلا ثبت شده است.")
        return redirect(f"{reverse('dashboard')}#profile")

    customer.username = username
    customer.phone_number = phone_number
    customer.display_name = username or phone_number or customer.display_name
    customer.save(update_fields=["username", "phone_number", "display_name", "updated_at"])
    messages.success(request, "اطلاعات حساب ذخیره شد.")
    return redirect(f"{reverse('dashboard')}#profile")


def referral_landing(request, referral_code):
    customer = get_current_customer(request)
    if customer:
        assign_referrer(customer, referral_code)
    return redirect(f"{reverse('home')}?ref={referral_code}#plans")


def get_owned_client_or_404(tracking_code, config_id):
    order = get_order_by_tracking_code(tracking_code)
    if not order:
        raise Http404("Item not found.")
    client = get_object_or_404(
        order.get_vpn_clients(),
        public_id=config_id,
    )
    return order, client


def config_detail(request, tracking_code, config_id):
    if not customer_has_orders(get_current_customer(request)):
        return first_time_redirect()

    order, client = get_owned_client_or_404(tracking_code, config_id)
    stats = sync_vpn_client_stats(client)
    response = render(
        request,
        "config_detail.html",
        {
            "order": order,
            "client": client,
            "stats": stats,
        },
    )
    set_tracking_cookie(response, order.order_tracking_code)
    return response


def serialize_usage_history(stats):
    history = stats.get("history") or [
        {
            "recorded_at": timezone.now(),
            "used_traffic_bytes": stats.get("used_traffic_bytes", 0),
            "remaining_traffic_bytes": stats.get("remaining_traffic_bytes", 0),
            "total_traffic_bytes": stats.get("total_traffic_bytes", 0),
        }
    ]
    labels = []
    used_gb = []
    remaining_gb = []
    total_gb = []
    for item in history:
        recorded_at = item["recorded_at"]
        if isinstance(recorded_at, str):
            label = recorded_at
        else:
            label = format_jalali_chart_label(recorded_at)
        labels.append(label)
        used_gb.append(round(item.get("used_traffic_bytes", 0) / (1024 ** 3), 2))
        remaining_gb.append(round(item.get("remaining_traffic_bytes", 0) / (1024 ** 3), 2))
        total_gb.append(round(item.get("total_traffic_bytes", 0) / (1024 ** 3), 2))
    return {
        "labels": labels,
        "used_gb": used_gb,
        "remaining_gb": remaining_gb,
        "total_gb": total_gb,
    }


def config_usage(request, tracking_code, config_id):
    if not customer_has_orders(get_current_customer(request)):
        return JsonResponse({"ok": False, "error": "سفارش فعالی پیدا نشد."}, status=403)

    if is_rate_limited(request, scope="config_usage", limit=60, window=60):
        return JsonResponse({"ok": False, "error": "درخواست‌ها زیاد شد. کمی بعد دوباره امتحان کن."}, status=429)
    _, client = get_owned_client_or_404(tracking_code, config_id)
    stats = sync_vpn_client_stats(client)
    return JsonResponse(
        {
            "ok": True,
            "status": client.status,
            "panel_available": stats.get("panel_available", False),
            "usage": serialize_usage_history(stats),
        }
    )


@require_GET
def discount_preview(request):
    if is_rate_limited(request, scope="discount_preview", limit=40, window=60):
        return JsonResponse({"ok": False, "message": "چند بار پشت سر هم امتحان شد. کمی بعد دوباره بزن."}, status=429)

    store = get_current_store()
    selected_operator = None
    if sales_mode_requires_operator(store):
        selected_operator = get_active_operator(store, request.GET.get("operator_id", ""))
        if not selected_operator:
            return JsonResponse({"ok": False, "message": "برای بررسی تخفیف ابتدا اپراتور را انتخاب کن."}, status=400)
    plans = get_store_plans(store, selected_operator)
    custom_volume_selected = request.GET.get("custom_volume_selected") == "1"
    plan_id = request.GET.get("plan_id", "")
    plan = None
    code = request.GET.get("code", "")
    if custom_volume_selected:
        try:
            custom_volume_gb = normalize_custom_volume_gb(request.GET.get("custom_volume_gb", ""))
        except ValidationError as exc:
            return JsonResponse({"ok": False, "message": exc.messages[0]}, status=400)
        price_per_gb = store_custom_volume_price_per_gb(store)
        if not price_per_gb:
            return JsonResponse({"ok": False, "message": "خرید حجم دلخواه هنوز فعال نشده است."}, status=400)
        unit_price = calculate_custom_volume_price(custom_volume_gb, price_per_gb)
        currency = get_custom_volume_currency(store)
    else:
        plan = plans.filter(pk=plan_id).first() if str(plan_id).isdigit() else None
        if plan:
            unit_price = plan.price
            currency = plan.currency
    if not custom_volume_selected and not plan:
        return JsonResponse({"ok": False, "message": "اول یک پلن معتبر انتخاب کن."}, status=400)
    try:
        quantity = validate_order_quantity(request.GET.get("quantity", "1"))
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": exc.messages[0]}, status=400)
    subtotal = unit_price * quantity
    if not code.strip():
        wholesale_percent = get_customer_wholesale_discount_percent(get_current_customer(request))
        discount_amount = calculate_percentage_discount(subtotal, wholesale_percent)
        return JsonResponse(
            {
                "ok": True,
                "valid": bool(discount_amount),
                "message": "تخفیف همکاری شما خودکار اعمال شد." if discount_amount else "",
                "source": "wholesale" if discount_amount else "none",
                "original_amount": subtotal,
                "discount_amount": discount_amount,
                "payable_amount": max(subtotal - discount_amount, 0),
            }
        )

    try:
        if custom_volume_selected:
            discount = DiscountCode.objects.filter(code=DiscountCode.normalize_code(code)).first()
            if not discount:
                raise ValidationError("کد تخفیف معتبر نیست.")
            validate_reward_discount_owner(discount, get_current_customer(request))
            discount.validate_basic()
            if discount.applicable_plans.exists():
                raise ValidationError("این کد تخفیف برای حجم دلخواه فعال نیست.")
            discount_amount = discount.calculate_discount(subtotal)
            payable_amount = max(subtotal - discount_amount, 0)
        else:
            discount, discount_amount, payable_amount = preview_discount(
                plan,
                code,
                customer=get_current_customer(request),
                quantity=quantity,
            )
    except ValidationError as exc:
        return JsonResponse(
            {
                "ok": True,
                "valid": False,
                "message": exc.messages[0],
                "original_amount": subtotal,
                "discount_amount": 0,
                "payable_amount": subtotal,
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "valid": True,
            "message": f"کد {discount.code} اعمال شد.",
            "source": "manual",
            "code": discount.code,
            "original_amount": subtotal,
            "discount_amount": discount_amount,
            "payable_amount": payable_amount,
            "currency": currency,
        }
    )
