import re

from store.models import BotUser, Customer

from .formatting import normalize_bot_number


def phone_request_keyboard():
    return {
        "keyboard": [
            [{"text": "ارسال شماره موبایل", "request_contact": True}],
            [{"text": "بعدا ثبت می‌کنم"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def normalize_bot_phone_number(value):
    compact = normalize_bot_number(value)
    compact = re.sub(r"[\s\-\(\)\.]+", "", compact)
    if compact.startswith("0098"):
        compact = f"0{compact[4:]}"
    elif compact.startswith("+98") and len(compact) == 13:
        compact = f"0{compact[3:]}"
    elif compact.startswith("98") and len(compact) == 12:
        compact = f"0{compact[2:]}"
    elif compact.startswith("9") and len(compact) == 10:
        compact = f"0{compact}"
    return compact[:30]


def is_valid_bot_phone_number(phone_number):
    return bool(
        re.fullmatch(r"09\d{9}", phone_number or "")
        or re.fullmatch(r"\+\d{8,15}", phone_number or "")
        or re.fullmatch(r"\d{8,15}", phone_number or "")
    )


def _first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_contact_phone(message):
    contact = _first_present(message, "contact") or {}
    if not isinstance(contact, dict):
        return ""
    return _first_present(contact, "phone_number", "phoneNumber", "phone") or ""


def save_bot_user_phone(
    bot_user,
    raw_phone,
    *,
    get_or_create_bot_customer_func,
    update_customer_identity_from_bot_func,
    customer_username_is_available_func,
    normalize_bot_username_func,
):
    phone_number = normalize_bot_phone_number(raw_phone)
    if not is_valid_bot_phone_number(phone_number):
        return False, "شماره موبایل معتبر نیست. لطفا شماره را مثل 09123456789 ارسال کنید."

    if not bot_user.customer_id:
        bot_user.customer = get_or_create_bot_customer_func(display_name=bot_user.display_name, username=bot_user.username)
        bot_user.save(update_fields=["customer", "updated_at"])

    customer = bot_user.customer
    existing_customer = Customer.objects.filter(phone_number=phone_number).exclude(pk=customer.pk).first()
    if existing_customer:
        if not customer.orders.exists():
            bot_user.customer = update_customer_identity_from_bot_func(
                existing_customer,
                display_name=bot_user.display_name,
                username=bot_user.username,
            )
            bot_user.save(update_fields=["customer", "updated_at"])
            return True, "این شماره قبلا ثبت شده بود؛ پروفایل ربات به همان حساب وصل شد."
        return False, "این شماره قبلا برای یک پروفایل دیگر ثبت شده است. اگر شماره برای شماست، از پشتیبانی کمک بگیرید."

    changed_fields = []
    if customer.phone_number != phone_number:
        customer.phone_number = phone_number
        changed_fields.append("phone_number")
    if bot_user.display_name and (
        not customer.display_name
        or customer.display_name.startswith("Customer ")
        or customer.display_name == customer.phone_number
    ):
        customer.display_name = bot_user.display_name[:120]
        changed_fields.append("display_name")
    if bot_user.username and not customer.username and customer_username_is_available_func(customer, bot_user.username):
        customer.username = normalize_bot_username_func(bot_user.username)
        changed_fields.append("username")
    if changed_fields:
        customer.save(update_fields=[*changed_fields, "updated_at"])
    return True, "شماره موبایل در پروفایل شما ذخیره شد."


def start_profile_phone_flow(client, bot_user, *, chat_id):
    bot_user.state = BotUser.State.PROFILE_WAIT_PHONE
    bot_user.state_data = {}
    bot_user.save(update_fields=["state", "state_data", "updated_at"])
    client.send_message(
        "برای ذخیره شماره، دکمه ارسال شماره موبایل را بزنید یا شماره را به صورت متن ارسال کنید.",
        chat_id=chat_id,
        reply_markup=phone_request_keyboard(),
    )
    return {"ok": True, "handled": True}


def handle_profile_callback(client, bot_user, data, *, chat_id, send_profile_func):
    if data == "user:profile":
        return send_profile_func(client, bot_user, chat_id=chat_id)
    if data == "user:profile_phone":
        return start_profile_phone_flow(client, bot_user, chat_id=chat_id)
    return {"ok": True, "ignored": True}
