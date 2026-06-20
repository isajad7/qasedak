import logging
from decimal import Decimal, InvalidOperation

from store.revenue_engine.ai.generator import OfferGenerator
from store.revenue_engine.ai.predictor import PurchasePredictor
from store.revenue_engine.ai.strategy import RevenueStrategyEngine
from store.revenue_engine.guards import get_revenue_settings
from store.revenue_engine.optimization.experiment import ExperimentEngine
from store.revenue_engine.optimization.scoring import ScoringEngine
from store.revenue_engine.optimization.selector import OfferSelector
from store.revenue_engine.optimization.tracker import OfferTracker, resolve_offer_user_id


logger = logging.getLogger(__name__)


def _decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class AIRevenueOptimizer:
    def __init__(
        self,
        *,
        strategy_engine=None,
        offer_generator=None,
        predictor=None,
        experiment_engine=None,
        offer_selector=None,
        scoring_engine=None,
        tracker=None,
        confidence_threshold=0.58,
        prediction_threshold=0.18,
    ):
        self.strategy_engine = strategy_engine or RevenueStrategyEngine()
        self.offer_generator = offer_generator or OfferGenerator()
        self.predictor = predictor or PurchasePredictor()
        self.experiment_engine = experiment_engine or ExperimentEngine()
        self.offer_selector = offer_selector or OfferSelector()
        self.scoring_engine = scoring_engine or ScoringEngine()
        self.tracker = tracker or OfferTracker()
        self.confidence_threshold = confidence_threshold
        self.prediction_threshold = prediction_threshold

    def optimize(self, offer_type, rule_decision, user=None, context=None):
        context = dict(context or {})
        if not rule_decision:
            return None

        user_id = resolve_offer_user_id(user, context)
        settings = get_revenue_settings(context.get("store"))
        if settings and not getattr(settings, "revenue_optimization_enabled", True):
            return rule_decision
        fallback_decision = self.optimization_fallback(offer_type, rule_decision, user_id=user_id)
        if settings and not getattr(settings, "ai_revenue_optimizer_enabled", True):
            return fallback_decision or rule_decision
        if rule_decision.get("type") == "support_check_in":
            return fallback_decision or rule_decision
        if user_id and self.tracker.user_in_cooldown(user_id, offer_type):
            return self._mark_fallback(fallback_decision or rule_decision, "cooldown_active")

        try:
            user_context = self._build_user_context(user, context)
            strategy = self.strategy_engine.select_strategy(user_context)
            generated = self.offer_generator.generate(
                user_context,
                {"usage_percent": user_context.get("usage_percent")},
                {
                    "subscription_active": user_context.get("subscription_active"),
                    "subscription_expired": user_context.get("subscription_expired"),
                },
                {
                    "purchase_count": user_context.get("purchase_count"),
                    "lifetime_value": user_context.get("lifetime_value"),
                },
                strategy=strategy,
                base_offer=rule_decision,
                offer_type=offer_type,
            )
            prediction = self.predictor.predict(
                user_behavior=user_context,
                past_purchases=user_context,
                session_data={**context, **generated},
            )
            generated["ai_prediction"] = prediction
            generated["ai_confidence"] = min(float(generated.get("ai_confidence") or 0), float(strategy.get("confidence") or 0))
            store_confidence = float(getattr(settings, "revenue_min_ai_confidence", self.confidence_threshold) or self.confidence_threshold)
            confidence_threshold = max(store_confidence, float(self.confidence_threshold))

            if prediction < self.prediction_threshold:
                return self._skip_decision(rule_decision, "low_prediction", prediction=prediction)
            if generated["ai_confidence"] < confidence_threshold:
                return self._mark_fallback(fallback_decision or rule_decision, "low_confidence", prediction=prediction)
            if generated.get("discount") and self._discount_stacking_risk(context):
                return self._mark_fallback(fallback_decision or rule_decision, "discount_stacking_risk", prediction=prediction)

            generated_revenue = self.expected_revenue(generated, context, prediction=prediction)
            fallback_probability = self._fallback_probability(offer_type, fallback_decision, prediction)
            fallback_revenue = self.expected_revenue(fallback_decision or rule_decision, context, prediction=fallback_probability)

            if generated_revenue >= fallback_revenue:
                generated["selection_reason"] = "ai_expected_revenue"
                generated["ai_expected_revenue"] = float(generated_revenue)
                generated["fallback_expected_revenue"] = float(fallback_revenue)
                return generated

            return self._mark_fallback(
                fallback_decision or rule_decision,
                "ab_expected_revenue",
                prediction=prediction,
                generated_revenue=generated_revenue,
                fallback_revenue=fallback_revenue,
            )
        except Exception as exc:
            logger.warning("AI revenue optimizer fallback offer_type=%s user=%s: %s", offer_type, getattr(user, "pk", None), exc)
            return self._mark_fallback(fallback_decision or rule_decision, "ai_failed")

    def optimization_fallback(self, offer_type, rule_decision, *, user_id=None):
        try:
            variants = self.experiment_engine.generate_variants(rule_decision, offer_type=offer_type)
            return self.offer_selector.select(offer_type, variants, user_id=user_id) or rule_decision
        except Exception as exc:
            logger.warning("A/B fallback failed offer_type=%s: %s", offer_type, exc)
            return self._mark_fallback(rule_decision, "rule_engine")

    def expected_revenue(self, decision, context, *, prediction):
        decision = decision or {}
        amount = self._base_amount(decision, context)
        discount = min(max(_decimal(decision.get("discount")), Decimal("0")), Decimal("90"))
        multiplier = _decimal(decision.get("expected_revenue_multiplier"), "1")
        net_amount = amount * (Decimal("1") - (discount / Decimal("100")))
        return max(net_amount, Decimal("0")) * _decimal(prediction) * multiplier

    def _base_amount(self, decision, context):
        pricing = context.get("pricing") or {}
        for key in ("total", "amount", "final_amount", "subtotal"):
            if pricing.get(key):
                return _decimal(pricing.get(key))
        upgrade_plan = decision.get("upgrade_plan")
        if getattr(upgrade_plan, "price", None):
            return _decimal(upgrade_plan.price)
        plan = context.get("selected_plan") or context.get("plan")
        quantity = _decimal(context.get("quantity") or 1, "1")
        if getattr(plan, "price", None):
            return _decimal(plan.price) * quantity
        order = context.get("order")
        if getattr(order, "amount", None):
            return _decimal(order.amount)
        return Decimal("0")

    def _fallback_probability(self, offer_type, decision, generated_prediction):
        variant = str((decision or {}).get("experiment_variant") or (decision or {}).get("variant") or "")
        stats = self.scoring_engine.cached_or_current_stats(offer_type)
        if variant in stats and stats[variant].get("impressions", 0) > 0:
            return stats[variant].get("conversion_rate", 0)
        return max(float(generated_prediction) * 0.85, 0.05)

    def _build_user_context(self, user, context):
        data = dict(context or {})
        usage_percent = data.get("usage_percent")
        if usage_percent in (None, "") and data.get("usage"):
            usage_percent = data.get("usage")
        data["usage_percent"] = usage_percent or 0

        customer = data.get("customer")
        bot_user = data.get("bot_user")
        if not customer and getattr(bot_user, "customer", None):
            customer = bot_user.customer
        if not customer and getattr(user, "customer", None):
            customer = user.customer
        order = getattr(user, "order", None)
        if not customer and getattr(order, "customer", None):
            customer = order.customer

        purchase_count = data.get("purchase_count")
        lifetime_value = data.get("lifetime_value")
        if customer is not None and getattr(customer, "pk", None):
            try:
                orders = customer.orders.filter(status="completed")
                purchase_count = purchase_count if purchase_count is not None else orders.count()
                lifetime_value = lifetime_value if lifetime_value is not None else sum(order.amount or 0 for order in orders)
            except Exception:
                purchase_count = purchase_count if purchase_count is not None else 0
                lifetime_value = lifetime_value if lifetime_value is not None else 0

        data["purchase_count"] = purchase_count or 0
        data["lifetime_value"] = lifetime_value or 0
        event_type = str(data.get("event_type") or "")
        data["subscription_expired"] = bool(data.get("subscription_expired") or "expired" in event_type.lower())
        data["churn_risk"] = bool(data.get("churn_risk") or "inactive_72h" in event_type or "expired" in event_type.lower())
        data["session_active"] = bool(data.get("session_active") or data.get("selected_plan") or data.get("plan"))
        if data.get("source") == "telegram_payment_screen":
            data["payment_screen_opened"] = True
        if "checkout" in event_type:
            data["checkout_started"] = True
        return data

    def _discount_stacking_risk(self, context):
        return bool(context.get("discount_active") or context.get("discount_code") or context.get("discount"))

    def _mark_fallback(self, decision, reason, *, prediction=None, generated_revenue=None, fallback_revenue=None):
        marked = dict(decision or {})
        marked.setdefault("selection_reason", marked.get("selection_reason") or "fallback")
        marked["ai_fallback_reason"] = reason
        if prediction is not None:
            marked["ai_prediction"] = prediction
        if generated_revenue is not None:
            marked["ai_expected_revenue"] = float(generated_revenue)
        if fallback_revenue is not None:
            marked["fallback_expected_revenue"] = float(fallback_revenue)
        return marked

    def _skip_decision(self, decision, reason, *, prediction=None):
        skipped = dict(decision or {})
        skipped["skip_offer"] = True
        skipped["skip_reason"] = reason
        skipped["ai_fallback_reason"] = reason
        if prediction is not None:
            skipped["ai_prediction"] = prediction
        return skipped
