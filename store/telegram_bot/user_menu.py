from store.jalali import persian_digits
from store.models import VPNClient

from .formatting import bot_gb_from_bytes


def main_menu_keyboard(*, is_admin=False):
    rows = [
        [
            {"text": "خرید سرویس 🛒", "callback_data": "user:buy"},
            {"text": "سرویس‌های من 🔐", "callback_data": "user:subs"},
        ],
        [
            {"text": "تست رایگان 🎁", "callback_data": "user:free_trial"},
        ],
        [
            {"text": "تمدید سرویس 🔄", "callback_data": "user:renew"},
            {"text": "سفارش‌های من 📦", "callback_data": "user:orders"},
        ],
        [
            {"text": "مشاهده باقی‌مانده 📊", "callback_data": "user:config_lookup"},
        ],
        [
            {"text": "دعوت دوستان 🎁", "callback_data": "user:referrals"},
            {"text": "پشتیبانی 💬", "callback_data": "user:support"},
        ],
        [
            {"text": "راهنما ❓", "callback_data": "user:help"},
            {"text": "پروفایل من", "callback_data": "user:profile"},
        ],
    ]
    if is_admin:
        rows.extend(
            [
                [
                    {"text": "سفارش‌های pending", "callback_data": "admin:orders:pending"},
                    {"text": "گزارش فروش", "callback_data": "admin:sales_report"},
                ],
                [
                    {"text": "گزارش مشتریان 📊", "callback_data": "admin:ca:menu"},
                    {"text": "ارسال پیام 📣", "callback_data": "admin:bc:menu"},
                ],
                [{"text": "تنظیمات سریع", "callback_data": "admin:quick_settings"}],
            ]
        )
    return {"inline_keyboard": rows}


def help_text():
    return (
        "راهنما ❓\n"
        "━━━━━━━━━━━━━━\n"
        "خرید: از «خرید سرویس» پلن را انتخاب کنید، خلاصه را ببینید و پرداخت را تایید کنید.\n"
        "رسید: بعد از کارت‌به‌کارت، عکس واضح رسید را در همان گفتگو بفرستید.\n"
        "فعال‌سازی: بعد از بررسی پرداخت، لینک کانفیگ در ربات ارسال می‌شود.\n"
        "دریافت لینک: از «سرویس‌های من» روی دریافت لینک یا مدیریت سرویس بزنید.\n"
        "تمدید: از «تمدید سرویس» کانفیگ را انتخاب کنید و رسید تمدید را بفرستید.\n"
        "تست رایگان: از «دریافت تست رایگان» می‌توانید در بازه مجاز یک کانفیگ کوتاه‌مدت بگیرید.\n"
        "دعوت دوستان: لینک دعوت خود را بفرستید؛ بعد از خرید موفق زیرمجموعه، هدیه حجمی آماده دریافت می‌شود."
    )


def send_help(client, *, chat_id, is_admin=False):
    client.send_message(help_text(), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin))
    return {"ok": True, "handled": True}


def profile_keyboard(bot_user):
    phone_label = "ویرایش شماره موبایل" if bot_user.customer_id and bot_user.customer.phone_number else "ثبت شماره موبایل"
    return {
        "inline_keyboard": [
            [{"text": phone_label, "callback_data": "user:profile_phone"}],
            [
                {"text": "اشتراک‌های من", "callback_data": "user:subs"},
                {"text": "سفارش‌های من", "callback_data": "user:orders"},
            ],
            [{"text": "منوی اصلی", "callback_data": "user:menu"}],
        ]
    }


def _profile_subscription_clients(bot_user):
    if not bot_user.customer_id:
        return []
    return list(
        VPNClient.objects.select_related("plan", "order", "inbound", "inbound__panel")
        .filter(
            order__customer=bot_user.customer,
            status__in=[
                VPNClient.Status.ACTIVE,
                VPNClient.Status.INACTIVE,
                VPNClient.Status.CREATED,
                VPNClient.Status.EXPIRED,
            ],
        )
        .order_by("-created_at")
    )


def format_profile(bot_user):
    customer = bot_user.customer
    display_name = (customer.display_name if customer else "") or bot_user.display_name or "کاربر"
    username = (customer.username if customer else "") or bot_user.username
    username_label = f"@{username.lstrip('@')}" if username else "ثبت نشده"
    phone_label = customer.phone_number if customer and customer.phone_number else "ثبت نشده"
    if customer:
        orders_count = customer.orders.exclude(metadata__customer_hidden=True).count()
        clients = _profile_subscription_clients(bot_user)
    else:
        orders_count = 0
        clients = []
    active_count = sum(1 for item in clients if item.status == VPNClient.Status.ACTIVE)
    remaining_bytes = sum(item.remaining_traffic_bytes for item in clients)

    lines = [
        "پروفایل شما",
        "━━━━━━━━━━━━━━",
        f"نام: {display_name}",
        f"نام کاربری: {username_label}",
        f"شماره موبایل: {phone_label}",
        "",
        f"سفارش‌ها: {persian_digits(orders_count)}",
        f"اشتراک فعال: {persian_digits(active_count)}",
        f"حجم باقی‌مانده ثبت‌شده: {bot_gb_from_bytes(remaining_bytes)} گیگابایت",
    ]
    if phone_label == "ثبت نشده":
        lines.extend(["", "برای پیگیری سریع‌تر سفارش‌ها می‌توانید شماره موبایل خود را ثبت کنید."])
    return "\n".join(lines)


def send_profile(client, bot_user, *, chat_id):
    if bot_user.customer_id:
        bot_user.customer.refresh_from_db()
    client.send_message(format_profile(bot_user), chat_id=chat_id, reply_markup=profile_keyboard(bot_user))
    return {"ok": True, "handled": True}


def send_main_menu(client, bot_user, *, chat_id, is_admin=False):
    bot_user.reset_state()
    profile_name = bot_user.customer.display_name if bot_user.customer_id else bot_user.display_name
    client.send_message(
        (
            f"سلام {profile_name or 'دوست عزیز'}.\n"
            "از منوی زیر انتخاب کنید."
        ),
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )
    return {"ok": True, "handled": True}
