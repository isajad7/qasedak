from html import escape

from django.utils import timezone

from store.jalali import persian_digits
from store.models import BotConfiguration, BotEventLog, BotPendingAction, BotUser, Order

from .formatting import bot_datetime, bot_money, money


BOT_ORDER_STATUS_LABELS = {
    Order.Status.PENDING_PAYMENT: "در انتظار پرداخت",
    Order.Status.PENDING_VERIFICATION: "در انتظار بررسی",
    Order.Status.CONFIRMED: "تایید شده",
    Order.Status.COMPLETED: "فعال شده",
    Order.Status.REJECTED: "رد شده",
    Order.Status.CANCELLED: "لغو شده",
}

BOT_VERIFICATION_STATUS_LABELS = {
    Order.VerificationStatus.PENDING: "در انتظار بررسی",
    Order.VerificationStatus.VERIFIED: "تایید شده",
    Order.VerificationStatus.REJECTED: "رد شده",
}


def bot_order_status(order):
    return BOT_ORDER_STATUS_LABELS.get(order.status, order.get_status_display())


def bot_verification_status(order):
    return BOT_VERIFICATION_STATUS_LABELS.get(order.verification_status, order.get_verification_status_display())


def admin_order_type_label(order):
    metadata = order.metadata or {}
    if metadata.get("renewal") or metadata.get("renewal_client_pk"):
        return "تمدید"
    return "خرید جدید"


def admin_customer_label(order):
    if order.customer_id:
        return (
            order.customer.display_name
            or order.customer.username
            or order.customer.phone_number
            or f"Customer {str(order.customer.public_id)[:8]}"
        )
    return order.sender_card_name or "-"


def admin_customer_phone(order):
    if order.customer_id and order.customer.phone_number:
        return order.customer.phone_number
    return "-"


def admin_customer_telegram_id(order):
    metadata = order.metadata or {}
    bot_metadata = metadata.get("bot") or {}
    provider_user_id = bot_metadata.get("provider_user_id") or bot_metadata.get("chat_id")
    if provider_user_id:
        return str(provider_user_id)
    if not order.customer_id:
        return "-"
    bot_user = (
        BotUser.objects.filter(customer_id=order.customer_id, bot_config__provider=BotConfiguration.Provider.TELEGRAM)
        .order_by("-last_seen_at", "-updated_at")
        .first()
    )
    if not bot_user:
        return "-"
    return bot_user.provider_user_id or bot_user.chat_id or "-"


def admin_receipt_label(order):
    metadata = order.metadata or {}
    if order.payment_receipt_image:
        return "عکس رسید پیوست شده است."
    if (metadata.get("receipt_text") or "").strip():
        return "متن رسید ثبت شده است."
    if metadata.get("receipt"):
        return "رسید در پیام ربات دریافت شده است."
    if order.payment_submitted_at:
        return "اطلاعات پرداخت ثبت شده است."
    return "ثبت نشده"


def order_admin_keyboard(order):
    tracking_code = order.order_tracking_code
    return {
        "inline_keyboard": [
            [
                {"text": "تایید سفارش ✅", "callback_data": f"approve:{tracking_code}"},
                {"text": "رد سفارش ❌", "callback_data": f"reject:{tracking_code}"},
            ],
            [{"text": "مشاهده جزئیات 📦", "callback_data": f"order:detail:{tracking_code}"}],
        ]
    }


def format_order_message(order, *, title="سفارش جدید VPN"):
    metadata = order.metadata or {}
    receipt_analysis = metadata.get("receipt_analysis") or {}
    receipt_text = (metadata.get("receipt_text") or "").strip()
    duplicate_warning = metadata.get("duplicate_warning") or {}
    source = metadata.get("source") or "-"
    created_at = timezone.localtime(order.created_at).strftime("%Y-%m-%d %H:%M") if order.created_at else "-"
    paid_at = (
        f"{order.payment_date or '-'} {order.payment_time.strftime('%H:%M') if order.payment_time else ''}".strip()
        if order.payment_date or order.payment_time
        else "-"
    )
    lines = [
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━",
        f"<b>شماره سفارش:</b> <code>#{order.pk}</code>",
        f"<b>کد پیگیری:</b> <code>{order.order_tracking_code}</code>",
        f"<b>نوع:</b> {admin_order_type_label(order)}",
        f"<b>مشتری:</b> {escape(admin_customer_label(order))}",
        f"<b>شماره موبایل:</b> <code>{escape(admin_customer_phone(order))}</code>",
        f"<b>تلگرام:</b> <code>{escape(admin_customer_telegram_id(order))}</code>",
        f"<b>پلن:</b> {escape(order.plan.name) if order.plan_id else '-'}",
        f"<b>اپراتور:</b> {escape(order.operator.name) if order.operator_id else '-'}",
        f"<b>حجم/مدت:</b> {order.plan.volume_gb if order.plan_id else '-'} GB / {order.plan.duration_days if order.plan_id else '-'} روز",
        f"<b>تعداد:</b> {getattr(order, 'quantity', 1) or 1}",
        f"<b>مبلغ نهایی:</b> <code>{money(order.amount, order.currency)}</code>",
        "",
        f"<b>وضعیت:</b> {bot_order_status(order)}",
        f"<b>تایید:</b> {bot_verification_status(order)}",
        f"<b>رسید:</b> {admin_receipt_label(order)}",
        f"<b>زمان ثبت:</b> <code>{created_at}</code>",
        f"<b>زمان پرداخت:</b> <code>{paid_at}</code>",
        "",
        f"<b>پرداخت‌کننده:</b> {escape(order.sender_card_name or '-')}",
        f"<b>۴ رقم کارت:</b> <code>{order.sender_card_last4 or '-'}</code>",
        f"<b>کانفیگ:</b> <code>{order.username}</code>",
        f"<b>منبع:</b> <code>{escape(str(source))}</code>",
    ]
    if order.bank_tracking_code:
        lines.append(f"<b>کد پیگیری بانکی:</b> <code>{escape(order.bank_tracking_code)}</code>")
    if duplicate_warning.get("detected"):
        lines.extend(
            [
                "",
                "<b>هشدار درخواست تکراری</b>",
                f"<b>تعداد تلاش مشابه:</b> <code>{duplicate_warning.get('attempt_count') or 1}</code>",
                f"<b>بازه بررسی:</b> <code>{int((duplicate_warning.get('window_seconds') or 600) / 60)} دقیقه</code>",
                "سفارش جدید ساخته نشد و این درخواست به همین سفارش وصل شد. قبل از تایید، رسید و اطلاعات پرداخت را دقیق‌تر بررسی کن.",
            ]
        )
    if receipt_analysis:
        status = receipt_analysis.get("status") or "-"
        lines.extend(["", f"<b>بررسی رسید:</b> {escape(str(status))}"])
        expected = receipt_analysis.get("expected_amount_irr")
        detected = receipt_analysis.get("matched_amount_irr")
        if expected is not None:
            lines.append(f"<b>مبلغ مورد انتظار:</b> <code>{money(expected, 'IRR')}</code>")
        if detected is not None:
            lines.append(f"<b>مبلغ پیدا شده:</b> <code>{money(detected, 'IRR')}</code>")
        if receipt_analysis.get("requires_admin_review"):
            lines.append("<b>هشدار:</b> رسید نیاز به بررسی دستی دارد.")
        if receipt_analysis.get("warning"):
            lines.append(f"<b>هشدار رسید:</b> {escape(str(receipt_analysis['warning']))}")
    if receipt_text:
        excerpt = receipt_text[:350] + ("..." if len(receipt_text) > 350 else "")
        lines.extend(["", f"<b>متن رسید:</b>\n{escape(excerpt)}"])
    if order.rejection_reason:
        lines.extend(["", f"<b>دلیل رد:</b> {escape(order.rejection_reason)}"])
    return "\n".join(lines)


def admin_pending_orders_keyboard(orders):
    rows = []
    for order in orders:
        tracking = order.order_tracking_code
        label = f"{order.plan.name if order.plan_id else '-'} | {tracking}"[:60]
        rows.append([{"text": label, "callback_data": f"order:approve:{tracking}"}])
        rows.append(
            [
                {"text": "تایید", "callback_data": f"approve:{tracking}"},
                {"text": "رد", "callback_data": f"reject:{tracking}"},
            ]
        )
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def admin_pending_orders_text(config):
    orders = Order.objects.select_related("plan", "operator", "customer").filter(
        status=Order.Status.PENDING_VERIFICATION,
        verification_status=Order.VerificationStatus.PENDING,
    )
    if config.store_id:
        orders = orders.filter(store=config.store)
    orders = list(orders.order_by("created_at")[:10])
    if not orders:
        return "سفارش pending برای بررسی وجود ندارد.", admin_pending_orders_keyboard([])

    lines = ["سفارش‌های pending", "━━━━━━━━━━━━━━"]
    for order in orders:
        customer_label = order.customer.display_name if order.customer_id else order.sender_card_name or "-"
        lines.extend(
            [
                f"شماره سفارش: {order.order_tracking_code}",
                f"پلن: {order.plan.name if order.plan_id else '-'}",
                *([f"اپراتور: {order.operator.name}"] if order.operator_id else []),
                f"مشتری: {customer_label}",
                f"مبلغ: {bot_money(order.amount, order.currency)}",
                f"ثبت: {bot_datetime(order.created_at)}",
                "",
            ]
        )
    return "\n".join(lines).strip(), admin_pending_orders_keyboard(orders)


def handle_admin_orders_menu_callback(client, config, data, *, chat_id):
    if data != "admin:orders:pending":
        return {"ok": True, "ignored": True}
    text, reply_markup = admin_pending_orders_text(config)
    client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
    return {"ok": True, "handled": True}


def handle_admin_order_callback_update(
    config,
    callback_query,
    *,
    chat_id,
    client_cls,
    get_callback_id_func,
    get_callback_data_func,
    normalize_id_func,
    get_sender_object_func,
    parse_callback_data_func,
    log_callback_func,
    format_order_message_func,
    order_admin_keyboard_func,
    sync_admin_order_messages_func,
    send_final_order_status_func,
):
    callback_id = get_callback_id_func(callback_query)
    data = get_callback_data_func(callback_query)
    client = client_cls(config)
    client.answer_callback(callback_id, "دریافت شد")

    action, tracking_code = parse_callback_data_func(data)
    admin_user_id = normalize_id_func(get_sender_object_func(callback_query))
    log_callback_func(
        config,
        status=BotEventLog.Status.RECEIVED,
        message=(
            f"callback_data={data!r}; action={action or '-'}; "
            f"order_id={tracking_code or '-'}; admin_user_id={admin_user_id or '-'}"
        ),
        raw_payload={
            "callback_data": data,
            "callback_id": callback_id,
            "admin_user_id": admin_user_id,
            "chat_id": chat_id,
            "callback_query": callback_query,
        },
    )

    if not action or not tracking_code:
        log_callback_func(
            config,
            status=BotEventLog.Status.FAILED,
            message=f"Malformed callback data: {data!r}",
            raw_payload={"callback_data": data, "callback_query": callback_query},
        )
        client.send_message(f"Malformed callback data: <code>{data}</code>", chat_id=chat_id)
        return {"ok": True, "ignored": True, "error": "Malformed callback data."}

    order = Order.objects.select_related("plan", "store", "inbound", "inbound__panel").filter(
        order_tracking_code=tracking_code
    ).first()
    if not order:
        log_callback_func(
            config,
            status=BotEventLog.Status.FAILED,
            message=f"Order not found for callback_data={data!r}; order_id={tracking_code}",
            raw_payload={"callback_data": data, "order_id": tracking_code},
        )
        client.send_message("Order was not found.", chat_id=chat_id)
        return {"ok": True, "success": False, "error": "Order not found."}

    log_callback_func(
        config,
        status=BotEventLog.Status.RECEIVED,
        order=order,
        message=f"Processing {action} for order {order.order_tracking_code}; admin_user_id={admin_user_id or '-'}",
        raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
    )

    if action == "detail":
        client.send_message(
            format_order_message_func(order, title="جزئیات سفارش"),
            chat_id=chat_id,
            reply_markup=order_admin_keyboard_func(order),
        )
        log_callback_func(
            config,
            status=BotEventLog.Status.SUCCESS,
            order=order,
            message=f"Order detail sent for {order.order_tracking_code}; admin_user_id={admin_user_id or '-'}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        return {"ok": True, "success": True, "detail": True}

    if action == "approve":
        from store.order_actions import activate_order

        log_callback_func(
            config,
            status=BotEventLog.Status.RECEIVED,
            order=order,
            message=f"Calling activate_order for order {order.order_tracking_code}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        result = activate_order(order, user=None, notify=True)
        order.refresh_from_db()
        updated_count = sync_admin_order_messages_func(
            order,
            title="Order approved" if result.success else "Order approval failed",
            event_type=BotEventLog.EventType.ORDER_APPROVED if result.success else BotEventLog.EventType.ERROR,
            prefix_message=result.message,
            respect_notify=False,
            configs=[config],
        )
        log_callback_func(
            config,
            status=BotEventLog.Status.SUCCESS if result.success else BotEventLog.Status.FAILED,
            order=order,
            message=f"activate_order finished for {order.order_tracking_code}: success={result.success}; message={result.message}",
            raw_payload={
                "callback_data": data,
                "order_id": tracking_code,
                "action": action,
                "success": result.success,
                "message": result.message,
                "status": order.status,
                "verification_status": order.verification_status,
            },
        )
        if not updated_count:
            send_final_order_status_func(
                client,
                order,
                title="Order approved" if result.success else "Order approval failed",
                chat_id=chat_id,
                callback_query=callback_query,
                prefix_message=result.message,
            )
        return {"ok": True, "success": result.success, "message": result.message}

    if action == "reject":
        order.refresh_from_db()
        if order.status == Order.Status.COMPLETED:
            log_callback_func(
                config,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Reject denied because order {order.order_tracking_code} is already completed.",
                raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
            )
            send_final_order_status_func(
                client,
                order,
                title="Order already completed",
                chat_id=chat_id,
                callback_query=callback_query,
                prefix_message="Completed orders cannot be rejected from the bot.",
            )
            return {"ok": True, "success": False, "message": "Completed order cannot be rejected."}

        BotPendingAction.objects.filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        ).update(status=BotPendingAction.Status.CANCELLED, resolved_at=timezone.now(), updated_at=timezone.now())
        BotPendingAction.objects.create(
            bot_config=config,
            order=order,
            admin_user_id=admin_user_id,
            action=BotPendingAction.Action.REJECT_ORDER,
        )
        log_callback_func(
            config,
            status=BotEventLog.Status.SUCCESS,
            order=order,
            message=f"Reject reason requested for order {order.order_tracking_code}; admin_user_id={admin_user_id}",
            raw_payload={"callback_data": data, "order_id": tracking_code, "action": action},
        )
        client.send_message(
            f"Please send the rejection reason for order <code>{order.order_tracking_code}</code>.",
            chat_id=chat_id,
        )
        return {"ok": True, "waiting_for_reason": True}

    return {"ok": True, "ignored": True}
