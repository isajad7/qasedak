"""Authenticated callbacks tied to the recipient and purchase, never a raw client ID."""
from django.utils import timezone

from .models import OutreachEvent, OutreachPreference
from .outreach import purchase_clients, renewal_state


def owned_event(event_id, config, bot_user, chat_id):
    if (not str(event_id or "").isdigit() or not bot_user.customer_id or not config.is_active
            or str(chat_id) != str(bot_user.chat_id) or str(chat_id) != str(bot_user.provider_user_id)
            or not str(chat_id).isdigit() or int(chat_id) <= 0):
        return None
    return OutreachEvent.objects.select_related("order__plan", "order__store").filter(
        pk=event_id, bot_user=bot_user, bot_user__bot_config=config,
        order__customer_id=bot_user.customer_id, order__store_id=config.store_id,
        status__in=("sent", "uncertain", "sending"),
    ).first()


def handle_callback(client, config, bot_user, data, *, chat_id, start_renewal):
    parts = data.split(":")
    event = owned_event(parts[-1], config, bot_user, chat_id) if len(parts) == 3 else None
    if not event:
        client.send_message("این یادآوری برای حساب شما در دسترس نیست.", chat_id=chat_id)
        return {"ok": True, "success": False}
    action = parts[1]
    if action in {"mute", "resume"}:
        muted = action == "mute"
        OutreachPreference.objects.update_or_create(store_id=event.order.store_id, customer_id=bot_user.customer_id,
                                                    defaults={"muted": muted})
        client.send_message("یادآوری‌های خودکار متوقف شدند." if muted else "یادآوری‌های خودکار دوباره فعال شدند.", chat_id=chat_id,
            reply_markup={"inline_keyboard": [[{"text": "فعال کردن یادآوری‌ها" if muted else "توقف یادآوری‌ها",
                                                "callback_data": f"journey:{'resume' if muted else 'mute'}:{event.pk}"}]]})
    elif action == "support":
        bot_user.state = "support_wait_message"
        bot_user.state_data = {"support_category": "connection", "support_subject": f"پیگیری خرید #{event.order_id}",
                               "journey_event_id": event.pk}
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(f"مشکل خرید #{event.order_id} را بنویس. مشخصات همین خرید همراه پیام به پشتیبانی می‌رسد.",
                            chat_id=chat_id, reply_markup={"inline_keyboard": [[{"text": "لغو", "callback_data": "user:cancel"}]]})
    elif action == "renew":
        reason, _ = renewal_state(event.order, timezone.now())
        if reason or event.order.status not in {"completed", "confirmed"}:
            client.send_message("این خرید قبلاً تمدید شده یا تمدید آن در انتظار بررسی است. وضعیت را از بخش سرویس‌های من ببین.", chat_id=chat_id)
        else:
            clients = list(purchase_clients(event.order).exclude(status="deleted"))
            if len(clients) == 1:
                return start_renewal(client, config, bot_user, str(clients[0].public_id), chat_id=chat_id)
            if clients:
                client.send_message("کدام سرویس همین خرید را می‌خواهی تمدید کنی؟", chat_id=chat_id,
                    reply_markup={"inline_keyboard": [[{"text": f"سرویس {index}", "callback_data": f"user:client_renew:{vpn.public_id}"}]
                                                        for index, vpn in enumerate(clients, 1)]})
            else:
                client.send_message("برای تمدید این خرید از پشتیبانی کمک بگیر.", chat_id=chat_id,
                    reply_markup={"inline_keyboard": [[{"text": "پشتیبانی همین خرید", "callback_data": f"journey:support:{event.pk}"}]]})
    else:
        return {"ok": True, "success": False}
    return {"ok": True, "handled": True}
