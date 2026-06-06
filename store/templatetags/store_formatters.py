from django import template
from decimal import Decimal, InvalidOperation

from store.jalali import (
    format_jalali_date,
    format_jalali_datetime,
    format_jalali_time,
    persian_digits,
)

register = template.Library()


@register.filter
def commafy(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@register.filter
def currency_label(value):
    labels = {
        "TOMAN": "تومان",
        "IRR": "ریال",
        "USD": "USD",
    }
    return labels.get(value, value)


@register.filter
def traffic_gb(value):
    try:
        return f"{int(value) / (1024 ** 3):.2f}"
    except (TypeError, ValueError):
        return "0.00"


@register.filter
def clean_gb(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


@register.filter
def volume_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value

    if number < Decimal("1"):
        mb_value = number * Decimal("1000")
        normalized = format(mb_value.normalize(), "f")
        label = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
        return f"{label} MB"

    return f"{clean_gb(number)} GB"


@register.filter
def percent_of(value, total):
    try:
        total = int(total)
        if total <= 0:
            return 0
        return min(round((int(value) / total) * 100), 100)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


@register.filter
def fa_digits(value):
    return persian_digits(value)


@register.filter
def jalali_date(value):
    return format_jalali_date(value)


@register.filter
def jalali_time(value):
    return format_jalali_time(value)


@register.filter
def jalali_datetime(value):
    return format_jalali_datetime(value)
