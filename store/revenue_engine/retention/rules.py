from .triggers import (
    SILENT_ACTIVE_USER,
    USER_CANCELLED_SUBSCRIPTION,
    USER_EXPIRED_NO_RENEW,
    USER_INACTIVE_24H,
    USER_INACTIVE_72H,
    USER_RETURNED_AFTER_ABSENCE,
)


class RetentionRuleEngine:
    def evaluate(self, event_type, user, context=None):
        context = context or {}

        if event_type == USER_INACTIVE_24H:
            return {
                "type": "retention_offer",
                "message": "مدتی است از سرویس استفاده نکرده‌اید؛ هر وقت آماده بودید، خرید بعدی همین‌جاست.",
            }

        if event_type == USER_INACTIVE_72H:
            return {
                "type": "retention_offer",
                "discount": 20,
                "message": "💔 دلتنگ شدیم! 20% تخفیف بازگشت فعال شد",
            }

        if event_type == USER_EXPIRED_NO_RENEW:
            return {
                "type": "retention_offer",
                "discount": 25,
                "message": "سرویس شما تمدید نشده؛ برای بازگشت، 25% تخفیف ویژه فعال شد.",
            }

        if event_type == USER_CANCELLED_SUBSCRIPTION:
            return {
                "type": "retention_offer",
                "discount": 15,
                "message": "اگر دوباره خواستید برگردید، یک پیشنهاد فعال‌سازی مجدد برای شما آماده است.",
            }

        if event_type == USER_RETURNED_AFTER_ABSENCE:
            return {
                "type": "retention_offer",
                "bonus_gb": 1,
                "message": "🎁 خوش برگشتی! 1GB هدیه برای شما فعال شد",
            }

        if event_type == SILENT_ACTIVE_USER:
            return {
                "type": "support_check_in",
                "message": "user has active subscription but low usage",
            }

        return None
