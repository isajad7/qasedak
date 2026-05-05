import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime
from django.conf import settings
from django.utils import timezone


class SMSParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPaymentSMS:
    amount: int
    balance: int
    sms_datetime: datetime


DIGIT_TRANSLATION = str.maketrans(
    {
        "۰": "0",
        "۱": "1",
        "۲": "2",
        "۳": "3",
        "۴": "4",
        "۵": "5",
        "۶": "6",
        "۷": "7",
        "۸": "8",
        "۹": "9",
        "٠": "0",
        "١": "1",
        "٢": "2",
        "٣": "3",
        "٤": "4",
        "٥": "5",
        "٦": "6",
        "٧": "7",
        "٨": "8",
        "٩": "9",
        "ي": "ی",
        "ك": "ک",
        "٬": ",",
        "،": ",",
        "\u066c": ",",
        "\u200c": " ",
        "\u200e": "",
        "\u200f": "",
    }
)

AMOUNT_TOKEN_RE = r"[0-9][0-9,\s]*"
RIAL_AMOUNT_RE = re.compile(rf"(?P<amount>{AMOUNT_TOKEN_RE})\s*ریال")
BALANCE_RE = re.compile(rf"موجودی\s*[:：]?\s*(?P<balance>{AMOUNT_TOKEN_RE})\s*ریال")
TIME_RE = re.compile(r"(?<!\d)(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2})(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(?P<year>\d{4})\s*[./-]\s*(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})(?!\d)")
DEPOSIT_KEYWORDS = ("واریز", "نشست", "به حساب", "حساب شما")


def normalize_sms_text(text):
    return (text or "").translate(DIGIT_TRANSLATION)


def normalize_number(value):
    digits = re.sub(r"\D", "", normalize_sms_text(value))
    if not digits:
        raise SMSParseError("Expected a numeric value.")
    return int(digits)


def _extract_balance(text):
    match = BALANCE_RE.search(text)
    if not match:
        raise SMSParseError("Could not find SMS balance.")
    return normalize_number(match.group("balance"))


def _extract_deposit_amount(text):
    fallback = None
    for line in text.splitlines():
        if "موجودی" in line:
            continue
        match = RIAL_AMOUNT_RE.search(line)
        if not match:
            continue
        if fallback is None:
            fallback = match.group("amount")
        if any(keyword in line for keyword in DEPOSIT_KEYWORDS):
            return normalize_number(match.group("amount"))

    if fallback is not None:
        return normalize_number(fallback)

    raise SMSParseError("Could not find SMS deposit amount.")


def _sms_timezone():
    configured_timezone = getattr(settings, "PAYMENT_SMS_TIME_ZONE", None)
    if configured_timezone:
        try:
            return ZoneInfo(configured_timezone)
        except ZoneInfoNotFoundError:
            pass
    return timezone.get_default_timezone()


def _extract_sms_datetime(text):
    time_match = TIME_RE.search(text)
    date_match = DATE_RE.search(text)
    if not time_match:
        raise SMSParseError("Could not find SMS time.")
    if not date_match:
        raise SMSParseError("Could not find SMS Jalali date.")

    try:
        jalali_datetime = jdatetime.datetime(
            int(date_match.group("year")),
            int(date_match.group("month")),
            int(date_match.group("day")),
            int(time_match.group("hour")),
            int(time_match.group("minute")),
        )
        gregorian_datetime = jalali_datetime.togregorian()
    except ValueError as exc:
        raise SMSParseError("SMS Jalali date/time is invalid.") from exc

    return timezone.make_aware(gregorian_datetime, _sms_timezone())


def parse_payment_sms(raw_text):
    text = normalize_sms_text(raw_text)
    return ParsedPaymentSMS(
        amount=_extract_deposit_amount(text),
        balance=_extract_balance(text),
        sms_datetime=_extract_sms_datetime(text),
    )
