import logging
import re

from django.core.exceptions import ValidationError

from store.jalali import persian_digits
from store.models import BotUser, DiscountCode, Order
from store.order_services import (
    CUSTOM_VOLUME_DURATION_DAYS,
    CUSTOM_VOLUME_MAX_GB,
    CUSTOM_VOLUME_MIN_GB,
    MAX_ORDER_QUANTITY,
    OPERATOR_INVALID_MESSAGE,
    OPERATOR_NO_PLANS_MESSAGE,
    OPERATOR_REQUIRED_MESSAGE,
    calculate_percentage_discount,
    custom_volume_is_available,
    get_active_operator,
    get_active_operators,
    get_current_store,
    get_custom_volume_currency,
    get_customer_wholesale_discount_percent,
    get_or_create_custom_volume_plan,
    get_store_plans,
    normalize_custom_volume_gb,
    operator_has_available_plans,
    preview_discount,
    sales_mode_requires_operator,
    store_custom_volume_price_per_gb,
    validate_order_quantity,
)

from .constants import PAYMENT_NAME_CALLBACK, PAYMENT_NAME_LABEL, PAYMENT_RECEIPT_LABEL
from .formatting import bot_money, bot_volume_label, normalize_bot_number
from .services_flow import bot_client_label
from .user_menu import main_menu_keyboard

logger = logging.getLogger(__name__)


def _upsell_context(config, bot_user, *, chat_id="", store=None, operator=None, plan=None, quantity=1, pricing=None):
    data = bot_user.state_data or {}
    if data.get("flow") == "renewal":
        return None
    store = store or config.store or get_current_store()
    if operator is None:
        operator = get_purchase_operator_from_state(store, data)
    if plan is None:
        plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        return None
    return {
        "bot_user": bot_user,
        "chat_id": chat_id,
        "bot_config": config,
        "store": store,
        "operator": operator,
        "selected_plan": plan,
        "plan": plan,
        "quantity": quantity or data.get("quantity", 1) or 1,
        "pricing": pricing or {},
        "discount_active": bool(data.get("discount_code")),
        "discount_code": data.get("discount_code") or "",
        "source": "telegram_buy_flow",
    }


def _emit_upsell_event(event_type, config, bot_user, *, chat_id="", context=None):
    if context is None:
        context = _upsell_context(config, bot_user, chat_id=chat_id)
    if not context:
        return None
    try:
        from store.revenue_engine.upsell.triggers import safe_emit_event

        return safe_emit_event(event_type, bot_user, context)
    except Exception as exc:
        logger.warning("Upsell hook skipped event=%s bot_user=%s: %s", event_type, bot_user.pk, exc)
        return None


def _emit_plan_selected_upsell(config, bot_user, *, chat_id, store, operator, plan):
    try:
        from store.revenue_engine.upsell.triggers import LOW_PRICE_PLAN_SELECTED, USER_PLAN_SELECTED

        context = _upsell_context(
            config,
            bot_user,
            chat_id=chat_id,
            store=store,
            operator=operator,
            plan=plan,
            quantity=(bot_user.state_data or {}).get("quantity", 1),
        )
        _emit_upsell_event(USER_PLAN_SELECTED, config, bot_user, chat_id=chat_id, context=context)
        _emit_upsell_event(LOW_PRICE_PLAN_SELECTED, config, bot_user, chat_id=chat_id, context=context)
    except Exception as exc:
        logger.warning("Plan-selected upsell hook skipped bot_user=%s plan=%s: %s", bot_user.pk, getattr(plan, "pk", None), exc)


def _emit_checkout_upsell(config, bot_user, *, chat_id, store, operator, plan, quantity, pricing):
    try:
        from store.revenue_engine.upsell.triggers import CHECKOUT_STARTED

        context = _upsell_context(
            config,
            bot_user,
            chat_id=chat_id,
            store=store,
            operator=operator,
            plan=plan,
            quantity=quantity,
            pricing=pricing,
        )
        return _emit_upsell_event(CHECKOUT_STARTED, config, bot_user, chat_id=chat_id, context=context)
    except Exception as exc:
        logger.warning("Checkout upsell hook skipped bot_user=%s: %s", bot_user.pk, exc)
        return None


def purchase_summary_keyboard(*, has_discount=False):
    discount_label = "تغییر کد تخفیف" if has_discount else "وارد کردن کد تخفیف"
    rows = [
        [
            {"text": discount_label, "callback_data": "user:discount:start"},
            {"text": PAYMENT_RECEIPT_LABEL, "callback_data": "user:buy_confirm"},
        ],
    ]
    if has_discount:
        rows.append([{"text": "حذف تخفیف", "callback_data": "user:discount:skip"}])
    rows.append([{"text": PAYMENT_NAME_LABEL, "callback_data": PAYMENT_NAME_CALLBACK}])
    rows.append(
        [
            {"text": "برگشت", "callback_data": "user:buy_back_plans"},
            {"text": "لغو", "callback_data": "user:cancel"},
        ],
    )
    return {"inline_keyboard": rows}


def discount_code_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "رد شدن از کد تخفیف", "callback_data": "user:discount:skip"}],
            [{"text": "برگشت", "callback_data": "user:buy_back_summary"}],
            [{"text": "لغو", "callback_data": "user:cancel"}],
        ]
    }


def quantity_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": persian_digits(1), "callback_data": "user:buyqty:1"},
                {"text": persian_digits(2), "callback_data": "user:buyqty:2"},
                {"text": persian_digits(3), "callback_data": "user:buyqty:3"},
            ],
            [
                {"text": persian_digits(5), "callback_data": "user:buyqty:5"},
                {"text": persian_digits(10), "callback_data": "user:buyqty:10"},
            ],
            [
                {"text": "برگشت", "callback_data": "user:buy_back_plans"},
                {"text": "لغو", "callback_data": "user:cancel"},
            ],
        ]
    }


def operator_keyboard(operators):
    rows = []
    for operator in operators:
        rows.append([{"text": operator.name, "callback_data": f"user:buyop:{operator.pk}"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def custom_volume_prompt_text(store):
    price_per_gb = store_custom_volume_price_per_gb(store)
    currency = get_custom_volume_currency(store)
    return (
        "حجم دلخواه ۳۰ روزه\n"
        "━━━━━━━━━━━━━━\n"
        f"قیمت هر گیگ: {bot_money(price_per_gb, currency)}\n"
        f"حجم را به گیگابایت، بین {persian_digits(CUSTOM_VOLUME_MIN_GB)} تا {persian_digits(CUSTOM_VOLUME_MAX_GB)} ارسال کنید."
    )


def plan_button_label(plan):
    return (
        f"{bot_volume_label(plan.volume_gb)} | "
        f"{persian_digits(plan.duration_days)} روزه | "
        f"{bot_money(plan.price, plan.currency)}"
    )


def plan_keyboard(plans, *, prefix="user:buyplan", custom_volume=False, operator=None):
    rows = []
    operator_suffix = f":op:{operator.pk}" if operator else ""
    for plan in plans:
        rows.append(
            [
                {
                    "text": plan_button_label(plan)[:64],
                    "callback_data": f"{prefix}:{plan.pk}{operator_suffix}",
                }
            ]
        )
    if custom_volume:
        custom_callback = f"user:buycustom:{operator.pk}" if operator else "user:buycustom"
        rows.append([{"text": "حجم دلخواه ۳۰ روزه", "callback_data": custom_callback}])
    back_callback = "user:buy_back_ops" if operator else "user:menu"
    rows.append([{"text": "بازگشت", "callback_data": back_callback}])
    return {"inline_keyboard": rows}


def format_operator_lines(operators):
    return "🌐 اپراتور خود را انتخاب کنید:"


def format_plan_lines(plans, *, store=None, custom_volume=False, operator=None):
    if operator:
        return f"🛒 پلن‌های {operator.name}\n\nیکی از پلن‌های زیر را انتخاب کنید:"
    return "🛒 خرید سرویس\n\nیکی از پلن‌های زیر را انتخاب کنید:"


def bot_discount_error_message(message):
    text = str(message or "")
    if "invalid" in text.lower():
        return "کد تخفیف معتبر نیست."
    if "not active" in text.lower():
        return "کد تخفیف فعال نیست."
    if "not valid right now" in text.lower():
        return "کد تخفیف در حال حاضر معتبر نیست."
    if "usage limit" in text.lower():
        return "ظرفیت استفاده از این کد تخفیف تمام شده است."
    if "not available for this account" in text.lower():
        return "این کد هدیه برای حساب شما قابل استفاده نیست."
    return text or "کد تخفیف قابل اعمال نیست."


def calculate_bot_pricing(plan, quantity=1, *, customer=None, discount_code=""):
    quantity = validate_order_quantity(quantity)
    original_amount = plan.price * quantity
    discount_code = DiscountCode.normalize_code(discount_code)
    if discount_code:
        discount, discount_amount, payable_amount = preview_discount(
            plan,
            discount_code,
            customer=customer,
            quantity=quantity,
        )
        return {
            "original_amount": original_amount,
            "discount_amount": discount_amount,
            "payable_amount": payable_amount,
            "discount_code": discount.code if discount else discount_code,
            "discount_label": f"کد تخفیف {discount.code if discount else discount_code}",
            "discount_source": Order.DiscountSource.MANUAL,
        }

    wholesale_percent = get_customer_wholesale_discount_percent(customer)
    discount_amount = calculate_percentage_discount(original_amount, wholesale_percent)
    return {
        "original_amount": original_amount,
        "discount_amount": discount_amount,
        "payable_amount": max(original_amount - discount_amount, 0),
        "discount_code": "",
        "discount_label": f"تخفیف همکاری {persian_digits(wholesale_percent)}٪" if discount_amount else "",
        "discount_source": Order.DiscountSource.WHOLESALE if discount_amount else Order.DiscountSource.NONE,
    }


def pricing_from_state(plan, quantity=1, *, customer=None, data=None):
    data = data or {}
    discount_code = data.get("discount_code") or ""
    if discount_code and data.get("discount_amount") not in (None, ""):
        original_amount = plan.price * validate_order_quantity(quantity)
        discount_amount = int(data.get("discount_amount") or 0)
        return {
            "original_amount": original_amount,
            "discount_amount": discount_amount,
            "payable_amount": max(original_amount - discount_amount, 0),
            "discount_code": DiscountCode.normalize_code(discount_code),
            "discount_label": f"کد تخفیف {DiscountCode.normalize_code(discount_code)}",
            "discount_source": Order.DiscountSource.MANUAL,
        }
    return calculate_bot_pricing(plan, quantity, customer=customer, discount_code=discount_code)


def format_purchase_summary(store, plan, *, quantity=1, operator=None, pricing=None, flow="purchase", vpn_client=None):
    quantity = validate_order_quantity(quantity)
    pricing = pricing or pricing_from_state(plan, quantity)
    title = "✅ تخفیف اعمال شد" if pricing.get("discount_amount") else ("✅ خلاصه تمدید" if flow == "renewal" else "✅ خلاصه سفارش")
    lines = [
        title,
        "",
    ]
    if vpn_client:
        lines.append(f"سرویس: {bot_client_label(vpn_client)}")
    lines.extend(
        [
            f"پلن: {plan.name}",
            f"حجم/مدت: {bot_volume_label(plan.volume_gb)} / {persian_digits(plan.duration_days)} روز",
        ]
    )
    if operator:
        lines.append(f"اپراتور: {operator.name}")
    if quantity > 1:
        lines.append(f"تعداد کانفیگ: {persian_digits(quantity)}")
    if pricing.get("discount_amount"):
        lines.extend(
            [
                f"مبلغ اولیه: {bot_money(pricing['original_amount'], plan.currency)}",
                f"تخفیف: {bot_money(pricing['discount_amount'], plan.currency)}",
                f"مبلغ نهایی: {bot_money(pricing['payable_amount'], plan.currency)}",
            ]
        )
    else:
        lines.append(f"مبلغ: {bot_money(pricing['payable_amount'], plan.currency)}")
    lines.extend(["", "در صورت داشتن کد تخفیف، آن را وارد کنید."])
    return "\n".join(lines)


def parse_buy_plan_callback(data):
    match = re.fullmatch(r"user:buyplan:(\d+)(?::op:(\d+))?", data or "")
    if not match:
        return None, None
    plan_id, operator_id = match.groups()
    return int(plan_id), int(operator_id) if operator_id else None


def parse_buy_custom_callback(data):
    if data == "user:buycustom":
        return True, None
    match = re.fullmatch(r"user:buycustom:(\d+)", data or "")
    if not match:
        return False, None
    return True, int(match.group(1))


def get_purchase_operator_from_state(store, data):
    if not sales_mode_requires_operator(store):
        return None
    return get_active_operator(store, (data or {}).get("operator_id"))


def send_operator_list(client, store, *, chat_id):
    operators = list(get_active_operators(store))
    if not operators:
        client.send_message("در حال حاضر اپراتور فعالی برای خرید تعریف نشده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}
    client.send_message(
        format_operator_lines(operators),
        chat_id=chat_id,
        reply_markup=operator_keyboard(operators),
    )
    return {"ok": True, "handled": True, "operator_select": True}


def send_plan_list(client, config, *, chat_id, buy_mode=False, operator=None):
    store = config.store or get_current_store()
    if sales_mode_requires_operator(store):
        if operator is None:
            return send_operator_list(client, store, chat_id=chat_id)
        if not get_active_operators(store).filter(pk=operator.pk).exists():
            client.send_message(OPERATOR_INVALID_MESSAGE, chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}

    plans = list(get_store_plans(store, public_only=True, operator=operator))
    custom_volume = custom_volume_is_available(store)
    if operator and not operator_has_available_plans(store, operator):
        client.send_message(OPERATOR_NO_PLANS_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "handled": True}
    if not plans and not custom_volume:
        client.send_message("در حال حاضر پلن فعالی برای خرید وجود ندارد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "handled": True}

    client.send_message(
        format_plan_lines(plans, store=store, custom_volume=custom_volume, operator=operator),
        chat_id=chat_id,
        reply_markup=plan_keyboard(plans, custom_volume=custom_volume, operator=operator),
    )
    return {"ok": True, "handled": True, "buy_mode": buy_mode}


def get_selected_purchase_plan(store, plan_id, *, operator=None):
    plan = get_store_plans(store, public_only=True, operator=operator).filter(pk=plan_id).first()
    if plan:
        return plan
    return get_store_plans(store, public_only=False, operator=operator).filter(pk=plan_id, is_custom_volume=True).first()


def purchase_context_from_state(config, bot_user):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        return store, None, None, None, OPERATOR_REQUIRED_MESSAGE
    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        return store, operator, None, None, "پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید."
    try:
        quantity = validate_order_quantity(data.get("quantity", 1))
    except ValidationError:
        quantity = 1
    return store, operator, plan, quantity, ""


def send_purchase_summary(client, config, bot_user, *, chat_id):
    store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}
    try:
        pricing = pricing_from_state(plan, quantity, customer=bot_user.customer, data=bot_user.state_data)
    except ValidationError as exc:
        pricing = calculate_bot_pricing(plan, quantity, customer=bot_user.customer)
        data = bot_user.state_data or {}
        data.pop("discount_code", None)
        data.pop("discount_amount", None)
        data.pop("payable_amount", None)
        bot_user.state_data = data
        bot_user.save(update_fields=["state_data", "updated_at"])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id)
    data = bot_user.state_data or {}
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    _emit_checkout_upsell(
        config,
        bot_user,
        chat_id=chat_id,
        store=store,
        operator=operator,
        plan=plan,
        quantity=quantity,
        pricing=pricing,
    )
    client.send_message(
        format_purchase_summary(
            store,
            plan,
            quantity=quantity,
            operator=operator,
            pricing=pricing,
            flow=data.get("flow") or "purchase",
        ),
        chat_id=chat_id,
        reply_markup=purchase_summary_keyboard(has_discount=bool(data.get("discount_code"))),
    )
    return {"ok": True, "handled": True}


def send_current_order_summary(client, config, bot_user, *, chat_id, send_renewal_summary_func):
    if (bot_user.state_data or {}).get("flow") == "renewal":
        return send_renewal_summary_func(client, config, bot_user, chat_id=chat_id)
    return send_purchase_summary(client, config, bot_user, chat_id=chat_id)


def start_discount_code_flow(client, config, bot_user, *, chat_id):
    data = bot_user.state_data or {}
    if not data.get("plan_id"):
        client.send_message("ابتدا یک پلن انتخاب کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}
    data["step"] = "discount"
    bot_user.state = "buy_wait_discount"
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "کد تخفیف را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=discount_code_keyboard(),
    )
    return {"ok": True, "handled": True}


def apply_discount_code_for_bot(
    client,
    config,
    bot_user,
    code,
    *,
    chat_id,
    renewal_context_from_state_func,
    send_renewal_summary_func,
):
    data = bot_user.state_data or {}
    code = DiscountCode.normalize_code(code)
    if not code:
        client.send_message("کد تخفیف خالی است. کد را ارسال کنید یا از این مرحله رد شوید.", chat_id=chat_id, reply_markup=discount_code_keyboard())
        return {"ok": True, "success": False}

    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state_func(config, bot_user)
        quantity = 1
    else:
        store, operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}

    try:
        pricing = calculate_bot_pricing(plan, quantity, customer=bot_user.customer, discount_code=code)
    except ValidationError as exc:
        logger.info("Bot discount preview failed code=%s user=%s error=%s", code, bot_user.pk, exc.messages[0])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id, reply_markup=discount_code_keyboard())
        return {"ok": True, "success": False}

    data["discount_code"] = pricing["discount_code"]
    data["discount_amount"] = int(pricing["discount_amount"])
    data["payable_amount"] = int(pricing["payable_amount"])
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(f"کد تخفیف {pricing['discount_code']} اعمال شد.", chat_id=chat_id)
    return send_current_order_summary(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        send_renewal_summary_func=send_renewal_summary_func,
    )


def skip_discount_for_bot(client, config, bot_user, *, chat_id, show_payment_step_func):
    data = bot_user.state_data or {}
    data.pop("discount_code", None)
    data.pop("discount_amount", None)
    data.pop("payable_amount", None)
    data["step"] = "payment"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return show_payment_step_func(client, config, bot_user, chat_id=chat_id)


def start_purchase_flow(client, config, bot_user, plan_id, *, chat_id, operator_id=None):
    store = config.store or get_current_store()
    operator = None
    if sales_mode_requires_operator(store):
        operator = get_active_operator(store, operator_id)
        if not operator:
            client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
            bot_user.reset_state()
            return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_store_plans(store, public_only=True, operator=operator).filter(pk=plan_id).first()
    if not plan:
        client.send_message("این پلن پیدا نشد یا دیگر فعال نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Plan not found."}

    bot_user.state = BotUser.State.BUY_WAIT_QUANTITY
    bot_user.state_data = {
        "flow": "purchase",
        "plan_id": plan.pk,
        "operator_id": operator.pk if operator else "",
        "step": "quantity",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    _emit_plan_selected_upsell(config, bot_user, chat_id=chat_id, store=store, operator=operator, plan=plan)
    client.send_message(
        (
            f"پلن انتخابی: {plan.name}\n"
            f"قیمت هر کانفیگ: {bot_money(plan.price, plan.currency)}\n\n"
            f"تعداد کانفیگ را انتخاب کنید یا عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} بفرستید."
        ),
        chat_id=chat_id,
        reply_markup=quantity_keyboard(),
    )
    return {"ok": True, "handled": True}


def continue_purchase_after_quantity(client, config, bot_user, quantity, *, chat_id):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Plan not found."}

    data["quantity"] = quantity
    data["step"] = "summary"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return send_purchase_summary(client, config, bot_user, chat_id=chat_id)


def start_custom_volume_flow(client, config, bot_user, *, chat_id, operator_id=None):
    store = config.store or get_current_store()
    operator = None
    if sales_mode_requires_operator(store):
        operator = get_active_operator(store, operator_id)
        if not operator:
            client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
            bot_user.reset_state()
            return {"ok": True, "success": False, "error": "Operator required."}
    if not custom_volume_is_available(store):
        client.send_message("خرید حجم دلخواه هنوز فعال نشده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False}
    bot_user.state = BotUser.State.BUY_WAIT_CUSTOM_VOLUME
    bot_user.state_data = {
        "flow": "purchase",
        "operator_id": operator.pk if operator else "",
        "step": "custom_volume",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(custom_volume_prompt_text(store), chat_id=chat_id, reply_markup=cancel_keyboard())
    return {"ok": True, "handled": True}


def continue_purchase_after_custom_volume(client, config, bot_user, volume_value, *, chat_id):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        bot_user.reset_state()
        return {"ok": True, "success": False, "error": "Operator required."}

    try:
        volume_gb = normalize_custom_volume_gb(volume_value)
        plan = get_or_create_custom_volume_plan(store, volume_gb, operator=operator)
    except ValidationError as exc:
        client.send_message(exc.messages[0], chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False}

    bot_user.state = BotUser.State.BUY_WAIT_QUANTITY
    bot_user.state_data = {
        "flow": "purchase",
        "plan_id": plan.pk,
        "operator_id": operator.pk if operator else "",
        "custom_volume_gb": str(volume_gb),
        "step": "quantity",
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        (
            f"حجم انتخابی: {bot_volume_label(volume_gb)}\n"
            f"مدت: {persian_digits(CUSTOM_VOLUME_DURATION_DAYS)} روز\n"
            f"قیمت هر کانفیگ: {bot_money(plan.price, plan.currency)}\n\n"
            f"تعداد کانفیگ را انتخاب کنید یا عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} بفرستید."
        ),
        chat_id=chat_id,
        reply_markup=quantity_keyboard(),
    )
    return {"ok": True, "handled": True}


def cancel_keyboard():
    return {"inline_keyboard": [[{"text": "انصراف", "callback_data": "user:cancel"}]]}


def is_buy_callback_data(data):
    data = str(data or "")
    if data in {
        "user:buy_confirm",
        "user:discount:start",
        "user:discount:skip",
        "user:buy_back_summary",
        "user:buy_back_ops",
        "user:buy_back_plans",
        "user:plans",
        "user:buy",
        "user:buycustom",
    }:
        return True
    return data.startswith(("user:buyop:", "user:buyplan:", "user:buyqty:", "user:buycustom:"))


def handle_buy_callback(
    client,
    config,
    bot_user,
    data,
    *,
    chat_id,
    show_payment_step_func,
    send_renewal_summary_func,
    renewal_context_from_state_func,
):
    if data == "user:buy_confirm":
        return show_payment_step_func(client, config, bot_user, chat_id=chat_id)
    if data == "user:discount:start":
        return start_discount_code_flow(client, config, bot_user, chat_id=chat_id)
    if data == "user:discount:skip":
        return skip_discount_for_bot(
            client,
            config,
            bot_user,
            chat_id=chat_id,
            show_payment_step_func=show_payment_step_func,
        )
    if data == "user:buy_back_summary":
        return send_current_order_summary(
            client,
            config,
            bot_user,
            chat_id=chat_id,
            send_renewal_summary_func=send_renewal_summary_func,
        )
    if data == "user:buy_back_ops":
        return send_operator_list(client, config.store or get_current_store(), chat_id=chat_id)
    if data == "user:buy_back_plans":
        state_data = bot_user.state_data or {}
        store = config.store or get_current_store()
        operator = get_purchase_operator_from_state(store, state_data)
        if sales_mode_requires_operator(store) and not operator:
            return send_operator_list(client, store, chat_id=chat_id)
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True, operator=operator)
    if data == "user:plans":
        return send_plan_list(client, config, chat_id=chat_id)
    if data == "user:buy":
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True)
    if data.startswith("user:buyop:"):
        operator_id = data.rsplit(":", 1)[-1]
        operator = get_active_operator(config.store or get_current_store(), operator_id)
        if not operator:
            client.send_message(OPERATOR_INVALID_MESSAGE, chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        return send_plan_list(client, config, chat_id=chat_id, buy_mode=True, operator=operator)
    is_custom_callback, custom_operator_id = parse_buy_custom_callback(data)
    if is_custom_callback:
        return start_custom_volume_flow(client, config, bot_user, chat_id=chat_id, operator_id=custom_operator_id)
    if data.startswith("user:buyplan:"):
        plan_id, operator_id = parse_buy_plan_callback(data)
        if not plan_id:
            client.send_message("شناسه پلن نامعتبر است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        return start_purchase_flow(client, config, bot_user, plan_id, chat_id=chat_id, operator_id=operator_id)
    if data.startswith("user:buyqty:"):
        raw_quantity = data.rsplit(":", 1)[-1]
        try:
            quantity = validate_order_quantity(normalize_bot_number(raw_quantity))
        except ValidationError:
            client.send_message(
                f"تعداد باید عددی بین ۱ تا {persian_digits(MAX_ORDER_QUANTITY)} باشد.",
                chat_id=chat_id,
                reply_markup=quantity_keyboard(),
            )
            return {"ok": True, "success": False}
        return continue_purchase_after_quantity(client, config, bot_user, quantity, chat_id=chat_id)
    return {"ok": True, "ignored": True}
