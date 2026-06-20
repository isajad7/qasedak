import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .bots import handle_bot_update
from .bot_proxy import telegram_webhook_response_context
from .models import BotConfiguration, BotEventLog
from .telegram_bot.redaction import sanitize_bot_event_log_value

logger = logging.getLogger(__name__)


def safe_webhook_response(payload=None):
    return JsonResponse(payload or {"ok": True}, status=200)


def log_webhook_error(provider, webhook_secret, *, message, raw_payload=None):
    logger.exception("Bot webhook error provider=%s secret=%s: %s", provider, webhook_secret, message)
    config = BotConfiguration.objects.filter(
        provider=provider,
        webhook_secret=webhook_secret,
    ).first()
    if config:
        BotEventLog.objects.create(
            bot_config=config,
            event_type=BotEventLog.EventType.ERROR,
            status=BotEventLog.Status.FAILED,
            message=sanitize_bot_event_log_value(message),
            raw_payload=sanitize_bot_event_log_value(raw_payload or {}),
        )


@csrf_exempt
def bot_webhook(request, provider, webhook_secret):
    if request.method == "GET":
        return safe_webhook_response(
            {
                "ok": True,
                "provider": provider,
                "webhook": "reachable",
                "message": "Webhook endpoint is reachable. Bot providers must POST updates here.",
            }
        )

    if request.method != "POST":
        return safe_webhook_response({"ok": True, "ignored": True, "error": "Unsupported method."})

    if provider not in BotConfiguration.Provider.values:
        logger.warning("Unsupported bot provider received: %s", provider)
        return safe_webhook_response({"ok": True, "ignored": True, "error": "Unsupported provider."})

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        logger.warning("Invalid bot webhook JSON provider=%s secret=%s body=%r", provider, webhook_secret, request.body[:500])
        config = BotConfiguration.objects.filter(provider=provider, webhook_secret=webhook_secret).first()
        if config:
            BotEventLog.objects.create(
                bot_config=config,
                event_type=BotEventLog.EventType.WEBHOOK,
                status=BotEventLog.Status.FAILED,
                message="Invalid JSON payload.",
                raw_payload=sanitize_bot_event_log_value(
                    {"body": request.body.decode("utf-8", errors="replace")[:2000]}
                ),
            )
        return safe_webhook_response({"ok": True, "ignored": True, "error": "Invalid JSON."})

    webhook_response = None
    try:
        with telegram_webhook_response_context(provider) as response_context:
            result = handle_bot_update(provider, webhook_secret, payload)
            if response_context is not None:
                webhook_response = response_context.payload
    except Exception as exc:
        log_webhook_error(
            provider,
            webhook_secret,
            message=str(exc),
            raw_payload=payload if isinstance(payload, dict) else {"payload": payload},
        )
        return safe_webhook_response({"ok": True, "handled": False, "error": "Webhook handler failed."})

    if webhook_response:
        return safe_webhook_response(webhook_response)
    return safe_webhook_response(result)
