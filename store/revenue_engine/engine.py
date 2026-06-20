import logging

from .actions import execute
from .ai.optimizer import AIRevenueOptimizer
from .guards import mark_latest_revenue_offer_converted, resolve_revenue_context
from .optimization.experiment import ExperimentEngine
from .optimization.selector import OfferSelector
from .optimization.tracker import OfferTracker, resolve_offer_user_id
from .rules import RuleEngine


logger = logging.getLogger(__name__)


class RevenueEngine:
    offer_type = "renewal"

    def __init__(self, rule_engine=None, experiment_engine=None, offer_selector=None, tracker=None, ai_optimizer=None):
        self.rule_engine = rule_engine or RuleEngine()
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
        if event_type == "USER_PURCHASE":
            return self._track_purchase(user, context)

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
                "AI revenue optimization fallback offer_type=%s user=%s: %s",
                self.offer_type,
                getattr(user, "pk", None),
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
                "Revenue optimization fallback offer_type=%s user=%s: %s",
                self.offer_type,
                getattr(user, "pk", None),
                exc,
            )
            return decision

    def _track_purchase(self, user, context):
        try:
            resolved = resolve_revenue_context(user, context)
            revenue_log = mark_latest_revenue_offer_converted(
                resolved["customer"],
                bot_user=resolved["bot_user"],
                order=user if getattr(user, "_meta", None) and user._meta.model_name == "order" else context.get("order"),
                store=resolved["store"],
                metadata={
                    "event_type": context.get("event_type"),
                    "flow": context.get("flow", ""),
                    "source": context.get("source", ""),
                },
            )
            event = self.tracker.user_purchased_after_offer(
                resolve_offer_user_id(user, context),
                bot_config=context.get("bot_config"),
                order=user if getattr(user, "_meta", None) and user._meta.model_name == "order" else context.get("order"),
                metadata={
                    "event_type": context.get("event_type"),
                    "flow": context.get("flow", ""),
                    "source": context.get("source", ""),
                },
            )
            return {
                "handled": bool(event or revenue_log),
                "decision": None,
                "action": {
                    "tracked": bool(event or revenue_log),
                    "revenue_offer_log_id": getattr(revenue_log, "pk", None),
                },
            }
        except Exception as exc:
            logger.warning("Offer conversion tracking skipped user=%s: %s", getattr(user, "pk", None), exc)
            return {"handled": False, "decision": None, "action": {"tracked": False, "failed": True, "reason": str(exc)}}
