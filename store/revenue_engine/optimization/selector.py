import random

from .scoring import ScoringEngine
from .tracker import OfferTracker


class OfferSelector:
    def __init__(self, scoring_engine=None, tracker=None, *, min_impressions=10, randomizer=None):
        self.scoring_engine = scoring_engine or ScoringEngine()
        self.tracker = tracker or OfferTracker()
        self.min_impressions = min_impressions
        self.randomizer = randomizer or random.SystemRandom()

    def select(self, offer_type, variants, *, user_id=None):
        normalized = [dict(variant) for variant in variants if variant]
        if not normalized:
            return None

        stats = self.scoring_engine.cached_or_current_stats(offer_type)
        enough_data = sum(entry.get("impressions", 0) for entry in stats.values()) >= self.min_impressions
        if enough_data:
            selected = self._best_performing_variant(normalized, stats)
            if selected:
                selected["selection_reason"] = "best_performing"
                return selected

        selected = self._safe_random_variant(offer_type, normalized, user_id=user_id)
        selected["selection_reason"] = "safe_random"
        return selected

    def _best_performing_variant(self, variants, stats):
        by_name = {str(variant.get("experiment_variant") or variant.get("variant") or ""): variant for variant in variants}
        ranked = []
        for variant_name, entry in stats.items():
            if variant_name in by_name and entry.get("impressions", 0) > 0:
                ranked.append(
                    (
                        entry.get("conversion_rate", 0.0),
                        entry.get("conversions", 0),
                        entry.get("impressions", 0),
                        variant_name,
                    )
                )
        if not ranked:
            return None
        ranked.sort(reverse=True)
        return dict(by_name[ranked[0][3]])

    def _safe_random_variant(self, offer_type, variants, *, user_id=None):
        candidates = list(variants)
        recent_variants = self.tracker.recent_variants_for_user(user_id, offer_type) if user_id else []
        if len(candidates) > 1 and recent_variants:
            filtered = [
                variant
                for variant in candidates
                if str(variant.get("experiment_variant") or variant.get("variant") or "") not in recent_variants
            ]
            if filtered:
                candidates = filtered
        return dict(self.randomizer.choice(candidates))
