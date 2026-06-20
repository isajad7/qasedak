from decimal import Decimal, InvalidOperation

from django.db.models import Q

from store.models import BotUser, Customer, Order, Plan, VPNClient

from .triggers import CHECKOUT_STARTED, LOW_PRICE_PLAN_SELECTED, PAYMENT_SCREEN_OPENED, USER_PLAN_SELECTED


def _decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _plan_from_context(context):
    context = context or {}
    plan = context.get("selected_plan") or context.get("plan")
    return plan if isinstance(plan, Plan) else None


def _store_from_context(context, plan=None):
    return (context or {}).get("store") or getattr(plan, "store", None)


def _candidate_plans(plan, context):
    if not plan:
        return Plan.objects.none()
    store = _store_from_context(context, plan)
    plans = Plan.objects.filter(is_active=True, is_public=True, is_custom_volume=False)
    if store:
        plans = plans.filter(Q(store=store) | Q(store__isnull=True))
    operator = (context or {}).get("operator")
    if operator:
        plans = plans.filter(operators=operator)
    return plans.distinct().order_by("price", "sort_order", "pk")


def _upgrade_plan(plan, context, *, min_percent=20, max_percent=30):
    if not plan or not plan.price:
        return None
    plans = _candidate_plans(plan, context).exclude(pk=plan.pk).filter(price__gt=plan.price)
    min_price = int(Decimal(plan.price) * (Decimal("1") + Decimal(min_percent) / Decimal("100")))
    max_price = int(Decimal(plan.price) * (Decimal("1") + Decimal(max_percent) / Decimal("100")))
    return plans.filter(price__gte=min_price, price__lte=max_price).first() or plans.first()


def _large_plan(plan, context):
    if not plan:
        return None
    plans = _candidate_plans(plan, context).exclude(pk=plan.pk).filter(
        Q(volume_gb__gte=Decimal(plan.volume_gb or 0) * Decimal("2")) | Q(price__gt=plan.price)
    )
    return plans.order_by("-volume_gb", "price", "pk").first() or _upgrade_plan(plan, context, min_percent=40, max_percent=100)


def _is_small_plan(plan, context):
    if (context or {}).get("is_small_plan") is True:
        return True
    if not plan:
        return False
    plans = list(_candidate_plans(plan, context))
    if len(plans) <= 1:
        return False
    first_tier = max(len(plans) // 3, 1)
    return plan.pk in {candidate.pk for candidate in plans[:first_tier]}


def _customer_from_user(user, context):
    bot_user = (context or {}).get("bot_user")
    if isinstance(bot_user, BotUser) and bot_user.customer_id:
        return bot_user.customer
    if isinstance(user, BotUser) and user.customer_id:
        return user.customer
    if isinstance(user, Customer):
        return user
    if isinstance(user, Order) and user.customer_id:
        return user.customer
    return None


def _usage_history_high(user, context):
    context = context or {}
    if context.get("usage_history_high") is True:
        return True
    if _decimal(context.get("usage_percent")) > Decimal("80"):
        return True

    customer = _customer_from_user(user, context)
    if not customer:
        return False
    clients = VPNClient.objects.filter(order__customer=customer, traffic_limit_bytes__gt=0).order_by("-updated_at")[:5]
    for vpn_client in clients:
        usage_percent = (Decimal(vpn_client.used_traffic_bytes or 0) * Decimal("100")) / Decimal(vpn_client.traffic_limit_bytes)
        if usage_percent > Decimal("80"):
            return True
    return False


def _discount_active(context):
    context = context or {}
    return bool(context.get("discount_active") or context.get("discount_code") or context.get("discount"))


def _plan_label(plan):
    if not plan:
        return "پلن بهتر"
    return f"{plan.name} ({plan.volume_gb:g}GB)"


class UpsellRuleEngine:
    def evaluate(self, event_type, user, context=None):
        context = context or {}
        plan = _plan_from_context(context)

        if _discount_active(context):
            upgrade_plan = _upgrade_plan(plan, context)
            return {
                "type": "upsell_offer",
                "title": "ارتقا به‌جای تخفیف",
                "upgrade_plan": upgrade_plan,
                "message": f"🔥 به‌جای تخفیف کوچک، با ارتقا به {_plan_label(upgrade_plan)} ارزش بیشتری بگیرید.",
            }

        if _usage_history_high(user, context):
            upgrade_plan = _large_plan(plan, context)
            return {
                "type": "upsell_offer",
                "title": "پیشنهاد پلن بزرگ‌تر",
                "upgrade_plan": upgrade_plan,
                "message": "🔥 فقط با 30٪ بیشتر → 2 برابر حجم دریافت کنید",
            }

        if event_type in {CHECKOUT_STARTED, PAYMENT_SCREEN_OPENED}:
            offer_type = "speed_boost" if event_type == PAYMENT_SCREEN_OPENED else "extra_gb"
            title = "افزایش سرعت" if offer_type == "speed_boost" else "حجم اضافه"
            message = "🚀 با افزونه سرعت، تجربه اتصال روان‌تری داشته باشید." if offer_type == "speed_boost" else "🎁 همین حالا چند گیگ حجم اضافه به سفارشتان اضافه کنید."
            return {
                "type": "upsell_offer",
                "title": title,
                "add_on": offer_type,
                "message": message,
            }

        if event_type in {USER_PLAN_SELECTED, LOW_PRICE_PLAN_SELECTED} and (
            event_type == LOW_PRICE_PLAN_SELECTED or _is_small_plan(plan, context)
        ):
            upgrade_plan = _upgrade_plan(plan, context)
            if not upgrade_plan:
                return None
            return {
                "type": "upsell_offer",
                "title": "پلن بهتر با ارزش بیشتر",
                "upgrade_plan": upgrade_plan,
                "message": f"🔥 فقط با 30٪ بیشتر → پلن {_plan_label(upgrade_plan)} را انتخاب کنید.",
            }

        return None
