import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from store.models import Inbound, Panel
from store.xui_api import XUIError, XUIService, find_client_by_identifier


class InteractiveLookupLatencyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.panel = Panel.objects.create(
            name="Offline panel", url="https://offline.example", username="admin", password="test-only"
        )
        for number in range(1, 7):
            Inbound.objects.create(
                panel=self.panel, inbound_id=number, server_ip="127.0.0.1", port="443", is_active=True
            )

    def test_offline_panel_is_contacted_once_and_subsequent_search_backs_off(self):
        session = Mock()
        session.post.side_effect = requests.ConnectTimeout("unreachable")
        with patch("store.xui_api.requests.Session", return_value=session):
            with self.assertRaises(requests.ConnectTimeout):
                find_client_by_identifier(self.panel, "test-identifier")
            with self.assertRaises(XUIError) as failure:
                find_client_by_identifier(self.panel, "another-identifier")
        self.assertEqual(failure.exception.category, "network_timeout")
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs["timeout"], (3, 3))

    def test_changed_endpoint_does_not_inherit_previous_network_backoff(self):
        session = Mock()
        session.post.side_effect = requests.ConnectTimeout("unreachable")
        with patch("store.xui_api.requests.Session", return_value=session):
            with self.assertRaises(requests.ConnectTimeout):
                find_client_by_identifier(self.panel, "test-identifier")
            self.panel.url = "https://replacement.example"
            with self.assertRaises(requests.ConnectTimeout):
                find_client_by_identifier(self.panel, "test-identifier")
        self.assertEqual(session.post.call_count, 2)

    def test_inbound_specific_error_still_searches_other_inbounds(self):
        service = XUIService(self.panel)
        payload = {
            "id": 2,
            "settings": '{"clients":[{"id":"test-identifier","email":"test"}]}',
            "clientStats": [{"email": "test", "up": 1, "down": 2}],
        }
        service.get_inbound = Mock(side_effect=[XUIError("Inbound not found"), payload])
        service.get_client_traffic = Mock(side_effect=AssertionError("Unexpected network fallback"))
        result = service.find_client_by_identifier("test-identifier")
        self.assertEqual(result["inbound_id"], 2)
        self.assertEqual(service.get_inbound.call_count, 2)

    def test_unreachable_panel_does_not_hide_result_from_next_panel(self):
        from store.config_lookup import lookup_client_across_panels

        other = Panel.objects.create(name="Working panel", url="https://working.example")
        result = {"panel": other, "identifier": "test-identifier", "is_enabled": True}
        with patch.object(XUIService, "find_client_by_identifier", side_effect=[requests.ConnectTimeout(), result]) as finder:
            response = lookup_client_across_panels("test-identifier")
        self.assertTrue(response["found"])
        self.assertEqual(response["panel_id"], other.pk)
        self.assertEqual(finder.call_count, 2)
        self.assertEqual(len(response["panel_errors"]), 1)

    def test_regular_service_keeps_existing_retries_and_timeouts(self):
        service = XUIService(self.panel)
        operation = Mock(side_effect=[requests.ConnectTimeout(), "recovered"])
        with patch("store.xui_api.time.sleep"):
            self.assertEqual(service._login_request_with_retries(operation), "recovered")
        self.assertEqual(operation.call_count, 2)
        self.assertEqual(service.login_timeout, (5, 30))
        self.assertEqual(service.timeout_seconds, 30)


class PollingTimingTests(SimpleTestCase):
    def test_timing_distinguishes_queue_lag_from_handler_without_logging_contents(self):
        from store.management.commands.run_telegram_polling import TelegramPollingWorker

        logs = []
        worker = TelegramPollingWorker(
            SimpleNamespace(_log=lambda message, *args: logs.append(message)),
            7,
            threading.Event(),
            {"timeout": 25, "limit": 100, "idle_sleep": 1, "retry_sleep": 10, "skip_delete_webhook": True},
        )
        worker.config = SimpleNamespace(provider="telegram", webhook_secret="test-secret")
        update = {"update_id": 123, "message": {"date": 998, "text": "private-message-content"}}
        with (
            patch("store.management.commands.run_telegram_polling.handle_bot_update"),
            patch("store.management.commands.run_telegram_polling.time.monotonic", side_effect=[10, 12.5]),
            patch("store.management.commands.run_telegram_polling.time.time", return_value=1000.0),
        ):
            worker._handle_update(update)
        self.assertEqual(worker.offset, 124)
        self.assertIn("handler_ms=2500.0", logs[0])
        self.assertIn("delivery_lag_ms=2000.0", logs[0])
        self.assertNotIn("private-message-content", " ".join(logs))
        self.assertNotIn("test-secret", " ".join(logs))
