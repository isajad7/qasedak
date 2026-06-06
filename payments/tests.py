from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from store.models import BotConfiguration, Order, Plan, Store

from .models import IncomingPaymentSMS
from .payment_matching import confirm_incoming_payment_sms, find_matching_orders, process_incoming_payment_sms
from .sms_parser import SMSParseError, normalize_number, parse_payment_sms


SAMPLE_SMS = """بلو
واریز پول
سجاد عزیز، 4,830,000 ریال به حساب شما نشست.
موجودی: 109,609,358 ریال
۱۸:۵۶
۱۴۰۵.۰۲.۱۴"""


class DummyBotResponse:
    def __init__(self, payload=None):
        self.payload = payload or {"ok": True, "result": {}}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@override_settings(PAYMENT_SMS_TIME_ZONE="Asia/Tehran")
class PaymentSMSParserTests(TestCase):
    def test_parse_sample_sms_extracts_amount_balance_and_aware_datetime(self):
        parsed = parse_payment_sms(SAMPLE_SMS)

        self.assertEqual(parsed.amount, 4830000)
        self.assertEqual(parsed.balance, 109609358)
        self.assertTrue(timezone.is_aware(parsed.sms_datetime))

        local_datetime = timezone.localtime(parsed.sms_datetime, ZoneInfo("Asia/Tehran"))
        self.assertEqual(local_datetime.year, 2026)
        self.assertEqual(local_datetime.month, 5)
        self.assertEqual(local_datetime.day, 4)
        self.assertEqual(local_datetime.hour, 18)
        self.assertEqual(local_datetime.minute, 56)

    def test_parse_supports_arabic_digits_and_unformatted_amounts(self):
        text = """بلو
واریز ٤٨٣٠٠٠٠ ريال به حساب شما نشست
موجودي: ١٠٩٦٠٩٣٥٨ ريال
18:56
1405/2/14"""

        parsed = parse_payment_sms(text)

        self.assertEqual(parsed.amount, 4830000)
        self.assertEqual(parsed.balance, 109609358)
        self.assertEqual(timezone.localtime(parsed.sms_datetime, ZoneInfo("Asia/Tehran")).date().isoformat(), "2026-05-04")

    def test_parse_supports_persian_thousands_separator_and_spaced_time(self):
        text = """بانک
پرداخت انجام شد
۴٬۸۳۰٬۰۰۰ ریال به حساب شما نشست
موجودی: ۱۰۹٬۶۰۹٬۳۵۸ ریال
۱۸ : ۵۶
۱۴۰۵/۰۲/۱۴"""

        parsed = parse_payment_sms(text)

        self.assertEqual(parsed.amount, 4830000)
        self.assertEqual(parsed.balance, 109609358)

    def test_normalize_number_removes_commas_and_converts_persian_digits(self):
        self.assertEqual(normalize_number("۴,۸۳۰,۰۰۰"), 4830000)

    def test_parse_rejects_sms_without_time(self):
        text = """بلو
۴٬۸۳۰٬۰۰۰ ریال به حساب شما نشست
موجودی: ۱۰۹٬۶۰۹٬۳۵۸ ریال
۱۴۰۵/۰۲/۱۴"""

        with self.assertRaises(SMSParseError):
            parse_payment_sms(text)


@override_settings(PAYMENT_SMS_TIME_ZONE="Asia/Tehran")
class PaymentMatchingTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Rial plan",
            slug="rial-plan",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=4830000,
            currency=Plan.Currency.IRR,
            is_active=True,
            is_public=True,
        )
        self.sms_datetime = timezone.make_aware(datetime(2026, 5, 4, 18, 56), ZoneInfo("Asia/Tehran"))

    def create_order(self, *, amount=4830000, created_at=None, status=Order.Status.PENDING_PAYMENT):
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            status=status,
            original_amount=amount,
            amount=amount,
            currency=Plan.Currency.IRR,
        )
        if created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
            order.refresh_from_db()
        return order

    def test_matching_window_includes_minus_30_and_plus_15_minutes(self):
        included_start = self.create_order(created_at=self.sms_datetime - timedelta(minutes=30))
        included_end = self.create_order(created_at=self.sms_datetime + timedelta(minutes=15))
        self.create_order(created_at=self.sms_datetime - timedelta(minutes=31))
        self.create_order(created_at=self.sms_datetime + timedelta(minutes=16))
        self.create_order(amount=4830001, created_at=self.sms_datetime)
        self.create_order(created_at=self.sms_datetime, status=Order.Status.COMPLETED)

        matches = list(find_matching_orders(4830000, self.sms_datetime))

        self.assertEqual(matches, [included_start, included_end])

    def test_process_sms_sets_no_match_when_no_candidate_exists(self):
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
        )

        matches = process_incoming_payment_sms(payment_sms, notify=False)
        payment_sms.refresh_from_db()

        self.assertEqual(matches, [])
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.NO_MATCH)
        self.assertEqual(payment_sms.matched_orders.count(), 0)

    def test_process_sms_links_matches_without_confirming_order(self):
        order = self.create_order(created_at=self.sms_datetime)
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
        )

        matches = process_incoming_payment_sms(payment_sms, notify=False)
        payment_sms.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(matches, [order])
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.MATCHED)
        self.assertEqual(list(payment_sms.matched_orders.all()), [order])
        self.assertEqual(order.status, Order.Status.PENDING_PAYMENT)

    def test_process_sms_schedules_actionable_order_review_after_commit(self):
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="SMS review bot",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        order = self.create_order(created_at=self.sms_datetime)
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
        )
        post_calls = []
        message_id = {"value": 500}

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            if url.endswith("/sendMessage"):
                message_id["value"] += 1
                return DummyBotResponse({"ok": True, "result": {"message_id": message_id["value"]}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                matches = process_incoming_payment_sms(payment_sms, notify=True)
                self.assertEqual(matches, [order])
                self.assertEqual(post_calls, [])

            self.assertGreaterEqual(len(callbacks), 1)
            self.assertEqual(post_calls, [])
            for callback in callbacks:
                callback()

        order.refresh_from_db()
        self.assertIsNotNone(order.admin_receipt_notified_at)
        actionable_messages = [
            call["json"]
            for call in post_calls
            if call["url"].endswith("/sendMessage")
            and "سفارش نیازمند بررسی پرداخت" in call.get("json", {}).get("text", "")
        ]
        self.assertEqual(len(actionable_messages), 1)
        callback_values = [
            button["callback_data"]
            for row in actionable_messages[0]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"approve:{order.order_tracking_code}", callback_values)
        self.assertIn(f"reject:{order.order_tracking_code}", callback_values)
        self.assertEqual(bot_config.get_admin_user_ids(), ["999"])

    def test_process_sms_notification_is_idempotent_for_same_matches(self):
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="SMS review bot",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        order = self.create_order(created_at=self.sms_datetime)
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
        )
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            return DummyBotResponse({"ok": True, "result": {"message_id": len(post_calls)}})

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(process_incoming_payment_sms(payment_sms, notify=True), [order])
            payment_sms.refresh_from_db()
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(process_incoming_payment_sms(payment_sms, notify=True), [order])

        match_messages = [
            call["json"]
            for call in post_calls
            if call["url"].endswith("/sendMessage")
            and "Bank deposit SMS matched" in call.get("json", {}).get("text", "")
        ]
        self.assertEqual(len(match_messages), 1)

    def test_manual_confirmation_confirms_sms_and_order(self):
        order = self.create_order(created_at=self.sms_datetime)
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
            status=IncomingPaymentSMS.Status.MATCHED,
        )
        payment_sms.matched_orders.add(order)

        confirmed_order = confirm_incoming_payment_sms(payment_sms)
        payment_sms.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(confirmed_order, order)
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.CONFIRMED)
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertTrue(order.is_paid)
        self.assertEqual(order.verification_status, Order.VerificationStatus.VERIFIED)

    def test_manual_confirmation_preserves_completed_order_status(self):
        verified_at = timezone.now() - timedelta(hours=1)
        order = self.create_order(created_at=self.sms_datetime, status=Order.Status.COMPLETED)
        Order.objects.filter(pk=order.pk).update(
            is_paid=True,
            verification_status=Order.VerificationStatus.VERIFIED,
            verified_at=verified_at,
        )
        order.refresh_from_db()
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
            status=IncomingPaymentSMS.Status.MATCHED,
        )
        payment_sms.matched_orders.add(order)

        confirmed_order = confirm_incoming_payment_sms(payment_sms)
        payment_sms.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(confirmed_order, order)
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.CONFIRMED)
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.verification_status, Order.VerificationStatus.VERIFIED)
        self.assertEqual(order.verified_at, verified_at)

    def test_confirmed_sms_is_not_reprocessed_or_renotified(self):
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="SMS review bot",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        order = self.create_order(created_at=self.sms_datetime, status=Order.Status.CONFIRMED)
        payment_sms = IncomingPaymentSMS.objects.create(
            raw_text=SAMPLE_SMS,
            amount=4830000,
            balance=109609358,
            sms_datetime=self.sms_datetime,
            status=IncomingPaymentSMS.Status.CONFIRMED,
        )
        payment_sms.matched_orders.add(order)
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            with self.captureOnCommitCallbacks(execute=True):
                matches = process_incoming_payment_sms(payment_sms, notify=True)

        payment_sms.refresh_from_db()
        self.assertEqual(matches, [order])
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.CONFIRMED)
        self.assertEqual(post_calls, [])


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="secret", PAYMENT_SMS_TIME_ZONE="Asia/Tehran")
class SMSForwarderWebhookTests(TestCase):
    def test_webhook_rejects_invalid_token(self):
        response = self.client.post(
            reverse("smsforwarder_webhook"),
            data={"text": SAMPLE_SMS},
            HTTP_X_WEBHOOK_TOKEN="wrong",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(IncomingPaymentSMS.objects.count(), 0)

    def test_webhook_accepts_valid_token_and_stores_sms(self):
        response = self.client.post(
            reverse("smsforwarder_webhook"),
            data={"text": SAMPLE_SMS},
            HTTP_X_WEBHOOK_TOKEN="secret",
        )

        self.assertEqual(response.status_code, 200)
        payment_sms = IncomingPaymentSMS.objects.get()
        self.assertEqual(payment_sms.amount, 4830000)
        self.assertEqual(payment_sms.balance, 109609358)
        self.assertEqual(payment_sms.status, IncomingPaymentSMS.Status.NO_MATCH)

    def test_webhook_accepts_query_token_for_smsforwarder_apps_without_headers(self):
        response = self.client.post(
            f"{reverse('smsforwarder_webhook')}?token=secret",
            data={"message": SAMPLE_SMS},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IncomingPaymentSMS.objects.count(), 1)
