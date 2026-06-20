from html import escape

from django.utils import timezone

from store.jalali import persian_digits
from store.models import BotUser, BroadcastMessage
from store.order_services import get_current_store

from .user_menu import main_menu_keyboard

BROADCAST_CALLBACK_PREFIX = "admin:bc:"

BROADCAST_AUDIENCE_LABELS = {
    BroadcastMessage.AudienceType.ALL: "همه کاربران",
    BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS: "مشتریان دارای خرید موفق",
    BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG: "دارای کانفیگ فعال",
    BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER: "بدون سفارش",
    BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED: "کاربران قدیمی WizWiz",
    BroadcastMessage.AudienceType.LOYAL: "مشتریان وفادار",
    BroadcastMessage.AudienceType.GOOD: "مشتریان خوب",
    BroadcastMessage.AudienceType.TOP_BUYER: "خریداران برتر",
    BroadcastMessage.AudienceType.TOP_REFERRER: "معرف‌های برتر",
    BroadcastMessage.AudienceType.INACTIVE: "مشتریان غیرفعال",
    BroadcastMessage.AudienceType.NO_ORDER: "بدون خرید موفق",
}

BROADCAST_CHANNEL_LABELS = {
    BroadcastMessage.Channel.TELEGRAM: "تلگرام",
    BroadcastMessage.Channel.BALE: "بله",
    BroadcastMessage.Channel.ALL_AVAILABLE: "همه کانال‌های موجود",
}


def is_broadcast_callback_data(data):
    return str(data or "").startswith(BROADCAST_CALLBACK_PREFIX)


def broadcast_audience_keyboard():
    audience_rows = [
        (BroadcastMessage.AudienceType.ALL, BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS),
        (BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG, BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER),
        (BroadcastMessage.AudienceType.LOYAL, BroadcastMessage.AudienceType.GOOD),
        (BroadcastMessage.AudienceType.TOP_BUYER, BroadcastMessage.AudienceType.TOP_REFERRER),
        (BroadcastMessage.AudienceType.INACTIVE, BroadcastMessage.AudienceType.NO_ORDER),
    ]
    rows = []
    for left, right in audience_rows:
        rows.append(
            [
                {"text": BROADCAST_AUDIENCE_LABELS[left], "callback_data": f"admin:bc:aud:{left}"},
                {"text": BROADCAST_AUDIENCE_LABELS[right], "callback_data": f"admin:bc:aud:{right}"},
            ]
        )
    rows.append(
        [
            {
                "text": BROADCAST_AUDIENCE_LABELS[BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED],
                "callback_data": f"admin:bc:aud:{BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED}",
            }
        ]
    )
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def broadcast_confirm_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "تایید و ارسال", "callback_data": "admin:bc:send"},
                {"text": "لغو", "callback_data": "admin:bc:cancel"},
            ],
            [{"text": "انتخاب مخاطب", "callback_data": "admin:bc:menu"}],
        ]
    }


def broadcast_menu_text(config):
    channel = BROADCAST_CHANNEL_LABELS.get(config.provider, config.get_provider_display())
    return (
        "ارسال پیام 📣\n"
        "گروه مخاطبان را انتخاب کنید.\n\n"
        f"کانال ارسال این ربات: {channel}"
    )


def broadcast_campaign_preview(config, *, audience_type, message_text, channel=None):
    from store.broadcast_services import resolve_campaign_recipients

    campaign = BroadcastMessage(
        store=config.store,
        title="Broadcast preview",
        message_text=message_text,
        audience_type=audience_type,
        channel=channel or config.provider,
    )
    rows = resolve_campaign_recipients(campaign, bot_config=config)
    total = len(rows)
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    return {
        "total": total,
        "pending": total - skipped,
        "skipped": skipped,
    }


def format_broadcast_preview(*, audience_type, channel, message_text, counts):
    excerpt = message_text[:1200] + ("..." if len(message_text) > 1200 else "")
    audience_label = BROADCAST_AUDIENCE_LABELS.get(audience_type, audience_type)
    channel_label = BROADCAST_CHANNEL_LABELS.get(channel, channel)
    return "\n".join(
        [
            "پیش‌نمایش ارسال پیام 📣",
            "━━━━━━━━━━━━━━",
            f"مخاطب: {audience_label}",
            f"کانال: {channel_label}",
            "",
            f"{persian_digits(counts['total'])} مخاطب پیدا شد.",
            f"قابل ارسال: {persian_digits(counts['pending'])}",
            f"Skipped: {persian_digits(counts['skipped'])}",
            "",
            "متن پیام:",
            escape(excerpt),
        ]
    )


def format_broadcast_result(campaign, counts):
    skipped = counts.get("skipped")
    if skipped is None:
        skipped = campaign.recipients.filter(status="skipped").count()
    return "\n".join(
        [
            "گزارش ارسال پیام 📣",
            "━━━━━━━━━━━━━━",
            f"عنوان: {escape(campaign.title)}",
            f"مخاطب کل: {persian_digits(counts.get('total', campaign.total_recipients))}",
            f"موفق: {persian_digits(counts.get('success', campaign.success_count))}",
            f"ناموفق: {persian_digits(counts.get('failed', campaign.failed_count))}",
            f"Skipped: {persian_digits(skipped)}",
        ]
    )


def start_broadcast_flow(client, config, bot_user, *, chat_id, get_current_store_func=get_current_store):
    store = config.store or get_current_store_func()
    if store and not getattr(store, "broadcast_enabled", True):
        client.send_message("ارسال پیام در تنظیمات فروشگاه غیرفعال است.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": False, "broadcast_disabled": True}
    bot_user.reset_state()
    client.send_message(broadcast_menu_text(config), chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
    return {"ok": True, "broadcast_menu": True}


def select_broadcast_audience(client, config, bot_user, audience_type, *, chat_id, cancel_keyboard_func):
    if audience_type not in BROADCAST_AUDIENCE_LABELS:
        client.send_message("این گروه مخاطب پیدا نشد.", chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
        return {"ok": True, "success": False}
    channel = config.provider
    bot_user.state = BotUser.State.BROADCAST_WAIT_TEXT
    bot_user.state_data = {"audience_type": audience_type, "channel": channel}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        (
            f"مخاطب انتخاب شد: {BROADCAST_AUDIENCE_LABELS[audience_type]}\n"
            "متن پیام را ارسال کنید."
        ),
        chat_id=chat_id,
        reply_markup=cancel_keyboard_func(),
    )
    return {"ok": True, "waiting_for_broadcast_text": True}


def handle_broadcast_text(
    config,
    bot_user,
    message_text,
    *,
    chat_id,
    client_cls,
    is_admin_bot_user_func,
    cancel_keyboard_func,
):
    client = client_cls(config)
    if not is_admin_bot_user_func(config, bot_user):
        return {"ok": True, "ignored": True}
    message_text = (message_text or "").strip()
    if not message_text:
        client.send_message("متن خالی پذیرفته نمی‌شود. لطفا متن پیام را ارسال کنید.", chat_id=chat_id, reply_markup=cancel_keyboard_func())
        return {"ok": True, "success": False}

    data = bot_user.state_data or {}
    audience_type = data.get("audience_type") or BroadcastMessage.AudienceType.ALL
    channel = data.get("channel") or config.provider
    counts = broadcast_campaign_preview(
        config,
        audience_type=audience_type,
        message_text=message_text,
        channel=channel,
    )
    bot_user.state = BotUser.State.BROADCAST_CONFIRM
    bot_user.state_data = {
        "audience_type": audience_type,
        "channel": channel,
        "message_text": message_text,
        "preview": counts,
    }
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        format_broadcast_preview(
            audience_type=audience_type,
            channel=channel,
            message_text=message_text,
            counts=counts,
        ),
        chat_id=chat_id,
        reply_markup=broadcast_confirm_keyboard(),
    )
    return {"ok": True, "broadcast_preview": True, **counts}


def send_confirmed_broadcast(client, config, bot_user, *, chat_id):
    from store.broadcast_services import send_campaign

    if bot_user.state != BotUser.State.BROADCAST_CONFIRM:
        client.send_message("کمپین آماده ارسال پیدا نشد.", chat_id=chat_id, reply_markup=broadcast_audience_keyboard())
        return {"ok": True, "success": False}
    data = bot_user.state_data or {}
    message_text = (data.get("message_text") or "").strip()
    if not message_text:
        bot_user.reset_state()
        client.send_message("متن پیام خالی بود و ارسال انجام نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": False}

    now_label = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
    campaign = BroadcastMessage.objects.create(
        store=config.store,
        title=f"Bot broadcast {now_label}",
        message_text=message_text,
        audience_type=data.get("audience_type") or BroadcastMessage.AudienceType.ALL,
        channel=data.get("channel") or config.provider,
        status=BroadcastMessage.Status.QUEUED,
        metadata={
            "source": "admin_bot",
            "provider": config.provider,
            "bot_config_id": config.pk,
            "admin_user_id": bot_user.provider_user_id,
            "preview": data.get("preview") or {},
        },
    )
    counts = send_campaign(campaign, bot_config=config)
    bot_user.reset_state()
    client.send_message(format_broadcast_result(campaign, counts), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
    return {"ok": True, "success": True, "campaign_id": campaign.pk, **counts}


def handle_broadcast_callback_update(
    config,
    callback_query,
    *,
    chat_id,
    client_cls,
    get_callback_id_func,
    get_callback_data_func,
    normalize_id_func,
    get_sender_object_func,
    delete_callback_message_func,
    update_bot_user_from_update_func,
    cancel_keyboard_func,
):
    callback_id = get_callback_id_func(callback_query)
    data = get_callback_data_func(callback_query)
    user_id = normalize_id_func(get_sender_object_func(callback_query))
    client = client_cls(config)
    client.answer_callback(callback_id, "دریافت شد")
    delete_callback_message_func(client, callback_query, fallback_chat_id=chat_id)

    if not (config.is_admin_user(user_id) or config.is_admin_user(chat_id)):
        client.send_message("شما اجازه ارسال پیام انبوه را ندارید.", chat_id=chat_id)
        return {"ok": True, "success": False, "permission_denied": True}

    bot_user = update_bot_user_from_update_func(
        config,
        {"callback_query": callback_query},
        chat_id=chat_id,
        user_id=user_id or chat_id,
    )
    if data == "admin:bc:menu":
        return start_broadcast_flow(client, config, bot_user, chat_id=chat_id)
    if data.startswith("admin:bc:aud:"):
        audience_type = data.rsplit(":", 1)[-1]
        return select_broadcast_audience(
            client,
            config,
            bot_user,
            audience_type,
            chat_id=chat_id,
            cancel_keyboard_func=cancel_keyboard_func,
        )
    if data == "admin:bc:cancel":
        bot_user.reset_state()
        client.send_message("ارسال پیام لغو شد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "cancelled": True}
    if data == "admin:bc:send":
        return send_confirmed_broadcast(client, config, bot_user, chat_id=chat_id)
    return {"ok": True, "ignored": True}
