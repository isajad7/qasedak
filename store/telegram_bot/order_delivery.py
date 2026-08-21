from django.db.models import Q

from store.jalali import persian_digits
from store.models import BotEventLog, BotUser
from store.subscription_cups import (
    build_subscription_cup_base64_url,
    build_subscription_cup_dashboard_url,
    get_subscription_cup_for_order,
    get_subscription_cup_for_vpn_client,
)

from .config_delivery import config_send_result_count, send_config_links_message


def format_customer_order_event(order, *, event_type, format_order_message_func):
    if event_type == "approved":
        lines = [
            "✅ سرویس شما آماده شد",
            "",
            f"کد پیگیری: {order.order_tracking_code}",
            f"پلن: {order.plan.name if order.plan_id else '-'}",
            f"تعداد کانفیگ: {persian_digits(order.quantity or 1)}",
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
    links = []
    clients = list(order.get_vpn_clients())
    if clients:
        groups = order_config_link_groups(order)
        for index, group in enumerate(groups, start=1):
            prefix = f"کانفیگ {persian_digits(index)}" if len(groups) > 1 else "کانفیگ"
            for label, link in (
                ("لینک اشتراک", group.get("subscription_link")),
                ("لینک مستقیم", group.get("direct_link")),
                ("لینک مدیریت و ورود به برنامه", group.get("project_subscription_link")),
                ("لینک سازگار جایگزین", group.get("project_client_link")),
            ):
                if not link:
                    continue
                links.append((f"{prefix} - {label}", link))
        return links
    if order.sub_link:
        links.append(("کانفیگ - لینک اشتراک", order.sub_link))
    if order.direct_link:
        links.append(("کانفیگ - لینک مستقیم", order.direct_link))
    cup = get_subscription_cup_for_order(order)
    if cup:
        links.append(("کانفیگ - لینک مدیریت و ورود به برنامه", build_subscription_cup_dashboard_url(cup, store=order.store)))
        links.append(("کانفیگ - لینک سازگار جایگزین", build_subscription_cup_base64_url(cup, store=order.store)))
    return links


def _project_subscription_urls(cup, store):
    if not cup:
        return "", ""
    return (
        build_subscription_cup_dashboard_url(cup, store=store),
        build_subscription_cup_base64_url(cup, store=store),
    )


def order_config_link_groups(order):
    clients = list(order.get_vpn_clients())
    if clients:
        expanded = []
        for vpn_client in clients:
            cup = get_subscription_cup_for_vpn_client(vpn_client)
            project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
            bundle_results = (vpn_client.xui_raw or {}).get("bundle_inbound_results") or []
            if bundle_results:
                for index, result in enumerate(bundle_results, start=1):
                    expanded.append(
                        {
                            "subscription_link": result.get("sub_link") or vpn_client.sub_link,
                            "direct_link": result.get("direct_link") or "",
                            "project_subscription_link": project_subscription_link if index == 1 else "",
                            "project_client_link": project_client_link if index == 1 else "",
                        }
                    )
            else:
                expanded.append(
                    {
                        "subscription_link": vpn_client.sub_link,
                        "direct_link": vpn_client.direct_link,
                        "project_subscription_link": project_subscription_link,
                        "project_client_link": project_client_link,
                    }
                )
        total = len(expanded)
        groups = []
        seen_subscription_links = set()
        seen_project_subscription_links = set()
        seen_project_client_links = set()
        for index, item in enumerate(expanded, start=1):
            label = f"کانفیگ {persian_digits(index)}" if total > 1 else ""
            subscription_link = item["subscription_link"]
            if subscription_link and subscription_link in seen_subscription_links:
                subscription_link = ""
            elif subscription_link:
                seen_subscription_links.add(subscription_link)
            project_subscription_link = item.get("project_subscription_link") or ""
            if project_subscription_link and project_subscription_link in seen_project_subscription_links:
                project_subscription_link = ""
            elif project_subscription_link:
                seen_project_subscription_links.add(project_subscription_link)
            project_client_link = item.get("project_client_link") or ""
            if project_client_link and project_client_link in seen_project_client_links:
                project_client_link = ""
            elif project_client_link:
                seen_project_client_links.add(project_client_link)
            groups.append(
                {
                    "label": label,
                    "subscription_link": subscription_link,
                    "direct_link": item["direct_link"],
                    "project_subscription_link": project_subscription_link,
                    "project_client_link": project_client_link,
                }
            )
        return groups
    if order.sub_link or order.direct_link:
        cup = get_subscription_cup_for_order(order)
        project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
        return [
            {
                "label": "",
                "subscription_link": order.sub_link,
                "direct_link": order.direct_link,
                "project_subscription_link": project_subscription_link,
                "project_client_link": project_client_link,
            }
        ]
    cup = get_subscription_cup_for_order(order)
    if cup:
        project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
        return [
            {
                "label": "",
                "subscription_link": "",
                "direct_link": "",
                "project_subscription_link": project_subscription_link,
                "project_client_link": project_client_link,
            }
        ]
    return []


def approved_order_detail_lines(order, *, config_label=""):
    lines = [
        f"کد پیگیری: {order.order_tracking_code}",
        f"پلن: {order.plan.name if order.plan_id else '-'}",
        f"تعداد کانفیگ: {persian_digits(order.quantity or 1)}",
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
