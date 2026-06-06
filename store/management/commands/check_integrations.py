from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import get_commands
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from store.bot_targets import customer_has_telegram_target, get_vpn_client_telegram_targets
from store.models import BotConfiguration, FreeTrialRequest, Inbound, Panel, Plan, Store
from store.renewal_reminder_services import get_active_clients_for_reminders, normalize_reminder_days


@dataclass
class CheckItem:
    level: str
    subject: str
    message: str


class Command(BaseCommand):
    help = "Check deployment and integration settings for staging/production."

    LEVEL_OK = "OK"
    LEVEL_WARNING = "WARNING"
    LEVEL_ERROR = "ERROR"

    def add_arguments(self, parser):
        parser.add_argument(
            "--live-bot",
            action="store_true",
            help="Call getMe for active bot configurations with tokens.",
        )
        parser.add_argument(
            "--live-xui",
            action="store_true",
            help="Try logging in to active X-UI panels.",
        )
        parser.add_argument(
            "--send-telegram-test-message",
            action="store_true",
            help="Send a test message to the first configured Telegram admin, or --telegram-chat-id.",
        )
        parser.add_argument(
            "--telegram-chat-id",
            help="Override Telegram chat ID for --send-telegram-test-message.",
        )
        parser.add_argument(
            "--no-fail",
            action="store_true",
            help="Print ERROR items but exit with status 0.",
        )

    def handle(self, *args, **options):
        self.items = []
        self.live_bot = bool(options["live_bot"])
        self.live_xui = bool(options["live_xui"])
        self.send_telegram_test_message = bool(options["send_telegram_test_message"])
        self.telegram_chat_id = (options.get("telegram_chat_id") or "").strip()

        self.check_deployment_settings()
        self.check_stores()
        self.check_bot_configurations()
        self.check_panels()
        self.check_inbounds()
        self.check_sms_webhook()
        self.check_static_media_settings()

        counts = {level: 0 for level in (self.LEVEL_OK, self.LEVEL_WARNING, self.LEVEL_ERROR)}
        for item in self.items:
            counts[item.level] += 1
            self.stdout.write(self.format_item(item))

        self.stdout.write(
            f"Summary: OK={counts[self.LEVEL_OK]} "
            f"WARNING={counts[self.LEVEL_WARNING]} ERROR={counts[self.LEVEL_ERROR]}"
        )

        if counts[self.LEVEL_ERROR] and not options["no_fail"]:
            raise CommandError(f"Integration checks failed with {counts[self.LEVEL_ERROR]} error(s).")

    def add_item(self, level, subject, message):
        self.items.append(CheckItem(level, subject, message))

    def ok(self, subject, message):
        self.add_item(self.LEVEL_OK, subject, message)

    def warning(self, subject, message):
        self.add_item(self.LEVEL_WARNING, subject, message)

    def error(self, subject, message):
        self.add_item(self.LEVEL_ERROR, subject, message)

    def format_item(self, item):
        text = f"[{item.level}] {item.subject}: {item.message}"
        if item.level == self.LEVEL_OK:
            return self.style.SUCCESS(text)
        if item.level == self.LEVEL_WARNING:
            return self.style.WARNING(text)
        return self.style.ERROR(text)

    def check_deployment_settings(self):
        secret_key = (getattr(settings, "SECRET_KEY", "") or "").strip()
        if not secret_key:
            self.error("Django settings", "SECRET_KEY is missing.")
        elif secret_key in {"change-me", "changeme"} or secret_key.startswith("django-insecure-"):
            self.warning("Django settings", "SECRET_KEY looks like a development/default value.")
        else:
            self.ok("Django settings", "SECRET_KEY is configured.")

        if getattr(settings, "DEBUG", False):
            self.warning("Django settings", "DEBUG is enabled; keep it False for production.")
        else:
            self.ok("Django settings", "DEBUG is disabled.")

        allowed_hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        if not allowed_hosts:
            self.error("Django settings", "ALLOWED_HOSTS is empty.")
        elif "*" in allowed_hosts:
            self.warning("Django settings", "ALLOWED_HOSTS contains '*'.")
        else:
            self.ok("Django settings", f"ALLOWED_HOSTS has {len(allowed_hosts)} value(s).")

        csrf_origins = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or [])
        if getattr(settings, "DEBUG", False) or csrf_origins:
            self.ok("Django settings", f"CSRF_TRUSTED_ORIGINS has {len(csrf_origins)} value(s).")
        else:
            self.warning("Django settings", "CSRF_TRUSTED_ORIGINS is empty.")

        database = (getattr(settings, "DATABASES", {}) or {}).get("default", {})
        engine = database.get("ENGINE", "")
        if engine.endswith("sqlite3"):
            self.warning("Database", "SQLite is configured; use PostgreSQL for real production traffic.")
        elif engine:
            self.ok("Database", f"Database backend is {engine}.")
        else:
            self.error("Database", "Default database backend is not configured.")

    def check_stores(self):
        stores = list(Store.objects.filter(is_active=True).order_by("pk"))
        if not stores:
            self.error("Store", "No active Store exists.")
            return

        self.ok("Store", f"{len(stores)} active store(s) found.")
        for store in stores:
            subject = f"Store #{store.pk} {store.name}"
            if not (store.name or "").strip():
                self.error(subject, "Store name is missing.")
            if not (store.card_number or "").strip():
                self.error(subject, "Card number is missing.")
            elif not str(store.card_number).isdigit() or len(str(store.card_number)) != 16:
                self.warning(subject, "Card number should be exactly 16 digits.")
            else:
                self.ok(subject, "Payment card number is configured.")
            if not (store.card_owner or "").strip():
                self.error(subject, "Card owner is missing.")
            else:
                self.ok(subject, "Payment card owner is configured.")

            public_plans = Plan.objects.filter(store=store, is_active=True, is_public=True, is_custom_volume=False)
            if public_plans.exists() or getattr(store, "custom_volume_price_per_gb", 0):
                self.ok(subject, "At least one public plan or custom-volume offer is available.")
            else:
                self.warning(subject, "No active public plan or custom-volume offer is available.")

            if store.sales_mode == Store.SalesMode.OPERATOR_BASED and not store.operators.filter(is_active=True).exists():
                self.error(subject, "Operator-based sales is enabled but no active operator exists.")
            self.check_free_trial(store, subject)
            self.check_renewal_reminders(store, subject)

    def check_free_trial(self, store, subject):
        if not store.free_trial_enabled:
            self.ok(subject, "Free Trial is disabled.")
            return

        panel = store.free_trial_panel
        inbound = store.free_trial_inbound
        has_error = False

        if not panel:
            self.error(subject, "Free Trial is enabled but panel is missing.")
            has_error = True
        elif not panel.is_active:
            self.error(subject, "Free Trial panel is inactive.")
            has_error = True
        elif panel.store_id and panel.store_id != store.pk:
            self.error(subject, "Free Trial panel belongs to a different store.")
            has_error = True

        if not inbound:
            self.error(subject, "Free Trial is enabled but inbound is missing.")
            has_error = True
        elif not inbound.is_active:
            self.error(subject, "Free Trial inbound is inactive.")
            has_error = True
        elif panel and inbound.panel_id != panel.pk:
            self.error(subject, "Free Trial inbound does not belong to the selected panel.")
            has_error = True
        elif inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
            self.warning(subject, "Free Trial inbound capacity is full.")

        try:
            traffic_gb = float(store.free_trial_traffic_gb or 0)
        except (TypeError, ValueError):
            traffic_gb = 0
        if traffic_gb <= 0:
            self.error(subject, "Free Trial traffic must be positive.")
            has_error = True

        if int(store.free_trial_duration_hours or 0) <= 0:
            self.error(subject, "Free Trial duration must be positive.")
            has_error = True
        if int(store.free_trial_cooldown_days or 0) <= 0:
            self.error(subject, "Free Trial cooldown must be positive.")
            has_error = True

        if not has_error:
            self.ok(subject, "Free Trial settings look complete.")

    def check_renewal_reminders(self, store, subject):
        reminders_enabled = bool(store.renewal_reminders_enabled or store.low_traffic_reminders_enabled)
        if not reminders_enabled:
            self.ok(subject, "Renewal reminders are disabled.")
            return

        has_error = False
        for field_name, default in (
            ("reminder_days_before_expiry", (3, 1, 0)),
            ("reminder_days_after_expiry", (1, 3)),
        ):
            try:
                normalize_reminder_days(getattr(store, field_name), default=default)
            except ValueError as exc:
                self.error(subject, f"{field_name} is invalid: {exc}")
                has_error = True

        try:
            percent = int(store.low_traffic_percent_threshold)
        except (TypeError, ValueError):
            percent = 0
        if not 1 <= percent <= 100:
            self.error(subject, "low_traffic_percent_threshold must be between 1 and 100.")
            has_error = True

        try:
            gb_threshold = Decimal(str(store.low_traffic_gb_threshold))
        except (InvalidOperation, TypeError, ValueError):
            gb_threshold = Decimal("0")
        if gb_threshold <= 0:
            self.error(subject, "low_traffic_gb_threshold must be positive.")
            has_error = True

        if int(store.reminder_cooldown_hours or 0) <= 0:
            self.error(subject, "reminder_cooldown_hours must be positive.")
            has_error = True
        if int(store.reminder_max_per_client_per_day or 0) <= 0:
            self.error(subject, "reminder_max_per_client_per_day must be positive.")
            has_error = True

        if not has_error:
            self.ok(subject, "Renewal reminder settings look valid.")

        if "send_renewal_reminders" in get_commands():
            self.ok(subject, "send_renewal_reminders command is available.")
        else:
            self.error(subject, "send_renewal_reminders command is missing.")
            has_error = True

        configs = (
            BotConfiguration.objects.filter(
                provider=BotConfiguration.Provider.TELEGRAM,
                is_active=True,
            )
            .filter(Q(store=store) | Q(store__isnull=True))
            .order_by("pk")
        )
        if not configs.exists():
            self.warning(subject, "Renewal reminders are enabled but no active Telegram BotConfiguration is available.")
        for config in configs:
            config_subject = f"BotConfiguration #{config.pk} {config.provider}/{config.name}"
            if not (config.bot_token or "").strip():
                self.warning(config_subject, "Renewal reminders need a bot token to send customer messages.")
            if not config.get_admin_user_ids():
                self.warning(config_subject, "Admin IDs are missing; reminder delivery can continue, but bot setup is incomplete.")

        start_at = getattr(store, "renewal_reminders_start_at", None)
        start_label = start_at.isoformat() if start_at else "not set"
        self.ok(subject, f"renewal_reminders_start_at={start_label}")

        reminder_clients = list(get_active_clients_for_reminders(store=store))
        if start_at:
            candidates_after_start_at = [
                client
                for client in reminder_clients
                if getattr(client, "created_at", None) and client.created_at >= start_at
            ]
        else:
            candidates_after_start_at = reminder_clients
        ignored_before_start_at = len(reminder_clients) - len(candidates_after_start_at)

        clients_with_target_after_start_at = 0
        customer_ids_with_target = set()
        for client in candidates_after_start_at:
            targets = get_vpn_client_telegram_targets(client, store=store)
            if targets:
                clients_with_target_after_start_at += 1

            customer = self.reminder_client_customer(client)
            if customer and customer.pk not in customer_ids_with_target and customer_has_telegram_target(customer, store=store):
                customer_ids_with_target.add(customer.pk)

        self.ok(
            subject,
            (
                f"total reminder clients={len(reminder_clients)} "
                f"ignored_before_start_at={ignored_before_start_at} "
                f"candidates_after_start_at={len(candidates_after_start_at)}"
            ),
        )
        self.ok(
            subject,
            (
                f"customers_with_telegram_target={len(customer_ids_with_target)} "
                f"clients_with_telegram_target_after_start_at={clients_with_target_after_start_at}"
            ),
        )
        if start_at and ignored_before_start_at:
            self.ok(subject, f"{ignored_before_start_at} old reminder client(s) are ignored by renewal_reminders_start_at.")

        missing_after_start_at = len(candidates_after_start_at) - clients_with_target_after_start_at
        if missing_after_start_at:
            self.warning(
                subject,
                f"{missing_after_start_at} reminder candidate(s) after renewal_reminders_start_at have no Telegram target and will be skipped.",
            )
        else:
            self.ok(subject, "Reminder candidates after renewal_reminders_start_at have Telegram targets or there are no candidates.")

        if not start_at and missing_after_start_at:
            self.warning(
                subject,
                "برای جلوگیری از پردازش دیتای قدیمی، renewal_reminders_start_at را تنظیم کنید.",
            )

    def reminder_client_customer(self, client):
        order = getattr(client, "order", None)
        if order and getattr(order, "customer", None):
            return order.customer
        trial_request = (
            FreeTrialRequest.objects.select_related("customer")
            .filter(vpn_client=client, customer__isnull=False)
            .order_by("-created_at", "-pk")
            .first()
        )
        return getattr(trial_request, "customer", None)

    def check_bot_configurations(self):
        configs = list(BotConfiguration.objects.filter(is_active=True).order_by("provider", "pk"))
        if not configs:
            self.warning("BotConfiguration", "No active bot configuration exists.")
            return

        self.ok("BotConfiguration", f"{len(configs)} active bot configuration(s) found.")
        for config in configs:
            subject = f"BotConfiguration #{config.pk} {config.provider}/{config.name}"
            if not (config.bot_token or "").strip():
                self.error(subject, "Bot token is missing.")
            else:
                self.ok(subject, "Bot token is configured.")
                if config.provider == BotConfiguration.Provider.TELEGRAM and ":" not in config.bot_token:
                    self.warning(subject, "Telegram token does not look like the usual '<id>:<secret>' format.")
                if self.live_bot:
                    self.check_live_bot(config, subject)

            admin_ids = config.get_admin_user_ids()
            if admin_ids:
                self.ok(subject, f"{len(admin_ids)} admin id(s) configured.")
                if self.send_telegram_test_message and config.provider == BotConfiguration.Provider.TELEGRAM:
                    self.send_test_telegram_message(config, subject, admin_ids)
            else:
                self.error(subject, "Admin user IDs are missing.")

            if config.provider == BotConfiguration.Provider.TELEGRAM:
                bot_username = (
                    getattr(settings, "TELEGRAM_BOT_USERNAME", "")
                    or getattr(settings, "BOT_USERNAME", "")
                    or ""
                ).strip()
                if bot_username:
                    self.ok(subject, f"Telegram bot username is configured: @{bot_username.lstrip('@')}.")
                else:
                    self.warning(subject, "TELEGRAM_BOT_USERNAME is not configured; referral links may be empty.")

            if config.force_telegram_channel_join:
                if config.provider != BotConfiguration.Provider.TELEGRAM:
                    self.warning(subject, "Force join is only enforced for Telegram bots.")
                self.check_force_join(config, subject)

    def check_live_bot(self, config, subject):
        try:
            from store.bots import BotClient

            payload = BotClient(config).get_me()
        except Exception as exc:
            self.error(subject, f"Bot getMe failed: {exc}")
            return

        result = payload.get("result") if isinstance(payload, dict) else {}
        username = (result or {}).get("username") or (result or {}).get("first_name") or "-"
        self.ok(subject, f"Bot getMe succeeded ({username}).")

    def send_test_telegram_message(self, config, subject, admin_ids):
        if not (config.bot_token or "").strip():
            self.error(subject, "Telegram test message was skipped because bot token is missing.")
            return

        chat_id = self.telegram_chat_id or admin_ids[0]
        try:
            from store.bots import BotClient

            BotClient(config).send_message("Integration check test message.", chat_id=chat_id)
        except Exception as exc:
            self.error(subject, f"Telegram test message failed chat_id={chat_id}: {exc}")
            return

        self.ok(subject, f"Telegram test message sent chat_id={chat_id}.")

    def check_force_join(self, config, subject):
        channel_id = (config.telegram_required_channel_id or "").strip()
        username = (config.telegram_required_channel_username or "").strip()
        invite_link = (config.telegram_required_channel_invite_link or "").strip()
        if channel_id or username:
            self.ok(subject, "Force join channel identifier is configured.")
        else:
            self.error(subject, "Force join is enabled but channel id/username is missing.")

        if invite_link or username:
            self.ok(subject, "Force join join URL is available.")
        else:
            self.warning(subject, "Force join has only a numeric channel id; set an invite link for users.")

    def check_panels(self):
        panels = list(Panel.objects.filter(is_active=True).order_by("pk"))
        if not panels:
            self.error("Panel", "No active X-UI panel exists.")
            return

        self.ok("Panel", f"{len(panels)} active X-UI panel(s) found.")
        for panel in panels:
            subject = f"Panel #{panel.pk} {panel.name}"
            if not (panel.name or "").strip():
                self.error(subject, "Panel name is missing.")
            if self.valid_url(panel.url):
                self.ok(subject, "Panel URL is configured.")
            else:
                self.error(subject, "Panel URL is missing or invalid.")
            if (panel.username or "").strip() and (panel.password or "").strip():
                self.ok(subject, "Panel username/password are configured.")
                if self.live_xui:
                    self.check_live_panel(panel, subject)
            else:
                self.error(subject, "Panel username/password are missing.")

    def check_live_panel(self, panel, subject):
        try:
            from store.xui_api import login_to_panel

            session = login_to_panel(panel)
        except Exception as exc:
            self.error(subject, f"X-UI login failed: {exc}")
            return

        if session:
            self.ok(subject, "X-UI login succeeded.")
        else:
            self.error(subject, "X-UI login failed.")

    def check_inbounds(self):
        inbounds = list(Inbound.objects.filter(is_active=True).select_related("panel").order_by("pk"))
        if not inbounds:
            self.error("Inbound", "No active inbound exists.")
            return

        self.ok("Inbound", f"{len(inbounds)} active inbound(s) found.")
        for inbound in inbounds:
            subject = f"Inbound #{inbound.pk} panel={inbound.panel_id} inbound_id={inbound.inbound_id}"
            if not inbound.panel_id:
                self.error(subject, "Inbound is not linked to a panel.")
                continue
            if not inbound.panel.is_active:
                self.error(subject, "Inbound is linked to an inactive panel.")
            else:
                self.ok(subject, "Inbound is linked to an active panel.")
            if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
                self.warning(subject, "Inbound capacity is full.")

    def check_sms_webhook(self):
        token = (getattr(settings, "SMSFORWARDER_WEBHOOK_TOKEN", "") or "").strip()
        if token:
            self.ok("SMS webhook", "SMSFORWARDER_WEBHOOK_TOKEN is configured.")
        else:
            self.error("SMS webhook", "SMSFORWARDER_WEBHOOK_TOKEN is missing; webhook returns 503.")

    def check_static_media_settings(self):
        static_root = getattr(settings, "STATIC_ROOT", "")
        media_root = getattr(settings, "MEDIA_ROOT", "")
        if static_root:
            self.ok("Static files", f"STATIC_ROOT={static_root}")
        else:
            self.warning("Static files", "STATIC_ROOT is not configured.")
        if media_root:
            self.ok("Media files", f"MEDIA_ROOT={media_root}")
        else:
            self.error("Media files", "MEDIA_ROOT is not configured.")

    def valid_url(self, value):
        parsed = urlsplit(str(value or ""))
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
