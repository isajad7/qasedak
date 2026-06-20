from django.core.cache import cache

from store.models import BotEventLog

from .tracker import USER_RECEIVED_OFFER


SCORE_CACHE_PREFIX = "revenue_engine:optimization:scores"


class ScoringEngine:
    def __init__(self, cache_timeout=25 * 60 * 60):
        self.cache_timeout = cache_timeout

    def conversion_rates(self, offer_type, *, since=None):
        return {
            variant: stats["conversion_rate"]
            for variant, stats in self.variant_stats(offer_type, since=since).items()
        }

    def variant_stats(self, offer_type, *, since=None):
        qs = BotEventLog.objects.filter(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event=USER_RECEIVED_OFFER,
            raw_payload__offer_type=str(offer_type),
        )
        if since:
            qs = qs.filter(created_at__gte=since)

        stats = {}
        for log in qs.iterator():
            payload = log.raw_payload or {}
            variant = str(payload.get("variant") or "")
            if not variant:
                continue
            entry = stats.setdefault(variant, {"impressions": 0, "conversions": 0, "conversion_rate": 0.0})
            entry["impressions"] += 1
            if payload.get("converted"):
                entry["conversions"] += 1

        for entry in stats.values():
            impressions = entry["impressions"]
            entry["conversion_rate"] = (entry["conversions"] / impressions) if impressions else 0.0
        return stats

    def update_scores(self, offer_type, *, since=None):
        stats = self.variant_stats(offer_type, since=since)
        cache.set(self.cache_key(offer_type), stats, self.cache_timeout)
        return stats

    def cached_or_current_stats(self, offer_type):
        cached = cache.get(self.cache_key(offer_type))
        if cached is not None:
            return cached
        return self.update_scores(offer_type)

    def update_all_scores(self, offer_types=None, *, since=None):
        offer_types = list(offer_types or self.offer_types())
        return {offer_type: self.update_scores(offer_type, since=since) for offer_type in offer_types}

    def offer_types(self):
        seen = []
        logs = BotEventLog.objects.filter(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event=USER_RECEIVED_OFFER,
        ).only("raw_payload")
        for log in logs.iterator():
            offer_type = str((log.raw_payload or {}).get("offer_type") or "")
            if offer_type and offer_type not in seen:
                seen.append(offer_type)
        return seen

    def cache_key(self, offer_type):
        return f"{SCORE_CACHE_PREFIX}:{offer_type}"
