import json
import base64
import tempfile
import threading
from io import BytesIO, StringIO
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .middleware import (
    CUSTOMER_COOKIE_NAME,
    CUSTOMER_COOKIE_SALT,
    LEGACY_CUSTOMER_COOKIE_NAME,
    LEGACY_CUSTOMER_COOKIE_SALT,
)
from .models import (
    BotConfiguration,
    BotAdminOrderMessage,
    BotEventLog,
    BotPendingAction,
    BotUser,
    BroadcastMessage,
    BroadcastRecipient,
    Customer,
    DiscountCode,
    FreeTrialRequest,
    Inbound,
    Order,
    Operator,
    Panel,
    Plan,
    Referral,
    ReferralRewardLedger,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    VPNClientReminderLog,
)
from .broadcast_services import (
    create_campaign_recipients,
    get_customers_for_audience,
    resolve_campaign_recipients,
    send_campaign,
)
from .order_actions import activate_order, reject_order
from .bots import format_customer_analytics_report
from .customer_analytics import (
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_7_DAYS,
    PERIOD_TODAY,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_LOYAL,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_BUYER,
    SEGMENT_TOP_REFERRER,
    get_customer_segment,
    get_customer_stats,
    get_customers_by_segment,
    get_period_range,
)
from .order_services import create_manual_payment_order, get_store_plans
from .referral_services import (
    apply_referral_code,
    build_telegram_referral_link,
    create_referral_reward_for_order,
    ensure_referral_code,
    get_available_referral_gb,
    get_referral_summary,
    redeem_referral_rewards,
)
from .receipt_analysis import analyze_receipt_text, extract_receipt_amount_candidates


class DummyBotResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload or {"ok": True, "result": {}}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DummyXUIResponse:
    status_code = 200

    def __init__(self, payload=None):
        self.payload = payload or {"success": True}

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


class TelegramProxyTests(TestCase):
    proxy_url = "http://proxy-user:proxy-pass@proxy.example:7880"
    expected_proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    def test_bot_event_log_redacts_config_links(self):
        from .bots import log_event

        store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
        )
        config = BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )
        vless_link = "vless://11111111-1111-4111-8111-111111111111@example.com:443?type=tcp#private"
        ss_link = "ss://secret@example.com:443#private"

        event = log_event(
            config,
            event_type=BotEventLog.EventType.WEBHOOK,
            status=BotEventLog.Status.RECEIVED,
            message=f"User sent {vless_link}",
            raw_payload={"message": {"text": vless_link}, "links": [ss_link]},
        )

        self.assertIn("<config-link-redacted>", event.message)
        self.assertNotIn("vless://", event.message)
        payload_text = json.dumps(event.raw_payload, ensure_ascii=False)
        self.assertNotIn("vless://", payload_text)
        self.assertNotIn("ss://", payload_text)
        self.assertIn("<config-link-redacted>", payload_text)

    @override_settings(
        TELEGRAM_PROXY_URL="",
        TELEGRAM_PROXY_PROTOCOL="http",
        TELEGRAM_PROXY_HOST="proxy.example",
        TELEGRAM_PROXY_PORT="7880",
        TELEGRAM_PROXY_USERNAME="proxy user",
        TELEGRAM_PROXY_PASSWORD="p@ss",
    )
    def test_structured_proxy_settings_build_proxy_url(self):
        from .bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url

        proxy_url = telegram_proxy_url()

        self.assertEqual(proxy_url, "http://proxy%20user:p%40ss@proxy.example:7880")
        self.assertEqual(sanitized_telegram_proxy_url(proxy_url), "http://proxy%20user:****@proxy.example:7880")

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    def test_proxy_kwargs_are_limited_to_telegram_provider(self):
        from .bot_proxy import bot_request_kwargs

        self.assertEqual(
            bot_request_kwargs(BotConfiguration.Provider.TELEGRAM),
            {"proxies": self.expected_proxies},
        )
        self.assertEqual(bot_request_kwargs(BotConfiguration.Provider.BALE), {})

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_bot_api_calls_use_configured_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).send_message("hello", chat_id="42")

        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)

    @override_settings(TELEGRAM_PROXY_URL=proxy_url, BOT_API_CONNECT_TIMEOUT_SECONDS=4, BOT_API_READ_TIMEOUT_SECONDS=1)
    @patch("store.bots.requests.post", return_value=DummyBotResponse(payload={"ok": True, "result": []}))
    def test_telegram_get_updates_uses_configured_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).get_updates(timeout=2, limit=1)

        self.assertEqual(post_mock.call_args.args[0], "https://api.telegram.org/bottelegram-token/getUpdates")
        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)
        self.assertEqual(post_mock.call_args.kwargs["json"]["timeout"], 2)
        self.assertEqual(post_mock.call_args.kwargs["timeout"], (4, 7))

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_delete_webhook_keeps_pending_updates(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).delete_webhook(drop_pending_updates=False)

        self.assertEqual(post_mock.call_args.args[0], "https://api.telegram.org/bottelegram-token/deleteWebhook")
        self.assertEqual(post_mock.call_args.kwargs["json"], {"drop_pending_updates": False})
        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_non_telegram_bot_api_calls_do_not_use_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.BALE,
            name="Bale",
            bot_token="bale-token",
            admin_user_id="42",
        )

        BotClient(config).send_message("hello", chat_id="42")

        self.assertNotIn("proxies", post_mock.call_args.kwargs)

    @override_settings(TELEGRAM_WEBHOOK_RESPONSE_ENABLED=True)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_webhook_response_can_capture_send_message(self, post_mock):
        from .bot_proxy import telegram_webhook_response_context
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        with telegram_webhook_response_context(BotConfiguration.Provider.TELEGRAM) as response_context:
            BotClient(config).send_message("hello", chat_id="42")

        self.assertEqual(
            response_context.payload,
            {
                "method": "sendMessage",
                "chat_id": "42",
                "text": "hello",
                "disable_web_page_preview": True,
                "parse_mode": "HTML",
            },
        )
        post_mock.assert_not_called()


class TelegramPollingTests(TestCase):
    def test_polling_command_discovers_new_bot_configs_while_running(self):
        from store.management.commands.run_telegram_polling import Command

        first_config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling first",
            bot_token="telegram-token-1",
            admin_user_id="42",
        )
        second_config = None
        started_config_ids = []

        class FakeThread:
            def __init__(self, target, name, daemon):
                self.name = name
                self.started = False

            def start(self):
                self.started = True
                started_config_ids.append(int(self.name.rsplit("-", 1)[-1]))

            def is_alive(self):
                return self.started

            def join(self, timeout=None):
                return None

        class FakeStopEvent:
            def __init__(self):
                self.wait_calls = 0
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                nonlocal second_config
                self.wait_calls += 1
                if self.wait_calls == 1:
                    second_config = BotConfiguration.objects.create(
                        provider=BotConfiguration.Provider.TELEGRAM,
                        name="Telegram polling second",
                        bot_token="telegram-token-2",
                        admin_user_id="42",
                    )
                else:
                    self.stopped = True
                return self.stopped

        command = Command()
        command._log = lambda *args, **kwargs: None
        options = {
            "config_id": None,
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "discovery_interval": 0.1,
            "skip_delete_webhook": False,
        }

        with patch("store.management.commands.run_telegram_polling.threading.Thread", FakeThread):
            command._run_discovery_loop(FakeStopEvent(), options)

        self.assertIsNotNone(second_config)
        self.assertEqual(started_config_ids, [first_config.pk, second_config.pk])

    def test_polling_worker_dispatches_callback_query_updates(self):
        from store.management.commands.run_telegram_polling import TelegramPollingWorker

        config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling",
            bot_token="telegram-token",
            admin_user_id="42",
        )
        command = SimpleNamespace(_log=lambda *args, **kwargs: None)
        options = {
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "skip_delete_webhook": False,
        }
        callback_update = {
            "update_id": 100,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": 42},
                "message": {"message_id": 5, "chat": {"id": 42}},
                "data": "order:detail:ABC123",
            },
        }

        with (
            patch("store.management.commands.run_telegram_polling.BotClient") as client_mock,
            patch("store.management.commands.run_telegram_polling.handle_bot_update") as handle_mock,
        ):
            client_mock.return_value.get_updates.return_value = {"ok": True, "result": [callback_update]}
            worker = TelegramPollingWorker(command, config.pk, threading.Event(), options)

            self.assertEqual(worker.poll_once(), 1)

        client_mock.return_value.get_updates.assert_called_once_with(
            offset=None,
            timeout=0,
            limit=10,
            allowed_updates=["message", "callback_query"],
        )
        handle_mock.assert_called_once_with(
            BotConfiguration.Provider.TELEGRAM,
            config.webhook_secret,
            callback_update,
            source="polling",
        )
        self.assertEqual(worker.offset, 101)

    def test_polling_worker_continues_after_delivery_failure(self):
        from store.bots import BotDeliveryError
        from store.management.commands.run_telegram_polling import TelegramPollingWorker

        config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling",
            bot_token="telegram-token",
            admin_user_id="42",
        )
        logs = []
        command = SimpleNamespace(_log=lambda *args, **kwargs: logs.append(args[0]))
        options = {
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "skip_delete_webhook": False,
        }
        updates = [
            {"update_id": 100, "message": {"message_id": 1, "chat": {"id": 42}, "text": "/start"}},
            {"update_id": 101, "callback_query": {"id": "callback-id", "from": {"id": 42}, "data": "noop"}},
        ]

        with (
            patch("store.management.commands.run_telegram_polling.BotClient") as client_mock,
            patch("store.management.commands.run_telegram_polling.handle_bot_update") as handle_mock,
        ):
            client_mock.return_value.get_updates.return_value = {"ok": True, "result": updates}
            handle_mock.side_effect = [BotDeliveryError("Forbidden: bot was blocked by the user"), None]
            worker = TelegramPollingWorker(command, config.pk, threading.Event(), options)

            self.assertEqual(worker.poll_once(), 2)

        self.assertEqual(handle_mock.call_count, 2)
        self.assertEqual(worker.offset, 102)
        self.assertTrue(any("delivery failure" in message for message in logs))


class IntegrationCheckCommandTests(TestCase):
    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_incomplete_configuration_reports_errors(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        store = Store.objects.create(
            name="Broken store",
            english_name="Broken store",
            card_number="",
            card_owner="",
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Broken bot",
            bot_token="",
            admin_user_id="",
            force_telegram_channel_join=True,
            is_active=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Broken panel",
            url="",
            username="",
            password="",
            is_active=True,
        )
        Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command("check_integrations", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("[ERROR]", output)
        self.assertIn("Card number is missing", output)
        self.assertIn("Bot token is missing", output)
        self.assertIn("Admin user IDs are missing", output)
        self.assertIn("channel id/username is missing", output)
        self.assertIn("Panel URL is missing or invalid", output)
        self.assertIn("SMSFORWARDER_WEBHOOK_TOKEN is missing", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="sms-secret", TELEGRAM_BOT_USERNAME="azadnet_bot")
    @patch("store.bots.requests.post", return_value=DummyBotResponse({"ok": True, "result": {"username": "azadnet_bot"}}))
    def test_complete_configuration_passes(self, _post_mock):
        from io import StringIO

        from django.core.management import call_command

        store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
        )
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug="check-integrations-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="123456:abcdefghijklmnopqrstuvwxyz",
            admin_user_id="999",
            force_telegram_channel_join=True,
            telegram_required_channel_username="azadnet_channel",
            telegram_required_channel_invite_link="https://t.me/azadnet_channel",
            is_active=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        stdout = StringIO()

        with patch("store.xui_api.login_to_panel", return_value=object()):
            call_command(
                "check_integrations",
                "--live-bot",
                "--live-xui",
                "--send-telegram-test-message",
                "--telegram-chat-id",
                "999",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("[OK]", output)
        self.assertIn("Bot getMe succeeded", output)
        self.assertIn("Telegram test message sent", output)
        self.assertIn("X-UI login succeeded", output)
        self.assertIn("ERROR=0", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="sms-secret", TELEGRAM_BOT_USERNAME="azadnet_bot")
    def test_free_trial_configuration_errors_are_reported(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            free_trial_enabled=True,
            free_trial_traffic_gb=Decimal("0.000"),
            free_trial_duration_hours=0,
        )
        trial_panel = Panel.objects.create(
            store=store,
            name="Inactive trial panel",
            url="https://trial-panel.example.com",
            username="admin",
            password="secret",
            is_active=False,
        )
        other_panel = Panel.objects.create(
            store=store,
            name="Other panel",
            url="https://other-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        trial_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=9,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            current_users=1,
            max_clients=1,
        )
        store.free_trial_panel = trial_panel
        store.free_trial_inbound = trial_inbound
        store.save(
            update_fields=[
                "free_trial_panel",
                "free_trial_inbound",
                "updated_at",
            ]
        )
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug="free-trial-check-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="123456:abcdefghijklmnopqrstuvwxyz",
            admin_user_id="999",
            is_active=True,
        )
        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command("check_integrations", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Free Trial panel is inactive", output)
        self.assertIn("Free Trial inbound does not belong to the selected panel", output)
        self.assertIn("Free Trial traffic must be positive", output)
        self.assertIn("Free Trial duration must be positive", output)


class XUIPanelProxyTests(TestCase):
    proxy_url = "http://panel-proxy.example:8080"
    expected_proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    @override_settings(XUI_PANEL_PROXY_URL=proxy_url)
    def test_xui_service_uses_configured_panel_proxy(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, self.expected_proxies)

    @override_settings(XUI_PANEL_PROXY_URL="http://env-proxy.example:8080")
    def test_panel_proxy_overrides_environment_panel_proxy(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
            proxy_url=self.proxy_url,
        )

        service = XUIService(panel)

        self.assertEqual(service.session.proxies, self.expected_proxies)

    @override_settings(XUI_PANEL_PROXY_URL="")
    def test_xui_service_ignores_environment_proxies_by_default(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, {})

    @patch("store.xui_api.requests.Session")
    def test_xui_login_retries_transient_connection_errors(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.side_effect = [
            requests.ConnectionError("temporary disconnect"),
            DummyXUIResponse(),
        ]
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        XUIService(panel).login()

        self.assertEqual(session.post.call_count, 2)


class ClientNamingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Very Long 1 GB Premium Plan With Extra Metadata",
            slug="long-premium-plan",
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
        self.uuid_counter = 0

    def xui_result(self, **kwargs):
        self.uuid_counter += 1
        uuid_value = f"00000000-0000-4000-8000-{self.uuid_counter:012d}"
        email = f"{kwargs['email_prefix']}_{self.uuid_counter:08d}"
        return {
            "uuid": uuid_value,
            "email": email,
            "sub_id": f"sub-{self.uuid_counter}",
            "sub_link": f"https://example.com/sub/{self.uuid_counter}",
            "direct_link": f"vless://example#{email}",
            "raw": {"id": uuid_value, "email": email},
        }

    def create_order(self, *, customer=None, sender_card_name="Alice Buyer", bank_tracking_code="TRK1"):
        return create_manual_payment_order(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name=sender_card_name,
            sender_card_last4="1234",
            payment_time=time(14, 35),
            bank_tracking_code=bank_tracking_code,
            metadata={"source": "test"},
        )

    @patch("store.order_services.create_inactive_client_details")
    def test_order_name_uses_purchase_name_and_excludes_plan_noise(self, xui_mock):
        xui_mock.side_effect = self.xui_result

        result = self.create_order(sender_card_name="Alice Buyer")

        self.assertTrue(result.success)
        prefix = xui_mock.call_args.kwargs["email_prefix"]
        self.assertRegex(prefix, r"^alice_buyer_[0-9a-f]{8}$")
        self.assertEqual(result.order.username, prefix)
        self.assertNotIn("gb", prefix)
        self.assertNotIn("premium", prefix)
        self.assertLessEqual(len(result.vpn_client.xui_email), 40)

    @patch("store.order_services.create_inactive_client_details")
    def test_order_name_falls_back_to_customer_display_name(self, xui_mock):
        xui_mock.side_effect = self.xui_result
        customer = Customer.objects.create(display_name="Sajad Customer", username="sajad")

        result = self.create_order(
            customer=customer,
            sender_card_name="رسید تصویری",
        )

        self.assertTrue(result.success)
        prefix = xui_mock.call_args.kwargs["email_prefix"]
        self.assertRegex(prefix, r"^sajad_customer_[0-9a-f]{8}$")

    def test_client_name_uses_telegram_username_when_customer_name_missing(self):
        from .naming import build_client_display_name

        customer = SimpleNamespace(pk=7, public_id="", display_name="", username="telegram_user", phone_number="")

        self.assertEqual(
            build_client_display_name(customer, short_id="12345678"),
            "telegram_user_12345678",
        )

    def test_client_name_falls_back_to_phone_number(self):
        from .naming import build_client_display_name

        customer = SimpleNamespace(pk=7, public_id="", display_name="", username="", phone_number="+98 912-123-4567")

        self.assertEqual(
            build_client_display_name(customer, short_id="12345678"),
            "98_912_123_4567_12345678",
        )

    def test_client_name_falls_back_to_customer_id(self):
        from .naming import build_client_display_name

        customer = SimpleNamespace(pk=55, public_id="", display_name="", username="", phone_number="")

        self.assertEqual(
            build_client_display_name(customer, short_id="12345678"),
            "customer_55_12345678",
        )

    def test_long_and_dangerous_names_are_shortened_and_sanitized(self):
        from .naming import build_client_display_name

        full_uuid = "11111111-1111-4111-8111-111111111111"
        name = build_client_display_name(
            preferred_name=f"Very Long Buyer Name <script>alert(1)</script> {full_uuid}",
            short_id="abcdef12",
        )

        self.assertLessEqual(len(name), 31)
        self.assertTrue(name.endswith("_abcdef12"))
        self.assertNotIn("<", name)
        self.assertNotIn(">", name)
        self.assertNotIn(full_uuid, name)

    @patch("store.order_services.create_inactive_client_details")
    def test_two_orders_for_same_customer_get_different_short_ids(self, xui_mock):
        xui_mock.side_effect = self.xui_result
        customer = Customer.objects.create(display_name="Alice")

        with patch(
            "store.order_services.generate_order_tracking",
            side_effect=[
                "abc12345000040008000000000000000",
                "def67890000040008000000000000000",
            ],
        ):
            first = self.create_order(customer=customer, sender_card_name="Alice Buyer", bank_tracking_code="TRK1")
            second = self.create_order(customer=customer, sender_card_name="Alice Buyer", bank_tracking_code="TRK2")

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        prefixes = [call.kwargs["email_prefix"] for call in xui_mock.call_args_list]
        self.assertEqual(len(prefixes), 2)
        self.assertNotEqual(prefixes[0], prefixes[1])

    def test_persian_name_is_preserved(self):
        from .naming import build_client_display_name, clean_client_name

        self.assertEqual(clean_client_name("سجاد رضایی"), "سجاد_رضایی")
        self.assertEqual(
            build_client_display_name(preferred_name="سجاد رضایی", short_id="12345678"),
            "سجاد_رضایی_12345678",
        )

    def test_xui_final_email_is_limited_and_uses_short_uuid_suffix(self):
        from .naming import build_xui_client_email

        full_uuid = "11111111-1111-4111-8111-111111111111"
        email = build_xui_client_email("sajad_abcdef12", full_uuid)

        self.assertEqual(email, "sajad_abcdef12_11111111")
        self.assertLessEqual(len(email), 40)
        self.assertNotIn(full_uuid, email)


class ConfigLookupServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel A",
            url="https://panel-a.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.other_panel = Panel.objects.create(
            store=self.store,
            name="Panel B",
            url="https://panel-b.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def vmess_link(self, client_id):
        payload = {
            "v": "2",
            "ps": "demo",
            "add": "example.com",
            "port": "443",
            "id": client_id,
            "aid": "0",
            "net": "tcp",
            "type": "none",
            "tls": "tls",
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"vmess://{encoded}"

    def lookup_result(self, panel=None, inbound=None, *, total_gb=30, used_gb=12.5):
        total = int(total_gb * (1024 ** 3)) if total_gb else 0
        used = int(used_gb * (1024 ** 3))
        return {
            "panel": panel or self.panel,
            "inbound": inbound or self.inbound,
            "protocol": "vless",
            "client": {"id": "11111111-1111-4111-8111-111111111111", "email": "demo_user", "remark": "Demo"},
            "client_stats": {"email": "demo_user"},
            "total_traffic_bytes": total,
            "used_traffic_bytes": used,
            "used_upload_bytes": used,
            "used_download_bytes": 0,
            "remaining_traffic_bytes": max(total - used, 0) if total else 0,
            "expiry_at": timezone.now() + timedelta(days=12),
            "last_online_at": timezone.now(),
            "is_enabled": True,
        }

    def xui_inbound_payload(self, *, clients, client_stats=None, protocol="vless", stream_settings=None):
        return {
            "id": self.inbound.inbound_id,
            "remark": self.inbound.remark,
            "protocol": protocol,
            "port": self.inbound.port,
            "settings": json.dumps({"clients": clients}),
            "clientStats": client_stats if client_stats is not None else [],
            "streamSettings": json.dumps(stream_settings or {"network": "tcp", "security": "none"}),
        }

    def lookup_xui_payload(self, identifier, payload):
        from .xui_api import XUIService

        service = XUIService(self.panel)
        service.get_inbound = Mock(return_value=payload)
        service.get_client_traffic = Mock(side_effect=Exception("traffic endpoint unavailable"))
        return service.find_client_by_identifier(identifier)

    def test_extracts_vless_identifier(self):
        from .config_lookup import extract_client_identifier_from_config

        client_id = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            extract_client_identifier_from_config(f"vless://{client_id}@example.com:443?type=tcp#demo"),
            client_id,
        )

    def test_extracts_vmess_base64_identifier(self):
        from .config_lookup import extract_client_identifier_from_config

        client_id = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(extract_client_identifier_from_config(self.vmess_link(client_id)), client_id)

    def test_extracts_trojan_identifier(self):
        from .config_lookup import extract_client_identifier_from_config

        self.assertEqual(
            extract_client_identifier_from_config("trojan://secret-password@example.com:443?security=tls#demo"),
            "secret-password",
        )

    def test_extracts_raw_uuid_identifier(self):
        from .config_lookup import extract_client_identifier_from_config

        client_id = "33333333-3333-4333-8333-333333333333"
        self.assertEqual(extract_client_identifier_from_config(client_id), client_id)

    def test_config_link_fingerprint_ignores_fragment_and_empty_host(self):
        from .config_lookup import config_link_fingerprint

        client_id = "33333333-3333-4333-8333-333333333333"
        first = f"vless://{client_id}@example.com:443?type=ws&encryption=none&path=%2F&host=&security=none#old-name"
        second = f"vless://{client_id}@example.com:443?security=none&path=/&encryption=none&type=ws#new-name"

        self.assertEqual(config_link_fingerprint(first), config_link_fingerprint(second))

    def test_rejects_invalid_link_text(self):
        from .config_lookup import InvalidConfigLink, extract_client_identifier_from_config

        with self.assertRaises(InvalidConfigLink):
            extract_client_identifier_from_config("این یک لینک کانفیگ نیست")

    @patch("store.config_lookup.find_client_by_identifier")
    def test_lookup_finds_client_on_first_panel(self, finder_mock):
        from .config_lookup import lookup_client_across_panels

        finder_mock.return_value = self.lookup_result()

        result = lookup_client_across_panels("11111111-1111-4111-8111-111111111111", store=self.store)

        self.assertTrue(result["found"])
        self.assertEqual(result["panel"], self.panel)
        self.assertEqual(finder_mock.call_count, 1)

    @patch("store.config_lookup.find_client_by_identifier")
    def test_lookup_finds_client_on_second_panel_after_first_fails(self, finder_mock):
        from .config_lookup import lookup_client_across_panels
        from .xui_api import XUIError

        other_inbound = Inbound.objects.create(
            panel=self.other_panel,
            inbound_id=2,
            remark="Backup inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.2",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        finder_mock.side_effect = [
            XUIError("panel unavailable"),
            self.lookup_result(panel=self.other_panel, inbound=other_inbound),
        ]

        result = lookup_client_across_panels("11111111-1111-4111-8111-111111111111", store=self.store)

        self.assertTrue(result["found"])
        self.assertEqual(result["panel"], self.other_panel)
        self.assertEqual(len(result["panel_errors"]), 1)

    @patch("store.config_lookup.find_client_by_identifier", return_value=None)
    def test_lookup_returns_not_found_when_no_panel_has_client(self, _finder_mock):
        from .config_lookup import CONFIG_NOT_FOUND_MESSAGE, check_config_usage

        result = check_config_usage("11111111-1111-4111-8111-111111111111", store=self.store)

        self.assertFalse(result["found"])
        self.assertEqual(result["message"], CONFIG_NOT_FOUND_MESSAGE)

    @patch("store.config_lookup.find_client_by_identifier", return_value=None)
    def test_lookup_does_not_use_local_vpnclient_without_live_panel_match(self, finder_mock):
        from .config_lookup import check_config_usage

        client_id = "11111111-1111-4111-8111-111111111111"
        VPNClient.objects.create(
            store=self.store,
            inbound=self.inbound,
            username="local-only",
            xui_email="local-only",
            uuid=client_id,
            traffic_limit_bytes=30 * (1024 ** 3),
            used_traffic_bytes=12 * (1024 ** 3),
            status=VPNClient.Status.ACTIVE,
        )

        result = check_config_usage(client_id, store=self.store)

        self.assertFalse(result["found"])
        finder_mock.assert_called()

    def test_xui_lookup_uses_client_stats_up_down_total(self):
        client_id = "11111111-1111-4111-8111-111111111111"
        total = 30 * (1024 ** 3)
        upload = 5 * (1024 ** 3)
        download = 7 * (1024 ** 3)

        result = self.lookup_xui_payload(
            client_id,
            self.xui_inbound_payload(
                clients=[{"id": client_id, "email": "alice", "totalGB": total, "expiryTime": 0, "enable": True}],
                client_stats=[{"email": "alice", "up": upload, "down": download, "total": total}],
            ),
        )

        self.assertEqual(result["used_upload_bytes"], upload)
        self.assertEqual(result["used_download_bytes"], download)
        self.assertEqual(result["used_traffic_bytes"], upload + download)
        self.assertEqual(result["remaining_traffic_bytes"], total - upload - download)
        self.assertTrue(result["stats_available"])

    def test_xui_lookup_without_client_stats_keeps_usage_unknown(self):
        client_id = "11111111-1111-4111-8111-111111111111"

        result = self.lookup_xui_payload(
            client_id,
            self.xui_inbound_payload(
                clients=[{"id": client_id, "email": "alice", "totalGB": 30 * (1024 ** 3), "enable": True}],
                client_stats=None,
            ),
        )

        self.assertIsNone(result["used_traffic_bytes"])
        self.assertIsNone(result["remaining_traffic_bytes"])
        self.assertFalse(result["stats_available"])

    def test_xui_lookup_falls_back_to_get_client_traffic_when_client_stats_missing(self):
        from .xui_api import XUIService

        client_id = "11111111-1111-4111-8111-111111111111"
        total = 10 * (1024 ** 3)
        upload = 1 * (1024 ** 3)
        download = 8 * (1024 ** 3)
        service = XUIService(self.panel)
        service.get_inbound = Mock(
            return_value=self.xui_inbound_payload(
                clients=[{"id": client_id, "email": "alice", "totalGB": total, "enable": True}],
                client_stats=[],
            )
        )
        service.get_client_traffic = Mock(
            return_value={
                "email": "alice",
                "uuid": client_id,
                "up": upload,
                "down": download,
                "total": total,
                "enable": True,
            }
        )

        result = service.find_client_by_identifier(client_id)

        self.assertEqual(result["used_traffic_bytes"], upload + download)
        self.assertEqual(result["remaining_traffic_bytes"], total - upload - download)
        self.assertTrue(result["stats_available"])
        service.get_client_traffic.assert_called_once_with("alice", use_cache=False)

    def test_xui_lookup_matches_email_id_and_trojan_password(self):
        client_id = "11111111-1111-4111-8111-111111111111"
        email_payload = self.xui_inbound_payload(
            clients=[{"id": client_id, "email": "alice@example", "totalGB": 0, "enable": True}],
            client_stats=[{"email": "alice@example", "up": 1, "down": 2, "total": 0}],
        )
        self.assertEqual(self.lookup_xui_payload("alice@example", email_payload)["matched_field"], "email")

        id_payload = self.xui_inbound_payload(
            clients=[{"id": client_id, "email": "bob", "totalGB": 0, "enable": True}],
            client_stats=[{"id": client_id, "up": 3, "down": 4, "total": 0}],
        )
        self.assertEqual(self.lookup_xui_payload(client_id, id_payload)["matched_field"], "id")

        password_payload = self.xui_inbound_payload(
            clients=[{"password": "trojan-secret", "email": "trojan-user", "totalGB": 0, "enable": True}],
            client_stats=[{"email": "trojan-user", "up": 5, "down": 6, "total": 0}],
            protocol="trojan",
        )
        result = self.lookup_xui_payload("trojan-secret", password_payload)
        self.assertEqual(result["matched_field"], "password")
        self.assertEqual(result["protocol"], "trojan")

    def test_build_config_link_for_identifier_uses_live_inbound_settings(self):
        from .xui_api import XUIService

        client_id = "11111111-1111-4111-8111-111111111111"
        service = XUIService(self.panel)
        service.get_inbound = Mock(
            return_value=self.xui_inbound_payload(
                clients=[{"id": client_id, "email": "Alice Config", "flow": "xtls-rprx-vision"}],
                stream_settings={
                    "network": "ws",
                    "security": "tls",
                    "tlsSettings": {"serverName": "new.example.com", "fingerprint": "chrome"},
                    "wsSettings": {"path": "/new-path", "headers": {"Host": "new.example.com"}},
                },
            )
        )

        result = service.build_config_link_for_identifier(self.inbound.inbound_id, client_id)

        self.assertTrue(result["config_link_updated"])
        self.assertIn("vless://", result["updated_config_link"])
        self.assertIn("type=ws", result["updated_config_link"])
        self.assertIn("host=new.example.com", result["updated_config_link"])
        self.assertIn("path=%2Fnew-path", result["updated_config_link"])

    def test_format_calculates_remaining_volume(self):
        from .config_lookup import format_client_usage_result

        result = self.lookup_result()
        result["found"] = True

        text = format_client_usage_result(result)

        self.assertIn("📊 وضعیت کانفیگ شما", text)
        self.assertIn("حجم کل: ۳۰ گیگ", text)
        self.assertIn("مصرف‌شده: ۱۲.۵ گیگ", text)
        self.assertIn("باقی‌مانده: ۱۷.۵ گیگ", text)
        self.assertIn("باقی‌مانده زمانی: ۱۲ روز", text)

    def test_format_displays_unknown_usage_when_stats_missing(self):
        from .config_lookup import format_client_usage_result

        result = self.lookup_result()
        result.update(
            {
                "found": True,
                "used_traffic_bytes": None,
                "used_upload_bytes": None,
                "used_download_bytes": None,
                "remaining_traffic_bytes": None,
                "stats_available": False,
            }
        )

        text = format_client_usage_result(result)

        self.assertIn("مصرف‌شده: نامشخص", text)
        self.assertIn("باقی‌مانده: نامشخص", text)
        self.assertIn("آمار مصرف از پنل در دسترس نبود.", text)

    def test_format_displays_unlimited_volume_and_time(self):
        from .config_lookup import format_client_usage_result

        result = self.lookup_result(total_gb=0, used_gb=3)
        result.update({"found": True, "expiry_at": None})

        text = format_client_usage_result(result)

        self.assertIn("حجم کل: نامحدود", text)
        self.assertIn("باقی‌مانده: نامحدود", text)
        self.assertIn("زمان انقضا: نامحدود", text)
        self.assertIn("باقی‌مانده زمانی: نامحدود", text)

    def test_format_marks_expired_time(self):
        from .config_lookup import format_client_usage_result

        result = self.lookup_result()
        result.update({"found": True, "expiry_at": timezone.now() - timedelta(days=1)})

        text = format_client_usage_result(result)

        self.assertIn("وضعیت زمانی: منقضی شده", text)


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


class AdminPlanBulkPricingTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="secret",
        )
        self.client.force_login(self.admin_user)
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.two_gb_plan = Plan.objects.create(
            store=self.store,
            name="2 GB",
            slug="bulk-price-2gb",
            volume_gb=Decimal("2.000"),
            duration_days=30,
            price=1,
            currency=Plan.Currency.TOMAN,
        )
        self.half_gb_plan = Plan.objects.create(
            store=self.store,
            name="0.5 GB",
            slug="bulk-price-half-gb",
            volume_gb=Decimal("0.500"),
            duration_days=30,
            price=1,
            currency=Plan.Currency.TOMAN,
        )

    def test_admin_can_apply_price_per_gb_to_all_plans(self):
        response = self.client.post(
            reverse("admin:store_plan_changelist"),
            {"price_per_gb": "100", "_apply_price_per_gb": "1"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "اعمال قیمت هر گیگ")
        self.two_gb_plan.refresh_from_db()
        self.half_gb_plan.refresh_from_db()
        self.store.refresh_from_db()
        self.assertEqual(self.two_gb_plan.price, 200)
        self.assertEqual(self.half_gb_plan.price, 50)
        self.assertEqual(self.store.custom_volume_price_per_gb, Decimal("100.000"))


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
            payment_time=time(14, 35),
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
    def test_duplicate_manual_order_returns_existing_order_before_panel_call(self, xui_mock):
        first = self.create_order(quantity=1)
        second = self.create_order(quantity=1)

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.duplicate_detected)
        self.assertEqual(first.order.pk, second.order.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(xui_mock.call_count, 1)
        second.order.refresh_from_db()
        self.assertTrue(second.order.metadata["duplicate_warning"]["detected"])
        self.assertEqual(second.order.metadata["duplicate_warning"]["attempt_count"], 1)

        from .bots import format_order_message

        admin_message = format_order_message(second.order)
        self.assertIn("هشدار درخواست تکراری", admin_message)
        self.assertEqual(second.order.metadata["payment_destination_card_number"], self.store.card_number)
        self.assertEqual(second.order.metadata["payment_destination_card_owner"], self.store.card_owner)

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

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_custom_volume_order_uses_store_price_per_gb_for_30_days(self, xui_mock):
        self.store.custom_volume_price_per_gb = Decimal("125000")
        self.store.save(update_fields=["custom_volume_price_per_gb", "updated_at"])

        result = create_manual_payment_order(
            store=self.store,
            customer=None,
            plan=None,
            custom_volume_gb="7",
            inbound=self.inbound,
            sender_card_name="Alice Buyer",
            sender_card_last4="1234",
            payment_time=time(14, 35),
            bank_tracking_code="TRK123",
            metadata={"source": "test"},
        )

        self.assertTrue(result.success)
        order = result.order
        self.assertTrue(order.plan.is_custom_volume)
        self.assertFalse(order.plan.is_public)
        self.assertEqual(order.plan.volume_gb, Decimal("7.000"))
        self.assertEqual(order.plan.duration_days, 30)
        self.assertEqual(order.plan.price, 875000)
        self.assertEqual(order.original_amount, 875000)
        self.assertEqual(order.amount, 875000)
        self.assertTrue(order.metadata["custom_volume"])
        self.assertEqual(order.metadata["custom_volume_gb"], "7.000")
        xui_mock.assert_called_once()
        self.assertEqual(xui_mock.call_args.kwargs["total_gb"], Decimal("7.000"))
        self.assertEqual(xui_mock.call_args.kwargs["expire_days"], 30)


class SalesModePurchaseTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.operator_a = Operator.objects.create(store=self.store, name="همراه اول", slug="mci", sort_order=1)
        self.operator_b = Operator.objects.create(store=self.store, name="ایرانسل", slug="irancell", sort_order=2)
        self.plan_a = Plan.objects.create(
            store=self.store,
            name="MCI 1 GB",
            slug="mci-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.plan_b = Plan.objects.create(
            store=self.store,
            name="Irancell 2 GB",
            slug="irancell-2gb",
            volume_gb=Decimal("2.000"),
            duration_days=30,
            price=180000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.plan_a.operators.add(self.operator_a)
        self.plan_b.operators.add(self.operator_b)
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

    def enable_operator_based_sales(self):
        self.store.sales_mode = Store.SalesMode.OPERATOR_BASED
        self.store.save(update_fields=["sales_mode", "updated_at"])

    def order_kwargs(self, *, plan=None, operator=None):
        return {
            "store": self.store,
            "customer": None,
            "plan": plan or self.plan_a,
            "operator": operator,
            "inbound": self.inbound,
            "sender_card_name": "Alice Buyer",
            "sender_card_last4": "1234",
            "payment_time": time(14, 35),
            "bank_tracking_code": "TRK123",
            "metadata": {"source": "test"},
        }

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_tunnel_sales_mode_allows_order_without_operator(self, xui_mock):
        result = create_manual_payment_order(**self.order_kwargs(operator=None))

        self.assertTrue(result.success)
        self.assertEqual(self.store.sales_mode, Store.SalesMode.TUNNEL)
        self.assertIsNone(result.order.operator)
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_buyer_[0-9a-f]{8}$")

    def test_operator_based_filters_plans_by_selected_operator(self):
        self.enable_operator_based_sales()

        operator_a_plans = list(get_store_plans(self.store, public_only=True, operator=self.operator_a))
        operator_b_plans = list(get_store_plans(self.store, public_only=True, operator=self.operator_b))

        self.assertEqual(operator_a_plans, [self.plan_a])
        self.assertEqual(operator_b_plans, [self.plan_b])

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_operator_based_order_saves_selected_operator(self, xui_mock):
        self.enable_operator_based_sales()

        result = create_manual_payment_order(**self.order_kwargs(operator=self.operator_a))

        self.assertTrue(result.success)
        result.order.refresh_from_db()
        self.assertEqual(result.order.operator, self.operator_a)
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_buyer_[0-9a-f]{8}$")

    @patch("store.order_services.create_inactive_client_details")
    def test_operator_based_rejects_order_without_operator(self, xui_mock):
        self.enable_operator_based_sales()

        result = create_manual_payment_order(**self.order_kwargs(operator=None))

        self.assertFalse(result.success)
        self.assertIn("اپراتور", result.message)
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.order_services.create_inactive_client_details")
    def test_web_checkout_requires_operator_before_creating_order(self, xui_mock):
        self.enable_operator_based_sales()

        response = self.client.post(
            reverse("home"),
            data={
                "plan_id": str(self.plan_a.pk),
                "sender_card_name": "Alice Buyer",
                "payment_time": "14:35",
                "quantity": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "برای خرید ابتدا اپراتور اینترنت خود را انتخاب کن.")
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    def test_web_operator_selection_shows_only_operator_plans(self):
        self.enable_operator_based_sales()

        response = self.client.get(reverse("home"))

        self.assertContains(response, self.operator_a.name)
        self.assertContains(response, self.operator_b.name)
        self.assertContains(response, "اول اپراتور اینترنتت رو انتخاب کن.")
        self.assertNotContains(response, self.plan_a.name)
        self.assertNotContains(response, self.plan_b.name)

        selected_response = self.client.get(reverse("home"), {"operator": str(self.operator_a.pk)})

        self.assertContains(selected_response, self.operator_a.name)
        self.assertContains(selected_response, self.plan_a.name)
        self.assertNotContains(selected_response, self.plan_b.name)


class InboundPanelRoutingTests(TestCase):
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
            slug="routing-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel A",
            url="https://panel-a.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.other_panel = Panel.objects.create(
            store=self.store,
            name="Panel B",
            url="https://panel-b.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = self.create_inbound(self.panel, inbound_id=10, current_users=5)
        self.other_inbound = self.create_inbound(self.other_panel, inbound_id=10, current_users=0)

    def create_inbound(self, panel, *, inbound_id, current_users=0, is_active=True):
        return Inbound.objects.create(
            panel=panel,
            inbound_id=inbound_id,
            remark=f"inbound-{panel.name}-{inbound_id}",
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=is_active,
            current_users=current_users,
        )

    def order_kwargs(self, *, inbound=None):
        return {
            "store": self.store,
            "customer": None,
            "plan": self.plan,
            "inbound": inbound,
            "sender_card_name": "Alice Buyer",
            "sender_card_last4": "1234",
            "payment_time": time(14, 35),
            "bank_tracking_code": "TRK-PANEL",
            "metadata": {"source": "test"},
        }

    def test_inbound_is_created_with_panel(self):
        self.assertEqual(self.inbound.panel, self.panel)
        self.assertIn(self.inbound, self.panel.inbounds.all())

    def test_inbound_id_is_unique_per_panel(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_inbound(self.panel, inbound_id=self.inbound.inbound_id)

    def test_same_inbound_id_is_allowed_on_different_panels(self):
        self.assertEqual(self.inbound.inbound_id, self.other_inbound.inbound_id)
        self.assertNotEqual(self.inbound.panel, self.other_inbound.panel)

    @patch("store.order_services.create_inactive_client_details")
    def test_order_creation_rejects_inbound_without_panel(self, xui_mock):
        orphan_inbound = Inbound(
            inbound_id=99,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

        result = create_manual_payment_order(**self.order_kwargs(inbound=orphan_inbound))

        self.assertFalse(result.success)
        self.assertIn("پنل", result.message)
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_order_creation_selects_inbound_own_panel_for_client_prepare(self, xui_mock):
        result = create_manual_payment_order(**self.order_kwargs(inbound=None))

        self.assertTrue(result.success)
        self.assertEqual(result.order.inbound, self.other_inbound)
        xui_mock.assert_called_once()
        self.assertEqual(xui_mock.call_args.kwargs["inbound"], self.other_inbound)
        self.assertEqual(xui_mock.call_args.kwargs["panel"], self.other_panel)

    @patch("store.xui_api.XUIService")
    def test_activation_uses_vpn_client_inbound_panel(self, service_cls):
        service_cls.return_value.update_client_enabled.return_value = True
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=self.other_inbound,
            uuid="22222222-2222-4222-8222-222222222222",
            username="panel_b_user",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.other_inbound,
            username=order.username,
            xui_email=order.username,
            uuid=order.uuid,
            status=VPNClient.Status.INACTIVE,
        )

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        service_cls.assert_called_once_with(self.other_panel)
        service_cls.return_value.update_client_enabled.assert_called_once_with(vpn_client)

    @patch("store.xui_api.XUIService")
    def test_activation_fails_safely_when_vpn_client_has_no_inbound_panel(self, service_cls):
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=None,
            uuid="33333333-3333-4333-8333-333333333333",
            username="orphan_user",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )
        VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=None,
            username=order.username,
            xui_email=order.username,
            uuid=order.uuid,
            status=VPNClient.Status.INACTIVE,
        )

        result = activate_order(order, notify=False)

        self.assertFalse(result.success)
        self.assertIn("اینباند و پنل", result.message)
        service_cls.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_tunnel_mode_still_creates_order_with_selected_inbound_panel(self, xui_mock):
        self.assertEqual(self.store.sales_mode, Store.SalesMode.TUNNEL)

        result = create_manual_payment_order(**self.order_kwargs(inbound=self.inbound))

        self.assertTrue(result.success)
        self.assertIsNone(result.order.operator)
        self.assertEqual(result.order.inbound, self.inbound)
        self.assertEqual(xui_mock.call_args.kwargs["panel"], self.panel)


class FreeTrialServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Trial Panel",
            url="https://trial-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=7,
            remark="Trial inbound",
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.customer = Customer.objects.create(display_name="Alice")
        self.enable_free_trial()

    def enable_free_trial(self):
        self.store.free_trial_enabled = True
        self.store.free_trial_panel = self.panel
        self.store.free_trial_inbound = self.inbound
        self.store.free_trial_traffic_gb = Decimal("1.000")
        self.store.free_trial_duration_hours = 24
        self.store.free_trial_cooldown_days = 30
        self.store.save(
            update_fields=[
                "free_trial_enabled",
                "free_trial_panel",
                "free_trial_inbound",
                "free_trial_traffic_gb",
                "free_trial_duration_hours",
                "free_trial_cooldown_days",
                "updated_at",
            ]
        )

    def test_store_validation_requires_trial_inbound_to_match_panel(self):
        other_panel = Panel.objects.create(
            store=self.store,
            name="Other Panel",
            url="https://other-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=8,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.store.free_trial_inbound = other_inbound

        with self.assertRaises(ValidationError) as ctx:
            self.store.full_clean()

        self.assertIn("free_trial_inbound", ctx.exception.message_dict)

    @patch("store.free_trial_services.create_trial_client_details", return_value=fake_client_result())
    def test_create_free_trial_persists_request_and_vpn_client(self, xui_mock):
        from .free_trial_services import create_free_trial_for_customer

        result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertTrue(result.success)
        trial_request = FreeTrialRequest.objects.get()
        self.assertEqual(trial_request.status, FreeTrialRequest.Status.DELIVERED)
        self.assertEqual(trial_request.customer, self.customer)
        self.assertEqual(trial_request.telegram_user_id, "42")
        self.assertEqual(trial_request.panel, self.panel)
        self.assertEqual(trial_request.inbound, self.inbound)
        self.assertEqual(trial_request.config_link, "vless://example")
        self.assertIsNotNone(trial_request.delivered_at)
        vpn_client = trial_request.vpn_client
        self.assertEqual(vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertEqual(vpn_client.store, self.store)
        self.assertEqual(vpn_client.inbound, self.inbound)
        self.assertEqual(vpn_client.traffic_limit_bytes, 1024 ** 3)
        self.inbound.refresh_from_db()
        self.assertEqual(self.inbound.current_users, 1)
        xui_mock.assert_called_once()
        self.assertEqual(xui_mock.call_args.kwargs["email_prefix"], "trial_alice_42")
        self.assertEqual(xui_mock.call_args.kwargs["panel"], self.panel)
        self.assertEqual(xui_mock.call_args.kwargs["inbound"], self.inbound)
        self.assertEqual(xui_mock.call_args.kwargs["duration_hours"], 24)

    @patch("store.free_trial_services.create_trial_client_details")
    def test_fast_duplicate_click_is_blocked_by_lock_before_xui_call(self, xui_mock):
        from .free_trial_services import create_free_trial_for_customer

        cache.add("free-trial:create:telegram:42", "1", timeout=120)

        result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        self.assertIn("در حال ساخت تست رایگان قبلی", result.message)
        self.assertFalse(FreeTrialRequest.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.free_trial_services.create_trial_client_details")
    def test_cooldown_uses_telegram_id_even_without_customer(self, xui_mock):
        from .free_trial_services import can_customer_request_free_trial, create_free_trial_for_customer

        FreeTrialRequest.objects.create(
            customer=None,
            telegram_user_id="42",
            panel=self.panel,
            inbound=self.inbound,
            status=FreeTrialRequest.Status.DELIVERED,
            traffic_gb=Decimal("1.000"),
            duration_hours=24,
            config_link="vless://old",
            delivered_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=24),
        )

        self.assertFalse(can_customer_request_free_trial(None, telegram_user_id="42", store=self.store))
        result = create_free_trial_for_customer(None, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        self.assertIn("قبلاً تست رایگان", result.message)
        xui_mock.assert_not_called()

    @patch("store.free_trial_services.create_trial_client_details")
    def test_delivered_trial_blocks_customer_until_cooldown(self, xui_mock):
        from .free_trial_services import create_free_trial_for_customer

        FreeTrialRequest.objects.create(
            customer=self.customer,
            telegram_user_id="42",
            panel=self.panel,
            inbound=self.inbound,
            status=FreeTrialRequest.Status.DELIVERED,
            traffic_gb=Decimal("1.000"),
            duration_hours=24,
            config_link="vless://old",
            delivered_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=24),
        )

        result = create_free_trial_for_customer(self.customer, telegram_user_id="99", store=self.store)

        self.assertFalse(result.success)
        self.assertIn("قبلاً تست رایگان", result.message)
        xui_mock.assert_not_called()

    @patch("store.free_trial_services.create_trial_client_details", return_value=None)
    def test_failed_panel_creation_records_failed_request_without_cooldown(self, _xui_mock):
        from .free_trial_services import can_customer_request_free_trial, create_free_trial_for_customer

        result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        trial_request = FreeTrialRequest.objects.get()
        self.assertEqual(trial_request.status, FreeTrialRequest.Status.FAILED)
        self.assertIn("ساخت تست رایگان", trial_request.error_message)
        self.assertTrue(can_customer_request_free_trial(self.customer, telegram_user_id="42", store=self.store))

    @patch("store.free_trial_services.create_trial_client_details")
    def test_failed_trial_can_be_retried_successfully(self, xui_mock):
        from .free_trial_services import create_free_trial_for_customer

        xui_mock.side_effect = [None, fake_client_result("56565656-5656-4565-8565-565656565656")]

        first = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)
        second = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(first.success)
        self.assertTrue(second.success)
        self.assertEqual(FreeTrialRequest.objects.filter(status=FreeTrialRequest.Status.FAILED).count(), 1)
        self.assertEqual(FreeTrialRequest.objects.filter(status=FreeTrialRequest.Status.DELIVERED).count(), 1)

    @patch("store.free_trial_services.create_trial_client_details")
    def test_inactive_panel_or_inbound_stops_trial_before_xui(self, xui_mock):
        from .free_trial_services import create_free_trial_for_customer

        self.panel.is_active = False
        self.panel.save(update_fields=["is_active", "updated_at"])

        result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        self.assertIn("غیرفعال", result.message)
        xui_mock.assert_not_called()

        self.panel.is_active = True
        self.panel.save(update_fields=["is_active", "updated_at"])
        self.inbound.is_active = False
        self.inbound.save(update_fields=["is_active", "updated_at"])

        result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        self.assertIn("غیرفعال", result.message)
        xui_mock.assert_not_called()

    @patch("store.free_trial_services.delete_client", return_value=True)
    @patch("store.free_trial_services.VPNClient.objects.create")
    @patch("store.free_trial_services.create_trial_client_details")
    def test_panel_client_is_cleaned_up_when_local_persistence_fails(self, xui_mock, create_mock, delete_mock):
        from .free_trial_services import create_free_trial_for_customer

        xui_mock.return_value = {
            **fake_client_result("67676767-6767-4767-8767-676767676767"),
            "direct_link": "vless://secret-config@example.com:443?type=tcp#trial",
        }
        create_mock.side_effect = RuntimeError("db failed for vless://secret-config@example.com")

        with self.assertLogs("store.free_trial_services", level="WARNING") as logs:
            result = create_free_trial_for_customer(self.customer, telegram_user_id="42", store=self.store)

        self.assertFalse(result.success)
        trial_request = FreeTrialRequest.objects.get()
        self.assertEqual(trial_request.status, FreeTrialRequest.Status.FAILED)
        self.assertIn("<config-link-redacted>", trial_request.error_message)
        delete_mock.assert_called_once()
        cleanup_target = delete_mock.call_args.args[0]
        self.assertEqual(cleanup_target.uuid, "67676767-6767-4767-8767-676767676767")
        log_output = "\n".join(logs.output)
        self.assertIn("local persistence failed", log_output)
        self.assertIn("67676767-6767-4767-8767-676767676767", log_output)
        self.assertNotIn("vless://secret-config", log_output)
        self.assertIn("<config-link-redacted>", log_output)


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
    def test_web_checkout_saves_valid_receipt_image(self, xui_mock):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(reverse("home"), data=self.checkout_payload(receipt))

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_last4, "")
        self.assertTrue(order.payment_receipt_image.name.endswith(".png"))
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "image_only")
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_buyer_[0-9a-f]{8}$")

    @patch("store.order_services.create_inactive_client_details", return_value=None)
    def test_web_checkout_records_order_when_panel_is_unavailable(self, _xui):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(reverse("home"), data=self.checkout_payload(receipt))

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertTrue(order.metadata["panel_provisioning_deferred"])
        self.assertEqual(order.metadata["panel_provisioning_reason"], "panel_unavailable_on_checkout")
        self.assertFalse(order.vpn_clients.exists())

    @patch("store.order_services.create_inactive_client_details")
    def test_web_checkout_rejects_receipt_text_without_image(self, xui_mock):
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(receipt_text="مبلغ انتقال ۱,۰۰۰,۰۰۰ ریال با موفقیت انجام شد."),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عکس رسید را بارگذاری کن.")
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch(
        "store.views.sync_vpn_client_stats",
        return_value={
            "is_enabled": False,
            "is_expired": False,
            "total_traffic_bytes": 0,
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": 0,
            "panel_available": False,
        },
    )
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_tracking_link_recovers_order_when_customer_cookie_was_lost(self, _xui, _stats):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(receipt),
        )
        order = Order.objects.select_related("customer", "plan").get()
        original_customer_id = order.customer_id

        target = urlsplit(response["Location"])
        recovery_path = target.path + (f"?{target.query}" if target.query else "")
        recovery_client = Client()

        recovery_response = recovery_client.get(recovery_path)

        self.assertEqual(recovery_response.status_code, 200)
        self.assertContains(recovery_response, order.order_tracking_code)

        dashboard_response = recovery_client.get(reverse("dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, order.plan.name)

        order.refresh_from_db()
        self.assertEqual(order.customer_id, original_customer_id)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_old_tracking_link_without_cookie_does_not_recover_customer(self, _xui):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(receipt),
        )
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=1))

        target = urlsplit(response["Location"])
        recovery_path = target.path + (f"?{target.query}" if target.query else "")
        recovery_client = Client()

        recovery_response = recovery_client.get(recovery_path)

        self.assertEqual(recovery_response.status_code, 404)
        dashboard_response = recovery_client.get(reverse("dashboard"))
        self.assertEqual(dashboard_response.status_code, 302)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_ignores_receipt_text_when_image_is_uploaded(self, _xui):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")
        response = self.client.post(
            reverse("home"),
            data=self.checkout_payload(
                receipt,
                receipt_text="مبلغ انتقال ۹۰۰,۰۰۰ ریال با موفقیت انجام شد.",
            ),
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertNotIn("receipt_text", order.metadata)
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "image_only")
        from .bots import format_order_message

        admin_message = format_order_message(order)
        self.assertIn("بررسی رسید", admin_message)
        self.assertNotIn("۹۰۰,۰۰۰", admin_message)

    @patch("store.order_services.create_inactive_client_details")
    def test_web_checkout_requires_receipt_text_or_image_before_panel_call(self, xui_mock):
        response = self.client.post(reverse("home"), data=self.checkout_payload())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عکس رسید را بارگذاری کن.")
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_image_only_card_accepts_only_receipt_image(self, _xui):
        self.store.receipt_image_only_payment = True
        self.store.save(update_fields=["receipt_image_only_payment", "updated_at"])
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(
            reverse("home"),
            data={
                "plan_id": str(self.plan.pk),
                "quantity": "1",
                "payment_receipt_image": receipt,
                "payment_receipt_text": "مبلغ انتقال ۱,۰۰۰,۰۰۰ ریال",
            },
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_name, "رسید تصویری")
        self.assertEqual(order.metadata["payment_capture_mode"], "receipt_image_only")
        self.assertEqual(order.metadata["receipt_analysis"]["status"], "image_only")
        self.assertNotIn("receipt_text", order.metadata)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_creates_custom_volume_plan(self, xui_mock):
        self.store.custom_volume_price_per_gb = Decimal("120000")
        self.store.save(update_fields=["custom_volume_price_per_gb", "updated_at"])
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(
            reverse("home"),
            data={
                "custom_volume_selected": "1",
                "custom_volume_gb": "5",
                "quantity": "1",
                "payment_receipt_image": receipt,
            },
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.select_related("plan").get()
        self.assertTrue(order.plan.is_custom_volume)
        self.assertEqual(order.plan.volume_gb, Decimal("5.000"))
        self.assertEqual(order.plan.duration_days, 30)
        self.assertEqual(order.amount, 600000)
        xui_mock.assert_called_once()
        self.assertEqual(xui_mock.call_args.kwargs["total_gb"], Decimal("5.000"))
        self.assertEqual(xui_mock.call_args.kwargs["expire_days"], 30)

    @patch("store.order_services.create_inactive_client_details")
    def test_image_only_card_requires_receipt_image_before_panel_call(self, xui_mock):
        self.store.receipt_image_only_payment = True
        self.store.save(update_fields=["receipt_image_only_payment", "updated_at"])

        response = self.client.post(
            reverse("home"),
            data={
                "plan_id": str(self.plan.pk),
                "quantity": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "عکس رسید را بارگذاری کن.")
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


class AdminCardReceiptsReportTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="secret",
        )
        self.client.force_login(self.admin_user)
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1111222233334444",
            card_owner="Azad Net",
            bank_name="Bank One",
        )
        self.second_store = Store.objects.create(
            name="Second",
            english_name="Second",
            slug="second",
            card_number="5555666677778888",
            card_owner="Second Owner",
            bank_name="Bank Two",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="1 GB",
            slug="admin-report-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )

    def create_paid_order(
        self,
        *,
        store=None,
        amount=100000,
        status=Order.Status.PENDING_VERIFICATION,
        paid_at=None,
        metadata=None,
    ):
        store = store or self.store
        return Order.objects.create(
            store=store,
            plan=self.plan,
            amount=amount,
            original_amount=amount,
            currency=Plan.Currency.TOMAN,
            payment_method=Order.PaymentMethod.MANUAL_CARD,
            is_paid=True,
            payment_submitted_at=paid_at or timezone.now(),
            status=status,
            verification_status=Order.VerificationStatus.PENDING,
            sender_card_name="Alice Buyer",
            payment_time=time(14, 35),
            metadata=metadata
            or {
                "payment_destination_card_number": store.card_number,
                "payment_destination_card_owner": store.card_owner,
                "payment_destination_bank_name": store.bank_name,
            },
        )

    def test_card_receipts_report_groups_totals_and_excludes_rejected_by_default(self):
        self.create_paid_order(amount=100000)
        self.create_paid_order(amount=200000, status=Order.Status.COMPLETED)
        self.create_paid_order(amount=900000, status=Order.Status.REJECTED)
        self.create_paid_order(store=self.second_store, amount=50000)

        response = self.client.get(reverse("admin_card_receipts_report"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grand_total_irr"], 3500000)
        first_summary = next(
            item for item in response.context["card_summaries"] if item["card_number"] == self.store.card_number
        )
        self.assertEqual(first_summary["order_count"], 2)
        self.assertEqual(first_summary["total_irr"], 3000000)

        response = self.client.get(reverse("admin_card_receipts_report"), {"status": "all"})

        self.assertEqual(response.context["grand_total_irr"], 12500000)
        first_summary = next(
            item for item in response.context["card_summaries"] if item["card_number"] == self.store.card_number
        )
        self.assertEqual(first_summary["order_count"], 3)
        self.assertEqual(first_summary["rejected_count"], 1)

    def test_card_receipts_report_uses_order_card_snapshot_before_store_fallback(self):
        old_card = self.store.card_number
        self.create_paid_order(
            amount=100000,
            metadata={
                "payment_destination_card_number": old_card,
                "payment_destination_card_owner": "Old Owner",
                "payment_destination_bank_name": "Old Bank",
            },
        )
        self.store.card_number = "9999000011112222"
        self.store.save(update_fields=["card_number", "updated_at"])
        self.create_paid_order(amount=50000)

        response = self.client.get(reverse("admin_card_receipts_report"), {"card": old_card})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grand_total_irr"], 1000000)
        self.assertEqual(response.context["card_summaries"][0]["card_owner"], "Old Owner")
        self.assertEqual(len(response.context["order_rows"]), 1)


class AdminNotificationTests(TestCase):
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
            slug="admin-notify-1gb",
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
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram admin notifications",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )

    def checkout_payload(self, receipt=None):
        payload = {
            "plan_id": str(self.plan.pk),
            "sender_card_name": "Alice Buyer",
            "payment_time": "14:35",
            "quantity": "1",
        }
        if receipt is not None:
            payload["payment_receipt_image"] = receipt
        return payload

    def base_order(self, **kwargs):
        defaults = {
            "store": self.store,
            "plan": self.plan,
            "amount": self.plan.price,
            "original_amount": self.plan.price,
            "currency": Plan.Currency.TOMAN,
            "status": Order.Status.PENDING_PAYMENT,
            "verification_status": Order.VerificationStatus.PENDING,
            "sender_card_name": "Alice Buyer",
        }
        defaults.update(kwargs)
        return Order.objects.create(**defaults)

    def bot_post_side_effect(self, post_calls, *, fail_chat_ids=()):
        message_id = {"value": 200}

        def side_effect(url, json=None, data=None, files=None, **kwargs):
            post_calls.append({"url": url, "json": json, "data": data, "files": files, **kwargs})
            payload = json or data or {}
            chat_id = str(payload.get("chat_id") or "")
            if chat_id in {str(item) for item in fail_chat_ids} and (
                url.endswith("/sendMessage") or url.endswith("/sendPhoto")
            ):
                raise requests.RequestException("delivery failed")
            if url.endswith("/sendMessage") or url.endswith("/sendPhoto"):
                message_id["value"] += 1
                return DummyBotResponse({"ok": True, "result": {"message_id": message_id["value"]}})
            return DummyBotResponse()

        return side_effect

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_checkout_notifies_admin_after_commit_with_receipt_photo(self, _xui):
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("home"), data=self.checkout_payload(receipt))

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertIsNotNone(order.admin_notified_at)
        self.assertIsNotNone(order.admin_receipt_notified_at)
        send_photo_calls = [call for call in post_calls if call["url"].endswith("/sendPhoto")]
        self.assertEqual(len(send_photo_calls), 1)
        self.assertIn(order.order_tracking_code, send_photo_calls[0]["data"]["caption"])

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_manual_order_notification_waits_for_commit(self, _xui):
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                result = create_manual_payment_order(
                    store=self.store,
                    customer=None,
                    plan=self.plan,
                    inbound=self.inbound,
                    sender_card_name="Alice Buyer",
                    sender_card_last4="",
                    payment_time=time(14, 35),
                    metadata={"source": "test"},
                )
                self.assertTrue(result.success)
                self.assertEqual(post_calls, [])

            self.assertGreaterEqual(len(callbacks), 1)
            self.assertEqual(post_calls, [])
            for callback in callbacks:
                callback()

        self.assertEqual(len([call for call in post_calls if call["url"].endswith("/sendMessage")]), 1)

    def test_new_order_notification_is_idempotent(self):
        from .admin_notifications import notify_admins_new_order

        order = self.base_order()
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            notify_admins_new_order(order.pk)
            notify_admins_new_order(order.pk)

        self.assertEqual(len([call for call in post_calls if call["url"].endswith("/sendMessage")]), 1)
        order.refresh_from_db()
        self.assertIsNotNone(order.admin_notified_at)

    def test_admin_order_message_reference_is_idempotent(self):
        from .bots import remember_admin_order_message

        order = self.base_order()

        first_ref = remember_admin_order_message(
            self.bot_config,
            order,
            admin_user_id="999",
            chat_id="999",
            message_id="201",
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )
        second_ref = remember_admin_order_message(
            self.bot_config,
            order,
            admin_user_id="999",
            chat_id="999",
            message_id="201",
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )

        self.assertEqual(first_ref.pk, second_ref.pk)
        self.assertEqual(BotAdminOrderMessage.objects.filter(order=order, admin_user_id="999").count(), 1)

    def test_sends_to_multiple_admins_and_continues_after_one_failure(self):
        from .admin_notifications import notify_admins_new_order

        self.bot_config.additional_admin_user_ids = "777"
        self.bot_config.save(update_fields=["additional_admin_user_ids", "updated_at"])
        order = self.base_order()
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls, fail_chat_ids={"999"})):
            notify_admins_new_order(order.pk)

        attempted_chats = [
            call["json"]["chat_id"]
            for call in post_calls
            if call["url"].endswith("/sendMessage")
        ]
        self.assertCountEqual(attempted_chats, ["999", "777"])
        self.assertEqual(BotAdminOrderMessage.objects.filter(order=order, admin_user_id="777").count(), 1)
        order.refresh_from_db()
        self.assertIsNotNone(order.admin_notified_at)

    def test_notify_disabled_in_bot_settings_does_not_send_or_mark(self):
        from .admin_notifications import notify_admins_new_order

        self.bot_config.notify_new_orders = False
        self.bot_config.save(update_fields=["notify_new_orders", "updated_at"])
        order = self.base_order()
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            notify_admins_new_order(order.pk)

        self.assertEqual(post_calls, [])
        order.refresh_from_db()
        self.assertIsNone(order.admin_notified_at)

    def test_payment_receipt_notification_is_idempotent_after_order_notice(self):
        from .admin_notifications import notify_admins_new_order, notify_admins_payment_receipt

        order = self.base_order()
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            notify_admins_new_order(order.pk)
            order.submit_manual_payment(
                sender_card_name="Alice Buyer",
                sender_card_last4="",
                payment_time=time(14, 35),
                receipt_text="رسید پرداخت ثبت شد.",
            )
            order.save()
            notify_admins_payment_receipt(order.pk)
            notify_admins_payment_receipt(order.pk)

        send_calls = [call for call in post_calls if call["url"].endswith("/sendMessage")]
        self.assertEqual(len(send_calls), 2)
        self.assertIn("رسید پرداخت برای سفارش ثبت شد", send_calls[-1]["json"]["text"])
        order.refresh_from_db()
        self.assertIsNotNone(order.admin_receipt_notified_at)


class OrderRejectCleanupTests(TestCase):
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
            slug="reject-cleanup-1gb",
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
            current_users=1,
        )
        self.order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=self.inbound,
            uuid="11111111-1111-4111-8111-111111111111",
            username="test_user",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )
        self.vpn_client = VPNClient.objects.create(
            store=self.store,
            order=self.order,
            plan=self.plan,
            inbound=self.inbound,
            username="test_user",
            xui_email="test_user",
            uuid=self.order.uuid,
            status=VPNClient.Status.INACTIVE,
        )

    @patch("store.order_actions.delete_client", return_value=True)
    def test_reject_order_deletes_panel_client_and_suspends_local_client(self, delete_client_mock):
        result = reject_order(self.order, reason="bad receipt", notify=False)

        self.assertTrue(result.success)
        delete_client_mock.assert_called_once()
        self.order.refresh_from_db()
        self.vpn_client.refresh_from_db()
        self.inbound.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REJECTED)
        self.assertTrue(self.order.metadata["panel_client_deleted_on_reject"])
        self.assertEqual(self.vpn_client.status, VPNClient.Status.SUSPENDED)
        self.assertEqual(self.inbound.current_users, 0)


class DashboardSubscriptionActionTests(TestCase):
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
            slug="dashboard-actions-1gb",
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
            current_users=1,
        )
        self.client.get(reverse("home"))
        self.customer = Customer.objects.get()
        self.order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            uuid="11111111-1111-4111-8111-111111111111",
            username="test_user",
            sub_link="https://example.com/sub/test",
            direct_link="vless://example",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        self.vpn_client = VPNClient.objects.create(
            store=self.store,
            order=self.order,
            plan=self.plan,
            inbound=self.inbound,
            username="test_user",
            xui_email="test_user",
            uuid=self.order.uuid,
            sub_link=self.order.sub_link,
            direct_link=self.order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=self.plan.traffic_limit_bytes,
            used_traffic_bytes=self.plan.traffic_limit_bytes,
            expires_at=timezone.now() - timedelta(days=1),
        )

    @patch("store.views.sync_vpn_client_stats")
    def test_dashboard_shows_minimal_battery_card_for_expired_config(self, stats_mock):
        stats_mock.return_value = {
            "is_enabled": False,
            "is_expired": True,
            "total_traffic_bytes": self.plan.traffic_limit_bytes,
            "used_traffic_bytes": self.plan.traffic_limit_bytes,
            "remaining_traffic_bytes": 0,
            "panel_available": True,
        }

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.plan.name)
        self.assertContains(response, "تمام شده")
        self.assertContains(response, reverse("order_detail", args=[self.order.public_id]))
        self.assertNotContains(response, "نمودار")

    def test_renew_config_creates_pending_renewal_order_for_same_client(self):
        response = self.client.post(reverse("renew_config", args=[self.vpn_client.public_id]))

        self.assertEqual(response.status_code, 302)
        renewal = Order.objects.exclude(pk=self.order.pk).get()
        self.assertEqual(renewal.customer, self.customer)
        self.assertEqual(renewal.status, Order.Status.PENDING_PAYMENT)
        self.assertEqual(renewal.uuid, self.vpn_client.uuid)
        self.assertEqual(renewal.metadata["renewal_client_pk"], self.vpn_client.pk)
        self.assertEqual(VPNClient.objects.count(), 1)

    @patch("store.order_actions.delete_client", return_value=True)
    def test_delete_order_hides_order_and_suspends_panel_client(self, delete_client_mock):
        response = self.client.post(reverse("delete_order", args=[self.order.public_id]))

        self.assertEqual(response.status_code, 302)
        delete_client_mock.assert_called_once()
        self.order.refresh_from_db()
        self.vpn_client.refresh_from_db()
        self.inbound.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)
        self.assertTrue(self.order.metadata["customer_hidden"])
        self.assertEqual(self.vpn_client.status, VPNClient.Status.SUSPENDED)
        self.assertEqual(self.inbound.current_users, 0)

    @patch("store.order_actions.renew_client")
    def test_activate_renewal_order_extends_existing_client(self, renew_client_mock):
        new_expiry = timezone.now() + timedelta(days=30)
        renew_client_mock.return_value = {
            "expiry_at": new_expiry,
            "raw": {"id": self.vpn_client.uuid, "enable": True},
        }
        renewal = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            uuid=self.vpn_client.uuid,
            username=self.vpn_client.username,
            sub_link=self.vpn_client.sub_link,
            direct_link=self.vpn_client.direct_link,
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            metadata={"renewal": True, "renewal_client_pk": self.vpn_client.pk},
        )

        result = activate_order(renewal, notify=False)

        self.assertTrue(result.success)
        renew_client_mock.assert_called_once()
        renewal.refresh_from_db()
        self.vpn_client.refresh_from_db()
        self.assertEqual(renewal.status, Order.Status.COMPLETED)
        self.assertEqual(self.vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertEqual(self.vpn_client.used_traffic_bytes, 0)
        self.assertEqual(self.vpn_client.expires_at, new_expiry)

    @patch("store.order_actions.enable_client", return_value=True)
    @patch(
        "store.xui_api.create_inactive_client_details",
        return_value=fake_client_result("22222222-2222-4222-8222-222222222222"),
    )
    def test_activate_order_provisions_deferred_panel_client(self, provision_mock, enable_mock):
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            username="deferred_user",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            metadata={"panel_provisioning_deferred": True},
        )

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertFalse(order.metadata["panel_provisioning_deferred"])
        self.assertTrue(order.vpn_clients.exists())
        self.assertEqual(order.vpn_clients.first().status, VPNClient.Status.ACTIVE)
        provision_mock.assert_called_once()
        enable_mock.assert_called_once()

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.xui_api.create_inactive_client_details")
    def test_activate_bulk_order_creates_and_enables_requested_clients(self, provision_mock, enable_mock):
        def bulk_result(index, uuid):
            result = fake_client_result(uuid)
            result["email"] = f"bulk_user_{index}"
            result["sub_id"] = f"bulk-sub-{index}"
            result["sub_link"] = f"https://example.com/sub/bulk-{index}"
            result["direct_link"] = f"vless://bulk-{index}"
            return result

        provision_mock.side_effect = [
            bulk_result(2, "22222222-2222-4222-8222-222222222222"),
            bulk_result(3, "33333333-3333-4333-8333-333333333333"),
        ]
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            quantity=3,
            username="bulk_user",
            uuid="44444444-4444-4444-8444-444444444444",
            sub_link="https://example.com/sub/bulk-1",
            direct_link="vless://bulk-1",
            amount=300000,
            original_amount=300000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )
        VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="bulk_user_1",
            xui_email="bulk_user_1",
            uuid=order.uuid,
            sub_id="bulk-sub-1",
            sub_link=order.sub_link,
            direct_link=order.direct_link,
            status=VPNClient.Status.INACTIVE,
            traffic_limit_bytes=self.plan.traffic_limit_bytes,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        order.refresh_from_db()
        clients = list(order.vpn_clients.order_by("created_at", "pk"))
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(len(clients), 3)
        self.assertTrue(all(client.status == VPNClient.Status.ACTIVE for client in clients))
        self.assertEqual(provision_mock.call_count, 2)
        self.assertEqual(enable_mock.call_count, 3)

        from .bots import format_customer_order_event

        message = format_customer_order_event(order, event_type="approved")
        self.assertIn("تعداد کانفیگ: ۳", message)
        self.assertIn("vless://bulk-1", message)
        self.assertIn("vless://bulk-2", message)
        self.assertIn("vless://bulk-3", message)


class ReferralRewardSystemTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
            referral_reward_gb=Decimal("2.000"),
            referral_reward_duration_days=30,
            referral_system_enabled=True,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="1 GB",
            slug="referral-1gb",
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
        self.inviter = Customer.objects.create(display_name="Inviter")
        self.invited = Customer.objects.create(display_name="Invited")
        apply_referral_code(self.invited, self.inviter.referral_code)
        self.uuid_counter = 0

    def next_uuid(self):
        self.uuid_counter += 1
        return f"00000000-0000-4000-8000-{self.uuid_counter:012d}"

    def create_order(self, *, customer=None, status=None, verification_status=None, with_client=True):
        customer = customer or self.invited
        uuid_value = self.next_uuid()
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            uuid=uuid_value,
            username=f"user_{self.uuid_counter}",
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=status or Order.Status.PENDING_VERIFICATION,
            verification_status=verification_status or Order.VerificationStatus.PENDING,
        )
        if with_client:
            VPNClient.objects.create(
                store=self.store,
                order=order,
                plan=self.plan,
                inbound=self.inbound,
                username=order.username,
                xui_email=order.username,
                uuid=uuid_value,
                status=VPNClient.Status.INACTIVE,
                traffic_limit_bytes=self.plan.traffic_limit_bytes,
                duration_days=self.plan.duration_days,
                device_limit=self.plan.device_limit,
            )
        return order

    def set_customer_cookie(self, customer):
        response = HttpResponse()
        response.set_signed_cookie(
            CUSTOMER_COOKIE_NAME,
            str(customer.public_id),
            salt=CUSTOMER_COOKIE_SALT,
        )
        self.client.cookies[CUSTOMER_COOKIE_NAME] = response.cookies[CUSTOMER_COOKIE_NAME].value

    def test_customer_gets_referral_code_automatically(self):
        customer = Customer.objects.create(display_name="New Customer")

        self.assertTrue(customer.referral_code.startswith("RF"))
        self.assertEqual(len(customer.referral_code), 10)

    def test_ensure_referral_code_backfills_blank_customer(self):
        customer = Customer.objects.create(display_name="Legacy Customer")
        Customer.objects.filter(pk=customer.pk).update(referral_code="")
        customer.refresh_from_db()

        code = ensure_referral_code(customer)

        self.assertTrue(code.startswith("RF"))
        customer.refresh_from_db()
        self.assertEqual(customer.referral_code, code)

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_bot")
    def test_telegram_referral_link_uses_configured_bot_username(self):
        link = build_telegram_referral_link(self.inviter)

        self.assertEqual(link, f"https://t.me/azadnet_bot?start=ref_{self.inviter.referral_code}")

    @patch.dict("os.environ", {"TELEGRAM_BOT_USERNAME": ""})
    @override_settings(TELEGRAM_BOT_USERNAME="")
    def test_referral_summary_reports_missing_bot_username(self):
        summary = get_referral_summary(self.inviter, store=self.store)

        self.assertEqual(summary["telegram_link"], "")
        self.assertEqual(summary["telegram_link_missing_message"], "نام کاربری ربات تنظیم نشده است.")
        self.assertEqual(summary["invite_text"], "نام کاربری ربات تنظیم نشده است.")

    def test_web_ref_query_sets_inviter_once(self):
        response = self.client.get(reverse("home"), {"ref": self.inviter.referral_code})

        self.assertEqual(response.status_code, 200)
        browser_customer = Customer.objects.exclude(pk__in=[self.inviter.pk, self.invited.pk]).get()
        self.assertEqual(browser_customer.referred_by, self.inviter)
        self.client.get(reverse("home"), {"ref": self.invited.referral_code})
        browser_customer.refresh_from_db()
        self.assertEqual(browser_customer.referred_by, self.inviter)

    def test_self_referral_is_ignored(self):
        result = apply_referral_code(self.inviter, self.inviter.referral_code)

        self.assertIsNone(result)
        self.inviter.refresh_from_db()
        self.assertIsNone(self.inviter.referred_by)

    def test_existing_inviter_cannot_be_changed(self):
        other = Customer.objects.create(display_name="Other")
        self.invited.referred_by = other

        with self.assertRaises(ValidationError):
            self.invited.save(update_fields=["referred_by"])

    @patch("store.order_actions.enable_client", return_value=True)
    def test_first_successful_order_creates_available_reward(self, _enable):
        order = self.create_order()

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        ledger = ReferralRewardLedger.objects.get(invited=self.invited)
        self.assertEqual(ledger.inviter, self.inviter)
        self.assertEqual(ledger.order_id, order.pk)
        self.assertEqual(ledger.reward_gb, Decimal("2.000"))
        self.assertEqual(ledger.reward_duration_days, 30)
        self.assertEqual(ledger.status, ReferralRewardLedger.Status.AVAILABLE)
        self.assertIsNotNone(ledger.available_at)

    def test_pending_and_rejected_orders_do_not_create_reward(self):
        pending = self.create_order(with_client=False)
        rejected = self.create_order(
            status=Order.Status.REJECTED,
            verification_status=Order.VerificationStatus.REJECTED,
            with_client=False,
        )

        self.assertIsNone(create_referral_reward_for_order(pending))
        self.assertIsNone(create_referral_reward_for_order(rejected))
        self.assertFalse(ReferralRewardLedger.objects.exists())

    @patch("store.order_actions.enable_client", return_value=True)
    def test_repeated_approval_does_not_duplicate_reward(self, _enable):
        order = self.create_order()

        first_result = activate_order(order, notify=False)
        second_result = activate_order(order, notify=False)

        self.assertTrue(first_result.success)
        self.assertTrue(second_result.success)
        self.assertEqual(ReferralRewardLedger.objects.filter(invited=self.invited).count(), 1)

    @patch("store.order_actions.enable_client", return_value=True)
    def test_referral_system_disabled_keeps_code_but_skips_reward(self, _enable):
        self.store.referral_system_enabled = False
        self.store.save(update_fields=["referral_system_enabled", "updated_at"])
        order = self.create_order()

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        self.inviter.refresh_from_db()
        self.assertTrue(self.inviter.referral_code)
        self.assertFalse(ReferralRewardLedger.objects.exists())

    def create_active_inviter_config(self):
        order = self.create_order(
            customer=self.inviter,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            with_client=False,
        )
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="inviter_active",
            xui_email="inviter_active",
            uuid=self.next_uuid(),
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=self.plan.traffic_limit_bytes,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
            expires_at=timezone.now() + timedelta(days=10),
        )

    def create_available_ledger(self, *, invited=None, reward_gb=Decimal("2.000"), reward_duration_days=30):
        if invited is None:
            invited = self.invited
        if invited.referred_by_id != self.inviter.pk:
            apply_referral_code(invited, self.inviter.referral_code)
        order = self.create_order(
            customer=invited,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            with_client=False,
        )
        return ReferralRewardLedger.objects.create(
            inviter=self.inviter,
            invited=invited,
            order=order,
            reward_gb=reward_gb,
            reward_duration_days=reward_duration_days,
            status=ReferralRewardLedger.Status.AVAILABLE,
            available_at=timezone.now(),
        )

    def test_available_referral_gb_is_summed(self):
        self.create_available_ledger()

        self.assertEqual(get_available_referral_gb(self.inviter), Decimal("2.000"))
        summary = get_referral_summary(self.inviter, store=self.store)
        self.assertEqual(summary["available_reward_count"], 1)
        self.assertEqual(summary["available_duration_days"], 30)

    @patch("store.referral_services.add_client_traffic")
    def test_redeem_applies_available_rewards_to_active_config(self, add_traffic_mock):
        vpn_config = self.create_active_inviter_config()
        ledger = self.create_available_ledger()
        new_total = self.plan.traffic_limit_bytes + (2 * 1024 ** 3)
        original_expiry = vpn_config.expires_at
        add_traffic_mock.return_value = {
            "total_traffic_bytes": new_total,
            "raw": {"totalGB": new_total},
        }

        result = redeem_referral_rewards(self.inviter, vpn_config)

        self.assertTrue(result.success)
        ledger.refresh_from_db()
        vpn_config.refresh_from_db()
        self.assertEqual(ledger.status, ReferralRewardLedger.Status.REDEEMED)
        self.assertEqual(ledger.redeemed_config, vpn_config)
        self.assertEqual(ledger.applied_traffic_gb, Decimal("2.000"))
        self.assertEqual(ledger.applied_duration_days, 30)
        self.assertEqual(vpn_config.traffic_limit_bytes, new_total)
        self.assertEqual(vpn_config.expires_at, original_expiry + timedelta(days=30))
        self.assertEqual(vpn_config.duration_days, self.plan.duration_days + 30)
        called_config, called_gb = add_traffic_mock.call_args.args
        self.assertEqual(called_config.pk, vpn_config.pk)
        self.assertEqual(called_gb, Decimal("2.000"))
        self.assertEqual(add_traffic_mock.call_args.kwargs["extra_days"], 30)

    @patch("store.referral_services.add_client_traffic")
    def test_redeem_two_packages_applies_four_gb_and_sixty_days(self, add_traffic_mock):
        vpn_config = self.create_active_inviter_config()
        first = self.create_available_ledger()
        second_invited = Customer.objects.create(display_name="Second Invited")
        second = self.create_available_ledger(invited=second_invited)
        new_total = self.plan.traffic_limit_bytes + (4 * 1024 ** 3)
        original_expiry = vpn_config.expires_at
        add_traffic_mock.return_value = {
            "total_traffic_bytes": new_total,
            "raw": {"totalGB": new_total},
        }

        result = redeem_referral_rewards(self.inviter, vpn_config)

        self.assertTrue(result.success)
        self.assertEqual(result.reward_gb, Decimal("4.000"))
        self.assertEqual(result.reward_duration_days, 60)
        self.assertEqual(result.reward_count, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        vpn_config.refresh_from_db()
        self.assertEqual(first.status, ReferralRewardLedger.Status.REDEEMED)
        self.assertEqual(second.status, ReferralRewardLedger.Status.REDEEMED)
        self.assertEqual(vpn_config.traffic_limit_bytes, new_total)
        self.assertEqual(vpn_config.expires_at, original_expiry + timedelta(days=60))
        self.assertEqual(add_traffic_mock.call_args.kwargs["extra_days"], 60)

    @patch("store.referral_services.add_client_traffic")
    def test_redeem_expired_active_config_extends_from_now(self, add_traffic_mock):
        vpn_config = self.create_active_inviter_config()
        VPNClient.objects.filter(pk=vpn_config.pk).update(expires_at=timezone.now() - timedelta(days=2))
        vpn_config.refresh_from_db()
        self.create_available_ledger()
        new_total = self.plan.traffic_limit_bytes + (2 * 1024 ** 3)
        add_traffic_mock.return_value = {
            "total_traffic_bytes": new_total,
            "raw": {"totalGB": new_total},
        }
        before = timezone.now()

        result = redeem_referral_rewards(self.inviter, vpn_config)

        after = timezone.now()
        self.assertTrue(result.success)
        vpn_config.refresh_from_db()
        self.assertGreaterEqual(vpn_config.expires_at, before + timedelta(days=30))
        self.assertLessEqual(vpn_config.expires_at, after + timedelta(days=30, seconds=1))

    @patch("store.referral_services.add_client_traffic", return_value=None)
    def test_redeem_keeps_ledger_available_when_xui_update_fails(self, _add_traffic):
        vpn_config = self.create_active_inviter_config()
        ledger = self.create_available_ledger()
        original_total = vpn_config.traffic_limit_bytes

        result = redeem_referral_rewards(self.inviter, vpn_config)

        self.assertFalse(result.success)
        ledger.refresh_from_db()
        vpn_config.refresh_from_db()
        self.assertEqual(ledger.status, ReferralRewardLedger.Status.AVAILABLE)
        self.assertIsNone(ledger.redeemed_config)
        self.assertEqual(vpn_config.traffic_limit_bytes, original_total)

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_web_bot")
    @patch("store.views.sync_vpn_client_stats")
    def test_dashboard_shows_referral_section(self, stats_mock):
        vpn_config = self.create_active_inviter_config()
        self.create_available_ledger()
        stats_mock.return_value = {
            "is_enabled": True,
            "is_expired": False,
            "total_traffic_bytes": vpn_config.traffic_limit_bytes,
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": vpn_config.traffic_limit_bytes,
            "panel_available": True,
        }
        self.set_customer_cookie(self.inviter)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "دعوت دوستان")
        self.assertContains(response, self.inviter.referral_code)
        self.assertContains(response, f"https://t.me/azadnet_web_bot?start=ref_{self.inviter.referral_code}")
        self.assertContains(response, "متن آماده دعوت")
        self.assertContains(response, "بسته آماده دریافت")
        self.assertContains(response, "۳۰ روز")
        self.assertContains(response, "دریافت هدیه")


class CustomerAnalyticsTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
            top_customers_limit=1,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="5 GB",
            slug="analytics-5gb",
            volume_gb=Decimal("5.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram analytics",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        self.now = timezone.now()
        self.customer_counter = 0
        self.uuid_counter = 0

    def customer(self, name):
        self.customer_counter += 1
        return Customer.objects.create(display_name=name, username=f"analytics_user_{self.customer_counter}")

    def next_uuid(self):
        self.uuid_counter += 1
        return f"10000000-0000-4000-8000-{self.uuid_counter:012d}"

    def order(
        self,
        customer,
        *,
        amount=100000,
        days_ago=0,
        status=Order.Status.COMPLETED,
        verification_status=Order.VerificationStatus.VERIFIED,
        metadata=None,
        quantity=1,
    ):
        purchased_at = self.now - timedelta(days=days_ago)
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            amount=amount,
            original_amount=amount,
            currency=Plan.Currency.TOMAN,
            quantity=quantity,
            is_paid=status == Order.Status.COMPLETED,
            status=status,
            verification_status=verification_status,
            verified_at=purchased_at if verification_status == Order.VerificationStatus.VERIFIED else None,
            metadata=metadata or {},
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=purchased_at,
            verified_at=purchased_at if verification_status == Order.VerificationStatus.VERIFIED else None,
        )
        order.created_at = purchased_at
        order.verified_at = purchased_at if verification_status == Order.VerificationStatus.VERIFIED else None
        return order

    def create_whale(self):
        whale = self.customer("Whale")
        self.order(whale, amount=1000000)
        return whale

    def test_customer_stats_count_only_successful_paid_orders(self):
        customer = self.customer("Alice")
        successful = self.order(customer, amount=200000, quantity=2)
        self.order(customer, amount=300000)
        self.order(
            customer,
            amount=900000,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )
        self.order(
            customer,
            amount=800000,
            status=Order.Status.REJECTED,
            verification_status=Order.VerificationStatus.REJECTED,
        )
        VPNClient.objects.create(
            store=self.store,
            order=successful,
            plan=self.plan,
            username="analytics_active",
            xui_email="analytics_active",
            uuid=self.next_uuid(),
            status=VPNClient.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=10),
        )

        stats = get_customer_stats(customer)

        self.assertEqual(stats["total_paid_amount"], 500000)
        self.assertEqual(stats["successful_orders_count"], 2)
        self.assertEqual(stats["total_purchased_gb"], Decimal("15"))
        self.assertEqual(stats["active_configs_count"], 1)

    def test_customer_stats_count_successful_renewals(self):
        customer = self.customer("Renewal Buyer")
        self.order(customer, metadata={"renewal": True, "renewal_client_pk": 123})
        self.order(customer)

        stats = get_customer_stats(customer)

        self.assertEqual(stats["renewal_orders_count"], 1)

    def test_customer_stats_include_referral_ledger_totals(self):
        inviter = self.customer("Inviter")
        invited = self.customer("Invited")
        order = self.order(invited)
        ReferralRewardLedger.objects.create(
            inviter=inviter,
            invited=invited,
            order=order,
            reward_gb=Decimal("2.000"),
            status=ReferralRewardLedger.Status.AVAILABLE,
            available_at=timezone.now(),
        )
        redeemed_invited = self.customer("Redeemed Invited")
        redeemed_order = self.order(redeemed_invited)
        ReferralRewardLedger.objects.create(
            inviter=inviter,
            invited=redeemed_invited,
            order=redeemed_order,
            reward_gb=Decimal("1.500"),
            status=ReferralRewardLedger.Status.REDEEMED,
            redeemed_at=timezone.now(),
        )

        stats = get_customer_stats(inviter)

        self.assertEqual(stats["available_referral_gb"], Decimal("2"))
        self.assertEqual(stats["redeemed_referral_gb"], Decimal("1.5"))

    def test_segment_detects_loyal_customer(self):
        self.create_whale()
        customer = self.customer("Loyal")
        self.order(customer, amount=100000)
        self.order(customer, amount=100000, days_ago=2)

        self.assertEqual(get_customer_segment(customer), SEGMENT_LOYAL)

    def test_segment_detects_good_customer(self):
        self.create_whale()
        customer = self.customer("Good")
        self.order(customer, amount=500000)

        self.assertEqual(get_customer_segment(customer), SEGMENT_GOOD)

    def test_segment_detects_top_buyer(self):
        whale = self.create_whale()
        self.order(self.customer("Small Buyer"), amount=100000)

        self.assertEqual(get_customer_segment(whale), SEGMENT_TOP_BUYER)

    def test_segment_detects_top_referrer(self):
        inviter = self.customer("Referrer")
        invited = self.customer("Referral Child")
        invited.referred_by = inviter
        invited.save(update_fields=["referred_by", "updated_at"])
        self.order(invited, amount=400000)

        self.assertEqual(get_customer_segment(inviter), SEGMENT_TOP_REFERRER)

    def test_segment_detects_inactive_customer(self):
        self.create_whale()
        customer = self.customer("Inactive")
        self.order(customer, amount=100000, days_ago=45)

        self.assertEqual(get_customer_segment(customer), SEGMENT_INACTIVE)

    def test_segment_detects_no_order_customer(self):
        customer = self.customer("No Order")

        self.assertEqual(get_customer_segment(customer), SEGMENT_NO_ORDER)

    def test_period_ranges_are_timezone_aware(self):
        fixed_now = timezone.make_aware(datetime(2026, 6, 4, 12, 30), timezone.get_current_timezone())

        with patch("store.customer_analytics.timezone.now", return_value=fixed_now):
            today_from, today_to = get_period_range(PERIOD_TODAY)
            last_7_from, last_7_to = get_period_range(PERIOD_LAST_7_DAYS)
            last_30_from, last_30_to = get_period_range(PERIOD_LAST_30_DAYS)

        self.assertTrue(timezone.is_aware(today_from))
        self.assertEqual(timezone.localtime(today_from).time(), time(0, 0))
        self.assertEqual(today_to, fixed_now)
        self.assertEqual(last_7_to, fixed_now)
        self.assertEqual(last_7_from, fixed_now - timedelta(days=7))
        self.assertEqual(last_30_to, fixed_now)
        self.assertEqual(last_30_from, fixed_now - timedelta(days=30))

    def test_get_customers_by_segment_returns_expected_querysets(self):
        top_buyer = self.create_whale()
        no_order = self.customer("No Order")
        loyal = self.customer("Loyal")
        self.order(loyal, amount=100000)
        self.order(loyal, amount=100000, days_ago=1)

        self.assertEqual(list(get_customers_by_segment(SEGMENT_TOP_BUYER)), [top_buyer])
        self.assertIn(no_order, list(get_customers_by_segment(SEGMENT_NO_ORDER)))
        self.assertIn(loyal, list(get_customers_by_segment(SEGMENT_LOYAL)))

    def test_bot_customer_report_with_data(self):
        customer = self.customer("Alice")
        self.order(customer, amount=250000)

        report = format_customer_analytics_report("top_30d", config=self.bot_config)

        self.assertIn("۱۰ خریدار برتر ۳۰ روز اخیر", report)
        self.assertIn("Alice", report)
        self.assertIn("۲۵۰,۰۰۰ تومان", report)

    def test_bot_customer_report_without_data(self):
        report = format_customer_analytics_report("top_today", config=self.bot_config)

        self.assertIn("داده‌ای برای این گزارش پیدا نشد", report)


class BroadcastCampaignTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
            bank_name="Test Bank",
            top_customers_limit=1,
            broadcast_rate_limit_per_second=1000,
            broadcast_max_recipients_per_campaign=100,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Broadcast 5 GB",
            slug="broadcast-5gb",
            volume_gb=Decimal("5.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram broadcast",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        self.url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])
        self.now = timezone.now()
        self.customer_counter = 0
        self.uuid_counter = 0

    def customer(self, name, *, is_active=True):
        self.customer_counter += 1
        return Customer.objects.create(
            display_name=name,
            username=f"broadcast_user_{self.customer_counter}",
            is_active=is_active,
        )

    def order(self, customer, *, amount=100000, days_ago=0):
        purchased_at = self.now - timedelta(days=days_ago)
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            amount=amount,
            original_amount=amount,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            verified_at=purchased_at,
        )
        Order.objects.filter(pk=order.pk).update(created_at=purchased_at, verified_at=purchased_at)
        order.created_at = purchased_at
        order.verified_at = purchased_at
        return order

    def bot_user(self, customer, *, chat_id="42", bot_config=None):
        bot_config = bot_config or self.bot_config
        return BotUser.objects.create(
            bot_config=bot_config,
            customer=customer,
            provider_user_id=str(chat_id),
            chat_id=str(chat_id),
            username=f"broadcast_{chat_id}",
            display_name=f"Broadcast {chat_id}",
        )

    def campaign(self, *, audience_type=BroadcastMessage.AudienceType.ALL, channel=BroadcastMessage.Channel.TELEGRAM):
        return BroadcastMessage.objects.create(
            store=self.store,
            title="Test broadcast",
            message_text="سلام مشتری",
            audience_type=audience_type,
            channel=channel,
        )

    def post_update(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def message(self, text, *, message_id=1, user_id=999, username="admin", first_name="Admin"):
        return {
            "message": {
                "message_id": message_id,
                "from": {"id": user_id, "username": username, "first_name": first_name},
                "chat": {"id": user_id, "type": "private"},
                "text": text,
            }
        }

    def callback(self, data, *, message_id=10, callback_id="bc-cb", user_id=999, username="admin", first_name="Admin"):
        return {
            "callback_query": {
                "id": callback_id,
                "from": {"id": user_id, "username": username, "first_name": first_name},
                "message": {"message_id": message_id, "chat": {"id": user_id, "type": "private"}},
                "data": data,
            }
        }

    def test_resolve_audience_returns_all_active_customers(self):
        first = self.customer("First")
        second = self.customer("Second")
        inactive = self.customer("Inactive Account", is_active=False)
        self.bot_user(first, chat_id="101")
        self.bot_user(second, chat_id="102")
        self.bot_user(inactive, chat_id="103")

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.ALL, store=self.store))

        self.assertIn(first, customers)
        self.assertIn(second, customers)
        self.assertNotIn(inactive, customers)

    def test_loyal_audience_uses_customer_analytics(self):
        loyal = self.customer("Loyal")
        self.order(loyal)
        self.order(loyal, days_ago=1)
        self.bot_user(loyal)

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.LOYAL, store=self.store))

        self.assertIn(loyal, customers)

    def test_top_buyer_audience_uses_customer_analytics(self):
        whale = self.customer("Whale")
        small = self.customer("Small")
        self.order(whale, amount=1000000)
        self.order(small, amount=100000)

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.TOP_BUYER, store=self.store))

        self.assertEqual(customers, [whale])

    def test_inactive_audience_uses_customer_analytics(self):
        inactive = self.customer("Inactive")
        recent = self.customer("Recent")
        self.order(inactive, days_ago=45)
        self.order(recent, days_ago=5)

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.INACTIVE, store=self.store))

        self.assertIn(inactive, customers)
        self.assertNotIn(recent, customers)

    def test_recipient_uniqueness_is_idempotent(self):
        customer = self.customer("Alice")
        self.bot_user(customer)
        campaign = self.campaign()

        create_campaign_recipients(campaign)
        create_campaign_recipients(campaign)

        self.assertEqual(BroadcastRecipient.objects.filter(campaign=campaign, customer=customer).count(), 1)

    def test_users_without_bot_target_are_skipped(self):
        customer = self.customer("No Bot")
        self.order(customer)
        campaign = self.campaign(audience_type=BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS)

        create_campaign_recipients(campaign)

        recipient = BroadcastRecipient.objects.get(campaign=campaign, customer=customer)
        self.assertEqual(recipient.status, BroadcastRecipient.Status.SKIPPED)
        self.assertIn("No active bot user", recipient.error_message)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_send_campaign_sends_each_pending_recipient(self, post_mock):
        first = self.customer("First")
        second = self.customer("Second")
        self.order(first)
        self.order(second)
        self.bot_user(first, chat_id="201")
        self.bot_user(second, chat_id="202")
        campaign = self.campaign(audience_type=BroadcastMessage.AudienceType.ACTIVE_CUSTOMERS)

        counts = send_campaign(campaign)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, BroadcastMessage.Status.SENT)
        self.assertEqual(counts["success"], 2)
        self.assertEqual(campaign.success_count, 2)
        self.assertEqual(BroadcastRecipient.objects.filter(status=BroadcastRecipient.Status.SENT).count(), 2)
        sent_chat_ids = {
            call.kwargs["json"]["chat_id"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("text") == "سلام مشتری"
        }
        self.assertEqual(sent_chat_ids, {"201", "202"})

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_non_admin_cannot_start_broadcast_from_bot(self, post_mock):
        response = self.post_update(self.message("ارسال پیام 📣", user_id=42, username="alice", first_name="Alice"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(BroadcastMessage.objects.exists())
        sent_texts = [
            call.kwargs["json"].get("text", "")
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "42"
        ]
        self.assertFalse(any("گروه مخاطبان را انتخاب کنید" in text for text in sent_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_bot_preview_shows_resolved_recipient_count(self, post_mock):
        customer = self.customer("Active Customer")
        self.order(customer)
        self.bot_user(customer, chat_id="301")

        self.post_update(self.callback("admin:bc:aud:active_customers"))
        response = self.post_update(self.message("سلام مشتری", message_id=2))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="999")
        self.assertEqual(bot_user.state, BotUser.State.BROADCAST_CONFIRM)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("پیش‌نمایش ارسال پیام", payload["text"])
        self.assertIn("۱ مخاطب پیدا شد", payload["text"])
        self.assertIn("قابل ارسال: ۱", payload["text"])

    @patch("store.bots.requests.post")
    def test_bot_final_send_creates_campaign_and_reports_counts(self, post_mock):
        customer = self.customer("Active Customer")
        self.order(customer)
        self.bot_user(customer, chat_id="401")
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            return DummyBotResponse({"ok": True, "result": {"message_id": 100}})

        post_mock.side_effect = post_side_effect

        self.post_update(self.callback("admin:bc:aud:active_customers"))
        self.post_update(self.message("سلام مشتری", message_id=2))
        response = self.post_update(self.callback("admin:bc:send", callback_id="bc-send", message_id=20))

        self.assertEqual(response.status_code, 200)
        campaign = BroadcastMessage.objects.get()
        self.assertEqual(campaign.status, BroadcastMessage.Status.SENT)
        self.assertEqual(campaign.success_count, 1)
        self.assertEqual(campaign.recipients.get().status, BroadcastRecipient.Status.SENT)
        self.assertTrue(
            any(
                call["url"].endswith("/sendMessage")
                and call.get("json", {}).get("chat_id") == "401"
                and call.get("json", {}).get("text") == "سلام مشتری"
                for call in post_calls
            )
        )
        self.assertTrue(
            any(
                call["url"].endswith("/sendMessage")
                and call.get("json", {}).get("chat_id") == "999"
                and "موفق: ۱" in call.get("json", {}).get("text", "")
                for call in post_calls
            )
        )


class SupportChatTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Azad Net",
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram support",
            bot_token="telegram-token",
            admin_user_id="999",
            is_active=True,
        )
        self.webhook_url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_customer_message_creates_dynamic_support_conversation(self, post_mock):
        response = self.client.post(
            reverse("support_send_message"),
            data={"contact_value": "@alice", "body": "سلام، کانفیگم وصل نمی‌شود."},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        conversation = SupportConversation.objects.get()
        self.assertEqual(conversation.contact_value, "@alice")
        self.assertEqual(conversation.status, SupportConversation.Status.WAITING_ADMIN)

        support_message = SupportMessage.objects.get()
        self.assertEqual(support_message.sender_type, SupportMessage.SenderType.CUSTOMER)
        self.assertEqual(support_message.body, "سلام، کانفیگم وصل نمی‌شود.")

        send_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("پیام جدید پشتیبانی", send_payload["text"])
        self.assertIn("@alice", send_payload["text"])
        callback_values = [
            button["callback_data"]
            for row in send_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"support:reply:{conversation.pk}", callback_values)

        messages_response = self.client.get(reverse("support_messages"))
        self.assertEqual(messages_response.status_code, 200)
        messages_payload = messages_response.json()
        self.assertEqual(messages_payload["conversation"]["id"], conversation.pk)
        self.assertEqual(messages_payload["messages"][0]["body"], support_message.body)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_reply_from_bot_is_saved_for_support_page(self, _post_mock):
        customer = Customer.objects.create(display_name="Alice")
        conversation = SupportConversation.objects.create(
            store=self.store,
            customer=customer,
            contact_value="@alice",
            status=SupportConversation.Status.WAITING_ADMIN,
        )
        SupportMessage.objects.create(
            conversation=conversation,
            sender_type=SupportMessage.SenderType.CUSTOMER,
            customer=customer,
            body="سلام",
        )

        callback_response = self.client.post(
            self.webhook_url,
            data=json.dumps(
                {
                    "callback_query": {
                        "id": "support-cb",
                        "from": {"id": 999, "username": "admin"},
                        "message": {"message_id": 10, "chat": {"id": 999, "type": "private"}},
                        "data": f"support:replay:{conversation.pk}",
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(callback_response.status_code, 200)
        pending = BotPendingAction.objects.get(support_conversation=conversation)
        self.assertEqual(pending.action, BotPendingAction.Action.SUPPORT_REPLY)
        self.assertEqual(pending.status, BotPendingAction.Status.PENDING)

        reply_response = self.client.post(
            self.webhook_url,
            data=json.dumps(
                {
                    "message": {
                        "message_id": 11,
                        "from": {"id": 999, "username": "admin"},
                        "chat": {"id": 999, "type": "private"},
                        "text": "لطفا یک بار لینک را بروزرسانی کن.",
                    }
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(reply_response.status_code, 200)
        conversation.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(conversation.status, SupportConversation.Status.ANSWERED)
        self.assertEqual(pending.status, BotPendingAction.Status.COMPLETED)
        admin_message = SupportMessage.objects.get(sender_type=SupportMessage.SenderType.ADMIN)
        self.assertEqual(admin_message.body, "لطفا یک بار لینک را بروزرسانی کن.")


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
        cache.clear()

    def post_update(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def message(self, text, *, message_id=1, user_id=42, username="alice", first_name="Alice"):
        return {
            "message": {
                "message_id": message_id,
                "from": {"id": user_id, "username": username, "first_name": first_name},
                "chat": {"id": user_id, "type": "private"},
                "text": text,
            }
        }

    def contact_message(self, phone_number, *, message_id=2, user_id=42, username="alice", first_name="Alice"):
        return {
            "message": {
                "message_id": message_id,
                "from": {"id": user_id, "username": username, "first_name": first_name},
                "chat": {"id": user_id, "type": "private"},
                "contact": {
                    "phone_number": phone_number,
                    "user_id": user_id,
                    "first_name": first_name,
                },
            }
        }

    def callback(self, data, *, message_id=10, callback_id="cb", user_id=42, username="alice", first_name="Alice"):
        return {
            "callback_query": {
                "id": callback_id,
                "from": {"id": user_id, "username": username, "first_name": first_name},
                "message": {"message_id": message_id, "chat": {"id": user_id, "type": "private"}},
                "data": data,
            }
        }

    def enable_force_join(self, *, channel_id="", username="azadnet_channel", invite_link="https://t.me/azadnet_channel"):
        self.bot_config.force_telegram_channel_join = True
        self.bot_config.telegram_required_channel_id = channel_id
        self.bot_config.telegram_required_channel_username = username
        self.bot_config.telegram_required_channel_invite_link = invite_link
        self.bot_config.telegram_join_check_message = "برای استفاده از ربات ابتدا عضو کانال شوید."
        self.bot_config.save(
            update_fields=[
                "force_telegram_channel_join",
                "telegram_required_channel_id",
                "telegram_required_channel_username",
                "telegram_required_channel_invite_link",
                "telegram_join_check_message",
                "updated_at",
            ]
        )

    def enable_free_trial(self, *, enabled=True):
        self.store.free_trial_enabled = enabled
        self.store.free_trial_panel = self.panel
        self.store.free_trial_inbound = self.inbound
        self.store.free_trial_traffic_gb = Decimal("1.000")
        self.store.free_trial_duration_hours = 24
        self.store.free_trial_cooldown_days = 30
        self.store.save(
            update_fields=[
                "free_trial_enabled",
                "free_trial_panel",
                "free_trial_inbound",
                "free_trial_traffic_gb",
                "free_trial_duration_hours",
                "free_trial_cooldown_days",
                "updated_at",
            ]
        )

    def membership_post_side_effect(self, post_calls, *, status="member", api_failure_description=""):
        def side_effect(url, json=None, data=None, **kwargs):
            post_calls.append({"url": url, "json": json, "data": data, **kwargs})
            if url.endswith("/getChatMember"):
                if api_failure_description:
                    return DummyBotResponse({"ok": False, "description": api_failure_description})
                return DummyBotResponse({"ok": True, "result": {"status": status}})
            return DummyBotResponse({"ok": True, "result": {"message_id": 100 + len(post_calls)}})

        return side_effect

    def sent_message_payloads(self, post_calls):
        return [
            call["json"]
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json")
        ]

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_creates_bot_user_and_customer(self, _post):
        response = self.post_update(self.message("/start"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.chat_id, "42")
        self.assertEqual(bot_user.username, "alice")
        self.assertIsNotNone(bot_user.customer)
        self.assertEqual(bot_user.customer.display_name, "Alice")
        self.assertEqual(bot_user.customer.username, "alice")
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        self.assertEqual(Customer.objects.count(), 1)
        from .bot_targets import get_primary_customer_telegram_target

        target = get_primary_customer_telegram_target(bot_user.customer, store=self.store)
        self.assertIsNotNone(target)
        self.assertEqual(target.chat_id, "42")
        self.assertEqual(target.telegram_user_id, "42")
        self.assertEqual(target.source, "bot_user")
        payload = _post.call_args.kwargs["json"]
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callback_values,
            [
                "user:buy",
                "user:subs",
                "user:free_trial",
                "user:renew",
                "user:orders",
                "user:config_lookup",
                "user:referrals",
                "user:support",
                "user:help",
                "user:profile",
            ],
        )
        self.assertIn("پروفایل شما آماده است", payload["text"])

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_payload_sets_customer_referrer(self, _post):
        inviter = Customer.objects.create(display_name="Inviter")

        response = self.post_update(
            self.message(
                f"/start ref_{inviter.referral_code}",
                user_id=43,
                username="bob",
                first_name="Bob",
            )
        )

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="43")
        bot_user.customer.refresh_from_db()
        self.assertEqual(bot_user.customer.referred_by, inviter)
        self.assertTrue(
            Referral.objects.filter(referrer=inviter, referred_customer=bot_user.customer).exists()
        )

    @patch("store.bots.requests.post")
    def test_force_join_disabled_does_not_check_membership(self, post_mock):
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(call["url"].endswith("/getChatMember") for call in post_calls))
        self.assertIn("پلن‌های فعال", self.sent_message_payloads(post_calls)[-1]["text"])

    @patch("store.bots.requests.post")
    def test_force_join_enabled_allows_channel_member(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="member")

        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        membership_payloads = [
            call["json"]
            for call in post_calls
            if call["url"].endswith("/getChatMember")
        ]
        self.assertEqual(membership_payloads[0]["chat_id"], "@azadnet_channel")
        self.assertEqual(membership_payloads[0]["user_id"], 42)
        self.assertIn("پلن‌های فعال", self.sent_message_payloads(post_calls)[-1]["text"])

    @patch("store.bots.requests.post")
    def test_force_join_enabled_blocks_non_member(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        message_payload = self.sent_message_payloads(post_calls)[-1]
        self.assertIn("برای استفاده از ربات ابتدا عضو کانال شوید", message_payload["text"])
        callback_values = [
            button.get("callback_data")
            for row in message_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        urls = [
            button.get("url")
            for row in message_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("check_membership", callback_values)
        self.assertIn("https://t.me/azadnet_channel", urls)
        self.assertFalse(any("پلن‌های فعال" in payload["text"] for payload in self.sent_message_payloads(post_calls)))

    @patch("store.bots.requests.post")
    def test_check_membership_callback_shows_menu_after_join(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="member")

        response = self.post_update(self.callback("check_membership", callback_id="membership-cb"))

        self.assertEqual(response.status_code, 200)
        message_payload = self.sent_message_payloads(post_calls)[-1]
        self.assertIn("پروفایل شما آماده است", message_payload["text"])
        callback_values = [
            button["callback_data"]
            for row in message_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:buy", callback_values)

    @patch("store.bots.requests.post")
    def test_force_join_admin_bypass_skips_membership_check(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(
            self.callback("user:buy", user_id=999, username="admin", first_name="Admin")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(call["url"].endswith("/getChatMember") for call in post_calls))
        self.assertIn("پلن‌های فعال", self.sent_message_payloads(post_calls)[-1]["text"])

    @patch("store.bots.requests.post")
    def test_force_join_invalid_settings_blocks_without_crashing(self, post_mock):
        self.enable_force_join(username="", invite_link="")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="member")

        response = self.post_update(self.callback("user:orders"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(call["url"].endswith("/getChatMember") for call in post_calls))
        message_payload = self.sent_message_payloads(post_calls)[-1]
        self.assertIn("برای استفاده از ربات ابتدا عضو کانال شوید", message_payload["text"])
        buttons = [
            button
            for row in message_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual([button.get("callback_data") for button in buttons], ["check_membership"])
        self.assertFalse(any("url" in button for button in buttons))

    @patch("store.bots.requests.post")
    def test_force_join_telegram_api_failure_blocks_without_crashing(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(
            post_calls,
            api_failure_description="Forbidden: bot is not admin",
        )

        response = self.post_update(self.callback("user:orders"))

        self.assertEqual(response.status_code, 200)
        message_payload = self.sent_message_payloads(post_calls)[-1]
        self.assertIn("برای استفاده از ربات ابتدا عضو کانال شوید", message_payload["text"])
        self.assertTrue(any(call["url"].endswith("/getChatMember") for call in post_calls))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_callback_prompts_for_config_link(self, post_mock):
        from .bots import BOT_STATE_CONFIG_LOOKUP_WAIT_LINK

        response = self.post_update(self.callback("user:config_lookup", callback_id="lookup-cb"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.state, BOT_STATE_CONFIG_LOOKUP_WAIT_LINK)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("لینک کانفیگ خود را ارسال کنید", payload["text"])
        button_texts = [
            button["text"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(button_texts, ["لغو", "بازگشت به منو"])

    @patch("store.bots.requests.post")
    def test_config_lookup_force_join_guard_blocks_non_member(self, post_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:config_lookup", callback_id="lookup-cb"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        sent_texts = [payload["text"] for payload in self.sent_message_payloads(post_calls)]
        self.assertTrue(any("برای استفاده از ربات ابتدا عضو کانال شوید" in text for text in sent_texts))
        self.assertFalse(any("لینک کانفیگ خود را ارسال کنید" in text for text in sent_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_free_trial_callback_shows_preview_when_enabled(self, post_mock):
        self.enable_free_trial()

        response = self.post_update(self.callback("user:free_trial", callback_id="trial-cb"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("دریافت تست رایگان", payload["text"])
        self.assertIn("حجم تست: ۱ گیگابایت", payload["text"])
        self.assertIn("مدت اعتبار: ۲۴ ساعت", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(callback_values, ["user:free_trial_confirm", "user:free_trial_cancel"])

    @patch("store.free_trial_services.create_trial_client_details", return_value=fake_client_result("45454545-4545-4545-8545-454545454545"))
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_free_trial_confirm_creates_config_and_sends_link(self, post_mock, xui_mock):
        self.enable_free_trial()

        response = self.post_update(self.callback("user:free_trial_confirm", callback_id="trial-confirm"))

        self.assertEqual(response.status_code, 200)
        trial_request = FreeTrialRequest.objects.get()
        self.assertEqual(trial_request.status, FreeTrialRequest.Status.DELIVERED)
        self.assertEqual(trial_request.telegram_user_id, "42")
        self.assertIsNotNone(trial_request.customer)
        self.assertIsNotNone(trial_request.vpn_client)
        self.assertEqual(trial_request.vpn_client.status, VPNClient.Status.ACTIVE)
        from .bot_targets import get_vpn_client_telegram_targets

        targets = get_vpn_client_telegram_targets(trial_request.vpn_client, store=self.store)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].chat_id, "42")
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("تست رایگان شما آماده شد", payload["text"])
        self.assertIn("vless://example", payload["text"])
        self.assertIn("خرید سرویس", payload["text"])
        xui_mock.assert_called_once()

    @patch("store.free_trial_services.create_trial_client_details")
    @patch("store.bots.requests.post")
    def test_free_trial_force_join_guard_blocks_non_member(self, post_mock, xui_mock):
        self.enable_free_trial()
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:free_trial", callback_id="trial-cb"))

        self.assertEqual(response.status_code, 200)
        sent_texts = [payload["text"] for payload in self.sent_message_payloads(post_calls)]
        self.assertTrue(any("برای استفاده از ربات ابتدا عضو کانال شوید" in text for text in sent_texts))
        self.assertFalse(any("دریافت تست رایگان" in text and "حجم تست" in text for text in sent_texts))
        self.assertFalse(FreeTrialRequest.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.bots.check_config_usage", return_value={"found": False, "message": "این کانفیگ در پنل‌های ما پیدا نشد."})
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_rate_limit_blocks_sixth_attempt(self, post_mock, check_mock):
        client_id = "11111111-1111-4111-8111-111111111111"

        for index in range(6):
            self.post_update(self.callback("user:config_lookup", callback_id=f"lookup-{index}"))
            self.post_update(self.message(client_id, message_id=20 + index))

        self.assertEqual(check_mock.call_count, 5)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("تعداد درخواست‌های بررسی شما زیاد شده", payload["text"])

    @patch("store.config_lookup.find_client_by_identifier")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_response_and_logs_do_not_include_full_config_link(self, post_mock, finder_mock):
        client_id = "11111111-1111-4111-8111-111111111111"
        full_link = f"vless://{client_id}@vpn.example.com:443?type=tcp&security=none#private-remark"
        total = 30 * (1024 ** 3)
        used = int(12.5 * (1024 ** 3))
        finder_mock.return_value = {
            "panel": self.panel,
            "inbound": self.inbound,
            "protocol": "vless",
            "client": {"id": client_id, "email": "alice_config", "remark": "Alice"},
            "client_stats": {"email": "alice_config"},
            "total_traffic_bytes": total,
            "used_traffic_bytes": used,
            "used_upload_bytes": used,
            "used_download_bytes": 0,
            "remaining_traffic_bytes": total - used,
            "expiry_at": timezone.now() + timedelta(days=12),
            "last_online_at": timezone.now(),
            "is_enabled": True,
        }

        self.post_update(self.callback("user:config_lookup", callback_id="lookup-cb"))
        response = self.post_update(self.message(full_link, message_id=22))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("📊 وضعیت کانفیگ شما", payload["text"])
        self.assertIn("باقی‌مانده: ۱۷.۵ گیگ", payload["text"])
        self.assertNotIn(full_link, payload["text"])
        self.assertNotIn("vless://", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        update_callback = next(value for value in callback_values if value.startswith("user:config_lookup_update:"))
        self.assertNotIn(full_link, update_callback)
        self.assertNotIn(client_id, update_callback)
        logged_payloads = "\n".join(
            json.dumps(event.raw_payload, ensure_ascii=False)
            for event in BotEventLog.objects.all()
        )
        self.assertNotIn(full_link, logged_payloads)
        self.assertNotIn("vless://", logged_payloads)
        self.assertIn("<config-link-redacted>", logged_payloads)

    def send_lookup_and_get_update_callback(self, post_mock, *, client_id="11111111-1111-4111-8111-111111111111"):
        full_link = f"vless://{client_id}@vpn.example.com:443?type=tcp&security=none#private-remark"
        total = 30 * (1024 ** 3)
        with patch(
            "store.bots.check_config_usage",
            return_value={
                "found": True,
                "message": "📊 وضعیت کانفیگ شما\n\nمصرف‌شده: ۱ گیگ",
                "panel": self.panel,
                "panel_id": self.panel.pk,
                "inbound": self.inbound,
                "inbound_id": self.inbound.inbound_id,
                "identifier": client_id,
                "protocol": "vless",
                "email": "alice_config",
                "total_bytes": total,
                "used_bytes": 1024 ** 3,
                "remaining_bytes": total - (1024 ** 3),
            },
        ):
            self.post_update(self.callback("user:config_lookup", callback_id="lookup-cb"))
            self.post_update(self.message(full_link, message_id=22))

        payload = post_mock.call_args.kwargs["json"]
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        return next(value for value in callback_values if value.startswith("user:config_lookup_update:"))

    @patch("store.bots.build_config_link_for_identifier")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_update_callback_sends_updated_link_without_leaking_callback(self, post_mock, builder_mock):
        client_id = "11111111-1111-4111-8111-111111111111"
        updated_link = f"vless://{client_id}@new.example.com:443?type=ws&host=new.example.com#Alice"
        callback_data = self.send_lookup_and_get_update_callback(post_mock, client_id=client_id)
        builder_mock.return_value = {
            "updated_config_link": updated_link,
            "protocol": "vless",
            "remark": "Alice",
            "email": "alice_config",
        }

        response = self.post_update(self.callback(callback_data, callback_id="update-cb", message_id=33))

        self.assertEqual(response.status_code, 200)
        builder_mock.assert_called_once()
        _, inbound_id, identifier = builder_mock.call_args.args
        self.assertEqual(inbound_id, self.inbound.inbound_id)
        self.assertEqual(identifier, client_id)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn(updated_link, payload["text"])
        self.assertNotIn(updated_link, callback_data)
        self.assertNotIn(client_id, callback_data)
        logged_payloads = "\n".join(
            json.dumps(event.raw_payload, ensure_ascii=False)
            for event in BotEventLog.objects.all()
        )
        self.assertNotIn(updated_link, logged_payloads)
        self.assertNotIn("new.example.com", logged_payloads)

    @patch("store.bots.build_config_link_for_identifier")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_update_callback_reports_no_update_when_link_unchanged(self, post_mock, builder_mock):
        client_id = "11111111-1111-4111-8111-111111111111"
        unchanged_link = f"vless://{client_id}@vpn.example.com:443?type=tcp&security=none#renamed-only"
        callback_data = self.send_lookup_and_get_update_callback(post_mock, client_id=client_id)
        builder_mock.return_value = {
            "updated_config_link": unchanged_link,
            "protocol": "vless",
            "remark": "Alice",
            "email": "alice_config",
            "enabled": True,
        }

        response = self.post_update(self.callback(callback_data, callback_id="update-cb", message_id=33))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("این کانفیگ آپدیت ندارد", payload["text"])
        self.assertNotIn("vless://", payload["text"])

    @patch("store.bots.build_config_link_for_identifier")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_update_callback_reports_no_update_when_client_inactive(self, post_mock, builder_mock):
        client_id = "11111111-1111-4111-8111-111111111111"
        callback_data = self.send_lookup_and_get_update_callback(post_mock, client_id=client_id)
        builder_mock.return_value = {
            "updated_config_link": f"vless://{client_id}@changed.example.com:443?type=ws#Alice",
            "protocol": "vless",
            "remark": "Alice",
            "email": "alice_config",
            "enabled": False,
        }

        response = self.post_update(self.callback(callback_data, callback_id="update-cb", message_id=33))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("این کانفیگ آپدیت ندارد", payload["text"])
        self.assertNotIn("vless://", payload["text"])

    @patch("store.bots.build_config_link_for_identifier")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_lookup_update_rate_limit_blocks_sixth_attempt(self, post_mock, builder_mock):
        callback_data = self.send_lookup_and_get_update_callback(post_mock)
        builder_mock.return_value = {
            "updated_config_link": "vless://updated.example",
            "remark": "Alice",
        }

        for index in range(6):
            self.post_update(self.callback(callback_data, callback_id=f"update-{index}", message_id=40 + index))

        self.assertEqual(builder_mock.call_count, 5)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("تعداد درخواست‌های آپدیت کانفیگ زیاد شده", payload["text"])

    @patch("store.bots.build_config_link_for_identifier")
    @patch("store.bots.requests.post")
    def test_config_lookup_update_force_join_guard_blocks_non_member(self, post_mock, builder_mock):
        self.enable_force_join(username="azadnet_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:config_lookup_update:safe-token", callback_id="update-cb"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(builder_mock.called)
        sent_texts = [payload["text"] for payload in self.sent_message_payloads(post_calls)]
        self.assertTrue(any("برای استفاده از ربات ابتدا عضو کانال شوید" in text for text in sent_texts))

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_test_bot")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_referral_menu_displays_invite_stats(self, post_mock):
        self.post_update(self.message("/start"))

        response = self.post_update(self.callback("user:referrals", callback_id="ref-cb"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("دعوت دوستان", payload["text"])
        self.assertIn("کد دعوت", payload["text"])
        self.assertIn("https://t.me/azadnet_test_bot?start=ref_", payload["text"])
        self.assertIn("متن آماده دعوت", payload["text"])
        self.assertIn("بسته‌های آماده دریافت", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertIn("user:referral_invite_text", callback_values)
        share_urls = [
            button["url"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
            if "url" in button
        ]
        self.assertTrue(any(url.startswith("https://t.me/share/url?") for url in share_urls))

    @patch.dict("os.environ", {"TELEGRAM_BOT_USERNAME": ""})
    @override_settings(TELEGRAM_BOT_USERNAME="")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_referral_menu_shows_missing_bot_username_message(self, post_mock):
        self.post_update(self.message("/start"))

        response = self.post_update(self.callback("user:referrals", callback_id="ref-missing-cb"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("نام کاربری ربات تنظیم نشده است.", payload["text"])
        self.assertNotIn("https://t.me/?start=ref_", payload["text"])

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_test_bot")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_referral_invite_text_callback_sends_prepared_text(self, post_mock):
        self.post_update(self.message("/start"))

        response = self.post_update(self.callback("user:referral_invite_text", callback_id="ref-text-cb"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("من از این ربات VPN گرفتم", payload["text"])
        self.assertIn("https://t.me/azadnet_test_bot?start=ref_", payload["text"])
        self.assertNotIn("parse_mode", payload)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_profile_accepts_telegram_contact_phone(self, post_mock):
        self.post_update(self.message("/start"))
        self.post_update(self.callback("user:profile_phone", callback_id="profile-phone"))

        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.PROFILE_WAIT_PHONE)

        response = self.post_update(self.contact_message("+989121234567", message_id=2))

        self.assertEqual(response.status_code, 200)
        bot_user.refresh_from_db()
        bot_user.customer.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        self.assertEqual(bot_user.customer.phone_number, "09121234567")
        sent_texts = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "42" and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("شماره موبایل در پروفایل شما ذخیره شد" in text for text in sent_texts))
        self.assertTrue(any("پروفایل شما" in text and "09121234567" in text for text in sent_texts))

    @patch("store.bots.requests.post")
    def test_user_callback_deletes_previous_inline_message_before_next_prompt(self, post_mock):
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            return DummyBotResponse()

        post_mock.side_effect = post_side_effect

        response = self.post_update(self.callback("user:buy", message_id=77))

        self.assertEqual(response.status_code, 200)
        methods = [call["url"].rsplit("/", 1)[-1] for call in post_calls]
        self.assertIn("answerCallbackQuery", methods)
        self.assertIn("deleteMessage", methods)
        self.assertIn("sendMessage", methods)
        delete_index = methods.index("deleteMessage")
        next_prompt_index = next(
            index
            for index, call in enumerate(post_calls)
            if call["url"].endswith("/sendMessage") and "پلن‌های فعال" in call.get("json", {}).get("text", "")
        )
        self.assertLess(delete_index, next_prompt_index)
        delete_payload = post_calls[delete_index]["json"]
        self.assertEqual(delete_payload["chat_id"], "42")
        self.assertEqual(delete_payload["message_id"], 77)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_purchase_flow_asks_for_quantity_after_plan(self, post_mock):
        self.post_update(self.message("/start"))
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))

        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_QUANTITY)
        quantity_prompt = post_mock.call_args.kwargs["json"]
        self.assertIn("تعداد کانفیگ", quantity_prompt["text"])
        callback_values = [
            button["callback_data"]
            for row in quantity_prompt["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:buyqty:3", callback_values)

        self.post_update(self.callback("user:buyqty:3", callback_id="qty-cb"))

        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_NAME)
        self.assertEqual(bot_user.state_data["quantity"], 3)
        payment_prompt = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("تعداد کانفیگ: ۳", payment_prompt)
        self.assertIn("مبلغ نهایی: ۳۰۰,۰۰۰ تومان", payment_prompt)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("12121212-1212-4212-8212-121212121212"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_purchase_flow_applies_discount_code_before_receipt(self, _get_mock, xui_mock):
        DiscountCode.objects.create(
            code="SAVE10",
            discount_type=DiscountCode.DiscountType.FIXED,
            value=10000,
        )
        post_calls = []

        def post_side_effect(url, json=None, data=None, **kwargs):
            post_calls.append({"url": url, "json": json, "data": data, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.callback("user:discount:start", callback_id="discount-cb"))
            self.post_update(self.message("save10", message_id=2))
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            self.post_update(self.message("Alice Buyer", message_id=3))
            response = self.post_update(
                {
                    "message": {
                        "message_id": 4,
                        "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                        "chat": {"id": 42, "type": "private"},
                        "photo": [{"file_id": "receipt-file", "file_unique_id": "receipt"}],
                    }
                }
            )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.discount_code_text, "SAVE10")
        self.assertEqual(order.discount_amount, 10000)
        self.assertEqual(order.amount, 90000)
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_buyer_[0-9a-f]{8}$")
        user_texts = [
            call.get("json", {}).get("text", "")
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json", {}).get("chat_id") == "42"
        ]
        self.assertTrue(any("کد تخفیف SAVE10 اعمال شد" in text for text in user_texts))
        self.assertTrue(any("مبلغ نهایی: ۹۰,۰۰۰ تومان" in text for text in user_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_purchase_flow_can_skip_discount_to_payment(self, post_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        response = self.post_update(self.callback("user:discount:skip", callback_id="skip-discount"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_NAME)
        self.assertEqual(bot_user.state_data["step"], "payment")
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("پرداخت کارت به کارت", payload["text"])
        self.assertIn("مبلغ: ۱۰۰,۰۰۰ تومان", payload["text"])

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_purchase_rejects_non_image_receipt_file(self, post_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        self.post_update(self.message("Alice Buyer", message_id=2))
        response = self.post_update(
            {
                "message": {
                    "message_id": 3,
                    "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                    "chat": {"id": 42, "type": "private"},
                    "document": {
                        "file_id": "receipt-text",
                        "file_unique_id": "receipt-text",
                        "file_name": "receipt.txt",
                        "mime_type": "text/plain",
                    },
                }
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertIn("فایل رسید باید تصویر", post_mock.call_args.kwargs["json"]["text"])

    @patch("store.bots.sync_vpn_client_stats")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_services_menu_lists_vpn_clients_and_referral_reward_button(self, post_mock, stats_mock):
        customer = Customer.objects.create(display_name="Alice")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        invited = Customer.objects.create(display_name="Invited", referred_by=customer)
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_1gb",
            xui_email="alice_1gb",
            uuid="abababab-abab-4aba-8aba-abababababab",
            sub_id="sub",
            sub_link="https://example.com/sub/alice",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            used_traffic_bytes=128 * 1024 * 1024,
            duration_days=30,
        )
        ReferralRewardLedger.objects.create(
            inviter=customer,
            invited=invited,
            order=order,
            reward_gb=Decimal("2.000"),
            status=ReferralRewardLedger.Status.AVAILABLE,
            available_at=timezone.now(),
        )
        stats_mock.return_value = {
            "panel_available": True,
            "total_traffic_bytes": 1024 ** 3,
            "used_traffic_bytes": 128 * 1024 * 1024,
            "remaining_traffic_bytes": (1024 ** 3) - (128 * 1024 * 1024),
            "expiry_at": timezone.now() + timedelta(days=20),
        }

        response = self.post_update(self.callback("user:subs"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("حجم کل", payload["text"])
        self.assertIn("حجم مصرف‌شده", payload["text"])
        self.assertIn("لینک کانفیگ: https://example.com/sub/alice", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:referral_redeem:{vpn_client.public_id}", callback_values)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_help_callback_shows_persian_help(self, post_mock):
        response = self.post_update(self.callback("user:help"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("راهنما", payload["text"])
        self.assertIn("خرید", payload["text"])
        self.assertIn("تمدید", payload["text"])

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_support_flow_creates_ticket_from_bot(self, post_mock):
        self.post_update(self.callback("user:support"))
        category_payload = post_mock.call_args.kwargs["json"]
        category_callbacks = [
            button["callback_data"]
            for row in category_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:support_cat:payment", category_callbacks)

        self.post_update(self.callback("user:support_cat:payment", callback_id="support-cat"))
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, "support_wait_message")

        response = self.post_update(self.message("رسیدم بررسی نشده است.", message_id=2))

        self.assertEqual(response.status_code, 200)
        conversation = SupportConversation.objects.get()
        self.assertEqual(conversation.subject, "مشکل پرداخت")
        self.assertEqual(conversation.status, SupportConversation.Status.WAITING_ADMIN)
        self.assertEqual(SupportMessage.objects.get(conversation=conversation).body, "رسیدم بررسی نشده است.")
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        user_messages = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "42" and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("شماره تیکت" in text for text in user_messages))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_menu_is_visible_only_to_admin_and_lists_pending_orders(self, post_mock):
        self.post_update(self.message("/start"))
        user_payload = post_mock.call_args.kwargs["json"]
        user_callbacks = [
            button["callback_data"]
            for row in user_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertNotIn("admin:orders:pending", user_callbacks)

        self.post_update(self.message("/start", user_id=999, username="admin", first_name="Admin"))
        admin_payload = post_mock.call_args.kwargs["json"]
        admin_callbacks = [
            button["callback_data"]
            for row in admin_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("admin:orders:pending", admin_callbacks)
        self.assertIn("admin:sales_report", admin_callbacks)

        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            sender_card_name="Alice Buyer",
        )

        response = self.post_update(
            self.callback("admin:orders:pending", user_id=999, username="admin", first_name="Admin")
        )

        self.assertEqual(response.status_code, 200)
        pending_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("سفارش‌های pending", pending_payload["text"])
        self.assertIn(order.order_tracking_code, pending_payload["text"])
        pending_callbacks = [
            button["callback_data"]
            for row in pending_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"approve:{order.order_tracking_code}", pending_callbacks)
        self.assertIn(f"reject:{order.order_tracking_code}", pending_callbacks)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_order_detail_callback_sends_order_details(self, post_mock):
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            sender_card_name="Alice Buyer",
        )

        response = self.post_update(
            self.callback(
                f"order:detail:{order.order_tracking_code}",
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("جزئیات سفارش", payload["text"])
        self.assertIn(order.order_tracking_code, payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"approve:{order.order_tracking_code}", callback_values)
        self.assertIn(f"reject:{order.order_tracking_code}", callback_values)
        self.assertIn(f"order:detail:{order.order_tracking_code}", callback_values)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_reject_callback_rejects_order_after_reason(self, post_mock):
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            sender_card_name="Alice Buyer",
        )

        callback_response = self.post_update(
            self.callback(
                f"reject:{order.order_tracking_code}",
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )
        self.assertEqual(callback_response.status_code, 200)
        pending = BotPendingAction.objects.get(order=order)
        self.assertEqual(pending.action, BotPendingAction.Action.REJECT_ORDER)

        reason_response = self.post_update(
            self.message(
                "رد شده توسط ادمین",
                message_id=11,
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )

        self.assertEqual(reason_response.status_code, 200)
        order.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REJECTED)
        self.assertEqual(order.rejection_reason, "رد شده توسط ادمین")
        self.assertEqual(pending.status, BotPendingAction.Status.COMPLETED)
        sent_texts = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "999" and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("Order rejected" in text or "رد شده" in text for text in sent_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_operator_based_bot_flow_selects_operator_before_filtered_plans(self, post_mock):
        self.store.sales_mode = Store.SalesMode.OPERATOR_BASED
        self.store.save(update_fields=["sales_mode", "updated_at"])
        operator_a = Operator.objects.create(store=self.store, name="همراه اول", slug="telegram-mci")
        operator_b = Operator.objects.create(store=self.store, name="ایرانسل", slug="telegram-irancell")
        self.plan.operators.add(operator_a)
        other_plan = Plan.objects.create(
            store=self.store,
            name="Irancell Bot Plan",
            slug="telegram-irancell-plan",
            volume_gb=Decimal("2.000"),
            duration_days=30,
            price=180000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        other_plan.operators.add(operator_b)

        self.post_update(self.callback("user:buy"))

        operator_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("اپراتور اینترنتت را انتخاب کن", operator_payload["text"])
        operator_callbacks = [
            button["callback_data"]
            for row in operator_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:buyop:{operator_a.pk}", operator_callbacks)
        self.assertFalse(any(value.startswith("user:buyplan:") for value in operator_callbacks))

        self.post_update(self.callback(f"user:buyop:{operator_a.pk}", callback_id="operator-cb"))

        plan_payload = post_mock.call_args.kwargs["json"]
        self.assertIn(self.plan.name, plan_payload["text"])
        self.assertNotIn(other_plan.name, plan_payload["text"])
        plan_callbacks = [
            button["callback_data"]
            for row in plan_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        selected_plan_callback = f"user:buyplan:{self.plan.pk}:op:{operator_a.pk}"
        self.assertIn(selected_plan_callback, plan_callbacks)

        self.post_update(self.callback(selected_plan_callback, callback_id="plan-cb"))

        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_QUANTITY)
        self.assertEqual(bot_user.state_data["operator_id"], operator_a.pk)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_custom_volume_purchase_asks_volume_then_quantity(self, post_mock):
        self.store.custom_volume_price_per_gb = Decimal("100000")
        self.store.save(update_fields=["custom_volume_price_per_gb", "updated_at"])

        self.post_update(self.callback("user:buy"))
        payload = post_mock.call_args.kwargs["json"]
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:buycustom", callback_values)

        self.post_update(self.callback("user:buycustom", callback_id="custom-cb"))
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_CUSTOM_VOLUME)
        self.assertIn("حجم دلخواه", post_mock.call_args.kwargs["json"]["text"])

        self.post_update(self.message("7", message_id=2))

        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_QUANTITY)
        custom_plan = Plan.objects.get(pk=bot_user.state_data["plan_id"])
        self.assertTrue(custom_plan.is_custom_volume)
        self.assertEqual(custom_plan.volume_gb, Decimal("7.000"))
        self.assertEqual(custom_plan.duration_days, 30)
        quantity_prompt = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("حجم انتخابی: ۷ گیگابایت", quantity_prompt)
        self.assertIn("قیمت هر کانفیگ: ۷۰۰,۰۰۰ تومان", quantity_prompt)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_purchase_requires_receipt_image(self, post_mock):
        self.post_update(self.message("/start"))
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        self.post_update(self.message("Alice Buyer", message_id=2))
        response = self.post_update(self.message("/skip", message_id=3))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertFalse(Order.objects.exists())
        last_message = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("عکس رسید", last_message)
        self.assertNotIn("<", last_message)
        self.assertNotIn("۴ رقم", last_message)
        self.assertNotIn("ساعت پرداخت", last_message)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("33333333-3333-4333-8333-333333333333"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_bale_purchase_flow_is_enabled_for_non_admin_users(self, _get, _xui):
        self.bot_config.provider = BotConfiguration.Provider.BALE
        self.bot_config.save(update_fields=["provider", "updated_at"])
        self.url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])

        def post_side_effect(url, json=None, **kwargs):
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            response = self.post_update(
                {
                    "message": {
                        "message_id": 3,
                        "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                        "chat": {"id": 42, "type": "private"},
                        "photo": [{"file_id": "receipt-file", "file_unique_id": "receipt"}],
                    }
                }
            )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.metadata["source"], "bale_bot")
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertEqual(order.sender_card_name, "Alice Buyer")
        self.assertEqual(order.sender_card_last4, "")
        self.assertEqual(order.bank_tracking_code, "")
        self.assertIsNotNone(order.payment_time)
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
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 3,
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
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(order.customer, bot_user.customer)
        vpn_client = VPNClient.objects.get(order=order)
        from .bot_targets import get_vpn_client_telegram_targets

        targets = get_vpn_client_telegram_targets(vpn_client, store=self.store)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].chat_id, "42")
        self.assertTrue(order.payment_receipt_image.name)
        self.assertEqual(order.sender_card_name, "Alice Buyer")
        self.assertEqual(order.sender_card_last4, "")
        self.assertEqual(order.bank_tracking_code, "")
        self.assertEqual(order.metadata["receipt"]["file_id"], "large-file")
        self.assertEqual(order.metadata["receipt"]["file_unique_id"], "large")
        self.assertEqual(order.metadata["receipt"]["file_path"], "photos/receipt.jpg")

        send_photo_calls = [call for call in post_calls if call["url"].endswith("/sendPhoto")]
        self.assertEqual(len(send_photo_calls), 1)
        photo_data = send_photo_calls[0]["data"]
        self.assertIn("سفارش جدید VPN", photo_data["caption"])
        self.assertIn(order.order_tracking_code, photo_data["caption"])
        reply_markup = json.loads(photo_data["reply_markup"])
        callback_values = [
            button["callback_data"]
            for row in reply_markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"approve:{order.order_tracking_code}", callback_values)
        self.assertIn(f"reject:{order.order_tracking_code}", callback_values)
        self.assertFalse(any(call["url"].endswith("/forwardMessage") for call in post_calls))

        user_messages = [
            call.get("json", {})
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json", {}).get("chat_id") == "42"
        ]
        self.assertTrue(any("پرداخت کارت به کارت" in item.get("text", "") for item in user_messages))
        self.assertTrue(any("سفارش شما ثبت شد" in item.get("text", "") for item in user_messages))
        for item in user_messages:
            self.assertNotIn("<", item.get("text", ""))
            self.assertNotIn("parse_mode", item)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("66666666-6666-4666-8666-666666666666"))
    def test_receipt_download_failure_still_creates_order_and_forwards_original_message(self, _xui):
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 3,
                            "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                            "chat": {"id": 42, "type": "private"},
                            "photo": [{"file_id": "missing-path", "file_unique_id": "missing"}],
                        }
                    }
                )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertFalse(order.payment_receipt_image)
        self.assertEqual(order.metadata["receipt"]["download_error"], "file_download_unavailable")
        self.assertTrue(any(call["url"].endswith("/forwardMessage") for call in post_calls))
        user_texts = [
            call.get("json", {}).get("text", "")
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json", {}).get("chat_id") == "42"
        ]
        self.assertTrue(any("سفارش شما ثبت شد" in text for text in user_texts))
        self.assertFalse(any("دانلود عکس رسید" in text for text in user_texts))

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("77777777-7777-4777-8777-777777777777"))
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_user_callbacks_use_customer_flow_and_directly_activate_without_receipt(self, post_mock, _xui, _enable):
        self.post_update(
            self.callback(
                f"user:buyplan:{self.plan.pk}",
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )

        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="999")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_QUANTITY)

        self.post_update(
            self.callback(
                "user:buyqty:1",
                callback_id="admin-qty-cb",
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )

        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_NAME)

        response = self.post_update(
            self.message(
                "Admin Config",
                message_id=2,
                user_id=999,
                username="admin",
                first_name="Admin",
            )
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_name, "Admin Config")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertTrue(order.is_paid)
        self.assertEqual(order.metadata["source"], "telegram_admin_bot_direct")
        self.assertTrue(order.metadata["suppress_new_order_notification"])
        vpn_client = VPNClient.objects.get(order=order)
        self.assertEqual(vpn_client.status, VPNClient.Status.ACTIVE)
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        sent_texts = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "999" and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("اشتراک شما فعال شد" in text for text in sent_texts))
        self.assertFalse(any("Malformed callback data" in text for text in sent_texts))

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("88888888-8888-4888-8888-888888888888"))
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_additional_admin_can_directly_activate_without_receipt(self, post_mock, _xui, _enable):
        self.bot_config.additional_admin_user_ids = "777"
        self.bot_config.save(update_fields=["additional_admin_user_ids", "updated_at"])

        self.post_update(
            self.callback(
                f"user:buyplan:{self.plan.pk}",
                user_id=777,
                username="helper",
                first_name="Helper",
            )
        )
        self.post_update(
            self.callback(
                "user:buyqty:1",
                callback_id="helper-qty-cb",
                user_id=777,
                username="helper",
                first_name="Helper",
            )
        )
        response = self.post_update(
            self.message(
                "Helper Config",
                message_id=2,
                user_id=777,
                username="helper",
                first_name="Helper",
            )
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.sender_card_name, "Helper Config")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertTrue(order.metadata["admin_direct_purchase"])
        self.assertEqual(order.metadata["source"], "telegram_admin_bot_direct")
        self.assertFalse(order.payment_receipt_image)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="777")
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        helper_texts = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "777" and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("اشتراک شما فعال شد" in text for text in helper_texts))

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("99999999-9999-4999-8999-999999999999"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_receipt_notification_is_sent_to_all_bot_admins(self, _get, _xui):
        self.bot_config.additional_admin_user_ids = "777"
        self.bot_config.save(update_fields=["additional_admin_user_ids", "updated_at"])
        post_calls = []
        message_id = 100

        def post_side_effect(url, json=None, data=None, **kwargs):
            nonlocal message_id
            post_calls.append({"url": url, "json": json, "data": data, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            if url.endswith("/sendPhoto") or url.endswith("/sendMessage"):
                message_id += 1
                return DummyBotResponse({"ok": True, "result": {"message_id": message_id}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 3,
                            "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                            "chat": {"id": 42, "type": "private"},
                            "photo": [{"file_id": "receipt-file", "file_unique_id": "receipt"}],
                        }
                    }
                )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        send_photo_chats = [
            call["data"]["chat_id"]
            for call in post_calls
            if call["url"].endswith("/sendPhoto")
        ]
        self.assertCountEqual(send_photo_chats, ["999", "777"])
        self.assertEqual(BotAdminOrderMessage.objects.filter(order=order).count(), 2)
        self.assertCountEqual(
            list(BotAdminOrderMessage.objects.filter(order=order).values_list("admin_user_id", flat=True)),
            ["999", "777"],
        )

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    def test_approval_by_one_admin_updates_all_admin_order_messages(self, _xui, _enable):
        self.bot_config.additional_admin_user_ids = "777"
        self.bot_config.save(update_fields=["additional_admin_user_ids", "updated_at"])
        result = create_manual_payment_order(
            store=self.store,
            customer=None,
            plan=Plan.objects.get(pk=self.plan.pk),
            inbound=self.inbound,
            sender_card_name="Alice Buyer",
            sender_card_last4="",
            payment_time=time(14, 35),
            metadata={"source": "test"},
        )
        self.assertTrue(result.success)
        order = result.order
        BotAdminOrderMessage.objects.create(
            bot_config=self.bot_config,
            order=order,
            admin_user_id="999",
            chat_id="999",
            message_id="201",
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )
        BotAdminOrderMessage.objects.create(
            bot_config=self.bot_config,
            order=order,
            admin_user_id="777",
            chat_id="777",
            message_id="202",
            message_kind=BotAdminOrderMessage.MessageKind.TEXT,
        )
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            response = self.post_update(
                self.callback(
                    f"approve:{order.order_tracking_code}",
                    message_id=201,
                    user_id=999,
                    username="admin",
                    first_name="Admin",
                )
            )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        edit_calls = [call for call in post_calls if call["url"].endswith("/editMessageText")]
        edited_chat_ids = {str(call["json"]["chat_id"]) for call in edit_calls}
        self.assertIn("999", edited_chat_ids)
        self.assertIn("777", edited_chat_ids)
        for call in edit_calls:
            if str(call["json"]["chat_id"]) in {"999", "777"}:
                self.assertEqual(call["json"]["reply_markup"], {"inline_keyboard": []})
                self.assertIn("Order approved", call["json"]["text"])

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("44444444-4444-4444-8444-444444444444"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_bale_approval_sends_config_to_same_bale_bot(self, _get, _xui, _enable_client):
        self.bot_config.provider = BotConfiguration.Provider.BALE
        self.bot_config.save(update_fields=["provider", "updated_at"])
        self.url = reverse("bot_webhook", args=[self.bot_config.provider, self.bot_config.webhook_secret])
        post_calls = []

        def post_side_effect(url, json=None, **kwargs):
            post_calls.append({"url": url, "json": json, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.message("Alice Buyer", message_id=2))
            with self.captureOnCommitCallbacks(execute=True):
                self.post_update(
                    {
                        "message": {
                            "message_id": 3,
                            "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                            "chat": {"id": 42, "type": "private"},
                            "photo": [{"file_id": "receipt-file", "file_unique_id": "receipt"}],
                        }
                    }
                )

            order = Order.objects.get()
            telegram_config = BotConfiguration.objects.create(
                store=self.store,
                provider=BotConfiguration.Provider.TELEGRAM,
                name="Telegram customer mirror",
                bot_token="telegram-token-2",
                admin_user_id="888",
                is_active=True,
                notify_order_updates=False,
            )
            BotUser.objects.create(
                bot_config=telegram_config,
                customer=order.customer,
                provider_user_id="42",
                chat_id="42",
                username="alice",
                display_name="Telegram Alice",
            )

            response = self.post_update(
                {
                    "callback_query": {
                        "id": "approve-cb",
                        "from": {"id": 999, "username": "admin"},
                        "message": {"message_id": 20, "chat": {"id": 999, "type": "private"}},
                        "data": f"approve:{order.order_tracking_code}",
                    }
                }
            )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        customer_messages = [
            call
            for call in post_calls
            if call["url"].startswith("https://tapi.bale.ai")
            and call["url"].endswith("/sendMessage")
            and call.get("json", {}).get("chat_id") == "42"
            and "اشتراک شما فعال شد" in call.get("json", {}).get("text", "")
        ]
        self.assertEqual(len(customer_messages), 1)
        self.assertIn("vless://example", customer_messages[0]["json"]["text"])
        self.assertFalse(any(call["url"].startswith("https://api.telegram.org") for call in post_calls))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_can_list_and_open_bot_orders(self, post_mock):
        customer = Customer.objects.create(display_name="Alice")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )

        response = self.post_update(self.message("/orders"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("سفارش‌های من", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:order:{order.order_tracking_code}", callback_values)

        detail_response = self.post_update(self.callback(f"user:order:{order.order_tracking_code}"))

        self.assertEqual(detail_response.status_code, 200)
        detail_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("جزئیات سفارش", detail_payload["text"])
        self.assertIn(order.order_tracking_code, detail_payload["text"])
        detail_callback_values = [
            button["callback_data"]
            for row in detail_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:order_cancel:{order.order_tracking_code}", detail_callback_values)

    @patch("store.bots.sync_vpn_client_stats")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_can_refresh_config_from_xui_in_bot(self, post_mock, stats_mock):
        customer = Customer.objects.create(display_name="Alice")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="alice_1gb",
            uuid="55555555-5555-4555-8555-555555555555",
            sub_link="https://old.example/sub/old",
            direct_link="vless://old",
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_1gb",
            xui_email="alice_1gb",
            uuid=order.uuid,
            sub_id="old",
            sub_link=order.sub_link,
            direct_link=order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )
        stats_mock.return_value = {
            "panel_available": True,
            "total_traffic_bytes": 1024 ** 3,
            "used_traffic_bytes": 256 * 1024 * 1024,
            "remaining_traffic_bytes": (1024 ** 3) - (256 * 1024 * 1024),
            "expiry_at": timezone.now() + timedelta(days=20),
        }

        def refresh_side_effect(client):
            client.sub_link = "https://new.example/sub/fresh"
            client.direct_link = "vless://fresh-config"
            client.save(update_fields=["sub_link", "direct_link", "updated_at"])
            client.order.sub_link = client.sub_link
            client.order.direct_link = client.direct_link
            client.order.save(update_fields=["sub_link", "direct_link", "updated_at"])
            return {
                "sub_link": client.sub_link,
                "direct_link": client.direct_link,
            }

        with patch("store.bots.refresh_vpn_client_links", side_effect=refresh_side_effect) as refresh_mock:
            response = self.post_update(self.callback(f"user:client_refresh:{vpn_client.public_id}"))

        self.assertEqual(response.status_code, 200)
        refresh_mock.assert_called_once()
        stats_mock.assert_called_once()
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("کانفیگ بروزرسانی شد", payload["text"])
        self.assertIn("https://new.example/sub/fresh", payload["text"])
        self.assertIn("vless://fresh-config", payload["text"])

    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_user_can_create_renewal_order_from_bot(self, _get_mock):
        customer = Customer.objects.create(display_name="Alice", username="alice")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="alice_1gb",
            uuid="99999999-9999-4999-8999-999999999999",
            sub_link="https://example.com/sub/old",
            direct_link="vless://old",
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_1gb",
            xui_email="alice_1gb",
            uuid=order.uuid,
            sub_id="old",
            sub_link=order.sub_link,
            direct_link=order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )
        post_calls = []

        def post_side_effect(url, json=None, data=None, **kwargs):
            post_calls.append({"url": url, "json": json, "data": data, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/renewal.jpg"}})
            if url.endswith("/sendPhoto") or url.endswith("/sendMessage"):
                return DummyBotResponse({"ok": True, "result": {"message_id": 321}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.callback(f"user:client_renew:{vpn_client.public_id}"))
            bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
            self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_NAME)
            self.assertEqual(bot_user.state_data["flow"], "renewal")

            self.post_update(self.message("Alice Buyer", message_id=2))
            bot_user.refresh_from_db()
            self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)

            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 3,
                            "from": {"id": 42, "username": "alice", "first_name": "Alice"},
                            "chat": {"id": 42, "type": "private"},
                            "photo": [{"file_id": "renewal-file", "file_unique_id": "renewal"}],
                        }
                    }
                )

        self.assertEqual(response.status_code, 200)
        renewal = Order.objects.exclude(pk=order.pk).get()
        self.assertEqual(renewal.customer, customer)
        self.assertEqual(renewal.status, Order.Status.PENDING_VERIFICATION)
        self.assertEqual(renewal.sender_card_name, "Alice Buyer")
        self.assertEqual(renewal.sender_card_last4, "")
        self.assertEqual(renewal.metadata["source"], "telegram_bot_renewal")
        self.assertEqual(renewal.metadata["renewal_client_pk"], vpn_client.pk)
        self.assertNotIn("suppress_new_order_notification", renewal.metadata)
        self.assertTrue(renewal.payment_receipt_image.name.endswith(".jpg"))
        self.assertEqual(VPNClient.objects.count(), 1)
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)
        self.assertTrue(any(call["url"].endswith("/sendPhoto") for call in post_calls))
        user_messages = [
            call.get("json", {}).get("text", "")
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json", {}).get("chat_id") == "42"
        ]
        self.assertTrue(any("درخواست تمدید شما ثبت شد" in text for text in user_messages))


class RenewalReminderServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="AzadNet",
            english_name="AzadNet",
            card_number="1234567890123456",
            card_owner="Alice",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10GB",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100000,
            device_limit=2,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn.example.com",
            port="443",
            config_params="security=reality",
        )
        self.customer = Customer.objects.create(display_name="Alice", username="alice")
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="123:token",
            admin_user_id="999",
        )
        self.bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=self.customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        self.vpn_client = self.make_client(expires_at=timezone.now() + timedelta(days=3))

    def make_client(self, *, customer=None, expires_at=None, status=VPNClient.Status.ACTIVE, used_gb=0, total_gb=10):
        customer = customer or self.customer
        client_index = VPNClient.objects.count() + 1
        uuid = f"11111111-1111-4111-8111-{client_index:012d}"
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username=f"alice_{client_index}",
            uuid=uuid,
        )
        total = int(Decimal(str(total_gb)) * Decimal(1024 ** 3))
        used = int(Decimal(str(used_gb)) * Decimal(1024 ** 3))
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username=f"alice_{client_index}",
            xui_email=f"alice_{client_index}",
            uuid=uuid,
            status=status,
            traffic_limit_bytes=total,
            used_traffic_bytes=used,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
            expires_at=expires_at or timezone.now() + timedelta(days=30),
            last_synced_at=timezone.now(),
        )

    def xui_stats(self, *, client=None, total_gb=10, remaining_gb=9, expiry_at=None):
        client = client or self.vpn_client
        total = int(Decimal(str(total_gb)) * Decimal(1024 ** 3))
        remaining = int(Decimal(str(remaining_gb)) * Decimal(1024 ** 3))
        used = max(total - remaining, 0)
        return {
            "uuid": client.uuid,
            "email": client.xui_email,
            "total_traffic_bytes": total,
            "used_upload_bytes": 0,
            "used_download_bytes": used,
            "used_traffic_bytes": used,
            "remaining_traffic_bytes": remaining,
            "expiry_at": expiry_at or client.expires_at,
            "is_enabled": True,
            "panel_available": True,
            "raw": {
                "traffic": {"up": 0, "down": used, "total": total},
                "client": {"id": client.uuid, "email": client.xui_email, "totalGB": total},
            },
        }

    def live_usage(self, *, client=None, total_gb=10, remaining_gb=9, expiry_at=None, usage_known=True):
        stats = self.xui_stats(client=client, total_gb=total_gb, remaining_gb=remaining_gb, expiry_at=expiry_at)
        stats["usage_known"] = usage_known
        stats["source"] = "xui" if usage_known else "unknown"
        return stats

    def decisions_for(self, client, *, now=None, live_usage=None):
        from .renewal_reminder_services import calculate_client_reminder_status, get_reminder_settings

        return [
            (decision.reminder_type, decision.trigger_key)
            for decision in calculate_client_reminder_status(
                client,
                live_usage or self.live_usage(client=client),
                settings=get_reminder_settings(self.store),
                now=now or timezone.now(),
            )
        ]

    def test_store_reminder_settings_validation(self):
        store = Store(
            name="Bad",
            english_name="Bad",
            card_number="1234567890123456",
            card_owner="Alice",
            reminder_days_before_expiry=["soon"],
            low_traffic_percent_threshold=101,
            low_traffic_gb_threshold=Decimal("0"),
            reminder_cooldown_hours=0,
        )
        with self.assertRaises(ValidationError) as ctx:
            store.full_clean()

        self.assertIn("reminder_days_before_expiry", ctx.exception.error_dict)
        self.assertIn("low_traffic_percent_threshold", ctx.exception.error_dict)
        self.assertIn("low_traffic_gb_threshold", ctx.exception.error_dict)
        self.assertIn("reminder_cooldown_hours", ctx.exception.error_dict)

    def test_selects_expiry_reminder_three_days_before(self):
        now = timezone.now()
        self.vpn_client.expires_at = now + timedelta(days=3)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.EXPIRY_BEFORE, "before_3d"),
            self.decisions_for(self.vpn_client, now=now, live_usage=self.live_usage(expiry_at=self.vpn_client.expires_at)),
        )

    def test_selects_expiry_reminder_one_day_before(self):
        now = timezone.now()
        self.vpn_client.expires_at = now + timedelta(days=1)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.EXPIRY_BEFORE, "before_1d"),
            self.decisions_for(self.vpn_client, now=now, live_usage=self.live_usage(expiry_at=self.vpn_client.expires_at)),
        )

    def test_selects_expiry_reminder_on_expiry_day(self):
        now = timezone.now()
        self.vpn_client.expires_at = now + timedelta(hours=2)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.EXPIRY_TODAY, "today"),
            self.decisions_for(self.vpn_client, now=now, live_usage=self.live_usage(expiry_at=self.vpn_client.expires_at)),
        )

    def test_selects_expiry_reminder_after_expiry(self):
        now = timezone.now()
        self.vpn_client.expires_at = now - timedelta(days=1)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.EXPIRY_AFTER, "after_1d"),
            self.decisions_for(self.vpn_client, now=now, live_usage=self.live_usage(expiry_at=self.vpn_client.expires_at)),
        )

    def test_selects_low_traffic_by_percent_threshold(self):
        self.vpn_client.expires_at = timezone.now() + timedelta(days=10)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.LOW_TRAFFIC, "low_traffic_20pct"),
            self.decisions_for(self.vpn_client, live_usage=self.live_usage(total_gb=10, remaining_gb=1)),
        )

    def test_selects_low_traffic_by_gb_threshold(self):
        self.vpn_client.expires_at = timezone.now() + timedelta(days=10)
        self.assertIn(
            (VPNClientReminderLog.ReminderType.LOW_TRAFFIC, "low_traffic_2gb"),
            self.decisions_for(self.vpn_client, live_usage=self.live_usage(total_gb=5, remaining_gb=Decimal("1.5"))),
        )

    def test_unknown_client_stats_do_not_send_low_traffic_reminder(self):
        self.vpn_client.expires_at = timezone.now() + timedelta(days=10)
        usage = self.live_usage(total_gb=10, remaining_gb=1, usage_known=False)
        usage["remaining_traffic_bytes"] = None
        self.assertEqual(self.decisions_for(self.vpn_client, live_usage=usage), [])

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_unknown_live_stats_fall_back_to_valid_local_usage(self, _send_mock, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        self.vpn_client.expires_at = timezone.now() + timedelta(days=10)
        self.vpn_client.used_traffic_bytes = int(Decimal("9") * Decimal(1024 ** 3))
        self.vpn_client.last_synced_at = timezone.now() - timedelta(hours=1)
        self.vpn_client.save(update_fields=["expires_at", "used_traffic_bytes", "last_synced_at", "updated_at"])
        sync_mock.return_value = {
            "total_traffic_bytes": int(Decimal("10") * Decimal(1024 ** 3)),
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": int(Decimal("10") * Decimal(1024 ** 3)),
            "expiry_at": self.vpn_client.expires_at,
            "is_enabled": True,
            "panel_available": True,
            "raw": {"traffic": {}, "client": {}},
        }

        summary = run_renewal_reminders(reminder_type="traffic")

        self.assertEqual(summary["sent"], 1)
        log = VPNClientReminderLog.objects.get()
        self.assertEqual(log.reminder_type, VPNClientReminderLog.ReminderType.LOW_TRAFFIC)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_duplicate_trigger_is_not_sent_twice(self, send_mock, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        first = run_renewal_reminders()
        second = run_renewal_reminders()

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(VPNClientReminderLog.objects.filter(status=VPNClientReminderLog.Status.SENT).count(), 1)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_failed_delivery_retries_after_cooldown(self, sync_mock):
        from .bots import BotDeliveryError
        from .renewal_reminder_services import run_renewal_reminders

        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        with patch("store.bots.BotClient.send_message", side_effect=BotDeliveryError("boom")) as send_mock:
            first = run_renewal_reminders()
            second = run_renewal_reminders()

        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(send_mock.call_count, 1)
        log = VPNClientReminderLog.objects.get()
        self.assertEqual(log.status, VPNClientReminderLog.Status.FAILED)
        self.assertEqual(log.error_message, "boom")

        VPNClientReminderLog.objects.filter(pk=log.pk).update(updated_at=timezone.now() - timedelta(hours=25))
        with patch("store.bots.BotClient.send_message", return_value={"ok": True}) as retry_mock:
            third = run_renewal_reminders()

        self.assertEqual(third["sent"], 1)
        self.assertEqual(retry_mock.call_count, 1)
        log.refresh_from_db()
        self.assertEqual(log.status, VPNClientReminderLog.Status.SENT)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_customer_without_telegram_id_is_skipped(self, send_mock, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        BotUser.objects.all().delete()
        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        summary = run_renewal_reminders()

        self.assertEqual(summary["skipped"], 1)
        send_mock.assert_not_called()
        log = VPNClientReminderLog.objects.get()
        self.assertEqual(log.status, VPNClientReminderLog.Status.SKIPPED)
        self.assertIn("No active Telegram", log.error_message)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_start_boundary_ignores_old_client_before_candidates(self, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        start_at = timezone.now()
        VPNClient.objects.filter(pk=self.vpn_client.pk).update(created_at=start_at - timedelta(days=1))
        self.store.renewal_reminders_start_at = start_at
        self.store.save(update_fields=["renewal_reminders_start_at", "updated_at"])

        summary = run_renewal_reminders(dry_run=True)

        self.assertEqual(summary["total_clients_seen"], 1)
        self.assertEqual(summary["ignored_before_start_at"], 1)
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["due"], 0)
        sync_mock.assert_not_called()

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_start_boundary_allows_new_client_after_start_at(self, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        start_at = timezone.now() - timedelta(minutes=1)
        VPNClient.objects.filter(pk=self.vpn_client.pk).update(created_at=start_at + timedelta(seconds=1))
        self.store.renewal_reminders_start_at = start_at
        self.store.save(update_fields=["renewal_reminders_start_at", "updated_at"])

        def stats_for_client(vpn_client, *args, **kwargs):
            return self.xui_stats(client=vpn_client, remaining_gb=9, expiry_at=vpn_client.expires_at)

        sync_mock.side_effect = stats_for_client
        summary = run_renewal_reminders(dry_run=True)

        self.assertEqual(summary["ignored_before_start_at"], 0)
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(summary["would_send"], 1)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_dry_run_without_telegram_target_is_skipped_without_log(self, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        BotUser.objects.all().delete()
        sync_mock.return_value = self.xui_stats(remaining_gb=9)

        summary = run_renewal_reminders(dry_run=True)

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["would_send"], 0)
        self.assertFalse(VPNClientReminderLog.objects.exists())

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_telegram_send_failure_does_not_fail_command(self, sync_mock):
        from .bots import BotDeliveryError

        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        out = StringIO()
        with patch("store.bots.BotClient.send_message", side_effect=BotDeliveryError("blocked")):
            call_command("send_renewal_reminders", stdout=out)

        self.assertIn("failed=1", out.getvalue())
        self.assertEqual(VPNClientReminderLog.objects.get().status, VPNClientReminderLog.Status.FAILED)

    def test_reminder_keyboard_connects_to_existing_callbacks(self):
        from .renewal_reminder_services import build_reminder_keyboard

        keyboard = build_reminder_keyboard(self.vpn_client)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:client_renew:{self.vpn_client.public_id}", callbacks)
        self.assertIn(f"user:client_usage:{self.vpn_client.public_id}", callbacks)
        self.assertIn("user:buy", callbacks)

    def test_reminder_keyboard_new_purchase_callback_uses_buy_flow(self):
        from .renewal_reminder_services import build_reminder_keyboard

        keyboard = build_reminder_keyboard(self.vpn_client)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(callbacks[-1], "user:buy")

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_dry_run_does_not_send_or_write_logs(self, send_mock, sync_mock):
        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        out = StringIO()
        call_command("send_renewal_reminders", "--dry-run", stdout=out)

        self.assertIn("would_send=1", out.getvalue())
        send_mock.assert_not_called()
        self.assertFalse(VPNClientReminderLog.objects.exists())

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_command_summary_reports_counts(self, _send_mock, sync_mock):
        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        out = StringIO()
        call_command("send_renewal_reminders", "--limit", "100", "--type", "all", stdout=out)

        output = out.getvalue()
        self.assertIn("candidates=1", output)
        self.assertIn("due=1", output)
        self.assertIn("sent=1", output)

    def test_command_summary_reports_start_boundary_counts(self):
        start_at = timezone.now()
        VPNClient.objects.filter(pk=self.vpn_client.pk).update(created_at=start_at - timedelta(days=1))
        self.store.renewal_reminders_start_at = start_at
        self.store.save(update_fields=["renewal_reminders_start_at", "updated_at"])

        out = StringIO()
        call_command("send_renewal_reminders", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("total_clients_seen=1", output)
        self.assertIn("ignored_before_start_at=1", output)
        self.assertIn("candidates=0", output)

    def test_set_renewal_reminders_start_now_command_sets_active_store(self):
        out = StringIO()
        call_command("set_renewal_reminders_start_now", stdout=out)

        self.store.refresh_from_db()
        self.assertIsNotNone(self.store.renewal_reminders_start_at)
        self.assertIn("updated_stores=1", out.getvalue())

    def test_check_integrations_reports_reminder_status(self):
        out = StringIO()
        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("Renewal reminder settings look valid.", output)
        self.assertIn("send_renewal_reminders command is available.", output)
        self.assertIn("renewal_reminders_start_at=not set", output)

    def test_check_integrations_with_start_at_does_not_warn_for_ignored_old_clients(self):
        BotUser.objects.all().delete()
        start_at = timezone.now()
        VPNClient.objects.filter(pk=self.vpn_client.pk).update(created_at=start_at - timedelta(days=1))
        self.store.renewal_reminders_start_at = start_at
        self.store.save(update_fields=["renewal_reminders_start_at", "updated_at"])

        out = StringIO()
        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("ignored_before_start_at=1", output)
        self.assertIn("candidates_after_start_at=0", output)
        self.assertIn("old reminder client(s) are ignored", output)
        self.assertNotIn("renewal_reminders_start_at را تنظیم کنید", output)

    def test_check_integrations_without_start_at_warns_about_old_targetless_clients(self):
        BotUser.objects.all().delete()

        out = StringIO()
        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("renewal_reminders_start_at=not set", output)
        self.assertIn("have no Telegram target", output)
        self.assertIn("renewal_reminders_start_at را تنظیم کنید", output)

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    def test_start_boundary_does_not_repair_old_missing_targets(self, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        BotUser.objects.all().delete()
        customer_count = Customer.objects.count()
        start_at = timezone.now()
        VPNClient.objects.filter(pk=self.vpn_client.pk).update(created_at=start_at - timedelta(days=1))
        self.store.renewal_reminders_start_at = start_at
        self.store.save(update_fields=["renewal_reminders_start_at", "updated_at"])

        summary = run_renewal_reminders(dry_run=True)

        self.assertEqual(summary["ignored_before_start_at"], 1)
        self.assertEqual(BotUser.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), customer_count)
        sync_mock.assert_not_called()

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_force_join_does_not_block_reminder_delivery(self, send_mock, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders

        self.bot_config.force_telegram_channel_join = True
        self.bot_config.telegram_required_channel_username = "azadnet"
        self.bot_config.save(update_fields=["force_telegram_channel_join", "telegram_required_channel_username", "updated_at"])
        sync_mock.return_value = self.xui_stats(remaining_gb=9)
        summary = run_renewal_reminders()

        self.assertEqual(summary["sent"], 1)
        send_mock.assert_called_once()
