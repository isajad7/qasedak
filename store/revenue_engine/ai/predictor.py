from decimal import Decimal, InvalidOperation

from django.core.cache import cache

from store.models import BotEventLog
from store.revenue_engine.optimization.tracker import USER_RECEIVED_OFFER


PREDICTION_ACCURACY_CACHE_KEY = "revenue_engine:ai:prediction_accuracy"
PREDICTION_ACCURACY_TIMEOUT = 25 * 60 * 60


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


class PurchasePredictor:
    def __init__(self, calibration=None):
        self.calibration = calibration if calibration is not None else self.current_accuracy().get("calibration", 0.0)

    def predict(self, user_behavior=None, past_purchases=None, session_data=None):
        behavior = user_behavior or {}
        purchases = past_purchases or {}
        session = session_data or {}

        probability = Decimal("0.20")
        purchase_count = _int(purchases.get("purchase_count", behavior.get("purchase_count", 0)))
        usage_percent = _decimal(behavior.get("usage_percent", session.get("usage_percent", 0)))

        if purchase_count >= 3:
            probability += Decimal("0.24")
        elif purchase_count >= 1:
            probability += Decimal("0.12")

        if usage_percent >= Decimal("80"):
            probability += Decimal("0.18")
        elif behavior.get("subscription_active") and usage_percent < Decimal("10"):
            probability -= Decimal("0.04")

        if session.get("checkout_started") or session.get("payment_screen_opened") or session.get("selected_plan"):
            probability += Decimal("0.14")
        if session.get("churn_risk") or behavior.get("subscription_expired"):
            probability += Decimal("0.08")
        if session.get("discount") or session.get("bonus_gb"):
            probability += Decimal("0.06")

        probability += Decimal(str(self.calibration or 0))
        probability = min(max(probability, Decimal("0")), Decimal("1"))
        return float(probability)

    def update_accuracy(self):
        samples = []
        logs = BotEventLog.objects.filter(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event=USER_RECEIVED_OFFER,
            raw_payload__metadata__ai_prediction__isnull=False,
        ).only("raw_payload")

        for log in logs.iterator():
            payload = log.raw_payload or {}
            metadata = payload.get("metadata") or {}
            try:
                predicted = float(metadata.get("ai_prediction"))
            except (TypeError, ValueError):
                continue
            actual = 1.0 if payload.get("converted") else 0.0
            samples.append((predicted, actual))

        if not samples:
            result = {
                "samples": 0,
                "average_prediction": 0.0,
                "conversion_rate": 0.0,
                "calibration_error": 0.0,
                "accuracy": 1.0,
                "calibration": 0.0,
            }
            cache.set(PREDICTION_ACCURACY_CACHE_KEY, result, PREDICTION_ACCURACY_TIMEOUT)
            return result

        average_prediction = sum(predicted for predicted, _actual in samples) / len(samples)
        conversion_rate = sum(actual for _predicted, actual in samples) / len(samples)
        calibration_error = abs(average_prediction - conversion_rate)
        calibration = max(min(conversion_rate - average_prediction, 0.1), -0.1)
        result = {
            "samples": len(samples),
            "average_prediction": average_prediction,
            "conversion_rate": conversion_rate,
            "calibration_error": calibration_error,
            "accuracy": max(0.0, 1.0 - calibration_error),
            "calibration": calibration,
        }
        cache.set(PREDICTION_ACCURACY_CACHE_KEY, result, PREDICTION_ACCURACY_TIMEOUT)
        self.calibration = calibration
        return result

    def current_accuracy(self):
        return cache.get(PREDICTION_ACCURACY_CACHE_KEY) or {
            "samples": 0,
            "average_prediction": 0.0,
            "conversion_rate": 0.0,
            "calibration_error": 0.0,
            "accuracy": 1.0,
            "calibration": 0.0,
        }
