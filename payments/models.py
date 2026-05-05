from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class IncomingPaymentSMS(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", _("New")
        MATCHED = "MATCHED", _("Matched")
        CONFIRMED = "CONFIRMED", _("Confirmed")
        DISMISSED = "DISMISSED", _("Dismissed")
        NO_MATCH = "NO_MATCH", _("No match")

    raw_text = models.TextField(_("raw text"), help_text=_("Original text received from the bank SMS."))
    amount = models.PositiveBigIntegerField(_("amount"), db_index=True)
    balance = models.PositiveBigIntegerField(_("balance"))
    sms_datetime = models.DateTimeField(_("SMS datetime"), db_index=True)
    received_at = models.DateTimeField(_("received at"), default=timezone.now, db_index=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
        help_text=_("Current matching and confirmation state for this incoming payment SMS."),
    )
    matched_orders = models.ManyToManyField(
        "store.Order",
        verbose_name=_("matched orders"),
        related_name="incoming_payment_sms",
        blank=True,
        help_text=_("Orders that match this SMS by amount and payment time window."),
    )

    class Meta:
        verbose_name = _("incoming payment SMS")
        verbose_name_plural = _("incoming payment SMS messages")
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["amount", "sms_datetime"]),
        ]

    def __str__(self):
        return _("SMS payment %(amount)s - %(status)s") % {
            "amount": f"{self.amount:,}",
            "status": self.get_status_display(),
        }
