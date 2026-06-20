from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import get_commands
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from store.bot_targets import customer_has_telegram_target, get_vpn_client_telegram_targets
from store.configuration_services import (
    get_payment_sms_timezone_name_source,
    get_smsforwarder_webhook_token_status,
    get_telegram_bot_username_source,
)
from store.models import (
    BotConfiguration,
    DailyAdminReportLog,
    FreeTrialRequest,
    Inbound,
    LegacyWizWizImportJob,
    LegacyWizWizImportRow,
    Panel,
    PanelHealthCheckLog,
    PanelUsageSnapshot,
    Plan,
    PlanInboundRoute,
    RevenueOfferLog,
    Store,
)
from store.renewal_reminder_services import get_active_clients_for_reminders, normalize_reminder_days


SAFE_PLACEHOLDER_CARD_NUMBER = "0000000000000000"
SAFE_PLACEHOLDER_CARD_OWNER = "Configure Payment Owner"


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
        self.check_plan_inbound_routes()
        self.check_panel_monitoring()
        self.check_daily_admin_reports()
        self.check_revenue_engine()
        self.check_panel_usage_tracking()
        self.check_sms_webhook()
        self.check_legacy_wizwiz_imports()
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
            setup_incomplete = self.store_setup_incomplete(store)
            if not (store.name or "").strip():
                self.error(subject, "Store name is missing.")
            card_number = str(store.card_number or "").strip()
            if not card_number:
                if setup_incomplete:
                    self.warning(subject, "Card number is missing; complete payment setup in Django Admin.")
                else:
                    self.error(subject, "Card number is missing.")
            elif not card_number.isdigit() or len(card_number) != 16:
                self.warning(subject, "Card number should be exactly 16 digits.")
            elif card_number == SAFE_PLACEHOLDER_CARD_NUMBER:
                self.warning(subject, "Payment card number still uses the installer placeholder.")
            else:
                self.ok(subject, "Payment card number is configured.")

            card_owner = (store.card_owner or "").strip()
            if not card_owner:
                if setup_incomplete:
                    self.warning(subject, "Card owner is missing; complete payment setup in Django Admin.")
                else:
                    self.error(subject, "Card owner is missing.")
            elif card_owner == SAFE_PLACEHOLDER_CARD_OWNER:
                self.warning(subject, "Payment card owner still uses the installer placeholder.")
            else:
                self.ok(subject, "Payment card owner is configured.")

            self.check_payment_sms_timezone(store, subject)

            public_plans = Plan.objects.filter(store=store, is_active=True, is_public=True, is_custom_volume=False)
            if public_plans.exists() or getattr(store, "custom_volume_price_per_gb", 0):
                self.ok(subject, "At least one public plan or custom-volume offer is available.")
            else:
                self.warning(subject, "No active public plan or custom-volume offer is available.")

            if store.sales_mode == Store.SalesMode.OPERATOR_BASED and not store.operators.filter(is_active=True).exists():
                self.error(subject, "Operator-based sales is enabled but no active operator exists.")
            self.check_free_trial(store, subject)
            self.check_renewal_reminders(store, subject)

    def check_payment_sms_timezone(self, store, subject):
        timezone_source = get_payment_sms_timezone_name_source(store=store)
        if timezone_source.source == "store":
            self.ok(subject, f"Payment SMS time zone is configured in admin: {timezone_source.value}.")
        elif timezone_source.source == "settings":
            self.warning(subject, f"Payment SMS time zone uses legacy settings fallback: {timezone_source.value}.")
        else:
            self.warning(subject, f"Payment SMS time zone uses default fallback: {timezone_source.value}.")

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
        elif not getattr(inbound, "available_for_new_orders", True):
            self.error(subject, "Free Trial inbound is not available for new orders.")
            has_error = True
        elif panel and inbound.panel_id != panel.pk:
            self.error(subject, "Free Trial inbound does not belong to the selected panel.")
            has_error = True
        elif inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
            self.warning(subject, "Free Trial inbound capacity is full.")

        if inbound and not getattr(inbound, "health_monitor_enabled", True):
            self.warning(subject, "Free Trial inbound is excluded from panel health monitor.")
        if self.live_xui and panel and inbound and panel.is_active and inbound.is_active:
            remote_ok, message = self.check_live_inbound_exists(panel, inbound)
            if remote_ok:
                self.ok(subject, f"Free Trial inbound exists in X-UI: inbound_id={inbound.inbound_id}.")
            else:
                self.error(subject, f"Free Trial inbound is not readable from X-UI: {message}")
                has_error = True

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
                username_source = get_telegram_bot_username_source(bot_config=config)
                if username_source.source == "bot_configuration":
                    source_config = username_source.instance
                    source_label = f"BotConfiguration #{source_config.pk}" if source_config else "BotConfiguration"
                    self.ok(
                        subject,
                        f"Telegram bot username is configured in {source_label}: @{username_source.value}.",
                    )
                elif username_source.source == "settings":
                    self.warning(
                        subject,
                        (
                            "Telegram bot username uses legacy settings/env fallback; "
                            "move it to BotConfiguration.telegram_bot_username."
                        ),
                    )
                else:
                    self.warning(subject, "TELEGRAM_BOT_USERNAME is not configured; referral links may be empty.")

            if config.force_telegram_channel_join:
                if config.provider != BotConfiguration.Provider.TELEGRAM:
                    self.warning(subject, "Force join is only enforced for Telegram bots.")
                self.check_force_join(config, subject)

    def check_live_bot(self, config, subject):
        try:
            from store.telegram_bot.client import BotClient

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
            from store.telegram_bot.client import BotClient

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
            if self.global_setup_incomplete():
                self.warning("Panel", "Setup incomplete: no active X-UI panel exists yet.")
            elif self.has_any_active_sellable_plan():
                self.error("Panel", "No active X-UI panel exists for active sellable plans.")
            else:
                self.warning("Panel", "No active X-UI panel exists.")
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

    def sanitize_panel_error(self, exc, panel):
        text = str(exc or "")
        for secret in (
            getattr(panel, "password", ""),
            getattr(panel, "username", ""),
            getattr(panel, "url", ""),
            getattr(panel, "proxy_url", ""),
        ):
            secret = str(secret or "").strip()
            if secret:
                text = text.replace(secret, "<redacted>")
        return text or exc.__class__.__name__

    def check_live_inbound_exists(self, panel, inbound):
        try:
            from store.xui_api import XUIService

            XUIService(panel).get_inbound(inbound.inbound_id, use_cache=False)
        except Exception as exc:
            return False, self.sanitize_panel_error(exc, panel)
        return True, ""

    def check_inbounds(self):
        inbounds = list(Inbound.objects.filter(is_active=True).select_related("panel").order_by("pk"))
        if not inbounds:
            if self.global_setup_incomplete():
                self.warning("Inbound", "Setup incomplete: no active inbound exists yet.")
            elif self.has_any_active_sellable_plan():
                self.error("Inbound", "No active inbound exists for active sellable plans.")
            else:
                self.warning("Inbound", "No active inbound exists.")
            return

        self.ok("Inbound", f"{len(inbounds)} active inbound(s) found.")
        legacy_inbounds = [
            inbound
            for inbound in inbounds
            if not inbound.available_for_new_orders and not inbound.health_monitor_enabled
        ]
        if legacy_inbounds:
            self.ok("Inbound", f"{len(legacy_inbounds)} legacy inbound(s) ignored from health monitor and new orders.")

        locally_available_for_sales = 0
        live_available_for_sales = 0
        for inbound in inbounds:
            subject = f"Inbound #{inbound.pk} panel={inbound.panel_id} inbound_id={inbound.inbound_id}"
            panel_is_usable = False
            has_capacity = inbound.max_clients is None or inbound.current_users < inbound.max_clients
            if not inbound.panel_id:
                self.error(subject, "Inbound is not linked to a panel.")
                continue
            if not inbound.panel.is_active:
                self.error(subject, "Inbound is linked to an inactive panel.")
            else:
                self.ok(subject, "Inbound is linked to an active panel.")
                panel_is_usable = True
            if inbound.available_for_new_orders:
                if panel_is_usable and has_capacity:
                    locally_available_for_sales += 1
            else:
                self.ok(subject, "Inbound is excluded from new orders.")
            if inbound.health_monitor_enabled:
                self.ok(subject, "Inbound is included in panel health monitor.")
            elif inbound.available_for_new_orders:
                self.warning(subject, "Inbound is available for new orders but excluded from panel health monitor.")
            else:
                self.ok(subject, "Legacy inbound is excluded from panel health monitor.")
            if not has_capacity:
                self.warning(subject, "Inbound capacity is full.")
            if self.live_xui and panel_is_usable:
                remote_ok, message = self.check_live_inbound_exists(inbound.panel, inbound)
                if remote_ok:
                    self.ok(subject, "Inbound exists in X-UI.")
                    if inbound.available_for_new_orders and has_capacity:
                        live_available_for_sales += 1
                    continue
                if inbound.available_for_new_orders:
                    self.error(subject, f"Inbound is available for new orders but missing/unreadable in X-UI: {message}")
                elif inbound.health_monitor_enabled:
                    self.warning(subject, f"Inbound is monitored but missing/unreadable in X-UI: {message}")
                else:
                    self.ok(subject, "Legacy inbound is missing/unreadable in X-UI and ignored from health monitor.")

        if locally_available_for_sales:
            self.ok("Inbound", f"{locally_available_for_sales} inbound(s) are locally available for new orders.")
        elif self.has_any_active_sellable_plan():
            self.error("Inbound", "No active inbound is available for new orders.")
        else:
            self.warning("Inbound", "No active inbound is available for new orders.")
        if self.live_xui:
            if live_available_for_sales:
                self.ok("Inbound", f"{live_available_for_sales} inbound(s) available for new orders exist in X-UI.")
            else:
                self.error("Inbound", "No inbound available for new orders could be verified in X-UI.")

    def route_queryset_for_store(self, store):
        return PlanInboundRoute.objects.filter(
            Q(store=store) | Q(store__isnull=True),
            Q(inbound__panel__store=store) | Q(inbound__panel__store__isnull=True),
        )

    def active_plans_for_store(self, store):
        return Plan.objects.filter(
            Q(store=store) | Q(store__isnull=True),
            is_active=True,
        ).order_by("sort_order", "price", "pk")

    def route_is_valid_for_store(self, route, store, *, require_active_plan=True):
        inbound = route.inbound
        panel = getattr(inbound, "panel", None)
        if not route.is_active:
            return False, "Route is inactive."
        if require_active_plan and not route.plan.is_active:
            return False, "Route plan is inactive."
        if not inbound.is_active:
            return False, "Route inbound is inactive."
        if not inbound.available_for_new_orders:
            return False, "Route inbound is legacy/not available for new orders."
        if not panel:
            return False, "Route inbound is missing panel."
        if not panel.is_active:
            return False, "Route panel is inactive."
        if panel.store_id and panel.store_id != store.pk:
            return False, "Route inbound belongs to a different store."
        if route.store_id and route.store_id != store.pk:
            return False, "Route belongs to a different store."
        if route.operator_id:
            if not route.operator.is_active:
                return False, "Route operator is inactive."
            if not route.plan.operators.filter(pk=route.operator_id).exists():
                return False, "Route operator is not enabled on the plan."
        if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
            return False, "Route inbound capacity is full."
        return True, ""

    def plan_has_valid_general_route(self, plan, store):
        for route in self.route_queryset_for_store(store).filter(plan=plan, operator__isnull=True, is_active=True).select_related(
            "store",
            "plan",
            "operator",
            "inbound",
            "inbound__panel",
        ):
            valid, _message = self.route_is_valid_for_store(route, store)
            if valid:
                return True
        return False

    def plan_has_valid_operator_route(self, plan, operator, store):
        for route in self.route_queryset_for_store(store).filter(plan=plan, operator=operator, is_active=True).select_related(
            "store",
            "plan",
            "operator",
            "inbound",
            "inbound__panel",
        ):
            valid, _message = self.route_is_valid_for_store(route, store)
            if valid:
                return True
        return False

    def active_plan_operators(self, plan, store):
        return plan.operators.filter(
            Q(store=store) | Q(store__isnull=True),
            is_active=True,
        ).order_by("sort_order", "name", "pk")

    def check_plan_inbound_routes(self):
        stores = list(Store.objects.filter(is_active=True).order_by("pk"))
        if not stores:
            return

        for store in stores:
            subject = f"Plan inbound routing #{store.pk} {store.name}"
            if not getattr(store, "plan_inbound_routing_enabled", True):
                self.ok(subject, "Plan inbound routing is disabled; global inbound selection is used.")
                continue

            plans = list(self.active_plans_for_store(store))
            active_routes = list(
                self.route_queryset_for_store(store)
                .filter(is_active=True)
                .select_related("store", "plan", "operator", "inbound", "inbound__panel")
                .order_by("plan_id", "operator_id", "priority", "pk")
            )
            if not plans and self.store_setup_incomplete(store):
                self.warning(subject, "Setup incomplete: no active Panel and no active Plan exist yet.")
                self.ok(
                    subject,
                    (
                        f"route_summary active_plans=0 routed_plans=0 "
                        f"missing_route_plans=0 active_routes={len(active_routes)} invalid_routes=0"
                    ),
                )
                continue

            invalid_routes = []
            for route in active_routes:
                valid, message = self.route_is_valid_for_store(route, store, require_active_plan=False)
                if not valid:
                    invalid_routes.append((route, message))
                    self.error(
                        f"{subject} route #{route.pk}",
                        (
                            f"Invalid active route plan={route.plan_id} operator={route.operator_id or '-'} "
                            f"inbound={route.inbound_id}: {message}"
                        ),
                    )

            missing_routes = []
            routed_plan_ids = set()
            for plan in plans:
                has_general = self.plan_has_valid_general_route(plan, store)
                if has_general:
                    routed_plan_ids.add(plan.pk)

                if store.sales_mode == Store.SalesMode.OPERATOR_BASED:
                    operators = list(self.active_plan_operators(plan, store))
                    if not operators and not has_general:
                        missing_routes.append(f"plan #{plan.pk} {plan.name}")
                        continue
                    for operator in operators:
                        if has_general or self.plan_has_valid_operator_route(plan, operator, store):
                            routed_plan_ids.add(plan.pk)
                            continue
                        missing_routes.append(f"plan #{plan.pk} {plan.name} / operator #{operator.pk} {operator.name}")
                elif not has_general:
                    missing_routes.append(f"plan #{plan.pk} {plan.name}")

            fallback_enabled = bool(getattr(store, "allow_global_inbound_fallback", True))
            if missing_routes and not self.store_has_available_sales_inbound(store):
                self.error(
                    subject,
                    (
                        f"{len(missing_routes)} active plan/operator route(s) are missing "
                        "and no active sales inbound is available. Examples: "
                        + "; ".join(missing_routes[:5])
                    ),
                )
            elif missing_routes and fallback_enabled:
                self.warning(
                    subject,
                    (
                        f"{len(missing_routes)} active plan/operator route(s) are missing explicit routes "
                        "and will use global fallback. Examples: "
                        + "; ".join(missing_routes[:5])
                    ),
                )
            elif missing_routes:
                self.error(
                    subject,
                    (
                        f"{len(missing_routes)} active plan/operator route(s) are missing explicit routes "
                        "and fallback is disabled. Examples: "
                        + "; ".join(missing_routes[:5])
                    ),
                )
            else:
                self.ok(subject, "All active plans/operators have explicit routes.")

            self.ok(
                subject,
                (
                    f"route_summary active_plans={len(plans)} "
                    f"routed_plans={len(routed_plan_ids)} "
                    f"missing_route_plans={len(missing_routes)} "
                    f"active_routes={len(active_routes)} "
                    f"invalid_routes={len(invalid_routes)}"
                ),
            )

    def check_panel_monitoring(self):
        commands = get_commands()
        if "check_panel_health" in commands:
            self.ok("Panel monitor", "check_panel_health command is available.")
        else:
            self.error("Panel monitor", "check_panel_health command is missing.")

        stores = list(Store.objects.filter(is_active=True, panel_monitor_enabled=True).order_by("pk"))
        if not stores:
            self.ok("Panel monitor", "Panel monitor is disabled for all active stores.")
            return

        for store in stores:
            subject = f"Panel monitor #{store.pk} {store.name}"
            active_panels = Panel.objects.filter(store=store, is_active=True)
            active_panel_count = active_panels.count()
            if active_panel_count:
                self.ok(subject, f"{active_panel_count} active panel(s) will be monitored.")
            else:
                self.warning(subject, "Panel monitor is enabled but no active panel exists for this store.")

            latest_check = (
                PanelHealthCheckLog.objects.filter(panel__store=store)
                .select_related("panel")
                .order_by("-checked_at")
                .first()
            )
            if not latest_check:
                self.warning(subject, "No panel health check has been recorded yet.")
                continue

            self.ok(
                subject,
                (
                    f"Latest health check: panel={latest_check.panel.name} "
                    f"status={latest_check.status} at {latest_check.checked_at.isoformat()}."
                ),
            )
            cooldown_minutes = max(int(store.panel_monitor_alert_cooldown_minutes or 30), 1)
            recent_cutoff = timezone.now() - timedelta(minutes=max(cooldown_minutes * 2, 60))
            if latest_check.checked_at < recent_cutoff:
                self.warning(subject, "No recent panel health check was found; verify the timer/cron schedule.")
            else:
                self.ok(subject, "A recent panel health check exists.")

    def check_daily_admin_reports(self):
        commands = get_commands()
        if "send_daily_admin_report" in commands:
            self.ok("Daily admin report", "send_daily_admin_report command is available.")
        else:
            self.error("Daily admin report", "send_daily_admin_report command is missing.")

        stores = list(Store.objects.filter(is_active=True, daily_admin_report_enabled=True).order_by("pk"))
        if not stores:
            self.ok("Daily admin report", "Daily admin reports are disabled for all active stores.")
            return

        for store in stores:
            subject = f"Daily admin report #{store.pk} {store.name}"
            configs = (
                BotConfiguration.objects.filter(
                    provider=BotConfiguration.Provider.TELEGRAM,
                    is_active=True,
                )
                .filter(Q(store=store) | Q(store__isnull=True))
                .exclude(bot_token="")
                .order_by("pk")
            )
            admin_ids = []
            for config in configs:
                for admin_id in config.get_admin_user_ids():
                    if admin_id not in admin_ids:
                        admin_ids.append(admin_id)
            if admin_ids:
                self.ok(subject, f"{len(admin_ids)} Telegram admin id(s) are available for daily reports.")
            else:
                self.warning(subject, "Daily report is enabled but no Telegram admin IDs are available.")

            latest_report = DailyAdminReportLog.objects.filter(store=store).order_by("-report_date", "-created_at").first()
            if not latest_report:
                self.warning(subject, "No daily admin report has been sent yet.")
                continue
            self.ok(
                subject,
                (
                    f"Latest daily report: date={latest_report.report_date.isoformat()} "
                    f"status={latest_report.status} sent_to={latest_report.sent_to_count}."
                ),
            )

    def check_revenue_engine(self):
        commands = get_commands()
        if "run_revenue_scan" in commands:
            self.ok("Revenue Engine", "run_revenue_scan command is available.")
        else:
            self.error("Revenue Engine", "run_revenue_scan command is missing.")
        if "revenue_report" in commands:
            self.ok("Revenue Engine", "revenue_report command is available.")
        else:
            self.warning("Revenue Engine", "revenue_report command is missing.")

        stores = list(Store.objects.filter(is_active=True).order_by("pk"))
        if not stores:
            return
        recent_cutoff = timezone.now() - timedelta(days=7)
        for store in stores:
            subject = f"Revenue Engine #{store.pk} {store.name}"
            try:
                store.full_clean()
            except ValidationError as exc:
                self.error(subject, f"Revenue settings invalid: {exc.message_dict}")
            else:
                self.ok(subject, "Revenue Engine settings look valid.")

            if not getattr(store, "revenue_engine_enabled", True):
                self.warning(subject, "Revenue Engine is disabled.")
                continue
            if getattr(store, "revenue_engine_dry_run", False):
                self.warning(subject, "Revenue Engine dry_run is enabled; no offers will be sent.")

            latest_log = RevenueOfferLog.objects.filter(Q(store=store) | Q(store__isnull=True)).order_by("-created_at").first()
            if latest_log:
                self.ok(
                    subject,
                    f"Latest RevenueOfferLog: status={latest_log.status} engine={latest_log.engine_type} at {latest_log.created_at.isoformat()}.",
                )
            else:
                self.warning(subject, "No RevenueOfferLog exists yet.")

            recent_exists = RevenueOfferLog.objects.filter(
                Q(store=store) | Q(store__isnull=True),
                created_at__gte=recent_cutoff,
            ).exists()
            if not recent_exists:
                self.warning(subject, "No revenue offer log exists in the last 7 days while the engine is enabled.")

            max_per_day = int(getattr(store, "revenue_max_total_offers_per_day", 0) or 0)
            cooldown = int(getattr(store, "revenue_offer_cooldown_hours", 0) or 0)
            if max_per_day > 5000 and cooldown < 6:
                self.warning(subject, "Revenue offer limits look aggressive: high daily cap with low cooldown.")
            else:
                self.ok(subject, "Revenue offer rate limits look conservative.")

    def check_panel_usage_tracking(self):
        commands = get_commands()
        if "collect_panel_usage_snapshots" in commands:
            self.ok("Panel usage", "collect_panel_usage_snapshots command is available.")
        else:
            self.error("Panel usage", "collect_panel_usage_snapshots command is missing.")
        if "calculate_panel_daily_usage" in commands:
            self.ok("Panel usage", "calculate_panel_daily_usage command is available.")
        else:
            self.error("Panel usage", "calculate_panel_daily_usage command is missing.")

        stores = list(Store.objects.filter(is_active=True, panel_usage_tracking_enabled=True).order_by("pk"))
        if not stores:
            self.ok("Panel usage", "Panel usage tracking is disabled for all active stores.")
            return

        stale_cutoff = timezone.now() - timedelta(hours=3)
        for store in stores:
            subject = f"Panel usage #{store.pk} {store.name}"
            active_panel_count = Panel.objects.filter(store=store, is_active=True).count()
            if active_panel_count:
                self.ok(subject, f"{active_panel_count} active panel(s) will be snapshotted.")
            else:
                self.warning(subject, "Panel usage tracking is enabled but no active panel exists for this store.")

            retention_days = int(store.panel_usage_snapshot_retention_days or 0)
            if retention_days > 0:
                self.ok(subject, f"Snapshot retention is {retention_days} day(s).")
            else:
                self.error(subject, "panel_usage_snapshot_retention_days must be positive.")

            latest_snapshot = (
                PanelUsageSnapshot.objects.filter(panel__store=store)
                .select_related("panel")
                .order_by("-captured_at")
                .first()
            )
            if not latest_snapshot:
                self.warning(subject, "هنوز snapshot مصرف پنل ثبت نشده است.")
            else:
                self.ok(
                    subject,
                    (
                        f"Latest usage snapshot: panel={latest_snapshot.panel.name} "
                        f"status={latest_snapshot.status} at {latest_snapshot.captured_at.isoformat()}."
                    ),
                )
                if latest_snapshot.captured_at < stale_cutoff:
                    self.warning(subject, "Latest panel usage snapshot is older than 3 hours; verify the timer/cron schedule.")
                else:
                    self.ok(subject, "A recent panel usage snapshot exists.")

            if store.daily_admin_report_enabled and store.panel_usage_report_enabled:
                self.ok(subject, "Panel usage is enabled in the daily admin report.")
            elif store.daily_admin_report_enabled:
                self.warning(subject, "Daily admin report is enabled but panel usage report is disabled.")
            else:
                self.ok(subject, "Daily admin report is disabled; panel usage snapshots can still be collected.")

    def check_sms_webhook(self):
        token_status = get_smsforwarder_webhook_token_status()
        if token_status.has_db_token:
            store = token_status.store
            hint = store.smsforwarder_webhook_token_hint or "----"
            self.ok("SMS webhook", f"SMSForwarder webhook token is configured in Store #{store.pk}; hint ends in {hint}.")
        elif token_status.has_legacy_settings_token:
            self.warning(
                "SMS webhook",
                "SMSFORWARDER_WEBHOOK_TOKEN uses legacy settings/env fallback; move it to Store admin.",
            )
        else:
            if self.global_setup_incomplete():
                self.warning(
                    "SMS webhook",
                    "SMSForwarder webhook token is not configured yet; complete payment setup in Django Admin.",
                )
            else:
                self.error(
                    "SMS webhook",
                    "SMSFORWARDER_WEBHOOK_TOKEN is missing in DB/admin and legacy settings; webhook returns 503.",
                )

    def check_legacy_wizwiz_imports(self):
        latest_job = LegacyWizWizImportJob.objects.order_by("-created_at").first()
        if not latest_job:
            return

        subject = "Legacy WizWiz import"
        self.ok(
            subject,
            (
                f"Latest job #{latest_job.pk} status={latest_job.status} "
                f"valid={latest_job.valid_users_count} applied_at="
                f"{latest_job.applied_at.isoformat() if latest_job.applied_at else '-'}."
            ),
        )

        failed_jobs = LegacyWizWizImportJob.objects.filter(status=LegacyWizWizImportJob.Status.FAILED).count()
        if failed_jobs:
            self.warning(subject, f"{failed_jobs} failed legacy WizWiz import job(s) need review.")

        imported_customers = (
            LegacyWizWizImportRow.objects.filter(
                source="wizwiz",
                job__status=LegacyWizWizImportJob.Status.APPLIED,
                status__in=(
                    LegacyWizWizImportRow.Status.CREATED,
                    LegacyWizWizImportRow.Status.LINKED,
                    LegacyWizWizImportRow.Status.EXISTING,
                    LegacyWizWizImportRow.Status.UPDATED,
                ),
                customer__isnull=False,
            )
            .values("customer_id")
            .distinct()
            .count()
        )
        self.ok(subject, f"{imported_customers} imported legacy customer(s) are available for the WizWiz audience.")

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

    def has_any_active_sellable_plan(self):
        return (
            Plan.objects.filter(is_active=True, is_public=True, is_custom_volume=False).exists()
            or Store.objects.filter(is_active=True, custom_volume_price_per_gb__gt=0).exists()
        )

    def store_has_active_sellable_plan(self, store):
        if (
            Plan.objects.filter(
                Q(store=store) | Q(store__isnull=True),
                is_active=True,
                is_public=True,
                is_custom_volume=False,
            ).exists()
        ):
            return True
        try:
            custom_price = Decimal(str(getattr(store, "custom_volume_price_per_gb", 0) or 0))
        except (InvalidOperation, TypeError, ValueError):
            custom_price = Decimal("0")
        return custom_price > 0

    def store_has_active_panel(self, store):
        return Panel.objects.filter(Q(store=store) | Q(store__isnull=True), is_active=True).exists()

    def store_has_available_sales_inbound(self, store):
        return Inbound.objects.filter(
            Q(panel__store=store) | Q(panel__store__isnull=True),
            panel__is_active=True,
            is_active=True,
            available_for_new_orders=True,
        ).exists()

    def store_setup_incomplete(self, store):
        return not self.store_has_active_sellable_plan(store) and not self.store_has_active_panel(store)

    def global_setup_incomplete(self):
        return not self.has_any_active_sellable_plan() and not Panel.objects.filter(is_active=True).exists()
