from django.db.models import Q

from store.customer_delivery import (
    customer_delivery_flat_links,
    customer_delivery_link_groups,
    customer_visible_config_count,
)
from store.jalali import persian_digits
from store.models import BotEventLog, BotUser

from .config_delivery import config_send_result_count, send_config_links_message


def format_customer_order_event(order, *, event_type, format_order_message_func):
    if event_type == "approved":
        lines = [
            "✅ سرویس شما آماده شد",
            "",
            f"کد پیگیری: {order.order_tracking_code}",
            f"پلن: {order.plan.name if order.plan_id else '-'}",
            f"تعداد کانفیگ: {persian_digits(customer_visible_config_count(order))}",
        ]
        if order.operator_id:
            lines.append(f"اپراتور: {order.operator.name}")
        lines.extend(["", "کانفیگ در پیام بعدی ارسال می‌شود."])
        return "\n".join(lines)

    if event_type == "rejected":
        lines = [
            "پرداخت شما تایید نشد",
            "━━━━━━━━━━━━━━",
            "",
            f"کد پیگیری: {order.order_tracking_code}",
        ]
        if order.rejection_reason:
            lines.append(f"دلیل: {order.rejection_reason}")
        lines.append("برای پیگیری با پشتیبانی در ارتباط باشید.")
        return "\n".join(lines)

    return format_order_message_func(order, title="Order updated")


def order_config_links(order):
    return customer_delivery_flat_links(order)


def order_config_link_groups(order):
    return customer_delivery_link_groups(order)


def approved_order_detail_lines(order, *, config_label=""):
    lines = [
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name if order.plan_id else '-'}",
        f"تعداد کانفیگ: {persian_digits(customer_visible_config_count(order))}",
    ]
    if order.operator_id:
        lines.append(f"اپراتور: {order.operator.name}")
    if config_label:
        lines.append(config_label)
    return lines


def send_customer_order_event_message(
    client,
    order,
    *,
    event_type,
    chat_id,
    reply_markup=None,
    format_customer_order_event_func,
):
    text = format_customer_order_event_func(order, event_type=event_type)
    if event_type != "approved":
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return 1

    groups = order_config_link_groups(order)
    if not groups:
        client.send_message(text, chat_id=chat_id, reply_markup=reply_markup)
        return 1

    sent = 0
    for group in groups:
        title = "✅ سرویس شما آماده شد"
        if group["label"]:
            title = f"{title} - {group['label']}"
        result = send_config_links_message(
            client,
            chat_id,
            subscription_link=group["subscription_link"],
            direct_link=group["direct_link"],
            title=title,
            detail_lines=approved_order_detail_lines(order, config_label=group["label"]),
        )
        sent += config_send_result_count(result)
        project_subscription_link = group.get("project_subscription_link") or ""
        project_client_link = group.get("project_client_link") or ""
        if project_subscription_link and project_subscription_link != group["subscription_link"]:
            project_result = send_config_links_message(
                client,
                chat_id,
                dashboard_link=project_subscription_link,
                client_link=project_client_link,
                title="✅ لینک سرویس شما آماده شد",
                detail_lines=approved_order_detail_lines(order, config_label=group["label"]),
            )
            sent += config_send_result_count(project_result)
    return sent


def notify_customer_order_event(
    order,
    *,
    event_type,
    client_cls,
    delivery_error_cls,
    log_event_func,
    send_customer_order_event_message_func,
):
    if not order.customer_id or (order.metadata or {}).get("suppress_customer_notification"):
        return 0

    base_bot_users = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=order.customer,
            is_active=True,
            bot_config__is_active=True,
        )
        .exclude(chat_id="")
    )
    bot_users = base_bot_users
    order_bot = (order.metadata or {}).get("bot") or {}
    bot_user_id = order_bot.get("bot_user_id")
    bot_config_id = order_bot.get("bot_config_id")
    if bot_user_id:
        targeted_bot_user = base_bot_users.filter(pk=bot_user_id).first()
        if targeted_bot_user:
            bot_users = [targeted_bot_user]
        elif bot_config_id:
            bot_users = base_bot_users.filter(bot_config_id=bot_config_id)
    elif bot_config_id:
        bot_users = base_bot_users.filter(bot_config_id=bot_config_id)
    elif order.store_id:
        bot_users = base_bot_users.filter(Q(bot_config__store=order.store) | Q(bot_config__store__isnull=True))

    sent = 0
    for bot_user in bot_users:
        try:
            send_customer_order_event_message_func(
                client_cls(bot_user.bot_config),
                order,
                event_type=event_type,
                chat_id=bot_user.chat_id,
            )
        except delivery_error_cls as exc:
            log_event_func(
                bot_user.bot_config,
                event_type=BotEventLog.EventType.ERROR,
                status=BotEventLog.Status.FAILED,
                order=order,
                message=f"Could not notify customer {bot_user.pk}: {exc}",
            )
            continue
        sent += 1
    return sent
