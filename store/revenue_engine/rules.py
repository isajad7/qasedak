from decimal import Decimal, InvalidOperation

from .triggers import HIGH_USAGE, HIGH_USAGE_USER, USER_EXPIRED, USER_NEAR_EXPIRY


def _usage_percent(context):
    if not isinstance(context, dict):
        return Decimal("0")
    raw_value = context.get("usage_percent")
    if raw_value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


class RuleEngine:
    def evaluate(self, event_type, user, context=None):
        context = context or {}
        usage_percent = _usage_percent(context)

        if event_type == USER_EXPIRED:
            return {
                "type": "user_expired",
                "discount": 25,
                "message": "سرویس شما منقضی شده - تمدید با 25% تخفیف",
            }

        if event_type == USER_NEAR_EXPIRY:
            if usage_percent > 80:
                return {
                    "type": "near_expiry_high_usage",
                    "discount": 15,
                    "bonus_volume": True,
                    "message": "سرویس شما در حال اتمام است + پیشنهاد ویژه",
                }
            if Decimal("50") <= usage_percent <= Decimal("80"):
                return {
                    "type": "near_expiry_medium_usage",
                    "discount": 10,
                    "message": "سرویس شما در حال اتمام است + پیشنهاد ویژه",
                }
            return {
                "type": "near_expiry_low_usage",
                "discount": 5,
                "message": "سرویس شما در حال اتمام است + پیشنهاد ویژه",
            }

        if event_type in {HIGH_USAGE, HIGH_USAGE_USER}:
            return {
                "type": "high_usage_upgrade",
                "volume_multiplier": 2,
                "message": "مصرف شما بالاست - پیشنهاد ارتقا",
            }

        return None
