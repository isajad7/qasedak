from django.core.exceptions import ValidationError

from store.models import BotUser, Order
from store.order_services import get_current_store

from .buy_flow import (
    bot_discount_error_message,
    calculate_bot_pricing,
    format_purchase_summary,
    pricing_from_state,
    purchase_summary_keyboard,
)
from .payments import format_payment_prompt
from .services_flow import client_config_keyboard, get_bot_client
from .user_menu import main_menu_keyboard


PENDING_RENEWAL_STATUSES = {
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
}


def pending_renewal_order(bot_user, vpn_client):
    if not bot_user.customer_id or not vpn_client:
        return None
    return (
        Order.objects.select_related("plan", "operator", "store")
        .filter(
            customer=bot_user.customer,
            metadata__renewal_client_pk=vpn_client.pk,
            status__in=PENDING_RENEWAL_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )


def renewal_payment_prompt(store, vpn_client, *, pricing=None):
    plan = vpn_client.plan
    return format_payment_prompt(
        store,
        plan,
        quantity=1,
        operator=vpn_client.order.operator if vpn_client.order_id else None,
        pricing=pricing,
        flow="renewal",
        vpn_client=vpn_client,
    )


def renewal_context_from_state(config, bot_user):
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        return None, None, None, "این کانفیگ پیدا نشد. لطفا دوباره تمدید را شروع کنید."
    if not vpn_client.plan_id:
        return vpn_client, None, None, "این کانفیگ پلن قابل تمدید ندارد."
    store = vpn_client.store or vpn_client.order.store or vpn_client.plan.store or config.store or get_current_store()
    return vpn_client, store, vpn_client.plan, ""


def send_renewal_summary(client, config, bot_user, *, chat_id):
    vpn_client, store, plan, error = renewal_context_from_state(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": error}
    try:
        pricing = pricing_from_state(plan, 1, customer=bot_user.customer, data=bot_user.state_data)
    except ValidationError as exc:
        pricing = calculate_bot_pricing(plan, 1, customer=bot_user.customer)
        data = bot_user.state_data or {}
        data.pop("discount_code", None)
        data.pop("discount_amount", None)
        data.pop("payable_amount", None)
        bot_user.state_data = data
        bot_user.save(update_fields=["state_data", "updated_at"])
        client.send_message(bot_discount_error_message(exc.messages[0]), chat_id=chat_id)
    data = bot_user.state_data or {}
    data["step"] = "summary"
    data["flow"] = "renewal"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    operator = vpn_client.order.operator if vpn_client.order_id else None
    client.send_message(
        format_purchase_summary(
            store,
            plan,
            quantity=1,
            operator=operator,
            pricing=pricing,
            flow="renewal",
            vpn_client=vpn_client,
        ),
        chat_id=chat_id,
        reply_markup=purchase_summary_keyboard(has_discount=bool(data.get("discount_code"))),
    )
    return {"ok": True, "handled": True}


def start_renewal_flow(
    client,
    config,
    bot_user,
    public_id,
    *,
    chat_id,
    format_user_order_detail_func,
    order_management_keyboard_func,
):
    vpn_client = get_bot_client(bot_user, public_id)
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        bot_user.reset_state()
        return {"ok": True, "success": False}
    if not vpn_client.plan_id:
        client.send_message("این کانفیگ پلن قابل تمدید ندارد.", chat_id=chat_id, reply_markup=client_config_keyboard(vpn_client, bot_user))
        bot_user.reset_state()
        return {"ok": True, "success": False}

    pending_order = pending_renewal_order(bot_user, vpn_client)
    if pending_order:
        client.send_message(
            "برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید وجود دارد.\n\n"
            f"{format_user_order_detail_func(pending_order)}",
            chat_id=chat_id,
            reply_markup=order_management_keyboard_func(pending_order, bot_user),
        )
        bot_user.reset_state()
        return {"ok": True, "handled": True, "pending": True}

    _store = vpn_client.store or vpn_client.order.store or vpn_client.plan.store or config.store or get_current_store()
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = {
        "flow": "renewal",
        "step": "summary",
        "renewal_client_public_id": str(vpn_client.public_id),
        "plan_id": vpn_client.plan_id,
        "quantity": 1,
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    return send_renewal_summary(client, config, bot_user, chat_id=chat_id)
