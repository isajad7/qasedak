import logging
import socket
import time
from urllib.parse import urlsplit

import requests
from django.core.management.base import BaseCommand, CommandError

from store.bot_proxy import bot_request_proxies, sanitized_telegram_proxy_url, telegram_proxy_url
from store.models import BotConfiguration
from store.telegram_bot.client import BotClient, BotDeliveryError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Test Telegram proxy connectivity, getMe, and a short getUpdates call."

    def add_arguments(self, parser):
        parser.add_argument("--config-id", type=int, help="BotConfiguration ID to test.")
        parser.add_argument("--timeout", type=float, default=8.0, help="Network timeout in seconds.")
        parser.add_argument("--updates-timeout", type=int, default=2, help="Short getUpdates timeout in seconds.")
        parser.add_argument("--retries", type=int, default=3, help="Retry count for flaky proxy network checks.")
        parser.add_argument("--send-test-message", action="store_true", help="Send a test message to the admin chat.")
        parser.add_argument("--chat-id", help="Override chat ID for --send-test-message.")

    def handle(self, *args, **options):
        config = self._get_config(options.get("config_id"))
        proxy_url = telegram_proxy_url()
        if not proxy_url:
            raise CommandError("Telegram proxy is disabled; configure TELEGRAM_PROXY_* or TELEGRAM_PROXY_URL.")

        self._with_retries(
            lambda: self._check_proxy_socket(proxy_url, options["timeout"]),
            attempts=options["retries"],
            label="proxy socket connection",
        )
        self._with_retries(
            lambda: self._check_telegram_api(config, options["timeout"]),
            attempts=options["retries"],
            label="Telegram API connection through proxy",
        )

        client = BotClient(config)
        try:
            me_payload = self._with_retries(client.get_me, attempts=options["retries"], label="getMe through proxy")
        except BotDeliveryError as exc:
            raise CommandError(f"getMe failed through proxy: {exc}") from exc
        bot_info = me_payload.get("result") or {}
        self._success(f"getMe succeeded through proxy bot={bot_info.get('username') or bot_info.get('first_name') or config.name}")

        try:
            updates_payload = self._with_retries(
                lambda: client.get_updates(timeout=options["updates_timeout"], limit=1),
                attempts=options["retries"],
                label="getUpdates through proxy",
            )
        except BotDeliveryError as exc:
            raise CommandError(f"getUpdates failed through proxy: {exc}") from exc
        updates = updates_payload.get("result") or []
        self._success(f"getUpdates succeeded through proxy count={len(updates)} timeout={options['updates_timeout']}")

        if options["send_test_message"]:
            chat_id = options.get("chat_id") or config.admin_user_id
            try:
                self._with_retries(
                    lambda: client.send_message("Telegram proxy test message.", chat_id=chat_id),
                    attempts=options["retries"],
                    label="sendMessage through proxy",
                )
            except BotDeliveryError as exc:
                raise CommandError(f"sendMessage failed through proxy: {exc}") from exc
            self._success(f"sendMessage succeeded through proxy chat_id={chat_id}")

    def _get_config(self, config_id=None):
        configs = BotConfiguration.objects.filter(
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        ).exclude(bot_token="")
        if config_id:
            configs = configs.filter(pk=config_id)
        config = configs.order_by("pk").first()
        if not config:
            raise CommandError("No active Telegram bot configuration with a token was found.")
        return config

    def _check_proxy_socket(self, proxy_url, timeout):
        parsed = urlsplit(proxy_url)
        if not parsed.hostname or not parsed.port:
            raise CommandError("Telegram proxy URL must include host and port.")

        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout):
                pass
        except OSError as exc:
            raise CommandError(f"Proxy socket connection failed: {exc}") from exc
        self._success(f"Proxy socket connection succeeded proxy={sanitized_telegram_proxy_url(proxy_url)}")

    def _check_telegram_api(self, config, timeout):
        proxies = bot_request_proxies(config.provider)
        try:
            response = requests.get("https://api.telegram.org", timeout=timeout, proxies=proxies, allow_redirects=False)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Telegram API connection failed through proxy: {exc}") from exc
        self._success(f"Telegram API connection succeeded through proxy status={response.status_code}")

    def _success(self, message):
        logger.info(message)
        self.stdout.write(self.style.SUCCESS(message))

    def _with_retries(self, func, *, attempts, label):
        attempts = max(1, int(attempts))
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                return func()
            except (OSError, requests.RequestException, BotDeliveryError, CommandError) as exc:
                last_exc = exc
                if attempt == attempts:
                    break
                logger.warning("%s failed on attempt %s/%s: %s", label, attempt, attempts, exc)
                time.sleep(min(2, attempt))
        raise last_exc
