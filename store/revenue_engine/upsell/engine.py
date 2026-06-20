import logging

from .actions import execute
from .rules import UpsellRuleEngine
from store.revenue_engine.ai.optimizer import AIRevenueOptimizer
from store.revenue_engine.optimization.experiment import ExperimentEngine
from store.revenue_engine.optimization.selector import OfferSelector
from store.revenue_engine.optimization.tracker import OfferTracker, resolve_offer_user_id


logger = logging.getLogger(__name__)


class UpsellEngine:
    offer_type = "upsell"

    def __init__(self, rule_engine=None, experiment_engine=None, offer_selector=None, tracker=None, ai_optimizer=None):
        self.rule_engine = rule_engine or UpsellRuleEngine()
        self.experiment_engine = experiment_engine or ExperimentEngine()
        self.offer_selector = offer_selector or OfferSelector()
        self.tracker = tracker or OfferTracker()
        self.ai_optimizer = ai_optimizer or AIRevenueOptimizer(
            experiment_engine=self.experiment_engine,
            offer_selector=self.offer_selector,
            tracker=self.tracker,
        )

    def handle(self, event_type, user, context=None):
        context = dict(context or {})
        context.setdefault("event_type", event_type)
        decision = self.rule_engine.evaluate(event_type, user, context)
        if not decision:
            return {"handled": False, "decision": None, "action": None}
        decision = self._optimize_offer(decision, user, context)
        if decision.get("skip_offer"):
            return {
                "handled": True,
                "decision": decision,
                "action": {"sent": False, "skipped": True, "reason": decision.get("skip_reason", "skip_offer")},
            }
        action_result = execute(user, decision, context)
        return {"handled": True, "decision": decision, "action": action_result}

    def _optimize_offer(self, decision, user, context):
        try:
            return self.ai_optimizer.optimize(self.offer_type, decision, user=user, context=context) or decision
        except Exception as exc:
            logger.warning(
                "AI upsell optimization fallback user=%s event=%s: %s",
                getattr(user, "pk", None),
                context.get("event_type"),
                exc,
            )
            return self._select_variant(decision, user, context)

    def _select_variant(self, decision, user, context):
        try:
            variants = self.experiment_engine.generate_variants(decision, offer_type=self.offer_type)
            user_id = resolve_offer_user_id(user, context)
            return self.offer_selector.select(self.offer_type, variants, user_id=user_id) or decision
        except Exception as exc:
            logger.warning(
                "Upsell optimization fallback user=%s event=%s: %s",
                getattr(user, "pk", None),
                context.get("event_type"),
                exc,
            )
            return decision
