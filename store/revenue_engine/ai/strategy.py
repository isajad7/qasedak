from decimal import Decimal, InvalidOperation

from django.core.cache import cache

from store.models import BotEventLog
from store.revenue_engine.optimization.tracker import USER_RECEIVED_OFFER


STRATEGY_WEIGHT_CACHE_KEY = "revenue_engine:ai:strategy_weights"
STRATEGY_WEIGHT_TIMEOUT = 25 * 60 * 60


def _decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


class RevenueStrategyEngine:
    DEFAULT_WEIGHTS = {
        "premium_offer": 1.08,
        "retention_offer": 1.0,
        "aggressive_discount": 0.96,
        "onboarding_upsell": 0.98,
        "balanced_offer": 1.0,
    }

    def __init__(self, weights=None):
        self.weights = weights or self.current_weights()

    def select_strategy(self, user_context=None):
        context = user_context or {}
        usage_percent = _decimal(context.get("usage_percent"))
        purchase_count = _int(context.get("purchase_count", context.get("past_purchase_count", 0)))
        lifetime_value = _decimal(context.get("lifetime_value", context.get("total_spent", 0)))
        session_started = bool(context.get("session_active") or context.get("selected_plan") or context.get("plan"))
        discount_active = bool(context.get("discount_active") or context.get("discount_code") or context.get("discount"))
        churn_risk = bool(context.get("churn_risk") or context.get("subscription_expired") or context.get("inactive_72h"))
        is_new_user = bool(context.get("is_new_user") or purchase_count == 0)

        if context.get("is_high_value") or lifetime_value >= Decimal("500000") or purchase_count >= 3:
            return self._decision(
                "premium_offer",
                confidence=0.78,
                revenue_multiplier=1.18,
                max_discount=0,
                reason="high_value_user",
            )

        if churn_risk:
            return self._decision(
                "aggressive_discount",
                confidence=0.74,
                revenue_multiplier=1.05,
                max_discount=0 if discount_active else 30,
                reason="churn_risk",
            )

        if context.get("subscription_active") and usage_percent < Decimal("10"):
            return self._decision(
                "retention_offer",
                confidence=0.66,
                revenue_multiplier=0.92,
                max_discount=0,
                reason="low_usage_active_subscription",
            )

        if is_new_user and session_started:
            return self._decision(
                "onboarding_upsell",
                confidence=0.62,
                revenue_multiplier=1.02,
                max_discount=0 if discount_active else 5,
                reason="new_user_session",
            )

        return self._decision(
            "balanced_offer",
            confidence=0.52,
            revenue_multiplier=1.0,
            max_discount=0 if discount_active else 10,
            reason="default_safe_strategy",
        )

    def _decision(self, strategy, *, confidence, revenue_multiplier, max_discount, reason):
        weight = Decimal(str(self.weights.get(strategy, 1)))
        weighted_confidence = float(min(Decimal(str(confidence)) * weight, Decimal("0.95")))
        return {
            "strategy": strategy,
            "confidence": weighted_confidence,
            "revenue_multiplier": float(Decimal(str(revenue_multiplier)) * weight),
            "max_discount": int(max_discount or 0),
            "reason": reason,
            "weight": float(weight),
        }

    def current_weights(self):
        return cache.get(STRATEGY_WEIGHT_CACHE_KEY) or dict(self.DEFAULT_WEIGHTS)

    def update_strategy_weights(self):
        stats = {}
        logs = BotEventLog.objects.filter(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event=USER_RECEIVED_OFFER,
            raw_payload__metadata__ai_generated=True,
        ).only("raw_payload")

        for log in logs.iterator():
            payload = log.raw_payload or {}
            metadata = payload.get("metadata") or {}
            strategy = metadata.get("ai_strategy")
            if not strategy:
                continue
            entry = stats.setdefault(strategy, {"impressions": 0, "conversions": 0})
            entry["impressions"] += 1
            if payload.get("converted"):
                entry["conversions"] += 1

        weights = dict(self.DEFAULT_WEIGHTS)
        for strategy, entry in stats.items():
            impressions = entry["impressions"]
            conversion_rate = (entry["conversions"] / impressions) if impressions else 0
            weights[strategy] = min(max(0.8 + conversion_rate, 0.75), 1.3)

        cache.set(STRATEGY_WEIGHT_CACHE_KEY, weights, STRATEGY_WEIGHT_TIMEOUT)
        self.weights = weights
        return {"weights": weights, "stats": stats}
