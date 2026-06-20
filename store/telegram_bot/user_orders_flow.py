from store.jalali import persian_digits
from store.models import Order, VPNClient

from .formatting import bot_datetime, bot_gb_from_bytes, bot_money, bot_volume_label
from .user_menu import main_menu_keyboard


def user_order_summary(order, *, bot_order_status_func=None):
    is_renewal = bool((order.metadata or {}).get("renewal"))
    lines = [
        "✅ درخواست تمدید شما ثبت شد." if is_renewal else "✅ سفارش شما ثبت شد.",
        "",
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name}",
        f"تعداد کانفیگ: {persian_digits(order.quantity or 1)}",
        f"مبلغ: {bot_money(order.amount, order.currency)}",
    ]
    if order.operator_id:
        lines.append(f"اپراتور: {order.operator.name}")
    lines.append(
        (
            "بعد از بررسی پرداخت، همین سرویس تمدید می‌شود."
            if is_renewal
            else "بعد از بررسی پرداخت، سرویس فعال می‌شود."
        )
    )
    return "\n".join(lines)


def visible_bot_orders(bot_user, *, limit=10):
    if not bot_user.customer_id:
        return []
    orders = (
        Order.objects.select_related("plan", "operator", "store")
        .prefetch_related("vpn_clients")
        .filter(customer=bot_user.customer)
        .order_by("-created_at")
    )
    return [
        order
        for order in orders[: max(limit * 2, limit)]
        if not (order.metadata or {}).get("customer_hidden")
    ][:limit]


def user_orders_keyboard(orders, *, bot_order_status_func):
    rows = []
    for order in orders:
        plan_name = order.plan.name if order.plan_id else "بدون پلن"
        label = f"{plan_name} | {bot_order_status_func(order)}"
        rows.append([{"text": label[:60], "callback_data": f"user:order:{order.order_tracking_code}"}])
    rows.append([{"text": "بازگشت به منو", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def order_management_keyboard(
    order,
    bot_user=None,
    *,
    user_client_delete_button_func,
):
    rows = []
    clients = list(order.vpn_clients.filter(deleted_at__isnull=True).exclude(status=VPNClient.Status.DELETED))
    for index, client in enumerate(clients, start=1):
        suffix = persian_digits(index) if len(clients) > 1 else ""
        rows.append(
            [
                {"text": f"حجم {suffix}".strip(), "callback_data": f"user:client_usage:{client.public_id}"},
                {"text": f"بروزرسانی کانفیگ {suffix}".strip(), "callback_data": f"user:client_refresh:{client.public_id}"},
            ]
        )
        rows.append(
            [
                {"text": f"دریافت لینک کانفیگ {suffix}".strip(), "callback_data": f"user:client_config:{client.public_id}"},
                {"text": f"تمدید {suffix}".strip(), "callback_data": f"user:client_renew:{client.public_id}"},
            ]
        )
        if bot_user:
            delete_button = user_client_delete_button_func(bot_user, client, suffix=suffix)
            if delete_button:
                rows.append([delete_button])

    if order.status in {Order.Status.PENDING_PAYMENT, Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED}:
        rows.append([{"text": "لغو این سفارش", "callback_data": f"user:order_cancel:{order.order_tracking_code}"}])

    rows.append([{"text": "بازگشت به سفارش‌ها", "callback_data": "user:orders"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_user_orders_list(bot_user, *, bot_order_status_func):
    orders = visible_bot_orders(bot_user)
    if not orders:
        return "هنوز سفارشی برای این حساب ثبت نشده است.", user_orders_keyboard(
            [],
            bot_order_status_func=bot_order_status_func,
        )

    lines = [
        "سفارش‌های من",
        "━━━━━━━━━━━━━━",
        "برای دیدن جزئیات یا مدیریت، یکی از سفارش‌ها را انتخاب کنید.",
        "",
    ]
    for order in orders:
        created_at = bot_datetime(order.created_at)
        lines.extend(
            [
                f"شماره سفارش: {order.order_tracking_code}",
                f"پلن: {order.plan.name if order.plan_id else 'بدون پلن'}",
                *([f"اپراتور: {order.operator.name}"] if order.operator_id else []),
                f"وضعیت: {bot_order_status_func(order)}",
                f"تعداد: {persian_digits(order.quantity or 1)}",
                f"مبلغ: {bot_money(order.amount, order.currency)}",
                f"ثبت: {created_at}",
                *([f"دلیل رد: {order.rejection_reason}"] if order.rejection_reason else []),
                "",
            ]
        )
    return "\n".join(lines).strip(), user_orders_keyboard(orders, bot_order_status_func=bot_order_status_func)


def format_user_order_detail(
    order,
    *,
    bot_verification_status_func,
    bot_order_status_func,
    bot_client_label_func,
    bot_client_status_func,
    sync_vpn_client_stats_func,
):
    created_at = bot_datetime(order.created_at)
    paid_at = (
        f"{persian_digits(order.payment_date.strftime('%Y/%m/%d'))}، {persian_digits(order.payment_time.strftime('%H:%M'))}"
        if order.payment_date and order.payment_time
        else "ثبت نشده"
    )
    lines = [
        "جزئیات سفارش",
        "━━━━━━━━━━━━━━",
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name if order.plan_id else 'بدون پلن'}",
        f"حجم/مدت: {bot_volume_label(order.plan.volume_gb) if order.plan_id else '-'} / {persian_digits(order.plan.duration_days if order.plan_id else '-')} روز",
        f"تعداد: {persian_digits(order.quantity or 1)}",
        f"مبلغ: {bot_money(order.amount, order.currency)}",
        f"وضعیت سفارش: {bot_order_status_func(order)}",
        f"وضعیت پرداخت: {bot_verification_status_func(order)}",
        f"زمان ثبت: {created_at}",
        f"زمان پرداخت: {paid_at}",
    ]
    if order.operator_id:
        lines.insert(5, f"اپراتور: {order.operator.name}")
    if order.bank_tracking_code:
        lines.append(f"کد پیگیری بانکی: {order.bank_tracking_code}")
    if order.rejection_reason:
        lines.extend(["", f"دلیل رد شدن: {order.rejection_reason}"])

    clients = list(order.vpn_clients.filter(deleted_at__isnull=True).exclude(status=VPNClient.Status.DELETED))
    if clients:
        lines.extend(["", "کانفیگ‌ها"])
        for index, client in enumerate(clients, start=1):
            stats = sync_vpn_client_stats_func(client)
            remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", client.remaining_traffic_bytes))
            total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
            expiry_at = stats.get("expiry_at") or client.expires_at
            lines.extend(
                [
                    f"{persian_digits(index)}. {bot_client_label_func(client)}",
                    f"وضعیت: {bot_client_status_func(client)}",
                    f"حجم باقی‌مانده: {remaining_gb} از {total_gb} گیگابایت",
                    f"انقضا: {bot_datetime(expiry_at)}",
                ]
            )
    else:
        lines.extend(["", "بعد از تایید پرداخت، کانفیگ همین‌جا نمایش داده می‌شود."])

    return "\n".join(lines)


def get_bot_order(bot_user, tracking_code):
    if not bot_user.customer_id:
        return None
    order = (
        Order.objects.select_related("plan", "operator", "store")
        .prefetch_related("vpn_clients", "vpn_clients__inbound", "vpn_clients__inbound__panel")
        .filter(customer=bot_user.customer, order_tracking_code=tracking_code)
        .first()
    )
    if order and (order.metadata or {}).get("customer_hidden"):
        return None
    return order


def show_user_orders(client, bot_user, *, chat_id, bot_order_status_func):
    text, reply_markup = format_user_orders_list(bot_user, bot_order_status_func=bot_order_status_func)
    client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
    return {"ok": True, "handled": True}


def handle_user_order_callback(
    client,
    bot_user,
    data,
    *,
    chat_id,
    bot_order_status_func,
    bot_verification_status_func,
    bot_client_label_func,
    bot_client_status_func,
    sync_vpn_client_stats_func,
    user_client_delete_button_func,
):
    if data == "user:orders":
        return show_user_orders(
            client,
            bot_user,
            chat_id=chat_id,
            bot_order_status_func=bot_order_status_func,
        )
    if data.startswith("user:order:"):
        tracking_code = data.rsplit(":", 1)[-1]
        order = get_bot_order(bot_user, tracking_code)
        if not order:
            client.send_message("این سفارش پیدا نشد یا دیگر در دسترس نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        client.send_message(
            format_user_order_detail(
                order,
                bot_order_status_func=bot_order_status_func,
                bot_verification_status_func=bot_verification_status_func,
                bot_client_label_func=bot_client_label_func,
                bot_client_status_func=bot_client_status_func,
                sync_vpn_client_stats_func=sync_vpn_client_stats_func,
            ),
            chat_id=chat_id,
            reply_markup=order_management_keyboard(
                order,
                bot_user,
                user_client_delete_button_func=user_client_delete_button_func,
            ),
        )
        return {"ok": True, "handled": True}
    if data.startswith("user:order_cancel:"):
        tracking_code = data.rsplit(":", 1)[-1]
        order = get_bot_order(bot_user, tracking_code)
        if not order:
            client.send_message("این سفارش پیدا نشد یا دیگر در دسترس نیست.", chat_id=chat_id, reply_markup=main_menu_keyboard())
            return {"ok": True, "success": False}
        if order.status == Order.Status.COMPLETED:
            client.send_message(
                "سفارش تکمیل‌شده قابل لغو نیست. برای تغییر، از پشتیبانی کمک بگیرید.",
                chat_id=chat_id,
                reply_markup=order_management_keyboard(
                    order,
                    bot_user,
                    user_client_delete_button_func=user_client_delete_button_func,
                ),
            )
            return {"ok": True, "success": False}
        from store.order_actions import cancel_order

        result = cancel_order(order)
        if result.success:
            client.send_message("سفارش از فهرست شما حذف شد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        else:
            client.send_message(
                result.message,
                chat_id=chat_id,
                reply_markup=order_management_keyboard(
                    order,
                    bot_user,
                    user_client_delete_button_func=user_client_delete_button_func,
                ),
            )
        return {"ok": True, "success": result.success}
    return {"ok": True, "ignored": True}
