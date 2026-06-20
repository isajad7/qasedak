import re
from decimal import Decimal, InvalidOperation
from html import escape

from store.jalali import format_jalali_datetime, persian_digits

from .constants import BOT_DIGIT_TRANSLATION


def normalize_bot_number(value):
    return str(value or "").strip().translate(BOT_DIGIT_TRANSLATION)


def money(value, currency="TOMAN"):
    try:
        return f"{int(value):,} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}"


def bot_money(value, currency="TOMAN"):
    labels = {
        "TOMAN": "تومان",
        "IRR": "ریال",
        "USD": "دلار",
    }
    try:
        amount = f"{int(value):,}"
    except (TypeError, ValueError):
        amount = str(value or 0)
    return f"{persian_digits(amount)} {labels.get(currency, currency)}"


def format_money_for_display(amount, currency="TOMAN"):
    return bot_money(amount, currency)


def format_money_for_copy(amount):
    try:
        return str(int(amount))
    except (TypeError, ValueError):
        return normalize_bot_number(amount)


def format_card_for_copy(card_number):
    return re.sub(r"\D+", "", normalize_bot_number(card_number))


def format_card_for_display(card_number):
    compact = format_card_for_copy(card_number)
    if not compact:
        return str(card_number or "").strip()
    return " ".join(compact[index : index + 4] for index in range(0, len(compact), 4))


def escape_for_telegram_code(text, parse_mode="HTML"):
    text = str(text or "")
    if parse_mode == "HTML":
        return escape(text)
    if parse_mode == "MarkdownV2":
        return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)
    if parse_mode == "Markdown":
        return text.replace("\\", "\\\\").replace("`", "\\`")
    return text


def telegram_code(text, *, parse_mode="HTML", block=False):
    escaped = escape_for_telegram_code(text, parse_mode)
    if parse_mode == "HTML":
        tag = "pre" if block else "code"
        return f"<{tag}>{escaped}</{tag}>"
    fence = "```" if block else "`"
    return f"{fence}{escaped}{fence}"


def bot_gb_from_bytes(value):
    try:
        number = round((int(value or 0)) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        number = 0
    label = f"{number:.2f}".rstrip("0").rstrip(".")
    return persian_digits(label)


def clean_decimal_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value or 0)
    label = format(number.normalize(), "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def bot_volume_label(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return f"{persian_digits(value)} گیگابایت"

    if number < Decimal("1"):
        mb_value = number * Decimal("1000")
        return f"{persian_digits(clean_decimal_label(mb_value))} مگابایت"
    return f"{persian_digits(clean_decimal_label(number))} گیگابایت"


def bot_datetime(value):
    return format_jalali_datetime(value, default="ثبت نشده") if value else "ثبت نشده"
