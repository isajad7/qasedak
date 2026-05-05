from django import template
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from payments.models import IncomingPaymentSMS
from store.models import Customer, Order

register = template.Library()


@register.simple_tag
def admin_dashboard_stats():
    today = timezone.localdate()
    pending_statuses = [
        Order.Status.PENDING_PAYMENT,
        Order.Status.PENDING_VERIFICATION,
        Order.Status.CONFIRMED,
    ]

    return [
        {
            "label": _("Pending orders"),
            "value": Order.objects.filter(status__in=pending_statuses).count(),
            "icon": "fas fa-shopping-cart",
            "tone": "primary",
        },
        {
            "label": _("Unmatched SMS"),
            "value": IncomingPaymentSMS.objects.filter(
                status__in=[IncomingPaymentSMS.Status.NEW, IncomingPaymentSMS.Status.NO_MATCH]
            ).count(),
            "icon": "fas fa-sms",
            "tone": "warning",
        },
        {
            "label": _("Wholesale users"),
            "value": Customer.objects.filter(is_active=True, is_wholesale=True).count(),
            "icon": "fas fa-user-tag",
            "tone": "success",
        },
        {
            "label": _("Orders today"),
            "value": Order.objects.filter(created_at__date=today).count(),
            "icon": "fas fa-calendar-day",
            "tone": "info",
        },
    ]
