from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from store.models import BotEventLog, BotUser, Customer, Order, VPNClient


USER_RECEIVED_OFFER = "user_received_offer"
USER_CLICKED_OFFER = "user_clicked_offer"
USER_PURCHASED_AFTER_OFFER = "user_purchased_after_offer"

OFFER_EVENT_PREFIX = "offer_event:"
DEFAULT_ATTRIBUTION_WINDOW = timedelta(days=7)
DEFAULT_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class OfferEvent:
    user_id: str
    offer_type: str
    variant: str
    timestamp: object
    converted: bool = False


def resolve_offer_user_id(user=None, context=None, target=None):
    context = context or {}
    if target is not None:
        raw = getattr(target, "telegram_user_id", None) or getattr(target, "chat_id", None)
        if raw:
            return str(raw)

    bot_user = context.get("bot_user")
    if isinstance(bot_user, BotUser):
        return str(bot_user.provider_user_id or bot_user.chat_id or bot_user.pk)

    for key in ("user_id", "telegram_user_id", "chat_id"):
        if context.get(key):
            return str(context[key])

    if isinstance(user, BotUser):
        return str(user.provider_user_id or user.chat_id or user.pk)
    if isinstance(user, Order) and user.customer_id:
        return f"customer:{user.customer_id}"
    if isinstance(user, VPNClient):
        order = getattr(user, "order", None)
        if order and getattr(order, "customer_id", None):
            return f"customer:{order.customer_id}"
    if isinstance(user, Customer):
        return f"customer:{user.pk}"
    if getattr(user, "pk", None):
        return str(user.pk)
    return ""


def offer_type_from_decision(decision, default=""):
    decision = decision or {}
    return str(decision.get("optimization_offer_type") or default or decision.get("type") or "offer")


def variant_from_decision(decision):
    decision = decision or {}
    return str(decision.get("experiment_variant") or decision.get("variant") or "control")


def experiment_payload(decision):
    decision = decision or {}
    return {
        "offer_type": offer_type_from_decision(decision),
        "variant": variant_from_decision(decision),
        "experiment_id": decision.get("experiment_id", ""),
        "label": decision.get("experiment_label", ""),
        "ai_generated": bool(decision.get("ai_generated")),
        "ai_strategy": decision.get("ai_strategy", ""),
        "ai_confidence": decision.get("ai_confidence"),
        "ai_prediction": decision.get("ai_prediction"),
        "ai_fallback_reason": decision.get("ai_fallback_reason", ""),
    }


class OfferTracker:
    def __init__(self, attribution_window=DEFAULT_ATTRIBUTION_WINDOW):
        self.attribution_window = attribution_window

    def user_received_offer(self, user_id, offer_type, variant, *, bot_config=None, order=None, metadata=None):
        return self._record(
            USER_RECEIVED_OFFER,
            user_id,
            offer_type,
            variant,
            bot_config=bot_config,
            order=order,
            metadata=metadata,
            converted=False,
        )

    def user_clicked_offer(self, user_id, offer_type=None, variant=None, *, bot_config=None, order=None, metadata=None):
        if not offer_type or not variant:
            latest = self.latest_received_offer(user_id, offer_type=offer_type)
            if latest:
                offer_type = offer_type or latest.offer_type
                variant = variant or latest.variant
        return self._record(
            USER_CLICKED_OFFER,
            user_id,
            offer_type,
            variant,
            bot_config=bot_config,
            order=order,
            metadata=metadata,
            converted=False,
            event_type=BotEventLog.EventType.CALLBACK,
            status=BotEventLog.Status.RECEIVED,
        )

    def user_purchased_after_offer(self, user_id, *, offer_type=None, bot_config=None, order=None, metadata=None):
        latest_log = self.latest_received_offer_log(user_id, offer_type=offer_type)
        if not latest_log:
            return None

        payload = dict(latest_log.raw_payload or {})
        payload["converted"] = True
        payload["converted_at"] = timezone.now().isoformat()
        payload["conversion_order_id"] = getattr(order, "pk", None)
        latest_log.raw_payload = payload
        latest_log.save(update_fields=["raw_payload"])

        return self._record(
            USER_PURCHASED_AFTER_OFFER,
            user_id,
            payload.get("offer_type"),
            payload.get("variant"),
            bot_config=bot_config or latest_log.bot_config,
            order=order,
            metadata=metadata,
            converted=True,
            status=BotEventLog.Status.SUCCESS,
        )

    def latest_received_offer(self, user_id, offer_type=None):
        log = self.latest_received_offer_log(user_id, offer_type=offer_type)
        return self.event_from_log(log) if log else None

    def latest_received_offer_log(self, user_id, offer_type=None):
        if not user_id:
            return None
        qs = self.received_events(user_id=user_id, offer_type=offer_type)
        since = timezone.now() - self.attribution_window
        return qs.filter(created_at__gte=since).order_by("-created_at", "-pk").first()

    def received_events(self, *, user_id=None, offer_type=None, since=None):
        qs = BotEventLog.objects.filter(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event=USER_RECEIVED_OFFER,
        )
        if user_id:
            qs = qs.filter(raw_payload__user_id=str(user_id))
        if offer_type:
            qs = qs.filter(raw_payload__offer_type=str(offer_type))
        if since:
            qs = qs.filter(created_at__gte=since)
        return qs

    def recent_variants_for_user(self, user_id, offer_type, *, since=None):
        if not user_id:
            return []
        since = since or timezone.now() - self.attribution_window
        variants = []
        for log in self.received_events(user_id=user_id, offer_type=offer_type, since=since).order_by("-created_at", "-pk"):
            variant = (log.raw_payload or {}).get("variant")
            if variant and variant not in variants:
                variants.append(str(variant))
        return variants

    def user_in_cooldown(self, user_id, offer_type, *, cooldown=DEFAULT_COOLDOWN):
        if not user_id or not offer_type:
            return False
        since = timezone.now() - cooldown
        return self.received_events(user_id=user_id, offer_type=offer_type, since=since).exists()

    def event_from_log(self, log):
        if not log:
            return None
        payload = log.raw_payload or {}
        return OfferEvent(
            user_id=str(payload.get("user_id") or ""),
            offer_type=str(payload.get("offer_type") or ""),
            variant=str(payload.get("variant") or ""),
            timestamp=log.created_at,
            converted=bool(payload.get("converted")),
        )

    def _record(
        self,
        event_name,
        user_id,
        offer_type,
        variant,
        *,
        bot_config=None,
        order=None,
        metadata=None,
        converted=False,
        event_type=None,
        status=None,
    ):
        user_id = str(user_id or "").strip()
        offer_type = str(offer_type or "").strip()
        variant = str(variant or "").strip()
        if not user_id or not offer_type or not variant:
            return None

        log = BotEventLog.objects.create(
            bot_config=bot_config,
            order=order,
            event_type=event_type or BotEventLog.EventType.WEBHOOK,
            status=status or BotEventLog.Status.SENT,
            message=f"{OFFER_EVENT_PREFIX}{event_name}",
            raw_payload={
                "revenue_optimization": True,
                "offer_event": event_name,
                "user_id": user_id,
                "offer_type": offer_type,
                "variant": variant,
                "timestamp": timezone.now().isoformat(),
                "converted": bool(converted),
                "metadata": metadata or {},
            },
        )
        return self.event_from_log(log)
