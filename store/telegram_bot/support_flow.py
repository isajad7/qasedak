from django.utils import timezone

from store.jalali import persian_digits
from store.models import SupportConversation, SupportMessage
from store.order_services import get_current_store

from .user_menu import main_menu_keyboard

BOT_STATE_SUPPORT_WAIT_MESSAGE = "support_wait_message"
BOT_SUPPORT_CATEGORIES = {
    "payment": "مشکل پرداخت",
    "connection": "مشکل اتصال",
    "renewal": "تمدید",
    "other": "سایر",
}


def default_is_admin_bot_user(config, bot_user):
    return bool(
        bot_user
        and (
            config.is_admin_user(getattr(bot_user, "provider_user_id", ""))
            or config.is_admin_user(getattr(bot_user, "chat_id", ""))
        )
    )


def is_support_user_callback_data(data):
    data = str(data or "")
    return data == "user:support" or data.startswith("user:support_cat:")


def support_category_keyboard():
    rows = [
        [{"text": label, "callback_data": f"user:support_cat:{key}"}]
        for key, label in BOT_SUPPORT_CATEGORIES.items()
    ]
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def support_wait_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "انتخاب دسته", "callback_data": "user:support"}],
            [{"text": "لغو", "callback_data": "user:cancel"}],
        ]
    }


def start_support_flow(client, config, bot_user, *, chat_id):
    bot_user.reset_state()
    client.send_message(
        "موضوع پشتیبانی را انتخاب کنید.",
        chat_id=chat_id,
        reply_markup=support_category_keyboard(),
    )
    return {"ok": True, "handled": True}


def select_support_category(client, bot_user, category, *, chat_id):
    if category not in BOT_SUPPORT_CATEGORIES:
        client.send_message("این دسته پشتیبانی پیدا نشد.", chat_id=chat_id, reply_markup=support_category_keyboard())
        return {"ok": True, "success": False}
    bot_user.state = BOT_STATE_SUPPORT_WAIT_MESSAGE
    bot_user.state_data = {
        "support_category": category,
        "support_subject": BOT_SUPPORT_CATEGORIES[category],
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        f"{BOT_SUPPORT_CATEGORIES[category]}\nپیام خود را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=support_wait_keyboard(),
    )
    return {"ok": True, "handled": True}


def bot_support_contact_value(bot_user):
    if bot_user.username:
        return f"@{bot_user.username.lstrip('@')}"
    if bot_user.customer_id and bot_user.customer.phone_number:
        return bot_user.customer.phone_number
    return bot_user.provider_user_id or bot_user.chat_id


def create_support_ticket_from_bot(
    config,
    bot_user,
    text,
    message,
    *,
    chat_id,
    client_cls,
    notify_support_message_func,
    get_message_id_func,
    is_admin_bot_user_func=default_is_admin_bot_user,
    get_current_store_func=get_current_store,
):
    client = client_cls(config)
    body = (text or "").strip()
    if not body:
        client.send_message("پیام پشتیبانی خالی است. لطفا متن مشکل را ارسال کنید.", chat_id=chat_id, reply_markup=support_wait_keyboard())
        return {"ok": True, "success": False}

    data = bot_user.state_data or {}
    subject = data.get("support_subject") or BOT_SUPPORT_CATEGORIES["other"]
    store = config.store or get_current_store_func()
    conversation = SupportConversation.objects.create(
        store=store,
        customer=bot_user.customer if bot_user.customer_id else None,
        subject=subject,
        contact_value=bot_support_contact_value(bot_user),
        status=SupportConversation.Status.WAITING_ADMIN,
        last_customer_message_at=timezone.now(),
    )
    support_message = SupportMessage.objects.create(
        conversation=conversation,
        sender_type=SupportMessage.SenderType.CUSTOMER,
        customer=bot_user.customer if bot_user.customer_id else None,
        bot_config=config,
        body=body,
        metadata={
            "source": "bot_support",
            "provider": config.provider,
            "chat_id": chat_id,
            "message_id": get_message_id_func(message),
            "category": data.get("support_category") or "other",
        },
    )
    notify_support_message_func(conversation, support_message)
    bot_user.reset_state()
    client.send_message(
        (
            "پیام پشتیبانی ثبت شد.\n"
            "━━━━━━━━━━━━━━\n"
            f"شماره تیکت: {persian_digits(conversation.pk)}\n"
            f"وضعیت: {conversation.get_status_display()}"
        ),
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
    )
    return {"ok": True, "success": True, "support_conversation": conversation.pk}
