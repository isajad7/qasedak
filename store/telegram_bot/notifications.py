import logging
from datetime import timedelta
from html import escape

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone

from store.models import BotAdminOrderMessage, BotConfiguration, BotEventLog, Order

from .formatting import money


logger = logging.getLogger(__name__)


def _default_delivery_deps():
    from .client import BotClient, BotDeliveryError
    from .order_finalizers import log_event

    return BotClient, BotDeliveryError, log_event


def active_bot_configs(order=None, *, store=None, reports=False):
    configs = BotConfiguration.objects.filter(is_active=True).exclude(bot_token="").exclude(admin_user_id="")
    store = store or (order.store if order and order.store_id else None)
    if order and order.store_id:
        configs = configs.filter(Q(store=order.store) | Q(store__isnull=True))
    elif store:
        configs = configs.filter(Q(store=store) | Q(store__isnull=True))
    if reports:
        configs = configs.filter(send_sales_reports=True)
    return configs


def is_sales_report_due(config, *, now=None):
    if not config.send_sales_reports:
        return False
    now = now or timezone.now()
    if not config.last_report_sent_at:
        return True
    return config.last_report_sent_at + timedelta(hours=config.report_interval_hours) <= now


def fit_photo_caption(text, *, limit=1000):
    if len(text) <= limit:
        return text
    lines = []
    current_length = 0
    for line in text.splitlines():
        next_length = current_length + len(line) + 1
        if next_length > limit - 45:
            break
        lines.append(line)
        current_length = next_length
    lines.append("جزئیات کامل در پیام بعدی ارسال شد.")
    return "\n".join(lines)


def _first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_sent_message_id(payload):
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result") or payload
    if isinstance(result, list):
        result = result[0] if result else {}
    if not isinstance(result, dict):
        return ""
    return str(_first_present(result, "message_id", "messageId", "id") or "")


def remember_admin_order_message(config, order, *, admin_user_id, chat_id, message_id, message_kind, metadata=None):
    if not message_id:
        return None
    admin_user_id = str(admin_user_id or chat_id)
    chat_id = str(chat_id or admin_user_id)
    message_id = str(message_id)
    existing = BotAdminOrderMessage.objects.filter(
        bot_config=config,
        order=order,
        admin_user_id=admin_user_id,
        chat_id=chat_id,
        message_id=message_id,
    ).first()
    if existing:
        return existing
    return BotAdminOrderMessage.objects.create(
        bot_config=config,
        order=order,
        admin_user_id=admin_user_id,
        chat_id=chat_id,
        message_id=message_id,
        message_kind=message_kind,
        metadata=metadata or {},
    )


def edit_admin_order_message(client, message_ref, text, *, reply_markup=None, delivery_error_cls=None):
    if delivery_error_cls is None:
        _client_cls, delivery_error_cls, _log_event_func = _default_delivery_deps()
    if message_ref.message_kind == BotAdminOrderMessage.MessageKind.PHOTO:
        try:
            client.edit_caption(
                chat_id=message_ref.chat_id,
                message_id=message_ref.message_id,
                caption=fit_photo_caption(text),
                reply_markup=reply_markup,
            )
            return True
        except delivery_error_cls:
            pass

    client.edit_message(
        chat_id=message_ref.chat_id,
        message_id=message_ref.message_id,
        text=text,
        reply_markup=reply_markup,
    )
    return True


def send_to_config(
    config,
    *,
    text,
    event_type,
    order=None,
    reply_markup=None,
    chat_id=None,
    client_cls=None,
    delivery_error_cls=None,
    log_event_func=None,
):
    if client_cls is None or delivery_error_cls is None or log_event_func is None:
        default_client_cls, default_delivery_error_cls, default_log_event_func = _default_delivery_deps()
        client_cls = client_cls or default_client_cls
        delivery_error_cls = delivery_error_cls or default_delivery_error_cls
        log_event_func = log_event_func or default_log_event_func

    client = client_cls(config)
    target_chat_ids = [str(chat_id)] if chat_id else config.get_admin_user_ids()
    sent = 0
    last_error = ""

    for target_chat_id in target_chat_ids:
        try:
            client.send_message(text, reply_markup=reply_markup, chat_id=target_chat_id)
        except delivery_error_cls as exc:
            last_error = str(exc)
            log_event_func(
                config,
                event_type=event_type,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=str(exc),
                raw_payload={"chat_id": target_chat_id},
            )
            logger.warning("Bot notification failed for %s chat_id=%s: %s", config, target_chat_id, exc)
            continue

        sent += 1
        log_event_func(
            config,
            event_type=event_type,
            status=BotEventLog.Status.SENT,
            order=order,
            message=text,
            raw_payload={"chat_id": target_chat_id},
        )

    if sent and config.last_error:
        config.last_error = ""
        config.save(update_fields=["last_error", "updated_at"])
    elif last_error:
        config.last_error = last_error
        config.save(update_fields=["last_error", "updated_at"])
    return sent > 0


def sync_admin_order_messages(
    order,
    *,
    title,
    event_type,
    prefix_message="",
    respect_notify=True,
    configs=None,
    format_order_message_func=None,
    active_bot_configs_func=None,
    client_cls=None,
    delivery_error_cls=None,
    log_event_func=None,
    send_to_config_func=None,
    edit_admin_order_message_func=None,
    empty_inline_keyboard_func=None,
):
    if format_order_message_func is None:
        from .admin_orders import format_order_message as format_order_message_func
    if active_bot_configs_func is None:
        active_bot_configs_func = active_bot_configs
    if client_cls is None or delivery_error_cls is None or log_event_func is None:
        default_client_cls, default_delivery_error_cls, default_log_event_func = _default_delivery_deps()
        client_cls = client_cls or default_client_cls
        delivery_error_cls = delivery_error_cls or default_delivery_error_cls
        log_event_func = log_event_func or default_log_event_func
    if send_to_config_func is None:
        send_to_config_func = send_to_config
    if edit_admin_order_message_func is None:
        edit_admin_order_message_func = edit_admin_order_message
    if empty_inline_keyboard_func is None:
        from .keyboards import empty_inline_keyboard as empty_inline_keyboard_func

    text = format_order_message_func(order, title=title)
    if prefix_message:
        text = f"{prefix_message}\n\n{text}"

    sent_or_edited = 0
    for config in (configs or active_bot_configs_func(order)):
        if (
            respect_notify
            and event_type in {BotEventLog.EventType.ORDER_APPROVED, BotEventLog.EventType.ORDER_REJECTED}
            and not config.notify_order_updates
        ):
            continue

        client = client_cls(config)
        refs = list(
            BotAdminOrderMessage.objects.filter(
                bot_config=config,
                order=order,
            ).order_by("created_at", "pk")
        )
        refs_by_admin = {}
        for ref in refs:
            refs_by_admin.setdefault(ref.admin_user_id, []).append(ref)

        for admin_user_id in config.get_admin_user_ids():
            admin_refs = refs_by_admin.get(admin_user_id) or []
            if not admin_refs:
                if send_to_config_func(
                    config,
                    text=text,
                    event_type=event_type,
                    order=order,
                    chat_id=admin_user_id,
                ):
                    sent_or_edited += 1
                continue

            for ref in admin_refs:
                try:
                    edit_admin_order_message_func(client, ref, text, reply_markup=empty_inline_keyboard_func())
                except delivery_error_cls as exc:
                    log_event_func(
                        config,
                        event_type=event_type,
                        status=BotEventLog.Status.FAILED,
                        order=order,
                        message=f"Could not edit admin order message: {exc}",
                        raw_payload={
                            "admin_user_id": ref.admin_user_id,
                            "chat_id": ref.chat_id,
                            "message_id": ref.message_id,
                            "message_kind": ref.message_kind,
                        },
                    )
                    continue
                sent_or_edited += 1
                log_event_func(
                    config,
                    event_type=event_type,
                    status=BotEventLog.Status.SENT,
                    order=order,
                    message="Admin order message updated.",
                    raw_payload={
                        "admin_user_id": ref.admin_user_id,
                        "chat_id": ref.chat_id,
                        "message_id": ref.message_id,
                        "message_kind": ref.message_kind,
                    },
                )
    return sent_or_edited


def send_new_order_to_config(
    config,
    order,
    *,
    title="سفارش جدید VPN",
    format_order_message_func=None,
    order_admin_keyboard_func=None,
    client_cls=None,
    delivery_error_cls=None,
    log_event_func=None,
    send_to_config_func=None,
    fit_photo_caption_func=None,
    extract_sent_message_id_func=None,
    remember_admin_order_message_func=None,
):
    if format_order_message_func is None or order_admin_keyboard_func is None:
        from .admin_orders import format_order_message, order_admin_keyboard

        format_order_message_func = format_order_message_func or format_order_message
        order_admin_keyboard_func = order_admin_keyboard_func or order_admin_keyboard
    if client_cls is None or delivery_error_cls is None or log_event_func is None:
        default_client_cls, default_delivery_error_cls, default_log_event_func = _default_delivery_deps()
        client_cls = client_cls or default_client_cls
        delivery_error_cls = delivery_error_cls or default_delivery_error_cls
        log_event_func = log_event_func or default_log_event_func
    send_to_config_func = send_to_config_func or send_to_config
    fit_photo_caption_func = fit_photo_caption_func or fit_photo_caption
    extract_sent_message_id_func = extract_sent_message_id_func or extract_sent_message_id
    remember_admin_order_message_func = remember_admin_order_message_func or remember_admin_order_message

    text = format_order_message_func(order, title=title)
    reply_markup = order_admin_keyboard_func(order)
    client = client_cls(config)
    sent = 0

    for admin_user_id in config.get_admin_user_ids():
        if order.payment_receipt_image:
            try:
                order.payment_receipt_image.open("rb")
                try:
                    payload = client.send_photo(
                        order.payment_receipt_image.file,
                        caption=fit_photo_caption_func(text),
                        reply_markup=reply_markup,
                        chat_id=admin_user_id,
                    )
                finally:
                    order.payment_receipt_image.close()
            except Exception as exc:
                config.last_error = str(exc)
                config.save(update_fields=["last_error", "updated_at"])
                log_event_func(
                    config,
                    event_type=BotEventLog.EventType.NEW_ORDER,
                    status=BotEventLog.Status.FAILED,
                    order=order,
                    message=f"Receipt photo notification failed: {exc}",
                    raw_payload={"admin_user_id": admin_user_id},
                )
                logger.warning(
                    "Bot receipt notification failed for %s order=%s admin=%s: %s",
                    config,
                    order.pk,
                    admin_user_id,
                    exc,
                )
                if send_to_config_func(
                    config,
                    text=text,
                    event_type=BotEventLog.EventType.NEW_ORDER,
                    order=order,
                    reply_markup=reply_markup,
                    chat_id=admin_user_id,
                ):
                    sent += 1
                continue

            message_id = extract_sent_message_id_func(payload)
            remember_admin_order_message_func(
                config,
                order,
                admin_user_id=admin_user_id,
                chat_id=admin_user_id,
                message_id=message_id,
                message_kind=BotAdminOrderMessage.MessageKind.PHOTO,
                metadata={"receipt_image": order.payment_receipt_image.name},
            )
            sent += 1
            log_event_func(
                config,
                event_type=BotEventLog.EventType.NEW_ORDER,
                status=BotEventLog.Status.SENT,
                order=order,
                message="New order notification sent with receipt photo.",
                raw_payload={
                    "receipt_image": order.payment_receipt_image.name,
                    "admin_user_id": admin_user_id,
                    "message_id": message_id,
                },
            )
            if len(text) > 1000:
                try:
                    client.send_message(text, chat_id=admin_user_id)
                except delivery_error_cls:
                    pass
            continue

        try:
            payload = client.send_message(text, reply_markup=reply_markup, chat_id=admin_user_id)
        except delivery_error_cls as exc:
            config.last_error = str(exc)
            config.save(update_fields=["last_error", "updated_at"])
            log_event_func(
                config,
                event_type=BotEventLog.EventType.NEW_ORDER,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=str(exc),
                raw_payload={"admin_user_id": admin_user_id},
            )
            continue

        message_id = extract_sent_message_id_func(payload)
        remember_admin_order_message_func(
            config,
            order,
            admin_user_id=admin_user_id,
            chat_id=admin_user_id,
            message_id=message_id,
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )
        sent += 1
        log_event_func(
            config,
            event_type=BotEventLog.EventType.NEW_ORDER,
            status=BotEventLog.Status.SENT,
            order=order,
            message=text,
            raw_payload={"admin_user_id": admin_user_id, "message_id": message_id},
        )

    if sent and config.last_error:
        config.last_error = ""
        config.save(update_fields=["last_error", "updated_at"])
    return sent > 0


def support_admin_keyboard(conversation):
    return {
        "inline_keyboard": [
            [
                {"text": "پاسخ", "callback_data": f"support:reply:{conversation.pk}"},
                {"text": "بستن", "callback_data": f"support:close:{conversation.pk}"},
            ]
        ]
    }


def format_support_message(conversation, message=None, *, title="پیام پشتیبانی"):
    created_at = timezone.localtime(conversation.created_at).strftime("%Y-%m-%d %H:%M") if conversation.created_at else "-"
    updated_at = timezone.localtime(conversation.updated_at).strftime("%Y-%m-%d %H:%M") if conversation.updated_at else "-"
    customer = conversation.customer
    customer_label = escape(str(customer)) if customer else "-"
    contact_value = escape(conversation.contact_value or "-")
    lines = [
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━",
        f"<b>شناسه گفتگو:</b> <code>{conversation.pk}</code>",
        f"<b>وضعیت:</b> {conversation.get_status_display()}",
        f"<b>فروشگاه:</b> {escape(conversation.store.name) if conversation.store_id else '-'}",
        f"<b>مشتری:</b> {customer_label}",
        f"<b>شماره یا تلگرام:</b> <code>{contact_value}</code>",
        f"<b>شروع:</b> <code>{created_at}</code>",
        f"<b>آخرین بروزرسانی:</b> <code>{updated_at}</code>",
    ]
    if customer:
        if customer.phone_number:
            lines.append(f"<b>شماره حساب:</b> <code>{escape(customer.phone_number)}</code>")
        if customer.username:
            lines.append(f"<b>نام کاربری حساب:</b> <code>{escape(customer.username)}</code>")
    if message:
        body = (message.body or "").strip()
        excerpt = body[:1200] + ("..." if len(body) > 1200 else "")
        lines.extend(
            [
                "",
                f"<b>متن پیام:</b>\n{escape(excerpt)}",
            ]
        )
    return "\n".join(lines)


def notify_new_order(order):
    from store.admin_notifications import notify_admins_new_order

    return notify_admins_new_order(order.pk)


def notify_duplicate_order_attempt(order):
    for config in active_bot_configs(order):
        if not config.notify_new_orders:
            continue
        send_new_order_to_config(config, order, title="هشدار سفارش تکراری")


def _send_customer_order_event_message(client, order, *, event_type, chat_id, reply_markup=None):
    from .admin_orders import format_order_message
    from .order_delivery import format_customer_order_event, send_customer_order_event_message

    def format_customer_order_event_func(order, *, event_type):
        return format_customer_order_event(
            order,
            event_type=event_type,
            format_order_message_func=format_order_message,
        )

    return send_customer_order_event_message(
        client,
        order,
        event_type=event_type,
        chat_id=chat_id,
        reply_markup=reply_markup,
        format_customer_order_event_func=format_customer_order_event_func,
    )


def notify_order_event(order, *, event_type):
    from .order_delivery import notify_customer_order_event

    title_by_event = {
        "approved": "Order approved",
        "rejected": "Order rejected",
    }
    log_type_by_event = {
        "approved": BotEventLog.EventType.ORDER_APPROVED,
        "rejected": BotEventLog.EventType.ORDER_REJECTED,
    }
    if not (order.metadata or {}).get("suppress_admin_order_updates"):
        sync_admin_order_messages(
            order,
            title=title_by_event.get(event_type, "Order updated"),
            event_type=log_type_by_event.get(event_type, BotEventLog.EventType.WEBHOOK),
        )

    client_cls, delivery_error_cls, log_event_func = _default_delivery_deps()
    notify_customer_order_event(
        order,
        event_type=event_type,
        client_cls=client_cls,
        delivery_error_cls=delivery_error_cls,
        log_event_func=log_event_func,
        send_customer_order_event_message_func=_send_customer_order_event_message,
    )


def notify_support_message(conversation, message):
    sent = 0
    for config in active_bot_configs(store=conversation.store):
        if send_to_config(
            config,
            text=format_support_message(conversation, message, title="پیام جدید پشتیبانی"),
            event_type=BotEventLog.EventType.SUPPORT_MESSAGE,
            reply_markup=support_admin_keyboard(conversation),
        ):
            sent += 1
    return sent


def build_sales_report(config):
    now = timezone.now()
    start = config.last_report_sent_at or (now - timedelta(hours=config.report_interval_hours))
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=now)
    if config.store_id:
        orders = orders.filter(store=config.store)

    completed = orders.filter(status=Order.Status.COMPLETED)
    pending = orders.filter(status=Order.Status.PENDING_VERIFICATION)
    rejected = orders.filter(status=Order.Status.REJECTED)
    gross = completed.aggregate(total=Sum("amount"))["total"] or 0
    completed_count = completed.count()
    plan_rows = (
        completed.values("plan__name")
        .annotate(count=Count("id"), total=Sum("amount"))
        .order_by("-count")[:5]
    )

    lines = [
        f"<b>{config.report_interval_hours}-hour sales report</b>",
        "",
        f"<b>Window:</b> {timezone.localtime(start).strftime('%Y-%m-%d %H:%M')} - {timezone.localtime(now).strftime('%H:%M')}",
        f"<b>Completed orders:</b> {completed_count}",
        f"<b>Pending verification:</b> {pending.count()}",
        f"<b>Rejected:</b> {rejected.count()}",
        f"<b>Revenue:</b> {money(gross)}",
    ]
    if plan_rows:
        lines.extend(["", "<b>Top plans:</b>"])
        for row in plan_rows:
            lines.append(f"- {row['plan__name'] or '-'}: {row['count']} / {money(row['total'] or 0)}")
    return "\n".join(lines), now


def maybe_send_due_sales_report(
    config,
    *,
    is_sales_report_due_func=None,
    build_sales_report_func=None,
    send_to_config_func=None,
    log_event_func=None,
):
    is_sales_report_due_func = is_sales_report_due_func or is_sales_report_due
    build_sales_report_func = build_sales_report_func or build_sales_report
    send_to_config_func = send_to_config_func or send_to_config

    if not is_sales_report_due_func(config):
        return False

    lock_key = f"bot:sales-report-lock:{config.pk}"
    if not cache.add(lock_key, "1", timeout=10 * 60):
        return False

    try:
        config.refresh_from_db(fields=["send_sales_reports", "report_interval_hours", "last_report_sent_at"])
        if not is_sales_report_due_func(config):
            return False
        report, sent_at = build_sales_report_func(config)
        if send_to_config_func(
            config,
            text=report,
            event_type=BotEventLog.EventType.SALES_REPORT,
        ):
            config.last_report_sent_at = sent_at
            config.save(update_fields=["last_report_sent_at", "updated_at"])
            return True
    except Exception as exc:
        if log_event_func is None:
            _client_cls, _delivery_error_cls, log_event_func = _default_delivery_deps()
        logger.warning("Due sales report failed for bot_config=%s: %s", config.pk, exc)
        log_event_func(
            config,
            event_type=BotEventLog.EventType.ERROR,
            status=BotEventLog.Status.FAILED,
            message=f"Due sales report failed: {exc}",
        )
    finally:
        cache.delete(lock_key)
    return False


def send_due_sales_reports(*, force=False):
    sent = 0
    now = timezone.now()
    for config in active_bot_configs(reports=True):
        due_at = (
            config.last_report_sent_at + timedelta(hours=config.report_interval_hours)
            if config.last_report_sent_at
            else None
        )
        if not force and due_at and due_at > now:
            continue

        report, sent_at = build_sales_report(config)
        if send_to_config(
            config,
            text=report,
            event_type=BotEventLog.EventType.SALES_REPORT,
        ):
            config.last_report_sent_at = sent_at
            config.save(update_fields=["last_report_sent_at", "updated_at"])
            sent += 1
    return sent
