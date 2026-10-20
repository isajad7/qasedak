from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from store.bot_proxy import bot_request_kwargs, effective_telegram_proxy_url
from store.models import BotConfiguration, Store, TelegramProxyConfiguration
from store.telegram_bot.client import BotClient


@override_settings(TELEGRAM_PROXY_URL="http://server.example:8080", TELEGRAM_API_IP="")
class TelegramProxyAdminTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="First", english_name="First", card_number="0000000000000000", card_owner="Owner"
        )
        self.other_store = Store.objects.create(
            name="Second", english_name="Second", card_number="0000000000000000", card_owner="Owner"
        )
        self.bot = BotConfiguration.objects.create(
            name="First bot", store=self.store, provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="test-bot-token",
        )
        self.other_bot = BotConfiguration.objects.create(
            name="Second bot", store=self.other_store, provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="other-test-token",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="proxy-admin", email="proxy@example.com", password="secret"
        )

    def create_proxy(self, **overrides):
        values = {
            "bot_config": self.bot, "mode": TelegramProxyConfiguration.Mode.CUSTOM,
            "protocol": "http", "host": "first.example", "port": 7880,
            "username": "proxy-user", "password": "proxy-secret",
        }
        values.update(overrides)
        return TelegramProxyConfiguration.objects.create(**values)

    def test_bot_specific_proxy_and_live_edits(self):
        proxy = self.create_proxy()
        expected = "http://proxy-user:proxy-secret@first.example:7880"
        self.assertEqual(effective_telegram_proxy_url(self.bot), expected)
        self.assertEqual(effective_telegram_proxy_url(self.other_bot), "http://server.example:8080")

        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {"id": 1}}
        with patch("store.telegram_bot.client.requests.post", return_value=response) as request:
            client = BotClient(self.bot)
            client.get_me()
            self.assertEqual(request.call_args.kwargs["proxies"]["https"], expected)
            proxy.host = "changed.example"
            proxy.save(update_fields=["host"])
            client.get_me()
            self.assertIn("changed.example:7880", request.call_args.kwargs["proxies"]["https"])

        proxy.mode = TelegramProxyConfiguration.Mode.DIRECT
        proxy.save(update_fields=["mode"])
        self.assertEqual(effective_telegram_proxy_url(self.bot), "")
        self.assertEqual(bot_request_kwargs(self.bot), {"proxies": {"http": "", "https": ""}})
        proxy.mode = TelegramProxyConfiguration.Mode.ENVIRONMENT
        proxy.save(update_fields=["mode"])
        self.assertEqual(effective_telegram_proxy_url(self.bot), "http://server.example:8080")
        proxy.delete()
        self.assertEqual(effective_telegram_proxy_url(self.bot), "http://server.example:8080")

    @override_settings(TELEGRAM_API_IP="149.154.166.110")
    def test_socks_proxy_uses_requests_even_with_pinned_api_ip(self):
        self.create_proxy(protocol="socks5")
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {"id": 1}}
        with patch("store.telegram_bot.client.requests.post", return_value=response) as request:
            BotClient(self.bot).get_me()
        self.assertEqual(request.call_args.kwargs["proxies"]["https"], "socks5://proxy-user:proxy-secret@first.example:7880")

    def test_admin_edit_keeps_password_when_blank_and_masks_status(self):
        proxy = self.create_proxy()
        self.client.force_login(self.admin)
        url = reverse("admin:store_telegramproxyconfiguration_change", args=[proxy.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "http://****@first.example:7880")
        self.assertNotContains(response, "proxy-secret")
        self.assertNotContains(response, "proxy-user")

        response = self.client.post(url, {
            "bot_config": self.bot.pk, "mode": "custom", "protocol": "http",
            "host": "changed.example", "port": "7880", "username": "proxy-user",
            "password": "", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        proxy.refresh_from_db()
        self.assertEqual(proxy.password, "proxy-secret")
        self.assertEqual(proxy.host, "changed.example")

    def test_test_button_uses_get_me_only_on_post_and_redacts_errors(self):
        proxy = self.create_proxy()
        url = reverse("admin:store_telegramproxyconfiguration_test", args=[proxy.pk])
        self.client.force_login(self.admin)
        with patch("store.telegram_bot.client.BotClient.call") as call:
            self.assertEqual(self.client.get(url).status_code, 200)
            call.assert_not_called()
            call.return_value = {"ok": True, "result": {"id": 1}}
            response = self.client.post(url)
            self.assertContains(response, "موفق بود")
            call.assert_called_once_with("getMe", {}, timeout=(3, 8))

        with patch("store.telegram_bot.client.BotClient.call", side_effect=RuntimeError("proxy-secret test-bot-token")):
            response = self.client.post(url)
            self.assertContains(response, "ناموفق بود")
            self.assertNotContains(response, "proxy-secret")
            self.assertNotContains(response, "test-bot-token")

    def test_test_button_requires_change_permission(self):
        proxy = self.create_proxy()
        url = reverse("admin:store_telegramproxyconfiguration_test", args=[proxy.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        staff = get_user_model().objects.create_user("readonly", password="secret", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post(url).status_code, 403)
