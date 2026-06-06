import logging
import signal
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from store.bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url
from store.bots import BotClient, BotDeliveryError, handle_bot_update
from store.models import BotConfiguration

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run Telegram bots with long polling instead of webhooks."

    def add_arguments(self, parser):
        parser.add_argument("--config-id", type=int, help="Only poll one BotConfiguration ID.")
        parser.add_argument("--timeout", type=int, default=25, help="Telegram getUpdates long-poll timeout in seconds.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum updates per getUpdates request.")
        parser.add_argument("--idle-sleep", type=float, default=1.0, help="Sleep between short or empty polling cycles.")
        parser.add_argument("--retry-sleep", type=float, default=10.0, help="Sleep after Telegram connection failures.")
        parser.add_argument("--discovery-interval", type=float, default=10.0, help="How often to discover newly added Telegram bot configs.")
        parser.add_argument("--startup-retries", type=int, default=3, help="Startup retry count for --once mode.")
        parser.add_argument("--once", action="store_true", help="Run one getUpdates request per config and exit.")
        parser.add_argument("--skip-delete-webhook", action="store_true", help="Do not call deleteWebhook before polling.")

    def handle(self, *args, **options):
        if options["timeout"] < 0:
            raise CommandError("--timeout must be zero or greater.")
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")
        if options["discovery_interval"] <= 0:
            raise CommandError("--discovery-interval must be greater than zero.")

        stop_event = threading.Event()
        self._install_signal_handlers(stop_event)

        config_ids = self._active_config_ids(options.get("config_id"))
        if options["once"]:
            if not config_ids:
                self._log("No active Telegram bot configurations found.")
                return
            for config_id in config_ids:
                worker = TelegramPollingWorker(self, config_id, stop_event, options)
                self._run_once_with_retries(worker, config_id, options)
            return

        self._run_discovery_loop(stop_event, options)

    def _run_discovery_loop(self, stop_event, options):
        workers = {}
        no_configs_logged = False

        while not stop_event.is_set():
            workers = {
                config_id: worker
                for config_id, worker in workers.items()
                if worker.is_alive()
            }

            config_ids = set(self._active_config_ids(options.get("config_id")))
            if not config_ids:
                if not no_configs_logged:
                    self._log("No active Telegram bot configurations found; waiting before retry.")
                    no_configs_logged = True
                stop_event.wait(options["discovery_interval"])
                continue
            no_configs_logged = False

            for config_id in sorted(config_ids):
                if config_id in workers:
                    continue
                self._log(f"Telegram polling worker discovered config={config_id}")
                worker = threading.Thread(
                    target=TelegramPollingWorker(self, config_id, stop_event, options).run,
                    name=f"telegram-polling-{config_id}",
                    daemon=True,
                )
                workers[config_id] = worker
                worker.start()

            stop_event.wait(options["discovery_interval"])

        for worker in workers.values():
            worker.join(timeout=5)

    def _active_config_ids(self, config_id=None):
        configs = BotConfiguration.objects.filter(
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        ).exclude(bot_token="")
        if config_id:
            configs = configs.filter(pk=config_id)
        return list(configs.values_list("pk", flat=True))

    def _install_signal_handlers(self, stop_event):
        def handler(signum, _frame):
            self._log(f"Stop signal received ({signum}); Telegram polling is shutting down.")
            stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, handler)

    def _run_once_with_retries(self, worker, config_id, options):
        attempts = max(1, int(options["startup_retries"]))
        for attempt in range(1, attempts + 1):
            try:
                worker.prepare()
                worker.poll_once()
                worker.confirm_processed_updates()
                return
            except Exception as exc:
                if attempt == attempts:
                    raise CommandError(f"Telegram polling failed config={config_id}: {exc}") from None
                self._log(
                    f"Telegram polling startup failed config={config_id} attempt={attempt}/{attempts}: {exc}",
                    logging.ERROR,
                )
                time.sleep(options["retry_sleep"])

    def _log(self, message, level=logging.INFO):
        logger.log(level, message)
        self.stdout.write(message)


class TelegramPollingWorker:
    allowed_updates = ["message", "callback_query"]

    def __init__(self, command, config_id, stop_event, options):
        self.command = command
        self.config_id = config_id
        self.stop_event = stop_event
        self.timeout = options["timeout"]
        self.limit = options["limit"]
        self.idle_sleep = options["idle_sleep"]
        self.retry_sleep = options["retry_sleep"]
        self.skip_delete_webhook = options["skip_delete_webhook"]
        self.offset = None
        self.config = None
        self.client = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.prepare()
                break
            except Exception as exc:
                self.command._log(
                    f"Telegram connection test failure config={self.config_id}: {exc}",
                    logging.ERROR,
                )
                self.stop_event.wait(self.retry_sleep)

        while not self.stop_event.is_set():
            try:
                count = self.poll_once()
                if not count and self.timeout == 0:
                    self.stop_event.wait(self.idle_sleep)
            except BotConfiguration.DoesNotExist:
                self.command._log(f"Telegram polling stopped; config={self.config_id} is no longer active.")
                return
            except Exception as exc:
                logger.exception("Telegram polling failed config=%s", self.config_id)
                self.command._log(f"Telegram polling failure config={self.config_id}: {exc}", logging.ERROR)
                self.stop_event.wait(self.retry_sleep)
            finally:
                close_old_connections()

    def prepare(self):
        close_old_connections()
        self.config = self._load_config()
        self.client = BotClient(self.config)

        proxy_url = telegram_proxy_url()
        if proxy_url:
            self.command._log(
                f"Telegram proxy enabled config={self.config.pk} proxy={sanitized_telegram_proxy_url(proxy_url)}"
            )
        else:
            self.command._log(f"Telegram proxy disabled config={self.config.pk}", logging.WARNING)

        me_payload = self.client.get_me()
        bot_info = me_payload.get("result") or {}
        username = bot_info.get("username") or bot_info.get("first_name") or self.config.name
        self.command._log(f"Telegram connection test success config={self.config.pk} bot={username}")

        if not self.skip_delete_webhook:
            self.client.delete_webhook(drop_pending_updates=False)
            self.command._log(f"Telegram webhook deleted config={self.config.pk} drop_pending_updates=false")

        self.command._log(f"Telegram polling started config={self.config.pk}")

    def poll_once(self):
        self.config = self._load_config()
        self.client = BotClient(self.config)
        payload = self.client.get_updates(
            offset=self.offset,
            timeout=self.timeout,
            limit=self.limit,
            allowed_updates=self.allowed_updates,
        )
        updates = payload.get("result") or []
        for update in updates:
            self._handle_update(update)
        return len(updates)

    def _handle_update(self, update):
        update_id = update.get("update_id") if isinstance(update, dict) else None
        try:
            handle_bot_update(
                self.config.provider,
                self.config.webhook_secret,
                update,
                source="polling",
            )
        except BotDeliveryError as exc:
            self.command._log(
                f"Telegram update handled with delivery failure config={self.config_id} update_id={update_id}: {exc}",
                logging.WARNING,
            )
        except Exception as exc:
            logger.exception("Telegram update handler failed config=%s update_id=%s", self.config_id, update_id)
            self.command._log(
                f"Telegram update handler failed config={self.config_id} update_id={update_id}: {exc}",
                logging.ERROR,
            )
        finally:
            if update_id is not None:
                next_offset = int(update_id) + 1
                self.offset = max(self.offset or next_offset, next_offset)

    def confirm_processed_updates(self):
        if self.offset is None:
            return
        self.client.get_updates(
            offset=self.offset,
            timeout=0,
            limit=1,
            allowed_updates=self.allowed_updates,
        )

    def _load_config(self):
        return BotConfiguration.objects.get(
            pk=self.config_id,
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        )
