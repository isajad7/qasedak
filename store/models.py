import random
import string
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
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
    existing_sender_card_name="",
    existing_sender_card_last4="",
):
    cleaned_receipt_text = clean_payment_receipt_text(receipt_text)
    cleaned_receipt_image = validate_payment_receipt_image(receipt_image)
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
    name = models.CharField(_("name"), max_length=100, default="AzadNet")
    english_name = models.CharField(_("English name"), max_length=100, default="AzadNet")
    slug = models.SlugField(_("slug"), max_length=80, unique=True, null=True, blank=True)
    domain = models.CharField(_("domain"), max_length=255, blank=True, null=True)
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)

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
    def telegram_support_url(self):
        if not self.telegram_support:
            return None
        return f"https://t.me/{self.telegram_support.lstrip('@')}"

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
    bot_token = models.CharField(_("bot token"), max_length=255, help_text=_("Bot token from Bale or Telegram."))
    admin_user_id = models.CharField(
        _("admin user ID"),
        max_length=80,
        help_text=_("Admin chat/user ID that receives notifications."),
    )
    webhook_secret = models.CharField(
        _("webhook secret"),
        max_length=32,
        default=generate_bot_webhook_secret,
        unique=True,
        editable=False,
    )
    is_active = models.BooleanField(_("is active"), default=True, db_index=True)
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


class BotUser(TimeStampedModel):
    class State(models.TextChoices):
        IDLE = "idle", _("Idle")
        BUY_WAIT_NAME = "buy_wait_name", _("Buy: waiting for payer name")
        BUY_WAIT_LAST4 = "buy_wait_last4", _("Buy: waiting for card last4")
        BUY_WAIT_TIME = "buy_wait_time", _("Buy: waiting for payment time")
        BUY_WAIT_TRACKING = "buy_wait_tracking", _("Buy: waiting for bank tracking")
        BUY_WAIT_RECEIPT = "buy_wait_receipt", _("Buy: waiting for receipt")
        GRANT_WAIT_USER = "grant_wait_user", _("Grant: waiting for user")
        GRANT_WAIT_REASON = "grant_wait_reason", _("Grant: waiting for reason")
        GRANT_CONFIRM = "grant_confirm", _("Grant: confirm")

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


class BotPendingAction(TimeStampedModel):
    class Action(models.TextChoices):
        REJECT_ORDER = "reject_order", _("Reject order")

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
        ]

    def __str__(self):
        return f"{self.get_action_display()} for {self.order_id}"

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at", "updated_at"])


class BotEventLog(TimeStampedModel):
    class EventType(models.TextChoices):
        NEW_ORDER = "new_order", _("New order")
        ORDER_APPROVED = "order_approved", _("Order approved")
        ORDER_REJECTED = "order_rejected", _("Order rejected")
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

    def validate_for_plan(self, plan, now=None):
        if self.discount_type == self.DiscountType.PERCENTAGE and self.value > 100:
            raise ValidationError(_("Percentage discounts cannot exceed 100."))
        if not self.is_active:
            raise ValidationError(_("Discount code is not active."))
        if not self.is_within_valid_window(now=now):
            raise ValidationError(_("Discount code is not valid right now."))
        if not self.has_usage_available():
            raise ValidationError(_("Discount code usage limit has been reached."))
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

    def save(self, *args, **kwargs):
        if self.username:
            self.username = self.username.strip()
        if self.phone_number:
            self.phone_number = self.phone_number.strip()
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
        return f"{self.panel.name} - {label}"

    @property
    def available_capacity(self):
        if self.max_clients is None:
            return None
        return max(self.max_clients - self.current_users, 0)

    @property
    def has_capacity(self):
        return self.max_clients is None or self.current_users < self.max_clients


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
    ):
        cleaned = clean_manual_payment_submission(
            sender_card_name=sender_card_name,
            sender_card_last4=sender_card_last4,
            payment_time=payment_time,
            receipt_image=receipt_image,
            receipt_text=receipt_text,
            require_receipt=require_receipt,
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
