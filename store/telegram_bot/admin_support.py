from html import escape

from django.utils import timezone

from store.models import BotEventLog, BotPendingAction, SupportConversation, SupportMessage

from .notifications import format_support_message


def parse_support_callback_data(data):
    data = (data or "").strip()
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "support" and parts[2].isdigit():
        action = parts[1]
        if action == "replay":
            action = "reply"
        if action in {"reply", "close"}:
            return action, int(parts[2])
    return "", None


def is_support_callback_data(data):
    action, conversation_id = parse_support_callback_data(data)
    return bool(action and conversation_id)


def handle_support_callback_update(
    config,
    callback_query,
    *,
    chat_id,
    client_cls,
    get_callback_id_func,
    get_callback_data_func,
    normalize_id_func,
    get_sender_object_func,
    log_callback_func,
    extract_callback_message_reference_func,
    empty_inline_keyboard_func,
    delivery_error_cls,
):
    callback_id = get_callback_id_func(callback_query)
    data = get_callback_data_func(callback_query)
    client = client_cls(config)
    client.answer_callback(callback_id, "دریافت شد")

    action, conversation_id = parse_support_callback_data(data)
    admin_user_id = normalize_id_func(get_sender_object_func(callback_query))
    log_callback_func(
        config,
        status=BotEventLog.Status.RECEIVED,
        message=(
            f"support_callback_data={data!r}; action={action or '-'}; "
            f"conversation_id={conversation_id or '-'}; admin_user_id={admin_user_id or '-'}"
        ),
        raw_payload={
            "callback_data": data,
            "callback_id": callback_id,
            "admin_user_id": admin_user_id,
            "chat_id": chat_id,
            "callback_query": callback_query,
        },
    )

    if not action or not conversation_id:
        client.send_message(f"Malformed support callback data: <code>{escape(data)}</code>", chat_id=chat_id)
        return {"ok": True, "ignored": True, "error": "Malformed support callback data."}

    conversation = (
        SupportConversation.objects.select_related("store", "customer")
        .filter(pk=conversation_id)
        .first()
    )
    if not conversation:
        client.send_message("گفتگوی پشتیبانی پیدا نشد.", chat_id=chat_id)
        return {"ok": True, "success": False, "error": "Support conversation not found."}

    if action == "close":
        conversation.close()
        BotPendingAction.objects.filter(
            bot_config=config,
            support_conversation=conversation,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        try:
            message_ref = extract_callback_message_reference_func(callback_query or {}, fallback_chat_id=chat_id)
            if message_ref["chat_id"] and message_ref["message_id"]:
                client.edit_message(
                    chat_id=message_ref["chat_id"],
                    message_id=message_ref["message_id"],
                    text=format_support_message(conversation, title="گفتگوی پشتیبانی بسته شد"),
                    reply_markup=empty_inline_keyboard_func(),
                )
                return {"ok": True, "closed": True}
        except delivery_error_cls:
            pass
        client.send_message("گفتگوی پشتیبانی بسته شد.", chat_id=chat_id)
        return {"ok": True, "closed": True}

    if action == "reply":
        BotPendingAction.objects.filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        BotPendingAction.objects.create(
            bot_config=config,
            support_conversation=conversation,
            admin_user_id=admin_user_id,
            action=BotPendingAction.Action.SUPPORT_REPLY,
        )
        client.send_message(
            (
                "پاسخ این گفتگوی پشتیبانی را ارسال کن.\n\n"
                f"<b>شناسه گفتگو:</b> <code>{conversation.pk}</code>\n"
                f"<b>تماس مشتری:</b> <code>{escape(conversation.contact_value or '-')}</code>"
            ),
            chat_id=chat_id,
        )
        return {"ok": True, "waiting_for_support_reply": True}

    return {"ok": True, "ignored": True}


def handle_pending_support_reply(
    config,
    pending,
    message,
    text,
    *,
    chat_id,
    admin_user_id,
    client_cls,
    get_message_id_func,
    log_event_func,
):
    conversation = pending.support_conversation
    if not conversation:
        pending.status = BotPendingAction.Status.CANCELLED
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at", "updated_at"])
        client_cls(config).send_message("این گفتگوی پشتیبانی دیگر پیدا نمی‌شود.", chat_id=chat_id)
        return {"ok": True, "success": False, "error": "Support conversation not found."}

    support_message = SupportMessage.objects.create(
        conversation=conversation,
        sender_type=SupportMessage.SenderType.ADMIN,
        bot_config=config,
        body=text,
        metadata={
            "provider": config.provider,
            "admin_user_id": admin_user_id,
            "chat_id": chat_id,
            "message_id": get_message_id_func(message),
        },
    )
    conversation.mark_answered()
    pending.mark_completed()
    log_event_func(
        config,
        event_type=BotEventLog.EventType.SUPPORT_REPLY,
        status=BotEventLog.Status.SUCCESS,
        message=f"Support reply saved for conversation {conversation.pk}",
        raw_payload={
            "conversation_id": conversation.pk,
            "support_message_id": support_message.pk,
            "chat_id": chat_id,
        },
    )
    client_cls(config).send_message(
        f"پاسخ پشتیبانی ثبت شد و در صفحه پشتیبانی مشتری نمایش داده می‌شود.\n\n<code>{escape(text[:500])}</code>",
        chat_id=chat_id,
    )
    return {"ok": True, "success": True, "support_conversation": conversation.pk}
