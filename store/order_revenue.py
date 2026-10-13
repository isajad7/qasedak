"""Reporting-only order revenue policy; never provisions or delivers a service."""
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import BigIntegerField, Case, F, Q, Value, When
from django.utils import timezone

from .models import Order, Plan, normalize_payment_digits


def completed_paid_order_q(prefix=""):
    return Q(**{f"{prefix}status": Order.Status.COMPLETED, f"{prefix}is_paid": True})


def is_admin_direct_order(order):
    metadata = order.metadata or {}
    return bool(metadata.get("admin_direct_purchase") or metadata.get("source") == "admin_direct_purchase")


def revenue_amount_expression(prefix=""):
    return Case(
        When(Q(**{f"{prefix}metadata__admin_direct_purchase": True}), then=Value(0)),
        When(Q(**{f"{prefix}metadata__source": "admin_direct_purchase"}), then=Value(0)),
        When(Q(**{f"{prefix}revenue_included": False}), then=Value(0)),
        When(completed_paid_order_q(prefix), then=Case(
            When(Q(**{f"{prefix}revenue_included": True}),
                 then=F(f"{prefix}revenue_amount")),
            default=F(f"{prefix}amount"), output_field=BigIntegerField(),
        )),
        default=Value(0), output_field=BigIntegerField(),
    )


def order_counts_in_revenue(order):
    return not is_admin_direct_order(order) and order.revenue_included is not False


def order_revenue_amount(order):
    if not order_counts_in_revenue(order) or order.status != Order.Status.COMPLETED or not order.is_paid:
        return 0
    return (order.revenue_amount or 0) if order.revenue_included is True else order.amount


class RevenueSettingsForm(forms.Form):
    revenue_included = forms.ChoiceField(
        label="این سرویس در درآمد حساب شود؟",
        choices=(("", "انتخاب کنید"), ("yes", "بله"), ("no", "خیر")),
    )
    revenue_amount = forms.CharField(label="مبلغ درآمد", required=False,
                                   widget=forms.TextInput(attrs={"inputmode": "numeric"}))

    def clean(self):
        cleaned = super().clean()
        included = cleaned.get("revenue_included") == "yes"
        raw = normalize_payment_digits(cleaned.get("revenue_amount") or "").strip().replace(",", "").replace("٬", "")
        amount = None
        if included:
            if not raw.isascii() or not raw.isdigit() or len(raw) > 10 or not 0 < int(raw) <= 2147483647:
                self.add_error("revenue_amount", "برای محاسبه در درآمد، مبلغ مثبت و معتبر وارد کن.")
            else:
                amount = int(raw)
        cleaned["revenue_included"] = included
        cleaned["revenue_amount"] = amount
        return cleaned


def revenue_form_for_order(order, data=None):
    amount = order.revenue_amount if order.revenue_amount is not None else (order.amount or order.plan.price)
    return RevenueSettingsForm(data, initial={
        "revenue_included": "yes" if order_counts_in_revenue(order) else "no",
        "revenue_amount": amount,
    })


def update_order_revenue(order, *, included, amount=None):
    if is_admin_direct_order(order) and included:
        raise ValidationError("خرید مستقیم ادمین در درآمد حساب نمی‌شود.")
    if included and (amount is None or not 0 < amount <= 2147483647):
        raise ValidationError("مبلغ درآمد باید مثبت باشد.")
    # QuerySet.update deliberately bypasses Order.save and its payment/referral signals.
    Order.objects.filter(pk=order.pk).update(
        revenue_included=bool(included), revenue_amount=amount if included else None,
        updated_at=timezone.now(),
    )
    order.refresh_from_db(fields=["revenue_included", "revenue_amount", "updated_at"])


class AdminServiceGrantForm(RevenueSettingsForm):
    plan = forms.ModelChoiceField(label="پلن سرویس", queryset=Plan.objects.none())
    reason = forms.CharField(label="شرح (اختیاری)", required=False, max_length=500)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(is_active=True, store__is_active=True).select_related("store")
