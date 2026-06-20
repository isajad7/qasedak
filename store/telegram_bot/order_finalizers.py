import logging

from django.core.exceptions import ValidationError
from django.utils import timezone

from store.models import BotEventLog, Order, validate_payment_receipt_image
from store.order_services import (
    OPERATOR_REQUIRED_MESSAGE,
    create_manual_payment_order,
    create_renewal_payment_order,
    get_active_operators,
    get_current_store,
    sales_mode_requires_operator,
)
from store.telegram_bot.buy_flow import (
    get_purchase_operator_from_state,
    get_selected_purchase_plan,
    operator_keyboard,
)
from store.telegram_bot.client import BotClient, BotDeliveryError
from store.telegram_bot.payments import (
    attach_bot_receipt as _attach_bot_receipt,
    bot_order_metadata,
    bot_payment_sender_name,
)
from store.telegram_bot.redaction import sanitize_bot_event_log_value
from store.telegram_bot.services_flow import (
    bot_client_label,
    bot_client_status,
    get_bot_client,
    user_client_delete_button,
)
from store.telegram_bot.user_menu import main_menu_keyboard
from store.telegram_bot.user_orders_flow import (
    format_user_order_detail as _format_user_order_detail,
    order_management_keyboard as _order_management_keyboard,
    user_order_summary,
)
from store.telegram_bot.order_delivery import (
    format_customer_order_event as _format_customer_order_event,
    send_customer_order_event_message as _send_customer_order_event_message,
)
from store.xui_api import sync_vpn_client_stats

from .buy_flow import cancel_keyboard

logger = logging.getLogger(__name__)

BOT_ORDER_STATUS_LABELS = {
    Order.Status.PENDING_PAYMENT: "در انتظار پرداخت",
    Order.Status.PENDING_VERIFICATION: "در انتظار بررسی",
    Order.Status.CONFIRMED: "تایید شده",
    Order.Status.COMPLETED: "فعال شده",
    Order.Status.REJECTED: "رد شده",
    Order.Status.CANCELLED: "لغو شده",
}

BOT_VERIFICATION_STATUS_LABELS = {
    Order.VerificationStatus.PENDING: "در انتظار بررسی",
    Order.VerificationStatus.VERIFIED: "تایید شده",
    Order.VerificationStatus.REJECTED: "رد شده",
}


def emit_purchase_revenue_event(config, bot_user, order, *, flow, extra=None):
    try:
        from store.revenue_engine.triggers import USER_PURCHASE, safe_emit_event

        safe_emit_event(
            USER_PURCHASE,
            order,
            {
                "bot_user": bot_user,
                "chat_id": bot_user.chat_id,
                "bot_config": config,
                "flow": flow,
                **(extra or {}),
            },
        )
    except Exception as exc:
        logger.warning("Revenue USER_PURCHASE hook skipped order=%s: %s", getattr(order, "pk", None), exc)


def bot_order_status(order):
    return BOT_ORDER_STATUS_LABELS.get(order.status, order.get_status_display())


def bot_verification_status(order):
    return BOT_VERIFICATION_STATUS_LABELS.get(order.verification_status, order.get_verification_status_display())


def log_event(config, *, event_type, status, order=None, message="", raw_payload=None):
    return BotEventLog.objects.create(
        bot_config=config,
        order=order,
        event_type=event_type,
        status=status,
        message=sanitize_bot_event_log_value(message),
        raw_payload=sanitize_bot_event_log_value(raw_payload or {}),
    )


def attach_bot_receipt(client, config, file_info, metadata):
    return _attach_bot_receipt(client, config, file_info, metadata, delivery_error_cls=BotDeliveryError)


def order_management_keyboard(order, bot_user=None):
    return _order_management_keyboard(
        order,
        bot_user,
        user_client_delete_button_func=user_client_delete_button,
    )


def format_user_order_detail(order):
    return _format_user_order_detail(
        order,
        bot_verification_status_func=bot_verification_status,
        bot_order_status_func=bot_order_status,
        bot_client_label_func=bot_client_label,
        bot_client_status_func=bot_client_status,
        sync_vpn_client_stats_func=sync_vpn_client_stats,
    )


def format_customer_order_event(order, *, event_type):
    return _format_customer_order_event(
        order,
        event_type=event_type,
        format_order_message_func=lambda order, *, title="Order updated": title,
    )


def send_customer_order_event_message(client, order, *, event_type, chat_id, reply_markup=None):
    return _send_customer_order_event_message(
        client,
        order,
        event_type=event_type,
        chat_id=chat_id,
        reply_markup=reply_markup,
        format_customer_order_event_func=format_customer_order_event,
    )


def create_bot_payment_order(*, config, bot_user, plan, metadata, receipt_image=None, require_receipt_image=False):
    store = config.store or get_current_store()
    data = bot_user.state_data or {}
    quantity = data.get("quantity", 1)
    operator = get_purchase_operator_from_state(store, data)
    return create_manual_payment_order(
        store=store,
        customer=bot_user.customer,
        plan=plan,
        operator=operator,
        sender_card_name=bot_payment_sender_name(bot_user),
        sender_card_last4="",
        payment_time=(bot_user.state_data or {}).get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M"),
        receipt_image=receipt_image,
        require_receipt_image=require_receipt_image,
        bank_tracking_code="",
        discount_code=(bot_user.state_data or {}).get("discount_code", ""),
        quantity=quantity,
        metadata=metadata,
    )


def submit_renewal_payment(order, bot_user, *, receipt_image=None, require_receipt_image=False):
    data = bot_user.state_data or {}
    try:
        order.submit_manual_payment(
            sender_card_name=bot_payment_sender_name(bot_user),
            sender_card_last4="",
            payment_time=data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M"),
            receipt_image=receipt_image,
            require_receipt_image=require_receipt_image,
        )
        order.save()
    except ValidationError as exc:
        return False, exc.messages[0]
    except OSError:
        logger.exception("Could not save renewal receipt image.")
        return False, "Could not save the receipt image. Please try a smaller JPG or PNG image, or contact support."
    return True, ""


def create_bot_renewal_order(config, bot_user, vpn_client, metadata):
    result = create_renewal_payment_order(
        customer=bot_user.customer,
        vpn_client=vpn_client,
        metadata=metadata,
        discount_code=(bot_user.state_data or {}).get("discount_code", ""),
    )
    if not result.success:
        return result
    return result


def finalize_admin_direct_renewal(config, bot_user, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        bot_user.reset_state()
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    metadata = bot_order_metadata(
        config,
        bot_user,
        source=f"{config.provider}_admin_bot_direct_renewal",
        extra={
            "admin_direct_purchase": True,
            "suppress_new_order_notification": True,
            "suppress_admin_order_updates": True,
        },
    )
    result = create_bot_renewal_order(config, bot_user, vpn_client, metadata)
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    emit_purchase_revenue_event(config, bot_user, order, flow="admin_direct_renewal")
    submitted, submit_message = submit_renewal_payment(order, bot_user)
    if not submitted:
        client.send_message(submit_message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": submit_message}

    from store.order_actions import activate_order

    activation = activate_order(order, user=None, notify=False)
    order.refresh_from_db()
    bot_user.reset_state()
    if not activation.success:
        client.send_message(
            f"سفارش تمدید ساخته شد اما تمدید کانفیگ ناموفق بود.\n{activation.message}",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(),
        )
        return {"ok": True, "success": False, "order": order.order_tracking_code, "message": activation.message}

    send_customer_order_event_message(
        client,
        order,
        event_type="approved",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(),
    )
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_admin_direct_purchase(config, bot_user, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    store = config.store or get_current_store()
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        bot_user.reset_state()
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        bot_user.reset_state()
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": "Plan not found."}

    metadata = {
        "source": f"{config.provider}_admin_bot_direct",
        "admin_direct_purchase": True,
        "suppress_new_order_notification": True,
        "suppress_admin_order_updates": True,
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    result = create_bot_payment_order(
        config=config,
        bot_user=bot_user,
        plan=plan,
        metadata=metadata,
        receipt_image=None,
        require_receipt_image=False,
    )
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    emit_purchase_revenue_event(config, bot_user, order, flow="admin_direct_purchase")
    from store.order_actions import activate_order

    activation = activate_order(order, user=None, notify=False)
    order.refresh_from_db()
    bot_user.reset_state()
    if not activation.success:
        client.send_message(
            f"سفارش ساخته شد اما ساخت یا فعال‌سازی کانفیگ ناموفق بود.\n{activation.message}",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(),
        )
        return {"ok": True, "success": False, "order": order.order_tracking_code, "message": activation.message}

    send_customer_order_event_message(
        client,
        order,
        event_type="approved",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(),
    )
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_bot_purchase(config, bot_user, message, file_info, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    store = config.store or get_current_store()
    operator = get_purchase_operator_from_state(store, data)
    if sales_mode_requires_operator(store) and not operator:
        bot_user.reset_state()
        client.send_message(OPERATOR_REQUIRED_MESSAGE, chat_id=chat_id, reply_markup=operator_keyboard(get_active_operators(store)))
        return {"ok": True, "success": False, "error": "Operator required."}

    plan = get_selected_purchase_plan(store, data.get("plan_id"), operator=operator)
    if not plan:
        bot_user.reset_state()
        client.send_message("پلن انتخابی دیگر فعال نیست. لطفا دوباره خرید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False, "error": "Plan not found."}

    metadata = {
        "source": f"{config.provider}_bot",
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    receipt_image = attach_bot_receipt(client, config, file_info, metadata)

    result = create_bot_payment_order(
        config=config,
        bot_user=bot_user,
        plan=plan,
        metadata=metadata,
        receipt_image=receipt_image,
        require_receipt_image=bool(receipt_image),
    )
    if not result.success:
        logger.warning("Bot purchase creation failed user=%s message=%s", bot_user.pk, result.message)
        client.send_message(result.message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    emit_purchase_revenue_event(
        config,
        bot_user,
        order,
        flow="purchase",
        extra={"duplicate_detected": result.duplicate_detected},
    )
    bot_user.reset_state()
    if result.duplicate_detected:
        client.send_message(
            "این سفارش قبلاً ثبت شده و هنوز در انتظار بررسی است.\n\n"
            f"{format_user_order_detail(order)}",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(order, bot_user),
        )
    else:
        client.send_message(user_order_summary(order), chat_id=chat_id, reply_markup=main_menu_keyboard())
    if file_info and not order.payment_receipt_image:
        forward_receipt_to_admin(client, order, file_info, from_chat_id=chat_id)
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def finalize_bot_renewal(config, bot_user, message, file_info, *, chat_id):
    client = BotClient(config)
    data = bot_user.state_data or {}
    vpn_client = get_bot_client(bot_user, data.get("renewal_client_public_id"))
    if not vpn_client:
        bot_user.reset_state()
        client.send_message("این کانفیگ پیدا نشد. لطفا دوباره تمدید را شروع کنید.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    pending_order = pending_renewal_order(bot_user, vpn_client)
    if pending_order:
        bot_user.reset_state()
        client.send_message(
            "برای این کانفیگ از قبل یک تمدید در انتظار پرداخت یا تایید وجود دارد.",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(pending_order, bot_user),
        )
        return {"ok": True, "handled": True, "pending": True}

    metadata = bot_order_metadata(
        config,
        bot_user,
        source=f"{config.provider}_bot_renewal",
        extra={"suppress_new_order_notification": True},
    )
    receipt_image = attach_bot_receipt(client, config, file_info, metadata)
    if receipt_image is not None:
        try:
            validate_payment_receipt_image(receipt_image)
        except ValidationError as exc:
            client.send_message(exc.messages[0], chat_id=chat_id, reply_markup=cancel_keyboard())
            return {"ok": True, "success": False, "error": exc.messages[0]}

    result = create_bot_renewal_order(config, bot_user, vpn_client, metadata)
    if not result.success:
        client.send_message(result.message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": result.message}

    order = result.order
    emit_purchase_revenue_event(config, bot_user, order, flow="renewal")
    submitted, submit_message = submit_renewal_payment(
        order,
        bot_user,
        receipt_image=receipt_image,
        require_receipt_image=bool(receipt_image),
    )
    if not submitted:
        logger.warning("Bot renewal payment submission failed user=%s order=%s message=%s", bot_user.pk, order.pk, submit_message)
        client.send_message(submit_message, chat_id=chat_id, reply_markup=cancel_keyboard())
        return {"ok": True, "success": False, "error": submit_message}

    bot_user.reset_state()
    order_metadata = dict(order.metadata or {})
    order_metadata.pop("suppress_new_order_notification", None)
    order.metadata = order_metadata
    order.save(update_fields=["metadata", "updated_at"])
    from store.admin_notifications import schedule_notify_admins_payment_receipt

    schedule_notify_admins_payment_receipt(order.pk)
    client.send_message(user_order_summary(order), chat_id=chat_id, reply_markup=main_menu_keyboard())
    if file_info and not order.payment_receipt_image:
        forward_receipt_to_admin(client, order, file_info, from_chat_id=chat_id)
    return {"ok": True, "success": True, "order": order.order_tracking_code}


def pending_renewal_order(bot_user, vpn_client):
    if not bot_user.customer_id or not vpn_client:
        return None
    return (
        Order.objects.select_related("plan", "operator", "store")
        .filter(
            customer=bot_user.customer,
            metadata__renewal_client_pk=vpn_client.pk,
            status__in={
                Order.Status.PENDING_PAYMENT,
                Order.Status.PENDING_VERIFICATION,
                Order.Status.CONFIRMED,
            },
        )
        .order_by("-created_at")
        .first()
    )


def forward_receipt_to_admin(client, order, file_info, *, from_chat_id):
    if not file_info or not file_info.get("message_id"):
        return
    for admin_user_id in client.config.get_admin_user_ids():
        try:
            client.forward_message(from_chat_id=from_chat_id, message_id=file_info["message_id"], chat_id=admin_user_id)
        except BotDeliveryError as exc:
            log_event(
                client.config,
                event_type=BotEventLog.EventType.ERROR,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Could not forward receipt: {exc}",
                raw_payload={"order_id": order.order_tracking_code, "receipt": file_info, "admin_user_id": admin_user_id},
            )
