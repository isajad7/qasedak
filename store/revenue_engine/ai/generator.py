from decimal import Decimal, InvalidOperation


def _decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class OfferGenerator:
    def generate(
        self,
        user_context=None,
        usage=None,
        subscription=None,
        history=None,
        *,
        strategy=None,
        base_offer=None,
        offer_type="renewal",
    ):
        context = {}
        context.update(user_context or {})
        context.update(usage or {})
        context.update(subscription or {})
        context.update(history or {})

        strategy = strategy or {}
        base_offer = dict(base_offer or {})
        strategy_name = strategy.get("strategy") or "balanced_offer"
        max_discount = int(strategy.get("max_discount") or 0)
        discount_active = bool(context.get("discount_active") or context.get("discount_code") or context.get("discount"))
        usage_percent = _decimal(context.get("usage_percent"))

        offer = {
            "type": "generated_offer",
            "title": "پیشنهاد اختصاصی",
            "message": base_offer.get("message") or "یک پیشنهاد اختصاصی برای شما آماده است.",
            "optimization_offer_type": offer_type,
            "experiment_variant": "AI",
            "experiment_label": strategy_name,
            "experiment_id": f"ai:{offer_type}:{strategy_name}",
            "ai_generated": True,
            "ai_strategy": strategy_name,
            "ai_confidence": float(strategy.get("confidence", 0)),
            "ai_strategy_reason": strategy.get("reason", ""),
            "expected_revenue_multiplier": float(strategy.get("revenue_multiplier", 1)),
            "base_rule_type": base_offer.get("type", ""),
        }

        upgrade_plan = base_offer.get("upgrade_plan")
        if upgrade_plan is not None:
            offer["upgrade_plan"] = upgrade_plan
        if base_offer.get("add_on"):
            offer["add_on"] = base_offer.get("add_on")
        if base_offer.get("bonus_gb"):
            offer["bonus_gb"] = int(base_offer.get("bonus_gb") or 0)
        if base_offer.get("discount") and not discount_active:
            offer["discount"] = int(base_offer.get("discount") or 0)

        if strategy_name == "premium_offer":
            offer.update(
                {
                    "title": "پیشنهاد پریمیوم اختصاصی",
                    "bonus_gb": max(int(base_offer.get("bonus_gb") or 0), 2),
                    "add_on": offer.get("add_on") or "speed_boost",
                    "message": "برای مصرف حرفه‌ای شما، پلن پریمیوم با سرعت بهتر و 2GB حجم هدیه پیشنهاد می‌شود.",
                }
            )
        elif strategy_name == "retention_offer":
            offer.update(
                {
                    "title": "پیشنهاد فعال‌سازی دوباره",
                    "bonus_gb": max(int(base_offer.get("bonus_gb") or 0), 1),
                    "message": "اگر سرویس کم استفاده شده، با 1GB هدیه و پشتیبانی سریع‌تر دوباره تستش کنید.",
                    "support_priority": True,
                }
            )
        elif strategy_name == "aggressive_discount":
            discount = 0 if discount_active else min(max(int(base_offer.get("discount") or 0), 20), max_discount or 30)
            offer.update(
                {
                    "title": "پیشنهاد بازگشت ویژه",
                    "message": f"برای بازگشت سریع، {discount}% تخفیف ویژه فعال شده است."
                    if discount
                    else "برای بازگشت سریع، پیشنهاد ویژه بدون تخفیف تکراری آماده شده است.",
                }
            )
            if discount:
                offer["discount"] = discount
        elif strategy_name == "onboarding_upsell":
            offer.update(
                {
                    "title": "پیشنهاد شروع بهتر",
                    "bonus_gb": 1,
                    "message": "برای شروع راحت‌تر، خرید این پلن با 1GB حجم هدیه پیشنهاد می‌شود.",
                }
            )
            if max_discount and not discount_active:
                offer["discount"] = min(max_discount, 5)
        else:
            discount = int(base_offer.get("discount") or 0)
            if discount and not discount_active:
                offer["discount"] = min(discount, max_discount or discount)
            if usage_percent > Decimal("80"):
                offer["bonus_gb"] = max(int(base_offer.get("bonus_gb") or 0), 1)
                offer["message"] = "با توجه به مصرف بالا، پیشنهاد بهتر با حجم اضافه برای شما آماده است."

        if discount_active:
            offer.pop("discount", None)
            offer["discount_blocked_reason"] = "discount_already_active"

        return offer
