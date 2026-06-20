import re
from html import escape
from pathlib import PurePosixPath

from django.core.files.base import ContentFile

from store.models import PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES, PAYMENT_RECEIPT_ALLOWED_EXTENSIONS
from store.order_services import validate_order_quantity

from .constants import PAYMENT_RECEIPT_LABEL, PAYMENT_RECEIPT_ONLY_CALLBACK
from .formatting import format_card_for_copy, format_money_for_copy, telegram_code
from .keyboards import build_payment_keyboard


def _first_present(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _get_message_id(message):
    return _first_present(message, "message_id", "messageId", "id")


def _bot_client_label(client):
    return client.xui_email or client.username


def bot_payment_sender_name(bot_user):
    data = bot_user.state_data or {}
    for value in (
        data.get("sender_card_name"),
        bot_user.display_name,
        bot_user.username,
        bot_user.provider_user_id,
    ):
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned[:100]
    return "Bot user"


def copy_payment_value_from_state(
    config,
    bot_user,
    kind,
    *,
    get_current_store,
    renewal_context_from_state,
    purchase_context_from_state,
    pricing_from_state,
    validation_error_cls,
):
    store = config.store or get_current_store()
    if kind == "payment_card":
        return format_card_for_copy(getattr(store, "card_number", ""))
    data = bot_user.state_data or {}
    try:
        if data.get("flow") == "renewal":
            _vpn_client, _store, plan, error = renewal_context_from_state(config, bot_user)
            if error:
                return ""
            pricing = pricing_from_state(plan, 1, customer=bot_user.customer, data=data)
        else:
            _store, _operator, plan, quantity, error = purchase_context_from_state(config, bot_user)
            if error:
                return ""
            pricing = pricing_from_state(plan, quantity, customer=bot_user.customer, data=data)
    except validation_error_cls:
        return ""
    return format_money_for_copy(pricing.get("payable_amount"))


def payment_step_keyboard(card_number="", amount=0, *, config=None):
    return build_payment_keyboard(card_number, amount, config=config)


def optional_config_name_keyboard():
    return {
        "inline_keyboard": [
            [{"text": PAYMENT_RECEIPT_LABEL, "callback_data": PAYMENT_RECEIPT_ONLY_CALLBACK}],
            [
                {"text": "برگشت", "callback_data": "user:buy_back_summary"},
                {"text": "لغو", "callback_data": "user:cancel"},
            ],
        ]
    }


def bot_order_metadata(config, bot_user, *, source, extra=None):
    metadata = {
        "source": source,
        "bot": {
            "bot_config_id": config.pk,
            "provider": config.provider,
            "bot_user_id": bot_user.pk,
            "provider_user_id": bot_user.provider_user_id,
            "chat_id": bot_user.chat_id,
            "username": bot_user.username,
        },
    }
    metadata.update(extra or {})
    return metadata


def store_payment_lines(store, plan, *, quantity=1, operator=None, pricing=None):
    quantity = validate_order_quantity(quantity)
    pricing = pricing or {"original_amount": plan.price * quantity, "discount_amount": 0, "payable_amount": plan.price * quantity}
    amount_for_copy = format_money_for_copy(pricing["payable_amount"])
    card_number = format_card_for_copy(store.card_number) or str(store.card_number or "").strip()
    bank_name = str(store.bank_name or "").strip()
    sheba_number = str(store.sheba_number or "").strip()
    lines = [
        "💳 پرداخت کارت‌به‌کارت",
        "",
        "مبلغ قابل پرداخت:",
        telegram_code(amount_for_copy),
        "",
        "شماره کارت:",
        telegram_code(card_number),
        "",
        "به نام:",
        escape(store.card_owner or "-"),
    ]
    if bank_name:
        lines.extend(["", f"بانک: {escape(bank_name)}"])
    if sheba_number:
        lines.extend(["", f"شبا: {telegram_code(sheba_number)}"])
    return "\n".join(lines)


def format_payment_prompt(store, plan, *, quantity=1, operator=None, pricing=None, flow="purchase", vpn_client=None):
    lines = []
    if flow == "renewal":
        lines.append("تمدید سرویس")
    if vpn_client:
        lines.append(f"سرویس: {escape(_bot_client_label(vpn_client))}")
    lines.append(store_payment_lines(store, plan, quantity=quantity, operator=operator, pricing=pricing))
    lines.extend(["", "بعد از پرداخت، عکس رسید را همینجا ارسال کنید."])
    return "\n".join(lines)


def extract_receipt_file(message):
    photos = message.get("photo") or []
    if photos:
        photo = photos[-1]
        return {
            "file_id": _first_present(photo, "file_id", "fileId"),
            "file_unique_id": _first_present(photo, "file_unique_id", "fileUniqueId"),
            "kind": "photo",
            "file_name": "telegram-receipt.jpg",
            "message_id": _get_message_id(message),
        }
    document = message.get("document") or {}
    if document:
        return {
            "file_id": _first_present(document, "file_id", "fileId"),
            "file_unique_id": _first_present(document, "file_unique_id", "fileUniqueId"),
            "kind": "document",
            "file_name": _first_present(document, "file_name", "fileName") or "telegram-receipt",
            "mime_type": _first_present(document, "mime_type", "mimeType") or "",
            "message_id": _get_message_id(message),
        }
    return {}


def receipt_file_type_error(file_info):
    if not file_info or file_info.get("kind") == "photo":
        return ""
    mime_type = str(file_info.get("mime_type") or "").lower()
    if mime_type and mime_type not in PAYMENT_RECEIPT_ALLOWED_CONTENT_TYPES:
        return "فایل رسید باید تصویر JPG، PNG، WEBP یا GIF باشد."
    extension = PurePosixPath(file_info.get("file_name") or "").suffix.lower()
    if extension and extension not in PAYMENT_RECEIPT_ALLOWED_EXTENSIONS:
        return "فایل رسید باید تصویر JPG، PNG، WEBP یا GIF باشد."
    return ""


def safe_receipt_filename(file_info):
    raw_name = PurePosixPath(file_info.get("file_name") or "telegram-receipt").name
    raw_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip(".-") or "telegram-receipt"
    if "." not in raw_name:
        raw_name = f"{raw_name}.jpg"
    return raw_name[:120]


def download_receipt_content(client, file_info, metadata, *, delivery_error_cls):
    file_id = file_info.get("file_id")
    if not file_id:
        return None
    try:
        file_payload = client.get_file(file_id)
        result = file_payload.get("result") or {}
        file_path = result.get("file_path") or result.get("filePath") or ""
        metadata["receipt"]["file_path"] = file_path
        content = client.download_file(file_path)
    except delivery_error_cls as exc:
        metadata["receipt"]["download_error"] = str(exc)
        return None
    if not content:
        return None
    return ContentFile(content, name=safe_receipt_filename(file_info))


def build_bot_receipt_metadata(config, file_info):
    return {
        "provider": config.provider,
        "kind": file_info.get("kind"),
        "file_id": file_info.get("file_id"),
        "file_unique_id": file_info.get("file_unique_id"),
        "message_id": file_info.get("message_id"),
        "file_name": file_info.get("file_name"),
        "mime_type": file_info.get("mime_type", ""),
    }


def attach_bot_receipt(client, config, file_info, metadata, *, delivery_error_cls):
    if not file_info:
        return None
    metadata["receipt"] = build_bot_receipt_metadata(config, file_info)
    receipt_image = download_receipt_content(client, file_info, metadata, delivery_error_cls=delivery_error_cls)
    if receipt_image is None:
        metadata["receipt"]["download_error"] = (
            metadata["receipt"].get("download_error")
            or "file_download_unavailable"
        )
    return receipt_image
