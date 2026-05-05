import json
import tempfile
from io import BytesIO
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .middleware import (
    CUSTOMER_COOKIE_NAME,
    LEGACY_CUSTOMER_COOKIE_NAME,
    LEGACY_CUSTOMER_COOKIE_SALT,
)
from .models import BotConfiguration, BotUser, Customer, DiscountCode, Inbound, Order, Panel, Plan, Store, VPNClient
from .order_services import create_manual_payment_order
from .receipt_analysis import analyze_receipt_text, extract_receipt_amount_candidates


class DummyBotResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload or {"ok": True, "result": {}}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def fake_client_result(uuid="11111111-1111-4111-8111-111111111111"):
    return {
        "uuid": uuid,
        "email": "bot_user_11111111",
        "sub_id": "sub123",
        "sub_link": "https://example.com/sub/sub123",
        "direct_link": "vless://example",
        "raw": {"id": uuid, "email": "bot_user_11111111"},
    }


def image_bytes(image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(output, format=image_format)
    return output.getvalue()


class CustomerTrackingCookieTests(TestCase):
    def test_new_visitor_gets_browser_customer_key(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(CUSTOMER_COOKIE_NAME, response.cookies)
        self.assertNotIn(LEGACY_CUSTOMER_COOKIE_NAME, response.cookies)
        self.assertEqual(Customer.objects.count(), 1)

        self.client.get(reverse("home"))
        self.assertEqual(Customer.objects.count(), 1)

    def test_legacy_customer_cookie_is_migrated_to_customer_key(self):
        customer = Customer.objects.create(display_name="Browser Customer")
        legacy_response = HttpResponse()
        legacy_response.set_signed_cookie(
            LEGACY_CUSTOMER_COOKIE_NAME,
            str(customer.public_id),
            salt=LEGACY_CUSTOMER_COOKIE_SALT,
        )
        self.client.cookies[LEGACY_CUSTOMER_COOKIE_NAME] = legacy_response.cookies[
            LEGACY_CUSTOMER_COOKIE_NAME
        ].value

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(CUSTOMER_COOKIE_NAME, response.cookies)
        self.assertIn(LEGACY_CUSTOMER_COOKIE_NAME, response.cookies)
        self.assertEqual(response.cookies[LEGACY_CUSTOMER_COOKIE_NAME]["max-age"], 0)
        self.assertEqual(Customer.objects.count(), 1)


class ReceiptTextAnalysisTests(TestCase):
    def test_matches_persian_rial_amount_for_toman_order(self):
        result = analyze_receipt_text(
            "مبلغ انتقال ۱,۰۰۰,۰۰۰ ریال با موفقیت انجام شد.",
            expected_amount=100000,
            currency=Plan.Currency.TOMAN,
        )

        self.assertEqual(result["status"], "matched")
        self.assertFalse(result["requires_admin_review"])
        self.assertEqual(result["matched_amount_irr"], 1000000)

    def test_converts_toman_unit_inside_receipt_text(self):
        result = analyze_receipt_text(
            "رسید پرداخت مبلغ ۱۰۰,۰۰۰ تومان",
            expected_amount=100000,
            currency=Plan.Currency.TOMAN,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["matched_amount_irr"], 1000000)

    def test_mismatch_keeps_detected_amount_for_admin_review(self):
        result = analyze_receipt_text(
            "مبلغ واریز ۹۰۰,۰۰۰ ریال",
            expected_amount=100000,
            currency=Plan.Currency.TOMAN,
        )

        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["requires_admin_review"])
        self.assertEqual(result["matched_amount_irr"], 900000)

    def test_ignores_time_when_finding_amount_candidates(self):
        candidates = extract_receipt_amount_candidates("پرداخت در ساعت ۱۴:۳۵ انجام شد.")

        self.assertEqual(candidates, [])


class OrderQuantityPricingTests(TestCase):
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
            name="1 GB",
            slug="quantity-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def create_order(self, *, quantity=1, discount_code=""):
        return create_manual_payment_order(
            store=self.store,
            customer=None,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name="Alice Buyer",
            sender_card_last4="1234",
            payment_time="14:35",
            bank_tracking_code="TRK123",
            discount_code=discount_code,
            quantity=quantity,
            metadata={"source": "test"},
        )

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_quantity_one_uses_unit_price(self, _xui):
        result = self.create_order(quantity=1)

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.quantity, 1)
        self.assertEqual(order.original_amount, 100000)
        self.assertEqual(order.discount_amount, 0)
        self.assertEqual(order.amount, 100000)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_quantity_above_one_uses_subtotal(self, _xui):
        result = self.create_order(quantity=3)

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.quantity, 3)
        self.assertEqual(order.original_amount, 300000)
        self.assertEqual(order.discount_amount, 0)
        self.assertEqual(order.amount, 300000)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_final_price_subtracts_fixed_discount_from_subtotal(self, _xui):
        DiscountCode.objects.create(
            code="SAVE25",
            discount_type=DiscountCode.DiscountType.FIXED,
            value=25000,
        )

        result = self.create_order(quantity=2, discount_code="SAVE25")

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 200000)
        self.assertEqual(order.discount_amount, 25000)
        self.assertEqual(order.amount, 175000)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_percentage_discount_applies_to_multiple_quantity_subtotal(self, _xui):
        DiscountCode.objects.create(
            code="MULTI20",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            value=20,
        )

        result = self.create_order(quantity=4, discount_code="MULTI20")

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 400000)
        self.assertEqual(order.discount_amount, 80000)
        self.assertEqual(order.amount, 320000)

    def test_invalid_quantity_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self.create_order(quantity=0)

        with self.assertRaises(ValidationError):
            self.create_order(quantity=51)


class WholesaleAutoDiscountTests(TestCase):
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
            name="1 GB",
            slug="wholesale-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.normal_customer = Customer.objects.create(display_name="Normal Customer")
        self.wholesale_customer = Customer.objects.create(
            display_name="Wholesale Customer",
            is_wholesale=True,
            default_discount_percent=30,
        )

    def create_order(self, *, customer, discount_code=""):
        return create_manual_payment_order(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name="Alice Buyer",
            sender_card_last4="1234",
            payment_time="14:35",
            bank_tracking_code="TRK123",
            discount_code=discount_code,
            metadata={"source": "test"},
        )

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_normal_customer_without_code_pays_full_price(self, _xui):
        result = self.create_order(customer=self.normal_customer)

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 100000)
        self.assertEqual(order.discount_amount, 0)
        self.assertEqual(order.amount, 100000)
        self.assertEqual(order.discount_source, Order.DiscountSource.NONE)
        self.assertIsNone(order.discount_code)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_normal_customer_with_manual_code_uses_coupon(self, _xui):
        discount = DiscountCode.objects.create(
            code="SAVE20",
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            value=20,
        )

        result = self.create_order(customer=self.normal_customer, discount_code="SAVE20")

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 100000)
        self.assertEqual(order.discount_amount, 20000)
        self.assertEqual(order.amount, 80000)
        self.assertEqual(order.discount_source, Order.DiscountSource.MANUAL)
        self.assertEqual(order.discount_code, discount)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_wholesale_customer_without_code_gets_default_discount(self, _xui):
        result = self.create_order(customer=self.wholesale_customer)

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 100000)
        self.assertEqual(order.discount_amount, 30000)
        self.assertEqual(order.amount, 70000)
        self.assertEqual(order.discount_source, Order.DiscountSource.WHOLESALE)
        self.assertIsNone(order.discount_code)
        self.assertEqual(order.discount_code_text, "WHOLESALE 30%")

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_wholesale_customer_manual_code_overrides_default_discount(self, _xui):
        discount = DiscountCode.objects.create(
            code="MANUAL10",
            discount_type=DiscountCode.DiscountType.FIXED,
            value=10000,
        )

        result = self.create_order(customer=self.wholesale_customer, discount_code="MANUAL10")

        self.assertTrue(result.success)
        order = result.order
        self.assertEqual(order.original_amount, 100000)
        self.assertEqual(order.discount_amount, 10000)
        self.assertEqual(order.amount, 90000)
        self.assertEqual(order.discount_source, Order.DiscountSource.MANUAL)
        self.assertEqual(order.discount_code, discount)
        self.assertEqual(order.discount_code_text, "MANUAL10")


class WebCheckoutReceiptTests(TestCase):
    def setUp(self):
        self.media_tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_tmp.cleanup)

        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="1 GB",
            slug="web-checkout-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def checkout_payload(self, receipt=None, receipt_text=""):
        payload = {
            "plan_id": str(self.plan.pk),
            "sender_card_name": "Alice Buyer",
            "payment_time": "14:35",
            "quantity": "1",
            "payment_receipt_text": receipt_text,
        }
        if receipt is not None:
            payload["payment_receipt_image"] = receipt
        return payload

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_saves_valid_receipt_image(self, _xui):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(reverse("home"), data=self.checkout_payload(receipt))

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_last4, "")
        self.assertTrue(order.payment_receipt_image.name.endswith(".png"))
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "image_only")

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_accepts_receipt_text_without_image(self, _xui):
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(receipt_text="مبلغ انتقال ۱,۰۰۰,۰۰۰ ریال با موفقیت انجام شد."),
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_last4, "")
        self.assertEqual(order.metadata["receipt_text"], "مبلغ انتقال ۱,۰۰۰,۰۰۰ ریال با موفقیت انجام شد.")
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "matched")
        self.assertFalse(order.metadata["receipt_analysis"]["requires_admin_review"])
        self.assertFalse(order.bank_tracking_code)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_saves_receipt_amount_mismatch_for_admin_review(self, _xui):
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(receipt_text="مبلغ انتقال ۹۰۰,۰۰۰ ریال با موفقیت انجام شد."),
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "mismatch")
        self.assertTrue(order.metadata["receipt_analysis"]["requires_admin_review"])
        self.assertEqual(order.metadata["receipt_analysis"]["matched_amount_irr"], 900000)
        from .bots import format_order_message

        admin_message = format_order_message(order)
        self.assertIn("Receipt check", admin_message)
        self.assertIn("Manual receipt review needed", admin_message)

    @patch("store.order_services.create_inactive_client_details")
    def test_web_checkout_requires_receipt_text_or_image_before_panel_call(self, xui_mock):
        response = self.client.post(reverse("home"), data=self.checkout_payload())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "رسید را به صورت متن وارد کن یا عکس رسید را بفرست.")
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.order_services.create_inactive_client_details")
    def test_web_checkout_rejects_non_image_receipt_before_panel_call(self, xui_mock):
        receipt = SimpleUploadedFile("receipt.txt", b"not an image", content_type="text/plain")

        response = self.client.post(reverse("home"), data=self.checkout_payload(receipt))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Receipt file must be a JPG, PNG, WEBP, or GIF image.")
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()


class TelegramPurchaseFlowTests(TestCase):
    def setUp(self):
        self.media_tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_tmp.cleanup)

        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="1 GB",
            slug="1gb",
            volume_gb="1.000",
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        self.url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])

    def post_update(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def message(self, text, *, message_id=1):
        return {
            "message": {
                "message_id": message_id,
                "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                "chat": {"id": 42, "type": "private"},
                "text": text,
            }
        }

    def callback(self, data, *, message_id=10, callback_id="cb"):
        return {
            "callback_query": {
                "id": callback_id,
                "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                "message": {"message_id": message_id, "chat": {"id": 42, "type": "private"}},
                "data": data,
            }
        }

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_creates_bot_user_and_customer(self, _post):
        response = self.post_update(self.message("/start"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.chat_id, "42")
        self.assertEqual(bot_user.username, "alice")
        self.assertIsNotNone(bot_user.customer)
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        self.assertEqual(Customer.objects.count(), 1)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_purchase_without_receipt_creates_pending_order(self, _post, _xui):
        self.post_update(self.message("/start"))
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.message("Alice Buyer", message_id=2))
        self.post_update(self.message("1234", message_id=3))
        self.post_update(self.message("14:35", message_id=4))
        self.post_update(self.message("TRK123", message_id=5))
        response = self.post_update(self.message("/skip", message_id=6))

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(order.customer, bot_user.customer)
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertTrue(order.is_paid)
        self.assertEqual(order.sender_card_name, "Alice Buyer")
        self.assertEqual(order.sender_card_last4, "1234")
        self.assertEqual(order.bank_tracking_code, "TRK123")
        self.assertEqual(order.metadata["source"], "telegram_bot")
        self.assertEqual(order.metadata["bot"]["provider_user_id"], "42")

        vpn_client = VPNClient.objects.get(order=order)
        self.assertEqual(vpn_client.status, VPNClient.Status.INACTIVE)
        self.assertEqual(vpn_client.sub_link, "https://example.com/sub/sub123")
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("33333333-3333-4333-8333-333333333333"))
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_bale_purchase_flow_is_enabled_for_non_admin_users(self, _post, _xui):
        self.bot_config.provider = BotConfiguration.Provider.BALE
        self.bot_config.save(update_fields=["provider", "updated_at"])
        self.url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])

        self.post_update(self.message("/start"))
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.message("Alice Buyer", message_id=2))
        self.post_update(self.message("1234", message_id=3))
        self.post_update(self.message("14:35", message_id=4))
        self.post_update(self.message("TRK123", message_id=5))
        response = self.post_update(self.message("/skip", message_id=6))

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.metadata["source"], "bale_bot")
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.bot_config.provider, BotConfiguration.Provider.BALE)

    @patch("store.bots.send_to_config", return_value=True)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_webhook_triggers_due_sales_report_once(self, _post, send_to_config_mock):
        self.bot_config.last_report_sent_at = timezone.now() - timedelta(hours=7)
        self.bot_config.report_interval_hours = 6
        self.bot_config.save(update_fields=["last_report_sent_at", "report_interval_hours", "updated_at"])

        first_response = self.post_update(self.message("/start"))
        second_response = self.post_update(self.message("/plans", message_id=2))

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(send_to_config_mock.call_count, 1)
        self.bot_config.refresh_from_db()
        self.assertGreater(self.bot_config.last_report_sent_at, timezone.now() - timedelta(minutes=1))

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("22222222-2222-4222-8222-222222222222"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_telegram_receipt_photo_is_saved_and_file_id_is_preserved(self, _get, _xui):
        def post_side_effect(url, json=None, **kwargs):
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            self.post_update(self.message("1234", message_id=3))
            self.post_update(self.message("14:35", message_id=4))
            self.post_update(self.message("-", message_id=5))
            response = self.post_update(
                {
                    "message": {
                        "message_id": 6,
                        "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                        "chat": {"id": 42, "type": "private"},
                        "photo": [
                            {"file_id": "small-file", "file_unique_id": "small"},
                            {"file_id": "large-file", "file_unique_id": "large"},
                        ],
                    }
                }
            )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertTrue(order.payment_receipt_image.name)
        self.assertEqual(order.metadata["receipt"]["file_id"], "large-file")
        self.assertEqual(order.metadata["receipt"]["file_unique_id"], "large")
        self.assertEqual(order.metadata["receipt"]["file_path"], "photos/receipt.jpg")
