import json
import base64
import random
import tempfile
import threading
from io import BytesIO, StringIO
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
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
    DailyAdminReportLog,
    DiscountCode,
    FreeTrialRequest,
    Inbound,
    LegacyWizWizImportJob,
    LegacyWizWizImportMessageBatch,
    LegacyWizWizImportMessageRecipient,
    LegacyWizWizImportRow,
    Order,
    Operator,
    Panel,
    PanelClientUsageSnapshot,
    PanelDailyUsage,
    PanelHealthCheckLog,
    PanelHealthStatus,
    PanelUsageSnapshot,
    Plan,
    PlanInboundRoute,
    Referral,
    ReferralRewardLedger,
    RevenueOfferLog,
    Store,
    SupportConversation,
    SupportMessage,
    VPNClient,
    VPNClientActionLog,
    VPNClientReminderLog,
    WebTelegramLinkToken,
)
from .broadcast_services import (
    create_campaign_recipients,
    get_customers_for_audience,
    resolve_campaign_recipients,
    send_campaign,
)
from .legacy_wizwiz_import_services import (
    analyze_wizwiz_import_job,
    apply_wizwiz_import_job,
    create_legacy_import_message_batch,
    normalize_wizwiz_user_row,
    parse_mysql_insert_values,
    parse_wizwiz_users_from_sql_file,
    preview_legacy_import_message_batch,
    send_legacy_import_message_batch,
    wizwiz_simple_restore,
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
from .order_services import create_manual_payment_order, get_store_plans, select_inbound_for_plan
from .plan_route_services import (
    BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
    BULK_ROUTE_STRATEGY_SKIP_EXISTING,
    BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    apply_bulk_plan_routes,
    get_valid_sales_inbounds,
    preview_bulk_plan_routes,
)
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
    proxy_url = "http://proxy.example:7880"
    expected_proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    def test_bot_event_log_redacts_config_links(self):
        from .bots import log_event

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        config = BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )
        vless_link = "vless://11111111-1111-4111-8111-111111111111@example.com:443?type=tcp#private"
        ss_link = "ss://secret@example.com:443#private"
        sub_link = "https://example.com/sub/private-sub-token?secret=1"

        event = log_event(
            config,
            event_type=BotEventLog.EventType.WEBHOOK,
            status=BotEventLog.Status.RECEIVED,
            message=f"User sent {vless_link} and {sub_link}",
            raw_payload={"message": {"text": vless_link}, "links": [ss_link, sub_link]},
        )

        self.assertIn("<config-link-redacted>", event.message)
        self.assertNotIn("vless://", event.message)
        self.assertNotIn("/sub/private-sub-token", event.message)
        payload_text = json.dumps(event.raw_payload, ensure_ascii=False)
        self.assertNotIn("vless://", payload_text)
        self.assertNotIn("ss://", payload_text)
        self.assertNotIn("/sub/private-sub-token", payload_text)
        self.assertIn("<config-link-redacted>", payload_text)

    def test_bot_redaction_sanitizes_keys_nested_values_and_link_tokens(self):
        from .bots import (
            sanitize_bot_event_log_value,
            sanitize_bot_text_for_logging,
            sanitize_bot_update_for_logging,
        )

        vless_link = "vless://11111111-1111-4111-8111-111111111111@example.com:443?type=tcp#private"
        vmess_link = "vmess://encoded-private-payload"
        trojan_link = "trojan://secret@example.com:443#private"
        sub_link = "https://example.com/sub/private-sub-token?secret=1"
        start_link = "https://t.me/vpn_store_bot?start=link_SUPER_SECRET_TOKEN"
        email = "alice.private@example.com"

        event_payload = sanitize_bot_event_log_value(
            {
                vless_link: [
                    {
                        "text": vmess_link,
                        "caption": f"{trojan_link} {sub_link} {start_link}",
                        "metadata": {
                            "email": email,
                            "proxy_password": "proxy-pass-secret",
                            "api_key": "api-key-secret",
                            "nested": [{"session_key": "session-key-secret"}],
                        },
                    }
                ],
                f"callback:{start_link}": "ok",
            }
        )
        update_payload = sanitize_bot_update_for_logging(
            {
                "callback_query": {"data": f"user:lookup:{start_link}"},
                "message": {
                    "text": f"{vmess_link} {start_link}",
                    "entities": [{"url": sub_link}],
                    "document": {
                        "file_id": "telegram-file-id-secret",
                        "file_unique_id": "telegram-file-unique-secret",
                        "file_path": "photos/private.jpg",
                    },
                },
            }
        )

        event_text = json.dumps(event_payload, ensure_ascii=False)
        update_text = json.dumps(update_payload, ensure_ascii=False)
        self.assertIn("<config-link-redacted>", event_payload)
        for raw in [
            "vless://",
            "vmess://",
            "trojan://",
            "/sub/private-sub-token",
            "SUPER_SECRET_TOKEN",
            email,
            "proxy-pass-secret",
            "api-key-secret",
            "session-key-secret",
        ]:
            self.assertNotIn(raw, event_text)
        self.assertIn("link_<redacted>", event_text)
        for raw in [
            "vmess://",
            "/sub/private-sub-token",
            "SUPER_SECRET_TOKEN",
            "telegram-file-id-secret",
            "telegram-file-unique-secret",
            "private.jpg",
        ]:
            self.assertNotIn(raw, update_text)
        self.assertIn("<receipt-file-redacted>", update_text)
        self.assertNotIn("SUPER_SECRET_TOKEN", sanitize_bot_text_for_logging(start_link))
        self.assertNotIn(email, sanitize_bot_text_for_logging(email))

    @override_settings(
        TELEGRAM_PROXY_URL="",
        TELEGRAM_PROXY_PROTOCOL="http",
        TELEGRAM_PROXY_HOST="proxy.example",
        TELEGRAM_PROXY_PORT="7880",
        TELEGRAM_PROXY_USERNAME="",
        TELEGRAM_PROXY_PASSWORD="",
    )
    def test_structured_proxy_settings_build_proxy_url(self):
        from .bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url

        proxy_url = telegram_proxy_url()

        self.assertEqual(proxy_url, "http://proxy.example:7880")
        self.assertEqual(sanitized_telegram_proxy_url(proxy_url), "http://proxy.example:7880")

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


class VPNClientManagementServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10 GB",
            slug="10gb-management",
            volume_gb=Decimal("10.000"),
            duration_days=30,
            price=100000,
            is_active=True,
            is_public=True,
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
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
        )
        self.customer = Customer.objects.create(display_name="Alice")
        self.order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sub_link="https://example.com/sub/private-token",
            direct_link="vless://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@vpn.example.com:443#Alice",
        )
        self.vpn_client = VPNClient.objects.create(
            store=self.store,
            order=self.order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_config",
            xui_email="alice_config",
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sub_id="private-token",
            sub_link=self.order.sub_link,
            direct_link=self.order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=10 * (1024 ** 3),
            used_traffic_bytes=1024 ** 3,
            expires_at=timezone.now() + timedelta(days=20),
        )

    @patch("store.vpn_client_management_services.delete_client_from_inbound")
    def test_user_delete_remote_success_soft_deletes_local_and_audits(self, delete_mock):
        from .vpn_client_management_services import delete_vpn_client_for_user

        delete_mock.return_value = {
            "deleted": True,
            "stats_deleted": True,
            "stats_remaining": False,
            "matched_field": "id",
        }

        result = delete_vpn_client_for_user(self.customer, self.vpn_client, actor_telegram_id="42")

        self.assertTrue(result["success"])
        delete_mock.assert_called_once()
        self.vpn_client.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.vpn_client.status, VPNClient.Status.DELETED)
        self.assertIsNotNone(self.vpn_client.deleted_at)
        self.assertEqual(self.vpn_client.deleted_by_customer, self.customer)
        self.assertEqual(self.vpn_client.direct_link, "")
        self.assertEqual(self.vpn_client.sub_link, "")
        self.assertEqual(self.order.direct_link, "")
        self.assertEqual(self.order.sub_link, "")

        log = VPNClientActionLog.objects.get()
        self.assertEqual(log.action, VPNClientActionLog.Action.USER_DELETE)
        self.assertEqual(log.status, VPNClientActionLog.Status.SUCCESS)
        self.assertEqual(log.actor_telegram_id, "42")
        self.assertNotIn(self.vpn_client.uuid, log.xui_identifier_masked)
        self.assertNotIn("vless://", json.dumps(log.metadata))

    @patch("store.vpn_client_management_services.delete_client_from_inbound", side_effect=Exception("panel timeout"))
    def test_user_delete_remote_failure_does_not_soft_delete_local(self, _delete_mock):
        from .vpn_client_management_services import VPNClientManagementError, delete_vpn_client_for_user

        with self.assertRaises(VPNClientManagementError):
            delete_vpn_client_for_user(self.customer, self.vpn_client, actor_telegram_id="42")

        self.vpn_client.refresh_from_db()
        self.assertEqual(self.vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertIsNone(self.vpn_client.deleted_at)
        log = VPNClientActionLog.objects.get()
        self.assertEqual(log.status, VPNClientActionLog.Status.FAILED)

    @patch("store.vpn_client_management_services.update_client_traffic_and_expiry")
    @patch("store.vpn_client_management_services.find_client_by_identifier")
    def test_admin_traffic_update_syncs_local_and_audits(self, find_mock, update_mock):
        from .vpn_client_management_services import update_vpn_client_limits_by_admin

        find_mock.return_value = {
            "panel": self.panel,
            "inbound": self.inbound,
            "identifier": self.vpn_client.uuid,
            "email": self.vpn_client.xui_email,
            "total_bytes": 10 * (1024 ** 3),
            "used_bytes": 1024 ** 3,
            "expiry_time": self.vpn_client.expires_at,
            "client": {"id": self.vpn_client.uuid, "email": self.vpn_client.xui_email},
        }
        update_mock.return_value = {
            "updated": True,
            "new_total_bytes": 15 * (1024 ** 3),
            "new_expiry_time": self.vpn_client.expires_at,
            "enabled": True,
            "raw": {"client": {"email": self.vpn_client.xui_email}},
        }

        result = update_vpn_client_limits_by_admin(
            "999",
            {
                "panel_id": self.panel.pk,
                "inbound_id": self.inbound.inbound_id,
                "identifier": self.vpn_client.uuid,
                "vpn_client_id": self.vpn_client.pk,
            },
            traffic_gb=5,
            mode="add",
        )

        self.assertEqual(result["new_total_bytes"], 15 * (1024 ** 3))
        self.vpn_client.refresh_from_db()
        self.assertEqual(self.vpn_client.traffic_limit_bytes, 15 * (1024 ** 3))
        log = VPNClientActionLog.objects.get(action=VPNClientActionLog.Action.ADMIN_UPDATE_TRAFFIC)
        self.assertEqual(log.status, VPNClientActionLog.Status.SUCCESS)
        self.assertEqual(log.actor_telegram_id, "999")


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

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_fresh_minimal_install_reports_setup_warnings_not_errors(self):
        from io import StringIO

        from django.core.management import call_command

        Store.objects.create(
            name="Qasedak",
            english_name="Qasedak",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )
        stdout = StringIO()

        call_command("check_integrations", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Setup incomplete: no active X-UI panel exists yet", output)
        self.assertIn("Setup incomplete: no active inbound exists yet", output)
        self.assertIn("Setup incomplete: no active Panel and no active Plan exist yet", output)
        self.assertIn("SMSForwarder webhook token is not configured yet", output)
        self.assertIn("ERROR=0", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    @patch("store.bots.requests.post", return_value=DummyBotResponse({"ok": True, "result": {"username": "vpn_store_bot"}}))
    def test_complete_configuration_passes(self, _post_mock):
        from io import StringIO

        from django.core.management import call_command

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        store.set_smsforwarder_webhook_token("sms-secret")
        store.save()
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
            bot_token="test-bot-token:placeholder",
            telegram_bot_username="vpn_store_bot",
            admin_user_id="999",
            force_telegram_channel_join=True,
            telegram_required_channel_username="vpn_store_channel",
            telegram_required_channel_invite_link="https://t.me/vpn_store_channel",
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

        class FakeXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_id, *, use_cache=True):
                return {"id": inbound_id, "protocol": "vless", "remark": "ok", "enable": True}

        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", FakeXUIService),
        ):
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
        self.assertIn("Telegram bot username is configured in BotConfiguration", output)
        self.assertIn("SMSForwarder webhook token is configured in Store", output)
        self.assertIn("ERROR=0", output)

    def create_integration_store_with_panel(self, *, inbound_available=True, inbound_health=True):
        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        store.set_smsforwarder_webhook_token("sms-secret")
        store.save()
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug=f"check-integrations-plan-{store.pk}",
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
            bot_token="test-bot-token:placeholder",
            telegram_bot_username="vpn_store_bot",
            admin_user_id="999",
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
        inbound = Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=inbound_available,
            health_monitor_enabled=inbound_health,
        )
        return store, panel, inbound

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_check_integrations_ignores_legacy_inbound_live_xui_missing(self):
        from io import StringIO

        from django.core.management import call_command
        from store.xui_api import XUIError

        _store, _panel, legacy_inbound = self.create_integration_store_with_panel(
            inbound_available=False,
            inbound_health=False,
        )
        Inbound.objects.create(
            panel=legacy_inbound.panel,
            inbound_id=2,
            server_ip="127.0.0.1",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

        class FakeXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_id, *, use_cache=True):
                if inbound_id == legacy_inbound.inbound_id:
                    raise XUIError("Obtain (record not found)")
                return {"id": inbound_id, "protocol": "vless", "remark": "ok", "enable": True}

        stdout = StringIO()
        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", FakeXUIService),
        ):
            call_command("check_integrations", "--live-xui", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("legacy inbound(s) ignored from health monitor and new orders", output)
        self.assertIn("Legacy inbound is missing/unreadable in X-UI and ignored from health monitor", output)
        self.assertIn("ERROR=0", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_check_integrations_errors_when_sales_inbound_missing_live_xui(self):
        from io import StringIO

        from django.core.management import call_command
        from store.xui_api import XUIError

        self.create_integration_store_with_panel(inbound_available=True, inbound_health=True)

        class MissingXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_id, *, use_cache=True):
                raise XUIError("Obtain (record not found)")

        stdout = StringIO()
        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", MissingXUIService),
        ):
            call_command("check_integrations", "--live-xui", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("available for new orders but missing/unreadable in X-UI", output)
        self.assertIn("No inbound available for new orders could be verified in X-UI", output)
        self.assertIn("ERROR=", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="sms-secret", TELEGRAM_BOT_USERNAME="vpn_store_bot")
    def test_free_trial_configuration_errors_are_reported(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            bot_token="test-bot-token:placeholder",
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


class PanelHealthServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            panel_monitor_alert_cooldown_minutes=30,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Germany 1",
            url="https://panel.example.com/secret",
            username="admin",
            password="panel-password",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def service_mock(self, *, login_side_effect=None, inbound_payload=None, inbound_side_effect=None):
        service = Mock()
        service.login.side_effect = login_side_effect
        service.get_inbound.return_value = inbound_payload or {
            "id": self.inbound.inbound_id,
            "protocol": self.inbound.protocol,
            "remark": self.inbound.remark,
            "enable": True,
        }
        if inbound_side_effect is not None:
            service.get_inbound.side_effect = inbound_side_effect
        return service

    def test_panel_ok_records_status_and_log(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock()
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        health = PanelHealthStatus.objects.get(panel=self.panel)
        self.assertEqual(health.status, PanelHealthStatus.Status.OK)
        log = PanelHealthCheckLog.objects.get(panel=self.panel)
        self.assertTrue(log.login_ok)
        self.assertEqual(log.inbounds_ok, 1)

    def test_panel_login_fail_records_error(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(login_side_effect=XUIError("Panel login failed for panel-password"))
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.ERROR)
        self.assertEqual(result["error_code"], "auth_failed")
        self.assertNotIn("panel-password", str(result))
        self.assertEqual(PanelHealthCheckLog.objects.get(panel=self.panel).status, PanelHealthStatus.Status.ERROR)

    def test_panel_timeout_is_caught_and_sanitized(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock(
            login_side_effect=requests.Timeout("timeout for https://panel.example.com/secret with panel-password")
        )
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.ERROR)
        self.assertEqual(result["error_code"], "timeout")
        payload = json.dumps(PanelHealthCheckLog.objects.get(panel=self.panel).metadata, ensure_ascii=False)
        self.assertNotIn("https://panel.example.com/secret", payload)
        self.assertNotIn("panel-password", payload)

    def test_missing_inbound_becomes_warning(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(inbound_side_effect=XUIError("Inbound was not found on panel."))
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.WARNING)
        self.assertEqual(result["inbounds_error"], 1)
        self.assertEqual(PanelHealthStatus.objects.get(panel=self.panel).status, PanelHealthStatus.Status.WARNING)

    def test_health_monitor_disabled_inbound_is_ignored(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.inbound.available_for_new_orders = False
        self.inbound.health_monitor_enabled = False
        self.inbound.legacy_note = "Legacy inbound kept for old clients."
        self.inbound.save(
            update_fields=[
                "available_for_new_orders",
                "health_monitor_enabled",
                "legacy_note",
                "updated_at",
            ]
        )
        service = self.service_mock(inbound_side_effect=XUIError("Inbound was not found on panel."))

        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(result["inbounds_checked"], 0)
        self.assertEqual(result["metadata"]["ignored_inbounds"], 1)
        self.assertEqual(result["metadata"]["ignored_inbound_ids"], [self.inbound.inbound_id])
        service.get_inbound.assert_not_called()

    def test_status_transition_ok_to_error_sends_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )
        PanelHealthStatus.objects.create(panel=self.panel, status=PanelHealthStatus.Status.OK, last_ok_at=timezone.now())
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))

        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["alert_sent_count"], 1)
        send_mock.assert_called_once()
        health = PanelHealthStatus.objects.get(panel=self.panel)
        self.assertIsNotNone(health.last_alert_sent_at)
        self.assertTrue(PanelHealthCheckLog.objects.get(panel=self.panel).alert_sent)

    def test_repeated_error_before_cooldown_does_not_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now(),
            last_alert_sent_at=timezone.now(),
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertTrue(result["alert_skipped"])
        self.assertEqual(result["alert_skip_reason"], "cooldown")

    def test_error_to_ok_sends_recovery_alert(self):
        from .panel_health_services import check_panel_health

        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now() - timedelta(minutes=21),
            last_alert_sent_at=timezone.now() - timedelta(minutes=20),
        )
        service = self.service_mock()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(result["alert_sent_count"], 1)
        self.assertGreaterEqual(result["downtime_minutes"], 20)
        send_mock.assert_called_once()
        self.assertIsNotNone(PanelHealthStatus.objects.get(panel=self.panel).last_recovery_alert_sent_at)

    def test_disabled_panel_is_not_connected_to(self):
        from .panel_health_services import check_panel_health

        self.panel.is_active = False
        self.panel.save(update_fields=["is_active", "updated_at"])
        with patch("store.panel_health_services.XUIService") as service_cls:
            result = check_panel_health(self.panel)

        service_cls.assert_not_called()
        self.assertEqual(result["status"], PanelHealthStatus.Status.DISABLED)
        self.assertEqual(PanelHealthCheckLog.objects.get(panel=self.panel).status, PanelHealthStatus.Status.DISABLED)

    def test_dry_run_does_not_log_or_alert(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True, dry_run=True)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(PanelHealthStatus.objects.count(), 0)
        self.assertEqual(PanelHealthCheckLog.objects.count(), 0)
        send_mock.assert_not_called()

    def test_command_dry_run_summary(self):
        service = self.service_mock()
        stdout = StringIO()
        with patch("store.panel_health_services.XUIService", return_value=service):
            call_command("check_panel_health", "--dry-run", "--verbose", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("total_panels=1", output)
        self.assertIn("checked=1", output)
        self.assertIn("ok=1", output)
        self.assertEqual(PanelHealthCheckLog.objects.count(), 0)

    def test_cleanup_old_logs(self):
        from .panel_health_services import cleanup_old_panel_health_logs

        old_log = PanelHealthCheckLog.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.OK,
            checked_at=timezone.now() - timedelta(days=40),
            login_ok=True,
        )
        new_log = PanelHealthCheckLog.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.OK,
            checked_at=timezone.now(),
            login_ok=True,
        )

        summary = cleanup_old_panel_health_logs(self.store)

        self.assertEqual(summary["deleted"], 1)
        self.assertFalse(PanelHealthCheckLog.objects.filter(pk=old_log.pk).exists())
        self.assertTrue(PanelHealthCheckLog.objects.filter(pk=new_log.pk).exists())


class PanelUsageServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            panel_usage_active_user_method=Store.PanelUsageActiveUserMethod.MIXED,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Germany 1",
            url="https://panel.example.com/secret",
            username="admin",
            password="panel-password",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.report_date = date(2026, 6, 5)
        self.tz = ZoneInfo("Asia/Tehran")
        self.period_start = datetime(2026, 6, 5, 0, 0, tzinfo=self.tz)
        self.period_end = datetime(2026, 6, 6, 0, 0, tzinfo=self.tz)

    def inbound_payload(self, clients=None, stats=None):
        return {
            "id": self.inbound.inbound_id,
            "protocol": "vless",
            "remark": "Main",
            "settings": json.dumps({"clients": clients or []}),
            "clientStats": stats or [],
        }

    def service_mock(self, *, login_side_effect=None, inbound_side_effect=None, online_clients=None):
        service = Mock()
        service.login.side_effect = login_side_effect
        service.get_online_clients.return_value = set(online_clients or [])
        if inbound_side_effect is not None:
            service.get_inbound.side_effect = inbound_side_effect
        else:
            service.get_inbound.return_value = self.inbound_payload(
                clients=[
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "email": "alice@example.com",
                        "enable": True,
                    }
                ],
                stats=[
                    {
                        "email": "alice@example.com",
                        "up": 100,
                        "down": 200,
                        "total": 1000,
                        "expiryTime": 0,
                        "enable": True,
                    }
                ],
            )
        return service

    def create_panel_snapshot(
        self,
        captured_at,
        *,
        total_upload=0,
        total_download=0,
        status=PanelUsageSnapshot.Status.OK,
        clients_count=0,
    ):
        return PanelUsageSnapshot.objects.create(
            panel=self.panel,
            captured_at=captured_at,
            status=status,
            total_upload_bytes=total_upload,
            total_download_bytes=total_download,
            total_used_bytes=total_upload + total_download,
            clients_count=clients_count,
            checked_inbounds_count=1,
            active_inbounds_count=1,
        )

    def create_client_snapshot(self, captured_at, identifier_hash, used, *, online=None):
        return PanelClientUsageSnapshot.objects.create(
            panel=self.panel,
            inbound=self.inbound,
            captured_at=captured_at,
            client_identifier_hash=identifier_hash,
            client_identifier_masked=f"{identifier_hash[:2]}***",
            email_masked=f"{identifier_hash[:2]}***",
            upload_bytes=used,
            download_bytes=0,
            used_bytes=used,
            online=online,
            source="clientStats",
        )

    def test_collect_panel_usage_snapshot_with_healthy_xui_response(self):
        from .panel_usage_services import collect_panel_usage_snapshot

        service = self.service_mock(online_clients={"alice@example.com"})
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel)

        self.assertEqual(result["status"], PanelUsageSnapshot.Status.OK)
        snapshot = PanelUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(snapshot.total_used_bytes, 300)
        self.assertEqual(snapshot.clients_count, 1)
        self.assertEqual(snapshot.online_clients_count, 1)
        client_snapshot = PanelClientUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(client_snapshot.used_bytes, 300)
        self.assertTrue(client_snapshot.online)
        payload = json.dumps(
            {
                "identifier": client_snapshot.client_identifier_hash,
                "masked": client_snapshot.client_identifier_masked,
                "email": client_snapshot.email_masked,
                "metadata": client_snapshot.metadata,
            },
            ensure_ascii=False,
        )
        self.assertNotIn("alice@example.com", payload)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", payload)

    def test_collect_panel_usage_snapshot_partial_when_one_inbound_fails(self):
        from .panel_usage_services import collect_panel_usage_snapshot
        from .xui_api import XUIError

        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Backup",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

        def get_inbound(inbound_id, *, use_cache=True):
            if inbound_id == second_inbound.inbound_id:
                raise XUIError("Inbound was not found on https://panel.example.com/secret with panel-password")
            return self.inbound_payload(
                clients=[{"id": "client-1", "email": "alice@example.com"}],
                stats=[{"email": "alice@example.com", "up": 10, "down": 5}],
            )

        service = self.service_mock(inbound_side_effect=get_inbound)
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel)

        snapshot = PanelUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(result["status"], PanelUsageSnapshot.Status.PARTIAL)
        self.assertEqual(snapshot.status, PanelUsageSnapshot.Status.PARTIAL)
        self.assertEqual(snapshot.metadata["inbound_error_count"], 1)
        self.assertNotIn("panel-password", json.dumps(snapshot.metadata))
        self.assertNotIn("https://panel.example.com/secret", json.dumps(snapshot.metadata))

    def test_collect_panel_usage_snapshot_failed_when_login_fails(self):
        from .panel_usage_services import collect_panel_usage_snapshot
        from .xui_api import XUIError

        service = self.service_mock(login_side_effect=XUIError("Panel login failed for panel-password"))
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel)

        snapshot = PanelUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(result["status"], PanelUsageSnapshot.Status.FAILED)
        self.assertEqual(snapshot.status, PanelUsageSnapshot.Status.FAILED)
        self.assertNotIn("panel-password", snapshot.error_message)

    def test_collect_panel_usage_snapshot_dry_run_does_not_save(self):
        from .panel_usage_services import collect_panel_usage_snapshot

        service = self.service_mock()
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel, dry_run=True)

        self.assertEqual(result["status"], PanelUsageSnapshot.Status.OK)
        self.assertEqual(result["snapshots_created"], 0)
        self.assertEqual(PanelUsageSnapshot.objects.count(), 0)
        self.assertEqual(PanelClientUsageSnapshot.objects.count(), 0)

    def test_cleanup_old_panel_usage_snapshots(self):
        from .panel_usage_services import cleanup_old_panel_usage_snapshots

        old_time = timezone.now() - timedelta(days=60)
        new_time = timezone.now() - timedelta(days=2)
        old_snapshot = self.create_panel_snapshot(old_time, total_upload=1, clients_count=1)
        new_snapshot = self.create_panel_snapshot(new_time, total_upload=1, clients_count=1)
        old_client = self.create_client_snapshot(old_time, "old-client", 1)
        new_client = self.create_client_snapshot(new_time, "new-client", 1)

        summary = cleanup_old_panel_usage_snapshots(retention_days=45)

        self.assertGreaterEqual(summary["deleted"], 2)
        self.assertFalse(PanelUsageSnapshot.objects.filter(pk=old_snapshot.pk).exists())
        self.assertTrue(PanelUsageSnapshot.objects.filter(pk=new_snapshot.pk).exists())
        self.assertFalse(PanelClientUsageSnapshot.objects.filter(pk=old_client.pk).exists())
        self.assertTrue(PanelClientUsageSnapshot.objects.filter(pk=new_client.pk).exists())

    def test_calculate_panel_daily_usage_delta_and_traffic_active_users(self):
        from .panel_usage_services import calculate_panel_daily_usage

        self.store.panel_usage_active_user_method = Store.PanelUsageActiveUserMethod.TRAFFIC_DELTA
        self.store.save(update_fields=["panel_usage_active_user_method", "updated_at"])
        self.create_panel_snapshot(self.period_start, total_upload=100, total_download=50, clients_count=1)
        self.create_panel_snapshot(self.period_end, total_upload=450, total_download=100, clients_count=2)
        self.create_client_snapshot(self.period_start, "alice", 100)
        self.create_client_snapshot(self.period_end, "alice", 300)
        self.create_client_snapshot(self.period_end, "bob", 100)

        usage = calculate_panel_daily_usage(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(usage.used_bytes, 400)
        self.assertEqual(usage.upload_bytes, 350)
        self.assertEqual(usage.download_bytes, 50)
        self.assertEqual(usage.active_users_count, 2)
        self.assertEqual(usage.data_quality, PanelDailyUsage.DataQuality.COMPLETE)

    def test_calculate_panel_daily_usage_negative_delta_is_partial(self):
        from .panel_usage_services import calculate_panel_daily_usage

        self.create_panel_snapshot(self.period_start, total_upload=500, total_download=200, clients_count=1)
        self.create_panel_snapshot(self.period_end, total_upload=100, total_download=50, clients_count=1)

        usage = calculate_panel_daily_usage(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(usage.used_bytes, 0)
        self.assertEqual(usage.data_quality, PanelDailyUsage.DataQuality.PARTIAL)
        self.assertTrue(any("delta" in warning for warning in usage.metadata["warnings"]))

    def test_active_users_can_use_online_api(self):
        from .panel_usage_services import calculate_panel_daily_usage

        self.store.panel_usage_active_user_method = Store.PanelUsageActiveUserMethod.ONLINE_API
        self.store.save(update_fields=["panel_usage_active_user_method", "updated_at"])
        self.create_panel_snapshot(self.period_start, total_upload=0, total_download=0, clients_count=1)
        self.create_panel_snapshot(self.period_end, total_upload=0, total_download=0, clients_count=1)
        self.create_client_snapshot(self.period_start + timedelta(hours=12), "online-user", 0, online=True)

        usage = calculate_panel_daily_usage(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(usage.active_users_count, 1)
        self.assertEqual(usage.online_users_count, 1)

    def test_mixed_active_users_unions_traffic_and_online(self):
        from .panel_usage_services import calculate_panel_daily_usage

        self.create_panel_snapshot(self.period_start, total_upload=0, total_download=0, clients_count=2)
        self.create_panel_snapshot(self.period_end, total_upload=10, total_download=0, clients_count=2)
        self.create_client_snapshot(self.period_start, "traffic-user", 0)
        self.create_client_snapshot(self.period_end, "traffic-user", 10)
        self.create_client_snapshot(self.period_start + timedelta(hours=3), "online-user", 0, online=True)

        usage = calculate_panel_daily_usage(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(usage.active_users_count, 2)

    def test_usage_comparison_with_previous_day_and_week_average(self):
        from .panel_usage_services import get_panel_usage_comparison

        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date,
            timezone="Asia/Tehran",
            used_bytes=200,
            active_users_count=2,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )
        for offset, used in enumerate([100, 70, 70, 70, 70, 70, 70], start=1):
            PanelDailyUsage.objects.create(
                panel=self.panel,
                usage_date=self.report_date - timedelta(days=offset),
                timezone="Asia/Tehran",
                used_bytes=used,
                data_quality=PanelDailyUsage.DataQuality.COMPLETE,
            )

        comparison = get_panel_usage_comparison(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(comparison["previous"].used_bytes, 100)
        self.assertEqual(comparison["week_average_used_bytes"], 74)
        self.assertIn("🔼", comparison["previous_change"])
        self.assertEqual(comparison["warnings"], [])

    def test_usage_comparison_warns_when_week_average_has_too_few_days(self):
        from .panel_usage_services import get_panel_usage_comparison

        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date,
            timezone="Asia/Tehran",
            used_bytes=200,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )
        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date - timedelta(days=1),
            timezone="Asia/Tehran",
            used_bytes=100,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )

        comparison = get_panel_usage_comparison(self.panel, self.report_date, timezone="Asia/Tehran")

        self.assertEqual(comparison["week_average_days"], 1)
        self.assertTrue(comparison["warnings"])

    def test_collect_panel_usage_snapshot_command_dry_run_summary(self):
        service = self.service_mock()
        stdout = StringIO()
        with patch("store.xui_api.XUIService", return_value=service):
            call_command("collect_panel_usage_snapshots", "--dry-run", "--verbose", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("total_panels=1", output)
        self.assertIn("ok=1", output)
        self.assertIn("snapshots_created=0", output)
        self.assertEqual(PanelUsageSnapshot.objects.count(), 0)

    def test_calculate_panel_daily_usage_command_dry_run_summary(self):
        self.create_panel_snapshot(self.period_start, total_upload=0, total_download=0, clients_count=0)
        self.create_panel_snapshot(self.period_end, total_upload=1024, total_download=0, clients_count=0)
        stdout = StringIO()

        call_command(
            "calculate_panel_daily_usage",
            "--dry-run",
            "--date",
            self.report_date.isoformat(),
            "--timezone",
            "Asia/Tehran",
            "--verbose",
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("calculated=1", output)
        self.assertIn("dry_run=True", output)
        self.assertEqual(PanelDailyUsage.objects.count(), 0)


class DailyAdminReportServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="30 Days",
            slug="daily-report-30-days",
            volume_gb=Decimal("10.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Germany 1",
            url="https://panel.example.com",
            username="admin",
            password="secret",
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
        )
        self.report_date = date(2026, 6, 5)
        self.period_start = timezone.make_aware(datetime(2026, 6, 5, 0, 30), ZoneInfo("Asia/Tehran"))

    def create_order(self, *, status=Order.Status.COMPLETED, amount=100000, metadata=None, created_at=None):
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=self.inbound,
            status=status,
            original_amount=amount,
            amount=amount,
            currency=Plan.Currency.TOMAN,
            metadata=metadata or {},
        )
        if created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
            order.refresh_from_db()
        return order

    def create_customer(self, username):
        return Customer.objects.create(username=username, display_name=username)

    def test_daily_sales_and_order_stats_use_real_order_statuses(self):
        from .daily_report_services import collect_daily_order_stats, collect_daily_sales_stats, get_report_period

        _report_date, start, end = get_report_period(self.report_date, store=self.store)
        self.create_order(status=Order.Status.COMPLETED, amount=100000, created_at=self.period_start)
        self.create_order(
            status=Order.Status.CONFIRMED,
            amount=200000,
            metadata={"renewal": True, "renewal_client_pk": 1},
            created_at=self.period_start,
        )
        self.create_order(status=Order.Status.PENDING_VERIFICATION, amount=300000, created_at=self.period_start)
        self.create_order(status=Order.Status.REJECTED, amount=400000, created_at=self.period_start)

        sales = collect_daily_sales_stats(start, end, store=self.store)
        orders = collect_daily_order_stats(start, end, store=self.store)

        self.assertEqual(sales["successful_amount"], 300000)
        self.assertEqual(sales["successful_count"], 2)
        self.assertEqual(sales["successful_renewals"], 1)
        self.assertEqual(orders["pending"], 1)
        self.assertEqual(orders["rejected"], 1)

    def test_daily_growth_and_operations_stats(self):
        from payments.models import IncomingPaymentSMS

        from .daily_report_services import (
            collect_daily_broadcast_stats,
            collect_daily_referral_stats,
            collect_daily_reminder_stats,
            collect_daily_sms_stats,
            collect_daily_trial_stats,
            get_report_period,
        )

        _report_date, start, end = get_report_period(self.report_date, store=self.store)
        inviter = self.create_customer("inviter")
        invited = self.create_customer("invited")
        order = self.create_order(status=Order.Status.COMPLETED, created_at=self.period_start)
        Referral.objects.create(
            referrer=inviter,
            referred_customer=invited,
            referral_code="ABC123",
            status=Referral.Status.PURCHASED,
            first_order=order,
            purchased_at=self.period_start,
        )
        ledger = ReferralRewardLedger.objects.create(
            inviter=inviter,
            invited=invited,
            order=order,
            reward_gb=Decimal("2.000"),
            reward_duration_days=30,
            status=ReferralRewardLedger.Status.REDEEMED,
            available_at=self.period_start,
            redeemed_at=self.period_start,
        )
        trial = FreeTrialRequest.objects.create(
            customer=invited,
            panel=self.panel,
            inbound=self.inbound,
            status=FreeTrialRequest.Status.DELIVERED,
            traffic_gb=Decimal("1.000"),
            duration_hours=24,
        )
        FreeTrialRequest.objects.filter(pk=trial.pk).update(created_at=self.period_start)
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="client",
            uuid="11111111-1111-4111-8111-111111111111",
        )
        reminder = VPNClientReminderLog.objects.create(
            customer=invited,
            vpn_client=vpn_client,
            reminder_type=VPNClientReminderLog.ReminderType.EXPIRY_BEFORE,
            trigger_key="expiry_before:3",
            status=VPNClientReminderLog.Status.SENT,
            sent_at=self.period_start,
        )
        campaign = BroadcastMessage.objects.create(
            store=self.store,
            title="Daily",
            message_text="hello",
            audience_type=BroadcastMessage.AudienceType.ALL,
            channel=BroadcastMessage.Channel.TELEGRAM,
            status=BroadcastMessage.Status.SENT,
            sent_at=self.period_start,
        )
        BroadcastRecipient.objects.create(
            campaign=campaign,
            customer=invited,
            channel=BroadcastMessage.Channel.TELEGRAM,
            target_identifier="42",
            status=BroadcastRecipient.Status.SENT,
            sent_at=self.period_start,
        )
        IncomingPaymentSMS.objects.create(
            raw_text="sms matched",
            amount=100000,
            balance=1,
            sms_datetime=self.period_start,
            received_at=self.period_start,
            status=IncomingPaymentSMS.Status.MATCHED,
        )
        IncomingPaymentSMS.objects.create(
            raw_text="sms no match",
            amount=999999,
            balance=1,
            sms_datetime=self.period_start,
            received_at=self.period_start,
            status=IncomingPaymentSMS.Status.NO_MATCH,
        )

        self.assertEqual(collect_daily_trial_stats(start, end, store=self.store)["total"], 1)
        self.assertEqual(collect_daily_referral_stats(start, end, store=self.store)["packages_redeemed"], 1)
        self.assertEqual(collect_daily_reminder_stats(start, end, store=self.store)["sent"], 1)
        self.assertEqual(collect_daily_broadcast_stats(start, end, store=self.store)["campaigns_sent"], 1)
        self.assertEqual(collect_daily_sms_stats(start, end, store=self.store)["matched"], 1)
        self.assertEqual(collect_daily_sms_stats(start, end, store=self.store)["no_match"], 1)
        self.assertEqual(ledger.status, ReferralRewardLedger.Status.REDEEMED)
        self.assertEqual(reminder.status, VPNClientReminderLog.Status.SENT)

    def test_panel_health_summary_in_report_message(self):
        from .daily_report_services import build_daily_admin_report_message

        PanelHealthStatus.objects.create(panel=self.panel, status=PanelHealthStatus.Status.WARNING, summary="Inbound missing")

        message = build_daily_admin_report_message(self.report_date, store=self.store)

        self.assertIn("گزارش روزانه", message)
        self.assertIn("💰 فروش:", message)
        self.assertIn("🔔 عملیات:", message)
        self.assertIn("Germany 1: WARNING", message)

    def test_duplicate_report_without_force_is_skipped(self):
        from .daily_report_services import get_report_period, send_daily_admin_report

        _report_date, start, end = get_report_period(self.report_date, store=self.store)
        DailyAdminReportLog.objects.create(
            store=self.store,
            report_date=self.report_date,
            period_start=start,
            period_end=end,
            status=DailyAdminReportLog.Status.SENT,
            sent_to_count=1,
            sent_at=timezone.now(),
        )

        with patch("store.daily_report_services.send_admin_message_to_telegram_admins") as send_mock:
            summary = send_daily_admin_report(self.report_date, store=self.store)

        send_mock.assert_not_called()
        self.assertEqual(summary["skipped"], 1)

    def test_force_allows_resending_existing_report_log(self):
        from .daily_report_services import get_report_period, send_daily_admin_report

        _report_date, start, end = get_report_period(self.report_date, store=self.store)
        log = DailyAdminReportLog.objects.create(
            store=self.store,
            report_date=self.report_date,
            period_start=start,
            period_end=end,
            status=DailyAdminReportLog.Status.SENT,
            sent_to_count=1,
            sent_at=timezone.now(),
        )

        with patch(
            "store.daily_report_services.send_admin_message_to_telegram_admins",
            return_value={"attempted": 1, "sent": 1, "failed": 0},
        ):
            summary = send_daily_admin_report(self.report_date, force=True, store=self.store)

        log.refresh_from_db()
        self.assertEqual(summary["sent"], 1)
        self.assertTrue(log.metadata["force"])
        self.assertEqual(DailyAdminReportLog.objects.filter(store=self.store, report_date=self.report_date).count(), 1)

    def test_daily_report_dry_run_does_not_send_or_log(self):
        from .daily_report_services import send_daily_admin_report

        with patch("store.daily_report_services.send_admin_message_to_telegram_admins") as send_mock:
            summary = send_daily_admin_report(self.report_date, dry_run=True, store=self.store)

        send_mock.assert_not_called()
        self.assertEqual(summary["would_send"], 1)
        self.assertEqual(DailyAdminReportLog.objects.count(), 0)
        self.assertEqual(PanelDailyUsage.objects.count(), 0)
        self.assertIn("گزارش روزانه", summary["reports"][0]["message"])

    def test_partial_admin_delivery_marks_report_sent(self):
        from .daily_report_services import send_daily_admin_report

        with patch(
            "store.daily_report_services.send_admin_message_to_telegram_admins",
            return_value={"attempted": 2, "sent": 1, "failed": 1},
        ):
            summary = send_daily_admin_report(self.report_date, store=self.store)

        log = DailyAdminReportLog.objects.get(store=self.store, report_date=self.report_date)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(log.status, DailyAdminReportLog.Status.SENT)
        self.assertTrue(log.metadata["partial_failure"])

    def test_daily_report_command_dry_run_prints_message(self):
        with patch("store.daily_report_services.send_admin_message_to_telegram_admins") as send_mock:
            stdout = StringIO()
            call_command("send_daily_admin_report", "--dry-run", "--date", self.report_date.isoformat(), "--verbose", stdout=stdout)

        send_mock.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("would_send=1", output)
        self.assertIn("گزارش روزانه", output)

    def test_daily_report_does_not_include_config_links_or_panel_passwords(self):
        from .daily_report_services import build_daily_admin_report_message

        self.create_order(
            status=Order.Status.COMPLETED,
            metadata={"direct_link": "vless://secret@example.com:443#private"},
            created_at=self.period_start,
        )
        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date,
            timezone="Asia/Tehran",
            used_bytes=1024,
            active_users_count=1,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )

        message = build_daily_admin_report_message(self.report_date, store=self.store)

        self.assertNotIn("vless://", message)
        self.assertNotIn("secret", message)

    def test_daily_report_includes_panel_usage_section(self):
        from .daily_report_services import build_daily_admin_report_message

        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date,
            timezone="Asia/Tehran",
            used_bytes=124 * 1024**3,
            active_users_count=183,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )
        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date - timedelta(days=1),
            timezone="Asia/Tehran",
            used_bytes=98 * 1024**3,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )
        for offset in range(2, 8):
            PanelDailyUsage.objects.create(
                panel=self.panel,
                usage_date=self.report_date - timedelta(days=offset),
                timezone="Asia/Tehran",
                used_bytes=87 * 1024**3,
                data_quality=PanelDailyUsage.DataQuality.COMPLETE,
            )

        message = build_daily_admin_report_message(self.report_date, store=self.store)

        self.assertIn("📈 مصرف پنل‌ها", message)
        self.assertIn("Germany 1", message)
        self.assertIn("مصرف دیروز", message)
        self.assertIn("نسبت به روز قبل", message)
        self.assertIn("نسبت به میانگین هفته", message)
        self.assertIn("کاربران فعال: ۱۸۳", message)

    def test_daily_report_warns_when_panel_usage_snapshots_are_insufficient(self):
        from .daily_report_services import build_daily_admin_report_message

        message = build_daily_admin_report_message(self.report_date, store=self.store)

        self.assertIn("📈 مصرف پنل‌ها", message)
        self.assertIn("مصرف دیروز: نامشخص", message)
        self.assertIn("دلیل:", message)

    def test_daily_report_panel_usage_section_is_capped(self):
        from .daily_report_services import build_daily_admin_report_message

        PanelDailyUsage.objects.create(
            panel=self.panel,
            usage_date=self.report_date,
            timezone="Asia/Tehran",
            used_bytes=1024,
            data_quality=PanelDailyUsage.DataQuality.COMPLETE,
        )
        for index in range(12):
            panel = Panel.objects.create(
                store=self.store,
                name=f"Panel {index}",
                url=f"https://panel-{index}.example.com",
                username="admin",
                password="secret",
            )
            PanelDailyUsage.objects.create(
                panel=panel,
                usage_date=self.report_date,
                timezone="Asia/Tehran",
                used_bytes=(index + 1) * 1024,
                active_users_count=index,
                data_quality=PanelDailyUsage.DataQuality.COMPLETE,
            )

        message = build_daily_admin_report_message(self.report_date, store=self.store)

        self.assertIn("پنل دیگر", message)
        self.assertLess(len(message), 4096)


class HealthAndReportIntegrationCheckTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        Plan.objects.create(
            store=self.store,
            name="1 GB",
            slug="health-report-check-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
        )
        Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
        )
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
            telegram_bot_username="vpn_store_bot",
        )
        self.store.set_smsforwarder_webhook_token("sms-secret")
        self.store.save()

    def test_check_integrations_reports_new_commands_and_initial_warnings(self):
        stdout = StringIO()

        call_command("check_integrations", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("check_panel_health command is available", output)
        self.assertIn("send_daily_admin_report command is available", output)
        self.assertIn("collect_panel_usage_snapshots command is available", output)
        self.assertIn("calculate_panel_daily_usage command is available", output)
        self.assertIn("No panel health check has been recorded yet", output)
        self.assertIn("No daily admin report has been sent yet", output)
        self.assertIn("هنوز snapshot مصرف پنل ثبت نشده است", output)


class ClientNamingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
        customer = Customer.objects.create(display_name="Sample Customer", username="sample")

        result = self.create_order(
            customer=customer,
            sender_card_name="رسید تصویری",
        )

        self.assertTrue(result.success)
        prefix = xui_mock.call_args.kwargs["email_prefix"]
        self.assertRegex(prefix, r"^sample_customer_[0-9a-f]{8}$")

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
        email = build_xui_client_email("sample_abcdef12", full_uuid)

        self.assertEqual(email, "sample_abcdef12_11111111")
        self.assertLessEqual(len(email), 40)
        self.assertNotIn(full_uuid, email)


class ConfigLookupServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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


class InboundAdminActionTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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

    def inbound_admin(self):
        from django.contrib import admin
        from .admin import InboundAdmin

        return InboundAdmin(Inbound, admin.site)

    def test_mark_as_legacy_action_excludes_inbound_and_sets_note(self):
        model_admin = self.inbound_admin()

        model_admin.mark_as_legacy(None, Inbound.objects.filter(pk=self.inbound.pk))

        self.inbound.refresh_from_db()
        self.assertFalse(self.inbound.available_for_new_orders)
        self.assertFalse(self.inbound.health_monitor_enabled)
        self.assertTrue(self.inbound.legacy_note)

    def test_enable_action_reenables_new_orders_and_health_monitor(self):
        self.inbound.available_for_new_orders = False
        self.inbound.health_monitor_enabled = False
        self.inbound.legacy_note = "Legacy inbound kept for old clients."
        self.inbound.save(
            update_fields=[
                "available_for_new_orders",
                "health_monitor_enabled",
                "legacy_note",
                "updated_at",
            ]
        )
        model_admin = self.inbound_admin()

        model_admin.enable_for_new_orders_and_health(None, Inbound.objects.filter(pk=self.inbound.pk))

        self.inbound.refresh_from_db()
        self.assertTrue(self.inbound.available_for_new_orders)
        self.assertTrue(self.inbound.health_monitor_enabled)


class OrderQuantityPricingTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
    def test_new_order_does_not_select_inbound_unavailable_for_new_orders(self, xui_mock):
        from .order_services import get_available_inbound

        self.inbound.available_for_new_orders = False
        self.inbound.health_monitor_enabled = False
        self.inbound.legacy_note = "Legacy inbound kept for old clients."
        self.inbound.save(
            update_fields=[
                "available_for_new_orders",
                "health_monitor_enabled",
                "legacy_note",
                "updated_at",
            ]
        )

        self.assertIsNone(get_available_inbound(self.store))
        result = create_manual_payment_order(
            store=self.store,
            customer=None,
            plan=self.plan,
            inbound=None,
            sender_card_name="Alice Buyer",
            sender_card_last4="1234",
            payment_time=time(14, 35),
            bank_tracking_code="TRK123",
            quantity=1,
            metadata={"source": "test"},
        )

        self.assertFalse(result.success)
        self.assertIn("سرور VPN فعالی", result.message)
        self.assertEqual(Order.objects.count(), 0)
        xui_mock.assert_not_called()

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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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


class PlanInboundRouteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            bank_name="Test Bank",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="50 GB",
            slug="route-50gb",
            volume_gb=Decimal("50.000"),
            duration_days=30,
            price=500000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Route Panel",
            url="https://route-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.fallback_inbound = self.create_inbound(1, remark="fallback", current_users=0)
        self.route_inbound = self.create_inbound(7, remark="route", current_users=10)
        self.exact_inbound = self.create_inbound(10, remark="exact", current_users=20)
        self.operator_a = Operator.objects.create(store=self.store, name="همراه اول", slug="mci")
        self.operator_b = Operator.objects.create(store=self.store, name="ایرانسل", slug="irancell")
        self.plan.operators.add(self.operator_a, self.operator_b)

    def create_inbound(self, inbound_id, *, remark="", current_users=0, available=True, panel=None):
        return Inbound.objects.create(
            panel=panel or self.panel,
            inbound_id=inbound_id,
            remark=remark,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=available,
            current_users=current_users,
        )

    def create_plan(self, name, *, is_active=True):
        slug = name.lower().replace(" ", "-")
        return Plan.objects.create(
            store=self.store,
            name=name,
            slug=f"route-{slug}",
            volume_gb=Decimal("10.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=is_active,
            is_public=True,
        )

    def create_route(self, *, plan=None, operator=None, inbound=None, priority=100, is_active=True):
        return PlanInboundRoute.objects.create(
            store=self.store,
            plan=plan or self.plan,
            operator=operator,
            inbound=inbound or self.route_inbound,
            priority=priority,
            is_active=is_active,
        )

    def login_admin(self):
        admin_user = get_user_model().objects.create_superuser(
            username="route-admin",
            email="route-admin@example.com",
            password="secret",
        )
        self.client.force_login(admin_user)
        return admin_user

    def order_kwargs(self, *, operator=None, bank_tracking_code="ROUTE1"):
        return {
            "store": self.store,
            "customer": None,
            "plan": self.plan,
            "operator": operator,
            "sender_card_name": "Alice Buyer",
            "sender_card_last4": "1234",
            "payment_time": time(14, 35),
            "bank_tracking_code": bank_tracking_code,
            "metadata": {"source": "test"},
        }

    def test_get_valid_sales_inbounds_only_returns_new_order_inbounds(self):
        unavailable = self.create_inbound(30, remark="unavailable", available=False)
        inactive = self.create_inbound(31, remark="inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active", "updated_at"])

        inbounds = list(get_valid_sales_inbounds(self.store))

        self.assertIn(self.route_inbound, inbounds)
        self.assertNotIn(unavailable, inbounds)
        self.assertNotIn(inactive, inbounds)

    def test_get_valid_sales_inbounds_excludes_legacy_note_inbounds(self):
        legacy = self.create_inbound(32, remark="legacy")
        legacy.legacy_note = "Legacy inbound kept for old clients."
        legacy.save(update_fields=["legacy_note", "updated_at"])

        self.assertNotIn(legacy, list(get_valid_sales_inbounds(self.store)))

    def test_preview_bulk_plan_routes_counts_all_active_plans(self):
        self.create_plan("10 GB")
        self.create_plan("Inactive 10 GB", is_active=False)

        preview = preview_bulk_plan_routes(
            store=self.store,
            inbound=self.route_inbound,
            all_active=True,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )

        self.assertEqual(preview["plans_count"], 2)
        self.assertEqual(preview["to_create"], 2)
        self.assertEqual(preview["errors"], [])

    def test_preview_bulk_plan_routes_counts_selected_plan_ids(self):
        selected = self.create_plan("Selected 10 GB")
        ignored = self.create_plan("Ignored 10 GB")

        preview = preview_bulk_plan_routes(
            store=self.store,
            inbound=self.route_inbound,
            selected_plan_ids=[selected.pk],
            all_active=False,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )

        self.assertEqual(preview["plans_count"], 1)
        self.assertEqual(preview["selected_plan_ids"], [selected.pk])
        self.assertNotIn(ignored.pk, preview["selected_plan_ids"])

    def test_apply_bulk_plan_routes_update_existing_creates_and_updates(self):
        new_plan = self.create_plan("New 10 GB")
        existing_route = self.create_route(inbound=self.route_inbound, priority=10)

        result = apply_bulk_plan_routes(
            store=self.store,
            inbound=self.exact_inbound,
            selected_plan_ids=[self.plan.pk, new_plan.pk],
            all_active=False,
            priority=25,
            weight=3,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
            note="Updated from test",
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["created"], 1)
        existing_route.refresh_from_db()
        self.assertEqual(existing_route.inbound, self.exact_inbound)
        self.assertEqual(existing_route.priority, 25)
        self.assertEqual(existing_route.weight, 3)
        self.assertEqual(PlanInboundRoute.objects.get(plan=new_plan).inbound, self.exact_inbound)

    def test_apply_bulk_plan_routes_skip_existing_keeps_existing_route(self):
        new_plan = self.create_plan("Skip New 10 GB")
        existing_route = self.create_route(inbound=self.route_inbound, priority=10)

        result = apply_bulk_plan_routes(
            store=self.store,
            inbound=self.exact_inbound,
            selected_plan_ids=[self.plan.pk, new_plan.pk],
            all_active=False,
            priority=50,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_SKIP_EXISTING,
        )

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 1)
        existing_route.refresh_from_db()
        self.assertEqual(existing_route.inbound, self.route_inbound)
        self.assertEqual(PlanInboundRoute.objects.get(plan=new_plan).inbound, self.exact_inbound)

    def test_apply_bulk_plan_routes_replace_active_deactivates_old_routes(self):
        replacement_inbound = self.create_inbound(40, remark="replacement")
        old_route = self.create_route(inbound=self.route_inbound)
        second_old_route = self.create_route(inbound=self.exact_inbound, priority=200)

        result = apply_bulk_plan_routes(
            store=self.store,
            inbound=replacement_inbound,
            selected_plan_ids=[self.plan.pk],
            all_active=False,
            priority=5,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
        )

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["deactivated"], 2)
        old_route.refresh_from_db()
        second_old_route.refresh_from_db()
        self.assertFalse(old_route.is_active)
        self.assertFalse(second_old_route.is_active)
        self.assertTrue(PlanInboundRoute.objects.get(plan=self.plan, inbound=replacement_inbound).is_active)

    def test_apply_bulk_plan_routes_skips_operator_not_enabled_on_plan(self):
        other_operator = Operator.objects.create(store=self.store, name="رایتل", slug="rightel")

        result = apply_bulk_plan_routes(
            store=self.store,
            inbound=self.route_inbound,
            operator=other_operator,
            selected_plan_ids=[self.plan.pk],
            all_active=False,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )

        self.assertEqual(result["skipped"], 1)
        self.assertFalse(PlanInboundRoute.objects.filter(plan=self.plan, operator=other_operator).exists())
        self.assertTrue(any("operator is not enabled" in warning for warning in result["warnings"]))

    def test_apply_bulk_plan_routes_creates_exact_operator_route(self):
        result = apply_bulk_plan_routes(
            store=self.store,
            inbound=self.exact_inbound,
            operator=self.operator_a,
            selected_plan_ids=[self.plan.pk],
            all_active=False,
            priority=30,
            weight=2,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )

        self.assertEqual(result["created"], 1)
        route = PlanInboundRoute.objects.get(plan=self.plan, operator=self.operator_a)
        self.assertEqual(route.inbound, self.exact_inbound)
        self.assertEqual(route.priority, 30)
        self.assertEqual(route.weight, 2)

    def test_preview_bulk_plan_routes_reports_invalid_inbound(self):
        invalid_inbound = self.create_inbound(41, remark="legacy", available=False)

        preview = preview_bulk_plan_routes(
            store=self.store,
            inbound=invalid_inbound,
            selected_plan_ids=[self.plan.pk],
            all_active=False,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )

        self.assertTrue(preview["errors"])
        self.assertIn("not available for new orders", " ".join(preview["errors"]))

    def test_apply_bulk_plan_routes_calls_route_full_clean(self):
        with patch("store.plan_route_services.PlanInboundRoute.full_clean", autospec=True) as full_clean:
            apply_bulk_plan_routes(
                store=self.store,
                inbound=self.route_inbound,
                selected_plan_ids=[self.plan.pk],
                all_active=False,
                priority=100,
                weight=1,
                existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
            )

        self.assertTrue(full_clean.called)

    def test_apply_bulk_plan_routes_rolls_back_on_apply_error(self):
        from . import plan_route_services

        second_plan = self.create_plan("Rollback 10 GB")
        original_apply_plan = plan_route_services.apply_plan_route_operation
        calls = {"count": 0}

        def flaky_apply_plan(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise ValidationError("boom")
            return original_apply_plan(*args, **kwargs)

        with patch("store.plan_route_services.apply_plan_route_operation", side_effect=flaky_apply_plan):
            with self.assertRaises(ValidationError):
                apply_bulk_plan_routes(
                    store=self.store,
                    inbound=self.route_inbound,
                    selected_plan_ids=[self.plan.pk, second_plan.pk],
                    all_active=False,
                    priority=100,
                    weight=1,
                    existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
                )

        self.assertFalse(PlanInboundRoute.objects.filter(plan__in=[self.plan, second_plan]).exists())

    def test_plan_inbound_route_validates_successfully(self):
        route = PlanInboundRoute(
            store=self.store,
            plan=self.plan,
            inbound=self.route_inbound,
            priority=10,
        )

        route.full_clean()

    def test_plan_inbound_route_rejects_legacy_inbound(self):
        self.route_inbound.available_for_new_orders = False
        self.route_inbound.legacy_note = "Legacy inbound kept for old orders."
        self.route_inbound.save(update_fields=["available_for_new_orders", "legacy_note", "updated_at"])
        route = PlanInboundRoute(store=self.store, plan=self.plan, inbound=self.route_inbound)

        with self.assertRaises(ValidationError) as ctx:
            route.full_clean()

        self.assertIn("inbound", ctx.exception.message_dict)

    def test_plan_inbound_route_rejects_operator_not_enabled_on_plan(self):
        other_operator = Operator.objects.create(store=self.store, name="رایتل", slug="rightel")
        route = PlanInboundRoute(
            store=self.store,
            plan=self.plan,
            operator=other_operator,
            inbound=self.route_inbound,
        )

        with self.assertRaises(ValidationError) as ctx:
            route.full_clean()

        self.assertIn("operator", ctx.exception.message_dict)

    def test_selector_uses_general_route_in_tunnel_mode(self):
        self.create_route(inbound=self.route_inbound)

        inbound = select_inbound_for_plan(self.plan, store=self.store, quantity=1)

        self.assertEqual(inbound, self.route_inbound)

    def test_selector_uses_route_when_fallback_disabled_and_route_complete(self):
        self.store.allow_global_inbound_fallback = False
        self.store.save(update_fields=["allow_global_inbound_fallback", "updated_at"])
        self.create_route(inbound=self.route_inbound)

        inbound = select_inbound_for_plan(self.plan, store=self.store, quantity=1)

        self.assertEqual(inbound, self.route_inbound)

    def test_selector_uses_exact_operator_route_first(self):
        self.store.sales_mode = Store.SalesMode.OPERATOR_BASED
        self.store.save(update_fields=["sales_mode", "updated_at"])
        self.create_route(inbound=self.route_inbound)
        self.create_route(operator=self.operator_a, inbound=self.exact_inbound, priority=50)

        inbound = select_inbound_for_plan(
            self.plan,
            store=self.store,
            operator=self.operator_a,
            quantity=1,
        )

        self.assertEqual(inbound, self.exact_inbound)

    def test_selector_falls_back_to_general_route_for_operator_without_exact_route(self):
        self.create_route(inbound=self.route_inbound)

        inbound = select_inbound_for_plan(
            self.plan,
            store=self.store,
            operator=self.operator_b,
            quantity=1,
        )

        self.assertEqual(inbound, self.route_inbound)

    def test_selector_uses_global_fallback_when_route_missing_and_allowed(self):
        inbound = select_inbound_for_plan(self.plan, store=self.store, quantity=1)

        self.assertEqual(inbound, self.fallback_inbound)

    def test_selector_raises_persian_error_when_route_missing_and_fallback_disabled(self):
        self.store.allow_global_inbound_fallback = False
        self.store.save(update_fields=["allow_global_inbound_fallback", "updated_at"])

        with self.assertRaises(ValidationError) as ctx:
            select_inbound_for_plan(self.plan, store=self.store, quantity=1)

        self.assertIn("مسیر سرور/اینباند", ctx.exception.messages[0])

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_new_order_saves_route_inbound(self, xui_mock):
        self.create_route(inbound=self.route_inbound)

        result = create_manual_payment_order(**self.order_kwargs(bank_tracking_code="ROUTE2"))

        self.assertTrue(result.success)
        self.assertEqual(result.order.inbound, self.route_inbound)
        self.assertEqual(result.vpn_client.inbound, self.route_inbound)
        self.assertEqual(xui_mock.call_args.kwargs["inbound"], self.route_inbound)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_bot_operator_based_purchase_uses_exact_route(self, _xui):
        from .bots import create_bot_payment_order

        self.store.sales_mode = Store.SalesMode.OPERATOR_BASED
        self.store.save(update_fields=["sales_mode", "updated_at"])
        self.create_route(operator=self.operator_a, inbound=self.exact_inbound)
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Customer bot",
            bot_token="123:secret",
            admin_user_id="1",
            is_active=True,
        )
        bot_user = BotUser.objects.create(
            bot_config=bot_config,
            provider_user_id="42",
            chat_id="42",
            state_data={
                "operator_id": self.operator_a.pk,
                "sender_card_name": "Alice Buyer",
                "payment_time": "14:35",
                "quantity": 1,
            },
        )

        result = create_bot_payment_order(
            config=bot_config,
            bot_user=bot_user,
            plan=self.plan,
            metadata={"source": "bot_purchase"},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.order.operator, self.operator_a)
        self.assertEqual(result.order.inbound, self.exact_inbound)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result())
    def test_web_tunnel_purchase_uses_plan_route(self, _xui):
        self.create_route(inbound=self.route_inbound)
        receipt = SimpleUploadedFile("receipt.png", image_bytes("PNG"), content_type="image/png")

        response = self.client.post(
            reverse("home"),
            data={
                "plan_id": str(self.plan.pk),
                "sender_card_name": "Alice Buyer",
                "payment_time": "14:35",
                "quantity": "1",
                "payment_receipt_image": receipt,
            },
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.inbound, self.route_inbound)

    def test_renewal_order_keeps_existing_client_inbound(self):
        self.create_route(inbound=self.route_inbound)
        original_order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=self.fallback_inbound,
            username="existing_user",
            uuid="44444444-4444-4444-8444-444444444444",
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=original_order,
            plan=self.plan,
            inbound=self.fallback_inbound,
            username="existing_user",
            xui_email="existing_user",
            uuid="44444444-4444-4444-8444-444444444444",
            status=VPNClient.Status.ACTIVE,
        )
        from .order_services import create_renewal_payment_order

        result = create_renewal_payment_order(customer=None, vpn_client=vpn_client)

        self.assertTrue(result.success)
        self.assertEqual(result.order.inbound, self.fallback_inbound)
        self.assertEqual(VPNClient.objects.count(), 1)

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.xui_api.create_inactive_client_details", return_value=fake_client_result("55555555-5555-4555-8555-555555555555"))
    def test_deferred_activation_selects_route_when_order_has_no_inbound(self, _create_client, _enable_client):
        self.create_route(inbound=self.route_inbound)
        order = Order.objects.create(
            store=self.store,
            plan=self.plan,
            inbound=None,
            username="deferred_user",
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
        )

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        vpn_client = VPNClient.objects.get(order=order)
        self.assertEqual(vpn_client.inbound, self.route_inbound)
        order.refresh_from_db()
        self.assertEqual(order.inbound, self.route_inbound)

    def test_legacy_route_is_not_selected_for_new_sales(self):
        self.route_inbound.available_for_new_orders = False
        self.route_inbound.legacy_note = "Legacy inbound kept for old clients."
        self.route_inbound.save(update_fields=["available_for_new_orders", "legacy_note", "updated_at"])
        self.create_route(inbound=self.route_inbound)

        inbound = select_inbound_for_plan(self.plan, store=self.store, quantity=1)

        self.assertEqual(inbound, self.fallback_inbound)

    def test_check_integrations_warns_for_missing_route_when_fallback_enabled(self):
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        self.assertIn("will use global fallback", out.getvalue())

    def test_check_integrations_errors_for_missing_route_when_fallback_disabled(self):
        self.store.allow_global_inbound_fallback = False
        self.store.save(update_fields=["allow_global_inbound_fallback", "updated_at"])
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        self.assertIn("fallback is disabled", out.getvalue())

    def test_check_integrations_errors_for_invalid_route(self):
        self.route_inbound.available_for_new_orders = False
        self.route_inbound.save(update_fields=["available_for_new_orders", "updated_at"])
        self.create_route(inbound=self.route_inbound)
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        self.assertIn("Invalid active route", out.getvalue())

    def test_check_integrations_has_no_missing_route_after_bulk_assign(self):
        self.store.allow_global_inbound_fallback = False
        self.store.save(update_fields=["allow_global_inbound_fallback", "updated_at"])
        apply_bulk_plan_routes(
            store=self.store,
            inbound=self.route_inbound,
            all_active=True,
            priority=100,
            weight=1,
            existing_strategy=BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
        )
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("All active plans/operators have explicit routes", output)
        self.assertNotIn("fallback is disabled", output)

    def test_admin_bulk_assign_view_loads_get_form(self):
        self.login_admin()

        response = self.client.get(reverse("admin:store_planinboundroute_bulk_assign"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تنظیم گروهی مسیر فروش پلن‌ها")
        self.assertContains(response, "Preview")

    def test_admin_bulk_assign_view_shows_preview(self):
        self.login_admin()

        response = self.client.post(
            reverse("admin:store_planinboundroute_bulk_assign"),
            data={
                "store": str(self.store.pk),
                "inbound": str(self.route_inbound.pk),
                "operator": "",
                "plan_selection_mode": "all_active",
                "priority": "100",
                "weight": "1",
                "existing_strategy": BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
                "note": "Admin preview",
                "_preview": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پلن‌های انتخاب‌شده")
        self.assertContains(response, "تایید و اعمال routeها")

    def test_admin_bulk_assign_view_applies_routes(self):
        self.login_admin()

        response = self.client.post(
            reverse("admin:store_planinboundroute_bulk_assign"),
            data={
                "store": str(self.store.pk),
                "inbound": str(self.route_inbound.pk),
                "operator": "",
                "plan_selection_mode": "all_active",
                "priority": "100",
                "weight": "1",
                "existing_strategy": BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
                "note": "Admin apply",
                "_apply": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PlanInboundRoute.objects.filter(plan=self.plan, inbound=self.route_inbound).exists())
        self.assertContains(response, "Routeها با موفقیت اعمال شدند")

    def test_plan_admin_action_redirects_selected_plans_to_bulk_assign(self):
        self.login_admin()

        response = self.client.post(
            reverse("admin:store_plan_changelist"),
            data={
                "action": "bulk_assign_inbound_routes",
                "_selected_action": [str(self.plan.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:store_planinboundroute_bulk_assign"), response["Location"])
        self.assertIn(f"plan_ids={self.plan.pk}", response["Location"])

    def test_audit_plan_inbound_routes_reports_missing_routes(self):
        out = StringIO()

        call_command("audit_plan_inbound_routes", "--store-id", str(self.store.pk), stdout=out)

        self.assertIn("missing_general_routes=1", out.getvalue())
        self.assertIn("Admin > Plan Inbound Routes > Bulk Assign", out.getvalue())

    def test_audit_plan_inbound_routes_fix_default_creates_route_when_one_healthy_inbound(self):
        store = Store.objects.create(
            name="Seed Store",
            english_name="Seed Store",
            card_number="0000000000000000",
            card_owner="Seed",
        )
        plan = Plan.objects.create(
            store=store,
            name="Seed 1 GB",
            slug="seed-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Seed Panel",
            url="https://seed-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        inbound = Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=True,
        )
        out = StringIO()

        call_command("audit_plan_inbound_routes", "--store-id", str(store.pk), "--fix-default", stdout=out)

        route = PlanInboundRoute.objects.get(plan=plan)
        self.assertEqual(route.inbound, inbound)
        self.assertIn("created route", out.getvalue())

    def test_audit_plan_inbound_routes_fix_default_does_not_guess_with_multiple_inbounds(self):
        out = StringIO()

        call_command("audit_plan_inbound_routes", "--store-id", str(self.store.pk), "--fix-default", stdout=out)

        self.assertFalse(PlanInboundRoute.objects.filter(plan=self.plan).exists())
        self.assertIn("exactly one is required", out.getvalue())

    def test_plan_admin_loads_route_inline(self):
        from django.contrib import admin
        from .admin import PlanAdmin, PlanInboundRouteInline

        model_admin = PlanAdmin(Plan, admin.site)

        self.assertIn(PlanInboundRouteInline, model_admin.inlines)

    def test_inbound_admin_active_route_count(self):
        from django.contrib import admin
        from .admin import InboundAdmin

        self.create_route(inbound=self.route_inbound)
        model_admin = InboundAdmin(Inbound, admin.site)
        inbound = model_admin.get_queryset(None).get(pk=self.route_inbound.pk)

        self.assertEqual(model_admin.active_plan_route_count(inbound), 1)


class FreeTrialServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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

    def test_store_validation_rejects_trial_inbound_unavailable_for_new_orders(self):
        self.inbound.available_for_new_orders = False
        self.inbound.save(update_fields=["available_for_new_orders", "updated_at"])

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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000001",
            card_owner="VPN Store",
            bank_name="Bank One",
        )
        self.second_store = Store.objects.create(
            name="Second",
            english_name="Second",
            slug="second",
            card_number="0000000000000002",
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
        self.store.card_number = "0000000000000009"
        self.store.save(update_fields=["card_number", "updated_at"])
        self.create_paid_order(amount=50000)

        response = self.client.get(reverse("admin_card_receipts_report"), {"card": old_card})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["grand_total_irr"], 1000000)
        self.assertEqual(response.context["card_summaries"][0]["card_owner"], "Old Owner")
        self.assertEqual(len(response.context["order_rows"]), 1)


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminSetupCenterTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="setup-admin",
            email="setup-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Qasedak",
            english_name="Qasedak",
            slug="qasedak",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def test_setup_center_url_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "راه‌اندازی قاصدک")
        self.assertContains(response, "Revenue Engine")

    def test_setup_center_requires_staff_login(self):
        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_minimal_install_setup_center_has_warnings_not_500(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "نیاز به بررسی")
        self.assertNotContains(response, "qadmin-status-error")

    def test_active_plan_without_route_is_error(self):
        Plan.objects.create(
            store=self.store,
            name="Starter",
            slug="starter",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "qadmin-status-error")
        self.assertContains(response, "route معتبر ندارد")

    def test_store_admin_fieldsets_are_grouped_for_owner_and_power_user(self):
        from django.contrib import admin as django_admin

        store_admin = django_admin.site._registry[Store]
        request = SimpleNamespace(user=self.admin_user)

        fieldsets = store_admin.get_fieldsets(request, self.store)
        fieldset_names = [str(name) for name, _options in fieldsets]

        self.assertIn("Quick Setup / وضعیت کلی", fieldset_names)
        self.assertIn("Brand / Identity", fieldset_names)
        self.assertIn("Payment", fieldset_names)
        self.assertIn("Customer / Sales Settings", fieldset_names)
        self.assertIn("Telegram / Bot Related", fieldset_names)
        self.assertIn("Reminders / Reports / Monitoring", fieldset_names)

        options_by_name = {str(name): options for name, options in fieldsets}
        self.assertIn("collapse", options_by_name["Revenue Engine Controls"]["classes"])
        self.assertIn("collapse", options_by_name["Advanced / Legacy"]["classes"])

    def test_setup_center_does_not_render_full_secrets(self):
        secret_card = "621986" + "1234567890"
        secret_token = "123456" + ":" + "telegram-secret-token"
        secret_password = "panel-super-secret"
        secret_proxy = "http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:8080"
        self.store.card_number = secret_card
        self.store.card_owner = "Owner"
        self.store.save(update_fields=["card_number", "card_owner", "updated_at"])
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="qasedak_bot",
            bot_token=secret_token,
            admin_user_id="999",
            is_active=True,
        )
        Panel.objects.create(
            store=self.store,
            name="Primary panel",
            url="https://panel.example.com/admin",
            username="panel-admin",
            password=secret_password,
            proxy_url=secret_proxy,
            is_active=True,
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"))
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret_card, body)
        self.assertNotIn(secret_token, body)
        self.assertNotIn(secret_password, body)
        self.assertNotIn(secret_proxy, body)
        self.assertNotIn("proxy-secret", body)

    def test_bot_configuration_admin_masks_token_and_webhook_secret(self):
        bot_token = "123456" + ":" + "telegram-secret-token"
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="qasedak_bot",
            bot_token=bot_token,
            admin_user_id="999",
            is_active=True,
        )
        self.login_admin()

        response = self.client.get(reverse("admin:store_botconfiguration_change", args=[bot_config.pk]))
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(bot_token, body)
        self.assertNotIn(bot_config.webhook_secret, body)
        self.assertIn("/bot/telegram/&lt;hidden&gt;/webhook/", body)

    def test_setup_center_does_not_call_live_integrations(self):
        self.login_admin()

        with patch("store.telegram_bot.client.BotClient.get_me") as get_me_mock, patch(
            "store.xui_api.login_to_panel"
        ) as login_mock:
            response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 200)
        get_me_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_check_integrations_minimal_setup_behavior_still_warns_without_errors(self):
        stdout = StringIO()

        call_command("check_integrations", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Setup incomplete: no active X-UI panel exists yet", output)
        self.assertIn("ERROR=0", output)


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminOwnerDashboardTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="owner-admin",
            email="owner-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Owner Store",
            english_name="Owner Store",
            slug="owner-store",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Owner 1GB",
            slug="owner-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def create_order(self, *, status=Order.Status.PENDING_PAYMENT, is_paid=False, amount=100000, **kwargs):
        data = {
            "store": self.store,
            "plan": self.plan,
            "amount": amount,
            "original_amount": amount,
            "currency": Plan.Currency.TOMAN,
            "status": status,
            "is_paid": is_paid,
        }
        data.update(kwargs)
        return Order.objects.create(**data)

    def test_owner_dashboard_url_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "داشبورد قاصدک")
        self.assertContains(response, "سفارش‌های امروز")

    def test_owner_dashboard_requires_staff_login(self):
        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="owner-regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_owner_dashboard_minimal_install_without_integrations_does_not_500(self):
        BotConfiguration.objects.all().delete()
        Panel.objects.all().delete()
        Plan.objects.all().delete()
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "پنل ندارد")
        self.assertContains(response, "تنظیم نشده")

    def test_owner_dashboard_does_not_render_full_secrets(self):
        secret_card = "621986" + "1234567890"
        secret_token = "123456" + ":" + "telegram-secret-token"
        secret_password = "panel-super-secret"
        secret_proxy = "http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:8080"
        self.store.card_number = secret_card
        self.store.card_owner = "Owner"
        self.store.save(update_fields=["card_number", "card_owner", "updated_at"])
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="owner_bot",
            bot_token=secret_token,
            admin_user_id="999",
            is_active=True,
        )
        Panel.objects.create(
            store=self.store,
            name="Primary panel",
            url="https://panel.example.com/admin",
            username="panel-admin",
            password=secret_password,
            proxy_url=secret_proxy,
            is_active=True,
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret_card, body)
        self.assertNotIn(secret_token, body)
        self.assertNotIn(secret_password, body)
        self.assertNotIn(secret_proxy, body)
        self.assertNotIn("proxy-secret", body)

    def test_pending_order_metric_counts_pending_statuses(self):
        self.create_order(status=Order.Status.PENDING_PAYMENT)
        self.create_order(status=Order.Status.PENDING_VERIFICATION, payment_submitted_at=timezone.now())
        self.create_order(status=Order.Status.CONFIRMED, is_paid=True)
        self.create_order(status=Order.Status.COMPLETED, is_paid=True)
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["order_metrics"]["pending"], 3)

    def test_completed_revenue_metric_uses_paid_completed_orders(self):
        self.create_order(status=Order.Status.COMPLETED, is_paid=True, amount=200000)
        self.create_order(status=Order.Status.COMPLETED, is_paid=False, amount=900000)
        self.create_order(status=Order.Status.CONFIRMED, is_paid=True, amount=700000)
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["revenue_metrics"]["today"]["by_currency"][Plan.Currency.TOMAN], 200000)

    def test_expiring_client_metric_counts_next_three_days(self):
        VPNClient.objects.create(
            store=self.store,
            plan=self.plan,
            username="expiring-client",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=10_000,
            used_traffic_bytes=1_000,
            duration_days=30,
            expires_at=timezone.now() + timedelta(days=2),
        )
        VPNClient.objects.create(
            store=self.store,
            plan=self.plan,
            username="later-client",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=10_000,
            used_traffic_bytes=1_000,
            duration_days=30,
            expires_at=timezone.now() + timedelta(days=10),
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["client_metrics"]["expiring_soon"], 1)

    def test_revenue_engine_summary_shows_dry_run_safe_state(self):
        self.store.revenue_engine_dry_run = True
        self.store.save(update_fields=["revenue_engine_dry_run", "updated_at"])
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["revenue_engine_summary"]["dry_run"])
        self.assertContains(response, "Dry-run")

    def test_action_items_are_created_for_missing_setup(self):
        BotConfiguration.objects.all().delete()
        Panel.objects.all().delete()
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"))
        titles = [item.title for item in response.context["action_items"]]

        self.assertEqual(response.status_code, 200)
        self.assertIn("Route پلن‌ها ناقص است", titles)
        self.assertIn("پنل X-UI/Sanaei را اضافه کن", titles)
        self.assertIn("BotConfiguration تلگرام را کامل کن", titles)

    def test_owner_dashboard_does_not_call_live_integrations(self):
        self.login_admin()

        with patch("store.telegram_bot.client.BotClient.get_me") as get_me_mock, patch(
            "store.xui_api.login_to_panel"
        ) as login_mock:
            response = self.client.get(reverse("admin_store_owner_dashboard"))

        self.assertEqual(response.status_code, 200)
        get_me_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_setup_center_links_back_to_owner_dashboard(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_owner_dashboard"))

    def test_admin_index_and_store_changelist_link_owner_dashboard(self):
        self.login_admin()

        index_response = self.client.get(reverse("admin:index"))
        changelist_response = self.client.get(reverse("admin:store_store_changelist"))

        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(changelist_response.status_code, 200)
        self.assertContains(index_response, reverse("admin_store_owner_dashboard"))
        self.assertContains(changelist_response, reverse("admin_store_owner_dashboard"))


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminRevenueControlCenterTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="revenue-admin",
            email="revenue-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Revenue Store",
            english_name="Revenue Store",
            slug="revenue-store",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Revenue Telegram",
            bot_token="123456" + ":" + "revenue-token",
            admin_user_id="999",
            is_active=True,
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def revenue_url(self):
        return reverse("admin_store_revenue_control")

    def create_offer_log(self, **extra):
        data = {
            "store": self.store,
            "engine_type": RevenueOfferLog.EngineType.RETENTION,
            "event_type": "user_inactive_72h",
            "offer_type": "retention",
            "decision_source": RevenueOfferLog.DecisionSource.AI,
            "status": RevenueOfferLog.Status.DRY_RUN,
            "metadata": {"safe": "ok"},
        }
        data.update(extra)
        return RevenueOfferLog.objects.create(**data)

    def test_revenue_control_center_url_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(self.revenue_url(), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "کنترل درآمد هوشمند")
        self.assertContains(response, "Revenue Engine Control Center")
        self.assertContains(response, "Command hints")

    def test_revenue_control_center_requires_staff_login(self):
        response = self.client.get(self.revenue_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="revenue-regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(self.revenue_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_revenue_control_center_fresh_install_without_logs_does_not_500(self):
        RevenueOfferLog.objects.all().delete()
        self.login_admin()

        response = self.client.get(self.revenue_url(), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RevenueOfferLog هنوز داده‌ای ندارد")

    def test_revenue_control_center_does_not_render_full_secrets(self):
        secret_card = "621986" + "1234567890"
        secret_token = "123456" + ":" + "telegram-secret-token"
        secret_uuid = "11111111" + "-2222-3333-4444-" + "555555555555"
        secret_config = "vless://" + secret_uuid + "@example.com:443"
        secret_phone = "0912" + "3456789"
        secret_email = "customer-secret@example.com"
        self.store.card_number = secret_card
        self.store.card_owner = "Owner"
        self.store.save(update_fields=["card_number", "card_owner", "updated_at"])
        self.bot_config.bot_token = secret_token
        self.bot_config.save(update_fields=["bot_token", "updated_at"])
        customer = Customer.objects.create(
            username=secret_email,
            phone_number=secret_phone,
            display_name="Secret Customer",
        )
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="98765" + "43210",
            chat_id="98765" + "43210",
            username="secret_user",
            display_name="Secret User",
        )
        self.create_offer_log(
            customer=customer,
            bot_user=bot_user,
            metadata={
                "config_link": secret_config,
                "uuid": secret_uuid,
                "phone": secret_phone,
                "email": secret_email,
                "token": secret_token,
            },
        )
        self.login_admin()

        response = self.client.get(self.revenue_url(), {"store": self.store.pk})
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret_card, body)
        self.assertNotIn(secret_token, body)
        self.assertNotIn(secret_config, body)
        self.assertNotIn(secret_uuid, body)
        self.assertNotIn(secret_phone, body)
        self.assertNotIn(secret_email, body)

    def test_revenue_control_center_get_does_not_call_live_integrations_or_scan(self):
        self.login_admin()

        with patch("store.revenue_engine.scheduler.run_revenue_scan") as scan_mock, patch(
            "store.telegram_bot.client.BotClient.get_me"
        ) as get_me_mock, patch("store.xui_api.login_to_panel") as login_mock:
            response = self.client.get(self.revenue_url(), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        scan_mock.assert_not_called()
        get_me_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_enable_dry_run_action_only_accepts_post(self):
        self.store.revenue_engine_dry_run = False
        self.store.save(update_fields=["revenue_engine_dry_run", "updated_at"])
        self.login_admin()

        response = self.client.get(self.revenue_url(), {"store": self.store.pk, "action": "enable_dry_run"})
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.revenue_engine_dry_run)

        response = self.client.post(self.revenue_url(), {"store": self.store.pk, "action": "enable_dry_run"})
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.store.revenue_engine_dry_run)

    def test_disable_revenue_action_only_accepts_post(self):
        self.store.revenue_engine_enabled = True
        self.store.save(update_fields=["revenue_engine_enabled", "updated_at"])
        self.login_admin()

        response = self.client.get(
            self.revenue_url(),
            {"store": self.store.pk, "action": "disable_revenue", "confirmation": "DISABLE_REVENUE_ENGINE"},
        )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.store.revenue_engine_enabled)

        response = self.client.post(
            self.revenue_url(),
            {"store": self.store.pk, "action": "disable_revenue", "confirmation": "DISABLE_REVENUE_ENGINE"},
        )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.store.revenue_engine_enabled)

    def test_reset_safe_defaults_sets_dry_run_true(self):
        self.store.revenue_engine_dry_run = False
        self.store.revenue_max_total_offers_per_day = 500
        self.store.revenue_min_ai_confidence = Decimal("0.10")
        self.store.save(
            update_fields=[
                "revenue_engine_dry_run",
                "revenue_max_total_offers_per_day",
                "revenue_min_ai_confidence",
                "updated_at",
            ]
        )
        self.login_admin()

        response = self.client.post(
            self.revenue_url(),
            {"store": self.store.pk, "action": "reset_safe_defaults", "confirmation": "RESET_REVENUE_SAFE_DEFAULTS"},
        )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.store.revenue_engine_enabled)
        self.assertTrue(self.store.revenue_engine_dry_run)
        self.assertEqual(self.store.revenue_max_total_offers_per_day, 100)
        self.assertEqual(self.store.revenue_min_ai_confidence, Decimal("0.50"))

    def test_enable_real_send_without_confirmation_does_not_activate(self):
        self.login_admin()

        response = self.client.post(
            self.revenue_url(),
            {"store": self.store.pk, "action": "enable_real_send", "confirmation": "WRONG"},
        )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.store.revenue_engine_dry_run)

    def test_enable_real_send_with_unsafe_conditions_does_not_activate(self):
        self.login_admin()

        with patch(
            "store.admin_views.get_real_send_safety",
            return_value={"safe": False, "blocking": ["unsafe"], "warnings": [], "target_coverage": {}},
        ):
            response = self.client.post(
                self.revenue_url(),
                {
                    "store": self.store.pk,
                    "action": "enable_real_send",
                    "confirmation": "ENABLE_REAL_REVENUE_SEND",
                },
            )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.store.revenue_engine_dry_run)

    def test_enable_real_send_with_safe_conditions_and_confirmation_sets_dry_run_false(self):
        self.login_admin()

        with patch(
            "store.admin_views.get_real_send_safety",
            return_value={"safe": True, "blocking": [], "warnings": [], "target_coverage": {}},
        ):
            response = self.client.post(
                self.revenue_url(),
                {
                    "store": self.store.pk,
                    "action": "enable_real_send",
                    "confirmation": "ENABLE_REAL_REVENUE_SEND",
                },
            )
        self.store.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.store.revenue_engine_enabled)
        self.assertFalse(self.store.revenue_engine_dry_run)

    def test_dashboard_links_to_revenue_control_center(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_revenue_control"))
        self.assertContains(response, "کنترل درآمد هوشمند")

    def test_store_admin_links_to_revenue_control_center(self):
        self.login_admin()

        response = self.client.get(reverse("admin:store_store_change", args=[self.store.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_revenue_control"))
        self.assertContains(response, "کنترل درآمد هوشمند")

    def test_revenue_metrics_from_offer_logs_are_correct(self):
        from .admin_revenue import get_revenue_metrics

        self.create_offer_log(status=RevenueOfferLog.Status.DRY_RUN)
        self.create_offer_log(status=RevenueOfferLog.Status.SENT)
        self.create_offer_log(status=RevenueOfferLog.Status.CONVERTED)
        self.create_offer_log(status=RevenueOfferLog.Status.FAILED)
        self.create_offer_log(status=RevenueOfferLog.Status.SKIPPED)
        self.create_offer_log(status=RevenueOfferLog.Status.SUPPRESSED)

        metrics = get_revenue_metrics(store=self.store, days=7)

        self.assertEqual(metrics["total"], 6)
        self.assertEqual(metrics["dry_run"], 1)
        self.assertEqual(metrics["sent"], 2)
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["skipped_suppressed"], 2)
        self.assertEqual(metrics["conversions"], 1)
        self.assertEqual(metrics["conversion_rate"], Decimal("50.0"))

    def test_failed_logs_are_shown_in_action_items(self):
        from .admin_revenue import get_revenue_action_items, get_revenue_metrics, get_real_send_safety

        self.create_offer_log(status=RevenueOfferLog.Status.FAILED)

        metrics = get_revenue_metrics(store=self.store, days=7)
        safety = get_real_send_safety(self.store)
        items = get_revenue_action_items(self.store, metrics=metrics, safety=safety)
        titles = [item["title"] for item in items]

        self.assertIn("failed logs نیاز به بررسی دارند", titles)


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminOrderWorkbenchTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="orders-admin",
            email="orders-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Orders Store",
            english_name="Orders Store",
            slug="orders-store",
            card_number="621986" + "1234567890",
            card_owner="Orders Owner",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Orders 1GB",
            slug="orders-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.customer = Customer.objects.create(
            display_name="Alice Buyer",
            phone_number="+989121234567",
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def create_order(self, **kwargs):
        data = {
            "store": self.store,
            "customer": self.customer,
            "plan": self.plan,
            "amount": 100000,
            "original_amount": 100000,
            "currency": Plan.Currency.TOMAN,
            "payment_method": Order.PaymentMethod.MANUAL_CARD,
            "status": Order.Status.PENDING_VERIFICATION,
            "verification_status": Order.VerificationStatus.PENDING,
            "is_paid": True,
            "payment_submitted_at": timezone.now(),
            "payment_receipt_image": "payment_receipts/safe-receipt.png",
        }
        data.update(kwargs)
        return Order.objects.create(**data)

    def create_inbound(self):
        panel = Panel.objects.create(
            store=self.store,
            name="Order Panel",
            url="https://panel.example.com/admin",
            username="panel-admin",
            password="panel-secret-password",
            is_active=True,
        )
        return Inbound.objects.create(
            panel=panel,
            inbound_id=7,
            remark="Main inbound",
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=True,
        )

    def test_workbench_url_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "میز کار سفارش‌ها")

    def test_workbench_requires_staff_login(self):
        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="orders-regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_fresh_install_without_orders_does_not_500(self):
        Order.objects.all().delete()
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "موردی در این صف نیست")

    def test_pending_order_appears_in_needs_review_section(self):
        order = self.create_order()
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary_counts"]["needs_review"], 1)
        self.assertContains(response, order.order_tracking_code)

    def test_completed_order_counts_in_completed_section(self):
        self.create_order(status=Order.Status.COMPLETED, verification_status=Order.VerificationStatus.VERIFIED)
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary_counts"]["completed"], 1)

    def test_order_review_page_loads_for_superuser(self):
        order = self.create_order()
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_review", args=[order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_tracking_code)
        self.assertContains(response, "خلاصه سفارش")

    def test_review_page_does_not_leak_sensitive_values(self):
        order = self.create_order(
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sub_link="https://example.com/sub/SECRET-SUB-TOKEN",
            direct_link="vless://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@vpn.example.com:443#Alice",
            metadata={
                "payment_destination_card_number": self.store.card_number,
                "receipt_text": f"card {self.store.card_number} token SECRET-SUB-TOKEN",
                "receipt_analysis": {"status": "matched", "matched_amount_irr": 1000000},
            },
        )
        inbound = self.create_inbound()
        VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=inbound,
            username="alice-config",
            xui_email="alice@example.com",
            uuid=order.uuid,
            sub_link=order.sub_link,
            direct_link=order.direct_link,
            status=VPNClient.Status.INACTIVE,
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_review", args=[order.pk]))
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.store.card_number, body)
        self.assertNotIn(self.customer.phone_number, body)
        self.assertNotIn("SECRET-SUB-TOKEN", body)
        self.assertNotIn("vless://", body)
        self.assertNotIn("alice@example.com", body)
        self.assertIn("+989***67", body)

    def test_get_review_page_has_no_side_effects(self):
        order = self.create_order()
        self.login_admin()

        with patch("store.admin_views.activate_order") as activate_mock, patch("store.admin_views.reject_order") as reject_mock:
            response = self.client.get(reverse("admin_store_order_review", args=[order.pk]), {"action": "approve"})

        self.assertEqual(response.status_code, 200)
        activate_mock.assert_not_called()
        reject_mock.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)

    def test_approve_action_requires_post_and_confirmation(self):
        order = self.create_order()
        self.login_admin()

        with patch("store.admin_views.activate_order") as activate_mock:
            get_response = self.client.get(reverse("admin_store_order_review", args=[order.pk]), {"action": "approve"})
            post_response = self.client.post(
                reverse("admin_store_order_review", args=[order.pk]),
                {"action": "approve"},
            )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 302)
        activate_mock.assert_not_called()

    def test_reject_action_requires_post_confirmation_and_reason(self):
        order = self.create_order()
        self.login_admin()

        with patch("store.admin_views.reject_order") as reject_mock:
            get_response = self.client.get(reverse("admin_store_order_review", args=[order.pk]), {"action": "reject"})
            missing_reason_response = self.client.post(
                reverse("admin_store_order_review", args=[order.pk]),
                {"action": "reject", "confirm_external": "1"},
            )
            ok_response = self.client.post(
                reverse("admin_store_order_review", args=[order.pk]),
                {"action": "reject", "confirm_external": "1", "reason": "receipt mismatch"},
            )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(missing_reason_response.status_code, 302)
        self.assertEqual(ok_response.status_code, 302)
        reject_mock.assert_called_once()

    def test_approve_action_uses_existing_service_and_second_post_is_idempotent(self):
        order = self.create_order()
        self.login_admin()

        def fake_activate(order_arg, *, user=None, notify=True):
            Order.objects.filter(pk=order_arg.pk).update(
                status=Order.Status.COMPLETED,
                verification_status=Order.VerificationStatus.VERIFIED,
                is_paid=True,
            )
            return SimpleNamespace(success=True, message="ok")

        with patch("store.admin_views.activate_order", side_effect=fake_activate) as activate_mock:
            for _index in range(2):
                self.client.post(
                    reverse("admin_store_order_review", args=[order.pk]),
                    {"action": "approve", "confirm_external": "1"},
                )

        self.assertEqual(activate_mock.call_count, 1)

    def test_delivery_failure_message_is_safe(self):
        order = self.create_order()
        self.login_admin()

        with patch(
            "store.admin_views.activate_order",
            return_value=SimpleNamespace(success=False, message="failed for vless://SECRET-CONFIG-LINK"),
        ):
            response = self.client.post(
                reverse("admin_store_order_review", args=[order.pk]),
                {"action": "approve", "confirm_external": "1"},
                follow=True,
            )
        body = response.content.decode()

        self.assertNotIn("vless://SECRET-CONFIG-LINK", body)
        self.assertContains(response, "مخفی")

    def test_dashboard_links_to_order_workbench(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_order_workbench"))
        self.assertTrue(any(reverse("admin_store_order_workbench") in link.url for link in response.context["quick_actions"] if link.url))

    def test_order_admin_quick_review_link_exists(self):
        from django.contrib import admin as django_admin

        order = self.create_order()
        order_admin = django_admin.site._registry[Order]

        html = order_admin.quick_review_link(order)

        self.assertIn(reverse("admin_store_order_review", args=[order.pk]), str(html))

    def test_workbench_and_review_get_do_not_call_live_integrations(self):
        order = self.create_order()
        self.login_admin()

        with (
            patch("store.admin_views.activate_order") as activate_mock,
            patch("store.admin_views.reject_order") as reject_mock,
            patch("store.xui_api.login_to_panel") as login_mock,
            patch("store.telegram_bot.notifications.notify_order_event") as notify_mock,
        ):
            workbench_response = self.client.get(reverse("admin_store_order_workbench"))
            review_response = self.client.get(reverse("admin_store_order_review", args=[order.pk]))

        self.assertEqual(workbench_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        activate_mock.assert_not_called()
        reject_mock.assert_not_called()
        login_mock.assert_not_called()
        notify_mock.assert_not_called()


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminServiceWorkbenchTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="services-admin",
            email="services-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Services Store",
            english_name="Services Store",
            slug="services-store",
            card_number="621986" + "1234567890",
            card_owner="Services Owner",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="Services 10GB",
            slug="services-10gb",
            volume_gb=Decimal("10.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Services Panel",
            url="https://panel.example.com/admin",
            username="panel-admin",
            password="panel-secret-password",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=17,
            remark="Services inbound",
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=True,
        )
        self.customer = Customer.objects.create(
            display_name="Alice Service",
            username="alice_service",
            phone_number="+989121234567",
        )
        self.order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=100000,
            original_amount=100000,
            currency=Plan.Currency.TOMAN,
            payment_method=Order.PaymentMethod.MANUAL_CARD,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            is_paid=True,
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def create_vpn_client(self, **kwargs):
        data = {
            "store": self.store,
            "order": self.order,
            "plan": self.plan,
            "inbound": self.inbound,
            "username": "alice-service-config",
            "xui_email": "alice-service-config",
            "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "sub_id": "private-sub-token",
            "sub_link": "https://example.com/sub/SECRET-SUB-TOKEN",
            "direct_link": "vless://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@vpn.example.com:443#Alice",
            "status": VPNClient.Status.ACTIVE,
            "traffic_limit_bytes": 10 * (1024 ** 3),
            "used_traffic_bytes": 2 * (1024 ** 3),
            "duration_days": 30,
            "expires_at": timezone.now() + timedelta(days=20),
        }
        data.update(kwargs)
        return VPNClient.objects.create(**data)

    def test_service_workbench_url_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_service_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "میز کار سرویس‌ها")

    def test_service_workbench_requires_staff_login(self):
        response = self.client.get(reverse("admin_store_service_workbench"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="services-regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_store_service_workbench"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_service_workbench_fresh_install_without_clients_does_not_500(self):
        VPNClient.objects.all().delete()
        Customer.objects.all().delete()
        self.login_admin()

        response = self.client.get(reverse("admin_store_service_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "موردی در این صف نیست")

    def test_active_and_expiring_clients_appear_in_workbench_sections(self):
        active_client = self.create_vpn_client(username="active-service")
        expiring_client = self.create_vpn_client(
            username="expiring-service",
            uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            expires_at=timezone.now() + timedelta(days=2),
        )
        self.login_admin()

        response = self.client.get(reverse("admin_store_service_workbench"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary_counts"]["active"], 2)
        self.assertEqual(response.context["summary_counts"]["expiring"], 1)
        self.assertContains(response, f"#{active_client.pk}")
        self.assertContains(response, f"#{expiring_client.pk}")

    def test_client_review_page_loads_and_masks_secrets(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        response = self.client.get(reverse("admin_store_service_review", args=[vpn_client.pk]))
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "خلاصه سرویس")
        self.assertNotIn(self.customer.phone_number, body)
        self.assertNotIn(vpn_client.uuid, body)
        self.assertNotIn("SECRET-SUB-TOKEN", body)
        self.assertNotIn("vless://", body)
        self.assertIn("+989***67", body)

    def test_customer_review_page_loads(self):
        self.create_vpn_client()
        self.login_admin()

        response = self.client.get(reverse("admin_store_customer_review", args=[self.customer.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "بررسی مشتری")
        self.assertContains(response, "سرویس‌های فعال")

    def test_workbench_and_review_get_do_not_call_live_integrations(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        with (
            patch("store.admin_views.sync_vpn_client_stats") as sync_mock,
            patch("store.admin_views.refresh_vpn_client_link_by_admin") as refresh_mock,
            patch("store.admin_views.set_vpn_client_enabled_by_admin") as enabled_mock,
            patch("store.xui_api.login_to_panel") as login_mock,
        ):
            workbench_response = self.client.get(reverse("admin_store_service_workbench"))
            review_response = self.client.get(reverse("admin_store_service_review", args=[vpn_client.pk]))

        self.assertEqual(workbench_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        sync_mock.assert_not_called()
        refresh_mock.assert_not_called()
        enabled_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_resend_config_requires_post_and_confirmation(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        with patch("store.admin_views.resend_vpn_client_config_to_telegram") as resend_mock:
            get_response = self.client.get(reverse("admin_store_service_review", args=[vpn_client.pk]), {"action": "resend_config"})
            post_response = self.client.post(
                reverse("admin_store_service_review", args=[vpn_client.pk]),
                {"action": "resend_config"},
            )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 302)
        resend_mock.assert_not_called()

    def test_resend_config_without_telegram_target_returns_safe_error(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        response = self.client.post(
            reverse("admin_store_service_review", args=[vpn_client.pk]),
            {"action": "resend_config", "confirm_external": "1"},
            follow=True,
        )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مقصد تلگرام")
        self.assertNotIn("SECRET-SUB-TOKEN", body)
        self.assertNotIn("vless://", body)

    def test_resend_config_success_uses_delivery_helper_and_does_not_leak_secret(self):
        vpn_client = self.create_vpn_client()
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Service Telegram",
            bot_token="123:service-secret-token",
            admin_user_id="999",
            is_active=True,
        )
        BotUser.objects.create(
            bot_config=bot_config,
            customer=self.customer,
            provider_user_id="42",
            chat_id="42",
            is_active=True,
        )
        self.login_admin()

        with patch("store.telegram_bot.services_flow.send_client_config_messages", return_value={"ok": True}) as send_mock:
            response = self.client.post(
                reverse("admin_store_service_review", args=[vpn_client.pk]),
                {"action": "resend_config", "confirm_external": "1"},
                follow=True,
            )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        send_mock.assert_called_once()
        self.assertContains(response, "ارسال شد")
        self.assertNotIn("SECRET-SUB-TOKEN", body)
        self.assertNotIn("service-secret-token", body)

    def test_update_usage_requires_post_and_confirmation(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        with patch("store.admin_views.sync_vpn_client_stats", return_value={"panel_available": True}) as sync_mock:
            get_response = self.client.get(reverse("admin_store_service_review", args=[vpn_client.pk]), {"action": "update_usage"})
            missing_confirm_response = self.client.post(
                reverse("admin_store_service_review", args=[vpn_client.pk]),
                {"action": "update_usage"},
            )
            ok_response = self.client.post(
                reverse("admin_store_service_review", args=[vpn_client.pk]),
                {"action": "update_usage", "confirm_external": "1"},
            )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(missing_confirm_response.status_code, 302)
        self.assertEqual(ok_response.status_code, 302)
        sync_mock.assert_called_once()

    @patch("store.vpn_client_management_services.update_client_traffic_and_expiry")
    def test_disable_enable_require_post_and_are_idempotent(self, update_mock):
        vpn_client = self.create_vpn_client()
        update_mock.return_value = {
            "updated": True,
            "new_total_bytes": vpn_client.traffic_limit_bytes,
            "new_expiry_time": vpn_client.expires_at,
            "enabled": False,
            "raw": {"client": {"email": vpn_client.xui_email}},
        }
        self.login_admin()

        get_response = self.client.get(reverse("admin_store_service_review", args=[vpn_client.pk]), {"action": "disable_client"})
        first_disable = self.client.post(
            reverse("admin_store_service_review", args=[vpn_client.pk]),
            {"action": "disable_client", "confirm_external": "1"},
        )
        second_disable = self.client.post(
            reverse("admin_store_service_review", args=[vpn_client.pk]),
            {"action": "disable_client", "confirm_external": "1"},
        )
        vpn_client.refresh_from_db()

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(first_disable.status_code, 302)
        self.assertEqual(second_disable.status_code, 302)
        self.assertEqual(vpn_client.status, VPNClient.Status.INACTIVE)

        update_mock.return_value = {
            "updated": True,
            "new_total_bytes": vpn_client.traffic_limit_bytes,
            "new_expiry_time": vpn_client.expires_at,
            "enabled": True,
            "raw": {"client": {"email": vpn_client.xui_email}},
        }
        enable_response = self.client.post(
            reverse("admin_store_service_review", args=[vpn_client.pk]),
            {"action": "enable_client", "confirm_external": "1"},
        )
        vpn_client.refresh_from_db()

        self.assertEqual(enable_response.status_code, 302)
        self.assertEqual(vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertEqual(update_mock.call_count, 3)

    def test_admin_quick_review_links_exist(self):
        from django.contrib import admin as django_admin

        vpn_client = self.create_vpn_client()
        vpn_admin = django_admin.site._registry[VPNClient]
        customer_admin = django_admin.site._registry[Customer]

        self.assertIn(reverse("admin_store_service_review", args=[vpn_client.pk]), str(vpn_admin.quick_service_review_link(vpn_client)))
        self.assertIn(reverse("admin_store_customer_review", args=[self.customer.pk]), str(customer_admin.customer_review_link(self.customer)))

    def test_dashboard_links_to_service_workbench(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_service_workbench"))
        self.assertTrue(any(reverse("admin_store_service_workbench") in link.url for link in response.context["quick_actions"] if link.url))

    def test_order_review_links_to_service_review_when_client_exists(self):
        vpn_client = self.create_vpn_client()
        self.login_admin()

        response = self.client.get(reverse("admin_store_order_review", args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_store_service_review", args=[vpn_client.pk]))


@override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="", TELEGRAM_PROXY_URL="")
class AdminSetupWizardTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="wizard-admin",
            email="wizard-admin@example.com",
            password="secret",
        )
        self.store = Store.objects.create(
            name="Wizard Store",
            english_name="Wizard Store",
            slug="wizard-store",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )

    def login_admin(self):
        self.client.force_login(self.admin_user)

    def step_url(self, step_name, store=None):
        url = reverse(f"admin_store_setup_wizard_{step_name}")
        store = self.store if store is None else store
        if store:
            return f"{url}?store={store.pk}"
        return url

    def create_panel(self, **kwargs):
        data = {
            "store": self.store,
            "name": "Primary panel",
            "url": "https://panel.example.com/admin",
            "username": "panel-admin",
            "password": "panel-super-secret",
            "is_active": True,
        }
        data.update(kwargs)
        return Panel.objects.create(**data)

    def create_inbound(self, panel=None, **kwargs):
        data = {
            "panel": panel or self.create_panel(),
            "inbound_id": 10,
            "remark": "Main inbound",
            "server_ip": "vpn.example.com",
            "port": "443",
            "config_params": "type=tcp&security=none",
            "is_active": True,
            "available_for_new_orders": True,
            "health_monitor_enabled": True,
        }
        data.update(kwargs)
        return Inbound.objects.create(**data)

    def create_plan(self, **kwargs):
        data = {
            "store": self.store,
            "name": "Starter",
            "slug": "starter",
            "volume_gb": Decimal("1.000"),
            "duration_days": 30,
            "price": 100000,
            "currency": Plan.Currency.TOMAN,
            "is_active": True,
            "is_public": True,
        }
        data.update(kwargs)
        return Plan.objects.create(**data)

    def assert_response_has_no_secrets(self, response, secrets):
        body = response.content.decode()
        for secret in secrets:
            self.assertNotIn(secret, body)

    def test_wizard_index_loads_for_superuser(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_wizard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "راه‌اندازی مرحله‌ای قاصدک")
        self.assertContains(response, reverse("admin_store_setup_wizard_store"))

    def test_wizard_requires_staff_login(self):
        response = self.client.get(reverse("admin_store_setup_wizard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        regular_user = get_user_model().objects.create_user(username="wizard-regular", password="secret")
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_store_setup_wizard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_fresh_minimal_wizard_pages_do_not_500(self):
        Store.objects.all().delete()
        self.login_admin()

        urls = [
            reverse("admin_store_setup_wizard"),
            reverse("admin_store_setup_wizard_store"),
            reverse("admin_store_setup_wizard_payment"),
            reverse("admin_store_setup_wizard_telegram"),
            reverse("admin_store_setup_wizard_telegram_proxy"),
            reverse("admin_store_setup_wizard_panel"),
            reverse("admin_store_setup_wizard_inbounds"),
            reverse("admin_store_setup_wizard_plans"),
            reverse("admin_store_setup_wizard_routes"),
            reverse("admin_store_setup_wizard_review"),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotContains(response, "Traceback")

    def test_store_identity_get_and_post_updates_store(self):
        self.login_admin()

        get_response = self.client.get(self.step_url("store"))
        post_response = self.client.post(
            self.step_url("store"),
            {
                "name": "Wizard Updated",
                "english_name": "Wizard Updated",
                "domain": "wizard.example.com",
            },
        )

        self.store.refresh_from_db()
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 302)
        self.assertIn(reverse("admin_store_setup_wizard_payment"), post_response["Location"])
        self.assertEqual(self.store.name, "Wizard Updated")
        self.assertEqual(self.store.domain, "wizard.example.com")

    def test_payment_step_masks_card_and_token(self):
        secret_card = "621986" + "1234567890"
        secret_token = "123456" + ":" + "smsforwarder-secret-token"
        self.store.card_number = secret_card
        self.store.card_owner = "Wizard Owner"
        self.store.set_smsforwarder_webhook_token(secret_token)
        self.store.save()
        self.login_admin()

        response = self.client.get(self.step_url("payment"))

        self.assertEqual(response.status_code, 200)
        self.assert_response_has_no_secrets(response, [secret_card, secret_token])
        self.assertContains(response, "مقدار قبلی حفظ")

    def test_telegram_step_masks_full_token(self):
        secret_token = "123456" + ":" + "telegram-secret-token"
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="wizard_bot",
            bot_token=secret_token,
            admin_user_id="999",
            is_active=True,
        )
        self.login_admin()

        response = self.client.get(self.step_url("telegram"))

        self.assertEqual(response.status_code, 200)
        self.assert_response_has_no_secrets(response, [secret_token])
        self.assertContains(response, "token کامل بعد از ذخیره نمایش داده نمی‌شود")

    def test_telegram_blank_token_preserves_previous_value(self):
        previous_token = "123456" + ":" + "telegram-secret-token"
        config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="wizard_bot",
            bot_token=previous_token,
            admin_user_id="999",
            is_active=True,
        )
        self.login_admin()

        response = self.client.post(
            self.step_url("telegram"),
            {
                "is_active": "on",
                "name": "Telegram bot",
                "telegram_bot_username": "wizard_bot",
                "bot_token": "",
                "admin_user_id": "1000",
                "additional_admin_user_ids": "",
            },
        )

        config.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_store_setup_wizard_telegram_proxy"), response["Location"])
        self.assertEqual(config.bot_token, previous_token)
        self.assertEqual(config.admin_user_id, "1000")

    def test_panel_step_masks_password_and_proxy(self):
        secret_password = "panel-super-secret"
        secret_proxy = "http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:8080"
        self.create_panel(password=secret_password, proxy_url=secret_proxy)
        self.login_admin()

        response = self.client.get(self.step_url("panel"))

        self.assertEqual(response.status_code, 200)
        self.assert_response_has_no_secrets(response, [secret_password, secret_proxy, "proxy-secret"])
        self.assertContains(response, "password قبلی حفظ")

    def test_panel_blank_password_preserves_previous_value(self):
        previous_password = "panel-super-secret"
        previous_proxy = "http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:8080"
        panel = self.create_panel(password=previous_password, proxy_url=previous_proxy)
        self.login_admin()

        response = self.client.post(
            self.step_url("panel"),
            {
                "name": "Primary panel",
                "url": "https://panel.example.com/admin",
                "username": "panel-admin",
                "password": "",
                "proxy_url": "",
                "is_active": "on",
            },
        )

        panel.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_store_setup_wizard_inbounds"), response["Location"])
        self.assertEqual(panel.password, previous_password)
        self.assertEqual(panel.proxy_url, previous_proxy)

    def test_plan_step_creates_plan(self):
        self.login_admin()

        response = self.client.post(
            self.step_url("plans"),
            {
                "name": "Wizard 1GB",
                "volume_gb": "1.000",
                "duration_days": "30",
                "price": "120000",
                "currency": Plan.Currency.TOMAN,
                "is_active": "on",
                "is_public": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Plan.objects.filter(store=self.store, name="Wizard 1GB", price=120000).exists())

    def test_route_step_creates_valid_route(self):
        plan = self.create_plan()
        inbound = self.create_inbound()
        self.login_admin()

        response = self.client.post(
            self.step_url("routes"),
            {
                "plan": str(plan.pk),
                "inbound": str(inbound.pk),
                "is_active": "on",
                "priority": "50",
                "weight": "1",
                "note": "Wizard route",
            },
        )

        self.assertEqual(response.status_code, 302)
        route = PlanInboundRoute.objects.get(store=self.store, plan=plan)
        self.assertEqual(route.inbound, inbound)
        self.assertEqual(route.priority, 50)

    def test_review_warns_when_active_plan_has_no_route(self):
        self.create_plan()
        self.login_admin()

        response = self.client.get(self.step_url("review"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "route معتبر ندارد")
        self.assertContains(response, "Revenue Engine")

    def test_skip_marks_step_as_skipped(self):
        self.login_admin()

        response = self.client.post(self.step_url("telegram_proxy"), {"_skip": "1"})
        index_response = self.client.get(reverse("admin_store_setup_wizard"), {"store": self.store.pk})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_store_setup_wizard_panel"), response["Location"])
        self.assertContains(index_response, "بعداً انجام می‌دهم")

    def test_wizard_does_not_call_live_integrations(self):
        self.login_admin()

        with patch("store.telegram_bot.client.BotClient.get_me") as get_me_mock, patch(
            "store.xui_api.login_to_panel"
        ) as login_mock:
            responses = [
                self.client.get(reverse("admin_store_setup_wizard")),
                self.client.get(self.step_url("telegram")),
                self.client.get(self.step_url("panel")),
                self.client.get(self.step_url("review")),
            ]

        for response in responses:
            self.assertEqual(response.status_code, 200)
        get_me_mock.assert_not_called()
        login_mock.assert_not_called()

    def test_setup_center_links_to_wizard_steps(self):
        self.login_admin()

        response = self.client.get(reverse("admin_store_setup_center"), {"store": self.store.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "شروع راه‌اندازی مرحله‌ای")
        self.assertContains(response, reverse("admin_store_setup_wizard"))
        self.assertContains(response, reverse("admin_store_setup_wizard_payment"))

    def test_dashboard_action_items_link_to_wizard_steps(self):
        BotConfiguration.objects.all().delete()
        Panel.objects.all().delete()
        self.create_plan()
        self.login_admin()

        response = self.client.get(reverse("admin_store_owner_dashboard"), {"store": self.store.pk})
        action_urls = [item.url for item in response.context["action_items"] if item.url]

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(reverse("admin_store_setup_wizard_routes") in url for url in action_urls))
        self.assertTrue(any(reverse("admin_store_setup_wizard_panel") in url for url in action_urls))
        self.assertTrue(any(reverse("admin_store_setup_wizard_telegram") in url for url in action_urls))

    def test_secrets_are_absent_from_wizard_responses(self):
        secret_card = "621986" + "1234567890"
        secret_token = "123456" + ":" + "telegram-secret-token"
        secret_password = "panel-super-secret"
        secret_proxy = "http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:8080"
        self.store.card_number = secret_card
        self.store.card_owner = "Wizard Owner"
        self.store.set_smsforwarder_webhook_token(secret_token)
        self.store.save()
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram bot",
            telegram_bot_username="wizard_bot",
            bot_token=secret_token,
            admin_user_id="999",
            is_active=True,
        )
        self.create_panel(password=secret_password, proxy_url=secret_proxy)
        self.login_admin()

        urls = [
            reverse("admin_store_setup_wizard"),
            self.step_url("payment"),
            self.step_url("telegram"),
            self.step_url("panel"),
            self.step_url("review"),
            reverse("admin_store_setup_center"),
            reverse("admin_store_owner_dashboard"),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assert_response_has_no_secrets(
                response,
                [secret_card, secret_token, secret_password, secret_proxy, "proxy-secret"],
            )


class AdminNotificationTests(TestCase):
    def setUp(self):
        self.media_tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_tmp.cleanup)

        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
        self.assertIn("کانفیگ در پیام بعدی ارسال می‌شود", message)
        self.assertNotIn("vless://bulk-1", message)


class WebTelegramLinkServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            bank_name="Test Bank",
        )
        self.customer = Customer.objects.create(display_name="Web Customer")
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            telegram_bot_username="vpn_store_bot",
            bot_token="123:token",
            admin_user_id="999",
            is_active=True,
        )

    def test_create_link_token_stores_hash_only(self):
        from .telegram_link_services import create_web_telegram_link_token, hash_web_telegram_link_token

        raw_token, token = create_web_telegram_link_token(self.customer, source="dashboard")

        self.assertGreaterEqual(len(raw_token), 32)
        self.assertEqual(token.customer, self.customer)
        self.assertEqual(token.status, WebTelegramLinkToken.Status.ACTIVE)
        self.assertEqual(token.token_hash, hash_web_telegram_link_token(raw_token))
        self.assertNotEqual(token.token_hash, raw_token)
        self.assertNotIn(raw_token, str(token.metadata))
        self.assertGreater(token.expires_at, timezone.now() + timedelta(days=6))

    def test_generate_web_telegram_link_uses_bot_username_and_token_payload(self):
        from .telegram_link_services import generate_web_telegram_link, hash_web_telegram_link_token

        link = generate_web_telegram_link(self.customer, source="dashboard", store=self.store)

        self.assertTrue(link.telegram_link.startswith("https://t.me/vpn_store_bot?start=link_"))
        self.assertIn(link.raw_token, link.telegram_link)
        self.assertEqual(
            WebTelegramLinkToken.objects.get().token_hash,
            hash_web_telegram_link_token(link.raw_token),
        )

    @override_settings(TELEGRAM_BOT_USERNAME="legacy_bot", BOT_USERNAME="legacy_bot")
    def test_generate_web_telegram_link_reports_missing_bot_username(self):
        self.bot_config.telegram_bot_username = ""
        self.bot_config.save(update_fields=["telegram_bot_username", "updated_at"])
        from .telegram_link_services import BOT_USERNAME_MISSING_MESSAGE, generate_web_telegram_link

        link = generate_web_telegram_link(self.customer, source="dashboard", store=self.store)

        self.assertEqual(link.telegram_link, "")
        self.assertEqual(link.missing_username_message, BOT_USERNAME_MISSING_MESSAGE)
        self.assertFalse(WebTelegramLinkToken.objects.exists())

    @override_settings(TELEGRAM_BOT_USERNAME="legacy_bot", BOT_USERNAME="legacy_bot")
    def test_generate_web_telegram_link_ignores_settings_username_fallback(self):
        self.bot_config.delete()
        from .telegram_link_services import BOT_USERNAME_MISSING_MESSAGE, generate_web_telegram_link

        link = generate_web_telegram_link(self.customer, source="dashboard", store=self.store)

        self.assertEqual(link.telegram_link, "")
        self.assertEqual(link.missing_username_message, BOT_USERNAME_MISSING_MESSAGE)
        self.assertFalse(WebTelegramLinkToken.objects.exists())

    def test_new_link_revokes_previous_active_token(self):
        from .telegram_link_services import create_web_telegram_link_token

        _first_raw, first_token = create_web_telegram_link_token(self.customer, source="dashboard")
        _second_raw, second_token = create_web_telegram_link_token(self.customer, source="order_detail")

        first_token.refresh_from_db()
        self.assertEqual(first_token.status, WebTelegramLinkToken.Status.REVOKED)
        self.assertIsNotNone(first_token.revoked_at)
        self.assertEqual(second_token.status, WebTelegramLinkToken.Status.ACTIVE)

    def test_repeated_link_generation_keeps_only_one_active_token(self):
        from .telegram_link_services import generate_web_telegram_link

        for _ in range(3):
            generate_web_telegram_link(self.customer, source="dashboard", store=self.store)

        self.assertEqual(
            WebTelegramLinkToken.objects.filter(customer=self.customer, status=WebTelegramLinkToken.Status.ACTIVE).count(),
            1,
        )
        self.assertEqual(
            WebTelegramLinkToken.objects.filter(customer=self.customer, status=WebTelegramLinkToken.Status.REVOKED).count(),
            2,
        )

    def test_link_generation_repairs_duplicate_active_tokens(self):
        from .telegram_link_services import create_web_telegram_link_token

        WebTelegramLinkToken.objects.create(customer=self.customer, token_hash="a" * 64)
        WebTelegramLinkToken.objects.create(customer=self.customer, token_hash="b" * 64)

        _raw_token, token = create_web_telegram_link_token(self.customer, source="dashboard")

        self.assertEqual(
            WebTelegramLinkToken.objects.filter(customer=self.customer, status=WebTelegramLinkToken.Status.ACTIVE).count(),
            1,
        )
        self.assertEqual(
            WebTelegramLinkToken.objects.filter(customer=self.customer, status=WebTelegramLinkToken.Status.REVOKED).count(),
            2,
        )
        self.assertEqual(WebTelegramLinkToken.objects.get(status=WebTelegramLinkToken.Status.ACTIVE), token)


class ReferralRewardSystemTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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

    @override_settings(TELEGRAM_BOT_USERNAME="vpn_store_bot")
    def test_telegram_referral_link_uses_configured_bot_username(self):
        link = build_telegram_referral_link(self.inviter)

        self.assertEqual(link, f"https://t.me/vpn_store_bot?start=ref_{self.inviter.referral_code}")

    @override_settings(TELEGRAM_BOT_USERNAME="legacy_bot")
    def test_telegram_referral_link_prefers_bot_configuration_username(self):
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Referral bot",
            telegram_bot_username="admin_bot",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )

        link = build_telegram_referral_link(self.inviter, bot_config=bot_config)

        self.assertEqual(link, f"https://t.me/admin_bot?start=ref_{self.inviter.referral_code}")

    @override_settings(TELEGRAM_BOT_USERNAME="legacy_bot")
    def test_referral_summary_prefers_store_bot_configuration_username(self):
        other_store = Store.objects.create(
            name="Other store",
            english_name="Other store",
            card_number="0000000000000000",
            card_owner="Other Owner",
        )
        BotConfiguration.objects.create(
            store=other_store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Other bot",
            telegram_bot_username="wrong_bot",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Referral bot",
            telegram_bot_username="admin_bot",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )

        summary = get_referral_summary(self.inviter, store=self.store)

        self.assertIn("https://t.me/admin_bot?start=ref_", summary["telegram_link"])

    def test_bot_configuration_cleans_telegram_username(self):
        bot_config = BotConfiguration(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Clean username bot",
            telegram_bot_username="@admin_bot",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )

        bot_config.full_clean()

        self.assertEqual(bot_config.telegram_bot_username, "admin_bot")

    def test_bot_configuration_rejects_invalid_telegram_username(self):
        bot_config = BotConfiguration(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Broken username bot",
            telegram_bot_username="bad-name",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )

        with self.assertRaises(ValidationError):
            bot_config.full_clean()

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
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            telegram_bot_username="azadnet_web_bot",
            bot_token="123:token",
            admin_user_id="999",
            is_active=True,
        )
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
        self.assertContains(response, "اتصال به ربات تلگرام")
        self.assertContains(response, "https://t.me/azadnet_web_bot?start=link_")

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_web_bot")
    @patch("store.views.sync_vpn_client_stats")
    def test_dashboard_shows_connected_telegram_state(self, stats_mock):
        vpn_config = self.create_active_inviter_config()
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            telegram_bot_username="azadnet_web_bot",
            bot_token="123:token",
            admin_user_id="999",
            is_active=True,
        )
        BotUser.objects.create(
            bot_config=BotConfiguration.objects.get(provider=BotConfiguration.Provider.TELEGRAM),
            customer=self.inviter,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
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
        self.assertContains(response, "✅ حساب شما به ربات تلگرام وصل است.")
        self.assertContains(response, "باز کردن ربات")
        self.assertNotContains(response, "https://t.me/azadnet_web_bot?start=link_")

    @override_settings(TELEGRAM_BOT_USERNAME="azadnet_web_bot")
    def test_order_detail_shows_telegram_link_cta_for_unlinked_customer(self):
        order = self.create_order(customer=self.inviter)
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            telegram_bot_username="azadnet_web_bot",
            bot_token="123:token",
            admin_user_id="999",
            is_active=True,
        )
        self.set_customer_cookie(self.inviter)

        response = self.client.get(reverse("order_detail", kwargs={"order_id": order.public_id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "برای پیگیری سفارش و دریافت یادآوری تمدید")
        self.assertContains(response, "https://t.me/azadnet_web_bot?start=link_")


class CustomerAnalyticsTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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


def wizwiz_insert(rows):
    columns = (
        "`id`, `userid`, `name`, `username`, `refcode`, `wallet`, `date`, `phone`, "
        "`refered_by`, `step`, `freetrial`, `isAdmin`, `first_start`, `temp`, "
        "`is_agent`, `discount_percent`, `agent_date`, `spam_info`"
    )
    return f"INSERT INTO `users` ({columns}) VALUES\n" + ",\n".join(rows) + ";\n"


class LegacyWizWizParserTests(TestCase):
    def test_parse_insert_users_multiple_rows_unicode_null_and_escaped_values(self):
        sql = wizwiz_insert(
            [
                "(1, '123456789', 'علی, تست', 'ali_test', 'RF1', 100, '2025-01-01', '09123456789', NULL, '', NULL, 0, '', '', 1, 0, NULL, 'safe')",
                "(2, '987654321', 'O\\'Connor', 'ندارد', NULL, NULL, '2025-01-02', NULL, '123456789', '', 'yes', 1, '', '', 0, 0, NULL, 'comma, inside')",
            ]
        )

        columns, values = parse_mysql_insert_values(sql)

        self.assertIn("userid", columns)
        self.assertEqual(values[0][2], "علی, تست")
        self.assertEqual(values[1][2], "O'Connor")
        self.assertIsNone(values[1][4])
        self.assertEqual(values[1][-1], "comma, inside")

    def test_parse_wizwiz_users_ignores_non_users_tables(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", encoding="utf-8") as handle:
            handle.write("INSERT INTO `admins` (`password`, `token`) VALUES ('secret', 'token');\n")
            handle.write(wizwiz_insert(["(1, '123456789', 'Ali', 'ali_test', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]))
            handle.flush()

            rows = list(parse_wizwiz_users_from_sql_file(handle.name))

        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["userid"]), "123456789")
        self.assertNotIn("password", rows[0])

    def test_normalize_username_empty_values_and_invalid_userid(self):
        normalized = normalize_wizwiz_user_row({"id": 1, "userid": "123456789", "username": " ندارد "})
        invalid = normalize_wizwiz_user_row({"id": 2, "userid": "not-a-number", "username": "valid_name"})

        self.assertTrue(normalized["valid"])
        self.assertEqual(normalized["username"], "")
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["reason"], "invalid_userid")


class LegacyWizWizImportBase(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )

    def tearDown(self):
        self.override.disable()
        self.media_dir.cleanup()

    def make_job(self, sql, **kwargs):
        job = LegacyWizWizImportJob.objects.create(
            title=kwargs.pop("title", "legacy.sql"),
            original_filename="legacy.sql",
            **kwargs,
        )
        job.uploaded_file.save("legacy.sql", ContentFile(sql.encode("utf-8")), save=True)
        return job


class LegacyWizWizAnalyzeTests(LegacyWizWizImportBase):
    def test_analyze_builds_preview_rows_without_creating_users(self):
        sql = wizwiz_insert(
            [
                "(1, '111111111', 'Ali', 'ali_user', 'RF1', 0, '2025-01-01', '09123456789', '', '', '', 0, '', '', 0, 0, '', '')",
                "(2, '111111111', 'Ali Dup', 'ali_dup', 'RF2', 0, '2025-01-02', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(3, 'bad-id', 'Bad', 'bad_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(4, '222222222', 'Admin', 'admin_user', '', 10, '', '', '', '', '', 1, '', '', 0, 0, '', '')",
                "(5, '333333333', 'Agent', 'agent_user', '', 50, '', '', '', '', '', 0, '', '', 1, 0, '', '')",
            ]
        )
        job = self.make_job(sql)

        summary = analyze_wizwiz_import_job(job)

        self.assertEqual(summary["parsed_users"], 5)
        self.assertEqual(summary["valid_users"], 3)
        self.assertEqual(summary["invalid_rows"], 1)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["admins"], 1)
        self.assertEqual(summary["agents"], 1)
        self.assertEqual(summary["wallet_positive"], 2)
        self.assertEqual(LegacyWizWizImportRow.objects.filter(job=job).count(), 4)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(BotUser.objects.exists())
        admin_row = LegacyWizWizImportRow.objects.get(job=job, telegram_user_id="222222222")
        self.assertEqual(admin_row.status, LegacyWizWizImportRow.Status.SKIPPED)
        self.assertEqual(admin_row.reason, "skipped_admin")

    def test_analyze_detects_existing_botuser_customer_and_duplicate_file_sha(self):
        customer = Customer.objects.create(username="legacy_user", display_name="Legacy User")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="444444444",
            chat_id="444444444",
            username="legacy_user",
        )
        sql = wizwiz_insert(["(1, '444444444', 'Legacy User', 'legacy_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"])
        first_job = self.make_job(sql)
        analyze_wizwiz_import_job(first_job)
        second_job = self.make_job(sql)

        summary = analyze_wizwiz_import_job(second_job)
        second_job.refresh_from_db()

        self.assertEqual(summary["existing_bot_users"], 1)
        self.assertEqual(summary["existing_customers"], 1)
        self.assertEqual(second_job.rows.get().status, LegacyWizWizImportRow.Status.EXISTING)
        self.assertIn("duplicate_file_jobs", second_job.metadata)


class LegacyWizWizApplyTests(LegacyWizWizImportBase):
    def test_apply_creates_customer_and_botuser_and_is_idempotent(self):
        sql = wizwiz_insert(["(1, '555555555', 'New User', 'new_user', 'RF1', 25, '2025-01-01', '09120000000', '', '', '', 0, '', '', 0, 0, '', '')"])
        job = self.make_job(sql)
        analyze_wizwiz_import_job(job)

        apply_wizwiz_import_job(job)
        apply_wizwiz_import_job(job)

        self.assertEqual(BotUser.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        bot_user = BotUser.objects.get(provider_user_id="555555555")
        self.assertEqual(bot_user.chat_id, "555555555")
        self.assertEqual(bot_user.username, "new_user")
        self.assertEqual(bot_user.customer.phone_number, "09120000000")
        row = job.rows.get()
        self.assertEqual(row.status, LegacyWizWizImportRow.Status.CREATED)
        job.refresh_from_db()
        self.assertEqual(job.created_bot_users_count, 1)
        self.assertEqual(job.created_customers_count, 1)

    def test_apply_links_existing_botuser_without_customer(self):
        BotUser.objects.create(
            bot_config=self.bot_config,
            provider_user_id="666666666",
            chat_id="666666666",
            username="old_user",
        )
        sql = wizwiz_insert(["(1, '666666666', 'Linked User', 'linked_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"])
        job = self.make_job(sql)
        analyze_wizwiz_import_job(job)

        apply_wizwiz_import_job(job)

        bot_user = BotUser.objects.get(provider_user_id="666666666")
        self.assertIsNotNone(bot_user.customer)
        self.assertEqual(job.rows.get().status, LegacyWizWizImportRow.Status.LINKED)

    def test_apply_keeps_existing_customer_unless_update_existing(self):
        customer = Customer.objects.create(username="old_name", display_name="Old Name")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="777777777",
            chat_id="777777777",
            username="old_name",
        )
        sql = wizwiz_insert(["(1, '777777777', 'Fresh Name', 'fresh_name', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"])
        job = self.make_job(sql, update_existing=True)
        analyze_wizwiz_import_job(job)

        apply_wizwiz_import_job(job)

        bot_user = BotUser.objects.get(provider_user_id="777777777")
        customer.refresh_from_db()
        self.assertEqual(bot_user.username, "fresh_name")
        self.assertEqual(customer.display_name, "Fresh Name")
        self.assertEqual(job.rows.get().status, LegacyWizWizImportRow.Status.UPDATED)

    def test_apply_respects_admin_agent_and_wallet_filters(self):
        sql = wizwiz_insert(
            [
                "(1, '888888881', 'Agent Wallet', 'agent_wallet', '', 10, '', '', '', '', '', 0, '', '', 1, 0, '', '')",
                "(2, '888888882', 'No Wallet', 'no_wallet', '', 0, '', '', '', '', '', 0, '', '', 1, 0, '', '')",
                "(3, '888888883', 'Not Agent', 'not_agent', '', 10, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(4, '888888884', 'Admin', 'admin_keepout', '', 10, '', '', '', '', '', 1, '', '', 1, 0, '', '')",
            ]
        )
        job = self.make_job(sql, only_agents=True, only_wallet_positive=True)
        analyze_wizwiz_import_job(job)

        apply_wizwiz_import_job(job)

        self.assertEqual(list(BotUser.objects.values_list("provider_user_id", flat=True)), ["888888881"])
        self.assertEqual(job.rows.filter(status=LegacyWizWizImportRow.Status.SKIPPED).count(), 3)


class WizWizSimpleRestoreServiceTests(LegacyWizWizImportBase):
    def upload(self, sql, name="legacy.sql"):
        return SimpleUploadedFile(name, sql.encode("utf-8"), content_type="application/sql")

    def test_simple_restore_runs_analyze_and_apply_without_duplicate_users(self):
        sql = wizwiz_insert(
            ["(1, '909090901', 'Simple User', 'simple_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
        )

        first = wizwiz_simple_restore(self.upload(sql), created_by=None)
        second = wizwiz_simple_restore(self.upload(sql), created_by=None)

        self.assertEqual(first["users_imported"], 1)
        self.assertEqual(first["users_existing"], 0)
        self.assertEqual(second["users_imported"], 0)
        self.assertEqual(second["users_existing"], 1)
        self.assertEqual(BotUser.objects.filter(provider_user_id="909090901").count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(LegacyWizWizImportJob.objects.filter(status=LegacyWizWizImportJob.Status.APPLIED).count(), 2)


class LegacyWizWizAdminAndCommandTests(LegacyWizWizImportBase):
    def test_admin_models_are_registered(self):
        from django.contrib import admin

        self.assertIn(LegacyWizWizImportJob, admin.site._registry)
        self.assertIn(LegacyWizWizImportRow, admin.site._registry)

    def test_command_create_job_analyze_dry_run_apply_and_export_csv(self):
        sql_path = Path(self.media_dir.name) / "wizwiz.sql"
        sql_path.write_text(
            wizwiz_insert(["(1, '999999991', 'Cmd User', 'cmd_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]),
            encoding="utf-8",
        )
        out = StringIO()

        call_command("import_wizwiz_users", "--sql-path", str(sql_path), "--create-job", "--analyze", "--dry-run", stdout=out)
        job = LegacyWizWizImportJob.objects.get()
        self.assertEqual(job.status, LegacyWizWizImportJob.Status.ANALYZED)
        self.assertFalse(BotUser.objects.exists())

        export_path = Path(self.media_dir.name) / "rows.csv"
        call_command("import_wizwiz_users", "--job-id", str(job.pk), "--apply", "--export-csv", str(export_path), stdout=out)

        self.assertTrue(export_path.exists())
        self.assertEqual(BotUser.objects.filter(provider_user_id="999999991").count(), 1)

    def test_command_invalid_path_error_is_safe(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("import_wizwiz_users", "--sql-path", "/tmp/missing-wizwiz.sql", "--create-job")

        self.assertIn("SQL file not found", str(ctx.exception))


class LegacyWizWizAdminWizardTests(LegacyWizWizImportBase):
    def setUp(self):
        super().setUp()
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="pass",
        )
        self.staff_user = user_model.objects.create_user(
            username="staff",
            email="staff@example.com",
            password="pass",
            is_staff=True,
        )
        self.client.force_login(self.admin_user)

    def upload_file(self, sql=None):
        sql = sql or wizwiz_insert(
            ["(1, '910000001', 'Admin Wizard', 'admin_wizard', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
        )
        return SimpleUploadedFile("legacy.sql", sql.encode("utf-8"), content_type="application/sql")

    def test_botuser_wizwiz_import_view_requires_staff_permission(self):
        url = reverse("admin:store_botuser_wizwiz_restore")

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        self.client.force_login(self.staff_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_botuser_restore_runs_full_import_and_shows_simple_result(self):
        response = self.client.post(
            reverse("admin:store_botuser_wizwiz_restore"),
            {
                "action": "start_import",
                "sql_file": self.upload_file(
                    wizwiz_insert(
                        [
                            "(1, '910000010', 'Restore New', 'restore_new', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                        ]
                    )
                ),
            },
            follow=True,
        )

        job = LegacyWizWizImportJob.objects.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Users imported")
        self.assertContains(response, "Users existing")
        self.assertContains(response, "Send Message")
        self.assertNotContains(response, "Analyze")
        self.assertNotContains(response, "Apply Import")
        self.assertNotContains(response, "Batch")
        self.assertEqual(job.status, LegacyWizWizImportJob.Status.APPLIED)
        self.assertEqual(BotUser.objects.filter(provider_user_id="910000010").count(), 1)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertFalse(LegacyWizWizImportMessageBatch.objects.exists())

    @patch("store.telegram_bot.client.requests.post", return_value=DummyBotResponse({"ok": True, "result": {}}))
    def test_restore_message_send_requires_admin_click_after_done(self, post_mock):
        self.client.post(
            reverse("admin:store_botuser_wizwiz_restore"),
            {
                "action": "start_import",
                "sql_file": self.upload_file(
                    wizwiz_insert(
                        [
                            "(1, '910000011', 'Restore Message', 'restore_message', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                        ]
                    )
                ),
            },
            follow=True,
        )
        self.assertFalse(LegacyWizWizImportMessageBatch.objects.exists())

        response = self.client.post(
            reverse("admin:store_botuser_wizwiz_restore"),
            {"action": "send_message", "text": "سلام کاربران قدیمی"},
            follow=True,
        )

        self.assertContains(response, "Message result")
        self.assertEqual(LegacyWizWizImportMessageBatch.objects.count(), 1)
        self.assertEqual(LegacyWizWizImportMessageRecipient.objects.filter(status="sent").count(), 1)
        self.assertEqual(post_mock.call_count, 1)

    def test_technical_botuser_import_wizard_urls_are_not_registered(self):
        for route_name, args in (
            ("admin:store_botuser_wizwiz_import", []),
            ("admin:store_botuser_wizwiz_import_detail", [1]),
            ("admin:store_botuser_wizwiz_import_apply", [1]),
        ):
            with self.subTest(route_name=route_name):
                with self.assertRaises(NoReverseMatch):
                    reverse(route_name, args=args)

    def test_engine_analyze_remains_internal_without_creating_customer_or_botuser(self):
        job = self.make_job(
            wizwiz_insert(
                ["(1, '910000001', 'Analyze User', 'analyze_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
            )
        )

        analyze_wizwiz_import_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, LegacyWizWizImportJob.Status.ANALYZED)
        self.assertEqual(job.valid_users_count, 1)
        self.assertEqual(job.would_create_bot_users_count, 1)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(BotUser.objects.exists())

    def test_engine_apply_and_reapply_are_idempotent(self):
        job = self.make_job(
            wizwiz_insert(
                ["(1, '910000002', 'Apply User', 'apply_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
            )
        )
        analyze_wizwiz_import_job(job)

        apply_wizwiz_import_job(job)
        apply_wizwiz_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, LegacyWizWizImportJob.Status.APPLIED)
        self.assertEqual(BotUser.objects.filter(provider_user_id="910000002").count(), 1)

    def test_engine_apply_without_analyzed_status_does_not_create_users(self):
        job = self.make_job(
            wizwiz_insert(
                ["(1, '910000003', 'Pending User', 'pending_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
            )
        )

        with self.assertRaises(ValidationError):
            apply_wizwiz_import_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, LegacyWizWizImportJob.Status.UPLOADED)
        self.assertFalse(BotUser.objects.exists())

    def test_legacy_job_admin_status_is_readonly_and_has_no_bulk_apply_action(self):
        from django.contrib import admin

        model_admin = admin.site._registry[LegacyWizWizImportJob]
        request = SimpleNamespace(user=self.admin_user, GET={})

        self.assertIn("status", model_admin.readonly_fields)
        self.assertNotIn("apply_selected", model_admin.get_actions(request))
        self.assertEqual(model_admin.get_model_perms(request), {})
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_view_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        response = self.client.get(reverse("admin:store_legacywizwizimportjob_changelist"))
        self.assertEqual(response.status_code, 403)

    def test_message_batch_service_requires_applied_job(self):
        job = self.make_job(
            wizwiz_insert(
                ["(1, '910000004', 'Message User', 'message_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
            )
        )
        analyze_wizwiz_import_job(job)
        with self.assertRaises(ValidationError):
            create_legacy_import_message_batch(job, "سلام", self.admin_user)

        apply_wizwiz_import_job(job)
        batch = create_legacy_import_message_batch(job, "سلام", self.admin_user)
        self.assertEqual(batch.recipients.count(), 1)


class LegacyWizWizImportMessageTests(LegacyWizWizImportBase):
    def setUp(self):
        super().setUp()
        self.store.broadcast_rate_limit_per_second = 1000
        self.store.save(update_fields=["broadcast_rate_limit_per_second", "updated_at"])

    def make_applied_job(self):
        sql = wizwiz_insert(
            [
                "(1, '920000001', 'First', 'first_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(2, '920000002', 'Blocked', 'blocked_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(3, '920000003', 'Timeout', 'timeout_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
                "(4, '920000004', 'Last', 'last_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')",
            ]
        )
        job = self.make_job(sql)
        analyze_wizwiz_import_job(job)
        apply_wizwiz_import_job(job)
        return job

    def test_preview_and_create_batch_are_job_specific_and_skip_missing_chat(self):
        job = self.make_applied_job()
        other_job = self.make_job(
            wizwiz_insert(
                ["(1, '920000099', 'Other', 'other_user', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]
            )
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="missing-chat",
            telegram_user_id_masked="missing",
            status=LegacyWizWizImportRow.Status.CREATED,
        )
        LegacyWizWizImportRow.objects.create(
            job=other_job,
            telegram_user_id="920000099",
            telegram_user_id_masked="92***099",
            status=LegacyWizWizImportRow.Status.CREATED,
        )

        preview = preview_legacy_import_message_batch(job, "سلام کاربران قدیمی")
        batch = create_legacy_import_message_batch(job, "سلام کاربران قدیمی")

        self.assertEqual(preview["recipients_total"], 5)
        self.assertEqual(preview["sendable"], 4)
        self.assertEqual(preview["skipped_no_chat_id"], 1)
        self.assertEqual(batch.recipients.count(), 5)
        self.assertFalse(batch.recipients.filter(row__job=other_job).exists())
        self.assertEqual(
            batch.recipients.get(row__telegram_user_id="missing-chat").status,
            LegacyWizWizImportMessageRecipient.Status.SKIPPED,
        )

    @patch("store.telegram_bot.client.requests.post")
    def test_send_batch_records_success_blocked_timeout_and_continues(self, post_mock):
        job = self.make_applied_job()
        batch = create_legacy_import_message_batch(job, "سلام کاربران قدیمی")

        def side_effect(url, json=None, **kwargs):
            chat_id = json["chat_id"]
            if chat_id == "920000002":
                return DummyBotResponse({"ok": False, "description": "Forbidden: bot was blocked by the user"})
            if chat_id == "920000003":
                raise requests.Timeout("timed out")
            return DummyBotResponse({"ok": True, "result": {"message_id": 10}})

        post_mock.side_effect = side_effect

        counts = send_legacy_import_message_batch(batch)

        batch.refresh_from_db()
        self.assertEqual(batch.status, LegacyWizWizImportMessageBatch.Status.PARTIAL)
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(
            batch.recipients.get(row__telegram_user_id="920000002").status,
            LegacyWizWizImportMessageRecipient.Status.BLOCKED,
        )
        self.assertEqual(
            batch.recipients.get(row__telegram_user_id="920000003").status,
            LegacyWizWizImportMessageRecipient.Status.FAILED,
        )
        self.assertEqual(post_mock.call_count, 4)

    @patch("store.telegram_bot.client.requests.post", return_value=DummyBotResponse({"ok": True, "result": {}}))
    def test_send_empty_or_config_link_text_is_rejected_and_resend_does_not_duplicate_recipients(self, post_mock):
        job = self.make_applied_job()

        with self.assertRaises(ValidationError):
            create_legacy_import_message_batch(job, "")
        with self.assertRaises(ValidationError):
            create_legacy_import_message_batch(job, "vless://secret-config")

        batch = create_legacy_import_message_batch(job, "سلام امن")
        send_legacy_import_message_batch(batch)
        send_legacy_import_message_batch(batch)

        self.assertEqual(batch.recipients.count(), 4)
        self.assertEqual(post_mock.call_count, 4)
        batch.refresh_from_db()
        self.assertEqual(batch.status, LegacyWizWizImportMessageBatch.Status.SENT)


class LegacyWizWizBroadcastAudienceTests(LegacyWizWizImportBase):
    def test_legacy_wizwiz_audience_returns_distinct_imported_customers(self):
        imported = Customer.objects.create(username="imported", display_name="Imported")
        other = Customer.objects.create(username="other", display_name="Other")
        job = self.make_job(
            wizwiz_insert(["(1, '123123123', 'Imported', 'imported', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]),
            status=LegacyWizWizImportJob.Status.APPLIED,
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="123123123",
            telegram_user_id_masked="12***123",
            status=LegacyWizWizImportRow.Status.CREATED,
            customer=imported,
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="123123124",
            telegram_user_id_masked="12***124",
            status=LegacyWizWizImportRow.Status.LINKED,
            customer=imported,
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="123123125",
            telegram_user_id_masked="12***125",
            status=LegacyWizWizImportRow.Status.SKIPPED,
            customer=other,
        )

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED))

        self.assertEqual(customers, [imported])

    def test_legacy_wizwiz_audience_without_chat_id_is_skipped_at_send_resolution(self):
        customer = Customer.objects.create(username="imported", display_name="Imported")
        job = self.make_job(
            wizwiz_insert(["(1, '321321321', 'Imported', 'imported', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"]),
            status=LegacyWizWizImportJob.Status.APPLIED,
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="321321321",
            telegram_user_id_masked="32***321",
            status=LegacyWizWizImportRow.Status.CREATED,
            customer=customer,
        )
        campaign = BroadcastMessage.objects.create(
            store=self.store,
            title="Legacy audience",
            message_text="Hello",
            audience_type=BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED,
            channel=BroadcastMessage.Channel.TELEGRAM,
        )

        rows = resolve_campaign_recipients(campaign)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], BroadcastRecipient.Status.SKIPPED)

    def test_legacy_wizwiz_audience_ignores_non_applied_preview_rows(self):
        customer = Customer.objects.create(username="preview", display_name="Preview")
        job = self.make_job(
            wizwiz_insert(["(1, '454545454', 'Preview', 'preview', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"])
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="454545454",
            telegram_user_id_masked="45***454",
            status=LegacyWizWizImportRow.Status.EXISTING,
            customer=customer,
        )

        customers = list(get_customers_for_audience(BroadcastMessage.AudienceType.LEGACY_WIZWIZ_IMPORTED))

        self.assertEqual(customers, [])


class LegacyWizWizIntegrationCheckTests(LegacyWizWizImportBase):
    def test_check_integrations_reports_latest_and_failed_jobs(self):
        LegacyWizWizImportJob.objects.create(
            title="failed.sql",
            uploaded_file="private/legacy_imports/wizwiz/failed.sql",
            status=LegacyWizWizImportJob.Status.FAILED,
        )
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("Legacy WizWiz import", output)
        self.assertIn("failed legacy WizWiz import job", output)

    def test_check_integrations_counts_only_applied_legacy_wizwiz_audience(self):
        customer = Customer.objects.create(username="preview_check", display_name="Preview Check")
        job = self.make_job(
            wizwiz_insert(["(1, '787878787', 'Preview', 'preview_check', '', 0, '', '', '', '', '', 0, '', '', 0, 0, '', '')"])
        )
        LegacyWizWizImportRow.objects.create(
            job=job,
            telegram_user_id="787878787",
            telegram_user_id_masked="78***787",
            status=LegacyWizWizImportRow.Status.EXISTING,
            customer=customer,
        )
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        self.assertIn("0 imported legacy customer(s)", out.getvalue())


class BroadcastCampaignTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
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

    def make_bot_user(
        self,
        *,
        user_id="42",
        username="alice",
        display_name="Alice",
        customer=None,
        state=BotUser.State.BUY_WAIT_RECEIPT,
        state_data=None,
    ):
        customer = customer or Customer.objects.create(display_name=display_name, username=username)
        return BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id=str(user_id),
            chat_id=str(user_id),
            username=username,
            display_name=display_name,
            state=state,
            state_data=state_data or {},
        )

    def receipt_file_info(self, *, file_id="receipt-file", unique_id="receipt", message_id=20):
        return {
            "kind": "photo",
            "file_id": file_id,
            "file_unique_id": unique_id,
            "file_name": "telegram-receipt.jpg",
            "message_id": message_id,
        }

    def bot_post_side_effect(self, calls=None, *, file_path="photos/receipt.jpg", message_id_start=100):
        calls = calls if calls is not None else []
        next_message_id = {"value": message_id_start}

        def side_effect(url, json=None, data=None, files=None, **kwargs):
            calls.append({"url": url, "json": json, "data": data, "files": files, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": file_path}})
            if url.endswith("/sendPhoto") or url.endswith("/sendMessage"):
                next_message_id["value"] += 1
                return DummyBotResponse({"ok": True, "result": {"message_id": next_message_id["value"]}})
            return DummyBotResponse()

        return side_effect

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("10101010-1010-4010-8010-101010101010"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_purchase_creates_pending_order_using_plan_route(self, _get_mock, xui_mock):
        from . import bots

        routed_panel = Panel.objects.create(
            store=self.store,
            name="Routed Panel",
            url="https://routed.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        routed_inbound = Inbound.objects.create(
            panel=routed_panel,
            inbound_id=2,
            server_ip="vpn-routed.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=routed_inbound, priority=1)
        bot_user = self.make_bot_user(
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Alice Laptop",
                "payment_time": "14:35",
            }
        )
        post_calls = []

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect(post_calls)):
            with self.captureOnCommitCallbacks(execute=True):
                result = bots.finalize_bot_purchase(
                    self.bot_config,
                    bot_user,
                    {},
                    self.receipt_file_info(),
                    chat_id="42",
                )

        self.assertTrue(result["success"])
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertEqual(order.inbound, routed_inbound)
        self.assertEqual(order.sender_card_name, "Alice Laptop")
        self.assertTrue(order.payment_receipt_image.name.endswith(".jpg"))
        self.assertEqual(order.metadata["source"], "telegram_bot")
        self.assertEqual(order.metadata["receipt"]["file_id"], "receipt-file")
        self.assertEqual(xui_mock.call_args.kwargs["inbound"], routed_inbound)
        self.assertEqual(BotAdminOrderMessage.objects.filter(order=order).count(), 1)
        order.refresh_from_db()
        self.assertIsNotNone(order.admin_notified_at)
        self.assertIsNotNone(order.admin_receipt_notified_at)

    @patch("store.order_services.create_inactive_client_details")
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_purchase_fails_safely_when_plan_route_missing_and_fallback_disabled(self, _get_mock, xui_mock):
        from . import bots
        from .order_services import PLAN_INBOUND_ROUTE_MISSING_MESSAGE

        self.store.allow_global_inbound_fallback = False
        self.store.save(update_fields=["allow_global_inbound_fallback", "updated_at"])
        bot_user = self.make_bot_user(
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "payment_time": "14:35",
            }
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_bot_purchase(
                self.bot_config,
                bot_user,
                {},
                self.receipt_file_info(),
                chat_id="42",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], PLAN_INBOUND_ROUTE_MISSING_MESSAGE)
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("20202020-2020-4020-8020-202020202020"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_purchase_preserves_custom_volume_quantity_discount_and_clean_name(self, _get_mock, xui_mock):
        from . import bots
        from .order_services import get_or_create_custom_volume_plan

        self.store.custom_volume_price_per_gb = Decimal("100000")
        self.store.save(update_fields=["custom_volume_price_per_gb", "updated_at"])
        custom_plan = get_or_create_custom_volume_plan(self.store, "7")
        DiscountCode.objects.create(
            code="SAVE10",
            discount_type=DiscountCode.DiscountType.FIXED,
            value=10000,
        )
        bot_user = self.make_bot_user(
            state_data={
                "plan_id": custom_plan.pk,
                "quantity": 2,
                "sender_card_name": "Alice Laptop!!",
                "payment_time": "14:35",
                "discount_code": "save10",
                "discount_amount": 10000,
            }
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_bot_purchase(
                self.bot_config,
                bot_user,
                {},
                self.receipt_file_info(),
                chat_id="42",
            )

        self.assertTrue(result["success"])
        order = Order.objects.get()
        self.assertEqual(order.plan, custom_plan)
        self.assertEqual(order.quantity, 2)
        self.assertEqual(order.original_amount, 1400000)
        self.assertEqual(order.discount_code_text, "SAVE10")
        self.assertEqual(order.discount_amount, 10000)
        self.assertEqual(order.amount, 1390000)
        self.assertTrue(order.metadata["custom_volume"])
        self.assertEqual(order.metadata["custom_volume_gb"], "7.000")
        self.assertEqual(order.vpn_clients.count(), 1)
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_laptop_[0-9a-z]{8}$")

    @patch("store.bots.sync_vpn_client_stats", return_value={})
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("30303030-3030-4030-8030-303030303030"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_purchase_duplicate_reuses_pending_order_without_reprovisioning(self, _get_mock, xui_mock, _stats_mock):
        from . import bots

        state_data = {
            "plan_id": self.plan.pk,
            "quantity": 1,
            "sender_card_name": "Alice Laptop",
            "payment_time": "14:35",
        }
        bot_user = self.make_bot_user(state_data=dict(state_data))

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            with self.captureOnCommitCallbacks(execute=True):
                first = bots.finalize_bot_purchase(
                    self.bot_config,
                    bot_user,
                    {},
                    self.receipt_file_info(file_id="receipt-a", unique_id="receipt-a", message_id=20),
                    chat_id="42",
                )
            bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
            bot_user.state_data = dict(state_data)
            bot_user.save(update_fields=["state", "state_data", "updated_at"])
            with self.captureOnCommitCallbacks(execute=True):
                duplicate = bots.finalize_bot_purchase(
                    self.bot_config,
                    bot_user,
                    {},
                    self.receipt_file_info(file_id="receipt-b", unique_id="receipt-b", message_id=21),
                    chat_id="42",
                )

        self.assertTrue(first["success"])
        self.assertTrue(duplicate["success"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(xui_mock.call_count, 1)
        order = Order.objects.get()
        self.assertTrue(order.metadata["duplicate_warning"]["detected"])
        self.assertEqual(order.metadata["duplicate_warning"]["attempt_count"], 1)

    @patch("store.order_services.create_inactive_client_details")
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=b"not an image"))
    def test_finalize_bot_purchase_invalid_downloaded_receipt_does_not_create_order(self, _get_mock, xui_mock):
        from . import bots

        bot_user = self.make_bot_user(
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "payment_time": "14:35",
            }
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_bot_purchase(
                self.bot_config,
                bot_user,
                {},
                self.receipt_file_info(),
                chat_id="42",
            )

        self.assertFalse(result["success"])
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()

    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_renewal_creates_pending_order_on_existing_client_and_blocks_duplicate(self, _get_mock):
        from . import bots

        customer = Customer.objects.create(display_name="Alice", username="alice")
        original_order = Order.objects.create(
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
            uuid="40404040-4040-4040-8040-404040404040",
            sub_link="https://example.com/sub/old",
            direct_link="vless://old",
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=original_order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_1gb",
            xui_email="alice_1gb",
            uuid=original_order.uuid,
            sub_id="old",
            sub_link=original_order.sub_link,
            direct_link=original_order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )
        DiscountCode.objects.create(
            code="RENEW10",
            discount_type=DiscountCode.DiscountType.FIXED,
            value=10000,
        )
        bot_user = self.make_bot_user(
            customer=customer,
            state_data={
                "flow": "renewal",
                "renewal_client_public_id": str(vpn_client.public_id),
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Alice Renewal",
                "payment_time": "14:35",
                "discount_code": "renew10",
                "discount_amount": 10000,
            },
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            with self.captureOnCommitCallbacks(execute=True):
                first = bots.finalize_bot_renewal(
                    self.bot_config,
                    bot_user,
                    {},
                    self.receipt_file_info(file_id="renewal-a", unique_id="renewal-a", message_id=30),
                    chat_id="42",
                )
            bot_user.state = BotUser.State.BUY_WAIT_RECEIPT
            bot_user.state_data = {
                "flow": "renewal",
                "renewal_client_public_id": str(vpn_client.public_id),
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Alice Renewal",
                "payment_time": "14:35",
            }
            bot_user.save(update_fields=["state", "state_data", "updated_at"])
            duplicate = bots.finalize_bot_renewal(
                self.bot_config,
                bot_user,
                {},
                self.receipt_file_info(file_id="renewal-b", unique_id="renewal-b", message_id=31),
                chat_id="42",
            )

        self.assertTrue(first["success"])
        self.assertTrue(duplicate["pending"])
        renewal = Order.objects.exclude(pk=original_order.pk).get()
        self.assertEqual(renewal.status, Order.Status.PENDING_VERIFICATION)
        self.assertEqual(renewal.inbound, self.inbound)
        self.assertEqual(renewal.metadata["renewal_client_pk"], vpn_client.pk)
        self.assertEqual(renewal.discount_code_text, "RENEW10")
        self.assertEqual(renewal.discount_amount, 10000)
        self.assertNotIn("suppress_new_order_notification", renewal.metadata)
        self.assertEqual(VPNClient.objects.count(), 1)

    def test_finalize_bot_renewal_rejects_deleted_client(self):
        from . import bots

        customer = Customer.objects.create(display_name="Alice", username="alice")
        original_order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="alice_deleted",
            uuid="50505050-5050-4050-8050-505050505050",
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=original_order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_deleted",
            xui_email="alice_deleted",
            uuid=original_order.uuid,
            status=VPNClient.Status.DELETED,
            deleted_at=timezone.now(),
        )
        bot_user = self.make_bot_user(
            customer=customer,
            state_data={
                "flow": "renewal",
                "renewal_client_public_id": str(vpn_client.public_id),
                "plan_id": self.plan.pk,
            },
        )

        with patch("store.bots.requests.post", return_value=DummyBotResponse()):
            result = bots.finalize_bot_renewal(
                self.bot_config,
                bot_user,
                {},
                {},
                chat_id="42",
            )

        self.assertFalse(result["success"])
        self.assertEqual(Order.objects.exclude(pk=original_order.pk).count(), 0)
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.IDLE)

    @patch("store.order_actions.enable_client", return_value=True)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("60606060-6060-4060-8060-606060606060"))
    def test_finalize_admin_direct_purchase_activates_on_plan_route(self, xui_mock, _enable_mock):
        from . import bots

        routed_panel = Panel.objects.create(
            store=self.store,
            name="Admin Routed Panel",
            url="https://admin-routed.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        routed_inbound = Inbound.objects.create(
            panel=routed_panel,
            inbound_id=3,
            server_ip="vpn-admin-routed.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=routed_inbound, priority=1)
        bot_user = self.make_bot_user(
            user_id="999",
            username="admin",
            display_name="Admin",
            state=BotUser.State.BUY_WAIT_NAME,
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Admin Config",
                "payment_time": "14:35",
            },
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_admin_direct_purchase(self.bot_config, bot_user, chat_id="999")

        self.assertTrue(result["success"])
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.inbound, routed_inbound)
        self.assertTrue(order.metadata["admin_direct_purchase"])
        self.assertEqual(xui_mock.call_args.kwargs["inbound"], routed_inbound)

    @patch("store.order_actions.enable_client", return_value=False)
    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("70707070-7070-4070-8070-707070707070"))
    def test_finalize_admin_direct_purchase_xui_enable_failure_does_not_fake_complete(self, _xui_mock, _enable_mock):
        from . import bots

        bot_user = self.make_bot_user(
            user_id="999",
            username="admin",
            display_name="Admin",
            state=BotUser.State.BUY_WAIT_NAME,
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Admin Config",
                "payment_time": "14:35",
            },
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_admin_direct_purchase(self.bot_config, bot_user, chat_id="999")

        self.assertFalse(result["success"])
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertEqual(order.verification_status, Order.VerificationStatus.PENDING)
        self.assertFalse(order.vpn_clients.filter(status=VPNClient.Status.ACTIVE).exists())

    @patch("store.order_actions.renew_client")
    def test_finalize_admin_direct_renewal_extends_existing_client_without_creating_new_client(self, renew_mock):
        from . import bots

        customer = Customer.objects.create(display_name="Admin", username="admin")
        original_order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="admin_1gb",
            uuid="80808080-8080-4080-8080-808080808080",
            sub_link="https://example.com/sub/admin",
            direct_link="vless://admin",
        )
        vpn_client = VPNClient.objects.create(
            store=self.store,
            order=original_order,
            plan=self.plan,
            inbound=self.inbound,
            username="admin_1gb",
            xui_email="admin_1gb",
            uuid=original_order.uuid,
            sub_id="admin",
            sub_link=original_order.sub_link,
            direct_link=original_order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )
        renew_mock.return_value = {
            "expiry_at": timezone.now() + timedelta(days=60),
            "raw": {"renewed": True},
        }
        bot_user = self.make_bot_user(
            user_id="999",
            username="admin",
            display_name="Admin",
            customer=customer,
            state=BotUser.State.BUY_WAIT_NAME,
            state_data={
                "flow": "renewal",
                "renewal_client_public_id": str(vpn_client.public_id),
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Admin Renewal",
                "payment_time": "14:35",
            },
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            result = bots.finalize_admin_direct_renewal(self.bot_config, bot_user, chat_id="999")

        self.assertTrue(result["success"])
        renewal = Order.objects.exclude(pk=original_order.pk).get()
        self.assertEqual(renewal.status, Order.Status.COMPLETED)
        self.assertEqual(renewal.inbound, self.inbound)
        self.assertEqual(renewal.metadata["renewal_client_pk"], vpn_client.pk)
        self.assertEqual(VPNClient.objects.count(), 1)
        vpn_client.refresh_from_db()
        self.assertEqual(vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertEqual(vpn_client.xui_raw, {"renewed": True})
        renew_mock.assert_called_once()

    @patch("store.order_services.create_inactive_client_details")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_regular_user_config_name_flow_waits_for_receipt_instead_of_admin_direct_activation(self, _post_mock, xui_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))

        response = self.post_update(self.message("Normal Config", message_id=2))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())
        xui_mock.assert_not_called()
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertEqual(bot_user.state_data["sender_card_name"], "Normal Config")

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("90909090-9090-4090-8090-909090909090"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_finalize_bot_purchase_notification_failure_does_not_abort_order_creation(self, _get_mock, _xui_mock):
        from . import bots

        bot_user = self.make_bot_user(
            state_data={
                "plan_id": self.plan.pk,
                "quantity": 1,
                "sender_card_name": "Alice Laptop",
                "payment_time": "14:35",
            }
        )

        with patch("store.bots.requests.post", side_effect=self.bot_post_side_effect()):
            with patch("store.bots.send_new_order_to_config", side_effect=Exception("notification boom")):
                with self.captureOnCommitCallbacks(execute=True):
                    result = bots.finalize_bot_purchase(
                        self.bot_config,
                        bot_user,
                        {},
                        self.receipt_file_info(),
                        chat_id="42",
                    )

        self.assertTrue(result["success"])
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertTrue(BotEventLog.objects.filter(order=order, status=BotEventLog.Status.FAILED).exists())

    def test_bot_event_log_redacts_receipt_file_ids_tokens_and_config_links(self):
        from .bots import log_event

        log_event(
            self.bot_config,
            event_type=BotEventLog.EventType.ERROR,
            status=BotEventLog.Status.FAILED,
            message="Failed for vless://11111111-1111-4111-8111-111111111111@example.com link_SECRET",
            raw_payload={
                "receipt": {
                    "file_id": "telegram-file-id-secret",
                    "file_unique_id": "telegram-file-unique-secret",
                    "file_path": "photos/private-receipt.jpg",
                },
                "bot_token": self.bot_config.bot_token,
                "url": "https://example.com/sub/private-subscription-token",
            },
        )

        event = BotEventLog.objects.get()
        serialized = json.dumps(event.raw_payload, ensure_ascii=False)
        self.assertNotIn("telegram-file-id-secret", serialized)
        self.assertNotIn("telegram-file-unique-secret", serialized)
        self.assertNotIn("private-receipt.jpg", serialized)
        self.assertNotIn(self.bot_config.bot_token, serialized)
        self.assertNotIn("private-subscription-token", serialized)
        self.assertIn("<receipt-file-redacted>", serialized)
        self.assertIn("<redacted-token>", serialized)
        self.assertIn("<config-link-redacted>", serialized)
        self.assertIn("<config-link-redacted>", event.message)

    def enable_force_join(self, *, channel_id="", username="vpn_store_channel", invite_link="https://t.me/vpn_store_channel"):
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
        self.assertIn("از منوی زیر انتخاب کنید", payload["text"])

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

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_link_payload_links_bot_user_to_web_customer(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        customer = Customer.objects.create(display_name="Web Customer")
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.customer, customer)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.USED)
        self.assertEqual(token.bot_user, bot_user)
        self.assertIsNotNone(token.used_at)
        sent_texts = [call.kwargs["json"]["text"] for call in post_mock.call_args_list]
        self.assertTrue(any("✅ حساب شما به ربات وصل شد" in text for text in sent_texts))
        self.assertTrue(any("از منوی زیر انتخاب کنید" in text for text in sent_texts))
        self.assertEqual(Customer.objects.count(), 1)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_used_start_link_payload_cannot_be_reused(self, _post):
        from .telegram_link_services import create_web_telegram_link_token

        customer = Customer.objects.create(display_name="Web Customer")
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")
        self.post_update(self.message(f"/start link_{raw_token}", user_id=42))

        response = self.post_update(self.message(f"/start link_{raw_token}", user_id=43, username="bob", first_name="Bob"))

        self.assertEqual(response.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.USED)
        second_bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="43")
        self.assertIsNone(second_bot_user.customer)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_used_start_link_payload_is_idempotent_for_same_bot_user(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        customer = Customer.objects.create(display_name="Web Customer")
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")
        self.post_update(self.message(f"/start link_{raw_token}", user_id=42))
        post_mock.reset_mock()

        response = self.post_update(self.message(f"/start link_{raw_token}", user_id=42))

        self.assertEqual(response.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.USED)
        self.assertEqual(BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42").customer, customer)
        sent_texts = [call.kwargs["json"]["text"] for call in post_mock.call_args_list]
        self.assertTrue(any("قبلاً به ربات وصل شده است" in text for text in sent_texts))
        self.assertTrue(any("از منوی زیر انتخاب کنید" in text for text in sent_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_expired_start_link_payload_is_rejected(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        customer = Customer.objects.create(display_name="Web Customer")
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")
        WebTelegramLinkToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(minutes=1))

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.EXPIRED)
        sent_text = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("لینک اتصال نامعتبر یا منقضی شده است", sent_text)
        self.assertIsNone(BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42").customer)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_invalid_start_link_payload_is_rejected_and_redacted_in_event_log(self, post_mock):
        raw_token = "shortsecret"

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        sent_text = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("لینک اتصال نامعتبر یا منقضی شده است", sent_text)
        logs_text = json.dumps(list(BotEventLog.objects.values("message", "raw_payload")), ensure_ascii=False)
        self.assertNotIn(raw_token, logs_text)
        self.assertNotIn(f"link_{raw_token}", logs_text)
        self.assertIn("link_<redacted>", logs_text)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_link_payload_for_same_customer_returns_already_linked(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        customer = Customer.objects.create(display_name="Web Customer")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.USED)
        sent_texts = [call.kwargs["json"]["text"] for call in post_mock.call_args_list]
        self.assertTrue(any("قبلاً به ربات وصل شده است" in text for text in sent_texts))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_start_link_payload_does_not_move_bot_user_from_other_customer(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        target_customer = Customer.objects.create(display_name="Web Customer")
        existing_customer = Customer.objects.create(display_name="Existing Bot Customer")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=existing_customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        raw_token, token = create_web_telegram_link_token(target_customer, source="dashboard")

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42")
        self.assertEqual(bot_user.customer, existing_customer)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.ACTIVE)
        sent_text = post_mock.call_args.kwargs["json"]["text"]
        self.assertIn("این حساب تلگرام قبلاً به یک حساب دیگر وصل شده است", sent_text)

    @patch("store.bots.requests.post")
    def test_force_join_start_link_keeps_link_after_membership_guard(self, post_mock):
        from .telegram_link_services import create_web_telegram_link_token

        self.enable_force_join()
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")
        customer = Customer.objects.create(display_name="Web Customer")
        raw_token, token = create_web_telegram_link_token(customer, source="dashboard")

        response = self.post_update(self.message(f"/start link_{raw_token}"))

        self.assertEqual(response.status_code, 200)
        token.refresh_from_db()
        self.assertEqual(token.status, WebTelegramLinkToken.Status.USED)
        self.assertEqual(BotUser.objects.get(bot_config=self.bot_config, provider_user_id="42").customer, customer)
        sent_texts = [payload["text"] for payload in self.sent_message_payloads(post_calls)]
        self.assertTrue(any("✅ حساب شما به ربات وصل شد" in text for text in sent_texts))
        self.assertTrue(any("برای استفاده از ربات ابتدا عضو کانال شوید" in text for text in sent_texts))
        self.assertFalse(any("از منوی زیر انتخاب کنید" in text for text in sent_texts))

    @patch("store.bots.requests.post")
    def test_force_join_disabled_does_not_check_membership(self, post_mock):
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(call["url"].endswith("/getChatMember") for call in post_calls))
        self.assertIn("خرید سرویس", self.sent_message_payloads(post_calls)[-1]["text"])

    @patch("store.bots.requests.post")
    def test_force_join_enabled_allows_channel_member(self, post_mock):
        self.enable_force_join(username="vpn_store_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="member")

        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        membership_payloads = [
            call["json"]
            for call in post_calls
            if call["url"].endswith("/getChatMember")
        ]
        self.assertEqual(membership_payloads[0]["chat_id"], "@vpn_store_channel")
        self.assertEqual(membership_payloads[0]["user_id"], 42)
        self.assertIn("خرید سرویس", self.sent_message_payloads(post_calls)[-1]["text"])

    @patch("store.bots.requests.post")
    def test_force_join_enabled_blocks_non_member(self, post_mock):
        self.enable_force_join(username="vpn_store_channel")
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
        self.assertIn("https://t.me/vpn_store_channel", urls)
        self.assertFalse(any("خرید سرویس" in payload["text"] for payload in self.sent_message_payloads(post_calls)))

    @patch("store.bots.requests.post")
    def test_check_membership_callback_shows_menu_after_join(self, post_mock):
        self.enable_force_join(username="vpn_store_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="member")

        response = self.post_update(self.callback("check_membership", callback_id="membership-cb"))

        self.assertEqual(response.status_code, 200)
        message_payload = self.sent_message_payloads(post_calls)[-1]
        self.assertIn("از منوی زیر انتخاب کنید", message_payload["text"])
        callback_values = [
            button["callback_data"]
            for row in message_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:buy", callback_values)

    @patch("store.bots.requests.post")
    def test_force_join_admin_bypass_skips_membership_check(self, post_mock):
        self.enable_force_join(username="vpn_store_channel")
        post_calls = []
        post_mock.side_effect = self.membership_post_side_effect(post_calls, status="left")

        response = self.post_update(
            self.callback("user:buy", user_id=999, username="admin", first_name="Admin")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(any(call["url"].endswith("/getChatMember") for call in post_calls))
        self.assertIn("خرید سرویس", self.sent_message_payloads(post_calls)[-1]["text"])

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
        self.enable_force_join(username="vpn_store_channel")
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
        self.enable_force_join(username="vpn_store_channel")
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
        payloads = [
            call.kwargs["json"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "42"
            and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("تست رایگان شما آماده شد" in payload["text"] for payload in payloads))
        config_payload = next(payload for payload in payloads if "vless://example" in payload["text"])
        self.assertEqual(config_payload["parse_mode"], "HTML")
        self.assertIn("<pre>", config_payload["text"])
        self.assertIn("https://example.com/sub/sub123", config_payload["text"])
        self.assertIn("🔗 لینک اشتراک", config_payload["text"])
        self.assertIn("⚡ لینک مستقیم", config_payload["text"])
        self.assertTrue(
            any(
                button.get("copy_text", {}).get("text") == "vless://example"
                for row in config_payload["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )
        self.assertTrue(
            any(
                button.get("copy_text", {}).get("text") == "https://example.com/sub/sub123"
                for row in config_payload["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )
        xui_mock.assert_called_once()

    @patch("store.free_trial_services.create_trial_client_details")
    @patch("store.bots.requests.post")
    def test_free_trial_force_join_guard_blocks_non_member(self, post_mock, xui_mock):
        self.enable_free_trial()
        self.enable_force_join(username="vpn_store_channel")
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

    def send_admin_lookup_and_get_callbacks(self, post_mock, *, client_id="11111111-1111-4111-8111-111111111111"):
        full_link = f"vless://{client_id}@vpn.example.com:443?type=tcp&security=none#private-remark"
        total = 30 * (1024 ** 3)
        with patch(
            "store.bots.check_config_usage",
            return_value={
                "found": True,
                "message": "📊 وضعیت کانفیگ شما\n\nمصرف‌شده: ۱ گیگ",
                "panel": self.panel,
                "panel_id": self.panel.pk,
                "panel_name": self.panel.name,
                "inbound": self.inbound,
                "inbound_id": self.inbound.inbound_id,
                "inbound_remark": self.inbound.remark,
                "identifier": client_id,
                "masked_identifier": "111111...1111",
                "protocol": "vless",
                "email": "alice_config",
                "enabled": True,
                "total_bytes": total,
                "used_bytes": 1024 ** 3,
                "remaining_bytes": total - (1024 ** 3),
            },
        ):
            self.post_update(self.callback("user:config_lookup", callback_id="admin-lookup-cb", user_id=999, username="admin"))
            self.post_update(self.message(full_link, message_id=23, user_id=999, username="admin", first_name="Admin"))

        payload = post_mock.call_args.kwargs["json"]
        callbacks = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        return callbacks

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_config_lookup_shows_management_buttons_without_identifier_leak(self, post_mock):
        client_id = "11111111-1111-4111-8111-111111111111"
        callbacks = self.send_admin_lookup_and_get_callbacks(post_mock, client_id=client_id)

        self.assertTrue(any(value.startswith("admin:config_delete:") for value in callbacks))
        self.assertTrue(any(value.startswith("admin:config_edit_traffic:") for value in callbacks))
        self.assertTrue(any(value.startswith("admin:config_edit_expiry:") for value in callbacks))
        self.assertTrue(any(value.startswith("admin:config_refresh_link:") for value in callbacks))
        self.assertFalse(any(value.startswith("user:config_lookup_update:") for value in callbacks))
        for callback_data in callbacks:
            self.assertNotIn(client_id, callback_data)
            self.assertNotIn("vless://", callback_data)

    @patch("store.bots.delete_vpn_client_by_admin_lookup")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_admin_config_delete_confirmation_calls_service(self, post_mock, delete_mock):
        callbacks = self.send_admin_lookup_and_get_callbacks(post_mock)
        delete_callback = next(value for value in callbacks if value.startswith("admin:config_delete:"))
        delete_mock.return_value = {"success": True, "local_match_status": "not_found"}

        self.post_update(self.callback(delete_callback, callback_id="admin-delete", user_id=999, username="admin"))
        confirm_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("⚠️ حذف کانفیگ از پنل", confirm_payload["text"])
        confirm_callbacks = [
            button["callback_data"]
            for row in confirm_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        confirm_callback = next(value for value in confirm_callbacks if value.startswith("admin:config_delete_confirm:"))

        response = self.post_update(
            self.callback(confirm_callback, callback_id="admin-delete-confirm", user_id=999, username="admin")
        )

        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once()
        self.assertEqual(delete_mock.call_args.args[0], "999")
        self.assertIn("✅ کانفیگ از پنل حذف شد", post_mock.call_args.kwargs["json"]["text"])

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
        self.assertIn(updated_link.replace("&", "&amp;"), payload["text"])
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(
            any(
                button.get("copy_text", {}).get("text") == updated_link
                for row in payload["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )
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
        self.enable_force_join(username="vpn_store_channel")
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
            if call["url"].endswith("/sendMessage") and "خرید سرویس" in call.get("json", {}).get("text", "")
        )
        self.assertLess(delete_index, next_prompt_index)
        delete_payload = post_calls[delete_index]["json"]
        self.assertEqual(delete_payload["chat_id"], "42")
        self.assertEqual(delete_payload["message_id"], 77)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_tunnel_plan_list_text_is_simple_and_buttons_have_plan_details(self, post_mock):
        response = self.post_update(self.callback("user:buy"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("خرید سرویس", payload["text"])
        self.assertIn("یکی از پلن‌های زیر را انتخاب کنید", payload["text"])
        self.assertNotIn(self.plan.name, payload["text"])
        plan_buttons = [
            button
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("user:buyplan:")
        ]
        self.assertTrue(plan_buttons)
        self.assertIn("۱ گیگابایت", plan_buttons[0]["text"])
        self.assertIn("۳۰ روزه", plan_buttons[0]["text"])
        self.assertIn("۱۰۰,۰۰۰ تومان", plan_buttons[0]["text"])

    @override_settings(BOT_COPY_TEXT_DISABLED=True)
    def test_copy_text_button_falls_back_when_copy_text_is_disabled(self):
        from .bots import build_copy_text_button

        button = build_copy_text_button(
            "کپی مبلغ",
            "250000",
            config=self.bot_config,
            fallback_callback_data="user:copy:payment_amount",
        )

        self.assertEqual(button["text"], "نمایش مبلغ")
        self.assertEqual(button["callback_data"], "user:copy:payment_amount")
        self.assertNotIn("copy_text", button)

    def test_copy_text_button_short_and_long_fallback_behaviour(self):
        from .bots import build_copy_text_button

        short_button = build_copy_text_button("کپی", "short-value", config=self.bot_config)
        long_link = "vless://" + ("a" * 300)
        fallback_button = build_copy_text_button(
            "کپی لینک",
            long_link,
            config=self.bot_config,
            fallback_callback_data="user:copy_config:direct:safe-token",
        )

        self.assertEqual(short_button["copy_text"]["text"], "short-value")
        self.assertEqual(fallback_button["callback_data"], "user:copy_config:direct:safe-token")
        self.assertNotIn("copy_text", fallback_button)
        self.assertNotIn(long_link, fallback_button["callback_data"])

    def test_payment_keyboard_contains_only_copy_back_and_cancel_actions(self):
        from .bots import build_payment_keyboard

        keyboard = build_payment_keyboard("0000 0000 0000 0000", 300000, config=self.bot_config)
        rows = keyboard["inline_keyboard"]
        self.assertEqual(
            [[button["text"] for button in row] for row in rows],
            [["کپی شماره کارت", "کپی مبلغ"], ["برگشت"], ["لغو"]],
        )

        buttons = [button for row in rows for button in row]
        texts = [button["text"] for button in buttons]
        callbacks = [button.get("callback_data", "") for button in buttons]
        self.assertNotIn("ارسال رسید", texts)
        self.assertNotIn("تعیین نام کانفیگ", texts)
        self.assertNotIn("user:payment_name:start", callbacks)
        self.assertNotIn("user:payment_receipt_only", callbacks)
        self.assertNotIn("ارسال رسید کردم / راهنما", texts)
        self.assertNotIn("راهنمای پرداخت ❓", texts)
        self.assertNotIn("user:payment_receipt_help", callbacks)
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "0000000000000000" for button in buttons))
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "300000" for button in buttons))

    def test_formatting_and_keyboard_helpers_remain_import_compatible_from_store_bots(self):
        from .bots import (
            build_payment_keyboard,
            format_card_for_copy,
            format_money_for_copy,
            sanitize_bot_event_log_value,
            telegram_code,
        )

        self.assertEqual(format_money_for_copy(Decimal("100000")), "100000")
        self.assertEqual(format_card_for_copy("۰۰۰۰ ۰۰۰۰ ۰۰۰۰ ۰۰۰۰"), "0000000000000000")
        keyboard = build_payment_keyboard("۰۰۰۰ ۰۰۰۰ ۰۰۰۰ ۰۰۰۰", Decimal("100000"), config=self.bot_config)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "0000000000000000" for button in buttons))
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "100000" for button in buttons))
        self.assertEqual(
            telegram_code("vless://host/path?a=1&b=<tag>", block=True),
            "<pre>vless://host/path?a=1&amp;b=&lt;tag&gt;</pre>",
        )
        self.assertEqual(
            sanitize_bot_event_log_value({"link_SECRET": ["trojan://secret@example.com"]}),
            {"link_<redacted>": ["<config-link-redacted>"]},
        )

    def test_config_delivery_and_payment_helpers_remain_import_compatible_from_store_bots(self):
        from . import bots
        from .telegram_bot import config_delivery, payments, services_flow, user_menu

        self.assertIs(bots.send_config_links_message, config_delivery.send_config_links_message)
        self.assertIs(bots.send_copyable_config_message, config_delivery.send_copyable_config_message)
        self.assertIs(bots.handle_config_copy_callback, config_delivery.handle_config_copy_callback)
        self.assertIs(bots.main_menu_keyboard, user_menu.main_menu_keyboard)
        self.assertIs(bots.help_text, user_menu.help_text)
        self.assertIs(bots.profile_keyboard, user_menu.profile_keyboard)
        self.assertIs(bots.format_profile, user_menu.format_profile)
        self.assertIs(bots.send_profile, user_menu.send_profile)
        self.assertIs(bots.bot_client_label, services_flow.bot_client_label)
        self.assertIs(bots.bot_client_status, services_flow.bot_client_status)
        self.assertIs(bots.bot_subscription_clients, services_flow.bot_subscription_clients)
        self.assertIs(bots.subscription_management_keyboard, services_flow.subscription_management_keyboard)
        self.assertIs(bots.client_config_keyboard, services_flow.client_config_keyboard)
        self.assertIs(bots.client_config_links, services_flow.client_config_links)
        self.assertIs(bots.user_client_delete_button, services_flow.user_client_delete_button)
        self.assertIs(bots.user_client_delete_confirmation_keyboard, services_flow.user_client_delete_confirmation_keyboard)
        self.assertIs(bots.store_payment_lines, payments.store_payment_lines)
        self.assertIs(bots.format_payment_prompt, payments.format_payment_prompt)
        self.assertIs(bots.bot_payment_sender_name, payments.bot_payment_sender_name)
        self.assertIs(bots.payment_step_keyboard, payments.payment_step_keyboard)
        self.assertIs(bots.optional_config_name_keyboard, payments.optional_config_name_keyboard)
        self.assertIs(bots.bot_order_metadata, payments.bot_order_metadata)
        self.assertIs(bots.extract_receipt_file, payments.extract_receipt_file)
        self.assertIs(bots.receipt_file_type_error, payments.receipt_file_type_error)
        self.assertIs(bots.safe_receipt_filename, payments.safe_receipt_filename)
        self.assertTrue(callable(bots.copy_payment_value_from_state))
        self.assertTrue(callable(bots.attach_bot_receipt))
        self.assertTrue(callable(bots.download_receipt_content))
        self.assertTrue(callable(bots.send_main_menu))
        self.assertTrue(callable(bots.active_subscription_lines))
        self.assertTrue(callable(bots.format_client_config))
        self.assertTrue(callable(bots.send_client_config_messages))
        self.assertTrue(callable(bots.start_user_client_delete_flow))
        self.assertTrue(callable(bots.confirm_user_client_delete_flow))

    def test_payment_state_helpers_keep_existing_outputs(self):
        from .bots import (
            bot_order_metadata,
            bot_payment_sender_name,
            copy_payment_value_from_state,
            optional_config_name_keyboard,
            payment_step_keyboard,
        )

        customer = Customer.objects.create(display_name="Alice")
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice Buyer",
            state_data={"plan_id": self.plan.pk, "quantity": 2, "sender_card_name": "Work Laptop"},
        )

        self.assertEqual(bot_payment_sender_name(bot_user), "Work Laptop")
        bot_user.state_data = {"plan_id": self.plan.pk, "quantity": 2}
        bot_user.save(update_fields=["state_data", "updated_at"])

        self.assertEqual(copy_payment_value_from_state(self.bot_config, bot_user, "payment_card"), "0000000000000000")
        self.assertEqual(copy_payment_value_from_state(self.bot_config, bot_user, "payment_amount"), "200000")

        payment_keyboard = payment_step_keyboard(self.store.card_number, Decimal("200000"), config=self.bot_config)
        payment_callbacks = [
            button.get("callback_data", "")
            for row in payment_keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertNotIn("user:payment_name:start", payment_callbacks)
        self.assertNotIn("user:payment_receipt_only", payment_callbacks)
        self.assertIn("user:buy_back_summary", payment_callbacks)
        self.assertIn("user:cancel", payment_callbacks)
        self.assertTrue(
            any(
                button.get("copy_text", {}).get("text") == "200000"
                for row in payment_keyboard["inline_keyboard"]
                for button in row
            )
        )

        name_keyboard = optional_config_name_keyboard()
        name_callbacks = [
            button.get("callback_data", "")
            for row in name_keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(name_callbacks, ["user:payment_receipt_only", "user:buy_back_summary", "user:cancel"])

        metadata = bot_order_metadata(self.bot_config, bot_user, source="bot_purchase", extra={"flow": "purchase"})
        self.assertEqual(metadata["source"], "bot_purchase")
        self.assertEqual(metadata["flow"], "purchase")
        self.assertEqual(metadata["bot"]["provider_user_id"], "42")
        self.assertNotIn("bot_token", metadata["bot"])
        self.assertNotIn("config_link", json.dumps(metadata, ensure_ascii=False))

    def test_receipt_helpers_detect_photo_and_image_document(self):
        from .bots import extract_receipt_file, receipt_file_type_error

        photo_info = extract_receipt_file(
            {
                "message_id": 11,
                "photo": [
                    {"file_id": "small-file", "file_unique_id": "small"},
                    {"file_id": "large-file", "file_unique_id": "large"},
                ],
            }
        )

        self.assertEqual(photo_info["kind"], "photo")
        self.assertEqual(photo_info["file_id"], "large-file")
        self.assertEqual(photo_info["message_id"], 11)
        self.assertEqual(receipt_file_type_error(photo_info), "")

        document_info = extract_receipt_file(
            {
                "messageId": 12,
                "document": {
                    "fileId": "doc-file",
                    "fileUniqueId": "doc-unique",
                    "fileName": "receipt.PNG",
                    "mimeType": "image/png",
                },
            }
        )

        self.assertEqual(document_info["kind"], "document")
        self.assertEqual(document_info["file_id"], "doc-file")
        self.assertEqual(document_info["file_unique_id"], "doc-unique")
        self.assertEqual(document_info["file_name"], "receipt.PNG")
        self.assertEqual(document_info["mime_type"], "image/png")
        self.assertEqual(document_info["message_id"], 12)
        self.assertEqual(receipt_file_type_error(document_info), "")

        text_document = extract_receipt_file(
            {
                "document": {
                    "file_id": "text-file",
                    "file_name": "receipt.txt",
                    "mime_type": "text/plain",
                },
            }
        )
        self.assertIn("فایل رسید باید تصویر", receipt_file_type_error(text_document))

    @override_settings(BOT_COPY_TEXT_DISABLED=True)
    def test_payment_keyboard_copy_fallbacks_do_not_include_help_callbacks(self):
        from .bots import build_payment_keyboard

        buttons = [
            button
            for row in build_payment_keyboard("0000 0000 0000 0000", 300000, config=self.bot_config)["inline_keyboard"]
            for button in row
        ]

        callbacks = [button.get("callback_data", "") for button in buttons]
        self.assertIn("user:copy:payment_card", callbacks)
        self.assertIn("user:copy:payment_amount", callbacks)
        self.assertNotIn("user:payment_receipt_help", callbacks)
        self.assertFalse(any("راهنما" in button.get("text", "") for button in buttons))

    def test_payment_text_uses_required_labels_and_hides_empty_optional_bank_fields(self):
        from .bots import store_payment_lines

        self.store.sheba_number = "IR1234567890"
        self.store.save(update_fields=["sheba_number", "updated_at"])

        text = store_payment_lines(self.store, self.plan)

        self.assertIn("مبلغ قابل پرداخت:", text)
        self.assertIn("<code>100000</code>", text)
        self.assertIn("شماره کارت:", text)
        self.assertIn("<code>0000000000000000</code>", text)
        self.assertIn("به نام:", text)
        self.assertIn("بانک: Test Bank", text)
        self.assertIn("شبا: <code>IR1234567890</code>", text)

        self.store.bank_name = " "
        self.store.sheba_number = " "
        self.store.save(update_fields=["bank_name", "sheba_number", "updated_at"])

        text = store_payment_lines(self.store, self.plan)

        self.assertNotIn("بانک:", text)
        self.assertNotIn("شبا:", text)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_copyable_config_message_uses_html_pre_and_copy_button_for_short_links(self, post_mock):
        from .bots import BotClient, send_copyable_config_message

        link = "vless://abc_def@example.com:443?type=ws&security=tls#Alice-1"
        send_copyable_config_message(BotClient(self.bot_config), "42", link, title="✅ تست کانفیگ")

        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("<pre>", payload["text"])
        self.assertIn("⚡ لینک مستقیم", payload["text"])
        self.assertIn("type=ws&amp;security=tls", payload["text"])
        self.assertTrue(
            any(
                button.get("text") == "کپی لینک مستقیم ⚡"
                and button.get("copy_text", {}).get("text") == link
                for row in payload["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_long_config_message_uses_tokenized_fallback_callback(self, post_mock):
        from .bots import BotClient, send_copyable_config_message

        long_link = "vmess://" + ("a" * 300)
        send_copyable_config_message(BotClient(self.bot_config), "42", long_link)

        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("<pre>", payload["text"])
        self.assertFalse(
            any(
                "copy_text" in button
                for row in payload["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )
        fallback_callbacks = [
            button.get("callback_data", "")
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("user:copy_config:direct:")
        ]
        self.assertEqual(len(fallback_callbacks), 1)
        self.assertNotIn(long_link, fallback_callbacks[0])

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_links_message_combines_subscription_and_direct_links(self, post_mock):
        from .bots import BotClient, send_config_links_message

        sub_link = "https://example.com/sub/alice"
        direct_link = "vless://alice@example.com:443?type=ws&security=tls#Alice"
        send_config_links_message(
            BotClient(self.bot_config),
            "42",
            subscription_link=sub_link,
            direct_link=direct_link,
            title="✅ سرویس تست",
        )

        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("🔗 لینک اشتراک", payload["text"])
        self.assertIn("⚡ لینک مستقیم", payload["text"])
        self.assertIn(sub_link, payload["text"])
        self.assertIn("type=ws&amp;security=tls", payload["text"])
        buttons = [
            button
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(any(button.get("text") == "کپی لینک اشتراک 🔗" and button.get("copy_text", {}).get("text") == sub_link for button in buttons))
        self.assertTrue(any(button.get("text") == "کپی لینک مستقیم ⚡" and button.get("copy_text", {}).get("text") == direct_link for button in buttons))
        self.assertTrue(any(button.get("callback_data") == "user:subs" for button in buttons))
        self.assertTrue(any(button.get("callback_data") == "user:help" for button in buttons))

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_config_copy_fallback_callback_sends_cached_link_without_deleting_source(self, post_mock):
        from .bots import BotClient, send_copyable_config_message

        self.post_update(self.message("/start"))
        long_link = "vmess://" + ("b" * 300)
        send_copyable_config_message(BotClient(self.bot_config), "42", long_link)
        config_payload = post_mock.call_args.kwargs["json"]
        callback_data = next(
            button["callback_data"]
            for row in config_payload["reply_markup"]["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("user:copy_config:direct:")
        )
        self.assertNotIn(long_link, callback_data)
        post_mock.reset_mock()

        response = self.post_update(self.callback(callback_data, message_id=77, callback_id="copy-cb"))

        self.assertEqual(response.status_code, 200)
        methods = [call.args[0].rsplit("/", 1)[-1] for call in post_mock.call_args_list]
        self.assertIn("answerCallbackQuery", methods)
        self.assertNotIn("deleteMessage", methods)
        payload = next(
            call.kwargs["json"]
            for call in post_mock.call_args_list
            if call.args[0].endswith("/sendMessage")
        )
        self.assertIn(long_link, payload["text"])
        self.assertEqual(payload["parse_mode"], "HTML")
        logged_payloads = "\n".join(
            json.dumps(event.raw_payload, ensure_ascii=False)
            for event in BotEventLog.objects.all()
        )
        self.assertNotIn(long_link, logged_payloads)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_expired_config_copy_fallback_callback_shows_retry_message(self, post_mock):
        self.post_update(self.message("/start"))
        post_mock.reset_mock()

        response = self.post_update(self.callback("user:copy_config:direct:missing-token", message_id=77, callback_id="copy-expired"))

        self.assertEqual(response.status_code, 200)
        methods = [call.args[0].rsplit("/", 1)[-1] for call in post_mock.call_args_list]
        self.assertNotIn("deleteMessage", methods)
        payload = next(
            call.kwargs["json"]
            for call in post_mock.call_args_list
            if call.args[0].endswith("/sendMessage")
        )
        self.assertIn("این لینک منقضی شده، دوباره از بخش سرویس‌های من دریافت کنید.", payload["text"])

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
        self.assertIn("مبلغ: ۳۰۰,۰۰۰ تومان", payment_prompt)

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
        self.assertEqual(order.discount_code_text, "SAVE10")
        self.assertEqual(order.discount_amount, 10000)
        self.assertEqual(order.amount, 90000)
        self.assertRegex(xui_mock.call_args.kwargs["email_prefix"], r"^alice_[0-9a-f]{8}$")
        user_texts = [
            call.get("json", {}).get("text", "")
            for call in post_calls
            if call["url"].endswith("/sendMessage") and call.get("json", {}).get("chat_id") == "42"
        ]
        self.assertTrue(any("کد تخفیف SAVE10 اعمال شد" in text for text in user_texts))
        self.assertTrue(any("مبلغ نهایی: ۹۰,۰۰۰ تومان" in text for text in user_texts))

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("13131313-1313-4313-8313-131313131313"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_purchase_flow_accepts_receipt_photo_without_config_name(self, _get_mock, _xui_mock):
        def post_side_effect(url, json=None, data=None, **kwargs):
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.message("/start"))
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
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
        self.assertEqual(order.sender_card_name, "Alice")
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)
        self.assertTrue(order.payment_receipt_image.name.endswith(".jpg"))
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.IDLE)

    @patch("store.order_services.create_inactive_client_details", return_value=fake_client_result("14141414-1414-4414-8414-141414141414"))
    @patch("store.bots.requests.get", return_value=DummyBotResponse(content=image_bytes("JPEG")))
    def test_purchase_flow_can_use_optional_config_name_before_receipt(self, _get_mock, _xui_mock):
        post_calls = []

        def post_side_effect(url, json=None, data=None, **kwargs):
            post_calls.append({"url": url, "json": json, "data": data, **kwargs})
            if url.endswith("/getFile"):
                return DummyBotResponse({"ok": True, "result": {"file_path": "photos/receipt.jpg"}})
            return DummyBotResponse()

        with patch("store.bots.requests.post", side_effect=post_side_effect):
            self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
            self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
            self.post_update(self.callback("user:payment_name:start", callback_id="name-cb"))
            bot_user = BotUser.objects.get(provider_user_id="42")
            self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_NAME)
            self.assertEqual(bot_user.state_data["step"], "config_name")

            self.post_update(self.message("Work Laptop", message_id=2))
            bot_user.refresh_from_db()
            self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
            name_prompt = post_calls[-1]["json"]["text"]
            self.assertIn("مبلغ قابل پرداخت:", name_prompt)
            self.assertIn("<code>100000</code>", name_prompt)
            self.assertIn("شماره کارت:", name_prompt)
            self.assertIn("<code>0000000000000000</code>", name_prompt)
            self.assertIn("نام کانفیگ ثبت شد", name_prompt)
            self.assertIn("تصویر رسید پرداخت", name_prompt)

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
        self.assertEqual(order.sender_card_name, "Work Laptop")
        self.assertEqual(order.status, Order.Status.PENDING_VERIFICATION)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_purchase_flow_can_skip_discount_to_payment(self, post_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        response = self.post_update(self.callback("user:discount:skip", callback_id="skip-discount"))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertEqual(bot_user.state_data["step"], "receipt")
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("پرداخت کارت‌به‌کارت", payload["text"])
        self.assertIn("مبلغ قابل پرداخت:", payload["text"])
        self.assertIn("<code>100000</code>", payload["text"])
        self.assertIn("شماره کارت:", payload["text"])
        self.assertIn("<code>0000000000000000</code>", payload["text"])
        self.assertIn("به نام:", payload["text"])
        self.assertIn("بانک: Test Bank", payload["text"])
        self.assertNotIn("شبا:", payload["text"])
        self.assertIn("بعد از پرداخت، عکس رسید را همینجا ارسال کنید.", payload["text"])
        buttons = [
            button
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            [button["text"] for button in buttons],
            ["کپی شماره کارت", "کپی مبلغ", "برگشت", "لغو"],
        )
        self.assertNotIn("ارسال رسید", [button["text"] for button in buttons])
        self.assertNotIn("تعیین نام کانفیگ", [button["text"] for button in buttons])
        self.assertNotIn("ارسال رسید کردم / راهنما", [button["text"] for button in buttons])
        self.assertNotIn("راهنمای پرداخت ❓", [button["text"] for button in buttons])
        self.assertNotIn("user:payment_receipt_help", [button.get("callback_data", "") for button in buttons])
        self.assertNotIn("user:payment_name:start", [button.get("callback_data", "") for button in buttons])
        self.assertNotIn("user:payment_receipt_only", [button.get("callback_data", "") for button in buttons])
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "0000000000000000" for button in buttons))
        self.assertTrue(any(button.get("copy_text", {}).get("text") == "100000" for button in buttons))

    @override_settings(BOT_COPY_TEXT_DISABLED=True)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_payment_copy_fallback_callbacks_keep_waiting_for_receipt(self, post_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))

        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)

        self.post_update(self.callback("user:copy:payment_card", callback_id="copy-card"))
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertIn("<code>0000000000000000</code>", post_mock.call_args.kwargs["json"]["text"])

        self.post_update(self.callback("user:copy:payment_amount", callback_id="copy-amount"))
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertIn("<code>100000</code>", post_mock.call_args.kwargs["json"]["text"])

        self.post_update(self.message("رسید متنی نیست", message_id=2))
        bot_user.refresh_from_db()
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertEqual(post_mock.call_args.kwargs["json"]["text"], "لطفاً عکس رسید پرداخت را ارسال کنید.")

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_purchase_rejects_non_image_receipt_file(self, post_mock):
        self.post_update(self.callback(f"user:buyplan:{self.plan.pk}"))
        self.post_update(self.callback("user:buyqty:1", callback_id="qty-cb"))
        self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
        response = self.post_update(
            {
                "message": {
                    "message_id": 2,
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
        self.assertIn("برای دریافت لینک", payload["text"])
        self.assertNotIn("https://example.com/sub/alice", payload["text"])
        callback_values = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:referral_redeem:{vpn_client.public_id}", callback_values)
        delete_callback = next(value for value in callback_values if value.startswith("user:client_delete:"))
        self.assertNotIn(str(vpn_client.public_id), delete_callback)
        self.assertNotIn(vpn_client.uuid, delete_callback)

    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_menu_callback_keeps_main_buttons(self, post_mock):
        response = self.post_update(self.callback("user:menu"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("از منوی زیر انتخاب کنید", payload["text"])
        callbacks = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("user:buy", callbacks)
        self.assertIn("user:subs", callbacks)
        self.assertIn("user:help", callbacks)
        self.assertIn("user:profile", callbacks)
        self.assertNotIn("admin:orders:pending", callbacks)

    @patch("store.bots.sync_vpn_client_stats")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_services_menu_hides_deleted_and_foreign_clients(self, post_mock, stats_mock):
        customer = Customer.objects.create(display_name="Alice")
        other_customer = Customer.objects.create(display_name="Bob")
        BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        visible_plan = Plan.objects.create(
            store=self.store,
            name="Visible Plan",
            slug="visible-plan",
            volume_gb="1.000",
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        deleted_plan = Plan.objects.create(
            store=self.store,
            name="Deleted Plan",
            slug="deleted-plan",
            volume_gb="2.000",
            duration_days=30,
            price=200000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        other_plan = Plan.objects.create(
            store=self.store,
            name="Other Plan",
            slug="other-plan",
            volume_gb="3.000",
            duration_days=30,
            price=300000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        order = Order.objects.create(
            store=self.store,
            customer=customer,
            plan=visible_plan,
            inbound=self.inbound,
            amount=visible_plan.price,
            original_amount=visible_plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        other_order = Order.objects.create(
            store=self.store,
            customer=other_customer,
            plan=other_plan,
            inbound=self.inbound,
            amount=other_plan.price,
            original_amount=other_plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        visible_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=visible_plan,
            inbound=self.inbound,
            username="visible",
            xui_email="visible",
            uuid="11111111-1111-4111-8111-111111111111",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
        )
        deleted_client = VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=deleted_plan,
            inbound=self.inbound,
            username="deleted",
            xui_email="deleted",
            uuid="22222222-2222-4222-8222-222222222222",
            status=VPNClient.Status.DELETED,
            traffic_limit_bytes=2 * 1024 ** 3,
            deleted_at=timezone.now(),
        )
        other_client = VPNClient.objects.create(
            store=self.store,
            order=other_order,
            plan=other_plan,
            inbound=self.inbound,
            username="other",
            xui_email="other",
            uuid="33333333-3333-4333-8333-333333333333",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=3 * 1024 ** 3,
        )
        stats_mock.return_value = {
            "panel_available": True,
            "total_traffic_bytes": 1024 ** 3,
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": 1024 ** 3,
            "expiry_at": timezone.now() + timedelta(days=20),
        }

        response = self.post_update(self.callback("user:subs"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("Visible Plan", payload["text"])
        self.assertNotIn("Deleted Plan", payload["text"])
        self.assertNotIn("Other Plan", payload["text"])
        callbacks = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(any(str(visible_client.public_id) in value for value in callbacks))
        self.assertFalse(any(str(deleted_client.public_id) in value for value in callbacks))
        self.assertFalse(any(str(other_client.public_id) in value for value in callbacks))

    @patch("store.bots.sync_vpn_client_stats")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_client_usage_callback_keeps_status_message(self, post_mock, stats_mock):
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
            uuid="44444444-4444-4444-8444-444444444444",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            used_traffic_bytes=128 * 1024 * 1024,
            duration_days=30,
        )
        stats_mock.return_value = {
            "panel_available": True,
            "total_traffic_bytes": 1024 ** 3,
            "used_traffic_bytes": 128 * 1024 * 1024,
            "remaining_traffic_bytes": (1024 ** 3) - (128 * 1024 * 1024),
            "expiry_at": timezone.now() + timedelta(days=20),
        }

        response = self.post_update(self.callback(f"user:client_usage:{vpn_client.public_id}"))

        self.assertEqual(response.status_code, 200)
        stats_mock.assert_called_once()
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("کانفیگ شما", payload["text"])
        self.assertIn("حجم باقی‌مانده", payload["text"])
        callbacks = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:client_refresh:{vpn_client.public_id}", callbacks)
        self.assertIn(f"user:client_renew:{vpn_client.public_id}", callbacks)

    @patch("store.bots.sync_vpn_client_stats", return_value={})
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_services_get_config_sends_subscription_and_direct_in_one_message(self, post_mock, stats_mock):
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
            uuid="cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
            sub_id="sub",
            sub_link="https://example.com/sub/alice",
            direct_link="vless://alice-config",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            used_traffic_bytes=128 * 1024 * 1024,
            duration_days=30,
        )

        response = self.post_update(self.callback(f"user:client_config:{vpn_client.public_id}"))

        self.assertEqual(response.status_code, 200)
        payload = post_mock.call_args.kwargs["json"]
        self.assertIn("https://example.com/sub/alice", payload["text"])
        self.assertIn("vless://alice-config", payload["text"])
        self.assertIn("🔗 لینک اشتراک", payload["text"])
        self.assertIn("⚡ لینک مستقیم", payload["text"])

    @patch("store.bots.sync_vpn_client_stats")
    @patch("store.bots.delete_vpn_client_for_user")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_delete_config_requires_confirmation_and_calls_service(self, post_mock, delete_mock, stats_mock):
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
            uuid="edededed-eded-4ede-8ede-edededededed",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
            expires_at=timezone.now() + timedelta(days=20),
        )
        stats_mock.return_value = {
            "panel_available": True,
            "total_traffic_bytes": 1024 ** 3,
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": 1024 ** 3,
            "expiry_at": vpn_client.expires_at,
        }
        delete_mock.return_value = {"success": True}

        self.post_update(self.callback("user:subs"))
        list_payload = post_mock.call_args.kwargs["json"]
        callbacks = [
            button["callback_data"]
            for row in list_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        delete_callback = next(value for value in callbacks if value.startswith("user:client_delete:"))

        self.post_update(self.callback(delete_callback, callback_id="delete-start"))
        confirm_payload = post_mock.call_args.kwargs["json"]
        self.assertIn("⚠️ حذف کانفیگ", confirm_payload["text"])
        confirm_callbacks = [
            button["callback_data"]
            for row in confirm_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        confirm_callback = next(value for value in confirm_callbacks if value.startswith("user:client_delete_confirm:"))
        self.assertNotIn(vpn_client.uuid, confirm_callback)

        response = self.post_update(self.callback(confirm_callback, callback_id="delete-confirm"))

        self.assertEqual(response.status_code, 200)
        delete_mock.assert_called_once()
        args, kwargs = delete_mock.call_args
        self.assertEqual(args[0], customer)
        self.assertEqual(args[1].pk, vpn_client.pk)
        self.assertEqual(kwargs["actor_telegram_id"], "42")
        self.assertIn("✅ کانفیگ حذف شد", post_mock.call_args.kwargs["json"]["text"])

    @patch("store.bots.delete_vpn_client_for_user")
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_user_delete_config_confirm_rejects_foreign_client(self, post_mock, delete_mock):
        from .bots import create_user_client_delete_token

        customer = Customer.objects.create(display_name="Alice")
        other_customer = Customer.objects.create(display_name="Bob")
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id="42",
            chat_id="42",
            username="alice",
            display_name="Alice",
        )
        other_order = Order.objects.create(
            store=self.store,
            customer=other_customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        other_client = VPNClient.objects.create(
            store=self.store,
            order=other_order,
            plan=self.plan,
            inbound=self.inbound,
            username="other_1gb",
            xui_email="other_1gb",
            uuid="99999999-9999-4999-8999-999999999999",
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=1024 ** 3,
        )
        token = create_user_client_delete_token(bot_user, other_client)

        response = self.post_update(self.callback(f"user:client_delete_confirm:{token}", callback_id="foreign-delete"))

        self.assertEqual(response.status_code, 200)
        delete_mock.assert_not_called()
        self.assertIn("این کانفیگ پیدا نشد", post_mock.call_args.kwargs["json"]["text"])

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
        self.assertIn("اپراتور خود را انتخاب کنید", operator_payload["text"])
        operator_callbacks = [
            button["callback_data"]
            for row in operator_payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"user:buyop:{operator_a.pk}", operator_callbacks)
        self.assertFalse(any(value.startswith("user:buyplan:") for value in operator_callbacks))

        self.post_update(self.callback(f"user:buyop:{operator_a.pk}", callback_id="operator-cb"))

        plan_payload = post_mock.call_args.kwargs["json"]
        self.assertIn(f"پلن‌های {operator_a.name}", plan_payload["text"])
        self.assertIn("یکی از پلن‌های زیر را انتخاب کنید", plan_payload["text"])
        self.assertNotIn(self.plan.name, plan_payload["text"])
        self.assertNotIn(other_plan.name, plan_payload["text"])
        plan_button_texts = [
            button["text"]
            for row in plan_payload["reply_markup"]["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("user:buyplan:")
        ]
        self.assertTrue(any("۱ گیگابایت" in text and "۳۰ روزه" in text and "۱۰۰,۰۰۰ تومان" in text for text in plan_button_texts))
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
        self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
        response = self.post_update(self.message("/skip", message_id=2))

        self.assertEqual(response.status_code, 200)
        bot_user = BotUser.objects.get(provider_user_id="42")
        self.assertEqual(bot_user.state, BotUser.State.BUY_WAIT_RECEIPT)
        self.assertFalse(Order.objects.exists())
        last_message = post_mock.call_args.kwargs["json"]["text"]
        self.assertEqual(last_message, "لطفاً عکس رسید پرداخت را ارسال کنید.")
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
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            response = self.post_update(
                {
                    "message": {
                        "message_id": 2,
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
        self.assertEqual(order.sender_card_name, "Alice")
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
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 2,
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
        self.assertEqual(order.sender_card_name, "Alice")
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
        self.assertTrue(
            any(
                "پرداخت کارت‌به‌کارت" in item.get("text", "")
                and "<code>100000</code>" in item.get("text", "")
                and item.get("parse_mode") == "HTML"
                for item in user_messages
            )
        )
        self.assertTrue(any("سفارش شما ثبت شد" in item.get("text", "") for item in user_messages))

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
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 2,
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
        self.assertTrue(any("سرویس شما آماده شد" in text for text in sent_texts))
        self.assertTrue(any("vless://example" in text for text in sent_texts))
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
        self.assertTrue(any("سرویس شما آماده شد" in text for text in helper_texts))
        config_text = next(text for text in helper_texts if "vless://example" in text)
        self.assertIn("https://example.com/sub/sub123", config_text)

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
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_update(
                    {
                        "message": {
                            "message_id": 2,
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
            self.post_update(self.callback("user:buy_confirm", callback_id="confirm-cb"))
            with self.captureOnCommitCallbacks(execute=True):
                self.post_update(
                    {
                        "message": {
                            "message_id": 2,
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
        ]
        self.assertTrue(any("سرویس شما آماده شد" in call["json"]["text"] for call in customer_messages))
        config_messages = [call for call in customer_messages if "vless://example" in call["json"]["text"]]
        self.assertEqual(len(config_messages), 1)
        config_payload = config_messages[0]["json"]
        self.assertIn("https://example.com/sub/sub123", config_payload["text"])
        callback_values = [
            button.get("callback_data", "")
            for row in config_payload.get("reply_markup", {}).get("inline_keyboard", [])
            for button in row
        ]
        self.assertTrue(any(value.startswith("user:copy_config:sub:") for value in callback_values))
        self.assertTrue(any(value.startswith("user:copy_config:direct:") for value in callback_values))
        self.assertFalse(any("vless://example" in value for value in callback_values))
        self.assertFalse(any("https://example.com/sub/sub123" in value for value in callback_values))
        self.assertFalse(
            any(
                "copy_text" in button
                for call in customer_messages
                for row in call["json"].get("reply_markup", {}).get("inline_keyboard", [])
                for button in row
            )
        )
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
        sent_texts = [
            call.kwargs["json"]["text"]
            for call in post_mock.call_args_list
            if call.kwargs.get("json", {}).get("chat_id") == "42"
            and "text" in call.kwargs.get("json", {})
        ]
        self.assertTrue(any("کانفیگ بروزرسانی شد" in text for text in sent_texts))
        config_text = next(text for text in sent_texts if "https://new.example/sub/fresh" in text)
        self.assertIn("vless://fresh-config", config_text)

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
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
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
            card_number="0000000000000000",
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

    def test_deleted_clients_are_not_reminder_candidates(self):
        from .renewal_reminder_services import get_active_clients_for_reminders

        self.vpn_client.mark_deleted(customer=self.customer, reason="test cleanup")
        self.vpn_client.save(
            update_fields=[
                "status",
                "deleted_at",
                "deleted_by_customer",
                "delete_reason",
                "remote_deleted_at",
                "disabled_at",
                "sub_link",
                "direct_link",
                "updated_at",
            ]
        )

        self.assertFalse(get_active_clients_for_reminders(store=self.store).filter(pk=self.vpn_client.pk).exists())

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
        now = timezone.make_aware(datetime(2026, 6, 4, 12, 0), timezone.get_current_timezone())
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

    def test_web_telegram_link_adds_target_for_customer_vpn_client(self):
        from .bot_targets import get_vpn_client_telegram_targets
        from .telegram_link_services import create_web_telegram_link_token, link_bot_user_to_customer

        BotUser.objects.all().delete()
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            provider_user_id="84",
            chat_id="84",
            username="webuser",
            display_name="Web User",
        )
        self.assertEqual(get_vpn_client_telegram_targets(self.vpn_client, store=self.store), [])
        raw_token, _token = create_web_telegram_link_token(self.customer, source="dashboard")

        result = link_bot_user_to_customer(raw_token, bot_user)

        self.assertTrue(result.success)
        targets = get_vpn_client_telegram_targets(self.vpn_client, store=self.store)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].chat_id, "84")

    def test_linked_web_customer_all_vpn_clients_are_visible_to_bot(self):
        from .bots import bot_subscription_clients
        from .telegram_link_services import create_web_telegram_link_token, link_bot_user_to_customer

        second_client = self.make_client(expires_at=timezone.now() + timedelta(days=10))
        BotUser.objects.all().delete()
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            provider_user_id="84",
            chat_id="84",
            username="webuser",
            display_name="Web User",
        )
        raw_token, _token = create_web_telegram_link_token(self.customer, source="dashboard")

        link_bot_user_to_customer(raw_token, bot_user)
        bot_user.refresh_from_db()

        visible_client_ids = set(bot_subscription_clients(bot_user).values_list("pk", flat=True))
        self.assertEqual(visible_client_ids, {self.vpn_client.pk, second_client.pk})

    @patch("store.renewal_reminder_services.sync_vpn_client_stats")
    @patch("store.bots.BotClient.send_message", return_value={"ok": True})
    def test_linked_web_customer_reminder_is_not_skipped_without_target(self, send_mock, sync_mock):
        from .renewal_reminder_services import run_renewal_reminders
        from .telegram_link_services import create_web_telegram_link_token, link_bot_user_to_customer

        BotUser.objects.all().delete()
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            provider_user_id="84",
            chat_id="84",
            username="webuser",
            display_name="Web User",
        )
        raw_token, _token = create_web_telegram_link_token(self.customer, source="dashboard")
        link_bot_user_to_customer(raw_token, bot_user)
        sync_mock.return_value = self.xui_stats(remaining_gb=9)

        summary = run_renewal_reminders()

        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 0)
        send_mock.assert_called_once()
        self.assertEqual(VPNClientReminderLog.objects.get().sent_to_telegram_id, "84")

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


class RevenueEnginePhaseOneTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Revenue Store",
            english_name="Revenue Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-revenue")
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

    def gb(self, value):
        return int(Decimal(str(value)) * Decimal(1024 ** 3))

    def make_client(self, *, expires_at=None, used_gb=1, total_gb=10, status=VPNClient.Status.ACTIVE):
        client_index = VPNClient.objects.count() + 1
        uuid = f"99999999-9999-4999-8999-{client_index:012d}"
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username=f"revenue_{client_index}",
            uuid=uuid,
        )
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username=f"revenue_{client_index}",
            xui_email=f"revenue_{client_index}",
            uuid=uuid,
            status=status,
            traffic_limit_bytes=self.gb(total_gb),
            used_traffic_bytes=self.gb(used_gb),
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
            expires_at=expires_at or timezone.now() + timedelta(days=10),
            last_online_at=timezone.now() - timedelta(hours=1),
            last_synced_at=timezone.now(),
        )

    @patch("store.revenue_engine.scheduler.emit_event")
    def test_expiry_trigger(self, emit_mock):
        from .revenue_engine.scheduler import run_revenue_scan
        from .revenue_engine.triggers import USER_EXPIRED

        client = self.make_client(expires_at=timezone.now() - timedelta(hours=1), used_gb=1)

        summary = run_revenue_scan()

        self.assertEqual(summary["scanned"], 1)
        self.assertTrue(any(call.args[0] == USER_EXPIRED and call.args[1] == client for call in emit_mock.call_args_list))

    @patch("store.revenue_engine.scheduler.emit_event")
    def test_usage_over_80_trigger(self, emit_mock):
        from .revenue_engine.scheduler import run_revenue_scan
        from .revenue_engine.triggers import HIGH_USAGE_USER

        client = self.make_client(expires_at=timezone.now() + timedelta(days=10), used_gb=9)

        run_revenue_scan()

        self.assertTrue(any(call.args[0] == HIGH_USAGE_USER and call.args[1] == client for call in emit_mock.call_args_list))

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_no_duplicate_messaging_in_24h(self, send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_EXPIRED

        client = self.make_client(expires_at=timezone.now() - timedelta(hours=1))
        context = {"usage_percent": Decimal("10")}
        first = RevenueEngine().handle(USER_EXPIRED, client, context)
        second = RevenueEngine().handle(USER_EXPIRED, client, context)

        self.assertTrue(first["action"]["sent"])
        self.assertEqual(second["action"]["reason"], "cooldown_active")
        send_mock.assert_called_once()

    def test_user_expired_logic(self):
        from .revenue_engine.rules import RuleEngine
        from .revenue_engine.triggers import USER_EXPIRED

        decision = RuleEngine().evaluate(USER_EXPIRED, self.customer, {})

        self.assertEqual(decision["discount"], 25)
        self.assertEqual(decision["type"], "user_expired")
        self.assertIn("25% تخفیف", decision["message"])

    def test_near_expiry_discount_logic(self):
        from .revenue_engine.rules import RuleEngine
        from .revenue_engine.triggers import USER_NEAR_EXPIRY

        engine = RuleEngine()

        self.assertEqual(engine.evaluate(USER_NEAR_EXPIRY, self.customer, {"usage_percent": 85})["discount"], 15)
        self.assertEqual(engine.evaluate(USER_NEAR_EXPIRY, self.customer, {"usage_percent": 65})["discount"], 10)
        self.assertEqual(engine.evaluate(USER_NEAR_EXPIRY, self.customer, {"usage_percent": 20})["discount"], 5)

    def test_high_usage_upgrade_suggestion(self):
        from .revenue_engine.rules import RuleEngine
        from .revenue_engine.triggers import HIGH_USAGE_USER

        decision = RuleEngine().evaluate(HIGH_USAGE_USER, self.customer, {"usage_percent": 90})

        self.assertEqual(decision["type"], "high_usage_upgrade")
        self.assertEqual(decision["volume_multiplier"], 2)
        self.assertIn("پیشنهاد ارتقا", decision["message"])

    def test_engine_does_not_crash_on_missing_data(self):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_NEAR_EXPIRY

        result = RevenueEngine().handle(USER_NEAR_EXPIRY, None, {})

        self.assertTrue(result["handled"])
        self.assertEqual(result["action"]["reason"], "no_personal_telegram_target")

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_scheduler_runs_without_db_errors(self, _send_mock):
        from .revenue_engine.scheduler import run_revenue_scan

        self.make_client(expires_at=timezone.now() + timedelta(hours=12), used_gb=6)
        out = StringIO()

        summary = run_revenue_scan()
        call_command("run_revenue_scan", stdout=out)

        self.assertEqual(summary["scanned"], 1)
        self.assertIn("Revenue scan summary:", out.getvalue())


class UpsellEnginePhaseTwoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Upsell Store",
            english_name="Upsell Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
        )
        self.small_plan = Plan.objects.create(
            store=self.store,
            name="Small",
            volume_gb=Decimal("5"),
            duration_days=30,
            price=100000,
            device_limit=2,
            sort_order=1,
        )
        self.medium_plan = Plan.objects.create(
            store=self.store,
            name="Medium",
            volume_gb=Decimal("8"),
            duration_days=30,
            price=125000,
            device_limit=2,
            sort_order=2,
        )
        self.large_plan = Plan.objects.create(
            store=self.store,
            name="Large",
            volume_gb=Decimal("20"),
            duration_days=30,
            price=200000,
            device_limit=3,
            sort_order=3,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-upsell")
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

    def context(self, plan=None, **extra):
        data = {
            "bot_user": self.bot_user,
            "chat_id": self.bot_user.chat_id,
            "bot_config": self.bot_config,
            "store": self.store,
            "selected_plan": plan or self.small_plan,
            "plan": plan or self.small_plan,
            "quantity": 1,
        }
        data.update(extra)
        return data

    def make_vpn_client(self, *, used_gb=9, total_gb=10):
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.small_plan,
            inbound=self.inbound,
            amount=self.small_plan.price,
            original_amount=self.small_plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="upsell_client",
            uuid="88888888-8888-4888-8888-888888888888",
        )
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.small_plan,
            inbound=self.inbound,
            username="upsell_client",
            xui_email="upsell_client",
            uuid=order.uuid,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=int(Decimal(str(total_gb)) * Decimal(1024 ** 3)),
            used_traffic_bytes=int(Decimal(str(used_gb)) * Decimal(1024 ** 3)),
            expires_at=timezone.now() + timedelta(days=20),
            last_synced_at=timezone.now(),
        )

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_small_plan_triggers_upsell(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import USER_PLAN_SELECTED

        result = UpsellEngine().handle(USER_PLAN_SELECTED, self.bot_user, self.context())

        self.assertTrue(result["handled"])
        self.assertEqual(result["decision"]["upgrade_plan"], self.medium_plan)
        self.assertTrue(result["action"]["sent"])
        self.assertEqual(send_mock.call_args.kwargs["chat_id"], "42")

    def test_high_usage_triggers_upgrade_offer(self):
        from .revenue_engine.upsell.rules import UpsellRuleEngine
        from .revenue_engine.upsell.triggers import USER_PLAN_SELECTED

        decision = UpsellRuleEngine().evaluate(
            USER_PLAN_SELECTED,
            self.bot_user,
            self.context(usage_percent=90),
        )

        self.assertEqual(decision["type"], "upsell_offer")
        self.assertEqual(decision["upgrade_plan"], self.large_plan)
        self.assertIn("2 برابر حجم", decision["message"])

    def test_checkout_triggers_add_on_offer(self):
        from .revenue_engine.upsell.rules import UpsellRuleEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        decision = UpsellRuleEngine().evaluate(CHECKOUT_STARTED, self.bot_user, self.context(plan=self.medium_plan))

        self.assertEqual(decision["type"], "upsell_offer")
        self.assertEqual(decision["add_on"], "extra_gb")

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_anti_spam_24h_rule(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        first = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context(plan=self.medium_plan))
        second = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context(plan=self.medium_plan))

        self.assertTrue(first["action"]["sent"])
        self.assertEqual(second["action"]["reason"], "cooldown_active")
        send_mock.assert_called_once()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_user_skip_no_repeat_for_48h(self, send_mock):
        from .revenue_engine.upsell.actions import mark_upsell_skipped
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        mark_upsell_skipped(self.bot_user, self.context(plan=self.medium_plan))
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context(plan=self.medium_plan))

        self.assertEqual(result["action"]["reason"], "user_skipped_recently")
        send_mock.assert_not_called()

    def test_no_crash_on_missing_context(self):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import USER_PLAN_SELECTED

        result = UpsellEngine().handle(USER_PLAN_SELECTED, None, {})

        self.assertFalse(result["handled"])
        self.assertIsNone(result["decision"])

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_integration_with_revenue_engine_without_duplicate_events(self, upsell_send_mock, renewal_send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_EXPIRED
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        vpn_client = self.make_vpn_client()
        upsell = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context(plan=self.medium_plan))
        renewal = RevenueEngine().handle(USER_EXPIRED, vpn_client, {"usage_percent": 10})

        self.assertTrue(upsell["action"]["sent"])
        self.assertEqual(renewal["action"]["reason"], "upsell_active")
        upsell_send_mock.assert_called_once()
        renewal_send_mock.assert_not_called()


class RetentionEnginePhaseThreeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Retention Store",
            english_name="Retention Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-retention")
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

    def context(self, **extra):
        data = {
            "bot_user": self.bot_user,
            "chat_id": self.bot_user.chat_id,
            "bot_config": self.bot_config,
            "customer": self.customer,
            "store": self.store,
        }
        data.update(extra)
        return data

    def make_client(self, *, expires_at=None, status=VPNClient.Status.ACTIVE):
        expires_at = expires_at or timezone.now() - timedelta(days=1)
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="retention_client",
            uuid="77777777-7777-4777-8777-777777777777",
        )
        Order.objects.filter(pk=order.pk).update(created_at=expires_at - timedelta(days=30))
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="retention_client",
            xui_email="retention_client",
            uuid=order.uuid,
            status=status,
            traffic_limit_bytes=int(Decimal("10") * Decimal(1024 ** 3)),
            used_traffic_bytes=int(Decimal("2") * Decimal(1024 ** 3)),
            expires_at=expires_at,
            last_synced_at=timezone.now(),
        )

    def make_silent_active_client(self, *, usage_gb=Decimal("0.5"), last_online_at=None):
        client = self.make_client(expires_at=timezone.now() + timedelta(days=20), status=VPNClient.Status.ACTIVE)
        client.used_traffic_bytes = int(usage_gb * Decimal(1024 ** 3))
        client.last_online_at = last_online_at
        client.save(update_fields=["used_traffic_bytes", "last_online_at"])
        return client

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_inactive_24h_triggers_soft_reminder(self, send_mock):
        from .revenue_engine.scheduler import run_retention_scan

        BotUser.objects.filter(pk=self.bot_user.pk).update(last_seen_at=timezone.now() - timedelta(hours=25))

        summary = run_retention_scan()

        self.assertEqual(summary["sent"], 1)
        self.assertIn("مدتی است", send_mock.call_args.kwargs["text"])
        self.assertEqual(send_mock.call_args.kwargs["chat_id"], "42")

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_inactive_72h_triggers_discount(self, send_mock):
        from .revenue_engine.scheduler import run_retention_scan

        BotUser.objects.filter(pk=self.bot_user.pk).update(last_seen_at=timezone.now() - timedelta(hours=73))

        summary = run_retention_scan()

        self.assertEqual(summary["sent"], 1)
        self.assertIn("20% تخفیف", send_mock.call_args.kwargs["text"])
        self.assertEqual(BotEventLog.objects.get(message="retention_engine_offer_sent").raw_payload["discount"], 20)

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_expired_user_triggers_winback(self, send_mock):
        from .revenue_engine.scheduler import run_retention_scan

        self.make_client(expires_at=timezone.now() - timedelta(days=2))

        summary = run_retention_scan()

        self.assertEqual(summary["sent"], 1)
        self.assertIn("25% تخفیف", send_mock.call_args.kwargs["text"])

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_returned_user_gets_bonus(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import USER_RETURNED_AFTER_ABSENCE

        result = RetentionEngine().handle(USER_RETURNED_AFTER_ABSENCE, self.bot_user, self.context())

        self.assertTrue(result["action"]["sent"])
        self.assertEqual(result["decision"]["bonus_gb"], 1)
        self.assertIn("1GB هدیه", send_mock.call_args.kwargs["text"])

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_anti_spam_48h_enforcement(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import USER_INACTIVE_24H

        first = RetentionEngine().handle(USER_INACTIVE_24H, self.bot_user, self.context())
        second = RetentionEngine().handle(USER_INACTIVE_24H, self.bot_user, self.context())

        self.assertTrue(first["action"]["sent"])
        self.assertEqual(second["action"]["reason"], "cooldown_active")
        send_mock.assert_called_once()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_priority_conflict_upsell_beats_retention(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import USER_INACTIVE_24H
        from .revenue_engine.upsell.actions import upsell_active_key

        cache.set(upsell_active_key("42"), "active", 60 * 60)

        result = RetentionEngine().handle(USER_INACTIVE_24H, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "upsell_active")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_no_duplicate_messages(self, send_mock):
        from .revenue_engine.scheduler import run_retention_scan

        BotUser.objects.filter(pk=self.bot_user.pk).update(last_seen_at=timezone.now() - timedelta(hours=73))
        self.make_client(expires_at=timezone.now() - timedelta(days=2))

        summary = run_retention_scan()

        self.assertEqual(summary["events"], 2)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 1)
        send_mock.assert_called_once()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_scheduler_integration(self, send_mock):
        from .revenue_engine.scheduler import run_retention_scan

        BotUser.objects.filter(pk=self.bot_user.pk).update(last_seen_at=timezone.now() - timedelta(hours=25))
        out = run_retention_scan()

        self.assertEqual(out["scanned"], 1)
        self.assertEqual(out["events"], 1)
        self.assertEqual(out["sent"], 1)
        send_mock.assert_called_once()

    def test_silent_active_detection(self):
        from .revenue_engine.scheduler import _context_for, _is_silent_active_user

        now = timezone.now()
        client = self.make_silent_active_client(last_online_at=now - timedelta(hours=49))

        self.assertTrue(_is_silent_active_user(client, _context_for(client, now), now))

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_low_usage_active_subscription_sends_support_check_in(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import SILENT_ACTIVE_USER

        result = RetentionEngine().handle(
            SILENT_ACTIVE_USER,
            self.bot_user,
            self.context(last_connection=timezone.now() - timedelta(hours=49)),
        )

        self.assertTrue(result["action"]["sent"])
        self.assertEqual(result["decision"]["type"], "support_check_in")
        self.assertIn("پشتیبانی در دسترس است", send_mock.call_args.kwargs["text"])
        self.assertNotIn("تخفیف", send_mock.call_args.kwargs["text"])

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_no_duplicate_silent_message_in_72h(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import SILENT_ACTIVE_USER

        context = self.context(last_connection=timezone.now() - timedelta(hours=49))
        first = RetentionEngine().handle(SILENT_ACTIVE_USER, self.bot_user, context)
        second = RetentionEngine().handle(SILENT_ACTIVE_USER, self.bot_user, context)

        self.assertTrue(first["action"]["sent"])
        self.assertEqual(second["action"]["reason"], "silent_cooldown_active")
        send_mock.assert_called_once()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_silent_active_suppression_if_upsell_active(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import SILENT_ACTIVE_USER
        from .revenue_engine.upsell.actions import upsell_active_key

        cache.set(upsell_active_key("42"), "active", 60 * 60)

        result = RetentionEngine().handle(
            SILENT_ACTIVE_USER,
            self.bot_user,
            self.context(last_connection=timezone.now() - timedelta(hours=49)),
        )

        self.assertEqual(result["action"]["reason"], "upsell_active")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_silent_active_suppression_if_renewal_active(self, send_mock):
        from .revenue_engine.retention.actions import renewal_active_key
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import SILENT_ACTIVE_USER

        cache.set(renewal_active_key("42"), "active", 60 * 60)

        result = RetentionEngine().handle(
            SILENT_ACTIVE_USER,
            self.bot_user,
            self.context(last_connection=timezone.now() - timedelta(hours=49)),
        )

        self.assertEqual(result["action"]["reason"], "renewal_active")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_silent_active_scheduler_integration(self, send_mock):
        from .revenue_engine.scheduler import run_revenue_scan

        self.make_silent_active_client(last_online_at=timezone.now() - timedelta(hours=49))

        summary = run_revenue_scan()

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertIn("استفاده‌ای ثبت نشده", send_mock.call_args.kwargs["text"])
        self.assertTrue(BotEventLog.objects.get(message="retention_engine_offer_sent").raw_payload["silent_active_user"])

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_silent_active_no_crash_on_missing_last_connection(self, send_mock):
        from .revenue_engine.scheduler import run_revenue_scan

        self.make_silent_active_client(last_online_at=None)

        summary = run_revenue_scan()

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["events"], 0)
        self.assertEqual(summary["sent"], 0)
        send_mock.assert_not_called()


class RevenueOptimizationPhaseFourTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Optimization Store",
            english_name="Optimization Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10GB",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100000,
            device_limit=2,
            sort_order=1,
        )
        self.upgrade_plan = Plan.objects.create(
            store=self.store,
            name="20GB",
            volume_gb=Decimal("20"),
            duration_days=30,
            price=125000,
            device_limit=2,
            sort_order=2,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-optimization")
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

    def context(self, **extra):
        data = {
            "bot_user": self.bot_user,
            "chat_id": self.bot_user.chat_id,
            "bot_config": self.bot_config,
            "store": self.store,
            "selected_plan": self.plan,
            "plan": self.plan,
            "quantity": 1,
        }
        data.update(extra)
        return data

    def make_vpn_client(self):
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="optimization_client",
            uuid="66666666-6666-4666-8666-666666666666",
        )
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="optimization_client",
            xui_email="optimization_client",
            uuid=order.uuid,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=int(Decimal("10") * Decimal(1024 ** 3)),
            used_traffic_bytes=int(Decimal("9") * Decimal(1024 ** 3)),
            expires_at=timezone.now() - timedelta(hours=1),
            last_synced_at=timezone.now(),
        )

    def seed_variant(self, offer_type, variant, *, impressions, conversions):
        from .revenue_engine.optimization.tracker import OfferTracker

        tracker = OfferTracker()
        for index in range(impressions):
            user_id = f"{offer_type}-{variant}-{index}"
            tracker.user_received_offer(user_id, offer_type, variant)
            if index < conversions:
                tracker.user_purchased_after_offer(user_id, offer_type=offer_type)

    def test_multiple_variants_generated(self):
        from .revenue_engine.optimization.experiment import ExperimentEngine

        variants = ExperimentEngine().generate_variants(
            {"type": "upsell_offer", "message": "base offer"},
            offer_type="upsell",
        )

        self.assertEqual({variant["experiment_variant"] for variant in variants}, {"A", "B", "C"})
        self.assertTrue(all(variant["optimization_offer_type"] == "upsell" for variant in variants))

    def test_tracker_records_conversion(self):
        from .revenue_engine.optimization.tracker import OfferTracker

        tracker = OfferTracker()
        tracker.user_received_offer("user-1", "upsell", "A")
        converted = tracker.user_purchased_after_offer("user-1", offer_type="upsell")

        self.assertTrue(converted.converted)
        received = BotEventLog.objects.get(message="offer_event:user_received_offer")
        self.assertTrue(received.raw_payload["converted"])

    def test_scoring_calculation_accuracy(self):
        from .revenue_engine.optimization.scoring import ScoringEngine

        self.seed_variant("upsell", "A", impressions=10, conversions=1)
        self.seed_variant("upsell", "B", impressions=20, conversions=7)
        self.seed_variant("upsell", "C", impressions=10, conversions=2)

        rates = ScoringEngine().conversion_rates("upsell")

        self.assertAlmostEqual(rates["A"], 0.10)
        self.assertAlmostEqual(rates["B"], 0.35)
        self.assertAlmostEqual(rates["C"], 0.20)

    def test_selector_picks_best_variant(self):
        from .revenue_engine.optimization.selector import OfferSelector

        self.seed_variant("upsell", "A", impressions=10, conversions=1)
        self.seed_variant("upsell", "B", impressions=20, conversions=7)
        self.seed_variant("upsell", "C", impressions=10, conversions=2)

        selected = OfferSelector(min_impressions=10, randomizer=random.Random(1)).select(
            "upsell",
            [{"experiment_variant": "A"}, {"experiment_variant": "B"}, {"experiment_variant": "C"}],
            user_id="selector-user",
        )

        self.assertEqual(selected["experiment_variant"], "B")
        self.assertEqual(selected["selection_reason"], "best_performing")

    def test_fallback_when_no_data(self):
        from .revenue_engine.optimization.selector import OfferSelector

        selected = OfferSelector(min_impressions=10, randomizer=random.Random(1)).select(
            "upsell",
            [{"experiment_variant": "A"}, {"experiment_variant": "B"}, {"experiment_variant": "C"}],
            user_id="new-user",
        )

        self.assertIn(selected["experiment_variant"], {"A", "B", "C"})
        self.assertEqual(selected["selection_reason"], "safe_random")

    def test_no_spam_same_variant_to_same_user(self):
        from .revenue_engine.optimization.selector import OfferSelector
        from .revenue_engine.optimization.tracker import OfferTracker

        tracker = OfferTracker()
        tracker.user_received_offer("repeat-user", "upsell", "A")

        selected = OfferSelector(tracker=tracker, min_impressions=10, randomizer=random.Random(1)).select(
            "upsell",
            [{"experiment_variant": "A"}, {"experiment_variant": "B"}, {"experiment_variant": "C"}],
            user_id="repeat-user",
        )

        self.assertTrue(tracker.user_in_cooldown("repeat-user", "upsell"))
        self.assertNotEqual(selected["experiment_variant"], "A")

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_integration_with_upsell_engine(self, send_mock):
        from .revenue_engine.optimization.tracker import USER_RECEIVED_OFFER
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertTrue(result["action"]["sent"])
        self.assertIn(result["decision"]["experiment_variant"], {"A", "B", "C", "AI"})
        self.assertEqual(
            BotEventLog.objects.filter(
                raw_payload__revenue_optimization=True,
                raw_payload__offer_event=USER_RECEIVED_OFFER,
                raw_payload__offer_type="upsell",
            ).count(),
            1,
        )
        send_mock.assert_called_once()


class AIRevenueDecisionPhaseFiveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="AI Revenue Store",
            english_name="AI Revenue Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10GB",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100000,
            device_limit=2,
            sort_order=1,
        )
        self.upgrade_plan = Plan.objects.create(
            store=self.store,
            name="20GB",
            volume_gb=Decimal("20"),
            duration_days=30,
            price=130000,
            device_limit=2,
            sort_order=2,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-ai")
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

    def context(self, **extra):
        data = {
            "bot_user": self.bot_user,
            "chat_id": self.bot_user.chat_id,
            "bot_config": self.bot_config,
            "store": self.store,
            "selected_plan": self.plan,
            "plan": self.plan,
            "quantity": 1,
            "pricing": {"total": self.plan.price},
        }
        data.update(extra)
        return data

    def make_order(self):
        return Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="ai_client",
            uuid="55555555-5555-4555-8555-555555555555",
        )

    def make_vpn_client(self):
        order = self.make_order()
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="ai_client",
            xui_email="ai_client",
            uuid=order.uuid,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=int(Decimal("10") * Decimal(1024 ** 3)),
            used_traffic_bytes=int(Decimal("9") * Decimal(1024 ** 3)),
            expires_at=timezone.now() - timedelta(hours=1),
            last_synced_at=timezone.now(),
        )

    def seed_variant(self, offer_type, variant, *, impressions, conversions):
        from .revenue_engine.optimization.tracker import OfferTracker

        tracker = OfferTracker()
        for index in range(impressions):
            user_id = f"ai-{offer_type}-{variant}-{index}"
            tracker.user_received_offer(user_id, offer_type, variant)
            if index < conversions:
                tracker.user_purchased_after_offer(user_id, offer_type=offer_type)

    def test_offer_generation_from_user_context(self):
        from .revenue_engine.ai.generator import OfferGenerator
        from .revenue_engine.ai.strategy import RevenueStrategyEngine

        strategy = RevenueStrategyEngine().select_strategy(
            {"is_high_value": True, "purchase_count": 4, "lifetime_value": 900000, "usage_percent": 85}
        )
        offer = OfferGenerator().generate(
            {"usage_percent": 85},
            strategy=strategy,
            base_offer={"type": "upsell_offer", "message": "base"},
            offer_type="upsell",
        )

        self.assertEqual(offer["type"], "generated_offer")
        self.assertEqual(offer["experiment_variant"], "AI")
        self.assertEqual(offer["ai_strategy"], "premium_offer")
        self.assertIn("پریمیوم", offer["title"])

    def test_fallback_to_ab_engine(self):
        from .revenue_engine.ai.optimizer import AIRevenueOptimizer

        self.seed_variant("upsell", "A", impressions=10, conversions=1)
        self.seed_variant("upsell", "B", impressions=10, conversions=8)

        decision = AIRevenueOptimizer(confidence_threshold=0.99).optimize(
            "upsell",
            {"type": "upsell_offer", "message": "base"},
            user=self.bot_user,
            context=self.context(),
        )

        self.assertEqual(decision["experiment_variant"], "B")
        self.assertEqual(decision["ai_fallback_reason"], "low_confidence")

    def test_fallback_to_rule_engine(self):
        from .revenue_engine.ai.optimizer import AIRevenueOptimizer

        class BrokenExperiment:
            def generate_variants(self, *_args, **_kwargs):
                raise RuntimeError("ab unavailable")

        class BrokenGenerator:
            def generate(self, *_args, **_kwargs):
                raise RuntimeError("ai unavailable")

        base = {"type": "upsell_offer", "message": "rule message"}
        decision = AIRevenueOptimizer(
            experiment_engine=BrokenExperiment(),
            offer_generator=BrokenGenerator(),
        ).optimize("upsell", base, user=self.bot_user, context=self.context())

        self.assertEqual(decision["type"], "upsell_offer")
        self.assertEqual(decision["message"], "rule message")
        self.assertFalse(decision.get("ai_generated"))

    def test_prediction_accuracy_updates(self):
        from .revenue_engine.ai.predictor import PurchasePredictor
        from .revenue_engine.optimization.tracker import OfferTracker

        tracker = OfferTracker()
        tracker.user_received_offer(
            "u1",
            "upsell",
            "AI",
            metadata={"ai_generated": True, "ai_strategy": "premium_offer", "ai_prediction": 0.8},
        )
        tracker.user_purchased_after_offer("u1", offer_type="upsell")
        tracker.user_received_offer(
            "u2",
            "upsell",
            "AI",
            metadata={"ai_generated": True, "ai_strategy": "premium_offer", "ai_prediction": 0.2},
        )

        result = PurchasePredictor().update_accuracy()

        self.assertEqual(result["samples"], 2)
        self.assertGreaterEqual(result["accuracy"], 0)
        self.assertLessEqual(result["accuracy"], 1)

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_no_spam_multiple_ai_offers(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        first = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())
        second = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertTrue(first["action"]["sent"])
        self.assertEqual(second["action"]["reason"], "cooldown_active")
        send_mock.assert_called_once()

    def test_low_confidence_safe_fallback(self):
        from .revenue_engine.ai.optimizer import AIRevenueOptimizer

        decision = AIRevenueOptimizer(confidence_threshold=0.99).optimize(
            "upsell",
            {"type": "upsell_offer", "message": "base"},
            user=self.bot_user,
            context=self.context(),
        )

        self.assertFalse(decision.get("ai_generated"))
        self.assertIn(decision["experiment_variant"], {"A", "B", "C"})
        self.assertEqual(decision["ai_fallback_reason"], "low_confidence")

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_conversion_tracking_ai_offers(self, _send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_PURCHASE
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())
        self.assertTrue(result["decision"].get("ai_generated"))

        RevenueEngine().handle(
            USER_PURCHASE,
            self.make_order(),
            {"bot_user": self.bot_user, "chat_id": "42", "bot_config": self.bot_config},
        )

        received = BotEventLog.objects.get(
            raw_payload__revenue_optimization=True,
            raw_payload__offer_event="user_received_offer",
            raw_payload__offer_type="upsell",
        )
        self.assertTrue(received.raw_payload["converted"])
        self.assertTrue(received.raw_payload["metadata"]["ai_generated"])

    def test_integration_with_optimization_engine(self):
        from .revenue_engine.ai.optimizer import AIRevenueOptimizer

        self.seed_variant("upsell", "A", impressions=10, conversions=1)
        self.seed_variant("upsell", "B", impressions=10, conversions=9)

        decision = AIRevenueOptimizer().optimize(
            "upsell",
            {"type": "upsell_offer", "message": "base"},
            user=self.bot_user,
            context=self.context(),
        )

        self.assertEqual(decision["experiment_variant"], "B")
        self.assertEqual(decision["ai_fallback_reason"], "ab_expected_revenue")

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_integration_with_renewal_engine(self, send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.optimization.tracker import USER_RECEIVED_OFFER
        from .revenue_engine.triggers import USER_EXPIRED

        result = RevenueEngine().handle(USER_EXPIRED, self.make_vpn_client(), {"usage_percent": Decimal("10")})

        self.assertTrue(result["action"]["sent"])
        self.assertIn(result["decision"]["experiment_variant"], {"A", "B", "C", "AI"})
        self.assertEqual(
            BotEventLog.objects.filter(
                raw_payload__revenue_optimization=True,
                raw_payload__offer_event=USER_RECEIVED_OFFER,
                raw_payload__offer_type="renewal",
            ).count(),
            1,
        )
        send_mock.assert_called_once()


class RevenueControlCenterPhaseSixTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Control Store",
            english_name="Control Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=False,
            revenue_max_offers_per_user_per_day=5,
            revenue_max_offers_per_user_per_week=10,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10GB",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100000,
            device_limit=2,
            sort_order=1,
        )
        self.upgrade_plan = Plan.objects.create(
            store=self.store,
            name="20GB",
            volume_gb=Decimal("20"),
            duration_days=30,
            price=130000,
            device_limit=2,
            sort_order=2,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-control")
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

    def context(self, **extra):
        data = {
            "bot_user": self.bot_user,
            "chat_id": self.bot_user.chat_id,
            "bot_config": self.bot_config,
            "store": self.store,
            "customer": self.customer,
            "selected_plan": self.plan,
            "plan": self.plan,
            "quantity": 1,
            "pricing": {"total": self.plan.price},
        }
        data.update(extra)
        return data

    def make_vpn_client(self, *, expires_at=None, used_gb=9, total_gb=10):
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            is_paid=True,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            username="control_client",
            uuid="44444444-4444-4444-8444-444444444444",
        )
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username="control_client",
            xui_email="control_client",
            uuid=order.uuid,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=int(Decimal(str(total_gb)) * Decimal(1024 ** 3)),
            used_traffic_bytes=int(Decimal(str(used_gb)) * Decimal(1024 ** 3)),
            expires_at=expires_at or timezone.now() - timedelta(hours=1),
            last_synced_at=timezone.now(),
        )

    def create_offer_log(self, **extra):
        data = {
            "store": self.store,
            "customer": self.customer,
            "bot_user": self.bot_user,
            "engine_type": RevenueOfferLog.EngineType.UPSELL,
            "event_type": "checkout_started",
            "offer_type": "upsell",
            "variant": "AI",
            "decision_source": RevenueOfferLog.DecisionSource.AI,
            "status": RevenueOfferLog.Status.SENT,
            "sent_at": timezone.now(),
            "metadata": {"safe": "ok"},
        }
        data.update(extra)
        return RevenueOfferLog.objects.create(**data)

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_global_disabled_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.revenue_engine_enabled = False
        self.store.save(update_fields=["revenue_engine_enabled", "updated_at"])
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "revenue_engine_disabled")
        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.SUPPRESSED)
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_engine_specific_disabled_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.upsell_engine_enabled = False
        self.store.save(update_fields=["upsell_engine_enabled", "updated_at"])
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "engine_disabled")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_dry_run_does_not_send_and_creates_log(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.revenue_engine_dry_run = True
        self.store.save(update_fields=["revenue_engine_dry_run", "updated_at"])
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertTrue(result["action"]["dry_run"])
        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.DRY_RUN)
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_quiet_hours_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        now_time = timezone.localtime(timezone.now()).time()
        self.store.revenue_engine_quiet_hours_enabled = True
        self.store.revenue_engine_quiet_hours_start = time(0, 0)
        self.store.revenue_engine_quiet_hours_end = time(23, 59)
        if now_time.hour == 23 and now_time.minute == 59:
            self.store.revenue_engine_quiet_hours_start = time(23, 0)
            self.store.revenue_engine_quiet_hours_end = time(23, 59)
        self.store.save()
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "quiet_hours")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_daily_cap_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.revenue_max_offers_per_user_per_day = 1
        self.store.revenue_offer_cooldown_hours = 1
        self.store.save(update_fields=["revenue_max_offers_per_user_per_day", "revenue_offer_cooldown_hours", "updated_at"])
        log = self.create_offer_log(engine_type=RevenueOfferLog.EngineType.RETENTION)
        RevenueOfferLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(hours=2))
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "daily_user_cap")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_weekly_cap_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.revenue_max_offers_per_user_per_week = 1
        self.store.revenue_offer_cooldown_hours = 1
        self.store.save(update_fields=["revenue_max_offers_per_user_per_week", "revenue_offer_cooldown_hours", "updated_at"])
        log = self.create_offer_log(engine_type=RevenueOfferLog.EngineType.RETENTION)
        RevenueOfferLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(days=2))
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "weekly_user_cap")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_cooldown_suppresses_offer(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.create_offer_log(engine_type=RevenueOfferLog.EngineType.UPSELL)
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertEqual(result["action"]["reason"], "cooldown_active")
        send_mock.assert_not_called()

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_no_telegram_target_is_skipped(self, send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_EXPIRED

        customer = Customer.objects.create(display_name="No Target")
        result = RevenueEngine().handle(USER_EXPIRED, customer, {"store": self.store, "customer": customer})

        self.assertEqual(result["action"]["reason"], "no_personal_telegram_target")
        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.SKIPPED)
        send_mock.assert_not_called()

    @patch("store.revenue_engine.upsell.actions.send_to_config", side_effect=RuntimeError("boom"))
    def test_send_fail_creates_failed_log_without_crash(self, send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertTrue(result["action"]["failed"])
        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.FAILED)
        send_mock.assert_called_once()

    def test_metadata_sanitize_removes_sensitive_values(self):
        from .revenue_engine.guards import sanitize_revenue_metadata

        sanitized = sanitize_revenue_metadata(
            {
                "config_link": "vless://11111111-1111-4111-8111-111111111111@example.com",
                "api_token": "a" * 40,
                "note": "user email alice@example.com phone +989121234567",
            }
        )

        text = json.dumps(sanitized)
        self.assertNotIn("vless://", text)
        self.assertNotIn("alice@example.com", text)
        self.assertNotIn("+989121234567", text)
        self.assertNotIn("a" * 40, text)

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_renewal_action_passes_guard_and_logs_sent(self, send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_EXPIRED

        result = RevenueEngine().handle(USER_EXPIRED, self.make_vpn_client(), self.context(usage_percent=10))

        self.assertTrue(result["action"]["sent"])
        self.assertEqual(RevenueOfferLog.objects.get().engine_type, RevenueOfferLog.EngineType.RENEWAL)
        send_mock.assert_called_once()

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_silent_active_remains_support_oriented(self, send_mock):
        from .revenue_engine.retention.engine import RetentionEngine
        from .revenue_engine.retention.triggers import SILENT_ACTIVE_USER

        result = RetentionEngine().handle(
            SILENT_ACTIVE_USER,
            self.bot_user,
            self.context(last_connection=timezone.now() - timedelta(hours=49)),
        )

        self.assertTrue(result["action"]["sent"])
        self.assertIn("پشتیبانی در دسترس است", send_mock.call_args.kwargs["text"])
        self.assertEqual(RevenueOfferLog.objects.get().engine_type, RevenueOfferLog.EngineType.SILENT_ACTIVE)

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_ai_low_confidence_falls_back(self, _send_mock):
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        self.store.revenue_min_ai_confidence = Decimal("0.95")
        self.store.save(update_fields=["revenue_min_ai_confidence", "updated_at"])
        result = UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())

        self.assertFalse(result["decision"].get("ai_generated"))
        self.assertIn(result["decision"]["experiment_variant"], {"A", "B", "C"})

    @patch("store.revenue_engine.upsell.actions.send_to_config", return_value=True)
    def test_purchase_after_offer_marks_converted(self, _send_mock):
        from .revenue_engine.engine import RevenueEngine
        from .revenue_engine.triggers import USER_PURCHASE
        from .revenue_engine.upsell.engine import UpsellEngine
        from .revenue_engine.upsell.triggers import CHECKOUT_STARTED

        UpsellEngine().handle(CHECKOUT_STARTED, self.bot_user, self.context())
        order = self.make_vpn_client().order
        RevenueEngine().handle(USER_PURCHASE, order, self.context(order=order))

        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.CONVERTED)

    def test_dry_run_offer_is_not_converted(self):
        from .revenue_engine.guards import mark_latest_revenue_offer_converted

        self.create_offer_log(status=RevenueOfferLog.Status.DRY_RUN)
        converted = mark_latest_revenue_offer_converted(self.customer, bot_user=self.bot_user)

        self.assertIsNone(converted)
        self.assertEqual(RevenueOfferLog.objects.get().status, RevenueOfferLog.Status.DRY_RUN)

    def test_duplicate_conversion_does_not_create_second_conversion(self):
        from .revenue_engine.guards import mark_latest_revenue_offer_converted

        self.create_offer_log()
        first = mark_latest_revenue_offer_converted(self.customer, bot_user=self.bot_user)
        second = mark_latest_revenue_offer_converted(self.customer, bot_user=self.bot_user)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.CONVERTED).count(), 1)

    @patch("store.revenue_engine.actions.send_to_config", return_value=True)
    def test_run_revenue_scan_dry_run_does_not_send(self, send_mock):
        self.make_vpn_client()
        out = StringIO()

        call_command("run_revenue_scan", "--engine", "renewal", "--dry-run", stdout=out)

        self.assertIn("dry_run=2", out.getvalue())
        send_mock.assert_not_called()

    def test_revenue_report_conversion_rate(self):
        self.create_offer_log(status=RevenueOfferLog.Status.CONVERTED)
        self.create_offer_log(status=RevenueOfferLog.Status.SENT, variant="B")
        out = StringIO()

        call_command("revenue_report", "--days", "1", stdout=out)

        self.assertIn("offers_sent=2", out.getvalue())
        self.assertIn("conversions=1", out.getvalue())
        self.assertIn("conversion_rate=50.00%", out.getvalue())

    def test_revenue_offer_log_admin_registered_and_store_fieldset_exists(self):
        from django.contrib import admin as django_admin
        from .admin import StoreAdmin as RegisteredStoreAdmin

        self.assertIn(RevenueOfferLog, django_admin.site._registry)
        user = get_user_model().objects.create_superuser(username="admin", password="x", email="admin@example.com")
        request = SimpleNamespace(user=user)
        store_admin = RegisteredStoreAdmin(Store, django_admin.site)
        fieldset_names = [name for name, _options in store_admin.get_fieldsets(request)]
        self.assertIn("Revenue Engine Controls", fieldset_names)

    def test_daily_report_includes_revenue_engine_section(self):
        from .daily_report_services import build_daily_admin_report_message

        self.create_offer_log(status=RevenueOfferLog.Status.CONVERTED)
        message = build_daily_admin_report_message(timezone.localdate(), store=self.store, persist_panel_usage=False)

        self.assertIn("Revenue Engine", message)
        self.assertIn("conversion", message)

    def test_check_integrations_reports_dry_run_and_latest_log(self):
        self.store.revenue_engine_dry_run = True
        self.store.save(update_fields=["revenue_engine_dry_run", "updated_at"])
        self.create_offer_log()
        out = StringIO()

        call_command("check_integrations", "--no-fail", stdout=out)

        output = out.getvalue()
        self.assertIn("Revenue Engine dry_run is enabled", output)
        self.assertIn("Latest RevenueOfferLog", output)

    def test_revenue_offer_log_metadata_does_not_store_sensitive_values(self):
        from .revenue_engine.guards import record_revenue_offer_attempt

        record_revenue_offer_attempt(
            user=self.bot_user,
            context=self.context(),
            engine_type="upsell",
            event_type="checkout_started",
            decision={"type": "upsell_offer", "experiment_variant": "AI"},
            status=RevenueOfferLog.Status.SENT,
            metadata={
                "subscription_link": "https://example.com/sub/SECRET-TOKEN",
                "uuid": "11111111-1111-4111-8111-111111111111",
                "email": "alice@example.com",
            },
        )

        payload = json.dumps(RevenueOfferLog.objects.get().metadata)
        self.assertNotIn("SECRET-TOKEN", payload)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", payload)
        self.assertNotIn("alice@example.com", payload)


class RevenueCanaryPhaseSevenDTests(TestCase):
    canary_confirm = "SEND_ONE_REVENUE_CANARY"

    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="Canary Store",
            english_name="Canary Store",
            card_number="0000000000000000",
            card_owner="Alice",
            revenue_engine_dry_run=True,
            revenue_max_offers_per_user_per_day=5,
            revenue_max_offers_per_user_per_week=10,
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10GB",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100000,
            device_limit=2,
            sort_order=1,
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
        self.customer = Customer.objects.create(display_name="Alice", username="alice-canary")
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

    def ai_decision(self, **extra):
        decision = {
            "type": "generated_offer",
            "message": "یک پیشنهاد بازگشت امن برای تست آماده است.",
            "optimization_offer_type": "retention",
            "experiment_variant": "AI",
            "ai_generated": True,
            "ai_confidence": 0.8,
            "ai_prediction": 0.4,
            "selection_reason": "ai_expected_revenue",
        }
        decision.update(extra)
        return decision

    def create_canary_source_log(self, **extra):
        data = {
            "store": self.store,
            "customer": self.customer,
            "bot_user": self.bot_user,
            "engine_type": RevenueOfferLog.EngineType.RETENTION,
            "event_type": "user_inactive_72h",
            "offer_type": "retention",
            "variant": "AI",
            "decision_source": RevenueOfferLog.DecisionSource.AI,
            "status": RevenueOfferLog.Status.DRY_RUN,
            "skip_reason": "dry_run",
            "metadata": {"safe": "ok"},
        }
        data.update(extra)
        return RevenueOfferLog.objects.create(**data)

    def create_offer_log(self, **extra):
        data = {
            "store": self.store,
            "customer": self.customer,
            "bot_user": self.bot_user,
            "engine_type": RevenueOfferLog.EngineType.RETENTION,
            "event_type": "user_inactive_72h",
            "offer_type": "retention",
            "variant": "AI",
            "decision_source": RevenueOfferLog.DecisionSource.AI,
            "status": RevenueOfferLog.Status.SENT,
            "sent_at": timezone.now(),
            "metadata": {"safe": "ok"},
        }
        data.update(extra)
        return RevenueOfferLog.objects.create(**data)

    def call_canary(self, source_log, **extra):
        options = {
            "offer_log_id": str(source_log.pk),
            "customer_id": str(self.customer.pk),
            "bot_user_id": str(self.bot_user.pk),
            "confirm": self.canary_confirm,
            "validation_result": {"ok": True, "reason": "valid", "safe_error": ""},
        }
        options.update(extra)
        validation_result = options.pop("validation_result")
        out = StringIO()
        with patch(
            "store.management.commands.send_revenue_canary.validate_telegram_target",
            return_value=validation_result,
        ) as validation_mock:
            self.last_target_validation_mock = validation_mock
            call_command(
                "send_revenue_canary",
                "--offer-log-id",
                options["offer_log_id"],
                "--customer-id",
                options["customer_id"],
                "--bot-user-id",
                options["bot_user_id"],
                "--confirm",
                options["confirm"],
                stdout=out,
            )
        return out.getvalue()

    def create_batch_candidate(self, suffix, **extra):
        customer = Customer.objects.create(display_name=f"Batch {suffix}", username=f"batch-{suffix}")
        bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=customer,
            provider_user_id=f"batch-{suffix}",
            chat_id=f"batch-{suffix}",
            username=f"batch{suffix}",
            display_name=f"Batch {suffix}",
        )
        return self.create_canary_source_log(customer=customer, bot_user=bot_user, **extra)

    def call_limited_batch(self, *, preview=False, confirm=False, limit=3, verbose=False, retry_transient_failed=False):
        args = [
            "send_revenue_limited_batch",
            "--engine",
            "retention",
            "--event",
            "user_inactive_72h",
            "--limit",
            str(limit),
            "--days",
            "7",
        ]
        if preview:
            args.append("--preview")
        if confirm:
            args.extend(["--confirm", "SEND_LIMITED_REVENUE_BATCH"])
        if verbose:
            args.append("--verbose")
        if retry_transient_failed:
            args.append("--retry-transient-failed")
        out = StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    @patch("store.telegram_bot.client.requests.post", return_value=DummyBotResponse({"ok": True, "result": {"id": 42}}))
    def test_target_validator_get_chat_success(self, post_mock):
        from .telegram_bot.target_validation import validate_telegram_target

        result = validate_telegram_target(self.bot_user)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "valid")
        self.assertTrue(post_mock.call_args.args[0].endswith("/getChat"))
        self.assertFalse(post_mock.call_args.args[0].endswith("/sendMessage"))

    @patch(
        "store.telegram_bot.client.requests.post",
        return_value=DummyBotResponse({"ok": False, "description": "Bad Request: chat not found"}),
    )
    def test_target_validator_chat_not_found(self, _post_mock):
        from .telegram_bot.target_validation import validate_telegram_target

        result = validate_telegram_target(self.bot_user)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "chat_not_found")
        self.assertNotIn(self.bot_user.chat_id, result["safe_error"])

    @patch(
        "store.telegram_bot.client.requests.post",
        return_value=DummyBotResponse({"ok": False, "description": "Forbidden: bot was blocked by the user"}),
    )
    def test_target_validator_bot_blocked(self, _post_mock):
        from .telegram_bot.target_validation import validate_telegram_target

        result = validate_telegram_target(self.bot_user)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bot_blocked")

    @patch("store.telegram_bot.client.requests.post", side_effect=requests.exceptions.Timeout("Read timed out"))
    def test_target_validator_timeout(self, _post_mock):
        from .telegram_bot.target_validation import validate_telegram_target

        result = validate_telegram_target(self.bot_user)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")

    def test_validate_revenue_targets_never_sends_and_recommends_valid_candidate(self):
        invalid_log = self.create_canary_source_log()
        valid_customer = Customer.objects.create(display_name="Valid Candidate", username="valid-candidate")
        valid_bot_user = BotUser.objects.create(
            bot_config=self.bot_config,
            customer=valid_customer,
            provider_user_id="777",
            chat_id="777",
            username="validuser",
            display_name="Valid User",
        )
        valid_log = self.create_canary_source_log(customer=valid_customer, bot_user=valid_bot_user)

        def fake_post(url, json=None, **kwargs):
            self.assertFalse(url.endswith("/sendMessage"))
            if json and str(json.get("chat_id")) == "777":
                return DummyBotResponse({"ok": True, "result": {"id": 777}})
            return DummyBotResponse({"ok": False, "description": "Bad Request: chat not found"})

        out = StringIO()
        with patch("store.telegram_bot.client.requests.post", side_effect=fake_post):
            call_command(
                "validate_revenue_targets",
                "--days",
                "7",
                "--limit",
                "10",
                "--only-dry-run-candidates",
                "--verbose",
                stdout=out,
            )
        output = out.getvalue()

        self.assertIn("valid_canary_candidate_found=yes", output)
        self.assertIn(f"revenue_offer_log_pk={valid_log.pk}", output)
        self.assertNotIn(f"recommended_candidate:\nrevenue_offer_log_pk={invalid_log.pk}", output)
        self.assertIn("chat_not_found=1", output)
        self.assertNotIn("sendMessage", output)
        self.assertNotIn("777", output)
        self.assertNotIn("42", output)
        self.assertNotIn("123:token", output)

    def test_validate_revenue_targets_writes_safe_metadata(self):
        self.bot_user.chat_id = "chat-secret-target"
        self.bot_user.provider_user_id = "chat-secret-target"
        self.bot_user.save(update_fields=["chat_id", "provider_user_id", "updated_at"])
        source_log = self.create_canary_source_log()
        out = StringIO()
        with patch(
            "store.telegram_bot.client.requests.post",
            return_value=DummyBotResponse({"ok": True, "result": {"id": "chat-secret-target"}}),
        ):
            call_command(
                "validate_revenue_targets",
                "--days",
                "7",
                "--limit",
                "10",
                "--only-dry-run-candidates",
                "--write-validation-metadata",
                stdout=out,
            )

        source_log.refresh_from_db()
        target_validation = source_log.metadata["target_validation"]
        self.assertTrue(target_validation["ok"])
        self.assertEqual(target_validation["reason"], "valid")
        payload = json.dumps(source_log.metadata)
        self.assertNotIn(self.bot_user.chat_id, payload)
        self.assertNotIn(self.bot_config.bot_token, payload)
        self.store.refresh_from_db()
        self.assertTrue(self.store.revenue_engine_dry_run)

    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_requires_confirmation(self, send_mock):
        source_log = self.create_canary_source_log()

        with self.assertRaises(CommandError):
            self.call_canary(source_log, confirm="NOPE")

        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_rejects_invalid_candidate(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log(variant="B")

        with self.assertRaises(CommandError):
            self.call_canary(source_log)

        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_sends_exactly_one_message_and_keeps_global_dry_run(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        output = self.call_canary(source_log)

        self.store.refresh_from_db()
        self.assertTrue(self.store.revenue_engine_dry_run)
        self.assertIn("Revenue canary sent", output)
        send_mock.assert_called_once()
        sent_logs = RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.SENT)
        self.assertEqual(sent_logs.count(), 1)
        sent_log = sent_logs.get()
        self.assertEqual(sent_log.customer, self.customer)
        self.assertTrue(sent_log.metadata["canary"])
        self.assertEqual(sent_log.metadata["source_dry_run_log_id"], source_log.pk)
        self.assertIn("canary_command_version", sent_log.metadata)
        source_log.refresh_from_db()
        self.assertEqual(source_log.metadata["canary_sent_log_id"], sent_log.pk)
        self.assertIn("canary_sent_at", source_log.metadata)
        self.assertIn("canary_command_version", source_log.metadata)
        self.last_target_validation_mock.assert_called_once()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_source_log_is_idempotent_after_sent(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        self.call_canary(source_log)

        with self.assertRaisesMessage(CommandError, "already attempted"):
            self.call_canary(source_log)

        send_mock.assert_called_once()
        self.assertEqual(RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.SENT).count(), 1)

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_global_cooldown_blocks_second_recent_canary(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()
        self.create_offer_log(
            metadata={"canary": True, "source_dry_run_log_id": 999, "canary_command_version": "test"},
        )

        with self.assertRaisesMessage(CommandError, "another canary was sent within the last 24 hours"):
            self.call_canary(source_log)

        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_failed_source_log_blocks_retry(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()
        failed_log = self.create_offer_log(
            status=RevenueOfferLog.Status.FAILED,
            sent_at=None,
            error_message="send_failed",
            metadata={"canary": True, "source_dry_run_log_id": source_log.pk},
        )

        with self.assertRaisesMessage(CommandError, "already has a canary attempt"):
            self.call_canary(source_log)

        send_mock.assert_not_called()
        self.assertEqual(failed_log.status, RevenueOfferLog.Status.FAILED)

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_refuses_when_store_dry_run_is_false(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()
        self.store.revenue_engine_dry_run = False
        self.store.save(update_fields=["revenue_engine_dry_run", "updated_at"])

        with self.assertRaises(CommandError):
            self.call_canary(source_log)

        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_keeps_source_dry_run_log_unchanged(self, _send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        self.call_canary(source_log)

        source_log.refresh_from_db()
        self.assertEqual(source_log.status, RevenueOfferLog.Status.DRY_RUN)
        self.assertEqual(source_log.skip_reason, "dry_run")

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_recent_sent_suppresses_send(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()
        self.create_offer_log(
            engine_type=RevenueOfferLog.EngineType.RETENTION,
            status=RevenueOfferLog.Status.SENT,
        )

        with self.assertRaises(CommandError):
            self.call_canary(source_log)

        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_no_telegram_target_is_skipped_safely(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()
        self.bot_user.chat_id = ""
        self.bot_user.save(update_fields=["chat_id", "updated_at"])

        with self.assertRaises(CommandError):
            self.call_canary(source_log)

        send_mock.assert_not_called()
        skipped = RevenueOfferLog.objects.exclude(pk=source_log.pk).get()
        self.assertEqual(skipped.status, RevenueOfferLog.Status.SKIPPED)
        self.assertEqual(skipped.skip_reason, "no_personal_telegram_target")

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_invalid_validated_target_fails_without_send(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        with self.assertRaises(CommandError):
            self.call_canary(
                source_log,
                validation_result={"ok": False, "reason": "chat_not_found", "safe_error": "chat not found"},
            )

        send_mock.assert_not_called()
        failed = RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.FAILED).get()
        self.assertEqual(failed.error_message, "telegram_target_invalid: chat_not_found")
        self.assertEqual(failed.metadata["target_validation"]["reason"], "chat_not_found")
        self.assertIn("canary_command_version", failed.metadata)
        source_log.refresh_from_db()
        self.assertEqual(source_log.metadata["canary_failed_log_id"], failed.pk)
        self.assertIn("canary_failed_at", source_log.metadata)
        self.assertNotIn(self.bot_user.chat_id, json.dumps(failed.metadata))
        self.store.refresh_from_db()
        self.assertTrue(self.store.revenue_engine_dry_run)

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", side_effect=RuntimeError("boom"))
    def test_canary_send_failure_creates_failed_log(self, send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        with self.assertRaises(CommandError):
            self.call_canary(source_log)

        send_mock.assert_called_once()
        failed = RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.FAILED).get()
        self.assertEqual(failed.customer, self.customer)
        self.assertTrue(failed.metadata["canary"])
        source_log.refresh_from_db()
        self.assertEqual(source_log.metadata["canary_failed_log_id"], failed.pk)
        self.assertIn("canary_failed_at", source_log.metadata)

    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_metadata_is_safe(self, _send_mock, optimize_mock):
        optimize_mock.return_value = self.ai_decision(message="token abcdefghijklmnopqrstuvwxyz1234567890 email alice@example.com")
        self.bot_user.chat_id = "chat-secret-target"
        self.bot_user.provider_user_id = "chat-secret-target"
        self.bot_user.save(update_fields=["chat_id", "provider_user_id", "updated_at"])
        source_log = self.create_canary_source_log(metadata={"token": "secret"})

        self.call_canary(source_log)

        payload = json.dumps(RevenueOfferLog.objects.get(status=RevenueOfferLog.Status.SENT).metadata)
        self.assertIn('"canary": true', payload)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz1234567890", payload)
        self.assertNotIn("alice@example.com", payload)
        self.assertNotIn("secret", payload)
        source_log.refresh_from_db()
        source_payload = json.dumps(source_log.metadata)
        self.assertNotIn(self.bot_user.chat_id, source_payload)
        self.assertNotIn(self.bot_config.bot_token, source_payload)
        self.assertNotIn("secret", source_payload)

    @patch("store.revenue_engine.scheduler.run_revenue_scan")
    @patch("store.management.commands.send_revenue_canary.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_canary_command_does_not_run_general_scan(self, _send_mock, optimize_mock, scan_mock):
        optimize_mock.return_value = self.ai_decision()
        source_log = self.create_canary_source_log()

        self.call_canary(source_log)

        scan_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_requires_confirm_before_send(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        self.create_batch_candidate("confirm")

        with self.assertRaises(CommandError):
            self.call_limited_batch()

        validation_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_preview_never_sends(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("preview")

        output = self.call_limited_batch(preview=True)

        self.assertIn("status=PREVIEW_OK", output)
        self.assertIn("selected_count=1", output)
        self.assertIn(f"selected_offer_log_ids=[{source_log.pk}]", output)
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_selects_only_retention_inactive_72h(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        selected = self.create_batch_candidate("retention")
        self.create_batch_candidate("upsell", engine_type=RevenueOfferLog.EngineType.UPSELL)
        self.create_batch_candidate("inactive24", event_type="user_inactive_24h")

        output = self.call_limited_batch(preview=True)

        self.assertIn(f"selected_offer_log_ids=[{selected.pk}]", output)
        self.assertNotIn("upsell", output)
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_skips_invalid_target(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": False, "reason": "chat_not_found"}
        self.create_batch_candidate("invalid")

        output = self.call_limited_batch(preview=True)

        self.assertIn("selected_count=0", output)
        self.assertIn("skipped_invalid_target=1", output)
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_skips_customer_with_recent_sent(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("recent")
        self.create_offer_log(customer=source_log.customer, bot_user=source_log.bot_user)

        output = self.call_limited_batch(preview=True)

        self.assertIn("selected_count=0", output)
        self.assertIn("skipped_recent_sent=1", output)
        validation_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_skips_existing_attempt(self, send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("attempt")
        self.create_offer_log(
            customer=source_log.customer,
            bot_user=source_log.bot_user,
            status=RevenueOfferLog.Status.FAILED,
            sent_at=None,
            metadata={"limited_batch": True, "source_dry_run_log_id": source_log.pk},
        )

        output = self.call_limited_batch(preview=True)

        self.assertIn("selected_count=0", output)
        self.assertIn("skipped_existing_attempt=1", output)
        validation_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("time.sleep")
    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_respects_max_limit_three(self, send_mock, optimize_mock, validation_mock, sleep_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        for index in range(5):
            self.create_batch_candidate(f"limit-{index}")

        output = self.call_limited_batch(confirm=True, limit=5)

        self.assertIn("status=LIMITED_BATCH_OK", output)
        self.assertIn("sent_count=3", output)
        self.assertEqual(send_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.store.refresh_from_db()
        self.assertTrue(self.store.revenue_engine_dry_run)

    @patch("time.sleep")
    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_all_uses_store_daily_cap(self, send_mock, optimize_mock, validation_mock, sleep_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        self.store.revenue_max_total_offers_per_day = 4
        self.store.save(update_fields=["revenue_max_total_offers_per_day", "updated_at"])
        for index in range(6):
            self.create_batch_candidate(f"all-{index}")

        output = self.call_limited_batch(confirm=True, limit="all")

        self.assertIn("limit_mode_all=1", output)
        self.assertIn("daily_cap_configured=4", output)
        self.assertIn("cap_used=4", output)
        self.assertIn("not_selected_due_to_cap=2", output)
        self.assertIn("estimated_real_sends=4", output)
        self.assertIn("sent_count=4", output)
        self.assertEqual(send_mock.call_count, 4)
        self.assertEqual(sleep_mock.call_count, 3)
        self.store.refresh_from_db()
        self.assertTrue(self.store.revenue_engine_dry_run)

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_requires_retry_flag_for_transient_failed_attempt(
        self,
        send_mock,
        optimize_mock,
        validation_mock,
    ):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("transient-retry")
        failed_log = self.create_offer_log(
            customer=source_log.customer,
            bot_user=source_log.bot_user,
            status=RevenueOfferLog.Status.FAILED,
            sent_at=None,
            error_message="Read timed out while connecting through proxy",
            metadata={"limited_batch": True, "source_dry_run_log_id": source_log.pk},
        )
        source_log.metadata = {"limited_batch_failed_log_id": failed_log.pk}
        source_log.save(update_fields=["metadata"])

        without_retry = self.call_limited_batch(preview=True, limit="all")
        with_retry = self.call_limited_batch(preview=True, limit="all", retry_transient_failed=True)

        self.assertIn("selected_count=0", without_retry)
        self.assertIn("skipped_existing_attempt=1", without_retry)
        self.assertIn("selected_count=1", with_retry)
        send_mock.assert_not_called()

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_retry_flag_still_skips_non_transient_failed_attempt(
        self,
        send_mock,
        optimize_mock,
        validation_mock,
    ):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("invalid-retry")
        failed_log = self.create_offer_log(
            customer=source_log.customer,
            bot_user=source_log.bot_user,
            status=RevenueOfferLog.Status.FAILED,
            sent_at=None,
            error_message="telegram_target_invalid: chat_not_found",
            metadata={"limited_batch": True, "source_dry_run_log_id": source_log.pk},
        )
        source_log.metadata = {"limited_batch_failed_log_id": failed_log.pk}
        source_log.save(update_fields=["metadata"])

        output = self.call_limited_batch(preview=True, limit="all", retry_transient_failed=True)

        self.assertIn("selected_count=0", output)
        self.assertIn("skipped_existing_attempt=1", output)
        validation_mock.assert_not_called()
        send_mock.assert_not_called()

    @patch("time.sleep")
    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config")
    def test_limited_batch_stops_after_send_failure(self, send_mock, optimize_mock, validation_mock, _sleep_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        send_mock.side_effect = [True, RuntimeError("boom"), True]
        for index in range(3):
            self.create_batch_candidate(f"fail-{index}")

        output = self.call_limited_batch(confirm=True)

        self.assertIn("status=LIMITED_BATCH_FAILED", output)
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.SENT).count(), 1)
        self.assertEqual(RevenueOfferLog.objects.filter(status=RevenueOfferLog.Status.FAILED).count(), 1)

    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_metadata_is_safe_and_updates_source(self, _send_mock, optimize_mock, validation_mock):
        optimize_mock.return_value = self.ai_decision(message="token abcdefghijklmnopqrstuvwxyz1234567890 email alice@example.com")
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        source_log = self.create_batch_candidate("safe", metadata={"token": "secret"})
        source_log.bot_user.chat_id = "chat-secret-target"
        source_log.bot_user.provider_user_id = "chat-secret-target"
        source_log.bot_user.save(update_fields=["chat_id", "provider_user_id", "updated_at"])

        output = self.call_limited_batch(confirm=True)

        self.assertIn("status=LIMITED_BATCH_OK", output)
        sent_log = RevenueOfferLog.objects.get(status=RevenueOfferLog.Status.SENT)
        self.assertTrue(sent_log.metadata["limited_batch"])
        self.assertIn("batch_command_version", sent_log.metadata)
        self.assertEqual(sent_log.metadata["source_dry_run_log_id"], source_log.pk)
        source_log.refresh_from_db()
        self.assertEqual(source_log.metadata["limited_batch_sent_log_id"], sent_log.pk)
        payload = json.dumps(sent_log.metadata)
        source_payload = json.dumps(source_log.metadata)
        for raw in [
            "chat-secret-target",
            self.bot_config.bot_token,
            "abcdefghijklmnopqrstuvwxyz1234567890",
            "alice@example.com",
            "secret",
        ]:
            self.assertNotIn(raw, payload)
            self.assertNotIn(raw, source_payload)

    @patch("store.revenue_engine.scheduler.run_revenue_scan")
    @patch("store.management.commands.send_revenue_limited_batch.validate_telegram_target")
    @patch("store.management.commands.send_revenue_limited_batch.RetentionEngine._optimize_offer")
    @patch("store.revenue_engine.retention.actions.send_to_config", return_value=True)
    def test_limited_batch_does_not_run_general_scan(self, _send_mock, optimize_mock, validation_mock, scan_mock):
        optimize_mock.return_value = self.ai_decision()
        validation_mock.return_value = {"ok": True, "reason": "valid"}
        self.create_batch_candidate("scan")

        self.call_limited_batch(confirm=True)

        scan_mock.assert_not_called()


class BootstrapInstallCommandTests(TestCase):
    admin_password = "admin-super-secret"
    bot_token = "test-bot-token:placeholder"
    panel_password = "panel-super-secret"

    def write_config(self, config):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "install.config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def base_config(self, *, telegram=True, xui=False):
        return {
            "app": {
                "install_dir": "/opt/vpn-store",
                "domain": "example.com",
                "enable_tls": False,
                "timezone": "Asia/Tehran",
                "language": "fa",
            },
            "admin": {
                "username": "bootstrap-admin",
                "email": "admin@example.com",
                "password": self.admin_password,
            },
            "database": {
                "engine": "sqlite",
                "sqlite_path": "/opt/vpn-store/data/db.sqlite3",
            },
            "store": {
                "slug": "bootstrap-store",
                "name": "Bootstrap Store",
                "english_name": "Bootstrap Store",
                "domain": "example.com",
                "card_number": "0000000000000000",
                "card_owner": "Configure Payment Owner",
            },
            "telegram": {
                "enabled": telegram,
                "bot_token": self.bot_token if telegram else "",
                "bot_username": "bootstrap_bot",
                "admin_ids": ["123456789"],
            },
            "xui": {
                "configure_now": xui,
                "name": "Primary X-UI panel",
                "panel_url": "https://panel.example.com",
                "username": "panel-admin",
                "password": self.panel_password,
                "inbounds": [
                    {
                        "key": "primary-vless",
                        "inbound_id": 1,
                        "remark": "Primary VLESS",
                        "protocol": "vless",
                        "server_ip": "vpn.example.com",
                        "port": "443",
                        "config_params": "type=tcp&security=none",
                        "network_type": "tcp",
                        "security": "none",
                    }
                ],
            },
            "plans": [
                {
                    "key": "starter-30d",
                    "name": "Starter 30D",
                    "traffic_gb": "30",
                    "duration_days": 30,
                    "price": 100000,
                    "currency": "TOMAN",
                    "device_limit": 2,
                    "is_public": True,
                }
            ],
            "plan_routes": [
                {
                    "plan": "starter-30d",
                    "inbound": "primary-vless",
                    "priority": 100,
                    "weight": 1,
                }
            ],
            "revenue_engine": {
                "enabled": True,
                "dry_run": True,
            },
        }

    def minimal_config(self):
        return {
            "app": {
                "install_dir": "/opt/qasedak",
                "domain": "",
                "enable_tls": False,
                "timezone": "Asia/Tehran",
                "language": "fa",
            },
            "admin": {
                "username": "qasedak-admin",
                "email": "",
                "password": self.admin_password,
            },
            "database": {
                "engine": "sqlite",
                "sqlite_path": "/opt/qasedak/data/db.sqlite3",
            },
            "store": {
                "name": "Qasedak",
                "english_name": "Qasedak",
            },
            "telegram": {
                "enabled": False,
            },
            "xui": {
                "configure_now": False,
            },
            "revenue_engine": {
                "enabled": True,
                "dry_run": True,
            },
        }

    def call_bootstrap(self, config, *args):
        out = StringIO()
        err = StringIO()
        call_command(
            "bootstrap_install",
            "--config",
            str(self.write_config(config)),
            *args,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def object_counts(self):
        User = get_user_model()
        return {
            "users": User.objects.count(),
            "stores": Store.objects.count(),
            "bots": BotConfiguration.objects.count(),
            "panels": Panel.objects.count(),
            "inbounds": Inbound.objects.count(),
            "plans": Plan.objects.count(),
            "routes": PlanInboundRoute.objects.count(),
        }

    def test_dry_run_does_not_create_db_objects(self):
        config = self.base_config(telegram=True, xui=True)
        before = self.object_counts()

        out, err = self.call_bootstrap(config, "--dry-run")

        self.assertEqual(self.object_counts(), before)
        self.assertIn("would_create", out)
        self.assertEqual(err, "")

    def test_real_run_creates_admin_store_and_revenue_dry_run(self):
        config = self.base_config(telegram=False, xui=False)

        self.call_bootstrap(config, "--yes")

        User = get_user_model()
        user = User.objects.get(username="bootstrap-admin")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(self.admin_password))
        store = Store.objects.get(slug="bootstrap-store")
        self.assertTrue(store.revenue_engine_enabled)
        self.assertTrue(store.revenue_engine_dry_run)

    def test_minimal_config_creates_admin_store_only_and_reports_setup_incomplete(self):
        config = self.minimal_config()

        out, err = self.call_bootstrap(config, "--yes")

        User = get_user_model()
        user = User.objects.get(username="qasedak-admin")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(self.admin_password))
        store = Store.objects.get(name="Qasedak")
        self.assertTrue(store.revenue_engine_enabled)
        self.assertTrue(store.revenue_engine_dry_run)
        self.assertEqual(BotConfiguration.objects.count(), 0)
        self.assertEqual(Panel.objects.count(), 0)
        self.assertEqual(Inbound.objects.count(), 0)
        self.assertEqual(Plan.objects.count(), 0)
        self.assertEqual(PlanInboundRoute.objects.count(), 0)
        self.assertIn("install_status=complete", out)
        self.assertIn("business_setup=incomplete", out)
        self.assertNotIn(self.admin_password, out + err)

    def test_rerun_is_idempotent(self):
        config = self.base_config(telegram=True, xui=True)

        self.call_bootstrap(config, "--yes")
        first_counts = self.object_counts()
        self.call_bootstrap(config, "--yes")

        self.assertEqual(self.object_counts(), first_counts)

    def test_bot_configuration_is_created_and_token_is_redacted(self):
        config = self.base_config(telegram=True, xui=False)

        out, err = self.call_bootstrap(config, "--yes")

        bot_config = BotConfiguration.objects.get(provider=BotConfiguration.Provider.TELEGRAM)
        self.assertEqual(bot_config.bot_token, self.bot_token)
        self.assertNotIn(self.bot_token, out + err)
        self.assertNotIn(self.admin_password, out + err)

    def test_telegram_enabled_without_token_fails(self):
        config = self.base_config(telegram=True, xui=False)
        config["telegram"]["bot_token"] = ""

        with self.assertRaises(CommandError):
            self.call_bootstrap(config, "--dry-run")

        self.assertEqual(BotConfiguration.objects.count(), 0)

    def test_invalid_telegram_admin_id_fails(self):
        config = self.base_config(telegram=True, xui=False)
        config["telegram"]["admin_ids"] = ["not-numeric"]

        with self.assertRaises(CommandError):
            self.call_bootstrap(config, "--dry-run")

    def test_xui_configure_false_without_panel_does_not_fail(self):
        config = self.base_config(telegram=False, xui=False)
        config["xui"] = {"configure_now": False}

        self.call_bootstrap(config, "--yes")

        self.assertEqual(Panel.objects.count(), 0)
        self.assertEqual(Inbound.objects.count(), 0)
        self.assertEqual(Plan.objects.count(), 0)
        self.assertEqual(PlanInboundRoute.objects.count(), 0)

    def test_xui_configure_true_without_credentials_fails(self):
        config = self.base_config(telegram=False, xui=True)
        config["xui"]["username"] = ""
        config["xui"]["password"] = ""

        with self.assertRaises(CommandError):
            self.call_bootstrap(config, "--dry-run")

    def test_xui_panel_inbound_plan_and_route_are_created(self):
        config = self.base_config(telegram=False, xui=True)

        self.call_bootstrap(config, "--yes")

        panel = Panel.objects.get(name="Primary X-UI panel")
        inbound = Inbound.objects.get(panel=panel, inbound_id=1)
        plan = Plan.objects.get(slug="starter-30d")
        route = PlanInboundRoute.objects.get(plan=plan, inbound=inbound)
        self.assertTrue(panel.is_active)
        self.assertTrue(inbound.available_for_new_orders)
        self.assertTrue(inbound.health_monitor_enabled)
        self.assertTrue(plan.is_public)
        self.assertTrue(route.is_active)

    def test_unknown_route_reference_fails(self):
        config = self.base_config(telegram=False, xui=True)
        config["plan_routes"][0]["plan"] = "missing-plan"

        with self.assertRaises(CommandError):
            self.call_bootstrap(config, "--dry-run")

    def test_revenue_dry_run_false_fails(self):
        config = self.base_config(telegram=False, xui=False)
        config["revenue_engine"]["dry_run"] = False

        with self.assertRaises(CommandError):
            self.call_bootstrap(config, "--dry-run")

    def test_secrets_do_not_appear_in_stdout_or_stderr(self):
        config = self.base_config(telegram=True, xui=True)

        out, err = self.call_bootstrap(config, "--yes")

        output = out + err
        self.assertNotIn(self.admin_password, output)
        self.assertNotIn(self.bot_token, output)
        self.assertNotIn(self.panel_password, output)
        self.assertNotIn(config["store"]["card_number"], output)

    def test_no_update_existing_skips_existing_records(self):
        config = self.base_config(telegram=False, xui=False)
        self.call_bootstrap(config, "--yes")
        config["store"]["name"] = "Changed Name"

        out, _err = self.call_bootstrap(config, "--yes", "--no-update-existing")

        self.assertEqual(Store.objects.count(), 1)
        self.assertEqual(Store.objects.get(slug="bootstrap-store").name, "Bootstrap Store")
        self.assertIn('"action": "skip"', out)

    @patch("store.productization.bootstrap.BootstrapInstaller._live_check_telegram")
    def test_live_check_runs_only_when_flag_is_explicit(self, live_check_mock):
        config = self.base_config(telegram=True, xui=False)

        out, _err = self.call_bootstrap(config, "--yes", "--live-check")

        live_check_mock.assert_called_once()
        self.assertIn("live_checks_run=yes", out)

    def test_transaction_rolls_back_on_bootstrap_error(self):
        from .productization.bootstrap import BootstrapInstallError

        config = self.base_config(telegram=False, xui=True)

        def fail_only_real(installer):
            if installer.dry_run:
                return None
            raise BootstrapInstallError("forced bootstrap failure")

        with patch("store.productization.bootstrap.BootstrapInstaller._bootstrap_panel", fail_only_real):
            with self.assertRaises(CommandError):
                self.call_bootstrap(config, "--yes")

        self.assertEqual(Store.objects.count(), 0)
        self.assertEqual(get_user_model().objects.filter(username="bootstrap-admin").count(), 0)
