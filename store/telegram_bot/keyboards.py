from django.conf import settings

from store.models import BotConfiguration

from .constants import (
    COPY_TEXT_MAX_LENGTH,
)
from .formatting import format_card_for_copy, format_money_for_copy


def copy_text_is_supported(config=None):
    if config and config.provider != BotConfiguration.Provider.TELEGRAM:
        return False
    return not getattr(settings, "BOT_COPY_TEXT_DISABLED", False)


def build_copy_text_button(label, text, *, config=None, fallback_callback_data="", fallback_label=None):
    copy_value = str(text or "").strip()
    if 0 < len(copy_value) <= COPY_TEXT_MAX_LENGTH and copy_text_is_supported(config):
        return {"text": label, "copy_text": {"text": copy_value}}
    if fallback_callback_data:
        return {"text": fallback_label or label.replace("کپی", "نمایش"), "callback_data": fallback_callback_data}
    return None


def merge_inline_keyboards(*keyboards):
    rows = []
    for keyboard in keyboards:
        if keyboard:
            rows.extend((keyboard.get("inline_keyboard") or []))
    return {"inline_keyboard": rows}


def build_payment_keyboard(card_number, amount, *, config=None):
    rows = []
    card_button = build_copy_text_button(
        "کپی شماره کارت",
        format_card_for_copy(card_number),
        config=config,
        fallback_callback_data="user:copy:payment_card",
    )
    amount_button = build_copy_text_button(
        "کپی مبلغ",
        format_money_for_copy(amount),
        config=config,
        fallback_callback_data="user:copy:payment_amount",
    )
    copy_row = [button for button in [card_button, amount_button] if button]
    if copy_row:
        rows.append(copy_row)
    rows.append([{"text": "برگشت", "callback_data": "user:buy_back_summary"}])
    rows.append([{"text": "لغو", "callback_data": "user:cancel"}])
    return {"inline_keyboard": rows}


def empty_inline_keyboard():
    return {"inline_keyboard": []}
