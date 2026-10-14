"""Admin-only directory and assignment wizard for the current bot."""
from html import escape
from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.paginator import Paginator

from store.customer_assignment import (
    assign_customer_service, assignment_assets, assignment_plans, asset_is_available,
    asset_label, ensure_target_customer, search_bot_users, target_label, target_orders,
)
from store.models import BotUser, ConfigInventoryAsset, Order, Plan
from store.order_recovery import customer_notification_summary, resend_order_result
from store.order_revenue import RevenueSettingsForm

from .admin_orders import bot_order_status, order_admin_keyboard
from .formatting import money

PREFIX = "admin:users:"
SEARCH_STATE = "admin_users_search"
AMOUNT_STATE = "admin_users_amount"
DRAFT_STATE = "admin_users_draft"


def button(text, value):
    return {"text": text, "callback_data": PREFIX + value}


def send(client, chat_id, text, rows):
    rows.append([button("بازگشت به کاربران", "list:1")])
    client.send_message(text, chat_id=chat_id, reply_markup={"inline_keyboard": rows})
    return {"ok": True, "handled": True}


def save_state(admin, state, data):
    admin.state, admin.state_data = state, data
    admin.save(update_fields=["state", "state_data", "updated_at"])


def page_rows(items, page, label, callback, page_callback):
    page = Paginator(items, 8).get_page(page)
    rows = [[button(label(item)[:90], callback(item))] for item in page]
    nav = []
    if page.has_previous():
        nav.append(button("قبلی", page_callback(page.previous_page_number())))
    if page.has_next():
        nav.append(button("بعدی", page_callback(page.next_page_number())))
    if nav:
        rows.append(nav)
    return page, rows


def show_users(client, config, admin, *, chat_id, page=1, query=""):
    save_state(admin, DRAFT_STATE, {"query": query})
    page, rows = page_rows(search_bot_users(config, query), page, target_label,
                           lambda user: f"view:{user.pk}", lambda number: f"page:{number}")
    rows.insert(0, [button("جست‌وجو با آیدی، @username یا نام", "search")])
    text = f"کاربران همین ربات · صفحه {page.number} از {page.paginator.num_pages}\n"
    text += f"جست‌وجو: {escape(query)}\n" if query else ""
    text += "\n".join(escape(target_label(user)) for user in page) or "کاربری پیدا نشد."
    return send(client, chat_id, text, rows)


def show_profile(client, target, *, chat_id):
    from store.customer_delivery import resolve_customer_order_delivery
    from .services_flow import bot_subscription_clients

    lines = ["پرونده کاربر", escape(target_label(target)), "", "سرویس‌های فعلی:"]
    clients = bot_subscription_clients(target).filter(order__in=target_orders(target))
    for vpn in clients[:10]:
        lines.append(f"#{vpn.pk} · {escape(vpn.plan.name if vpn.plan_id else 'سرویس')} · {escape(vpn.get_status_display())}")
    orders = list(target_orders(target)[:10])
    for order in target_orders(target).filter(status=Order.Status.COMPLETED, vpn_clients__isnull=True)[:10]:
        if resolve_customer_order_delivery(order).is_ready:
            lines.append(f"{escape(order.plan.name)} · {order.order_tracking_code}")
    lines.append("\nسفارش‌های اخیر:")
    rows = [[button("اختصاص سرویس جدید", f"plans:new:{target.pk}:1")],
            [button("اختصاص کانفیگ موجود", f"plans:existing:{target.pk}:1")]]
    for order in orders:
        lines.append(f"{order.order_tracking_code} · {escape(order.plan.name)} · {bot_order_status(order)}")
        rows.append([{"text": f"جزئیات سفارش {order.order_tracking_code}",
                      "callback_data": f"order:detail:{order.order_tracking_code}"}])
    rows.append([button("ارسال مجدد سرویس", f"services:{target.pk}:1")])
    if not orders:
        lines.append("هنوز سفارشی ثبت نشده است.")
    return send(client, chat_id, "\n".join(lines), rows)


def start_finance(client, admin, target, plan, mode, asset=None, *, chat_id):
    draft = {"target": target.pk, "plan": plan.pk, "mode": mode, "asset": asset.pk if asset else None,
             "request_id": uuid4().hex}
    save_state(admin, DRAFT_STATE, draft)
    token = draft["request_id"]
    return send(client, chat_id, f"{escape(target_label(target))}\n{escape(plan.name)}\nاین سرویس در درآمد حساب شود؟",
                [[button("بله", f"yes:{token}"), button("خیر", f"no:{token}")]])


def preview(client, config, admin, *, chat_id):
    draft = admin.state_data
    target = search_bot_users(config).get(pk=draft["target"])
    plan = assignment_plans(config).get(pk=draft["plan"])
    text = (f"تأیید اختصاص\nمشتری: {escape(target_label(target))}\nپلن: {escape(plan.name)}\n"
            f"نوع: {'کانفیگ موجود #' + str(draft['asset']) if draft['mode'] == 'existing' else 'سرویس جدید'}\n"
            f"درآمد: {escape(money(draft.get('amount') or 0, plan.currency))}\n"
            "با تأیید، سرویس برای همین مشتری ثبت و ارسال می‌شود.")
    save_state(admin, DRAFT_STATE, draft)
    return send(client, chat_id, text, [[button("تأیید اختصاص و ارسال", f"confirm:{draft['request_id']}")],
                                      [button("لغو", f"view:{target.pk}")]])


def handle_callback(client, config, admin, data, *, chat_id):
    if not config.is_admin_user(admin.provider_user_id):
        return {"ok": True, "permission_denied": True}
    parts = data[len(PREFIX):].split(":")
    action = parts[0]
    try:
        if action in {"list", "page"}:
            return show_users(client, config, admin, chat_id=chat_id, page=parts[1],
                              query=(admin.state_data or {}).get("query", "") if action == "page" else "")
        if action == "search":
            save_state(admin, SEARCH_STATE, {})
            return send(client, chat_id, "آیدی عددی تلگرام، @username یا نام کاربر را بفرست.", [])
        if action == "view":
            admin.reset_state()
            return show_profile(client, search_bot_users(config).get(pk=int(parts[1])), chat_id=chat_id)
        if action == "plans":
            mode, target_id, number = parts[1:4]
            target = search_bot_users(config).get(pk=int(target_id))
            if mode not in {"new", "existing"}:
                raise ValueError
            plans = assignment_plans(config)
            if mode == "existing":
                plans = [plan for plan in plans if assignment_assets(plan).exists()]
            page, rows = page_rows(plans, number, lambda plan: f"{plan.name} · {money(plan.price, plan.currency)}",
                                  lambda plan: f"plan:{mode}:{target.pk}:{plan.pk}",
                                  lambda n: f"plans:{mode}:{target.pk}:{n}")
            return send(client, chat_id, f"{escape(target_label(target))}\nپلن را انتخاب کن · صفحه {page.number}", rows)
        if action in {"plan", "assets"}:
            if action == "plan":
                mode, target_id, plan_id = parts[1:4]
                number = 1
            else:
                target_id, plan_id, number = parts[1:4]
                mode = "existing"
            target = search_bot_users(config).get(pk=int(target_id))
            plan = assignment_plans(config).get(pk=int(plan_id))
            if mode == "new":
                return start_finance(client, admin, target, plan, mode, chat_id=chat_id)
            assets = [asset for asset in assignment_assets(plan) if asset_is_available(asset)]
            page, rows = page_rows(assets, number, asset_label,
                                  lambda asset: f"asset:{target.pk}:{plan.pk}:{asset.pk}",
                                  lambda n: f"assets:{target.pk}:{plan.pk}:{n}")
            return send(client, chat_id, f"کانفیگ قابل‌واگذاری را انتخاب کن · صفحه {page.number}\nبدون ساخت سرویس روی پنل", rows)
        if action == "asset":
            target = search_bot_users(config).get(pk=int(parts[1]))
            plan = assignment_plans(config).get(pk=int(parts[2]))
            asset = assignment_assets(plan).get(pk=int(parts[3]))
            if not asset_is_available(asset):
                raise ValidationError("این کانفیگ دیگر قابل‌واگذاری نیست.")
            return start_finance(client, admin, target, plan, "existing", asset, chat_id=chat_id)
        if action in {"yes", "no", "default", "confirm"}:
            draft = dict(admin.state_data or {})
            token = parts[1]
            if token != draft.get("request_id"):
                # An old confirm can show its existing order, but never execute again.
                from uuid import UUID
                old = Order.objects.filter(public_id=UUID(token), metadata__assignment_actor=f"bot:{config.pk}:{admin.provider_user_id}").first()
                if action == "confirm" and old:
                    return show_result(client, old, "این عملیات قبلاً ثبت شده است.", chat_id=chat_id)
                raise ValidationError("این پیش‌نمایش دیگر فعال نیست؛ از پرونده کاربر شروع کن.")
            plan = assignment_plans(config).get(pk=draft["plan"])
            if action == "yes":
                draft["included"] = True
                save_state(admin, AMOUNT_STATE, draft)
                return send(client, chat_id, f"مبلغ درآمد را به {plan.get_currency_display()} بفرست یا مبلغ پیشنهادی را انتخاب کن.",
                            [[button(money(plan.price, plan.currency), f"default:{token}")]])
            if action in {"no", "default"}:
                form = RevenueSettingsForm({"revenue_included": "no" if action == "no" else "yes", "revenue_amount": str(plan.price)})
                if not form.is_valid():
                    raise ValidationError("مبلغ مثبت وارد کن.")
                draft.update(included=form.cleaned_data["revenue_included"], amount=form.cleaned_data["revenue_amount"])
                admin.state_data = draft
                return preview(client, config, admin, chat_id=chat_id)
            if admin.state != DRAFT_STATE or "included" not in draft or "amount" not in draft:
                raise ValidationError("ابتدا وضعیت درآمد و مبلغ را انتخاب کن.")
            target = search_bot_users(config).get(pk=draft["target"])
            customer = ensure_target_customer(target)
            asset = assignment_assets(plan).get(pk=draft["asset"]) if draft["asset"] else None
            result = assign_customer_service(
                customer=customer, target=target, plan=plan, mode=draft["mode"], asset=asset,
                included=draft["included"], amount=draft["amount"], request_id=token,
                actor_key=f"bot:{config.pk}:{admin.provider_user_id}",
            )
            if result.order:
                admin.reset_state()
                return show_result(client, result.order, result.message, chat_id=chat_id)
            return send(client, chat_id, escape(result.message), [[button("پرونده کاربر", f"view:{target.pk}")]])
        if action == "services":
            target = search_bot_users(config).get(pk=int(parts[1]))
            page, rows = page_rows(target_orders(target).filter(status=Order.Status.COMPLETED), parts[2],
                                  lambda order: f"{order.plan.name} · {order.order_tracking_code}",
                                  lambda order: f"resend:{target.pk}:{order.pk}", lambda n: f"services:{target.pk}:{n}")
            return send(client, chat_id, f"سرویس را برای ارسال مجدد انتخاب کن · صفحه {page.number}", rows)
        if action == "resend":
            target = search_bot_users(config).get(pk=int(parts[1]))
            order = target_orders(target).get(pk=int(parts[2]))
            result = resend_order_result(order, target_bot_user=target)
            return show_result(client, order, result.message, chat_id=chat_id)
    except (ValueError, IndexError, KeyError, ValidationError, BotUser.DoesNotExist, Order.DoesNotExist,
            Plan.DoesNotExist, ConfigInventoryAsset.DoesNotExist) as exc:
        text = "؛ ".join(exc.messages) if isinstance(exc, ValidationError) else "انتخاب معتبر نیست؛ دوباره از فهرست شروع کن."
        return send(client, chat_id, escape(text), [])
    return {"ok": True, "ignored": True}


def show_result(client, order, message, *, chat_id):
    order.refresh_from_db()
    text = f"{escape(message)}\nسفارش: {order.order_tracking_code}\nتکمیل سرویس: {bot_order_status(order)}\nپیام مشتری: {customer_notification_summary(order)['label']}"
    client.send_message(text, chat_id=chat_id, reply_markup=order_admin_keyboard(order))
    return {"ok": True, "handled": True, "order_id": order.pk}


def handle_text(client, config, admin, text, *, chat_id):
    if not config.is_admin_user(admin.provider_user_id):
        return {"ok": True, "permission_denied": True}
    if admin.state == SEARCH_STATE:
        return show_users(client, config, admin, chat_id=chat_id, query=text)
    if admin.state == AMOUNT_STATE:
        form = RevenueSettingsForm({"revenue_included": "yes", "revenue_amount": text})
        if not form.is_valid():
            return send(client, chat_id, "مبلغ مثبت و معتبر وارد کن.", [])
        admin.state_data.update(included=True, amount=form.cleaned_data["revenue_amount"])
        try:
            return preview(client, config, admin, chat_id=chat_id)
        except (ObjectDoesNotExist, KeyError):
            admin.reset_state()
            return send(client, chat_id, "مشتری یا پلن دیگر در دسترس نیست؛ از فهرست شروع کن.", [])
    return {"ok": True, "ignored": True}
