import logging

from django.core.cache import cache
from django.utils import timezone

from store.models import BotUser, Customer

from .client import BotDeliveryError


logger = logging.getLogger(__name__)

WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_COUNT = 5
WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_WINDOW_SECONDS = 10 * 60


def first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_id(value):
    if isinstance(value, dict):
        value = first_present(value, "id", "user_id", "chat_id")
    return str(value or "")


def get_callback_update(update):
    return first_present(update, "callback_query", "callbackQuery", "callback")


def get_message_update(update):
    return first_present(update, "message", "edited_message", "editedMessage")


def get_callback_id(callback_query):
    return first_present(callback_query, "id", "callback_id", "callbackQueryId", "callback_query_id")


def get_callback_data(callback_query):
    return first_present(callback_query, "data", "callback_data", "callbackData", "payload", "value") or ""


def get_sender_object(payload):
    return first_present(payload, "from", "from_user", "fromUser", "user", "sender", "author") or {}


def get_chat_object(payload):
    return first_present(payload, "chat", "peer") or {}


def get_message_id(message):
    return first_present(message, "message_id", "messageId", "id")


def get_message_text(message):
    return (first_present(message, "text", "caption") or "").strip()


def extract_user_id(update):
    callback_query = get_callback_update(update)
    if callback_query:
        return normalize_id(get_sender_object(callback_query))
    message = get_message_update(update)
    if message:
        return normalize_id(get_sender_object(message)) or normalize_id(get_chat_object(message))
    return ""


def extract_chat_id(update):
    callback_query = get_callback_update(update)
    if callback_query:
        message = first_present(callback_query, "message") or {}
        return (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or extract_user_id(update)
        )
    message = get_message_update(update)
    if message:
        return (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or extract_user_id(update)
        )
    return ""


def extract_callback_message_reference(callback_query, *, fallback_chat_id=""):
    message = first_present(callback_query, "message") or {}
    return {
        "chat_id": (
            normalize_id(get_chat_object(message))
            or normalize_id(first_present(message, "chat_id", "chatId"))
            or fallback_chat_id
        ),
        "message_id": get_message_id(message),
    }


def delete_callback_message(client, callback_query, *, fallback_chat_id=""):
    message_ref = extract_callback_message_reference(callback_query or {}, fallback_chat_id=fallback_chat_id)
    try:
        return client.delete_message(
            chat_id=message_ref["chat_id"],
            message_id=message_ref["message_id"],
        )
    except BotDeliveryError as exc:
        logger.info(
            "Could not delete callback message provider=%s chat_id=%s message_id=%s: %s",
            client.config.provider,
            message_ref["chat_id"],
            message_ref["message_id"],
            exc,
        )
    return None


def sender_display_name(sender):
    first_name = first_present(sender, "first_name", "firstName") or ""
    last_name = first_present(sender, "last_name", "lastName") or ""
    username = first_present(sender, "username") or ""
    display_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if not display_name and username:
        display_name = f"@{username}"
    return display_name


def normalize_bot_username(username):
    return str(username or "").strip().lstrip("@")[:80]


def customer_username_is_available(customer, username):
    username = normalize_bot_username(username)
    if not username:
        return False
    qs = Customer.objects.filter(username=username)
    if customer and customer.pk:
        qs = qs.exclude(pk=customer.pk)
    return not qs.exists()


def update_customer_identity_from_bot(customer, *, display_name="", username=""):
    if not customer:
        return None

    changed_fields = []
    display_name = str(display_name or "").strip()[:120]
    username = normalize_bot_username(username)
    if display_name and (
        not customer.display_name
        or customer.display_name.startswith("Customer ")
        or customer.display_name == customer.phone_number
    ):
        customer.display_name = display_name
        changed_fields.append("display_name")
    if username and not customer.username and customer_username_is_available(customer, username):
        customer.username = username
        changed_fields.append("username")
    if changed_fields:
        customer.save(update_fields=[*changed_fields, "updated_at"])
    return customer


def get_or_create_bot_customer(*, display_name="", username=""):
    username = normalize_bot_username(username)
    if username:
        customer = Customer.objects.filter(username=username).first()
        if customer:
            return update_customer_identity_from_bot(customer, display_name=display_name, username=username)

    defaults = {"display_name": (display_name or (f"@{username}" if username else "") or "کاربر ربات")[:120]}
    if username:
        defaults["username"] = username
    return Customer.objects.create(**defaults)


def update_bot_user_from_update(config, update, *, chat_id, user_id, attach_customer=True):
    callback_query = get_callback_update(update)
    payload = callback_query or get_message_update(update) or {}
    sender = get_sender_object(payload)
    username = (first_present(sender, "username") or "").strip()
    first_name = (first_present(sender, "first_name", "firstName") or "").strip()
    last_name = (first_present(sender, "last_name", "lastName") or "").strip()
    display_name = sender_display_name(sender) or f"{config.get_provider_display()} user {user_id}"
    now = timezone.now()

    bot_user = BotUser.objects.filter(bot_config=config, provider_user_id=str(user_id)).select_related("customer").first()
    if bot_user:
        bot_user.chat_id = str(chat_id or bot_user.chat_id or user_id)
        bot_user.username = username
        bot_user.first_name = first_name
        bot_user.last_name = last_name
        bot_user.display_name = display_name
        bot_user.last_seen_at = now
        bot_user.is_active = True
        if attach_customer and not bot_user.customer_id:
            bot_user.customer = get_or_create_bot_customer(display_name=display_name, username=username)
        elif bot_user.customer_id:
            update_customer_identity_from_bot(bot_user.customer, display_name=display_name, username=username)
        bot_user.save(
            update_fields=[
                "chat_id",
                "username",
                "first_name",
                "last_name",
                "display_name",
                "last_seen_at",
                "is_active",
                "customer",
                "updated_at",
            ]
        )
        return bot_user

    customer = get_or_create_bot_customer(display_name=display_name, username=username) if attach_customer else None
    return BotUser.objects.create(
        bot_config=config,
        customer=customer,
        provider_user_id=str(user_id),
        chat_id=str(chat_id or user_id),
        username=username,
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        last_seen_at=now,
    )


def referral_code_from_start_text(text):
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0].lower() != "/start":
        return ""
    payload = parts[1].strip()
    if payload.lower().startswith("link_"):
        return ""
    for prefix in ("ref_", "ref-", "ref:"):
        if payload.lower().startswith(prefix):
            payload = payload[len(prefix):]
            break
    return payload.strip()


def web_telegram_link_token_from_start_text(text):
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0].lower() != "/start":
        return ""
    payload = parts[1].strip()
    if not payload.lower().startswith("link_"):
        return ""
    return payload[5:].strip()


def is_web_telegram_link_start_text(text):
    return bool(web_telegram_link_token_from_start_text(text))


def web_telegram_link_invalid_rate_limited(config, bot_user):
    key = (
        f"rate:web_telegram_link_invalid:{config.pk}:"
        f"{bot_user.provider_user_id or bot_user.chat_id or bot_user.pk}"
    )
    if cache.add(key, 1, timeout=WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_WINDOW_SECONDS):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_WINDOW_SECONDS)
        return False
    return count > WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_COUNT


def dispatch_bot_update(
    *,
    config,
    update,
    callback_query,
    message,
    user_id,
    chat_id,
    is_admin,
    callback_data,
    deps,
):
    if callback_query and (
        callback_data.startswith("user:") or callback_data == deps["CHECK_MEMBERSHIP_CALLBACK"]
    ):
        return deps["handle_user_update"](config, update, chat_id=chat_id, user_id=user_id)

    if is_admin and callback_query and deps["is_support_callback_data"](callback_data):
        return deps["handle_support_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and deps["is_customer_analytics_callback_data"](callback_data):
        return deps["handle_customer_analytics_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and deps["is_broadcast_callback_data"](callback_data):
        return deps["handle_broadcast_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and deps["is_admin_config_management_callback_data"](callback_data):
        return deps["handle_admin_config_management_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and deps["is_admin_menu_callback_data"](callback_data):
        return deps["handle_admin_menu_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and callback_query and deps["is_admin_callback_data"](callback_data):
        return deps["handle_callback_update"](config, callback_query, chat_id=chat_id)

    if is_admin and message:
        admin_result = deps["handle_message_update"](config, message, chat_id=chat_id, admin_user_id=user_id or chat_id)
        if not admin_result.get("ignored"):
            return admin_result

    if not is_admin:
        return deps["handle_user_update"](config, update, chat_id=chat_id, user_id=user_id)

    if callback_query and deps["is_support_callback_data"](callback_data):
        return deps["handle_support_callback_update"](config, callback_query, chat_id=chat_id)
    if callback_query and deps["is_customer_analytics_callback_data"](callback_data):
        return deps["handle_customer_analytics_callback_update"](config, callback_query, chat_id=chat_id)
    if callback_query and deps["is_broadcast_callback_data"](callback_data):
        return deps["handle_broadcast_callback_update"](config, callback_query, chat_id=chat_id)
    if callback_query and deps["is_admin_config_management_callback_data"](callback_data):
        return deps["handle_admin_config_management_callback_update"](config, callback_query, chat_id=chat_id)
    if callback_query and deps["is_admin_menu_callback_data"](callback_data):
        return deps["handle_admin_menu_callback_update"](config, callback_query, chat_id=chat_id)
    if callback_query:
        return deps["handle_callback_update"](config, callback_query, chat_id=chat_id)
    if message:
        user_result = deps["handle_user_update"](config, update, chat_id=chat_id, user_id=user_id)
        if not user_result.get("ignored"):
            return user_result
    return {"ok": True, "ignored": True}


def dispatch_user_callback(config, bot_user, callback_query, *, chat_id, deps):
    client = deps["BotClient"](config)
    callback_id = deps["get_callback_id"](callback_query)
    data = deps["get_callback_data"](callback_query)
    client.answer_callback(callback_id, "دریافت شد")
    if not data.startswith(deps["CONFIG_COPY_CALLBACK_PREFIX"]):
        deps["delete_callback_message"](client, callback_query, fallback_chat_id=chat_id)

    if data == deps["CHECK_MEMBERSHIP_CALLBACK"]:
        membership_response = deps["telegram_membership_required_response"](
            config,
            bot_user,
            client,
            chat_id=chat_id,
            force_refresh=True,
        )
        if membership_response:
            return membership_response
        return deps["send_main_menu"](client, bot_user, chat_id=chat_id)

    if data.startswith("user:"):
        membership_response = deps["telegram_membership_required_response"](config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response

    if data == "user:menu":
        return deps["send_main_menu"](client, bot_user, chat_id=chat_id)
    if data == "user:cancel":
        bot_user.reset_state()
        client.send_message(
            "فرایند لغو شد.",
            chat_id=chat_id,
            reply_markup=deps["main_menu_keyboard"](is_admin=deps["is_admin_bot_user"](config, bot_user)),
        )
        return {"ok": True, "cancelled": True}
    if data.startswith(deps["CONFIG_COPY_CALLBACK_PREFIX"]):
        return deps["handle_config_copy_callback"](config, data, client=client, chat_id=chat_id)
    if data == "user:client_delete_cancel":
        return deps["cancel_user_client_delete_flow"](
            client,
            chat_id=chat_id,
            is_admin=deps["is_admin_bot_user"](config, bot_user),
            bot_user=bot_user,
        )
    if data.startswith(deps["USER_CLIENT_DELETE_CONFIRM_CALLBACK_PREFIX"]):
        token = deps["_token_after_prefix"](data, deps["USER_CLIENT_DELETE_CONFIRM_CALLBACK_PREFIX"])
        return deps["confirm_user_client_delete_flow"](client, config, bot_user, token, chat_id=chat_id)
    if data.startswith(deps["USER_CLIENT_DELETE_CALLBACK_PREFIX"]):
        token = deps["_token_after_prefix"](data, deps["USER_CLIENT_DELETE_CALLBACK_PREFIX"])
        return deps["start_user_client_delete_flow"](client, config, bot_user, token, chat_id=chat_id)
    if data == deps["PAYMENT_NAME_CALLBACK"]:
        return deps["start_optional_config_name_flow"](client, config, bot_user, chat_id=chat_id)
    if data == deps["PAYMENT_RECEIPT_ONLY_CALLBACK"]:
        return deps["continue_with_receipt_only"](client, config, bot_user, chat_id=chat_id)
    if data.startswith("user:copy:"):
        copy_kind = data.rsplit(":", 1)[-1]
        copy_value = deps["copy_payment_value_from_state"](config, bot_user, copy_kind)
        if not copy_value:
            client.send_message(
                "مقدار قابل کپی پیدا نشد. دوباره مرحله پرداخت را باز کنید.",
                chat_id=chat_id,
                reply_markup=deps["main_menu_keyboard"](),
            )
            return {"ok": True, "success": False}
        label = "شماره کارت" if copy_kind == "payment_card" else "مبلغ"
        client.send_message(
            f"{label}:\n{deps['telegram_code'](copy_value)}",
            chat_id=chat_id,
            reply_markup=deps["cancel_keyboard"](),
            parse_mode="HTML",
            force_parse_mode=True,
        )
        return {"ok": True, "handled": True}
    if data == "user:help":
        return deps["send_help"](client, config, bot_user, chat_id=chat_id)
    if data == "user:free_trial":
        return deps["start_free_trial_flow"](client, config, bot_user, chat_id=chat_id)
    if data == "user:free_trial_confirm":
        return deps["confirm_free_trial_flow"](client, config, bot_user, chat_id=chat_id)
    if data == "user:free_trial_cancel":
        return deps["cancel_free_trial_flow"](client, config, bot_user, chat_id=chat_id)
    if data.startswith(deps["CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX"]):
        return deps["handle_config_lookup_update_callback"](config, bot_user, data, client=client, chat_id=chat_id)
    if data == "user:config_lookup":
        return deps["start_config_lookup_flow"](client, bot_user, chat_id=chat_id)
    if deps["_is_support_user_callback_data"](data):
        if data == "user:support":
            return deps["start_support_flow"](client, config, bot_user, chat_id=chat_id)
        category = data.rsplit(":", 1)[-1]
        return deps["select_support_category"](client, bot_user, category, chat_id=chat_id)
    if deps["_is_buy_callback_data"](data):
        return deps["_handle_buy_callback"](
            client,
            config,
            bot_user,
            data,
            chat_id=chat_id,
            show_payment_step_func=deps["show_payment_step"],
            send_renewal_summary_func=deps["send_renewal_summary"],
            renewal_context_from_state_func=deps["renewal_context_from_state"],
        )
    if data in {"user:profile", "user:profile_phone"}:
        return deps["_handle_profile_callback"](
            client,
            bot_user,
            data,
            chat_id=chat_id,
            send_profile_func=deps["send_profile"],
        )
    if deps["_is_referral_callback_data"](data):
        return deps["_handle_referral_callback"](
            client,
            config,
            bot_user,
            data,
            chat_id=chat_id,
            get_current_store_func=deps["get_current_store"],
        )
    if data == "user:renew":
        return deps["show_user_services"](
            client,
            bot_user,
            chat_id=chat_id,
            title="کدام کانفیگ را تمدید می‌کنید؟",
            force_refresh=True,
            renew_mode=True,
        )
    if data == "user:orders" or data.startswith("user:order:") or data.startswith("user:order_cancel:"):
        return deps["_handle_user_order_callback"](
            client,
            bot_user,
            data,
            chat_id=chat_id,
            bot_order_status_func=deps["bot_order_status"],
            bot_verification_status_func=deps["bot_verification_status"],
            bot_client_label_func=deps["bot_client_label"],
            bot_client_status_func=deps["bot_client_status"],
            sync_vpn_client_stats_func=deps["sync_vpn_client_stats"],
            user_client_delete_button_func=deps["user_client_delete_button"],
        )
    if data == "user:usage":
        return deps["show_user_services"](
            client,
            bot_user,
            chat_id=chat_id,
            title="حجم باقی‌مانده اشتراک‌های شما",
            force_refresh=True,
        )
    if data == "user:subs":
        return deps["show_user_services"](client, bot_user, chat_id=chat_id)
    if data.startswith("user:client_usage:"):
        public_id = data.rsplit(":", 1)[-1]
        return deps["handle_user_client_usage"](client, config, bot_user, public_id, chat_id=chat_id)
    if data.startswith("user:client_config:"):
        public_id = data.rsplit(":", 1)[-1]
        return deps["handle_user_client_config"](client, config, bot_user, public_id, chat_id=chat_id)
    if data.startswith("user:client_refresh:"):
        public_id = data.rsplit(":", 1)[-1]
        return deps["handle_user_client_refresh"](client, config, bot_user, public_id, chat_id=chat_id)
    if data.startswith("user:client_renew:"):
        public_id = data.rsplit(":", 1)[-1]
        return deps["start_renewal_flow"](client, config, bot_user, public_id, chat_id=chat_id)

    return {"ok": True, "ignored": True}


def dispatch_user_message(config, bot_user, message, *, chat_id, deps):
    client = deps["BotClient"](config)
    text = deps["get_message_text"](message)
    lowered = text.lower()
    contact_phone = deps["extract_contact_phone"](message)
    BotUser = deps["BotUser"]

    if lowered in {"/cancel", "cancel", "لغو", "انصراف"}:
        bot_user.reset_state()
        client.send_message("فرایند لغو شد.", chat_id=chat_id, reply_markup=deps["remove_reply_keyboard"]())
        membership_response = deps["telegram_membership_required_response"](config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response
        client.send_message(
            "از منوی زیر ادامه دهید.",
            chat_id=chat_id,
            reply_markup=deps["main_menu_keyboard"](is_admin=deps["is_admin_bot_user"](config, bot_user)),
        )
        return {"ok": True, "cancelled": True}

    if lowered == "start" or lowered == "/start" or lowered.startswith("/start "):
        link_token = deps["web_telegram_link_token_from_start_text"](text)
        if link_token:
            result = deps["link_bot_user_to_customer"](
                link_token,
                bot_user,
                telegram_user_id=bot_user.provider_user_id,
            )
            if result.success:
                bot_user.refresh_from_db()
                client.send_message(result.message, chat_id=chat_id)
                membership_response = deps["telegram_membership_required_response"](
                    config,
                    bot_user,
                    client,
                    chat_id=chat_id,
                )
                if membership_response:
                    return membership_response
                return deps["send_main_menu"](client, bot_user, chat_id=chat_id)

            if result.code in {"invalid_token", "expired_token", "invalid_bot_user"}:
                if deps["web_telegram_link_invalid_rate_limited"](config, bot_user):
                    client.send_message(
                        "تلاش‌های ناموفق زیاد شد. کمی بعد دوباره از داشبورد سایت لینک جدید بگیرید.",
                        chat_id=chat_id,
                    )
                    return {"ok": True, "success": False, "rate_limited": True}
                client.send_message(deps["WEB_TELEGRAM_INVALID_MESSAGE"], chat_id=chat_id)
                return {"ok": True, "success": False, "invalid_link": True}

            client.send_message(result.message, chat_id=chat_id)
            return {"ok": True, "success": False, "link_conflict": True}

        referral_code = deps["referral_code_from_start_text"](text)
        if referral_code and bot_user.customer_id:
            deps["apply_referral_code"](bot_user.customer, referral_code)
        membership_response = deps["telegram_membership_required_response"](config, bot_user, client, chat_id=chat_id)
        if membership_response:
            return membership_response
        return deps["send_main_menu"](client, bot_user, chat_id=chat_id)

    membership_response = deps["telegram_membership_required_response"](config, bot_user, client, chat_id=chat_id)
    if membership_response:
        return membership_response

    if contact_phone and bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        success, feedback = deps["save_bot_user_phone"](bot_user, contact_phone)
        bot_user.reset_state()
        client.send_message(feedback, chat_id=chat_id, reply_markup=deps["remove_reply_keyboard"]())
        if success:
            return deps["send_profile"](client, bot_user, chat_id=chat_id)
        return {"ok": True, "success": False}

    if contact_phone and bot_user.state == BotUser.State.IDLE:
        success, feedback = deps["save_bot_user_phone"](bot_user, contact_phone)
        client.send_message(feedback, chat_id=chat_id, reply_markup=deps["remove_reply_keyboard"]())
        if success:
            return deps["send_profile"](client, bot_user, chat_id=chat_id)
        return {"ok": True, "success": False}

    if deps["is_admin_bot_user"](config, bot_user) and lowered in {
        "گزارش مشتریان",
        "گزارش مشتریان 📊",
        "/customer_reports",
        "customer_reports",
    }:
        client.send_message(
            deps["customer_analytics_menu_text"](),
            chat_id=chat_id,
            reply_markup=deps["customer_analytics_keyboard"](),
        )
        return {"ok": True, "customer_analytics_menu": True}
    if deps["is_admin_bot_user"](config, bot_user) and lowered in {
        "ارسال پیام",
        "ارسال پیام 📣",
        "/broadcast",
        "broadcast",
    }:
        return deps["start_broadcast_flow"](client, config, bot_user, chat_id=chat_id)
    if lowered in {"بعدا ثبت می‌کنم", "بعدا", "بعداً", "skip"} and bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        bot_user.reset_state()
        client.send_message(
            "باشه، هر وقت خواستید از بخش پروفایل شماره را ثبت کنید.",
            chat_id=chat_id,
            reply_markup=deps["remove_reply_keyboard"](),
        )
        return deps["send_profile"](client, bot_user, chat_id=chat_id)

    if deps["is_admin_bot_user"](config, bot_user) and bot_user.state == BotUser.State.BROADCAST_WAIT_TEXT:
        return deps["handle_broadcast_text"](config, bot_user, text, chat_id=chat_id)

    if deps["is_admin_bot_user"](config, bot_user) and bot_user.state == BotUser.State.BROADCAST_CONFIRM:
        client.send_message(
            "برای ارسال یا لغو از دکمه‌های پیش‌نمایش استفاده کنید.",
            chat_id=chat_id,
            reply_markup=deps["broadcast_confirm_keyboard"](),
        )
        return {"ok": True, "broadcast_confirm_waiting": True}

    if deps["is_admin_bot_user"](config, bot_user) and bot_user.state in {
        deps["BOT_STATE_ADMIN_CONFIG_WAIT_TRAFFIC_GB"],
        deps["BOT_STATE_ADMIN_CONFIG_WAIT_EXPIRY_DAYS"],
    }:
        return deps["handle_admin_config_waiting_text"](config, bot_user, text, chat_id=chat_id)

    if lowered in {"/plans", "plans", "پلن", "پلن‌ها", "پلن ها"}:
        return deps["send_plan_list"](client, config, chat_id=chat_id)
    if lowered in {"/buy", "buy", "خرید", "خرید سرویس", "خرید سرویس 🛒"}:
        return deps["send_plan_list"](client, config, chat_id=chat_id, buy_mode=True)
    if lowered in {
        "/free_trial",
        "free_trial",
        "free trial",
        "تست رایگان",
        "دریافت تست رایگان",
        "تست رایگان 🎁",
        "🎁 دریافت تست رایگان",
    }:
        return deps["start_free_trial_flow"](client, config, bot_user, chat_id=chat_id)
    if lowered in {"/profile", "profile", "پروفایل", "حساب"}:
        return deps["send_profile"](client, bot_user, chat_id=chat_id)
    if lowered in {"/phone", "phone", "شماره", "موبایل"}:
        return deps["start_profile_phone_flow"](client, bot_user, chat_id=chat_id)
    if lowered in {"/orders", "orders", "سفارش", "سفارش‌ها", "سفارش ها", "سفارش‌های من 📦"}:
        text, reply_markup = deps["format_user_orders_list"](bot_user)
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return {"ok": True, "handled": True}
    if lowered in {"/usage", "usage", "حجم", "باقی مانده", "باقی‌مانده"}:
        return deps["show_user_services"](
            client,
            bot_user,
            chat_id=chat_id,
            title="حجم باقی‌مانده اشتراک‌های شما",
            force_refresh=True,
        )
    if lowered in {"/subscription", "/subs", "subscription", "سرویس‌های من", "سرویس های من", "سرویس‌های من 🔐"}:
        return deps["show_user_services"](client, bot_user, chat_id=chat_id)
    if lowered in {"/renew", "renew", "تمدید", "تمدید سرویس", "تمدید سرویس 🔄"}:
        return deps["show_user_services"](
            client,
            bot_user,
            chat_id=chat_id,
            title="کدام کانفیگ را تمدید می‌کنید؟",
            force_refresh=True,
            renew_mode=True,
        )
    if lowered in {
        "/check_config",
        "check_config",
        "مشاهده باقی‌مانده کانفیگ",
        "مشاهده باقی مانده کانفیگ",
        "مشاهده باقی‌مانده کانفیگ 📊",
        "مشاهده باقی‌مانده 📊",
    }:
        return deps["start_config_lookup_flow"](client, bot_user, chat_id=chat_id)
    if lowered in {"/referrals", "referrals", "دعوت دوستان", "دعوت دوستان 🎁"}:
        client.send_message(
            deps["format_referral_panel"](bot_user, config),
            chat_id=chat_id,
            reply_markup=deps["referral_keyboard"](bot_user, config),
        )
        return {"ok": True, "handled": True}
    if lowered in {"/support", "support", "پشتیبانی", "پشتیبانی 💬"}:
        return deps["start_support_flow"](client, config, bot_user, chat_id=chat_id)
    if lowered in {"/help", "help", "راهنما", "راهنما ❓"}:
        return deps["send_help"](client, config, bot_user, chat_id=chat_id)

    if bot_user.state == deps["BOT_STATE_CONFIG_LOOKUP_WAIT_LINK"]:
        return deps["handle_config_lookup_text"](config, bot_user, text, chat_id=chat_id)

    if bot_user.state == deps["BOT_STATE_SUPPORT_WAIT_MESSAGE"]:
        return deps["create_support_ticket_from_bot"](config, bot_user, text, message, chat_id=chat_id)

    if bot_user.state == deps["BOT_STATE_BUY_WAIT_DISCOUNT"]:
        if lowered in {"skip", "رد", "رد شدن", "بدون تخفیف"}:
            return deps["skip_discount_for_bot"](client, config, bot_user, chat_id=chat_id)
        return deps["apply_discount_code_for_bot"](client, config, bot_user, text, chat_id=chat_id)

    if bot_user.state == BotUser.State.PROFILE_WAIT_PHONE:
        success, feedback = deps["save_bot_user_phone"](bot_user, text)
        if not success:
            client.send_message(feedback, chat_id=chat_id, reply_markup=deps["phone_request_keyboard"]())
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(feedback, chat_id=chat_id, reply_markup=deps["remove_reply_keyboard"]())
        return deps["send_profile"](client, bot_user, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_CUSTOM_VOLUME:
        return deps["continue_purchase_after_custom_volume"](client, config, bot_user, text, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_QUANTITY:
        try:
            quantity = deps["validate_order_quantity"](deps["normalize_bot_number"](text))
        except deps["ValidationError"]:
            client.send_message(
                f"تعداد باید عددی بین ۱ تا {deps['persian_digits'](deps['MAX_ORDER_QUANTITY'])} باشد.",
                chat_id=chat_id,
                reply_markup=deps["quantity_keyboard"](),
            )
            return {"ok": True, "handled": True}
        return deps["continue_purchase_after_quantity"](client, config, bot_user, quantity, chat_id=chat_id)

    if bot_user.state == BotUser.State.BUY_WAIT_NAME:
        return deps["_handle_buy_wait_name_message"](
            config,
            bot_user,
            message,
            text,
            client=client,
            chat_id=chat_id,
            extract_receipt_file_func=deps["extract_receipt_file"],
            receipt_file_type_error_func=deps["receipt_file_type_error"],
            optional_config_name_keyboard_func=deps["optional_config_name_keyboard"],
            cancel_keyboard_func=deps["cancel_keyboard"],
            payment_prompt_after_name_func=deps["payment_prompt_after_name"],
            is_admin_bot_user_func=deps["is_admin_bot_user"],
            finalize_admin_direct_renewal_func=deps["finalize_admin_direct_renewal"],
            finalize_admin_direct_purchase_func=deps["finalize_admin_direct_purchase"],
            finalize_bot_renewal_func=deps["finalize_bot_renewal"],
            finalize_bot_purchase_func=deps["finalize_bot_purchase"],
        )

    if deps["_is_legacy_receipt_state"](bot_user.state):
        return deps["_handle_legacy_receipt_state_message"](
            client,
            bot_user,
            chat_id=chat_id,
            cancel_keyboard_func=deps["cancel_keyboard"],
        )

    if bot_user.state == BotUser.State.BUY_WAIT_RECEIPT:
        return deps["_handle_buy_wait_receipt_message"](
            config,
            bot_user,
            message,
            client=client,
            chat_id=chat_id,
            extract_receipt_file_func=deps["extract_receipt_file"],
            receipt_file_type_error_func=deps["receipt_file_type_error"],
            cancel_keyboard_func=deps["cancel_keyboard"],
            finalize_bot_renewal_func=deps["finalize_bot_renewal"],
            finalize_bot_purchase_func=deps["finalize_bot_purchase"],
        )

    if text:
        client.send_message("برای شروع از منوی زیر استفاده کنید.", chat_id=chat_id, reply_markup=deps["main_menu_keyboard"]())
        return {"ok": True, "handled": True}

    return {"ok": True, "ignored": True}
