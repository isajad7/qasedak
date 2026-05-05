from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import jdatetime
from django.utils import timezone


TEHRAN_TZ = ZoneInfo("Asia/Tehran")
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def persian_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


def to_tehran(value):
    if not isinstance(value, datetime):
        return value
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value, TEHRAN_TZ)


def format_jalali_date(value, *, default=""):
    if not value:
        return default
    if isinstance(value, datetime):
        value = to_tehran(value).date()
    if not isinstance(value, date):
        return default
    jalali_date = jdatetime.date.fromgregorian(date=value)
    return persian_digits(jalali_date.strftime("%Y/%m/%d"))


def format_jalali_time(value, *, default=""):
    if not value:
        return default
    if isinstance(value, datetime):
        value = to_tehran(value).time()
    if not isinstance(value, time):
        return default
    return persian_digits(f"{value.hour:02d}:{value.minute:02d}")


def format_jalali_datetime(value, *, default=""):
    if not value:
        return default
    if isinstance(value, datetime):
        local_value = to_tehran(value)
        return f"{format_jalali_date(local_value)}، {format_jalali_time(local_value)}"
    if isinstance(value, date):
        return format_jalali_date(value)
    return default


def format_jalali_chart_label(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        local_value = to_tehran(value)
        jalali_date = jdatetime.date.fromgregorian(date=local_value.date())
        return persian_digits(jalali_date.strftime("%m/%d") + f" {local_value:%H:%M}")
    return format_jalali_date(value)
