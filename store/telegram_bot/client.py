import json
from pathlib import PurePosixPath

import requests
from django.conf import settings

from store.bot_proxy import bot_request_kwargs, capture_telegram_webhook_response
from store.models import BotConfiguration


class BotDeliveryError(Exception):
    pass


def bot_api_timeout():
    return (
        getattr(settings, "BOT_API_CONNECT_TIMEOUT_SECONDS", 3),
        getattr(settings, "BOT_API_READ_TIMEOUT_SECONDS", 8),
    )


class BotClient:
    BASE_URLS = {
        BotConfiguration.Provider.BALE: "https://tapi.bale.ai/bot{token}",
        BotConfiguration.Provider.TELEGRAM: "https://api.telegram.org/bot{token}",
    }
    FILE_BASE_URLS = {
        BotConfiguration.Provider.BALE: "https://tapi.bale.ai/file/bot{token}",
        BotConfiguration.Provider.TELEGRAM: "https://api.telegram.org/file/bot{token}",
    }

    def __init__(self, config):
        self.config = config
        self.base_url = self.BASE_URLS[config.provider].format(token=config.bot_token).rstrip("/")

    def sanitized_error(self, exc):
        message = str(exc)
        token = str(self.config.bot_token or "")
        if token:
            message = message.replace(token, "<redacted-token>")
        return message

    def call(self, method, payload, *, timeout=None):
        if capture_telegram_webhook_response(self.config.provider, method, payload):
            return {"ok": True, "result": {}}

        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=timeout or bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                data = {}
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None

        if getattr(response, "ok", True) is False:
            raise BotDeliveryError(
                data.get("description")
                or data.get("message")
                or getattr(response, "reason", "")
                or "Bot API request failed."
            )
        if data.get("ok") is False:
            raise BotDeliveryError(data.get("description") or data.get("message") or "Bot API rejected request.")
        return data

    def call_multipart(self, method, *, data, files):
        try:
            response = requests.post(
                f"{self.base_url}/{method}",
                data=data,
                files=files,
                timeout=bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            try:
                payload = response.json()
            except ValueError:
                response.raise_for_status()
                payload = {}
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None

        if getattr(response, "ok", True) is False:
            raise BotDeliveryError(
                payload.get("description")
                or payload.get("message")
                or getattr(response, "reason", "")
                or "Bot API request failed."
            )
        if payload.get("ok") is False:
            raise BotDeliveryError(payload.get("description") or payload.get("message") or "Bot API rejected request.")
        return payload

    def send_message(self, text, *, reply_markup=None, chat_id=None, parse_mode="HTML", force_parse_mode=False):
        target_chat_id = str(chat_id or self.config.admin_user_id)
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode and (force_parse_mode or self.config.is_admin_user(target_chat_id)):
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def get_chat(self, chat_id, *, timeout=None):
        return self.call("getChat", {"chat_id": str(chat_id)}, timeout=timeout)

    def send_photo(self, photo_file, *, caption="", reply_markup=None, chat_id=None):
        data = {
            "chat_id": str(chat_id or self.config.admin_user_id),
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        filename = PurePosixPath(getattr(photo_file, "name", "") or "receipt.jpg").name
        files = {"photo": (filename, photo_file)}
        return self.call_multipart("sendPhoto", data=data, files=files)

    def edit_message(self, *, chat_id, message_id, text, reply_markup=None):
        if not chat_id or not message_id:
            return None

        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        methods = ["editMessageText"]
        if self.config.provider == BotConfiguration.Provider.BALE:
            methods.append("editMessage")

        last_error = None
        for method in methods:
            try:
                return self.call(method, payload)
            except BotDeliveryError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return None

    def edit_caption(self, *, chat_id, message_id, caption, reply_markup=None):
        if not chat_id or not message_id:
            return None

        payload = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        methods = ["editMessageCaption"]
        if self.config.provider == BotConfiguration.Provider.BALE:
            methods.append("editMessage")

        last_error = None
        for method in methods:
            try:
                return self.call(method, payload)
            except BotDeliveryError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return None

    def forward_message(self, *, from_chat_id, message_id, chat_id=None):
        if not from_chat_id or not message_id:
            return None
        return self.call(
            "forwardMessage",
            {
                "chat_id": str(chat_id or self.config.admin_user_id),
                "from_chat_id": str(from_chat_id),
                "message_id": message_id,
            },
        )

    def delete_message(self, *, chat_id, message_id):
        if not chat_id or not message_id:
            return None
        return self.call(
            "deleteMessage",
            {
                "chat_id": str(chat_id),
                "message_id": message_id,
            },
        )

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path):
        if not file_path:
            return None
        file_base = self.FILE_BASE_URLS[self.config.provider].format(token=self.config.bot_token).rstrip("/")
        file_url = f"{file_base}/{file_path.lstrip('/')}"
        try:
            response = requests.get(
                file_url,
                timeout=bot_api_timeout(),
                **bot_request_kwargs(self.config.provider),
            )
            response.raise_for_status()
        except Exception as exc:
            raise BotDeliveryError(self.sanitized_error(exc)) from None
        return response.content

    def answer_callback(self, callback_query_id, text=""):
        if not callback_query_id:
            return None
        try:
            return self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": False,
                },
            )
        except BotDeliveryError:
            return None

    def get_me(self):
        return self.call("getMe", {})

    def delete_webhook(self, *, drop_pending_updates=False):
        return self.call("deleteWebhook", {"drop_pending_updates": bool(drop_pending_updates)})

    def get_updates(self, *, offset=None, timeout=20, limit=100, allowed_updates=None):
        payload = {
            "timeout": int(timeout),
            "limit": int(limit),
            "allowed_updates": allowed_updates or ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        connect_timeout, read_timeout = bot_api_timeout()
        polling_timeout = (connect_timeout, max(read_timeout, int(timeout) + 5))
        return self.call("getUpdates", payload, timeout=polling_timeout)
