import csv
import io
import re
from dataclasses import dataclass
from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from .admin_support_services import mask_identifier, sanitize_support_message
from .broadcast_services import (
    DELIVERY_CHANNELS,
    classify_delivery_error,
    create_campaign_recipients,
    get_campaign_recipient_limit,
    get_customers_for_audience,
    is_retryable_delivery_error,
    refresh_campaign_counts,
    resolve_campaign_recipients,
    safe_delivery_error,
)
from .models import BotConfiguration, BotUser, BroadcastMessage, BroadcastRecipient, Customer, Store
from .setup_readiness import SETUP_NOT_READY_MESSAGE, store_is_sellable
from .telegram_bot.admin_broadcast import BROADCAST_AUDIENCE_LABELS, BROADCAST_CHANNEL_LABELS


CAMPAIGN_WORKBENCH_LIMIT = 12
RECENT_RECIPIENT_LIMIT = 50
TELEGRAM_MESSAGE_LIMIT = 4096
LARGE_AUDIENCE_WARNING_THRESHOLD = 1000
QUEUE_CONFIRMATION_PREFIX = "SEND_CAMPAIGN"
MESSAGE_TEMPLATE_BODIES = [
    {
        "key": "general",
        "label": "اطلاع‌رسانی عمومی",
        "body": "سلام، یک اطلاع‌رسانی کوتاه از طرف پشتیبانی برای شما داریم:",
    },
    {
        "key": "discount",
        "label": "تخفیف",
        "body": "سلام، برای مدت محدود می‌توانید از تخفیف ویژه فروشگاه استفاده کنید.",
    },
    {
        "key": "system_update",
        "label": "بروزرسانی سیستم",
        "body": "سلام، سرویس‌ها در حال بروزرسانی هستند. اگر اختلال کوتاهی دیدید لطفاً چند دقیقه بعد دوباره تلاش کنید.",
    },
    {
        "key": "renewal",
        "label": "یادآوری تمدید",
        "body": "سلام، اگر سرویس شما رو به پایان است، می‌توانید از بخش سرویس‌های من برای تمدید اقدام کنید.",
    },
    {
        "key": "winback",
        "label": "بازگشت کاربران قدیمی",
        "body": "سلام، خوشحال می‌شویم دوباره کنارمان باشید. برای راهنمایی خرید یا تمدید همینجا پیام بدهید.",
    },
    {
        "key": "incident",
        "label": "اطلاع اختلال/رفع اختلال",
        "body": "سلام، اختلال گزارش‌شده بررسی شد. در صورت ادامه مشکل، نام برنامه و متن خطا را برای پشتیبانی بفرستید.",
    },
]

HTML_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
URL_WITH_CREDENTIALS_PATTERN = re.compile(r"\bhttps?://[^/\s:@]+:[^@\s]+@[^\s]+", re.IGNORECASE)


@dataclass(frozen=True)
class AudiencePreview:
    audience_label: str
    channel_label: str
    customers_matched: int
    bot_users_matched: int
    targetable: int
    missing_target: int
    inactive_bot_users: int
    duplicates_removed: int
    recently_messaged: int
    estimated_recipients: int
    skipped: int
    sample_customers: list[dict]
    warnings: list[str]
    bot_config_label: str


def add_query(url, params=None):
    query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    if not query:
        return url
    return f"{url}?{urlencode(query)}"


def campaign_workbench_url(store=None):
    return add_query(reverse("admin_store_campaign_workbench"), {"store": getattr(store, "pk", None)})


def campaign_new_url(store=None):
    return add_query(reverse("admin_store_campaign_new"), {"store": getattr(store, "pk", None)})


def campaign_review_url(campaign):
    return reverse("admin_store_campaign_review", args=[campaign.pk])


def campaign_edit_url(campaign):
    return reverse("admin_store_campaign_edit", args=[campaign.pk])


def campaign_audience_url(campaign):
    return reverse("admin_store_campaign_audience", args=[campaign.pk])


def campaign_preview_url(campaign):
    return reverse("admin_store_campaign_preview", args=[campaign.pk])


def campaign_confirm_url(campaign):
    return reverse("admin_store_campaign_confirm", args=[campaign.pk])


def campaign_export_url(campaign):
    return reverse("admin_store_campaign_export", args=[campaign.pk])


def selected_store_from_id(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def audience_choices():
    return [
        (value, BROADCAST_AUDIENCE_LABELS.get(value, label))
        for value, label in BroadcastMessage.AudienceType.choices
    ]


def channel_choices():
    return [
        (value, BROADCAST_CHANNEL_LABELS.get(value, label))
        for value, label in BroadcastMessage.Channel.choices
    ]


def audience_descriptions():
    descriptions = {
        BroadcastMessage.AudienceType.ALL: "همه مشتریان فعال قابل شناسایی در دیتابیس.",
        BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS: "مشتریانی که خرید موفق دارند.",
        BroadcastMessage.AudienceType.CUSTOMERS_WITH_ACTIVE_CONFIG: "مشتریانی که سرویس فعال دارند.",
        BroadcastMessage.AudienceType.CUSTOMERS_WITHOUT_ORDER: "مشتریان ثبت‌شده بدون سفارش موفق.",
        BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED: "مشتریان واردشده از WizWiz که import آن‌ها applied شده است.",
        BroadcastMessage.AudienceType.LOYAL: "بخش وفادار بر اساس customer analytics.",
        BroadcastMessage.AudienceType.GOOD: "بخش خوب بر اساس customer analytics.",
        BroadcastMessage.AudienceType.TOP_BUYER: "خریداران برتر بر اساس customer analytics.",
        BroadcastMessage.AudienceType.TOP_REFERRER: "معرف‌های برتر بر اساس customer analytics.",
        BroadcastMessage.AudienceType.INACTIVE: "مشتریانی که مدتی خرید یا فعالیت اخیر نداشته‌اند.",
        BroadcastMessage.AudienceType.NO_ORDER: "مشتریان بدون خرید موفق.",
    }
    return [
        {
            "value": value,
            "label": BROADCAST_AUDIENCE_LABELS.get(value, label),
            "description": descriptions.get(value, ""),
        }
        for value, label in BroadcastMessage.AudienceType.choices
    ]


def campaign_channels(campaign):
    if campaign.channel == BroadcastMessage.Channel.ALL_AVAILABLE:
        return DELIVERY_CHANNELS
    if campaign.channel in DELIVERY_CHANNELS:
        return (campaign.channel,)
    return ()


def clean_campaign_text(value):
    text = str(value or "").strip()
    if not text:
        raise ValidationError("متن پیام نمی‌تواند خالی باشد.")
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        raise ValidationError(f"متن پیام نباید بیشتر از {TELEGRAM_MESSAGE_LIMIT} کاراکتر باشد.")
    if HTML_TAG_PATTERN.search(text):
        raise ValidationError("HTML خام در پیام کمپین مجاز نیست.")
    if URL_WITH_CREDENTIALS_PATTERN.search(text):
        raise ValidationError("لینک دارای credential در پیام کمپین مجاز نیست.")
    sanitized = sanitize_support_message(text)
    if sanitized != text:
        raise ValidationError("متن پیام شامل token، لینک کانفیگ، UUID، شماره تماس یا شناسه حساس است.")
    return text


def safe_campaign_snippet(value, limit=140):
    return sanitize_support_message(value, limit=limit)


class CampaignMessageForm(forms.ModelForm):
    admin_note = forms.CharField(
        label="یادداشت داخلی",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 500}),
        help_text="فقط برای owner/admin؛ در پیام ارسالی استفاده نمی‌شود.",
    )

    class Meta:
        model = BroadcastMessage
        fields = ("store", "title", "message_text", "scheduled_at")
        widgets = {
            "message_text": forms.Textarea(attrs={"rows": 9, "maxlength": TELEGRAM_MESSAGE_LIMIT}),
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
        labels = {
            "store": "فروشگاه",
            "title": "عنوان داخلی کمپین",
            "message_text": "متن پیام",
            "scheduled_at": "زمان‌بندی اختیاری",
        }

    def __init__(self, *args, selected_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.order_by("-is_active", "name", "pk")
        self.fields["store"].required = False
        self.fields["title"].widget.attrs.update({"maxlength": 180})
        self.fields["title"].help_text = "این عنوان برای مدیریت داخلی است و برای مشتری ارسال نمی‌شود."
        self.fields["message_text"].help_text = "ارسال واقعی فقط بعد از مرحله تایید جداگانه انجام می‌شود."
        self.fields["scheduled_at"].required = False
        self.fields["scheduled_at"].help_text = "اگر خالی بماند، کمپین بعد از queue شدن آماده پردازش است."
        if selected_store and not self.instance.pk:
            self.initial.setdefault("store", selected_store.pk)
        metadata = getattr(self.instance, "metadata", None) or {}
        self.fields["admin_note"].initial = metadata.get("admin_note", "")

    def clean_title(self):
        title = str(self.cleaned_data.get("title") or "").strip()
        if not title:
            raise ValidationError("عنوان داخلی کمپین الزامی است.")
        if sanitize_support_message(title) != title:
            raise ValidationError("عنوان کمپین شامل داده حساس است.")
        return title

    def clean_message_text(self):
        return clean_campaign_text(self.cleaned_data.get("message_text"))

    def clean_admin_note(self):
        note = str(self.cleaned_data.get("admin_note") or "").strip()
        return sanitize_support_message(note, limit=500)

    def save(self, commit=True):
        campaign = super().save(commit=False)
        campaign.status = campaign.status or BroadcastMessage.Status.DRAFT
        metadata = dict(campaign.metadata or {})
        note = self.cleaned_data.get("admin_note", "")
        if note:
            metadata["admin_note"] = note
        else:
            metadata.pop("admin_note", None)
        metadata.setdefault("source", "admin_campaign_workbench")
        campaign.metadata = metadata
        if commit:
            campaign.save()
        return campaign


class CampaignAudienceForm(forms.ModelForm):
    class Meta:
        model = BroadcastMessage
        fields = ("audience_type", "channel")
        labels = {
            "audience_type": "مخاطبان",
            "channel": "کانال ارسال",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audience_type"].choices = audience_choices()
        self.fields["channel"].choices = channel_choices()
        self.fields["channel"].help_text = "ارسال واقعی از BotUserهای موجود همان کانال انجام می‌شود."

    def clean_audience_type(self):
        value = self.cleaned_data.get("audience_type")
        if value not in BroadcastMessage.AudienceType.values:
            raise ValidationError("Audience معتبر نیست.")
        return value

    def clean_channel(self):
        value = self.cleaned_data.get("channel")
        if value not in BroadcastMessage.Channel.values:
            raise ValidationError("کانال ارسال معتبر نیست.")
        return value


def active_bot_configurations(store=None, channels=None):
    queryset = BotConfiguration.objects.filter(is_active=True).exclude(bot_token="")
    if channels:
        queryset = queryset.filter(provider__in=channels)
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.order_by("provider", "-store_id", "name", "pk")


def bot_configuration_label(store=None, channels=None):
    configs = list(active_bot_configurations(store, channels)[:3])
    if not configs:
        return "BotConfiguration فعال با token ثبت نشده است."
    labels = []
    for config in configs:
        provider = BROADCAST_CHANNEL_LABELS.get(config.provider, config.get_provider_display())
        store_label = config.store.name if config.store_id else "عمومی"
        labels.append(f"{provider}: {config.name} ({store_label})")
    return "، ".join(labels)


def _customer_sample(customer_ids):
    customers = Customer.objects.filter(pk__in=customer_ids[:8]).order_by("pk")
    return [
        {
            "pk": customer.pk,
            "label": f"Customer #{customer.pk}",
        }
        for customer in customers
    ]


def get_audience_preview(campaign):
    store = campaign.store
    limit = get_campaign_recipient_limit(campaign)
    customers = list(get_customers_for_audience(campaign.audience_type, store=store, limit=limit).values_list("pk", flat=True))
    customer_count = len(customers)
    channels = campaign_channels(campaign)
    rows = resolve_campaign_recipients(campaign)
    targetable = sum(1 for row in rows if row["status"] == BroadcastRecipient.Status.PENDING)
    skipped = sum(1 for row in rows if row["status"] == BroadcastRecipient.Status.SKIPPED)

    bot_users = BotUser.objects.filter(customer_id__in=customers, bot_config__provider__in=channels)
    if store and getattr(store, "pk", None):
        bot_users = bot_users.filter(Q(bot_config__store=store) | Q(bot_config__store__isnull=True))
    bot_users_matched = bot_users.count()
    active_targets = (
        bot_users.filter(is_active=True, bot_config__is_active=True)
        .exclude(chat_id="")
        .exclude(bot_config__bot_token="")
    )
    active_target_count = active_targets.count()
    inactive_bot_users = max(bot_users_matched - active_target_count, 0)
    duplicates_removed = max(active_target_count - targetable, 0)
    warnings = []
    if targetable == 0:
        warnings.append("هیچ recipient قابل ارسال پیدا نشد؛ queue کردن مسدود است.")
    if targetable > LARGE_AUDIENCE_WARNING_THRESHOLD:
        warnings.append(f"مخاطب قابل ارسال بیشتر از {LARGE_AUDIENCE_WARNING_THRESHOLD:,} است؛ پردازش را فقط با command batch انجام بده.")
    if skipped:
        warnings.append("بخشی از مخاطبان Telegram/Bale target فعال ندارند و skipped می‌شوند.")
    if not active_bot_configurations(store, channels).exists():
        warnings.append("BotConfiguration فعال با token ذخیره‌شده برای این کانال پیدا نشد.")

    return AudiencePreview(
        audience_label=BROADCAST_AUDIENCE_LABELS.get(campaign.audience_type, campaign.get_audience_type_display()),
        channel_label=BROADCAST_CHANNEL_LABELS.get(campaign.channel, campaign.get_channel_display()),
        customers_matched=customer_count,
        bot_users_matched=bot_users_matched,
        targetable=targetable,
        missing_target=skipped,
        inactive_bot_users=inactive_bot_users,
        duplicates_removed=duplicates_removed,
        recently_messaged=0,
        estimated_recipients=len(rows),
        skipped=skipped,
        sample_customers=_customer_sample(customers),
        warnings=warnings,
        bot_config_label=bot_configuration_label(store, channels),
    )


def queue_confirmation_phrase(campaign):
    return f"{QUEUE_CONFIRMATION_PREFIX}_{campaign.pk}"


def validate_campaign_can_queue(campaign, actor=None):
    errors = []
    if not actor or not getattr(actor, "is_staff", False):
        errors.append("فقط staff/admin می‌تواند کمپین را queue کند.")
    if campaign.status != BroadcastMessage.Status.DRAFT:
        errors.append("فقط کمپین draft قابل queue شدن است.")
    if campaign.store and not store_is_sellable(campaign.store):
        errors.append(SETUP_NOT_READY_MESSAGE)
    try:
        campaign.full_clean()
    except ValidationError as exc:
        if hasattr(exc, "message_dict"):
            for values in exc.message_dict.values():
                errors.extend(values)
        else:
            errors.extend(exc.messages)
    try:
        clean_campaign_text(campaign.message_text)
    except ValidationError as exc:
        errors.extend(exc.messages)

    preview = get_audience_preview(campaign)
    if preview.targetable <= 0:
        errors.append("کمپین recipient قابل ارسال ندارد.")
    if not active_bot_configurations(campaign.store, campaign_channels(campaign)).exists():
        errors.append("BotConfiguration فعال با token ذخیره‌شده برای این کانال وجود ندارد.")
    return errors, preview


def queue_campaign_for_processing(campaign, actor):
    errors, preview = validate_campaign_can_queue(campaign, actor=actor)
    if errors:
        raise ValidationError(errors)
    counts = create_campaign_recipients(campaign)
    metadata = dict(campaign.metadata or {})
    metadata.update(
        {
            "queued_by_user_id": getattr(actor, "pk", None),
            "queued_at": timezone.now().isoformat(),
            "last_preview": {
                "customers_matched": preview.customers_matched,
                "targetable": preview.targetable,
                "missing_target": preview.missing_target,
                "duplicates_removed": preview.duplicates_removed,
                "estimated_recipients": preview.estimated_recipients,
            },
        }
    )
    campaign.status = BroadcastMessage.Status.QUEUED
    campaign.metadata = metadata
    campaign.save(update_fields=["status", "metadata", "updated_at"])
    counts["status"] = campaign.status
    return counts


def recipient_error_category(recipient_or_message):
    message = getattr(recipient_or_message, "error_message", recipient_or_message)
    return classify_delivery_error(message)


def recipient_error_label(recipient_or_message):
    message = getattr(recipient_or_message, "error_message", recipient_or_message)
    return safe_delivery_error(message)


def recipient_status_tone(status, error_category=""):
    if status == BroadcastRecipient.Status.SENT:
        return "success"
    if status == BroadcastRecipient.Status.PENDING:
        return "info"
    if status == BroadcastRecipient.Status.SKIPPED:
        return "skipped"
    if error_category == "blocked":
        return "danger"
    if error_category in {"rate_limited", "timeout_network"}:
        return "warning"
    return "danger"


def campaign_status_tone(campaign):
    if campaign.status == BroadcastMessage.Status.DRAFT:
        return "secondary"
    if campaign.status == BroadcastMessage.Status.QUEUED:
        return "warning"
    if campaign.status == BroadcastMessage.Status.SENDING:
        return "info"
    if campaign.status == BroadcastMessage.Status.SENT:
        return "success" if not campaign.failed_count else "warning"
    if campaign.status == BroadcastMessage.Status.FAILED:
        return "danger"
    if campaign.status == BroadcastMessage.Status.CANCELLED:
        return "secondary"
    return "secondary"


def campaign_delivery_metrics(campaign):
    recipients = BroadcastRecipient.objects.filter(campaign=campaign)
    total = recipients.count()
    pending = recipients.filter(status=BroadcastRecipient.Status.PENDING).count()
    sent = recipients.filter(status=BroadcastRecipient.Status.SENT).count()
    failed = recipients.filter(status=BroadcastRecipient.Status.FAILED).count()
    skipped = recipients.filter(status=BroadcastRecipient.Status.SKIPPED).count()
    categories = {
        "target_invalid": 0,
        "blocked": 0,
        "timeout_network": 0,
        "rate_limited": 0,
        "api_error": 0,
        "no_target": 0,
    }
    for status, error_message in recipients.values_list("status", "error_message"):
        category = classify_delivery_error(error_message)
        if status == BroadcastRecipient.Status.SKIPPED and category == "none":
            category = "no_target"
        if category in categories:
            categories[category] += 1
    denominator = sent + failed
    success_rate = (sent / denominator * 100) if denominator else None
    return {
        "total": total,
        "pending": pending,
        "sent": sent,
        "failed": failed,
        "blocked": categories["blocked"],
        "skipped": skipped,
        "success_rate": success_rate,
        "categories": categories,
        "retryable_failed": retryable_failed_recipients(campaign).count(),
    }


def retryable_failed_recipients(campaign):
    ids = []
    failed = BroadcastRecipient.objects.filter(campaign=campaign, status=BroadcastRecipient.Status.FAILED).only("pk", "error_message")
    for recipient in failed.iterator():
        if is_retryable_delivery_error(recipient.error_message):
            ids.append(recipient.pk)
    return BroadcastRecipient.objects.filter(pk__in=ids)


def reset_retryable_failed(campaign, actor=None):
    retryable = retryable_failed_recipients(campaign)
    count = retryable.update(status=BroadcastRecipient.Status.PENDING, error_message="", sent_at=None, updated_at=timezone.now())
    if count:
        metadata = dict(campaign.metadata or {})
        metadata["retry_requested_at"] = timezone.now().isoformat()
        metadata["retry_requested_by_user_id"] = getattr(actor, "pk", None)
        metadata["retry_requested_count"] = count
        campaign.status = BroadcastMessage.Status.QUEUED
        campaign.metadata = metadata
        campaign.save(update_fields=["status", "metadata", "updated_at"])
        refresh_campaign_counts(campaign)
    return count


def cancel_campaign(campaign, actor=None):
    if campaign.status not in {BroadcastMessage.Status.DRAFT, BroadcastMessage.Status.QUEUED}:
        raise ValidationError("فقط campaign draft یا queued قابل cancel است.")
    metadata = dict(campaign.metadata or {})
    metadata["cancelled_at"] = timezone.now().isoformat()
    metadata["cancelled_by_user_id"] = getattr(actor, "pk", None)
    campaign.status = BroadcastMessage.Status.CANCELLED
    campaign.metadata = metadata
    campaign.save(update_fields=["status", "metadata", "updated_at"])
    return campaign


def recipient_identity(recipient):
    if recipient.customer_id:
        return f"Customer #{recipient.customer_id}"
    return "Customer -"


def masked_target_label(recipient):
    if not recipient.target_identifier:
        return "-"
    return mask_identifier(recipient.target_identifier)


def recipient_row(recipient):
    category = recipient_error_category(recipient)
    return {
        "recipient": recipient,
        "customer_pk": recipient.customer_id,
        "bot_user_pk": bot_user_pk_for_recipient(recipient),
        "identity": recipient_identity(recipient),
        "target": masked_target_label(recipient),
        "status": recipient.get_status_display(),
        "status_value": recipient.status,
        "tone": recipient_status_tone(recipient.status, category),
        "error_category": category,
        "safe_error": recipient_error_label(recipient),
        "sent_at": recipient.sent_at,
        "created_at": recipient.created_at,
    }


def bot_user_pk_for_recipient(recipient):
    if not recipient.target_identifier:
        return ""
    bot_user = (
        BotUser.objects.filter(
            customer_id=recipient.customer_id,
            bot_config__provider=recipient.channel,
            chat_id=recipient.target_identifier,
        )
        .order_by("-last_seen_at", "-updated_at", "pk")
        .only("pk")
        .first()
    )
    return bot_user.pk if bot_user else ""


def campaign_row(campaign):
    metrics = campaign_delivery_metrics(campaign)
    return {
        "campaign": campaign,
        "title": safe_campaign_snippet(campaign.title, limit=80) or f"Campaign #{campaign.pk}",
        "status": campaign.get_status_display(),
        "status_value": campaign.status,
        "tone": campaign_status_tone(campaign),
        "audience": BROADCAST_AUDIENCE_LABELS.get(campaign.audience_type, campaign.get_audience_type_display()),
        "channel": BROADCAST_CHANNEL_LABELS.get(campaign.channel, campaign.get_channel_display()),
        "created_at": campaign.created_at,
        "sent_at": campaign.sent_at,
        "metrics": metrics,
        "review_url": campaign_review_url(campaign),
        "preview_url": campaign_preview_url(campaign),
    }


def base_campaign_queryset(store=None):
    queryset = BroadcastMessage.objects.select_related("store").order_by("-created_at", "-pk")
    if store and getattr(store, "pk", None):
        queryset = queryset.filter(store=store)
    return queryset


def limited_campaign_rows(queryset, limit=CAMPAIGN_WORKBENCH_LIMIT):
    return [campaign_row(campaign) for campaign in queryset[:limit]]


def get_workbench_context(selected_store=None):
    now = timezone.now()
    period_start = now - timedelta(days=30)
    campaigns = base_campaign_queryset(selected_store)
    recent_period = campaigns.filter(created_at__gte=period_start)
    recipients = BroadcastRecipient.objects.select_related("campaign")
    if selected_store and getattr(selected_store, "pk", None):
        recipients = recipients.filter(campaign__store=selected_store)
    recent_recipients = recipients.filter(created_at__gte=period_start)

    draft = campaigns.filter(status=BroadcastMessage.Status.DRAFT)
    queued = campaigns.filter(status=BroadcastMessage.Status.QUEUED)
    sending = campaigns.filter(status=BroadcastMessage.Status.SENDING)
    completed = campaigns.filter(status=BroadcastMessage.Status.SENT, failed_count=0)
    partial_failed = campaigns.filter(Q(status=BroadcastMessage.Status.FAILED) | Q(failed_count__gt=0)).distinct()
    cancelled = campaigns.filter(status=BroadcastMessage.Status.CANCELLED)

    sent_recipients = recent_recipients.filter(status=BroadcastRecipient.Status.SENT).count()
    failed_recipients = recent_recipients.filter(status=BroadcastRecipient.Status.FAILED).count()
    skipped_recipients = recent_recipients.filter(status=BroadcastRecipient.Status.SKIPPED).count()
    blocked_recipients = 0
    for error_message in recent_recipients.filter(status=BroadcastRecipient.Status.FAILED).values_list("error_message", flat=True):
        if classify_delivery_error(error_message) == "blocked":
            blocked_recipients += 1
    denominator = sent_recipients + failed_recipients
    success_rate = (sent_recipients / denominator * 100) if denominator else None
    sections = [
        {
            "key": "drafts",
            "title": "Drafts",
            "description": "کمپین‌های ذخیره‌شده که هنوز queue نشده‌اند.",
            "tone": "secondary",
            "count": draft.count(),
            "items": limited_campaign_rows(draft),
        },
        {
            "key": "queued",
            "title": "Ready / Queued",
            "description": "کمپین‌هایی که recipientها materialize شده‌اند و باید با command پردازش شوند.",
            "tone": "warning",
            "count": queued.count(),
            "items": limited_campaign_rows(queued),
        },
        {
            "key": "sending",
            "title": "Sending",
            "description": "کمپین‌هایی که processor در حال رسیدگی به آن‌هاست.",
            "tone": "info",
            "count": sending.count(),
            "items": limited_campaign_rows(sending),
        },
        {
            "key": "completed",
            "title": "Completed",
            "description": "کمپین‌های پایان‌یافته بدون failed ذخیره‌شده.",
            "tone": "success",
            "count": completed.count(),
            "items": limited_campaign_rows(completed),
        },
        {
            "key": "partial-failed",
            "title": "Partial / Failed",
            "description": "کمپین‌های failed یا sent همراه با failed recipient.",
            "tone": "danger",
            "count": partial_failed.count(),
            "items": limited_campaign_rows(partial_failed),
        },
        {
            "key": "cancelled",
            "title": "Cancelled",
            "description": "کمپین‌هایی که قبل از پردازش لغو شده‌اند.",
            "tone": "secondary",
            "count": cancelled.count(),
            "items": limited_campaign_rows(cancelled),
        },
        {
            "key": "recent",
            "title": "Recent campaigns",
            "description": "آخرین کمپین‌های ثبت‌شده برای مرور سریع.",
            "tone": "info",
            "count": campaigns.count(),
            "items": limited_campaign_rows(campaigns),
        },
    ]
    return {
        "metrics": {
            "draft": draft.count(),
            "queued": queued.count(),
            "sending": sending.count(),
            "sent_campaigns_30d": recent_period.filter(status=BroadcastMessage.Status.SENT).count(),
            "total_recipients_30d": recent_recipients.count(),
            "sent_recipients": sent_recipients,
            "failed_recipients": failed_recipients,
            "blocked_recipients": blocked_recipients,
            "skipped_recipients": skipped_recipients,
            "success_rate": success_rate,
        },
        "sections": sections,
        "quick_actions": [
            {"label": "ساخت کمپین جدید", "url": campaign_new_url(selected_store), "tone": "primary"},
            {"label": "کمپین‌های failed", "url": "#partial-failed", "tone": "danger"},
            {"label": "recipientهای blocked", "url": add_query(reverse("admin:store_broadcastrecipient_changelist"), {"status__exact": BroadcastRecipient.Status.FAILED}), "tone": "warning"},
            {"label": "Reports Center", "url": add_query(reverse("admin_store_reports_center"), {"store": getattr(selected_store, "pk", None)}) + "#campaigns", "tone": "info"},
            {"label": "BotConfiguration", "url": reverse("admin:store_botconfiguration_changelist"), "tone": "secondary"},
            {"label": "Broadcast admin خام", "url": reverse("admin:store_broadcastmessage_changelist"), "tone": "secondary"},
        ],
        "processor_command": "python manage.py process_broadcast_queue --batch-size 50",
    }


def get_review_context(campaign):
    metrics = campaign_delivery_metrics(campaign)
    recipients = (
        BroadcastRecipient.objects.select_related("campaign", "customer")
        .filter(campaign=campaign)
        .order_by("-updated_at", "-pk")[:RECENT_RECIPIENT_LIMIT]
    )
    metadata = campaign.metadata or {}
    return {
        "row": campaign_row(campaign),
        "metrics": metrics,
        "failure_breakdown": [
            {"label": "target invalid", "count": metrics["categories"]["target_invalid"], "tone": "danger"},
            {"label": "bot blocked", "count": metrics["categories"]["blocked"], "tone": "danger"},
            {"label": "timeout/network", "count": metrics["categories"]["timeout_network"], "tone": "warning"},
            {"label": "rate limited", "count": metrics["categories"]["rate_limited"], "tone": "warning"},
            {"label": "API error", "count": metrics["categories"]["api_error"], "tone": "danger"},
            {"label": "no target", "count": metrics["categories"]["no_target"], "tone": "skipped"},
        ],
        "recent_recipients": [recipient_row(recipient) for recipient in recipients],
        "queued_at": metadata.get("queued_at", ""),
        "cancelled_at": metadata.get("cancelled_at", ""),
        "created_by": metadata.get("created_by_user_id", ""),
        "queued_by": metadata.get("queued_by_user_id", ""),
        "admin_note": metadata.get("admin_note", ""),
        "actions": {
            "can_edit": campaign.status == BroadcastMessage.Status.DRAFT,
            "can_queue": campaign.status == BroadcastMessage.Status.DRAFT,
            "can_cancel": campaign.status in {BroadcastMessage.Status.DRAFT, BroadcastMessage.Status.QUEUED},
            "can_retry": metrics["retryable_failed"] > 0,
            "pause_supported": False,
            "resume_supported": False,
        },
        "confirmations": {
            "cancel": f"CANCEL_CAMPAIGN_{campaign.pk}",
            "retry": f"RETRY_CAMPAIGN_{campaign.pk}",
            "queue": queue_confirmation_phrase(campaign),
        },
        "urls": {
            "workbench": campaign_workbench_url(campaign.store),
            "edit": campaign_edit_url(campaign),
            "audience": campaign_audience_url(campaign),
            "preview": campaign_preview_url(campaign),
            "confirm": campaign_confirm_url(campaign),
            "export": campaign_export_url(campaign),
            "admin_change": reverse("admin:store_broadcastmessage_change", args=[campaign.pk]),
        },
    }


def build_campaign_export_csv(campaign):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        [
            "recipient_id",
            "customer_internal_pk",
            "bot_user_internal_pk",
            "status",
            "safe_error_category",
            "created_at",
            "sent_at",
        ]
    )
    recipients = (
        BroadcastRecipient.objects.select_related("customer", "campaign")
        .filter(campaign=campaign)
        .order_by("pk")
    )
    for recipient in recipients.iterator():
        writer.writerow(
            [
                recipient.pk,
                recipient.customer_id or "",
                bot_user_pk_for_recipient(recipient),
                recipient.status,
                recipient_error_category(recipient),
                timezone.localtime(recipient.created_at).isoformat() if recipient.created_at else "",
                timezone.localtime(recipient.sent_at).isoformat() if recipient.sent_at else "",
            ]
        )
    return output.getvalue()


def export_filename(campaign):
    return f"campaign-{campaign.pk}-recipients.csv"
