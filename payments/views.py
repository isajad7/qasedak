import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import IncomingPaymentSMS
from .payment_matching import process_incoming_payment_sms
from .sms_parser import SMSParseError, parse_payment_sms

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def smsforwarder_webhook(request):
    expected_token = getattr(settings, "SMSFORWARDER_WEBHOOK_TOKEN", "")
    if not expected_token:
        return JsonResponse({"ok": False, "error": "SMS webhook token is not configured."}, status=503)

    provided_token = extract_webhook_token(request)
    if not constant_time_compare(str(provided_token), str(expected_token)):
        return JsonResponse({"ok": False, "error": "Invalid webhook token."}, status=403)

    raw_text = extract_sms_text(request)
    if not raw_text:
        return JsonResponse({"ok": False, "error": "SMS text was not provided."}, status=400)

    try:
        parsed = parse_payment_sms(raw_text)
    except SMSParseError as exc:
        logger.info("Ignored unparsable SMS webhook payload: %s", exc)
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    payment_sms = IncomingPaymentSMS.objects.create(
        raw_text=raw_text,
        amount=parsed.amount,
        balance=parsed.balance,
        sms_datetime=parsed.sms_datetime,
    )
    matched_orders = process_incoming_payment_sms(payment_sms)

    return JsonResponse(
        {
            "ok": True,
            "sms_id": payment_sms.pk,
            "status": payment_sms.status,
            "matched_order_ids": [order.pk for order in matched_orders],
        }
    )


def extract_sms_text(request):
    content_type = request.META.get("CONTENT_TYPE", "")
    body = request.body.decode("utf-8", errors="replace").strip()

    if "application/json" in content_type and body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        return _extract_text_from_payload(payload)

    if request.POST:
        return _extract_text_from_payload(request.POST)

    return body


def extract_webhook_token(request):
    token = request.headers.get("X-Webhook-Token") or request.GET.get("token") or request.POST.get("token")
    if token:
        return token

    content_type = request.META.get("CONTENT_TYPE", "")
    if "application/json" not in content_type:
        return ""

    body = request.body.decode("utf-8", errors="replace").strip()
    if not body:
        return ""

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""

    if isinstance(payload, dict):
        value = payload.get("token")
        if isinstance(value, str):
            return value.strip()

    return ""


def _extract_text_from_payload(payload):
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, dict):
        for key in ("raw_text", "text", "message", "body", "sms", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for key in ("data", "payload", "sms"):
            value = payload.get(key)
            text = _extract_text_from_payload(value)
            if text:
                return text

    return ""
