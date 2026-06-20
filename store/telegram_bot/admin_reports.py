from datetime import timedelta
from html import escape

from django.db.models import Sum
from django.utils import timezone

from store.customer_analytics import (
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_7_DAYS,
    PERIOD_TODAY,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_REFERRER,
    analytics_enabled,
    get_customers_by_segment,
    get_loyal_customers,
    get_period_range,
    get_top_customers,
    get_top_referrers,
    top_customers_limit,
)
from store.jalali import persian_digits
from store.models import Order
from store.order_services import get_current_store

from .formatting import bot_datetime, bot_money
from .user_menu import main_menu_keyboard

CUSTOMER_ANALYTICS_CALLBACK_PREFIX = "admin:ca:"
ADMIN_REPORT_MENU_CALLBACKS = {"admin:sales_report", "admin:quick_settings"}

CUSTOMER_ANALYTICS_REPORTS = {
    "top_today": {
        "title": "۱۰ خریدار برتر امروز",
        "period": PERIOD_TODAY,
        "type": "top_buyers",
    },
    "top_7d": {
        "title": "۱۰ خریدار برتر ۷ روز اخیر",
        "period": PERIOD_LAST_7_DAYS,
        "type": "top_buyers",
    },
    "top_30d": {
        "title": "۱۰ خریدار برتر ۳۰ روز اخیر",
        "period": PERIOD_LAST_30_DAYS,
        "type": "top_buyers",
    },
    "loyal": {
        "title": "مشتریان ثابت",
        "type": "loyal",
    },
    "good": {
        "title": "مشتریان خوب",
        "type": "good",
    },
    "top_referrers": {
        "title": "معرف‌های برتر",
        "type": "top_referrers",
    },
    "no_order": {
        "title": "کاربران بدون خرید",
        "type": "segment",
        "segment": SEGMENT_NO_ORDER,
    },
    "inactive": {
        "title": "کاربران غیرفعال",
        "type": "segment",
        "segment": SEGMENT_INACTIVE,
    },
}


def is_customer_analytics_callback_data(data):
    return str(data or "").startswith(CUSTOMER_ANALYTICS_CALLBACK_PREFIX)


def is_admin_report_menu_callback_data(data):
    return str(data or "") in ADMIN_REPORT_MENU_CALLBACKS


def customer_analytics_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "برترین‌های امروز", "callback_data": "admin:ca:top_today"}],
            [{"text": "برترین‌های ۷ روز اخیر", "callback_data": "admin:ca:top_7d"}],
            [{"text": "برترین‌های ۳۰ روز اخیر", "callback_data": "admin:ca:top_30d"}],
            [
                {"text": "مشتریان ثابت", "callback_data": "admin:ca:loyal"},
                {"text": "مشتریان خوب", "callback_data": "admin:ca:good"},
            ],
            [{"text": "معرف‌های برتر", "callback_data": "admin:ca:top_referrers"}],
            [
                {"text": "کاربران بدون خرید", "callback_data": "admin:ca:no_order"},
                {"text": "کاربران غیرفعال", "callback_data": "admin:ca:inactive"},
            ],
            [{"text": "منوی اصلی", "callback_data": "user:menu"}],
        ]
    }


def customer_analytics_menu_text():
    return "گزارش مشتریان 📊\nیکی از گزارش‌ها را انتخاب کنید."


def customer_analytics_customer_name(customer):
    return escape(customer.display_name or customer.username or customer.phone_number or f"Customer {str(customer.public_id)[:8]}")


def customer_analytics_contact(customer):
    value = customer.phone_number or customer.username or ""
    return f" - <code>{escape(value)}</code>" if value else ""


def customer_analytics_empty_message(title):
    return f"<b>{escape(title)}</b>\n\nداده‌ای برای این گزارش پیدا نشد."


def format_customer_analytics_report(report_key, *, config=None):
    report = CUSTOMER_ANALYTICS_REPORTS.get(report_key)
    if not report:
        return "این گزارش پیدا نشد."
    if not analytics_enabled(config.store if config else None):
        return "گزارش مشتریان در تنظیمات فروشگاه غیرفعال است."

    limit = top_customers_limit(config.store if config else None)
    title = report["title"]
    date_from = date_to = None
    if report.get("period"):
        date_from, date_to = get_period_range(report["period"])

    report_type = report["type"]
    if report_type == "top_buyers":
        customers = list(get_top_customers(metric="amount", date_from=date_from, date_to=date_to, limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} خرید - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_orders_count", 0) or 0),
                    bot_money(getattr(customer, "analytics_total_paid_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "loyal":
        customers = list(get_loyal_customers(limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} خرید موفق - {} تمدید".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_orders_count", 0) or 0),
                    persian_digits(getattr(customer, "analytics_renewal_orders_count", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "good":
        customers = list(get_customers_by_segment(SEGMENT_GOOD, limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    bot_money(getattr(customer, "analytics_total_paid_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    if report_type == "top_referrers":
        customers = list(get_top_referrers(limit=limit))
        if not customers:
            return customer_analytics_empty_message(title)
        lines = [f"<b>{escape(title)}</b>"]
        for index, customer in enumerate(customers, start=1):
            lines.append(
                "{}. {}{} - {} زیرمجموعه موفق - {}".format(
                    persian_digits(index),
                    customer_analytics_customer_name(customer),
                    customer_analytics_contact(customer),
                    persian_digits(getattr(customer, "analytics_successful_referrals_count", 0) or 0),
                    bot_money(getattr(customer, "analytics_successful_referral_amount", 0) or 0),
                )
            )
        return "\n".join(lines)

    customers = list(get_customers_by_segment(report["segment"], limit=limit))
    if not customers:
        return customer_analytics_empty_message(title)
    lines = [f"<b>{escape(title)}</b>"]
    for index, customer in enumerate(customers, start=1):
        if report["segment"] == SEGMENT_INACTIVE:
            last_purchase = bot_datetime(getattr(customer, "analytics_last_purchase_at", None))
            suffix = f" - آخرین خرید: {last_purchase}"
        else:
            suffix = f" - عضویت: {bot_datetime(customer.created_at)}"
        lines.append(f"{persian_digits(index)}. {customer_analytics_customer_name(customer)}{customer_analytics_contact(customer)}{suffix}")
    return "\n".join(lines)


def admin_sales_report_text(config):
    now = timezone.now()
    start = now - timedelta(days=1)
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=now)
    if config.store_id:
        orders = orders.filter(store=config.store)
    completed = orders.filter(status=Order.Status.COMPLETED)
    pending = orders.filter(status=Order.Status.PENDING_VERIFICATION)
    rejected = orders.filter(status=Order.Status.REJECTED)
    revenue = completed.aggregate(total=Sum("amount"))["total"] or 0
    lines = [
        "گزارش فروش",
        "━━━━━━━━━━━━━━",
        f"بازه: ۲۴ ساعت اخیر تا {bot_datetime(now)}",
        f"سفارش فعال شده: {persian_digits(completed.count())}",
        f"در انتظار بررسی: {persian_digits(pending.count())}",
        f"رد شده: {persian_digits(rejected.count())}",
        f"فروش تاییدشده: {bot_money(revenue)}",
    ]
    return "\n".join(lines)


def admin_quick_settings_text(config, *, get_current_store_func=get_current_store):
    store = config.store or get_current_store_func()
    force_join = "فعال" if config.force_telegram_channel_join else "غیرفعال"
    analytics = "فعال" if analytics_enabled(store) else "غیرفعال"
    broadcast = "فعال" if getattr(store, "broadcast_enabled", True) else "غیرفعال"
    return (
        "تنظیمات سریع\n"
        "━━━━━━━━━━━━━━\n"
        f"Force Join: {force_join}\n"
        f"گزارش مشتریان: {analytics}\n"
        f"ارسال پیام: {broadcast}\n"
        f"فروشگاه: {store.name if store else '-'}"
    )


def handle_admin_report_menu_callback(client, config, data, *, chat_id, get_current_store_func=get_current_store):
    if data == "admin:sales_report":
        client.send_message(admin_sales_report_text(config), chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=True))
        return {"ok": True, "handled": True}
    if data == "admin:quick_settings":
        client.send_message(
            admin_quick_settings_text(config, get_current_store_func=get_current_store_func),
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=True),
        )
        return {"ok": True, "handled": True}
    return {"ok": True, "ignored": True}


def handle_customer_analytics_callback_update(
    config,
    callback_query,
    *,
    chat_id,
    client_cls,
    get_callback_id_func,
    get_callback_data_func,
):
    callback_id = get_callback_id_func(callback_query)
    data = get_callback_data_func(callback_query)
    client = client_cls(config)
    client.answer_callback(callback_id, "دریافت شد")

    report_key = data[len(CUSTOMER_ANALYTICS_CALLBACK_PREFIX):]
    if report_key == "menu":
        client.send_message(
            customer_analytics_menu_text(),
            chat_id=chat_id,
            reply_markup=customer_analytics_keyboard(),
        )
        return {"ok": True, "success": True, "customer_analytics_menu": True}

    text = format_customer_analytics_report(report_key, config=config)
    client.send_message(text, chat_id=chat_id, reply_markup=customer_analytics_keyboard())
    return {"ok": True, "success": True, "customer_analytics_report": report_key}
