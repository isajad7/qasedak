import random
import re
import string
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import PurePath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.hashers import make_password
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator, URLValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .jalali import TEHRAN_TZ


def generate_order_tracking():
    return uuid.uuid4().hex


def generate_public_id():
    return uuid.uuid4()


def generate_bot_webhook_secret():
    return uuid.uuid4().hex


def generate_short_code(prefix="", length=8):
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(alphabet, k=length))
    return f"{prefix}{suffix}"


def normalize_telegram_bot_username(value):
    return str(value or "").strip().lstrip("@")


def default_reminder_days_before_expiry():
    return [3, 1, 0]


def default_reminder_days_after_expiry():
    return [1, 3]


def default_web_telegram_link_token_expiry():
    return timezone.now() + timedelta(days=7)


card_last4_validator = RegexValidator(
    regex=r"^\d{4}$",
    message=_("Enter exactly 4 digits."),
)

tracking_code_validator = RegexValidator(
    regex=r"^([A-Za-z0-9]{8}|[0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$",
    message=_("Enter a valid tracking code."),
)


def parse_payment_time(value):
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if not value:
        raise ValidationError(_("Payment time is required."))
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError as exc:
        raise ValidationError(_("Enter payment time in HH:MM format.")) from exc


PERSIAN_ARABIC_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

PAYMENT_RECEIPT_DEFAULT_MAX_SIZE = 5 * 1024 * 1024
PAYMENT_RECEIPT_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


def normalize_payment_digits(value):
    return str(value or "").translate(PERSIAN_ARABIC_DIGIT_TRANSLATION)


def clean_sender_card_name(value, fallback=""):
    cleaned = str(value or "").strip() or str(fallback or "").strip()
    if not cleaned:
        raise ValidationError(_("Payer card owner name is required."))
    if len(cleaned) > 100:
        raise ValidationError(_("Payer card owner name must be 100 characters or fewer."))
    return cleaned


def clean_sender_card_last4(value, fallback=""):
    raw_value = value if value not in (None, "") else fallback
    digits = "".join(ch for ch in normalize_payment_digits(raw_value) if ch.isdigit())
    if not digits:
        return ""
    if len(digits) != 4:
        raise ValidationError(_("Enter exactly 4 digits."))
    return digits


def clean_payment_receipt_text(value):
    cleaned = str(value or "").strip()
    if len(cleaned) > 1200:
        raise ValidationError(_("متن رسید باید حداکثر ۱۲۰۰ کاراکتر باشد."))
    return cleaned


def clean_bank_tracking_code(value):
    cleaned = str(value or "").strip()
    if len(cleaned) > 50:
        raise ValidationError(_("Bank tracking code must be 50 characters or fewer."))
    return cleaned


def validate_payment_receipt_image(receipt_image):
    if not receipt_image:
        return None

    max_size = getattr(settings, "PAYMENT_RECEIPT_MAX_UPLOAD_SIZE", PAYMENT_RECEIPT_DEFAULT_MAX_SIZE)
    size = getattr(receipt_image, "size", None)
    if size and size > max_size:
        max_mb = max_size / (1024 * 1024)
        raise ValidationError(_("Receipt image must be smaller than %(size).0f MB.") % {"size": max_mb})

    content_type = (getattr(receipt_image, "content_type", "") or "").lower()
    if content_type and content_type not in PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES:
        raise ValidationError(_("Receipt file must be a JPG, PNG, WEBP, or GIF image."))

    extension = PurePath(getattr(receipt_image, "name", "") or "").suffix.lower()
    if extension and extension not in PAYMENT_RECEIPT_ALLOWED_EXTENSIONS:
        raise ValidationError(_("Receipt file must be a JPG, PNG, WEBP, or GIF image."))

    try:
        from PIL import Image, UnidentifiedImageError

        if hasattr(receipt_image, "seek"):
            receipt_image.seek(0)
        image = Image.open(receipt_image)
        image.verify()
    except UnidentifiedImageError as exc:
        raise ValidationError(_("Receipt file is not a valid image.")) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValidationError(_("Receipt image could not be read. Please upload a clear JPG or PNG image.")) from exc
    finally:
        if hasattr(receipt_image, "seek"):
            receipt_image.seek(0)

    return receipt_image


def clean_manual_payment_submission(
    *,
    sender_card_name="",
    sender_card_last4="",
    payment_time=None,
    receipt_image=None,
    receipt_text="",
    require_receipt=False,
    require_receipt_image=False,
    existing_sender_card_name="",
    existing_sender_card_last4="",
):
    cleaned_receipt_text = clean_payment_receipt_text(receipt_text)
    cleaned_receipt_image = validate_payment_receipt_image(receipt_image)
    if require_receipt_image and not cleaned_receipt_image:
        raise ValidationError(_("عکس رسید را بارگذاری کن."))
    if require_receipt and not cleaned_receipt_image and not cleaned_receipt_text:
        raise ValidationError(_("رسید را به صورت متن وارد کن یا عکس رسید را بفرست."))
    return {
        "sender_card_name": clean_sender_card_name(sender_card_name, existing_sender_card_name),
        "sender_card_last4": clean_sender_card_last4(sender_card_last4, existing_sender_card_last4),
        "payment_time": parse_payment_time(payment_time),
        "receipt_image": cleaned_receipt_image,
        "receipt_text": cleaned_receipt_text,
    }


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(_("created at"), default=timezone.now, db_index=True, editable=False)
    updated_at = models.DateTimeField(_("updated at"), default=timezone.now)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.updated_at = timezone.now()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "updated_at" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "updated_at"]
        super().save(*args, **kwargs)


class Store(TimeStampedModel):
    class SalesMode(models.TextChoices):
        TUNNEL = "tunnel", _("Tunnel")
        OPERATOR_BASED = "operator_based", _("Operator based")

    class PanelUsageActiveUserMethod(models.TextChoices):
        TRAFFIC_DELTA = "traffic_delta", _("Traffic delta")
        ONLINE_API = "online_api", _("Online API")
        MIXED = "mixed", _("Mixed")

    name = models.CharField(_("name"), max_length=100, default="VPN Store")
    english_name = models.CharField(_("English name"), max_length=100, default="VPN Store")
    slug = models.SlugField(_("slug"), max_length=80, unique=True, null=True, blank=True)
    domain = models.CharField(_("domain"), max_length=255, blank=True, null=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    sales_mode = models.CharField(
        _("sales mode"),
        max_length=30,
        choices=SalesMode.choices,
        default=SalesMode.TUNNEL,
        db_index=True,
        help_text=_("Tunnel shows plans directly; operator based asks customers to choose an operator first."),
    )
    plan_inbound_routing_enabled = models.BooleanField(
        _("plan inbound routing enabled"),
        default=True,
        help_text=_("اگر فعال باشد، فروش هر پلن از routeهای explicit همان پلن/اپراتور به inbound انجام می‌شود."),
    )
    allow_global_inbound_fallback = models.BooleanField(
        _("allow global inbound fallback"),
        default=True,
        help_text=_("اگر route explicit برای پلن پیدا نشد، اجازه استفاده از الگوریتم عمومی انتخاب inbound را می‌دهد."),
    )

    hero_title = models.CharField(_("hero title"), max_length=200, default="Fast and secure VPN access")
    hero_text = models.TextField(
        _("hero text"),
        default="Buy a VPN plan and receive your configuration after payment verification."
    )

    telegram_channel = models.URLField(_("Telegram channel"), max_length=200, blank=True, null=True)
    telegram_support = models.CharField(_("Telegram support"), max_length=100, blank=True, null=True)
    bale_support = models.CharField(
        _("Bale support"),
        max_length=200,
        blank=True,
        null=True,
        help_text=_("Bale support username, @username, or full ble.ir link."),
    )
    support_email = models.EmailField(_("support email"), blank=True, null=True)

    card_number = models.CharField(
        _("card number"),
        max_length=16,
        help_text=_("Manual card-to-card destination card number, without spaces."),
    )
    card_owner = models.CharField(_("card owner"), max_length=100)
    receipt_image_only_payment = models.BooleanField(
        _("receipt image only payment"),
        default=False,
        help_text=_("When enabled, checkout only asks for a receipt image for this card."),
    )
    smsforwarder_webhook_token_hash = models.CharField(
        _("SMSForwarder webhook token hash"),
        max_length=255,
        blank=True,
        editable=False,
        help_text=_("Hashed SMSForwarder webhook token. Set or rotate it from the admin write-only token field."),
    )
    smsforwarder_webhook_token_hint = models.CharField(
        _("SMSForwarder webhook token hint"),
        max_length=16,
        blank=True,
        editable=False,
        help_text=_("Last characters of the configured webhook token, used only as an admin hint."),
    )
    payment_sms_time_zone = models.CharField(
        _("payment SMS time zone"),
        max_length=64,
        default="Asia/Tehran",
        blank=True,
        help_text=_("Time zone used when parsing bank SMS dates. Leave blank to use the legacy settings fallback."),
    )
    custom_volume_price_per_gb = models.DecimalField(
        _("custom volume price per GB"),
        max_digits=14,
        decimal_places=3,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("When greater than zero, customers can buy custom GB volume for 30 days at this unit price."),
    )
    referral_system_enabled = models.BooleanField(
        _("referral system enabled"),
        default=True,
        help_text=_("When disabled, referral codes remain visible but new GB rewards are not created."),
    )
    referral_reward_gb = models.DecimalField(
        _("referral reward traffic GB"),
        max_digits=8,
        decimal_places=3,
        default=Decimal("2.000"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Gift-package traffic awarded to the inviter after an eligible invited purchase."),
    )
    referral_reward_duration_days = models.PositiveIntegerField(
        _("referral reward duration days"),
        default=30,
        validators=[MinValueValidator(1)],
        help_text=_("Gift-package duration awarded to the inviter after an eligible invited purchase."),
    )
    referral_min_order_required = models.BooleanField(
        _("referral minimum order required"),
        default=True,
        help_text=_("Only the invited customer's first successful order can create the referral reward."),
    )
    free_trial_enabled = models.BooleanField(
        _("free trial enabled"),
        default=False,
        help_text=_("Allow bot users to receive one free test configuration per cooldown window."),
    )
    free_trial_panel = models.ForeignKey(
        "Panel",
        verbose_name=_("free trial panel"),
        on_delete=models.SET_NULL,
        related_name="free_trial_stores",
        null=True,
        blank=True,
    )
    free_trial_inbound = models.ForeignKey(
        "Inbound",
        verbose_name=_("free trial inbound"),
        on_delete=models.SET_NULL,
        related_name="free_trial_stores",
        null=True,
        blank=True,
    )
    free_trial_traffic_gb = models.DecimalField(
        _("free trial traffic GB"),
        max_digits=8,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    free_trial_duration_hours = models.PositiveIntegerField(
        _("free trial duration hours"),
        default=24,
        validators=[MinValueValidator(1)],
    )
    free_trial_cooldown_days = models.PositiveIntegerField(
        _("free trial cooldown days"),
        default=30,
        validators=[MinValueValidator(1)],
    )
    analytics_enabled = models.BooleanField(
        _("analytics enabled"),
        default=True,
        help_text=_("Enable customer analytics reports in admin and admin bots."),
    )
    broadcast_enabled = models.BooleanField(
        _("broadcast enabled"),
        default=True,
        help_text=_("Allow admins to send targeted broadcast campaigns."),
    )
    broadcast_rate_limit_per_second = models.PositiveIntegerField(
        _("broadcast rate limit per second"),
        default=5,
        validators=[MinValueValidator(1)],
        help_text=_("Maximum customer broadcast messages sent per second."),
    )
    broadcast_max_recipients_per_campaign = models.PositiveIntegerField(
        _("broadcast max recipients per campaign"),
        default=1000,
        validators=[MinValueValidator(1)],
        help_text=_("Maximum customers resolved for a single broadcast campaign."),
    )
    revenue_engine_enabled = models.BooleanField(
        _("revenue engine enabled"),
        default=True,
        help_text=_("Master kill switch for all revenue engine messages."),
    )
    revenue_engine_dry_run = models.BooleanField(
        _("revenue engine dry run"),
        default=True,
        help_text=_("When enabled, revenue offers are logged but not sent."),
    )
    revenue_engine_quiet_hours_enabled = models.BooleanField(
        _("revenue engine quiet hours enabled"),
        default=False,
    )
    revenue_engine_quiet_hours_start = models.TimeField(
        _("revenue engine quiet hours start"),
        null=True,
        blank=True,
    )
    revenue_engine_quiet_hours_end = models.TimeField(
        _("revenue engine quiet hours end"),
        null=True,
        blank=True,
    )
    revenue_engine_timezone = models.CharField(
        _("revenue engine time zone"),
        max_length=64,
        default="Asia/Tehran",
        blank=True,
    )
    renewal_engine_enabled = models.BooleanField(_("renewal engine enabled"), default=True)
    upsell_engine_enabled = models.BooleanField(_("upsell engine enabled"), default=True)
    retention_engine_enabled = models.BooleanField(_("retention engine enabled"), default=True)
    ai_revenue_optimizer_enabled = models.BooleanField(_("AI revenue optimizer enabled"), default=True)
    revenue_optimization_enabled = models.BooleanField(_("revenue optimization enabled"), default=True)
    revenue_max_offers_per_user_per_day = models.PositiveIntegerField(
        _("revenue max offers per user per day"),
        default=1,
        validators=[MinValueValidator(1)],
    )
    revenue_max_offers_per_user_per_week = models.PositiveIntegerField(
        _("revenue max offers per user per week"),
        default=3,
        validators=[MinValueValidator(1)],
    )
    revenue_max_total_offers_per_day = models.PositiveIntegerField(
        _("revenue max total offers per day"),
        default=500,
        validators=[MinValueValidator(1)],
    )
    revenue_min_ai_confidence = models.DecimalField(
        _("revenue minimum AI confidence"),
        max_digits=4,
        decimal_places=2,
        default=Decimal("0.50"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
    )
    revenue_offer_cooldown_hours = models.PositiveIntegerField(
        _("revenue offer cooldown hours"),
        default=24,
        validators=[MinValueValidator(1)],
    )
    retention_offer_cooldown_hours = models.PositiveIntegerField(
        _("retention offer cooldown hours"),
        default=72,
        validators=[MinValueValidator(1)],
    )
    renewal_reminders_enabled = models.BooleanField(
        _("renewal reminders enabled"),
        default=True,
        help_text=_("Send Telegram reminders before and after VPN client expiry."),
    )
    renewal_reminders_start_at = models.DateTimeField(
        _("renewal reminders start at"),
        null=True,
        blank=True,
        help_text=_(
            "اگر تنظیم شود، یادآوری فقط برای سرویس‌هایی ارسال می‌شود که بعد از این زمان ساخته شده‌اند. "
            "برای نادیده گرفتن دیتای قدیمی استفاده می‌شود."
        ),
    )
    reminder_days_before_expiry = models.JSONField(
        _("reminder days before expiry"),
        default=default_reminder_days_before_expiry,
        blank=True,
        help_text=_("Integer day offsets before expiry, for example [3, 1, 0]."),
    )
    reminder_days_after_expiry = models.JSONField(
        _("reminder days after expiry"),
        default=default_reminder_days_after_expiry,
        blank=True,
        help_text=_("Integer day offsets after expiry, for example [1, 3]."),
    )
    low_traffic_reminders_enabled = models.BooleanField(
        _("low traffic reminders enabled"),
        default=True,
        help_text=_("Send Telegram reminders when remaining traffic is below the configured thresholds."),
    )
    low_traffic_percent_threshold = models.PositiveIntegerField(
        _("low traffic percent threshold"),
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text=_("Send a reminder when remaining traffic percent is at or below this value."),
    )
    low_traffic_gb_threshold = models.DecimalField(
        _("low traffic GB threshold"),
        max_digits=8,
        decimal_places=3,
        default=Decimal("2.000"),
        validators=[MinValueValidator(Decimal("0.001"))],
        help_text=_("Send a reminder when remaining traffic GB is at or below this value."),
    )
    panel_monitor_enabled = models.BooleanField(
        _("panel monitor enabled"),
        default=True,
        help_text=_("Run operational health checks for active X-UI/Sanaei panels."),
    )
    panel_monitor_alerts_enabled = models.BooleanField(
        _("panel monitor alerts enabled"),
        default=True,
        help_text=_("Send Telegram admin alerts when panel health changes or cooldown reminders are due."),
    )
    panel_monitor_check_timeout_seconds = models.PositiveIntegerField(
        _("panel monitor check timeout seconds"),
        default=15,
        validators=[MinValueValidator(1)],
    )
    panel_monitor_alert_cooldown_minutes = models.PositiveIntegerField(
        _("panel monitor alert cooldown minutes"),
        default=30,
        validators=[MinValueValidator(1)],
    )
    panel_monitor_max_log_age_days = models.PositiveIntegerField(
        _("panel monitor max log age days"),
        default=30,
        validators=[MinValueValidator(1)],
        help_text=_("Panel health check logs older than this value can be removed by the cleanup command."),
    )
    daily_admin_report_enabled = models.BooleanField(
        _("daily admin report enabled"),
        default=True,
        help_text=_("Send the daily Telegram admin operations report for this store."),
    )
    daily_admin_report_time = models.TimeField(
        _("daily admin report time"),
        default=time(9, 0),
        help_text=_("Intended local send time. Scheduling is handled by cron/systemd."),
    )
    daily_admin_report_timezone = models.CharField(
        _("daily admin report timezone"),
        max_length=64,
        default="Asia/Tehran",
        blank=True,
        help_text=_("IANA timezone used to calculate the daily report period."),
    )
    daily_admin_report_include_panel_health = models.BooleanField(
        _("daily admin report include panel health"),
        default=True,
    )
    daily_admin_report_include_financials = models.BooleanField(
        _("daily admin report include financials"),
        default=True,
    )
    daily_admin_report_include_errors = models.BooleanField(
        _("daily admin report include errors"),
        default=True,
    )
    panel_usage_tracking_enabled = models.BooleanField(
        _("panel usage tracking enabled"),
        default=True,
        help_text=_("Collect periodic X-UI/Sanaei usage snapshots for active panels."),
    )
    panel_usage_report_enabled = models.BooleanField(
        _("panel usage report enabled"),
        default=True,
        help_text=_("Include panel usage deltas and active users in the daily admin report."),
    )
    panel_usage_snapshot_retention_days = models.PositiveIntegerField(
        _("panel usage snapshot retention days"),
        default=45,
        validators=[MinValueValidator(1)],
    )
    panel_usage_active_user_method = models.CharField(
        _("panel usage active user method"),
        max_length=20,
        choices=PanelUsageActiveUserMethod.choices,
        default=PanelUsageActiveUserMethod.MIXED,
        help_text=_("How daily active panel users are counted from snapshots."),
    )
    reminder_cooldown_hours = models.PositiveIntegerField(
        _("reminder cooldown hours"),
        default=24,
        validators=[MinValueValidator(1)],
        help_text=_("Minimum hours between retryable reminders for the same VPN client."),
    )
    reminder_max_per_client_per_day = models.PositiveIntegerField(
        _("reminder max per client per day"),
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_("Maximum sent reminder messages per VPN client per calendar day."),
    )
    good_customer_min_total_amount = models.PositiveIntegerField(
        _("good customer minimum total amount"),
        default=500000,
        help_text=_("Minimum successful purchase amount used to tag good customers."),
    )
    loyal_customer_min_orders_30d = models.PositiveIntegerField(
        _("loyal customer minimum orders in 30 days"),
        default=2,
        help_text=_("Minimum successful orders in the last 30 days used to tag loyal customers."),
    )
    top_customers_limit = models.PositiveIntegerField(
        _("top customers limit"),
        default=10,
        help_text=_("Number of customers included in top buyer and top referrer segments."),
    )
    inactive_customer_days = models.PositiveIntegerField(
        _("inactive customer days"),
        default=30,
        help_text=_("Customers with older successful purchases and no recent purchase are tagged inactive."),
    )
    bank_name = models.CharField(_("bank name"), max_length=100, blank=True, null=True)
    sheba_number = models.CharField(_("Sheba number"), max_length=34, blank=True, null=True)

    class Meta:
        verbose_name = _("store")
        verbose_name_plural = _("stores")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "slug"]),
        ]

    def __str__(self):
        return self.name

    @property
    def referral_reward_traffic_gb(self):
        return self.referral_reward_gb

    def clean(self):
        super().clean()
        errors = {}

        for field_name in ("reminder_days_before_expiry", "reminder_days_after_expiry"):
            value = getattr(self, field_name)
            if not isinstance(value, (list, tuple)):
                errors[field_name] = _("Reminder days must be a list of integers.")
                continue
            invalid_items = [
                item
                for item in value
                if isinstance(item, bool) or not isinstance(item, int)
            ]
            if invalid_items:
                errors[field_name] = _("Reminder days must contain integers only.")

        if self.low_traffic_percent_threshold is not None and not (
            1 <= int(self.low_traffic_percent_threshold) <= 100
        ):
            errors["low_traffic_percent_threshold"] = _("Low traffic percent threshold must be between 1 and 100.")
        if self.low_traffic_gb_threshold is not None and self.low_traffic_gb_threshold <= 0:
            errors["low_traffic_gb_threshold"] = _("Low traffic GB threshold must be positive.")
        if self.panel_monitor_check_timeout_seconds is not None and self.panel_monitor_check_timeout_seconds <= 0:
            errors["panel_monitor_check_timeout_seconds"] = _("Panel monitor timeout must be positive.")
        if self.panel_monitor_alert_cooldown_minutes is not None and self.panel_monitor_alert_cooldown_minutes <= 0:
            errors["panel_monitor_alert_cooldown_minutes"] = _("Panel monitor alert cooldown must be positive.")
        if self.panel_monitor_max_log_age_days is not None and self.panel_monitor_max_log_age_days <= 0:
            errors["panel_monitor_max_log_age_days"] = _("Panel monitor log age must be positive.")
        if self.panel_usage_snapshot_retention_days is not None and self.panel_usage_snapshot_retention_days <= 0:
            errors["panel_usage_snapshot_retention_days"] = _("Panel usage snapshot retention must be positive.")
        if self.panel_usage_active_user_method not in self.PanelUsageActiveUserMethod.values:
            errors["panel_usage_active_user_method"] = _("Enter a valid active user method.")
        if self.reminder_cooldown_hours is not None and self.reminder_cooldown_hours <= 0:
            errors["reminder_cooldown_hours"] = _("Reminder cooldown must be positive.")
        if self.reminder_max_per_client_per_day is not None and self.reminder_max_per_client_per_day <= 0:
            errors["reminder_max_per_client_per_day"] = _("Daily reminder limit must be positive.")

        if self.free_trial_traffic_gb is not None and self.free_trial_traffic_gb <= 0:
            errors["free_trial_traffic_gb"] = _("Free trial traffic must be positive.")
        if self.free_trial_duration_hours is not None and self.free_trial_duration_hours <= 0:
            errors["free_trial_duration_hours"] = _("Free trial duration must be positive.")
        if self.free_trial_cooldown_days is not None and self.free_trial_cooldown_days <= 0:
            errors["free_trial_cooldown_days"] = _("Free trial cooldown must be positive.")
        payment_sms_time_zone = str(self.payment_sms_time_zone or "").strip()
        self.payment_sms_time_zone = payment_sms_time_zone
        if payment_sms_time_zone:
            try:
                ZoneInfo(payment_sms_time_zone)
            except ZoneInfoNotFoundError:
                errors["payment_sms_time_zone"] = _("Enter a valid IANA time zone, for example Asia/Tehran.")
        daily_report_timezone = str(self.daily_admin_report_timezone or "").strip() or "Asia/Tehran"
        self.daily_admin_report_timezone = daily_report_timezone
        try:
            ZoneInfo(daily_report_timezone)
        except ZoneInfoNotFoundError:
            errors["daily_admin_report_timezone"] = _("Enter a valid IANA time zone, for example Asia/Tehran.")
        revenue_timezone = str(self.revenue_engine_timezone or "").strip() or "Asia/Tehran"
        self.revenue_engine_timezone = revenue_timezone
        try:
            ZoneInfo(revenue_timezone)
        except ZoneInfoNotFoundError:
            errors["revenue_engine_timezone"] = _("Enter a valid IANA time zone, for example Asia/Tehran.")
        if self.revenue_engine_quiet_hours_enabled and (
            not self.revenue_engine_quiet_hours_start or not self.revenue_engine_quiet_hours_end
        ):
            errors["revenue_engine_quiet_hours_enabled"] = _("Quiet hours require both start and end times.")
        if self.revenue_max_offers_per_user_per_day is not None and self.revenue_max_offers_per_user_per_day <= 0:
            errors["revenue_max_offers_per_user_per_day"] = _("Daily revenue offer limit must be positive.")
        if self.revenue_max_offers_per_user_per_week is not None and self.revenue_max_offers_per_user_per_week <= 0:
            errors["revenue_max_offers_per_user_per_week"] = _("Weekly revenue offer limit must be positive.")
        if self.revenue_max_total_offers_per_day is not None and self.revenue_max_total_offers_per_day <= 0:
            errors["revenue_max_total_offers_per_day"] = _("Total daily revenue offer limit must be positive.")
        if self.revenue_offer_cooldown_hours is not None and self.revenue_offer_cooldown_hours <= 0:
            errors["revenue_offer_cooldown_hours"] = _("Revenue offer cooldown must be positive.")
        if self.retention_offer_cooldown_hours is not None and self.retention_offer_cooldown_hours <= 0:
            errors["retention_offer_cooldown_hours"] = _("Retention offer cooldown must be positive.")
        if self.revenue_min_ai_confidence is not None and not (
            Decimal("0") <= self.revenue_min_ai_confidence <= Decimal("1")
        ):
            errors["revenue_min_ai_confidence"] = _("Revenue AI confidence must be between 0 and 1.")

        if not self.free_trial_enabled:
            if errors:
                raise ValidationError(errors)
            return

        panel = self.free_trial_panel
        inbound = self.free_trial_inbound
        if not panel:
            errors["free_trial_panel"] = _("Free trial panel is required when free trial is enabled.")
        elif not panel.is_active:
            errors["free_trial_panel"] = _("Free trial panel must be active.")
        elif self.pk and panel.store_id not in (None, self.pk):
            errors["free_trial_panel"] = _("Free trial panel must belong to this store.")

        if not inbound:
            errors["free_trial_inbound"] = _("Free trial inbound is required when free trial is enabled.")
        elif not inbound.is_active:
            errors["free_trial_inbound"] = _("Free trial inbound must be active.")
        elif not inbound.available_for_new_orders:
            errors["free_trial_inbound"] = _("Free trial inbound must be available for new orders.")
        elif panel and inbound.panel_id != panel.pk:
            errors["free_trial_inbound"] = _("Free trial inbound must belong to the selected panel.")

        if errors:
            raise ValidationError(errors)

    @property
    def telegram_support_url(self):
        if not self.telegram_support:
            return None
        return f"https://t.me/{self.telegram_support.lstrip('@')}"

    def set_smsforwarder_webhook_token(self, raw_token):
        token = str(raw_token or "").strip()
        if not token:
            return False
        self.smsforwarder_webhook_token_hash = make_password(token)
        self.smsforwarder_webhook_token_hint = token[-4:]
        return True

    @property
    def bale_support_handle(self):
        if not self.bale_support:
            return ""
        value = self.bale_support.strip().rstrip("/")
        if value.startswith(("http://", "https://")):
            return value.split("/")[-1]
        return value.lstrip("@")

    @property
    def bale_support_url(self):
        if not self.bale_support:
            return None
        value = self.bale_support.strip()
        if value.startswith(("http://", "https://")):
            return value
        return f"https://ble.ir/{value.lstrip('@')}"


class Operator(TimeStampedModel):
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="operators",
        null=True,
        blank=True,
    )
    name = models.CharField(_("name"), max_length=100)
    slug = models.SlugField(_("slug"), max_length=100, blank=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    description = models.TextField(_("description"), blank=True)
    sort_order = models.PositiveIntegerField(_("sort order"), default=0, db_index=True)

    class Meta:
        verbose_name = _("operator")
        verbose_name_plural = _("operators")
        ordering = ["sort_order", "name", "id"]
        indexes = [
            models.Index(fields=["store", "is_active", "sort_order"]),
            models.Index(fields=["is_active", "sort_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "slug"],
                name="unique_operator_slug_per_store",
                condition=~models.Q(slug=""),
            ),
        ]

    def __str__(self):
        return self.name


class BotConfiguration(TimeStampedModel):
    class Provider(models.TextChoices):
        BALE = "bale", _("Bale")
        TELEGRAM = "telegram", _("Telegram")

    name = models.CharField(_("name"), max_length=100, default="Admin bot")
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="bot_configurations",
        null=True,
        blank=True,
    )
    provider = models.CharField(
        _("provider"),
        max_length=20,
        choices=Provider.choices,
        default=Provider.BALE,
        db_index=True,
    )
    telegram_bot_username = models.CharField(
        _("Telegram bot username"),
        max_length=64,
        blank=True,
        help_text=_("نام کاربری ربات بدون @، برای ساخت لینک دعوت و start parameter استفاده می‌شود."),
    )
    bot_token = models.CharField(_("bot token"), max_length=255, help_text=_("Bot token from Bale or Telegram."))
    admin_user_id = models.CharField(
        _("admin user ID"),
        max_length=80,
        help_text=_("Admin chat/user ID that receives notifications."),
    )
    additional_admin_user_ids = models.TextField(
        _("additional admin user IDs"),
        blank=True,
        help_text=_("Optional extra admin chat/user IDs, separated by comma, space, or new line."),
    )
    webhook_secret = models.CharField(
        _("webhook secret"),
        max_length=32,
        default=generate_bot_webhook_secret,
        unique=True,
        editable=False,
    )
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    force_telegram_channel_join = models.BooleanField(
        _("force Telegram channel join"),
        default=False,
        help_text=_("Require Telegram users to join the configured channel before using customer bot actions."),
    )
    telegram_required_channel_id = models.CharField(
        _("Telegram required channel ID"),
        max_length=120,
        blank=True,
        help_text=_("Numeric channel ID, for example -1001234567890."),
    )
    telegram_required_channel_username = models.CharField(
        _("Telegram required channel username"),
        max_length=100,
        blank=True,
        help_text=_("Channel username with or without @."),
    )
    telegram_required_channel_invite_link = models.URLField(
        _("Telegram required channel invite link"),
        max_length=255,
        blank=True,
        help_text=_("Public or private invite link shown to users."),
    )
    telegram_join_check_message = models.TextField(
        _("Telegram join check message"),
        default="برای استفاده از ربات ابتدا عضو کانال شوید.",
        blank=True,
    )
    notify_new_orders = models.BooleanField(_("notify new orders"), default=True)
    notify_order_updates = models.BooleanField(_("notify order updates"), default=True)
    send_sales_reports = models.BooleanField(_("send sales reports"), default=True)
    report_interval_hours = models.PositiveIntegerField(
        _("report interval hours"),
        default=6,
        validators=[MinValueValidator(1)],
    )
    last_report_sent_at = models.DateTimeField(_("last report sent at"), blank=True, null=True)
    last_error = models.TextField(_("last error"), blank=True)

    class Meta:
        verbose_name = _("bot configuration")
        verbose_name_plural = _("bot configurations")
        ordering = ["provider", "name"]
        indexes = [
            models.Index(fields=["provider", "is_active"]),
            models.Index(fields=["store", "is_active"]),
        ]

    def __str__(self):
        return f"{self.get_provider_display()} - {self.name}"

    def clean(self):
        super().clean()
        username = normalize_telegram_bot_username(self.telegram_bot_username)
        self.telegram_bot_username = username
        if not username:
            return

        errors = {}
        if len(username) < 5 or len(username) > 32:
            errors["telegram_bot_username"] = _("Telegram username must be between 5 and 32 characters.")
        if not re.fullmatch(r"[A-Za-z0-9_]+", username):
            errors["telegram_bot_username"] = _("Telegram username may contain only letters, numbers, and underscore.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.telegram_bot_username = normalize_telegram_bot_username(self.telegram_bot_username)
        super().save(*args, **kwargs)

    def get_admin_user_ids(self):
        raw_ids = [self.admin_user_id or ""]
        raw_ids.extend(
            (self.additional_admin_user_ids or "")
            .replace("،", ",")
            .replace(";", ",")
            .replace("\n", ",")
            .replace("\r", ",")
            .split(",")
        )
        admin_ids = []
        for raw_id in raw_ids:
            for value in str(raw_id or "").split():
                value = value.strip()
                if value and value not in admin_ids:
                    admin_ids.append(value)
        return admin_ids

    def is_admin_user(self, user_id):
        return str(user_id or "") in self.get_admin_user_ids()


class BotUser(TimeStampedModel):
    class State(models.TextChoices):
        IDLE = "idle", _("Idle")
        PROFILE_WAIT_PHONE = "profile_wait_phone", _("Profile: waiting for phone")
        BUY_WAIT_CUSTOM_VOLUME = "buy_wait_custom_volume", _("Buy: waiting for custom volume")
        BUY_WAIT_QUANTITY = "buy_wait_quantity", _("Buy: waiting for quantity")
        BUY_WAIT_NAME = "buy_wait_name", _("Buy: waiting for payer name")
        BUY_WAIT_LAST4 = "buy_wait_last4", _("Buy: waiting for card last4")
        BUY_WAIT_TIME = "buy_wait_time", _("Buy: waiting for payment time")
        BUY_WAIT_TRACKING = "buy_wait_tracking", _("Buy: waiting for bank tracking")
        BUY_WAIT_RECEIPT = "buy_wait_receipt", _("Buy: waiting for receipt")
        GRANT_WAIT_USER = "grant_wait_user", _("Grant: waiting for user")
        GRANT_WAIT_REASON = "grant_wait_reason", _("Grant: waiting for reason")
        GRANT_CONFIRM = "grant_confirm", _("Grant: confirm")
        BROADCAST_WAIT_TEXT = "broadcast_wait_text", _("Broadcast: waiting for text")
        BROADCAST_CONFIRM = "broadcast_confirm", _("Broadcast: confirm")

    bot_config = models.ForeignKey(
        BotConfiguration,
        verbose_name=_("bot configuration"),
        on_delete=models.CASCADE,
        related_name="bot_users",
    )
    customer = models.ForeignKey(
        "Customer",
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="bot_users",
        null=True,
        blank=True,
    )
    provider_user_id = models.CharField(_("provider user ID"), max_length=80, db_index=True)
    chat_id = models.CharField(_("chat ID"), max_length=80, db_index=True)
    username = models.CharField(_("username"), max_length=120, blank=True, db_index=True)
    first_name = models.CharField(_("first name"), max_length=120, blank=True)
    last_name = models.CharField(_("last name"), max_length=120, blank=True)
    display_name = models.CharField(_("display name"), max_length=160, blank=True)
    state = models.CharField(
        _("state"),
        max_length=40,
        choices=State.choices,
        default=State.IDLE,
        db_index=True,
    )
    state_data = models.JSONField(_("state data"), default=dict, blank=True)
    last_seen_at = models.DateTimeField(_("last seen at"), default=timezone.now, db_index=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)

    class Meta:
        verbose_name = _("bot user")
        verbose_name_plural = _("bot users")
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["bot_config", "provider_user_id"]),
            models.Index(fields=["bot_config", "chat_id"]),
            models.Index(fields=["bot_config", "state", "last_seen_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bot_config", "provider_user_id"],
                name="unique_bot_user_per_config",
            ),
        ]

    def __str__(self):
        return self.display_name or self.username or f"{self.bot_config.provider}:{self.provider_user_id}"

    def reset_state(self, *, save=True):
        self.state = self.State.IDLE
        self.state_data = {}
        if save:
            self.save(update_fields=["state", "state_data", "updated_at"])


class WebTelegramLinkToken(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        USED = "used", _("Used")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")

    class Source(models.TextChoices):
        DASHBOARD = "dashboard", _("Dashboard")
        ORDER_DETAIL = "order_detail", _("Order detail")
        AFTER_CHECKOUT = "after_checkout", _("After checkout")
        ADMIN = "admin", _("Admin")

    customer = models.ForeignKey(
        "Customer",
        verbose_name=_("customer"),
        on_delete=models.CASCADE,
        related_name="web_telegram_link_tokens",
    )
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True, db_index=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    bot_user = models.ForeignKey(
        BotUser,
        verbose_name=_("bot user"),
        on_delete=models.SET_NULL,
        related_name="web_link_tokens",
        null=True,
        blank=True,
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        default=default_web_telegram_link_token_expiry,
        db_index=True,
    )
    used_at = models.DateTimeField(_("used at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    source = models.CharField(
        _("source"),
        max_length=30,
        choices=Source.choices,
        default=Source.DASHBOARD,
        db_index=True,
    )
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("web telegram link token")
        verbose_name_plural = _("web telegram link tokens")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "status", "expires_at"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.customer_id}:{self.status}:{self.source}"


class SupportConversation(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        WAITING_ADMIN = "waiting_admin", _("Waiting for admin")
        ANSWERED = "answered", _("Answered")
        CLOSED = "closed", _("Closed")

    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.SET_NULL,
        related_name="support_conversations",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        "Customer",
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="support_conversations",
        null=True,
        blank=True,
    )
    subject = models.CharField(_("subject"), max_length=150, default=_("Support chat"))
    contact_value = models.CharField(
        _("phone number or Telegram ID"),
        max_length=120,
        blank=True,
        help_text=_("Customer phone number, Telegram username, or Telegram ID for follow-up."),
    )
    status = models.CharField(
        _("status"),
        max_length=30,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    last_customer_message_at = models.DateTimeField(_("last customer message at"), blank=True, null=True)
    last_admin_message_at = models.DateTimeField(_("last admin message at"), blank=True, null=True)
    closed_at = models.DateTimeField(_("closed at"), blank=True, null=True)

    class Meta:
        verbose_name = _("support conversation")
        verbose_name_plural = _("support conversations")
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["customer", "status", "updated_at"]),
            models.Index(fields=["store", "status", "updated_at"]),
        ]

    def __str__(self):
        label = self.contact_value or self.customer or _("Guest")
        return f"{self.pk or '-'} - {label}"

    def mark_waiting_admin(self, *, save=True):
        now = timezone.now()
        self.status = self.Status.WAITING_ADMIN
        self.last_customer_message_at = now
        self.closed_at = None
        if save:
            self.save(update_fields=["status", "last_customer_message_at", "closed_at", "updated_at"])

    def mark_answered(self, *, save=True):
        now = timezone.now()
        self.status = self.Status.ANSWERED
        self.last_admin_message_at = now
        if save:
            self.save(update_fields=["status", "last_admin_message_at", "updated_at"])

    def close(self, *, save=True):
        self.status = self.Status.CLOSED
        self.closed_at = timezone.now()
        if save:
            self.save(update_fields=["status", "closed_at", "updated_at"])


class SupportMessage(TimeStampedModel):
    class SenderType(models.TextChoices):
        CUSTOMER = "customer", _("Customer")
        ADMIN = "admin", _("Admin")
        SYSTEM = "system", _("System")

    conversation = models.ForeignKey(
        SupportConversation,
        verbose_name=_("support conversation"),
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_type = models.CharField(
        _("sender type"),
        max_length=20,
        choices=SenderType.choices,
        db_index=True,
    )
    customer = models.ForeignKey(
        "Customer",
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="support_messages",
        null=True,
        blank=True,
    )
    bot_config = models.ForeignKey(
        BotConfiguration,
        verbose_name=_("bot configuration"),
        on_delete=models.SET_NULL,
        related_name="support_messages",
        null=True,
        blank=True,
    )
    body = models.TextField(_("body"))
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("support message")
        verbose_name_plural = _("support messages")
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["sender_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_sender_type_display()} #{self.pk or '-'}"


class BotPendingAction(TimeStampedModel):
    class Action(models.TextChoices):
        REJECT_ORDER = "reject_order", _("Reject order")
        SUPPORT_REPLY = "support_reply", _("Support reply")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    bot_config = models.ForeignKey(
        BotConfiguration,
        verbose_name=_("bot configuration"),
        on_delete=models.CASCADE,
        related_name="pending_actions",
    )
    order = models.ForeignKey(
        "Order",
        verbose_name=_("order"),
        on_delete=models.CASCADE,
        related_name="bot_pending_actions",
        null=True,
        blank=True,
    )
    support_conversation = models.ForeignKey(
        SupportConversation,
        verbose_name=_("support conversation"),
        on_delete=models.CASCADE,
        related_name="bot_pending_actions",
        null=True,
        blank=True,
    )
    admin_user_id = models.CharField(_("admin user ID"), max_length=80, db_index=True)
    action = models.CharField(_("action"), max_length=30, choices=Action.choices)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    resolved_at = models.DateTimeField(_("resolved at"), blank=True, null=True)

    class Meta:
        verbose_name = _("bot pending action")
        verbose_name_plural = _("bot pending actions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["bot_config", "admin_user_id", "status", "created_at"]),
            models.Index(fields=["order", "status"]),
            models.Index(fields=["support_conversation", "status"]),
        ]

    def __str__(self):
        target = self.order_id or self.support_conversation_id or "-"
        return f"{self.get_action_display()} for {target}"

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at", "updated_at"])


class BotAdminOrderMessage(TimeStampedModel):
    class MessageKind(models.TextChoices):
        TEXT = "text", _("Text")
        PHOTO = "photo", _("Photo")

    bot_config = models.ForeignKey(
        BotConfiguration,
        verbose_name=_("bot configuration"),
        on_delete=models.CASCADE,
        related_name="admin_order_messages",
    )
    order = models.ForeignKey(
        "Order",
        verbose_name=_("order"),
        on_delete=models.CASCADE,
        related_name="bot_admin_messages",
    )
    admin_user_id = models.CharField(_("admin user ID"), max_length=80, db_index=True)
    chat_id = models.CharField(_("chat ID"), max_length=80, db_index=True)
    message_id = models.CharField(_("message ID"), max_length=80, db_index=True)
    message_kind = models.CharField(
        _("message kind"),
        max_length=20,
        choices=MessageKind.choices,
        default=MessageKind.TEXT,
    )
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("bot admin order message")
        verbose_name_plural = _("bot admin order messages")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["bot_config", "order", "admin_user_id"]),
            models.Index(fields=["order", "message_kind"]),
        ]

    def __str__(self):
        return f"{self.bot_config} order={self.order_id} admin={self.admin_user_id} message={self.message_id}"


class BotEventLog(TimeStampedModel):
    class EventType(models.TextChoices):
        NEW_ORDER = "new_order", _("New order")
        ORDER_APPROVED = "order_approved", _("Order approved")
        ORDER_REJECTED = "order_rejected", _("Order rejected")
        SUPPORT_MESSAGE = "support_message", _("Support message")
        SUPPORT_REPLY = "support_reply", _("Support reply")
        SALES_REPORT = "sales_report", _("Sales report")
        WEBHOOK = "webhook", _("Webhook")
        CALLBACK = "callback", _("Callback")
        ERROR = "error", _("Error")

    class Status(models.TextChoices):
        SUCCESS = "success", _("Success")
        SENT = "sent", _("Sent")
        RECEIVED = "received", _("Received")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    bot_config = models.ForeignKey(
        BotConfiguration,
        verbose_name=_("bot configuration"),
        on_delete=models.SET_NULL,
        related_name="event_logs",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(
        "Order",
        verbose_name=_("order"),
        on_delete=models.SET_NULL,
        related_name="bot_event_logs",
        null=True,
        blank=True,
    )
    event_type = models.CharField(_("event type"), max_length=30, choices=EventType.choices, db_index=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, db_index=True)
    message = models.TextField(_("message"), blank=True)
    raw_payload = models.JSONField(_("raw payload"), default=dict, blank=True)

    class Meta:
        verbose_name = _("bot event log")
        verbose_name_plural = _("bot event logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_type", "status", "created_at"]),
            models.Index(fields=["bot_config", "created_at"]),
            models.Index(fields=["order", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.get_status_display()}"


class RevenueOfferLog(models.Model):
    class EngineType(models.TextChoices):
        RENEWAL = "renewal", _("Renewal")
        UPSELL = "upsell", _("Upsell")
        RETENTION = "retention", _("Retention")
        SILENT_ACTIVE = "silent_active", _("Silent active")
        AI_OPTIMIZER = "ai_optimizer", _("AI optimizer")

    class DecisionSource(models.TextChoices):
        RULE = "rule", _("Rule")
        OPTIMIZATION = "optimization", _("Optimization")
        AI = "ai", _("AI")
        FALLBACK = "fallback", _("Fallback")

    class Status(models.TextChoices):
        DRY_RUN = "dry_run", _("Dry run")
        SENT = "sent", _("Sent")
        SKIPPED = "skipped", _("Skipped")
        SUPPRESSED = "suppressed", _("Suppressed")
        FAILED = "failed", _("Failed")
        CONVERTED = "converted", _("Converted")

    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.SET_NULL,
        related_name="revenue_offer_logs",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        "Customer",
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="revenue_offer_logs",
        null=True,
        blank=True,
    )
    bot_user = models.ForeignKey(
        BotUser,
        verbose_name=_("bot user"),
        on_delete=models.SET_NULL,
        related_name="revenue_offer_logs",
        null=True,
        blank=True,
    )
    vpn_client = models.ForeignKey(
        "VPNClient",
        verbose_name=_("VPN client"),
        on_delete=models.SET_NULL,
        related_name="revenue_offer_logs",
        null=True,
        blank=True,
    )
    engine_type = models.CharField(
        _("engine type"),
        max_length=30,
        choices=EngineType.choices,
        db_index=True,
    )
    event_type = models.CharField(_("event type"), max_length=80, db_index=True)
    offer_type = models.CharField(_("offer type"), max_length=80, db_index=True)
    variant = models.CharField(_("variant"), max_length=40, blank=True)
    decision_source = models.CharField(
        _("decision source"),
        max_length=30,
        choices=DecisionSource.choices,
        default=DecisionSource.RULE,
        db_index=True,
    )
    status = models.CharField(
        _("status"),
        max_length=30,
        choices=Status.choices,
        default=Status.SKIPPED,
        db_index=True,
    )
    skip_reason = models.CharField(_("skip reason"), max_length=120, blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    ai_confidence = models.DecimalField(
        _("AI confidence"),
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    predicted_purchase_probability = models.DecimalField(
        _("predicted purchase probability"),
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    converted_at = models.DateTimeField(_("converted at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), default=timezone.now, db_index=True, editable=False)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("revenue offer log")
        verbose_name_plural = _("revenue offer logs")
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["engine_type", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.engine_type}:{self.offer_type}:{self.status}"


class Plan(TimeStampedModel):
    class Currency(models.TextChoices):
        TOMAN = "TOMAN", _("Toman")
        IRR = "IRR", _("Iranian Rial")
        USD = "USD", _("US Dollar")

    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="plans",
        null=True,
        blank=True,
    )
    operators = models.ManyToManyField(
        Operator,
        verbose_name=_("operators"),
        related_name="plans",
        blank=True,
        help_text=_("Operators that can buy this plan when operator-based sales mode is enabled."),
    )
    name = models.CharField(_("name"), max_length=100)
    slug = models.SlugField(_("slug"), max_length=100, blank=True)
    description = models.TextField(_("description"), blank=True)
    volume_gb = models.DecimalField(
        _("volume in GB"),
        max_digits=8,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
        help_text=_("Traffic limit in GB. Use decimal values for small plans, e.g. 0.1 for 100 MB."),
    )
    duration_days = models.PositiveIntegerField(_("duration in days"), help_text=_("Plan duration in days."))
    price = models.PositiveIntegerField(_("price"), help_text=_("Plan price in the selected currency."))
    currency = models.CharField(
        _("currency"),
        max_length=10,
        choices=Currency.choices,
        default=Currency.TOMAN,
    )
    device_limit = models.PositiveIntegerField(_("device limit"), default=2)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    sort_order = models.PositiveIntegerField(_("sort order"), default=0, db_index=True)
    is_public = models.BooleanField(
        _("is public"),
        default=True,
        db_index=True,
        help_text=_("Hide internal reward plans from the public plan selector when disabled."),
    )
    is_custom_volume = models.BooleanField(
        _("is custom volume"),
        default=False,
        db_index=True,
        help_text=_("Generated internal plan for custom-volume purchases."),
    )

    class Meta:
        verbose_name = _("plan")
        verbose_name_plural = _("plans")
        ordering = ["sort_order", "price", "id"]
        indexes = [
            models.Index(fields=["store", "is_active", "is_public", "sort_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "slug"],
                name="unique_plan_slug_per_store",
                condition=~models.Q(slug=""),
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.price} {self.currency}"

    @property
    def traffic_limit_bytes(self):
        return int((self.volume_gb or Decimal("0")) * Decimal(1024 ** 3))

    def is_available_for_operator(self, operator):
        if not operator:
            return False
        return self.operators.filter(pk=operator.pk, is_active=True).exists()


class DiscountCode(TimeStampedModel):
    class DiscountType(models.TextChoices):
        PERCENTAGE = "percentage", _("Percentage")
        FIXED = "fixed", _("Fixed amount")

    code = models.CharField(_("code"), max_length=50, unique=True, db_index=True)
    discount_type = models.CharField(
        _("discount type"),
        max_length=20,
        choices=DiscountType.choices,
        default=DiscountType.PERCENTAGE,
    )
    value = models.PositiveIntegerField(
        _("value"),
        validators=[MinValueValidator(1)],
        help_text=_("Percentage value or fixed amount in the order currency."),
    )
    max_usage = models.PositiveIntegerField(_("maximum usage"), blank=True, null=True)
    used_count = models.PositiveIntegerField(_("used count"), default=0)
    valid_from = models.DateTimeField(_("valid from"), blank=True, null=True)
    valid_until = models.DateTimeField(_("valid until"), blank=True, null=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    applicable_plans = models.ManyToManyField(
        Plan,
        verbose_name=_("applicable plans"),
        related_name="discount_codes",
        blank=True,
        help_text=_("Leave empty to allow this code for every active plan."),
    )

    class Meta:
        verbose_name = _("discount code")
        verbose_name_plural = _("discount codes")
        ordering = ["code"]
        indexes = [
            models.Index(fields=["code", "is_active"]),
            models.Index(fields=["valid_from", "valid_until"]),
        ]

    def __str__(self):
        return self.code

    def clean(self):
        super().clean()
        if self.discount_type == self.DiscountType.PERCENTAGE and self.value > 100:
            raise ValidationError({"value": _("Percentage discounts cannot exceed 100.")})
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError({"valid_until": _("End date must be after start date.")})

    def save(self, *args, **kwargs):
        self.code = self.normalize_code(self.code)
        super().save(*args, **kwargs)

    @staticmethod
    def normalize_code(code):
        return (code or "").strip().upper()

    def is_within_valid_window(self, now=None):
        now = now or timezone.now()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True

    def has_usage_available(self):
        return self.max_usage is None or self.used_count < self.max_usage

    def is_applicable_to_plan(self, plan):
        if not self.applicable_plans.exists():
            return True
        return self.applicable_plans.filter(pk=plan.pk).exists()

    def validate_basic(self, now=None):
        if self.discount_type == self.DiscountType.PERCENTAGE and self.value > 100:
            raise ValidationError(_("Percentage discounts cannot exceed 100."))
        if not self.is_active:
            raise ValidationError(_("Discount code is not active."))
        if not self.is_within_valid_window(now=now):
            raise ValidationError(_("Discount code is not valid right now."))
        if not self.has_usage_available():
            raise ValidationError(_("Discount code usage limit has been reached."))

    def validate_for_plan(self, plan, now=None):
        self.validate_basic(now=now)
        if not self.is_applicable_to_plan(plan):
            raise ValidationError(_("Discount code is not available for this plan."))

    def calculate_discount(self, amount):
        if amount <= 0:
            return 0
        if self.discount_type == self.DiscountType.PERCENTAGE:
            return min((amount * self.value) // 100, amount)
        return min(self.value, amount)


class Customer(TimeStampedModel):
    public_id = models.UUIDField(_("public ID"), default=generate_public_id, editable=False, unique=True)
    username = models.CharField(_("username"), max_length=80, blank=True, db_index=True)
    phone_number = models.CharField(_("phone number"), max_length=30, blank=True, db_index=True)
    display_name = models.CharField(_("display name"), max_length=120, blank=True)
    is_wholesale = models.BooleanField(_("is wholesale"), default=False, db_index=True)
    default_discount_percent = models.PositiveIntegerField(
        _("default discount percent"),
        null=True,
        blank=True,
        validators=[MaxValueValidator(100)],
        help_text=_("Permanent wholesale discount percentage used when no coupon code is applied."),
    )
    referral_code = models.CharField(_("referral code"), max_length=16, unique=True, blank=True, db_index=True)
    referred_by = models.ForeignKey(
        "self",
        verbose_name=_("referred by"),
        on_delete=models.SET_NULL,
        related_name="referred_customers",
        null=True,
        blank=True,
    )
    referral_code_used = models.CharField(_("referral code used"), max_length=16, blank=True)
    referred_at = models.DateTimeField(_("referred at"), blank=True, null=True)
    first_ip = models.GenericIPAddressField(_("first IP"), blank=True, null=True)
    last_ip = models.GenericIPAddressField(_("last IP"), blank=True, null=True)
    user_agent_hash = models.CharField(_("user agent hash"), max_length=64, blank=True)
    last_seen_at = models.DateTimeField(_("last seen at"), default=timezone.now, db_index=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)

    class Meta:
        verbose_name = _("customer")
        verbose_name_plural = _("customers")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["public_id", "is_active"]),
            models.Index(fields=["referral_code"]),
            models.Index(fields=["referred_by", "created_at"]),
            models.Index(fields=["last_seen_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["username"],
                name="unique_customer_username_when_set",
                condition=~models.Q(username=""),
            ),
            models.UniqueConstraint(
                fields=["phone_number"],
                name="unique_customer_phone_when_set",
                condition=~models.Q(phone_number=""),
            ),
        ]

    def __str__(self):
        return self.display_name or f"Customer {str(self.public_id)[:8]}"

    def clean(self):
        super().clean()
        if self.pk and self.referred_by_id == self.pk:
            raise ValidationError({"referred_by": _("A customer cannot refer themselves.")})

    def save(self, *args, **kwargs):
        if self.username:
            self.username = self.username.strip()
        if self.phone_number:
            self.phone_number = self.phone_number.strip()
        if self.pk:
            if self.referred_by_id == self.pk:
                raise ValidationError({"referred_by": _("A customer cannot refer themselves.")})
            old_referred_by_id = (
                Customer.objects.filter(pk=self.pk)
                .values_list("referred_by_id", flat=True)
                .first()
            )
            if old_referred_by_id and old_referred_by_id != self.referred_by_id:
                raise ValidationError({"referred_by": _("Referrer cannot be changed once set.")})
        if not self.referral_code:
            self.referral_code = self.generate_unique_referral_code()
        if not self.display_name and self.public_id:
            self.display_name = self.username or self.phone_number or f"Customer {str(self.public_id)[:8]}"
        super().save(*args, **kwargs)

    @property
    def orders_count(self):
        return self.orders.count()

    @property
    def wholesale_discount_percent(self):
        if not self.is_wholesale or not self.default_discount_percent:
            return 0
        return max(min(int(self.default_discount_percent), 100), 0)

    @classmethod
    def generate_unique_referral_code(cls):
        for _ in range(20):
            code = generate_short_code("RF", 8)
            if not cls.objects.filter(referral_code=code).exists():
                return code
        return f"RF{uuid.uuid4().hex[:10].upper()}"

    @property
    def invited_count(self):
        return self.referrals_made.count()

    @property
    def successful_referrals_count(self):
        return self.referrals_made.filter(status=Referral.Status.PURCHASED).count()


class CustomerAnalyticsReport(Customer):
    class Meta:
        proxy = True
        verbose_name = _("customer analytics report")
        verbose_name_plural = _("customer analytics reports")
        ordering = ["-created_at"]


class BroadcastMessage(TimeStampedModel):
    class AudienceType(models.TextChoices):
        ALL = "all", _("All")
        ACTIVE_CUSTOMERS = "active_customers", _("Active customers")
        CUSTOMERS_WITH_ACTIVE_CONFIG = "customers_with_active_config", _("Customers with active config")
        CUSTOMERS_WITHOUT_ORDER = "customers_without_order", _("Customers without order")
        LEGACY_WIZWIZ_IMPORTED = "legacy_wizwiz_imported", _("کاربران قدیمی WizWiz")
        LOYAL = "loyal", _("Loyal")
        GOOD = "good", _("Good")
        TOP_BUYER = "top_buyer", _("Top buyer")
        TOP_REFERRER = "top_referrer", _("Top referrer")
        INACTIVE = "inactive", _("Inactive")
        NO_ORDER = "no_order", _("No order")

    class Channel(models.TextChoices):
        TELEGRAM = "telegram", _("Telegram")
        BALE = "bale", _("Bale")
        ALL_AVAILABLE = "all_available", _("All available")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        QUEUED = "queued", _("Queued")
        SENDING = "sending", _("Sending")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.SET_NULL,
        related_name="broadcast_messages",
        null=True,
        blank=True,
    )
    title = models.CharField(_("title"), max_length=180)
    message_text = models.TextField(_("message text"))
    audience_type = models.CharField(
        _("audience type"),
        max_length=40,
        choices=AudienceType.choices,
        default=AudienceType.ALL,
        db_index=True,
    )
    channel = models.CharField(
        _("channel"),
        max_length=30,
        choices=Channel.choices,
        default=Channel.TELEGRAM,
        db_index=True,
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    scheduled_at = models.DateTimeField(_("scheduled at"), blank=True, null=True)
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)
    total_recipients = models.PositiveIntegerField(_("total recipients"), default=0)
    success_count = models.PositiveIntegerField(_("success count"), default=0)
    failed_count = models.PositiveIntegerField(_("failed count"), default=0)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("broadcast message")
        verbose_name_plural = _("broadcast messages")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["audience_type", "status"]),
            models.Index(fields=["channel", "status"]),
            models.Index(fields=["scheduled_at", "status"]),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if not (self.title or "").strip():
            raise ValidationError({"title": _("Title is required.")})
        if not (self.message_text or "").strip():
            raise ValidationError({"message_text": _("Message text is required.")})

    def save(self, *args, **kwargs):
        self.title = (self.title or "").strip()
        self.message_text = (self.message_text or "").strip()
        super().save(*args, **kwargs)


class BroadcastRecipient(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    campaign = models.ForeignKey(
        BroadcastMessage,
        verbose_name=_("campaign"),
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.CASCADE,
        related_name="broadcast_recipients",
    )
    channel = models.CharField(_("channel"), max_length=30, choices=BroadcastMessage.Channel.choices, db_index=True)
    target_identifier = models.CharField(_("target identifier"), max_length=120, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    error_message = models.TextField(_("error message"), blank=True)
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)

    class Meta:
        verbose_name = _("broadcast recipient")
        verbose_name_plural = _("broadcast recipients")
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["customer", "channel"]),
            models.Index(fields=["channel", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "customer", "channel"],
                name="unique_broadcast_recipient_per_channel",
            ),
        ]

    def __str__(self):
        return f"{self.campaign_id}:{self.customer_id}:{self.channel}"


class LegacyWizWizImportJob(TimeStampedModel):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", _("Uploaded")
        ANALYZING = "analyzing", _("Analyzing")
        ANALYZED = "analyzed", _("Analyzed")
        APPLYING = "applying", _("Applying")
        APPLIED = "applied", _("Applied")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class Mode(models.TextChoices):
        USERS_ONLY = "users_only", _("Users only")

    source = models.CharField(_("source"), max_length=40, default="wizwiz", db_index=True)
    title = models.CharField(_("title"), max_length=180, blank=True)
    uploaded_file = models.FileField(
        _("uploaded file"),
        upload_to="private/legacy_imports/wizwiz/",
    )
    original_filename = models.CharField(_("original filename"), max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(_("file size"), default=0)
    file_sha256 = models.CharField(_("file SHA256"), max_length=64, db_index=True, blank=True)

    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
        db_index=True,
    )
    mode = models.CharField(
        _("mode"),
        max_length=20,
        choices=Mode.choices,
        default=Mode.USERS_ONLY,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_jobs",
        null=True,
        blank=True,
    )
    analyzed_at = models.DateTimeField(_("analyzed at"), blank=True, null=True)
    applied_at = models.DateTimeField(_("applied at"), blank=True, null=True)
    failed_at = models.DateTimeField(_("failed at"), blank=True, null=True)

    parsed_users_count = models.PositiveIntegerField(_("parsed users count"), default=0)
    valid_users_count = models.PositiveIntegerField(_("valid users count"), default=0)
    invalid_rows_count = models.PositiveIntegerField(_("invalid rows count"), default=0)
    duplicate_in_file_count = models.PositiveIntegerField(_("duplicate in file count"), default=0)
    existing_bot_users_count = models.PositiveIntegerField(_("existing bot users count"), default=0)
    existing_customers_count = models.PositiveIntegerField(_("existing customers count"), default=0)
    would_create_bot_users_count = models.PositiveIntegerField(_("would create bot users count"), default=0)
    would_create_customers_count = models.PositiveIntegerField(_("would create customers count"), default=0)
    admins_count = models.PositiveIntegerField(_("admins count"), default=0)
    agents_count = models.PositiveIntegerField(_("agents count"), default=0)
    wallet_positive_count = models.PositiveIntegerField(_("wallet positive count"), default=0)

    created_bot_users_count = models.PositiveIntegerField(_("created bot users count"), default=0)
    created_customers_count = models.PositiveIntegerField(_("created customers count"), default=0)
    linked_existing_count = models.PositiveIntegerField(_("linked existing count"), default=0)
    updated_existing_count = models.PositiveIntegerField(_("updated existing count"), default=0)
    skipped_count = models.PositiveIntegerField(_("skipped count"), default=0)
    failed_count = models.PositiveIntegerField(_("failed count"), default=0)

    skip_admins = models.BooleanField(_("skip admins"), default=True)
    import_agents = models.BooleanField(_("import agents"), default=True)
    only_agents = models.BooleanField(_("only agents"), default=False)
    only_wallet_positive = models.BooleanField(_("only wallet positive"), default=False)
    update_existing = models.BooleanField(_("update existing"), default=False)
    create_customers = models.BooleanField(_("create customers"), default=True)

    error_message = models.TextField(_("error message"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("legacy WizWiz import job")
        verbose_name_plural = _("legacy WizWiz import jobs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source", "status", "created_at"]),
            models.Index(fields=["file_sha256"]),
        ]

    def __str__(self):
        label = self.title or self.original_filename or f"Job {self.pk}"
        return f"{label} ({self.get_status_display()})"


class LegacyWizWizImportRow(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        WOULD_CREATE = "would_create", _("Would create")
        WOULD_LINK_EXISTING = "would_link_existing", _("Would link existing")
        EXISTING = "existing", _("Existing")
        CREATED = "created", _("Created")
        LINKED = "linked", _("Linked")
        UPDATED = "updated", _("Updated")
        SKIPPED = "skipped", _("Skipped")
        INVALID = "invalid", _("Invalid")
        FAILED = "failed", _("Failed")

    job = models.ForeignKey(
        LegacyWizWizImportJob,
        verbose_name=_("job"),
        on_delete=models.CASCADE,
        related_name="rows",
    )
    source = models.CharField(_("source"), max_length=40, default="wizwiz", db_index=True)
    legacy_pk = models.CharField(_("legacy PK"), max_length=80, blank=True)
    telegram_user_id = models.CharField(_("Telegram user ID"), max_length=80, db_index=True)
    telegram_user_id_masked = models.CharField(_("masked Telegram user ID"), max_length=80, blank=True)
    old_name_masked = models.CharField(_("old masked name"), max_length=160, blank=True)
    old_username = models.CharField(_("old username"), max_length=120, blank=True)
    old_phone_masked = models.CharField(_("old masked phone"), max_length=80, blank=True)
    old_wallet = models.IntegerField(_("old wallet"), null=True, blank=True)
    old_is_admin = models.BooleanField(_("old is admin"), default=False)
    old_is_agent = models.BooleanField(_("old is agent"), default=False)
    old_freetrial = models.CharField(_("old free trial"), max_length=120, blank=True)
    old_refcode = models.CharField(_("old refcode"), max_length=120, blank=True)
    old_refered_by = models.CharField(_("old referred by"), max_length=120, blank=True)
    status = models.CharField(
        _("status"),
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    reason = models.CharField(_("reason"), max_length=255, blank=True)
    bot_user = models.ForeignKey(
        BotUser,
        verbose_name=_("bot user"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_rows",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_rows",
        null=True,
        blank=True,
    )
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("legacy WizWiz import row")
        verbose_name_plural = _("legacy WizWiz import rows")
        ordering = ["job_id", "created_at", "id"]
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["source", "status"]),
            models.Index(fields=["telegram_user_id"]),
            models.Index(fields=["customer", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "telegram_user_id"],
                name="unique_legacy_wizwiz_row_per_job_user",
            ),
        ]

    def __str__(self):
        return f"{self.job_id}:{self.telegram_user_id_masked or self.telegram_user_id}:{self.status}"


class LegacyWizWizImportMessageBatch(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENDING = "sending", _("Sending")
        SENT = "sent", _("Sent")
        PARTIAL = "partial", _("Partial")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    job = models.ForeignKey(
        LegacyWizWizImportJob,
        verbose_name=_("job"),
        on_delete=models.CASCADE,
        related_name="message_batches",
    )
    text = models.TextField(_("message text"))
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_message_batches",
        null=True,
        blank=True,
    )
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)
    total_recipients = models.PositiveIntegerField(_("total recipients"), default=0)
    sent_count = models.PositiveIntegerField(_("sent count"), default=0)
    failed_count = models.PositiveIntegerField(_("failed count"), default=0)
    skipped_count = models.PositiveIntegerField(_("skipped count"), default=0)
    blocked_count = models.PositiveIntegerField(_("blocked count"), default=0)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("legacy WizWiz import message batch")
        verbose_name_plural = _("legacy WizWiz import message batches")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.job_id}:{self.status}:{self.total_recipients}"


class LegacyWizWizImportMessageRecipient(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")
        BLOCKED = "blocked", _("Blocked")

    batch = models.ForeignKey(
        LegacyWizWizImportMessageBatch,
        verbose_name=_("batch"),
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    row = models.ForeignKey(
        LegacyWizWizImportRow,
        verbose_name=_("import row"),
        on_delete=models.CASCADE,
        related_name="message_recipients",
    )
    bot_user = models.ForeignKey(
        BotUser,
        verbose_name=_("bot user"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_message_recipients",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="legacy_wizwiz_import_message_recipients",
        null=True,
        blank=True,
    )
    telegram_user_id_masked = models.CharField(_("masked Telegram user ID"), max_length=80, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    error_message = models.TextField(_("error message"), blank=True)
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("legacy WizWiz import message recipient")
        verbose_name_plural = _("legacy WizWiz import message recipients")
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["batch", "status"]),
            models.Index(fields=["row", "status"]),
            models.Index(fields=["customer", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row"],
                name="unique_legacy_wizwiz_message_recipient_per_row",
            ),
        ]

    def __str__(self):
        return f"{self.batch_id}:{self.telegram_user_id_masked}:{self.status}"


class Referral(TimeStampedModel):
    class Status(models.TextChoices):
        REGISTERED = "registered", _("Registered")
        PURCHASED = "purchased", _("Purchased")
        CANCELLED = "cancelled", _("Cancelled")

    referrer = models.ForeignKey(
        Customer,
        verbose_name=_("referrer"),
        on_delete=models.CASCADE,
        related_name="referrals_made",
    )
    referred_customer = models.OneToOneField(
        Customer,
        verbose_name=_("referred customer"),
        on_delete=models.CASCADE,
        related_name="referral_received",
    )
    referral_code = models.CharField(_("referral code"), max_length=16, db_index=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.REGISTERED,
        db_index=True,
    )
    first_order = models.ForeignKey(
        "Order",
        verbose_name=_("first order"),
        on_delete=models.SET_NULL,
        related_name="referral_conversions",
        null=True,
        blank=True,
    )
    purchased_at = models.DateTimeField(_("purchased at"), blank=True, null=True)

    class Meta:
        verbose_name = _("referral")
        verbose_name_plural = _("referrals")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["referrer", "status", "created_at"]),
            models.Index(fields=["referral_code", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["referrer", "referred_customer"],
                name="unique_referral_pair",
            ),
        ]

    def __str__(self):
        return f"{self.referrer} -> {self.referred_customer}"


class CustomerReward(TimeStampedModel):
    class RewardType(models.TextChoices):
        DISCOUNT_20 = "discount_20", _("20% discount coupon")
        FREE_1GB_PLAN = "free_1gb_plan", _("Free 1GB plan")

    class Status(models.TextChoices):
        AVAILABLE = "available", _("Available")
        USED = "used", _("Used")
        CANCELLED = "cancelled", _("Cancelled")

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.CASCADE,
        related_name="rewards",
    )
    referral = models.ForeignKey(
        Referral,
        verbose_name=_("referral"),
        on_delete=models.SET_NULL,
        related_name="rewards",
        null=True,
        blank=True,
    )
    reward_type = models.CharField(_("reward type"), max_length=30, choices=RewardType.choices, db_index=True)
    title = models.CharField(_("title"), max_length=150)
    description = models.TextField(_("description"), blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )
    discount_code = models.ForeignKey(
        DiscountCode,
        verbose_name=_("discount code"),
        on_delete=models.SET_NULL,
        related_name="customer_rewards",
        null=True,
        blank=True,
    )
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("plan"),
        on_delete=models.SET_NULL,
        related_name="customer_rewards",
        null=True,
        blank=True,
    )
    milestone = models.PositiveIntegerField(_("milestone"), blank=True, null=True)
    earned_at = models.DateTimeField(_("earned at"), default=timezone.now, db_index=True)
    used_at = models.DateTimeField(_("used at"), blank=True, null=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("customer reward")
        verbose_name_plural = _("customer rewards")
        ordering = ["-earned_at"]
        indexes = [
            models.Index(fields=["customer", "status", "earned_at"]),
            models.Index(fields=["reward_type", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "referral", "reward_type"],
                name="unique_reward_per_referral_type",
                condition=models.Q(referral__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["customer", "reward_type", "milestone"],
                name="unique_reward_per_referral_milestone",
                condition=models.Q(milestone__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.customer} - {self.title}"


class ReferralRewardLedger(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        AVAILABLE = "available", _("Available")
        REDEEMED = "redeemed", _("Redeemed")
        CANCELLED = "cancelled", _("Cancelled")

    inviter = models.ForeignKey(
        Customer,
        verbose_name=_("inviter"),
        on_delete=models.CASCADE,
        related_name="referral_gb_rewards",
    )
    invited = models.ForeignKey(
        Customer,
        verbose_name=_("invited customer"),
        on_delete=models.CASCADE,
        related_name="invited_gb_rewards",
    )
    order = models.ForeignKey(
        "Order",
        verbose_name=_("order"),
        on_delete=models.CASCADE,
        related_name="referral_reward_ledgers",
    )
    reward_gb = models.DecimalField(
        _("reward traffic GB"),
        max_digits=8,
        decimal_places=3,
        help_text=_("Traffic amount for this gift package. Kept as reward_gb for legacy compatibility."),
    )
    reward_duration_days = models.PositiveIntegerField(
        _("reward duration days"),
        default=30,
        validators=[MinValueValidator(1)],
        help_text=_("Duration amount for this gift package."),
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    redeemed_config = models.ForeignKey(
        "VPNClient",
        verbose_name=_("redeemed VPN config"),
        on_delete=models.SET_NULL,
        related_name="redeemed_referral_rewards",
        null=True,
        blank=True,
    )
    available_at = models.DateTimeField(_("available at"), blank=True, null=True)
    redeemed_at = models.DateTimeField(_("redeemed at"), blank=True, null=True)
    applied_traffic_gb = models.DecimalField(
        _("applied traffic GB"),
        max_digits=8,
        decimal_places=3,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Traffic actually applied when this package was redeemed."),
    )
    applied_duration_days = models.PositiveIntegerField(
        _("applied duration days"),
        default=0,
        help_text=_("Duration actually applied when this package was redeemed."),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("referral reward ledger")
        verbose_name_plural = _("referral reward ledger")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["inviter", "status", "created_at"]),
            models.Index(fields=["invited", "status"]),
            models.Index(fields=["order", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["invited"],
                name="unique_referral_gb_reward_per_invited_customer",
            ),
            models.CheckConstraint(
                condition=~models.Q(inviter=models.F("invited")),
                name="referral_gb_reward_no_self_invite",
            ),
        ]

    def __str__(self):
        return f"{self.inviter} <- {self.reward_gb}GB/{self.reward_duration_days}d from {self.invited}"

    @property
    def reward_traffic_gb(self):
        return self.reward_gb

    @reward_traffic_gb.setter
    def reward_traffic_gb(self, value):
        self.reward_gb = value


class Panel(TimeStampedModel):
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="panels",
        null=True,
        blank=True,
    )
    name = models.CharField(_("name"), max_length=100)
    url = models.URLField(_("URL"), help_text=_("Full panel URL without trailing slash."))
    username = models.CharField(_("username"), max_length=100)
    password = models.CharField(_("password"), max_length=100)
    proxy_url = models.URLField(
        _("HTTP proxy URL"),
        max_length=500,
        blank=True,
        null=True,
        validators=[URLValidator(schemes=["http", "https"])],
        help_text=_("Optional HTTP proxy URL for this panel, e.g. http://proxy-host:port."),
    )
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    last_sync_at = models.DateTimeField(_("last sync at"), blank=True, null=True)

    class Meta:
        verbose_name = _("panel")
        verbose_name_plural = _("panels")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["store", "is_active"]),
        ]

    def __str__(self):
        return self.name


class Inbound(TimeStampedModel):
    class Protocol(models.TextChoices):
        VLESS = "vless", _("VLESS")
        VMESS = "vmess", _("VMESS")
        TROJAN = "trojan", _("Trojan")

    class NetworkType(models.TextChoices):
        TCP = "tcp", _("TCP")
        WS = "ws", _("WebSocket")
        GRPC = "grpc", _("gRPC")

    class Security(models.TextChoices):
        NONE = "none", _("None")
        TLS = "tls", _("TLS")
        REALITY = "reality", _("Reality")

    panel = models.ForeignKey(Panel, verbose_name=_("panel"), on_delete=models.CASCADE, related_name="inbounds")
    inbound_id = models.PositiveIntegerField(_("inbound ID"), help_text=_("Inbound ID in Sanaei/X-UI."))
    remark = models.CharField(_("remark"), max_length=150, blank=True)
    protocol = models.CharField(
        _("protocol"),
        max_length=20,
        choices=Protocol.choices,
        default=Protocol.VLESS,
    )
    server_ip = models.CharField(_("server IP"), max_length=100)
    port = models.CharField(_("port"), max_length=10)
    config_params = models.CharField(_("configuration parameters"), max_length=500)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    available_for_new_orders = models.BooleanField(
        _("available for new orders"),
        default=True,
        db_index=True,
        help_text=_("اگر غیرفعال باشد، این inbound برای فروش جدید، تست رایگان یا ساخت کانفیگ جدید انتخاب نمی‌شود."),
    )
    health_monitor_enabled = models.BooleanField(
        _("health monitor enabled"),
        default=True,
        db_index=True,
        help_text=_("اگر غیرفعال باشد، سلامت پنل این inbound را بررسی نمی‌کند؛ مناسب inboundهای legacy."),
    )
    legacy_note = models.TextField(
        _("legacy note"),
        blank=True,
        help_text=_("توضیح ادمین برای inboundهای قدیمی که برای سفارش‌ها/کلاینت‌های قبلی نگه داشته شده‌اند."),
    )

    current_users = models.PositiveIntegerField(_("current users"), default=0)
    max_clients = models.PositiveIntegerField(_("maximum clients"), blank=True, null=True)

    network_type = models.CharField(
        _("network type"),
        max_length=20,
        choices=NetworkType.choices,
        default=NetworkType.TCP,
    )
    security = models.CharField(
        _("security"),
        max_length=20,
        choices=Security.choices,
        default=Security.NONE,
    )
    sni = models.CharField(_("SNI"), max_length=100, blank=True, null=True)
    fingerprint = models.CharField(_("fingerprint"), max_length=50, default="chrome", blank=True, null=True)

    pbk = models.CharField(_("public key"), max_length=100, blank=True, null=True)
    sid = models.CharField(_("short ID"), max_length=50, blank=True, null=True)

    ws_path = models.CharField(_("WebSocket path"), max_length=100, blank=True, null=True)
    ws_host = models.CharField(_("WebSocket host"), max_length=100, blank=True, null=True)
    last_synced_at = models.DateTimeField(_("last synced at"), blank=True, null=True)

    class Meta:
        verbose_name = _("inbound")
        verbose_name_plural = _("inbounds")
        ordering = ["panel__name", "inbound_id"]
        indexes = [
            models.Index(fields=["panel", "is_active"]),
            models.Index(fields=["panel", "is_active", "available_for_new_orders"]),
            models.Index(fields=["panel", "is_active", "health_monitor_enabled"]),
            models.Index(fields=["protocol", "network_type", "security"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["panel", "inbound_id"],
                name="unique_inbound_id_per_panel",
            ),
        ]

    def __str__(self):
        label = self.remark or f"Inbound {self.inbound_id}"
        panel_name = getattr(getattr(self, "panel", None), "name", None) or _("بدون پنل")
        return f"{panel_name} - {label}"

    @property
    def xui_inbound_id(self):
        return self.inbound_id

    def clean(self):
        super().clean()
        errors = {}
        panel = None
        try:
            panel = self.panel
        except (Panel.DoesNotExist, AttributeError):
            panel = None

        if not panel:
            errors["panel"] = _("هر inbound باید به یک پنل مشخص وصل باشد.")
        elif self.is_active and not panel.is_active:
            errors["is_active"] = _("اینباند فعال نمی‌تواند به پنل غیرفعال وصل باشد.")

        if errors:
            raise ValidationError(errors)

    @property
    def available_capacity(self):
        if self.max_clients is None:
            return None
        return max(self.max_clients - self.current_users, 0)

    @property
    def has_capacity(self):
        return self.max_clients is None or self.current_users < self.max_clients


class PlanInboundRoute(TimeStampedModel):
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="plan_inbound_routes",
        null=True,
        blank=True,
        help_text=_("برای پلن‌های مشترک بین فروشگاه‌ها می‌توان route را به یک فروشگاه خاص محدود کرد."),
    )
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("plan"),
        on_delete=models.CASCADE,
        related_name="inbound_routes",
        help_text=_("پلنی که فروش آن باید از inbound مشخص انجام شود."),
    )
    operator = models.ForeignKey(
        Operator,
        verbose_name=_("operator"),
        on_delete=models.CASCADE,
        related_name="plan_inbound_routes",
        null=True,
        blank=True,
        help_text=_("اگر خالی باشد route عمومی پلن است؛ اگر مقدار داشته باشد فقط برای همان اپراتور استفاده می‌شود."),
    )
    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.PROTECT,
        related_name="plan_routes",
        help_text=_("Inbound مقصد برای ساخت کانفیگ‌های جدید این پلن."),
    )
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
    priority = models.PositiveIntegerField(
        _("priority"),
        default=100,
        db_index=True,
        help_text=_("عدد کمتر یعنی اولویت بالاتر."),
    )
    weight = models.PositiveIntegerField(
        _("weight"),
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_("برای تعادل فروش آینده نگه داشته شده است."),
    )
    note = models.TextField(_("note"), blank=True)

    class Meta:
        verbose_name = _("plan inbound route")
        verbose_name_plural = _("plan inbound routes")
        ordering = ["plan", "operator", "priority", "id"]
        indexes = [
            models.Index(fields=["store", "is_active", "priority"]),
            models.Index(fields=["plan", "operator", "is_active", "priority"]),
            models.Index(fields=["inbound", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "operator", "inbound"],
                condition=models.Q(operator__isnull=False),
                name="unique_plan_operator_inbound_route",
            ),
            models.UniqueConstraint(
                fields=["plan", "inbound"],
                condition=models.Q(operator__isnull=True),
                name="unique_plan_default_inbound_route",
            ),
        ]

    def __str__(self):
        operator = self.operator.name if self.operator_id else _("عمومی")
        return f"{self.plan} / {operator} -> {self.inbound}"

    def clean(self):
        super().clean()
        errors = {}
        plan = None
        operator = None
        inbound = None
        store = None
        panel = None

        try:
            plan = self.plan
        except (Plan.DoesNotExist, AttributeError):
            plan = None
        try:
            operator = self.operator
        except (Operator.DoesNotExist, AttributeError):
            operator = None
        try:
            inbound = self.inbound
        except (Inbound.DoesNotExist, AttributeError):
            inbound = None
        try:
            store = self.store
        except (Store.DoesNotExist, AttributeError):
            store = None

        if inbound:
            try:
                panel = inbound.panel
            except (Panel.DoesNotExist, AttributeError):
                panel = None

        if store and plan and plan.store_id and plan.store_id != store.pk:
            errors["store"] = _("فروشگاه route باید با فروشگاه پلن یکی باشد.")
        if store and operator and operator.store_id and operator.store_id != store.pk:
            errors["operator"] = _("اپراتور انتخاب‌شده به فروشگاه route تعلق ندارد.")
        if store and panel and panel.store_id and panel.store_id != store.pk:
            errors["inbound"] = _("Inbound انتخاب‌شده به فروشگاه route تعلق ندارد.")
        if plan and panel and plan.store_id and panel.store_id and panel.store_id != plan.store_id:
            errors["inbound"] = _("Inbound انتخاب‌شده به فروشگاه پلن تعلق ندارد.")

        if not self.is_active:
            if errors:
                raise ValidationError(errors)
            return

        if not inbound:
            errors["inbound"] = _("برای route فعال باید inbound انتخاب شود.")
        else:
            if not inbound.is_active:
                errors["inbound"] = _("Inbound route فعال باید فعال باشد.")
            elif not inbound.available_for_new_orders:
                errors["inbound"] = _("Inbound legacy یا خارج از فروش جدید نمی‌تواند route فعال داشته باشد.")
            elif not panel:
                errors["inbound"] = _("Inbound route فعال باید به پنل معتبر وصل باشد.")
            elif not panel.is_active:
                errors["inbound"] = _("Inbound route فعال نمی‌تواند به پنل غیرفعال وصل باشد.")

        if operator:
            if not operator.is_active:
                errors["operator"] = _("اپراتور route فعال باید فعال باشد.")
            elif plan and plan.pk and not plan.operators.filter(pk=operator.pk).exists():
                errors["operator"] = _("این اپراتور جزو اپراتورهای مجاز پلن نیست.")

        if errors:
            raise ValidationError(errors)


class PanelHealthStatus(TimeStampedModel):
    class Status(models.TextChoices):
        UNKNOWN = "unknown", _("Unknown")
        OK = "ok", _("OK")
        WARNING = "warning", _("Warning")
        ERROR = "error", _("Error")
        DISABLED = "disabled", _("Disabled")

    panel = models.OneToOneField(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.CASCADE,
        related_name="health_status",
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.UNKNOWN,
        db_index=True,
    )
    last_checked_at = models.DateTimeField(_("last checked at"), blank=True, null=True, db_index=True)
    last_ok_at = models.DateTimeField(_("last OK at"), blank=True, null=True)
    last_error_at = models.DateTimeField(_("last error at"), blank=True, null=True)
    last_alert_sent_at = models.DateTimeField(_("last alert sent at"), blank=True, null=True)
    last_recovery_alert_sent_at = models.DateTimeField(_("last recovery alert sent at"), blank=True, null=True)
    consecutive_failures = models.PositiveIntegerField(_("consecutive failures"), default=0)
    consecutive_successes = models.PositiveIntegerField(_("consecutive successes"), default=0)
    response_time_ms = models.PositiveIntegerField(_("response time ms"), null=True, blank=True)
    error_code = models.CharField(_("error code"), max_length=80, blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    summary = models.TextField(_("summary"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("panel health status")
        verbose_name_plural = _("panel health statuses")
        ordering = ["panel__name"]
        indexes = [
            models.Index(fields=["status", "last_checked_at"]),
        ]

    def __str__(self):
        return f"{self.panel} - {self.get_status_display()}"


class PanelHealthCheckLog(models.Model):
    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.CASCADE,
        related_name="health_check_logs",
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=PanelHealthStatus.Status.choices,
        db_index=True,
    )
    checked_at = models.DateTimeField(_("checked at"), default=timezone.now, db_index=True)
    response_time_ms = models.PositiveIntegerField(_("response time ms"), null=True, blank=True)
    login_ok = models.BooleanField(_("login OK"), null=True, blank=True)
    inbounds_checked = models.PositiveIntegerField(_("inbounds checked"), default=0)
    inbounds_ok = models.PositiveIntegerField(_("inbounds OK"), default=0)
    inbounds_warning = models.PositiveIntegerField(_("inbounds warning"), default=0)
    inbounds_error = models.PositiveIntegerField(_("inbounds error"), default=0)
    error_code = models.CharField(_("error code"), max_length=80, blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    alert_sent = models.BooleanField(_("alert sent"), default=False)

    class Meta:
        verbose_name = _("panel health check log")
        verbose_name_plural = _("panel health check logs")
        ordering = ["-checked_at"]
        indexes = [
            models.Index(fields=["panel", "checked_at"]),
            models.Index(fields=["status", "checked_at"]),
            models.Index(fields=["alert_sent", "checked_at"]),
        ]

    def __str__(self):
        return f"{self.panel} - {self.get_status_display()} @ {self.checked_at:%Y-%m-%d %H:%M}"


class DailyAdminReportLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")

    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.CASCADE,
        related_name="daily_admin_report_logs",
    )
    report_date = models.DateField(_("report date"), db_index=True)
    period_start = models.DateTimeField(_("period start"))
    period_end = models.DateTimeField(_("period end"))
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, db_index=True)
    sent_to_count = models.PositiveIntegerField(_("sent to count"), default=0)
    message_text = models.TextField(_("message text"), blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    created_at = models.DateTimeField(_("created at"), default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("daily admin report log")
        verbose_name_plural = _("daily admin report logs")
        ordering = ["-report_date", "-created_at"]
        indexes = [
            models.Index(fields=["store", "report_date"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "report_date"],
                name="unique_daily_admin_report_per_store_date",
            ),
        ]

    def __str__(self):
        return f"{self.store} - {self.report_date} - {self.get_status_display()}"


class PanelUsageSnapshot(models.Model):
    class Status(models.TextChoices):
        OK = "ok", _("OK")
        PARTIAL = "partial", _("Partial")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.CASCADE,
        related_name="usage_snapshots",
    )
    captured_at = models.DateTimeField(_("captured at"), db_index=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, db_index=True)
    total_upload_bytes = models.BigIntegerField(_("total upload bytes"), default=0)
    total_download_bytes = models.BigIntegerField(_("total download bytes"), default=0)
    total_used_bytes = models.BigIntegerField(_("total used bytes"), default=0)
    clients_count = models.PositiveIntegerField(_("clients count"), default=0)
    online_clients_count = models.PositiveIntegerField(_("online clients count"), default=0)
    active_inbounds_count = models.PositiveIntegerField(_("active inbounds count"), default=0)
    checked_inbounds_count = models.PositiveIntegerField(_("checked inbounds count"), default=0)
    error_message = models.TextField(_("error message"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("panel usage snapshot")
        verbose_name_plural = _("panel usage snapshots")
        ordering = ["-captured_at"]
        indexes = [
            models.Index(fields=["panel", "captured_at"]),
            models.Index(fields=["status", "captured_at"]),
        ]

    def __str__(self):
        return f"{self.panel} - {self.status} @ {self.captured_at:%Y-%m-%d %H:%M}"


class PanelClientUsageSnapshot(models.Model):
    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.CASCADE,
        related_name="client_usage_snapshots",
    )
    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.SET_NULL,
        related_name="client_usage_snapshots",
        null=True,
        blank=True,
    )
    captured_at = models.DateTimeField(_("captured at"), db_index=True)
    client_identifier_hash = models.CharField(_("client identifier hash"), max_length=128, db_index=True)
    client_identifier_masked = models.CharField(_("client identifier masked"), max_length=128, blank=True)
    email_masked = models.CharField(_("email masked"), max_length=128, blank=True)
    upload_bytes = models.BigIntegerField(_("upload bytes"), default=0)
    download_bytes = models.BigIntegerField(_("download bytes"), default=0)
    used_bytes = models.BigIntegerField(_("used bytes"), default=0)
    total_bytes = models.BigIntegerField(_("total bytes"), null=True, blank=True)
    expiry_time = models.DateTimeField(_("expiry time"), null=True, blank=True)
    enabled = models.BooleanField(_("enabled"), null=True, blank=True)
    online = models.BooleanField(_("online"), null=True, blank=True)
    source = models.CharField(_("source"), max_length=80, blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("panel client usage snapshot")
        verbose_name_plural = _("panel client usage snapshots")
        ordering = ["-captured_at"]
        indexes = [
            models.Index(fields=["panel", "captured_at"]),
            models.Index(fields=["client_identifier_hash", "captured_at"]),
            models.Index(fields=["inbound", "captured_at"]),
        ]

    def __str__(self):
        return f"{self.panel} - {self.client_identifier_masked or self.client_identifier_hash[:12]} @ {self.captured_at:%Y-%m-%d %H:%M}"


class PanelDailyUsage(models.Model):
    class DataQuality(models.TextChoices):
        COMPLETE = "complete", _("Complete")
        PARTIAL = "partial", _("Partial")
        INSUFFICIENT = "insufficient", _("Insufficient")
        ESTIMATED = "estimated", _("Estimated")

    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.CASCADE,
        related_name="daily_usages",
    )
    usage_date = models.DateField(_("usage date"), db_index=True)
    timezone = models.CharField(_("timezone"), max_length=64, default="Asia/Tehran")
    upload_bytes = models.BigIntegerField(_("upload bytes"), default=0)
    download_bytes = models.BigIntegerField(_("download bytes"), default=0)
    used_bytes = models.BigIntegerField(_("used bytes"), default=0)
    active_users_count = models.PositiveIntegerField(_("active users count"), default=0)
    online_users_count = models.PositiveIntegerField(_("online users count"), default=0)
    clients_count_start = models.PositiveIntegerField(_("clients count start"), default=0)
    clients_count_end = models.PositiveIntegerField(_("clients count end"), default=0)
    snapshot_start_at = models.DateTimeField(_("snapshot start at"), null=True, blank=True)
    snapshot_end_at = models.DateTimeField(_("snapshot end at"), null=True, blank=True)
    data_quality = models.CharField(
        _("data quality"),
        max_length=20,
        choices=DataQuality.choices,
        default=DataQuality.INSUFFICIENT,
        db_index=True,
    )
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    calculated_at = models.DateTimeField(_("calculated at"), auto_now=True)

    class Meta:
        verbose_name = _("panel daily usage")
        verbose_name_plural = _("panel daily usages")
        ordering = ["-usage_date", "panel__name"]
        indexes = [
            models.Index(fields=["panel", "usage_date"]),
            models.Index(fields=["data_quality", "usage_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["panel", "usage_date", "timezone"],
                name="unique_panel_daily_usage_per_timezone",
            ),
        ]

    def __str__(self):
        return f"{self.panel} - {self.usage_date} - {self.data_quality}"


class Order(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", _("Pending payment")
        PENDING_VERIFICATION = "pending_verification", _("Pending verification")
        CONFIRMED = "confirmed", _("Confirmed")
        COMPLETED = "completed", _("Completed")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    class VerificationStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        VERIFIED = "verified", _("Verified")
        REJECTED = "rejected", _("Rejected")

    class PaymentMethod(models.TextChoices):
        MANUAL_CARD = "manual_card", _("Manual card-to-card")
        GATEWAY = "gateway", _("Online gateway")
        ADMIN_FREE = "admin_free", _("Admin free subscription")

    class DiscountSource(models.TextChoices):
        NONE = "none", _("No discount")
        MANUAL = "manual", _("Manual coupon")
        WHOLESALE = "wholesale", _("Wholesale discount")

    public_id = models.UUIDField(_("public ID"), default=generate_public_id, editable=False, unique=True)
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="orders",
        null=True,
        blank=True,
    )
    plan = models.ForeignKey(Plan, verbose_name=_("plan"), on_delete=models.PROTECT, related_name="orders")
    operator = models.ForeignKey(
        Operator,
        verbose_name=_("operator"),
        on_delete=models.SET_NULL,
        related_name="orders",
        null=True,
        blank=True,
        help_text=_("Selected internet operator for operator-based sales mode."),
    )
    quantity = models.PositiveIntegerField(
        _("quantity"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
        help_text=_("Number of VPN accounts requested in this order."),
    )

    order_tracking_code = models.CharField(
        _("order tracking code"),
        max_length=36,
        default=generate_order_tracking,
        unique=True,
        db_index=True,
        validators=[tracking_code_validator],
        help_text=_("Public tracking code used by customers and admins to find this order."),
    )
    status = models.CharField(
        _("status"),
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
        db_index=True,
        help_text=_("Operational lifecycle status of the order."),
    )

    original_amount = models.PositiveIntegerField(_("original amount"), default=0)
    discount_code = models.ForeignKey(
        DiscountCode,
        verbose_name=_("discount code"),
        on_delete=models.SET_NULL,
        related_name="orders",
        blank=True,
        null=True,
    )
    discount_code_text = models.CharField(_("discount code text"), max_length=50, blank=True)
    discount_amount = models.PositiveIntegerField(_("discount amount"), default=0)
    discount_source = models.CharField(
        _("discount source"),
        max_length=20,
        choices=DiscountSource.choices,
        default=DiscountSource.NONE,
        db_index=True,
    )
    amount = models.PositiveIntegerField(_("amount"), default=0)
    currency = models.CharField(
        _("currency"),
        max_length=10,
        choices=Plan.Currency.choices,
        default=Plan.Currency.TOMAN,
    )

    payment_method = models.CharField(
        _("payment method"),
        max_length=30,
        choices=PaymentMethod.choices,
        default=PaymentMethod.MANUAL_CARD,
        db_index=True,
    )
    payment_gateway = models.CharField(_("payment gateway"), max_length=50, blank=True)
    gateway_authority = models.CharField(_("gateway authority"), max_length=100, blank=True)
    gateway_reference_id = models.CharField(_("gateway reference ID"), max_length=100, blank=True)

    is_paid = models.BooleanField(_("is paid"), default=False, db_index=True)
    payment_submitted_at = models.DateTimeField(_("payment submitted at"), blank=True, null=True)

    sender_card_name = models.CharField(_("sender card name"), max_length=100, blank=True)
    sender_card_last4 = models.CharField(
        _("sender card last 4 digits"),
        max_length=4,
        blank=True,
        validators=[card_last4_validator],
        help_text=_("Last four digits of the payer card."),
    )
    payment_date = models.DateField(_("payment date"), blank=True, null=True)
    payment_time = models.TimeField(_("payment time"), blank=True, null=True)
    payment_receipt_image = models.ImageField(
        _("payment receipt image"),
        upload_to="payment_receipts/%Y/%m/",
        blank=True,
        null=True,
    )
    admin_notified_at = models.DateTimeField(_("admin notified at"), blank=True, null=True)
    admin_receipt_notified_at = models.DateTimeField(_("admin receipt notified at"), blank=True, null=True)

    verification_status = models.CharField(
        _("verification status"),
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
        db_index=True,
        help_text=_("Manual or SMS payment verification status."),
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("verified by"),
        on_delete=models.SET_NULL,
        related_name="verified_orders",
        blank=True,
        null=True,
    )
    verified_at = models.DateTimeField(_("verified at"), blank=True, null=True)
    rejection_reason = models.TextField(_("rejection reason"), blank=True)

    bank_tracking_code = models.CharField(_("bank tracking code"), max_length=50, blank=True, null=True)
    card_last_four = models.CharField(_("card last four digits"), max_length=4, blank=True, null=True)

    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.SET_NULL,
        related_name="orders",
        null=True,
        blank=True,
    )
    username = models.CharField(_("username"), max_length=100, default="admin")
    uuid = models.CharField(_("UUID"), max_length=100, null=True, blank=True)
    sub_link = models.URLField(_("subscription link"), max_length=500, blank=True, null=True)
    direct_link = models.CharField(_("direct link"), max_length=500, null=True, blank=True)

    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("order")
        verbose_name_plural = _("orders")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "status", "created_at"]),
            models.Index(fields=["store", "status", "created_at"]),
            models.Index(fields=["operator", "status", "created_at"]),
            models.Index(fields=["verification_status", "created_at"]),
            models.Index(fields=["payment_method", "payment_gateway"]),
            models.Index(fields=["discount_code", "created_at"]),
            models.Index(fields=["uuid"]),
        ]

    def __str__(self):
        return _("Order %(tracking_code)s - %(status)s") % {
            "tracking_code": self.order_tracking_code,
            "status": self.get_status_display(),
        }

    def clean(self):
        super().clean()
        sales_mode = self.store.sales_mode if self.store_id else Store.SalesMode.TUNNEL
        if sales_mode != Store.SalesMode.OPERATOR_BASED:
            return
        if not self.operator_id:
            raise ValidationError({"operator": _("Operator is required for operator-based sales mode.")})
        if self.plan_id and not self.plan.is_available_for_operator(self.operator):
            raise ValidationError({"plan": _("Selected plan is not available for the selected operator.")})

    @property
    def subtotal_amount(self):
        unit_price = self.plan.price if self.plan_id else 0
        return unit_price * (self.quantity or 1)

    @property
    def total_price(self):
        return self.amount

    def save(self, *args, **kwargs):
        changed_fields = []
        if self.status == "pending":
            self.status = self.Status.PENDING_VERIFICATION
            changed_fields.append("status")
        if self.plan_id:
            if not self.store_id and self.plan.store_id:
                self.store = self.plan.store
                changed_fields.append("store")
            if not self.original_amount:
                self.original_amount = self.subtotal_amount
                changed_fields.append("original_amount")
            if not self.amount and not self.discount_amount:
                self.amount = self.original_amount or self.subtotal_amount
                changed_fields.append("amount")
            if not self.currency:
                self.currency = self.plan.currency
                changed_fields.append("currency")
        if self.is_manual_payment and (
            self.is_paid
            or self.status == self.Status.PENDING_VERIFICATION
            or self.payment_submitted_at
        ):
            submitted_at = timezone.localtime(self.payment_submitted_at or timezone.now())
            if not self.payment_submitted_at:
                self.payment_submitted_at = submitted_at
                changed_fields.append("payment_submitted_at")
            if not self.payment_date:
                self.payment_date = timezone.localdate(timezone.now(), TEHRAN_TZ)
                changed_fields.append("payment_date")

        update_fields = kwargs.get("update_fields")
        if update_fields is not None and changed_fields:
            kwargs["update_fields"] = [*update_fields, *changed_fields]
        super().save(*args, **kwargs)

    @property
    def is_manual_payment(self):
        return self.payment_method == self.PaymentMethod.MANUAL_CARD

    @property
    def can_be_activated(self):
        return bool(self.inbound_id and self.uuid)

    def get_vpn_clients(self):
        return self.vpn_clients.select_related("plan", "inbound", "inbound__panel").all()

    def get_remaining_traffic(self):
        return sum(client.remaining_traffic_bytes for client in self.get_vpn_clients())

    def is_active(self):
        return self.vpn_clients.filter(status=VPNClient.Status.ACTIVE).exists()

    def apply_discount(self, discount_code, discount_amount, *, source=None, label=""):
        self.original_amount = self.original_amount or self.subtotal_amount
        self.discount_code = discount_code
        self.discount_code_text = label or (discount_code.code if discount_code else "")
        self.discount_amount = min(discount_amount, self.original_amount)
        self.amount = max(self.original_amount - self.discount_amount, 0)
        self.discount_source = source or self.DiscountSource.MANUAL

    def apply_wholesale_discount(self, discount_percent):
        self.original_amount = self.original_amount or self.subtotal_amount
        discount_percent = max(min(int(discount_percent or 0), 100), 0)
        if not discount_percent:
            return
        discount_amount = min((self.original_amount * discount_percent) // 100, self.original_amount)
        self.apply_discount(
            None,
            discount_amount,
            source=self.DiscountSource.WHOLESALE,
            label=f"WHOLESALE {discount_percent}%",
        )

    def submit_manual_payment(
        self,
        *,
        sender_card_name="",
        sender_card_last4="",
        payment_time=None,
        receipt_image=None,
        receipt_text="",
        require_receipt=False,
        require_receipt_image=False,
    ):
        cleaned = clean_manual_payment_submission(
            sender_card_name=sender_card_name,
            sender_card_last4=sender_card_last4,
            payment_time=payment_time,
            receipt_image=receipt_image,
            receipt_text=receipt_text,
            require_receipt=require_receipt,
            require_receipt_image=require_receipt_image,
            existing_sender_card_name=self.sender_card_name,
            existing_sender_card_last4=self.sender_card_last4,
        )
        submitted_at = timezone.localtime(timezone.now())
        self.payment_method = self.PaymentMethod.MANUAL_CARD
        self.sender_card_name = cleaned["sender_card_name"]
        self.sender_card_last4 = cleaned["sender_card_last4"]
        self.payment_date = timezone.localdate(timezone.now(), TEHRAN_TZ)
        self.payment_time = cleaned["payment_time"]
        if cleaned["receipt_image"]:
            self.payment_receipt_image = cleaned["receipt_image"]
        if cleaned["receipt_text"]:
            metadata = dict(self.metadata or {})
            metadata["receipt_text"] = cleaned["receipt_text"]
            self.metadata = metadata
        if cleaned["receipt_text"] or require_receipt:
            from .receipt_analysis import analyze_receipt_text

            metadata = dict(self.metadata or {})
            metadata["receipt_analysis"] = analyze_receipt_text(
                cleaned["receipt_text"],
                expected_amount=self.amount,
                currency=self.currency,
            )
            self.metadata = metadata
        self.card_last_four = cleaned["sender_card_last4"]
        self.is_paid = True
        self.payment_submitted_at = submitted_at
        self.status = self.Status.PENDING_VERIFICATION
        self.verification_status = self.VerificationStatus.PENDING

    def mark_payment_verified(self, user=None):
        self.is_paid = True
        self.verification_status = self.VerificationStatus.VERIFIED
        self.verified_by = user
        self.verified_at = timezone.now()
        self.status = self.Status.COMPLETED

    def mark_payment_rejected(self, reason="", user=None):
        self.verification_status = self.VerificationStatus.REJECTED
        self.verified_by = user
        self.verified_at = timezone.now()
        self.rejection_reason = reason
        self.status = self.Status.REJECTED


class VPNClient(TimeStampedModel):
    class Status(models.TextChoices):
        CREATED = "created", _("Created")
        INACTIVE = "inactive", _("Inactive")
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        EXPIRED = "expired", _("Expired")
        DELETED = "deleted", _("Deleted")
        ERROR = "error", _("Error")

    public_id = models.UUIDField(_("public ID"), default=generate_public_id, editable=False, unique=True)
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.PROTECT,
        related_name="vpn_clients",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(
        Order,
        verbose_name=_("order"),
        on_delete=models.SET_NULL,
        related_name="vpn_clients",
        null=True,
        blank=True,
    )
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("plan"),
        on_delete=models.SET_NULL,
        related_name="vpn_clients",
        null=True,
        blank=True,
    )
    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.SET_NULL,
        related_name="vpn_clients",
        null=True,
        blank=True,
    )

    username = models.CharField(_("username"), max_length=150, db_index=True)
    xui_email = models.CharField(_("X-UI email"), max_length=150, blank=True, db_index=True)
    uuid = models.CharField(_("UUID"), max_length=100, unique=True, null=True, blank=True)
    sub_id = models.CharField(_("subscription ID"), max_length=100, blank=True)
    sub_link = models.URLField(_("subscription link"), max_length=500, blank=True, null=True)
    direct_link = models.CharField(_("direct link"), max_length=500, blank=True, null=True)

    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED,
        db_index=True,
    )
    traffic_limit_bytes = models.PositiveBigIntegerField(_("traffic limit bytes"), default=0)
    used_upload_bytes = models.PositiveBigIntegerField(_("used upload bytes"), default=0)
    used_download_bytes = models.PositiveBigIntegerField(_("used download bytes"), default=0)
    used_traffic_bytes = models.PositiveBigIntegerField(_("used traffic bytes"), default=0)
    duration_days = models.PositiveIntegerField(_("duration days"), default=0)
    device_limit = models.PositiveIntegerField(_("device limit"), default=2)
    activated_at = models.DateTimeField(_("activated at"), blank=True, null=True)
    expires_at = models.DateTimeField(_("expires at"), blank=True, null=True)
    disabled_at = models.DateTimeField(_("disabled at"), blank=True, null=True)
    deleted_at = models.DateTimeField(_("deleted at"), blank=True, null=True)
    deleted_by_customer = models.ForeignKey(
        Customer,
        verbose_name=_("deleted by customer"),
        on_delete=models.SET_NULL,
        related_name="deleted_vpn_clients",
        null=True,
        blank=True,
    )
    deleted_by_admin_telegram_id = models.CharField(_("deleted by admin Telegram ID"), max_length=80, blank=True)
    delete_reason = models.TextField(_("delete reason"), blank=True)
    remote_deleted_at = models.DateTimeField(_("remote deleted at"), blank=True, null=True)
    last_online_at = models.DateTimeField(_("last online at"), blank=True, null=True)
    last_synced_at = models.DateTimeField(_("last synced at"), blank=True, null=True)
    xui_raw = models.JSONField(_("X-UI raw data"), default=dict, blank=True)

    class Meta:
        verbose_name = _("VPN client")
        verbose_name_plural = _("VPN clients")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "status"]),
            models.Index(fields=["inbound", "status"]),
            models.Index(fields=["deleted_at"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["xui_email"]),
        ]

    def __str__(self):
        return f"{self.username} - {self.get_status_display()}"

    def mark_active(self, *, duration_days=None):
        now = timezone.now()
        duration = duration_days or self.duration_days
        self.status = self.Status.ACTIVE
        self.activated_at = self.activated_at or now
        if duration:
            self.expires_at = now + timedelta(days=duration)
        self.disabled_at = None

    def mark_suspended(self):
        self.status = self.Status.SUSPENDED
        self.disabled_at = timezone.now()

    def mark_deleted(self, *, customer=None, admin_telegram_id="", reason="", remote_deleted_at=None):
        now = timezone.now()
        self.status = self.Status.DELETED
        self.deleted_at = self.deleted_at or now
        self.disabled_at = self.disabled_at or now
        self.remote_deleted_at = remote_deleted_at or self.remote_deleted_at or now
        if customer:
            self.deleted_by_customer = customer
        if admin_telegram_id:
            self.deleted_by_admin_telegram_id = str(admin_telegram_id)
        if reason:
            self.delete_reason = str(reason)
        self.sub_link = ""
        self.direct_link = ""

    @property
    def is_deleted(self):
        return self.status == self.Status.DELETED or bool(self.deleted_at)

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def remaining_traffic_bytes(self):
        if not self.traffic_limit_bytes:
            return 0
        return max(self.traffic_limit_bytes - self.used_traffic_bytes, 0)

    def sync_usage_fields(self, stats):
        self.used_upload_bytes = stats.get("used_upload_bytes", 0)
        self.used_download_bytes = stats.get("used_download_bytes", 0)
        self.used_traffic_bytes = stats.get("used_traffic_bytes", 0)
        self.traffic_limit_bytes = stats.get("total_traffic_bytes") or self.traffic_limit_bytes
        if stats.get("expiry_at"):
            self.expires_at = stats["expiry_at"]
        if stats.get("last_online_at"):
            self.last_online_at = stats["last_online_at"]
        self.last_synced_at = timezone.now()
        self.xui_raw = stats.get("raw", {})


class VPNClientActionLog(TimeStampedModel):
    class ActorType(models.TextChoices):
        USER = "user", _("User")
        ADMIN = "admin", _("Admin")
        SYSTEM = "system", _("System")

    class Action(models.TextChoices):
        USER_DELETE = "user_delete", _("User delete")
        ADMIN_DELETE = "admin_delete", _("Admin delete")
        ADMIN_UPDATE_TRAFFIC = "admin_update_traffic", _("Admin update traffic")
        ADMIN_UPDATE_EXPIRY = "admin_update_expiry", _("Admin update expiry")
        ADMIN_UPDATE_TRAFFIC_AND_EXPIRY = "admin_update_traffic_and_expiry", _("Admin update traffic and expiry")
        ADMIN_REFRESH_LINK = "admin_refresh_link", _("Admin refresh link")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    vpn_client = models.ForeignKey(
        VPNClient,
        verbose_name=_("VPN client"),
        on_delete=models.SET_NULL,
        related_name="action_logs",
        null=True,
        blank=True,
    )
    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="vpn_client_action_logs",
        null=True,
        blank=True,
    )
    actor_type = models.CharField(_("actor type"), max_length=20, choices=ActorType.choices, db_index=True)
    actor_telegram_id = models.CharField(_("actor Telegram ID"), max_length=80, blank=True, db_index=True)
    action = models.CharField(_("action"), max_length=50, choices=Action.choices, db_index=True)
    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.SET_NULL,
        related_name="vpn_client_action_logs",
        null=True,
        blank=True,
    )
    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.SET_NULL,
        related_name="vpn_client_action_logs",
        null=True,
        blank=True,
    )
    xui_identifier_masked = models.CharField(_("masked X-UI identifier"), max_length=120, blank=True)
    old_total_bytes = models.BigIntegerField(_("old total bytes"), null=True, blank=True)
    new_total_bytes = models.BigIntegerField(_("new total bytes"), null=True, blank=True)
    old_expiry_time = models.DateTimeField(_("old expiry time"), null=True, blank=True)
    new_expiry_time = models.DateTimeField(_("new expiry time"), null=True, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    error_message = models.TextField(_("error message"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("VPN client action log")
        verbose_name_plural = _("VPN client action logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vpn_client", "created_at"]),
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["action", "status", "created_at"]),
            models.Index(fields=["actor_type", "actor_telegram_id"]),
        ]

    def __str__(self):
        target = self.vpn_client_id or self.xui_identifier_masked or "-"
        return f"{self.get_action_display()} {target} - {self.get_status_display()}"


class VPNClientReminderLog(TimeStampedModel):
    class ReminderType(models.TextChoices):
        EXPIRY_BEFORE = "expiry_before", _("Expiry before")
        EXPIRY_TODAY = "expiry_today", _("Expiry today")
        EXPIRY_AFTER = "expiry_after", _("Expiry after")
        LOW_TRAFFIC = "low_traffic", _("Low traffic")

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="vpn_reminder_logs",
        null=True,
        blank=True,
    )
    vpn_client = models.ForeignKey(
        VPNClient,
        verbose_name=_("VPN client"),
        on_delete=models.CASCADE,
        related_name="reminder_logs",
    )
    reminder_type = models.CharField(
        _("reminder type"),
        max_length=30,
        choices=ReminderType.choices,
        db_index=True,
    )
    trigger_key = models.CharField(_("trigger key"), max_length=80, db_index=True)
    trigger_date = models.DateField(_("trigger date"), default=timezone.localdate, db_index=True)
    sent_to_telegram_id = models.CharField(_("sent to Telegram ID"), max_length=80, blank=True, db_index=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.SKIPPED,
        db_index=True,
    )
    message_text = models.TextField(_("message text"), blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    sent_at = models.DateTimeField(_("sent at"), blank=True, null=True)

    class Meta:
        verbose_name = _("VPN client reminder log")
        verbose_name_plural = _("VPN client reminder logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["vpn_client", "reminder_type", "trigger_key"]),
            models.Index(fields=["status", "trigger_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["vpn_client", "reminder_type", "trigger_key", "trigger_date"],
                name="unique_vpn_client_reminder_per_trigger_date",
            ),
        ]

    def __str__(self):
        return f"{self.vpn_client} {self.reminder_type}:{self.trigger_key} - {self.status}"


class FreeTrialRequest(TimeStampedModel):
    class Status(models.TextChoices):
        CREATED = "created", _("Created")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    customer = models.ForeignKey(
        Customer,
        verbose_name=_("customer"),
        on_delete=models.SET_NULL,
        related_name="free_trial_requests",
        null=True,
        blank=True,
    )
    panel = models.ForeignKey(
        Panel,
        verbose_name=_("panel"),
        on_delete=models.PROTECT,
        related_name="free_trial_requests",
    )
    inbound = models.ForeignKey(
        Inbound,
        verbose_name=_("inbound"),
        on_delete=models.PROTECT,
        related_name="free_trial_requests",
    )
    vpn_client = models.ForeignKey(
        VPNClient,
        verbose_name=_("VPN client"),
        on_delete=models.SET_NULL,
        related_name="free_trial_requests",
        null=True,
        blank=True,
    )
    telegram_user_id = models.CharField(_("Telegram user ID"), max_length=80, blank=True, db_index=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED,
        db_index=True,
    )
    traffic_gb = models.DecimalField(
        _("traffic GB"),
        max_digits=8,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    duration_hours = models.PositiveIntegerField(_("duration hours"), validators=[MinValueValidator(1)])
    config_link = models.TextField(_("config link"), blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    delivered_at = models.DateTimeField(_("delivered at"), blank=True, null=True)
    expires_at = models.DateTimeField(_("expires at"), blank=True, null=True)

    class Meta:
        verbose_name = _("free trial request")
        verbose_name_plural = _("free trial requests")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["telegram_user_id", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        target = self.customer_id or self.telegram_user_id or "-"
        return f"Free trial {target} - {self.get_status_display()}"

    def clean(self):
        super().clean()
        errors = {}
        if self.inbound_id and self.panel_id and self.inbound.panel_id != self.panel_id:
            errors["inbound"] = _("Inbound must belong to the selected panel.")
        if self.traffic_gb is not None and self.traffic_gb <= 0:
            errors["traffic_gb"] = _("Traffic must be positive.")
        if self.duration_hours is not None and self.duration_hours <= 0:
            errors["duration_hours"] = _("Duration must be positive.")
        if errors:
            raise ValidationError(errors)


class VPNClientUsageSnapshot(models.Model):
    vpn_client = models.ForeignKey(
        VPNClient,
        verbose_name=_("VPN client"),
        on_delete=models.CASCADE,
        related_name="usage_snapshots",
    )
    recorded_at = models.DateTimeField(_("recorded at"), default=timezone.now, db_index=True)
    total_traffic_bytes = models.PositiveBigIntegerField(_("total traffic bytes"), default=0)
    used_upload_bytes = models.PositiveBigIntegerField(_("used upload bytes"), default=0)
    used_download_bytes = models.PositiveBigIntegerField(_("used download bytes"), default=0)
    used_traffic_bytes = models.PositiveBigIntegerField(_("used traffic bytes"), default=0)
    remaining_traffic_bytes = models.PositiveBigIntegerField(_("remaining traffic bytes"), default=0)
    raw = models.JSONField(_("raw data"), default=dict, blank=True)

    class Meta:
        verbose_name = _("VPN client usage snapshot")
        verbose_name_plural = _("VPN client usage snapshots")
        ordering = ["recorded_at"]
        indexes = [
            models.Index(fields=["vpn_client", "recorded_at"]),
        ]

    def __str__(self):
        return f"{self.vpn_client} @ {self.recorded_at:%Y-%m-%d %H:%M}"
