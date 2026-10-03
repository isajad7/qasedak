"""Measured activity and purchase-bound outreach; sync timestamps do not prove traffic."""
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator


class ActivityCollector(models.Model):
    # A fenced lease keeps overlapping containers from writing competing baselines.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.CharField(max_length=32, blank=True)
    lease_until = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    summary = models.JSONField(default=dict)


class PurchaseActivity(models.Model):
    order = models.OneToOneField("store.Order", on_delete=models.CASCADE, related_name="activity")
    observed_at = models.DateTimeField(null=True, db_index=True)
    entitlement = models.CharField(max_length=24, default="unknown")
    reason = models.CharField(max_length=48, default="not_observed")
    # Keys are scoped hashes; values contain counters/timestamps, never credentials.
    counters = models.JSONField(default=dict)
    continuous_since = models.DateTimeField(null=True)
    last_activity_start = models.DateTimeField(null=True)
    last_activity_at = models.DateTimeField(null=True)
    source_count = models.PositiveIntegerField(default=0)
    cycle = models.PositiveIntegerField(default=1)
    ended_at = models.DateTimeField(null=True)


class ActivityObservation(models.Model):
    purchase = models.ForeignKey(PurchaseActivity, on_delete=models.CASCADE, related_name="observations")
    observed_at = models.DateTimeField()
    interval_start = models.DateTimeField(null=True)
    delta_bytes = models.PositiveBigIntegerField(null=True)
    quality = models.CharField(max_length=48)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["purchase", "observed_at"], name="activity_purchase_sample_unique")]
        indexes = [models.Index(fields=["observed_at"], name="activity_observed_idx")]


class OutreachSettings(models.Model):
    class Mode(models.TextChoices):
        OFF = "off", "خاموش"
        PREVIEW = "preview", "پیش‌نمایش"
        LIVE = "live", "ارسال فعال"

    store = models.OneToOneField("store.Store", on_delete=models.CASCADE)
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.PREVIEW)
    inactivity_enabled = models.BooleanField(default=True)
    renewal_enabled = models.BooleanField(default=True)
    daily_limit = models.PositiveSmallIntegerField(default=50, validators=[MinValueValidator(1), MaxValueValidator(500)])
    start_hour = models.PositiveSmallIntegerField(default=9, validators=[MaxValueValidator(23)])
    end_hour = models.PositiveSmallIntegerField(default=21, validators=[MinValueValidator(1), MaxValueValidator(24)])
    activated_at = models.DateTimeField(null=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)
    last_run_at = models.DateTimeField(null=True)
    summary = models.JSONField(default=dict)

    def clean(self):
        if self.start_hour >= self.end_hour:
            raise ValidationError("ساعت شروع باید قبل از ساعت پایان باشد.")


class OutreachPreference(models.Model):
    store = models.ForeignKey("store.Store", on_delete=models.CASCADE)
    customer = models.ForeignKey("store.Customer", on_delete=models.CASCADE)
    muted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "customer"], name="outreach_customer_store_unique")]


class OutreachEvent(models.Model):
    class Status(models.TextChoices):
        PREVIEW = "preview", "پیش‌نمایش"
        READY = "ready", "در انتظار ارسال"
        SENDING = "sending", "در حال ارسال"
        SENT = "sent", "ارسال شد"
        UNCERTAIN = "uncertain", "نتیجه نامشخص؛ نیازمند بررسی"
        FAILED = "failed", "ردشده توسط تلگرام"
        CANCELLED = "cancelled", "شرایط دیگر برقرار نیست"

    order = models.ForeignKey("store.Order", on_delete=models.CASCADE, related_name="purchase_outreach")
    cycle = models.PositiveIntegerField()
    kind = models.CharField(max_length=24)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PREVIEW, db_index=True)
    body = models.TextField(blank=True)
    reason = models.CharField(max_length=80, blank=True)
    bot_user = models.ForeignKey("store.BotUser", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    claimed_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)
    message_id = models.CharField(max_length=40, blank=True)
    support_conversation = models.ForeignKey("store.SupportConversation", null=True, on_delete=models.SET_NULL)
    converted_order = models.ForeignKey("store.Order", null=True, on_delete=models.SET_NULL, related_name="converted_purchase_outreach")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "cycle", "kind"], name="outreach_purchase_stage_unique")]
        indexes = [models.Index(fields=["claimed_at", "status"], name="outreach_claimed_idx")]
