import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .bots import handle_bot_update
from .models import BotConfiguration, BotEventLog

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
            message=message,
            raw_payload=raw_payload or {},
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
                raw_payload={"body": request.body.decode("utf-8", errors="replace")[:2000]},
            )
        return safe_webhook_response({"ok": True, "ignored": True, "error": "Invalid JSON."})

    try:
        result = handle_bot_update(provider, webhook_secret, payload)
    except Exception as exc:
        log_webhook_error(
            provider,
            webhook_secret,
            message=str(exc),
            raw_payload=payload if isinstance(payload, dict) else {"payload": payload},
        )
        return safe_webhook_response({"ok": True, "handled": False, "error": "Webhook handler failed."})

    return safe_webhook_response(result)
