import logging

from django.utils import timezone

from store.models import BotUser


logger = logging.getLogger(__name__)


def _emit_payment_screen_upsell(config, bot_user, *, chat_id, store, operator, plan, quantity, pricing):
    try:
        from store.revenue_engine.upsell.triggers import PAYMENT_SCREEN_OPENED, safe_emit_event

        return safe_emit_event(
            PAYMENT_SCREEN_OPENED,
            bot_user,
            {
                "bot_user": bot_user,
                "chat_id": chat_id,
                "bot_config": config,
                "store": store,
                "operator": operator,
                "selected_plan": plan,
                "plan": plan,
                "quantity": quantity,
                "pricing": pricing or {},
                "discount_active": bool((bot_user.state_data or {}).get("discount_code")),
                "discount_code": (bot_user.state_data or {}).get("discount_code") or "",
                "source": "telegram_payment_screen",
            },
        )
    except Exception as exc:
        logger.warning("Payment-screen upsell hook skipped bot_user=%s: %s", bot_user.pk, exc)
        return None


def handle_buy_wait_name_message(
    config,
    bot_user,
    message,
    text,
    *,
    client,
    chat_id,
    extract_receipt_file_func,
    receipt_file_type_error_func,
    optional_config_name_keyboard_func,
    cancel_keyboard_func,
    payment_prompt_after_name_func,
    is_admin_bot_user_func,
    finalize_admin_direct_renewal_func,
    finalize_admin_direct_purchase_func,
    finalize_bot_renewal_func,
    finalize_bot_purchase_func,
):
    data = bot_user.state_data or {}
    file_info = extract_receipt_file_func(message)
    if file_info:
        type_error = receipt_file_type_error_func(file_info)
        if type_error:
            client.send_message(type_error, chat_id=chat_id, reply_markup=optional_config_name_keyboard_func())
            return {"ok": True, "success": False}
        data["payment_time"] = data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M")
        data["step"] = "receipt"
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        if data.get("flow") == "renewal":
            return finalize_bot_renewal_func(config, bot_user, message, file_info, chat_id=chat_id)
        return finalize_bot_purchase_func(config, bot_user, message, file_info, chat_id=chat_id)

    if not text:
        client.send_message(
            "نام کانفیگ اختیاری است. می‌توانید نام دلخواه را به صورت متن بفرستید یا «ارسال رسید» را بزنید.",
            chat_id=chat_id,
            reply_markup=optional_config_name_keyboard_func(),
        )
        return {"ok": True, "handled": True}

    data["sender_card_name"] = text[:100]
    data["payment_time"] = timezone.localtime(timezone.now()).strftime("%H:%M")
    if data.get("flow") == "renewal":
        if is_admin_bot_user_func(config, bot_user):
            bot_user.state_data = data
            bot_user.save(update_fields=["state_data", "updated_at"])
            return finalize_admin_direct_renewal_func(config, bot_user, chat_id=chat_id)
        prompt_text = payment_prompt_after_name_func(config, bot_user)
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        data["step"] = "receipt"
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            prompt_text,
            chat_id=chat_id,
            reply_markup=cancel_keyboard_func(),
            parse_mode="HTML",
            force_parse_mode=True,
        )
        return {"ok": True, "handled": True}

    if is_admin_bot_user_func(config, bot_user):
        bot_user.state_data = data
        bot_user.save(update_fields=["state_data", "updated_at"])
        return finalize_admin_direct_purchase_func(config, bot_user, chat_id=chat_id)

    prompt_text = payment_prompt_after_name_func(config, bot_user)
    bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
    data["step"] = "receipt"
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        prompt_text,
        chat_id=chat_id,
        reply_markup=cancel_keyboard_func(),
        parse_mode="HTML",
        force_parse_mode=True,
    )
    return {"ok": True, "handled": True}


def is_legacy_receipt_state(state):
    return state in {
        BotUser.State.BUY_WAIT_LAST4,
        BotUser.State.BUY_WAIT_TIME,
        BotUser.State.BUY_WAIT_TRACKING,
    }


def handle_legacy_receipt_state_message(client, bot_user, *, chat_id, cancel_keyboard_func):
    data = bot_user.state_data or {}
    data["payment_time"] = data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M")
    bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message("برای تکمیل سفارش فقط عکس رسید پرداخت را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard_func())
    return {"ok": True, "handled": True}


def handle_buy_wait_receipt_message(
    config,
    bot_user,
    message,
    *,
    client,
    chat_id,
    extract_receipt_file_func,
    receipt_file_type_error_func,
    cancel_keyboard_func,
    finalize_bot_renewal_func,
    finalize_bot_purchase_func,
):
    file_info = extract_receipt_file_func(message)
    if not file_info:
        client.send_message("لطفاً عکس رسید پرداخت را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard_func())
        return {"ok": True, "handled": True}
    type_error = receipt_file_type_error_func(file_info)
    if type_error:
        client.send_message(type_error, chat_id=chat_id, reply_markup=cancel_keyboard_func())
        return {"ok": True, "success": False}
    if (bot_user.state_data or {}).get("flow") == "renewal":
        return finalize_bot_renewal_func(config, bot_user, message, file_info, chat_id=chat_id)
    return finalize_bot_purchase_func(config, bot_user, message, file_info, chat_id=chat_id)


def show_payment_step(
    client,
    config,
    bot_user,
    *,
    chat_id,
    renewal_context_from_state_func,
    purchase_context_from_state_func,
    pricing_from_state_func,
    main_menu_keyboard_func,
    renewal_payment_prompt_func,
    payment_step_keyboard_func,
    format_payment_prompt_func,
):
    data = bot_user.state_data or {}
    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state_func(config, bot_user)
        if error:
            bot_user.reset_state()
            client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard_func())
            return {"ok": True, "success": False, "error": error}
        pricing = pricing_from_state_func(plan, 1, customer=bot_user.customer, data=data)
        data["step"] = "receipt"
        data["payment_time"] = data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M")
        bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
        bot_user.state_data = data
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            renewal_payment_prompt_func(store, vpn_client, pricing=pricing),
            chat_id=chat_id,
            reply_markup=payment_step_keyboard_func(store.card_number, pricing["payable_amount"], config=config),
            parse_mode="HTML",
            force_parse_mode=True,
        )
        return {"ok": True, "handled": True}

    store, operator, plan, quantity, error = purchase_context_from_state_func(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard_func())
        return {"ok": True, "success": False, "error": error}
    pricing = pricing_from_state_func(plan, quantity, customer=bot_user.customer, data=data)
    data["step"] = "receipt"
    data["payment_time"] = data.get("payment_time") or timezone.localtime(timezone.now()).strftime("%H:%M")
    bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    _emit_payment_screen_upsell(
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
        format_payment_prompt_func(
            store,
            plan,
            quantity=quantity,
            operator=operator,
            pricing=pricing,
        ),
        chat_id=chat_id,
        reply_markup=payment_step_keyboard_func(store.card_number, pricing["payable_amount"], config=config),
        parse_mode="HTML",
        force_parse_mode=True,
    )
    return {"ok": True, "handled": True}


def start_optional_config_name_flow(
    client,
    config,
    bot_user,
    *,
    chat_id,
    renewal_context_from_state_func,
    purchase_context_from_state_func,
    main_menu_keyboard_func,
    optional_config_name_keyboard_func,
):
    data = bot_user.state_data or {}
    if data.get("flow") == "renewal":
        _vpn_client, _store, _plan, error = renewal_context_from_state_func(config, bot_user)
    else:
        _store, _operator, _plan, _quantity, error = purchase_context_from_state_func(config, bot_user)
    if error:
        bot_user.reset_state()
        client.send_message(error, chat_id=chat_id, reply_markup=main_menu_keyboard_func())
        return {"ok": True, "success": False, "error": error}

    data["step"] = "config_name"
    bot_user.state = BotUser.State.BUY_WAIT_NAME
    bot_user.state_data = data
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "نام دلخواه کانفیگ را ارسال کنید. اگر نام خاصی نمی‌خواهید، «ارسال رسید» را بزنید.",
        chat_id=chat_id,
        reply_markup=optional_config_name_keyboard_func(),
    )
    return {"ok": True, "handled": True}


def continue_with_receipt_only(client, config, bot_user, *, chat_id, show_payment_step_func):
    data = bot_user.state_data or {}
    data.pop("sender_card_name", None)
    data["step"] = "payment"
    bot_user.state_data = data
    bot_user.save(update_fields=["state_data", "updated_at"])
    return show_payment_step_func(client, config, bot_user, chat_id=chat_id)


def payment_prompt_after_name(
    config,
    bot_user,
    *,
    renewal_context_from_state_func,
    purchase_context_from_state_func,
    pricing_from_state_func,
    renewal_payment_prompt_func,
    format_payment_prompt_func,
):
    data = bot_user.state_data or {}
    if data.get("step") == "payment":
        if data.get("flow") == "renewal":
            return "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید تمدید را ارسال کنید."
        return "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید پرداخت را ارسال کنید."

    if data.get("flow") == "renewal":
        vpn_client, store, plan, error = renewal_context_from_state_func(config, bot_user)
        if error:
            return "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید تمدید را ارسال کنید."
        pricing = pricing_from_state_func(plan, 1, customer=bot_user.customer, data=data)
        return (
            f"{renewal_payment_prompt_func(store, vpn_client, pricing=pricing)}\n\n"
            "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید تمدید را ارسال کنید."
        )

    store, operator, plan, quantity, error = purchase_context_from_state_func(config, bot_user)
    if error:
        return "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید پرداخت را ارسال کنید."
    pricing = pricing_from_state_func(plan, quantity, customer=bot_user.customer, data=data)
    return (
        f"{format_payment_prompt_func(store, plan, quantity=quantity, operator=operator, pricing=pricing)}\n\n"
        "نام کانفیگ ثبت شد.\n\n📸 لطفاً تصویر رسید پرداخت را ارسال کنید."
    )
