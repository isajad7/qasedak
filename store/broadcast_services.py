import time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .customer_analytics import (
    SEGMENT_ACTIVE_CONFIG,
    SEGMENT_ACTIVE_CUSTOMERS,
    SEGMENT_ALL,
    SEGMENT_CUSTOMERS_WITHOUT_ORDER,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_LOYAL,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_BUYER,
    SEGMENT_TOP_REFERRER,
    get_customers_by_segment,
)
from .models import BotUser, BroadcastMessage, BroadcastRecipient, Customer, Store


AUDIENCE_SEGMENT_MAP = {
    BroadcastMessage.AudienceType.ALL: SEGMENT_ALL,
    BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS: SEGMENT_ACTIVE_CUSTOMERS,
    BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG: SEGMENT_ACTIVE_CONFIG,
    BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER: SEGMENT_CUSTOMERS_WITHOUT_ORDER,
    BroadcastMessage.AudienceType.LOYAL: SEGMENT_LOYAL,
    BroadcastMessage.AudienceType.GOOD: SEGMENT_GOOD,
    BroadcastMessage.AudienceType.TOP_BUYER: SEGMENT_TOP_BUYER,
    BroadcastMessage.AudienceType.TOP_REFERRER: SEGMENT_TOP_REFERRER,
    BroadcastMessage.AudienceType.INACTIVE: SEGMENT_INACTIVE,
    BroadcastMessage.AudienceType.NO_ORDER: SEGMENT_NO_ORDER,
}

DELIVERY_CHANNELS = (
    BroadcastMessage.Channel.TELEGRAM,
    BroadcastMessage.Channel.BALE,
)


def get_default_broadcast_store():
    return Store.objects.filter(is_active=True).order_by("id").first() or Store.objects.order_by("id").first()


def get_campaign_store(campaign):
    return getattr(campaign, "store", None) or get_default_broadcast_store()


def _positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def get_campaign_recipient_limit(campaign):
    store = get_campaign_store(campaign)
    return _positive_int(getattr(store, "broadcast_max_recipients_per_campaign", None), 1000)


def _apply_store_scope(queryset, store):
    if not store:
        return queryset
    return queryset.filter(
        Q(bot_users__bot_config__store=store)
        | Q(orders__store=store)
        | Q(orders__vpn_clients__store=store)
    ).distinct()


def get_customers_for_audience(audience_type, *, store=None, limit=None):
    audience_type = audience_type or BroadcastMessage.AudienceType.ALL
    segment = AUDIENCE_SEGMENT_MAP.get(audience_type)
    if not segment:
        raise ValueError(f"Unsupported broadcast audience: {audience_type}")

    queryset = get_customers_by_segment(segment)
    if store and getattr(queryset.query, "is_sliced", False):
        ordered_ids = list(queryset.values_list("pk", flat=True))
        queryset = Customer.objects.filter(pk__in=ordered_ids).order_by("display_name", "pk")
    queryset = _apply_store_scope(queryset, store)
    if limit is not None:
        queryset = queryset[: _positive_int(limit, 1000)]
    return queryset


def _campaign_channels(campaign):
    if campaign.channel == BroadcastMessage.Channel.ALL_AVAILABLE:
        return DELIVERY_CHANNELS
    if campaign.channel in DELIVERY_CHANNELS:
        return (campaign.channel,)
    raise ValueError(f"Unsupported broadcast channel: {campaign.channel}")


def _bot_user_queryset(*, customer, channel, store=None, bot_config=None):
    queryset = (
        BotUser.objects.select_related("bot_config")
        .filter(
            customer=customer,
            is_active=True,
            bot_config__is_active=True,
            bot_config__provider=channel,
        )
        .exclude(chat_id="")
        .exclude(bot_config__bot_token="")
    )
    if bot_config:
        if bot_config.provider != channel:
            return queryset.none()
        return queryset.filter(bot_config=bot_config)
    if store:
        queryset = queryset.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))
    return queryset.order_by("-last_seen_at", "-updated_at", "pk")


def _resolve_bot_user(customer, channel, *, store=None, bot_config=None, target_identifier=""):
    queryset = _bot_user_queryset(customer=customer, channel=channel, store=store, bot_config=bot_config)
    if target_identifier:
        queryset = queryset.filter(chat_id=str(target_identifier))
    return queryset.first()


def resolve_campaign_recipients(campaign, *, bot_config=None):
    store = getattr(campaign, "store", None) or getattr(bot_config, "store", None)
    limit = get_campaign_recipient_limit(campaign)
    customers = get_customers_for_audience(campaign.audience_type, store=store, limit=limit)
    channels = _campaign_channels(campaign)
    recipients = []

    for customer in customers:
        deliverable_rows = []
        for channel in channels:
            bot_user = _resolve_bot_user(customer, channel, store=store, bot_config=bot_config)
            if not bot_user:
                continue
            deliverable_rows.append(
                {
                    "customer": customer,
                    "channel": channel,
                    "target_identifier": bot_user.chat_id,
                    "status": BroadcastRecipient.Status.PENDING,
                    "error_message": "",
                }
            )

        if deliverable_rows:
            recipients.extend(deliverable_rows)
            continue

        recipients.append(
            {
                "customer": customer,
                "channel": campaign.channel,
                "target_identifier": "",
                "status": BroadcastRecipient.Status.SKIPPED,
                "error_message": "No active bot user target was found for this channel.",
            }
        )
    return recipients


def refresh_campaign_counts(campaign):
    recipients = BroadcastRecipient.objects.filter(campaign=campaign)
    campaign.total_recipients = recipients.count()
    campaign.success_count = recipients.filter(status=BroadcastRecipient.Status.SENT).count()
    campaign.failed_count = recipients.filter(status=BroadcastRecipient.Status.FAILED).count()
    campaign.save(update_fields=["total_recipients", "success_count", "failed_count", "updated_at"])
    return {
        "total": campaign.total_recipients,
        "success": campaign.success_count,
        "failed": campaign.failed_count,
        "skipped": recipients.filter(status=BroadcastRecipient.Status.SKIPPED).count(),
    }


@transaction.atomic
def create_campaign_recipients(campaign, *, bot_config=None):
    resolved_rows = resolve_campaign_recipients(campaign, bot_config=bot_config)
    BroadcastRecipient.objects.filter(
        campaign=campaign,
        status__in=(BroadcastRecipient.Status.PENDING, BroadcastRecipient.Status.SKIPPED),
    ).delete()
    for row in resolved_rows:
        recipient, created = BroadcastRecipient.objects.get_or_create(
            campaign=campaign,
            customer=row["customer"],
            channel=row["channel"],
            defaults={
                "target_identifier": row["target_identifier"],
                "status": row["status"],
                "error_message": row["error_message"],
            },
        )
        if created or recipient.status not in {
            BroadcastRecipient.Status.PENDING,
            BroadcastRecipient.Status.SKIPPED,
        }:
            continue
        changed_fields = []
        for field in ("target_identifier", "status", "error_message"):
            value = row[field]
            if getattr(recipient, field) != value:
                setattr(recipient, field, value)
                changed_fields.append(field)
        if changed_fields:
            recipient.save(update_fields=[*changed_fields, "updated_at"])
    return refresh_campaign_counts(campaign)


def normalize_delivery_error(error):
    message = str(error or "").strip()
    lowered = message.lower()
    if "chat not found" in lowered:
        return "chat not found"
    if "blocked" in lowered:
        return "bot blocked"
    if "forbidden" in lowered:
        return "forbidden"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return message[:1000] or "Unknown delivery error."


def _mark_recipient(recipient, *, status, error_message="", sent_at=None):
    recipient.status = status
    recipient.error_message = error_message
    recipient.sent_at = sent_at
    recipient.save(update_fields=["status", "error_message", "sent_at", "updated_at"])
    return recipient


def send_message_to_customer(campaign, recipient, *, bot_config=None):
    message_text = (campaign.message_text or "").strip()
    if not message_text:
        raise ValidationError("Message text is required.")

    if recipient.status == BroadcastRecipient.Status.SKIPPED and not recipient.target_identifier:
        return recipient

    if not recipient.target_identifier:
        return _mark_recipient(
            recipient,
            status=BroadcastRecipient.Status.SKIPPED,
            error_message="No target identifier was resolved for this recipient.",
        )

    store = getattr(campaign, "store", None) or getattr(bot_config, "store", None)
    bot_user = _resolve_bot_user(
        recipient.customer,
        recipient.channel,
        store=store,
        bot_config=bot_config,
        target_identifier=recipient.target_identifier,
    )
    if not bot_user:
        return _mark_recipient(
            recipient,
            status=BroadcastRecipient.Status.SKIPPED,
            error_message="Bot user target is no longer active.",
        )

    from .bots import BotClient, BotDeliveryError

    try:
        BotClient(bot_user.bot_config).send_message(
            message_text,
            chat_id=recipient.target_identifier,
            parse_mode=None,
        )
    except BotDeliveryError as exc:
        return _mark_recipient(
            recipient,
            status=BroadcastRecipient.Status.FAILED,
            error_message=normalize_delivery_error(exc),
        )

    return _mark_recipient(
        recipient,
        status=BroadcastRecipient.Status.SENT,
        error_message="",
        sent_at=timezone.now(),
    )


def _store_allows_broadcast(campaign, *, bot_config=None):
    store = getattr(campaign, "store", None) or getattr(bot_config, "store", None) or get_default_broadcast_store()
    return bool(getattr(store, "broadcast_enabled", True)), store


def send_campaign(campaign, *, bot_config=None):
    if campaign.status == BroadcastMessage.Status.CANCELLED:
        return refresh_campaign_counts(campaign)

    if not (campaign.message_text or "").strip():
        campaign.status = BroadcastMessage.Status.FAILED
        campaign.metadata = {**(campaign.metadata or {}), "error": "Message text is required."}
        campaign.save(update_fields=["status", "metadata", "updated_at"])
        raise ValidationError("Message text is required.")

    enabled, store = _store_allows_broadcast(campaign, bot_config=bot_config)
    if not enabled:
        campaign.status = BroadcastMessage.Status.FAILED
        campaign.metadata = {**(campaign.metadata or {}), "error": "Broadcast is disabled for this store."}
        campaign.save(update_fields=["status", "metadata", "updated_at"])
        return refresh_campaign_counts(campaign)

    campaign.status = BroadcastMessage.Status.SENDING
    campaign.save(update_fields=["status", "updated_at"])
    create_campaign_recipients(campaign, bot_config=bot_config)

    rate_limit = _positive_int(getattr(store, "broadcast_rate_limit_per_second", None), 5)
    delay = 1 / rate_limit if rate_limit else 0
    pending_recipients = BroadcastRecipient.objects.select_related("customer").filter(
        campaign=campaign,
        status=BroadcastRecipient.Status.PENDING,
    ).order_by("created_at", "pk")

    for recipient in pending_recipients:
        recipient.refresh_from_db()
        if campaign.status == BroadcastMessage.Status.CANCELLED:
            break
        send_message_to_customer(campaign, recipient, bot_config=bot_config)
        if delay:
            time.sleep(delay)

    counts = refresh_campaign_counts(campaign)
    campaign.status = (
        BroadcastMessage.Status.CANCELLED
        if campaign.status == BroadcastMessage.Status.CANCELLED
        else BroadcastMessage.Status.SENT
    )
    campaign.sent_at = timezone.now()
    campaign.save(update_fields=["status", "sent_at", "updated_at"])
    counts["status"] = campaign.status
    return counts
