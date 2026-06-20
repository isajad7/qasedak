import logging

from .config_delivery import send_config_links_message
from .user_menu import main_menu_keyboard

logger = logging.getLogger(__name__)

ADMIN_CONFIG_DELETE_CALLBACK_PREFIX = "admin:config_delete:"
ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX = "admin:config_delete_confirm:"
ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX = "admin:config_edit_traffic:"
ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX = "admin:config_traffic_add:"
ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX = "admin:config_traffic_set:"
ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX = "admin:config_traffic_confirm_set:"
ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX = "admin:config_edit_expiry:"
ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX = "admin:config_expiry_add:"
ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX = "admin:config_expiry_set_days:"
ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX = "admin:config_refresh_link:"
ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX = "admin:config_cancel:"
BOT_STATE_ADMIN_CONFIG_WAIT_TRAFFIC_GB = "admin_config_wait_traffic_gb"
BOT_STATE_ADMIN_CONFIG_WAIT_EXPIRY_DAYS = "admin_config_wait_expiry_days"


def admin_config_delete_confirmation_keyboard(token):
    return {
        "inline_keyboard": [
            [{"text": "بله، حذف شود 🗑", "callback_data": f"{ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX}{token}"}],
            [{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}],
        ]
    }


def admin_config_traffic_keyboard(token):
    return {
        "inline_keyboard": [
            [
                {"text": "+1 گیگ", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX}{token}:1"},
                {"text": "+5 گیگ", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX}{token}:5"},
            ],
            [
                {"text": "+10 گیگ", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX}{token}:10"},
                {"text": "+50 گیگ", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX}{token}:50"},
            ],
            [{"text": "تنظیم حجم کل", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX}{token}"}],
            [{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}],
        ]
    }


def admin_config_expiry_keyboard(token):
    return {
        "inline_keyboard": [
            [
                {"text": "+1 روز", "callback_data": f"{ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX}{token}:1"},
                {"text": "+7 روز", "callback_data": f"{ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX}{token}:7"},
            ],
            [
                {"text": "+30 روز", "callback_data": f"{ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX}{token}:30"},
                {"text": "+90 روز", "callback_data": f"{ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX}{token}:90"},
            ],
            [{"text": "تنظیم روز دلخواه", "callback_data": f"{ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX}{token}"}],
            [{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}],
        ]
    }


def admin_config_traffic_confirm_keyboard(token, gb):
    return {
        "inline_keyboard": [
            [{"text": "تایید تنظیم حجم", "callback_data": f"{ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX}{token}:{gb}"}],
            [{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}],
        ]
    }


def _token_after_prefix(data, prefix):
    return str(data or "")[len(prefix) :].strip()


def _split_token_value(data, prefix):
    remainder = _token_after_prefix(data, prefix)
    if ":" not in remainder:
        return remainder, ""
    token, value = remainder.rsplit(":", 1)
    return token, value


def _admin_config_payload_or_message(
    config,
    bot_user,
    token,
    client,
    *,
    chat_id,
    get_admin_config_management_payload_func,
    bot_user_cache_identity_func,
):
    payload = get_admin_config_management_payload_func(config, bot_user_cache_identity_func(bot_user), token)
    if payload:
        return payload
    client.send_message(
        "مهلت مدیریت این کانفیگ تمام شده. دوباره از مسیر مشاهده باقی‌مانده لینک را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=True),
    )
    return None


def _admin_config_error_message(client, message, *, chat_id, token=""):
    client.send_message(
        message or "عملیات انجام نشد. چند دقیقه دیگر تلاش کنید.",
        chat_id=chat_id,
        reply_markup=admin_config_traffic_keyboard(token) if token else main_menu_keyboard(is_admin=True),
    )


def handle_admin_config_management_callback_update(
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
    get_admin_config_management_payload_func,
    bot_user_cache_identity_func,
    delete_vpn_client_by_admin_lookup_func,
    update_vpn_client_limits_by_admin_func,
    refresh_vpn_client_link_by_admin_func,
    build_admin_delete_confirmation_text_func,
    build_admin_edit_summary_func,
    vpn_client_management_error_cls,
):
    callback_id = get_callback_id_func(callback_query)
    data = get_callback_data_func(callback_query)
    admin_user_id = normalize_id_func(get_sender_object_func(callback_query)) or chat_id
    client = client_cls(config)
    client.answer_callback(callback_id, "دریافت شد")
    delete_callback_message_func(client, callback_query, fallback_chat_id=chat_id)

    if not (config.is_admin_user(admin_user_id) or config.is_admin_user(chat_id)):
        client.send_message("شما اجازه مدیریت کانفیگ‌ها را ندارید.", chat_id=chat_id)
        return {"ok": True, "success": False, "permission_denied": True}

    bot_user = update_bot_user_from_update_func(
        config,
        {"callback_query": callback_query},
        chat_id=chat_id,
        user_id=admin_user_id,
    )

    def payload_or_message(token):
        return _admin_config_payload_or_message(
            config,
            bot_user,
            token,
            client,
            chat_id=chat_id,
            get_admin_config_management_payload_func=get_admin_config_management_payload_func,
            bot_user_cache_identity_func=bot_user_cache_identity_func,
        )

    if data.startswith(ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX):
        bot_user.reset_state()
        client.send_message("عملیات مدیریت کانفیگ لغو شد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "cancelled": True}

    if data.startswith(ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        try:
            result = delete_vpn_client_by_admin_lookup_func(admin_user_id, payload, reason="Deleted from config lookup")
        except vpn_client_management_error_cls as exc:
            client.send_message(
                "حذف کانفیگ انجام نشد. چند دقیقه دیگر تلاش کنید یا لاگ‌ها را بررسی کنید.",
                chat_id=chat_id,
                reply_markup=main_menu_keyboard(is_admin=True),
            )
            logger.warning("Admin config delete failed admin=%s token=%s error=%s", admin_user_id, token, exc)
            return {"ok": True, "success": False}
        bot_user.reset_state()
        local_status = result.get("local_match_status")
        extra = ""
        if local_status == "not_found":
            extra = "\nرکورد local متناظر پیدا نشد؛ حذف پنل انجام شد."
        elif local_status == "multiple":
            extra = "\nچند رکورد local match شد؛ برای جلوگیری از تغییر اشتباه فقط پنل حذف شد."
        client.send_message(
            f"✅ کانفیگ از پنل حذف شد.{extra}",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=True),
        )
        return {"ok": True, "success": True}

    if data.startswith(ADMIN_CONFIG_DELETE_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_DELETE_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        client.send_message(
            build_admin_delete_confirmation_text_func(payload),
            chat_id=chat_id,
            reply_markup=admin_config_delete_confirmation_keyboard(token),
        )
        return {"ok": True, "confirm_delete": True}

    if data.startswith(ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        client.send_message(
            "حجم جدید را انتخاب کنید یا عدد دلخواه را برای تنظیم حجم کل بفرستید.",
            chat_id=chat_id,
            reply_markup=admin_config_traffic_keyboard(token),
        )
        return {"ok": True, "traffic_menu": True}

    if data.startswith(ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX):
        token, gb = _split_token_value(data, ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        try:
            result = update_vpn_client_limits_by_admin_func(admin_user_id, payload, traffic_gb=gb, mode="add")
        except vpn_client_management_error_cls as exc:
            _admin_config_error_message(client, str(exc), chat_id=chat_id, token=token)
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(build_admin_edit_summary_func(result), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": True}

    if data.startswith(ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX):
        token, gb = _split_token_value(data, ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        try:
            result = update_vpn_client_limits_by_admin_func(admin_user_id, payload, set_total_gb=gb, mode="set")
        except vpn_client_management_error_cls as exc:
            _admin_config_error_message(client, str(exc), chat_id=chat_id, token=token)
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(build_admin_edit_summary_func(result), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": True}

    if data.startswith(ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        bot_user.state = BOT_STATE_ADMIN_CONFIG_WAIT_TRAFFIC_GB
        bot_user.state_data = {"admin_config_token": token}
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            "حجم کل جدید را به گیگابایت ارسال کنید. مثال: 30",
            chat_id=chat_id,
            reply_markup={"inline_keyboard": [[{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}]]},
        )
        return {"ok": True, "waiting_for_traffic": True}

    if data.startswith(ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        client.send_message(
            "زمان جدید را انتخاب کنید یا عدد روز دلخواه را بفرستید.",
            chat_id=chat_id,
            reply_markup=admin_config_expiry_keyboard(token),
        )
        return {"ok": True, "expiry_menu": True}

    if data.startswith(ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX):
        token, days = _split_token_value(data, ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        try:
            result = update_vpn_client_limits_by_admin_func(admin_user_id, payload, expiry_days=days)
        except vpn_client_management_error_cls as exc:
            client.send_message(str(exc), chat_id=chat_id, reply_markup=admin_config_expiry_keyboard(token))
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(build_admin_edit_summary_func(result), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": True}

    if data.startswith(ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        bot_user.state = BOT_STATE_ADMIN_CONFIG_WAIT_EXPIRY_DAYS
        bot_user.state_data = {"admin_config_token": token}
        bot_user.save(update_fields=["state", "state_data", "updated_at"])
        client.send_message(
            "تعداد روز را ارسال کنید. اگر تاریخ فعلی آینده باشد، روزها به همان تاریخ اضافه می‌شود؛ در غیر این صورت از امروز حساب می‌شود.",
            chat_id=chat_id,
            reply_markup={"inline_keyboard": [[{"text": "انصراف", "callback_data": f"{ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX}{token}"}]]},
        )
        return {"ok": True, "waiting_for_expiry": True}

    if data.startswith(ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX):
        token = _token_after_prefix(data, ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX)
        payload = payload_or_message(token)
        if not payload:
            return {"ok": True, "expired": True}
        try:
            details = refresh_vpn_client_link_by_admin_func(admin_user_id, payload)
        except vpn_client_management_error_cls as exc:
            client.send_message(str(exc), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
            return {"ok": True, "success": False}
        updated_link = details.get("updated_config_link") or ""
        if not updated_link:
            client.send_message("لینک به‌روز برای این کانفیگ ساخته نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
            return {"ok": True, "success": False}
        send_config_links_message(
            client,
            chat_id,
            direct_link=updated_link,
            title="✅ لینک کانفیگ به‌روز شد",
            keyboard=main_menu_keyboard(is_admin=True),
        )
        return {"ok": True, "success": True}

    return {"ok": True, "ignored": True}


def handle_admin_config_waiting_text(
    config,
    bot_user,
    text,
    *,
    chat_id,
    client_cls,
    get_admin_config_management_payload_func,
    bot_user_cache_identity_func,
    normalize_bot_number_func,
    gb_to_bytes_func,
    update_vpn_client_limits_by_admin_func,
    build_admin_edit_summary_func,
    vpn_client_management_error_cls,
):
    client = client_cls(config)
    token = (bot_user.state_data or {}).get("admin_config_token") or ""
    payload = _admin_config_payload_or_message(
        config,
        bot_user,
        token,
        client,
        chat_id=chat_id,
        get_admin_config_management_payload_func=get_admin_config_management_payload_func,
        bot_user_cache_identity_func=bot_user_cache_identity_func,
    )
    if not payload:
        bot_user.reset_state()
        return {"ok": True, "expired": True}

    admin_user_id = bot_user.provider_user_id or bot_user.chat_id
    if bot_user.state == BOT_STATE_ADMIN_CONFIG_WAIT_TRAFFIC_GB:
        raw_gb = normalize_bot_number_func(text).strip()
        try:
            new_total_bytes = gb_to_bytes_func(raw_gb)
        except vpn_client_management_error_cls:
            client.send_message("حجم باید عدد مثبت باشد. مثال: 30", chat_id=chat_id, reply_markup=admin_config_traffic_keyboard(token))
            return {"ok": True, "success": False}
        used_bytes = payload.get("used_bytes")
        if used_bytes is not None and int(used_bytes or 0) > new_total_bytes:
            client.send_message(
                "مصرف فعلی بیشتر از حجم جدید است. این کار ممکن است سرویس را محدود کند. تایید می‌کنید؟",
                chat_id=chat_id,
                reply_markup=admin_config_traffic_confirm_keyboard(token, raw_gb),
            )
            return {"ok": True, "warning": True}
        try:
            result = update_vpn_client_limits_by_admin_func(admin_user_id, payload, set_total_gb=raw_gb, mode="set")
        except vpn_client_management_error_cls as exc:
            client.send_message(str(exc), chat_id=chat_id, reply_markup=admin_config_traffic_keyboard(token))
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(build_admin_edit_summary_func(result), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": True}

    if bot_user.state == BOT_STATE_ADMIN_CONFIG_WAIT_EXPIRY_DAYS:
        raw_days = normalize_bot_number_func(text).strip()
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            client.send_message("روز باید عدد مثبت باشد. مثال: 30", chat_id=chat_id, reply_markup=admin_config_expiry_keyboard(token))
            return {"ok": True, "success": False}
        if days <= 0:
            client.send_message("روز باید عدد مثبت باشد. مثال: 30", chat_id=chat_id, reply_markup=admin_config_expiry_keyboard(token))
            return {"ok": True, "success": False}
        try:
            result = update_vpn_client_limits_by_admin_func(admin_user_id, payload, expiry_days=days)
        except vpn_client_management_error_cls as exc:
            client.send_message(str(exc), chat_id=chat_id, reply_markup=admin_config_expiry_keyboard(token))
            return {"ok": True, "success": False}
        bot_user.reset_state()
        client.send_message(build_admin_edit_summary_func(result), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "success": True}

    return {"ok": True, "ignored": True}
