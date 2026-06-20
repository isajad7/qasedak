import json
import os
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator, validate_email
from django.utils.text import slugify

from store.models import BotConfiguration, Inbound, Panel, Plan, PlanInboundRoute, Store


SAFE_PLACEHOLDER_CARD_NUMBER = "0000000000000000"
SAFE_PLACEHOLDER_CARD_OWNER = "Configure Payment Owner"
DEFAULT_STORE_SLUG = "default-store"
DEFAULT_PANEL_NAME = "Primary X-UI panel"
SUPPORTED_LANGUAGES = {"fa", "en"}
SECRET_KEYWORDS = ("secret", "token", "password", "key", "proxy_url", "card")


class BootstrapInstallError(Exception):
    """Safe validation/runtime error for install bootstrap failures."""

    def __init__(self, message, *, errors=None, warnings=None):
        self.message = str(message)
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])
        super().__init__(self.__str__())

    def __str__(self):
        if not self.errors:
            return self.message
        details = "; ".join(str(error) for error in self.errors)
        return f"{self.message}: {details}"


def load_install_config(path):
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise BootstrapInstallError(f"Install config file was not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapInstallError(
            f"Install config file is not valid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    except OSError as exc:
        raise BootstrapInstallError(f"Install config file could not be read: {config_path}") from exc

    if not isinstance(config, dict):
        raise BootstrapInstallError("Install config root must be a JSON object.")
    return config


def validate_install_config(config):
    errors = []
    warnings = []
    if not isinstance(config, dict):
        raise BootstrapInstallError("Install config root must be a JSON object.")

    app = _section(config, "app")
    admin = _section(config, "admin")
    database = _section(config, "database")
    store = _section(config, "store")
    telegram = _section(config, "telegram")
    xui = _section(config, "xui")
    revenue = _section(config, "revenue_engine")

    timezone_name = _clean_string(app.get("timezone") or store.get("timezone") or "Asia/Tehran")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        errors.append("app.timezone must be a valid IANA time zone.")

    language = _clean_string(app.get("language") or store.get("language") or "fa")
    if language not in SUPPORTED_LANGUAGES:
        errors.append("app.language must be one of: en, fa.")

    for domain_path, domain_value in (
        ("app.domain", app.get("domain")),
        ("store.domain", store.get("domain")),
    ):
        if _clean_string(domain_value) and not _valid_domain(_clean_string(domain_value)):
            errors.append(f"{domain_path} must be a valid domain, IP, or localhost value without scheme.")

    username = _clean_string(admin.get("username"))
    if not username:
        errors.append("admin.username is required.")

    password_mode = _clean_string(admin.get("password_mode")).lower()
    has_password = bool(_clean_string(admin.get("password")))
    has_password_env = bool(_clean_string(admin.get("password_env")))
    if password_mode == "generate":
        errors.append("admin.password_mode=generate is reserved for P3; use admin.password or admin.password_env in P2.")
    elif not has_password and not has_password_env:
        errors.append("admin.password or admin.password_env is required for P2 bootstrap.")
    if has_password_env and not _valid_env_name(_clean_string(admin.get("password_env"))):
        errors.append("admin.password_env must be a valid environment variable name.")

    email = _clean_string(admin.get("email"))
    if email:
        try:
            validate_email(email)
        except DjangoValidationError:
            warnings.append("admin.email does not look like a valid email address.")

    if database:
        engine = _clean_string(database.get("engine") or "sqlite").lower()
        if engine != "sqlite":
            errors.append("database.engine must be sqlite in P2.")

    telegram_enabled = _bool(telegram.get("enabled"), default=False)
    if telegram_enabled:
        if not _clean_string(telegram.get("bot_token")) and not _clean_string(telegram.get("bot_token_env")):
            errors.append("telegram.bot_token or telegram.bot_token_env is required when telegram.enabled=true.")
        if _clean_string(telegram.get("bot_token_env")) and not _valid_env_name(_clean_string(telegram.get("bot_token_env"))):
            errors.append("telegram.bot_token_env must be a valid environment variable name.")
        admin_ids = _admin_ids(telegram.get("admin_ids"))
        if not admin_ids:
            errors.append("telegram.admin_ids must contain at least one numeric ID when telegram.enabled=true.")
        for index, admin_id in enumerate(admin_ids):
            if not _numeric_telegram_id(admin_id):
                errors.append(f"telegram.admin_ids[{index}] must be numeric.")

    configure_xui = _bool(xui.get("configure_now"), default=False)
    if configure_xui:
        if not _clean_string(xui.get("panel_url")):
            errors.append("xui.panel_url is required when xui.configure_now=true.")
        elif not _valid_url(_clean_string(xui.get("panel_url"))):
            errors.append("xui.panel_url must be a valid http(s) URL.")
        if not _clean_string(xui.get("username")):
            errors.append("xui.username is required when xui.configure_now=true.")
        if not _clean_string(xui.get("password")) and not _clean_string(xui.get("password_env")):
            errors.append("xui.password or xui.password_env is required when xui.configure_now=true.")
        if _clean_string(xui.get("password_env")) and not _valid_env_name(_clean_string(xui.get("password_env"))):
            errors.append("xui.password_env must be a valid environment variable name.")
        _validate_inbounds(config, errors)
        _validate_plans(config, errors)
        _validate_routes(config, errors)

    if revenue and revenue.get("dry_run") is False:
        errors.append("revenue_engine.dry_run=false is not allowed for a new install.")
    if revenue and revenue.get("enabled") is False:
        errors.append("revenue_engine.enabled=false is not allowed for P2 bootstrap; start enabled with dry_run=true.")

    if errors:
        raise BootstrapInstallError("Install config validation failed", errors=errors, warnings=warnings)
    return warnings


def redact_bootstrap_summary(data):
    return _redact(deepcopy(data))


class BootstrapInstaller:
    def __init__(self, config, dry_run=False, update_existing=True, live_check=False, fail_on_live_check_error=False):
        self.config = config
        self.dry_run = bool(dry_run)
        self.update_existing = bool(update_existing)
        self.live_check = bool(live_check)
        self.fail_on_live_check_error = bool(fail_on_live_check_error)
        self.warnings = []
        self.summary = {
            "dry_run": self.dry_run,
            "update_existing": self.update_existing,
            "live_checks_run": False,
            "counts": {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "would_create": 0,
                "would_update": 0,
                "would_skip": 0,
            },
            "objects": {},
            "warnings": self.warnings,
        }
        self.store = None
        self.panel = None
        self.plan_by_key = {}
        self.inbound_by_key = {}

    def run(self):
        self.warnings.extend(validate_install_config(self.config))
        self._bootstrap_admin_user()
        self._bootstrap_store()
        self._bootstrap_bot_configuration()
        if _bool(self._xui_config().get("configure_now"), default=False):
            self._bootstrap_panel()
            self._bootstrap_inbounds()
            self._bootstrap_plans()
            self._bootstrap_routes()
        else:
            self._record_skip("panel", "xui.configure_now=false")
            self._record_skip("inbound", "xui.configure_now=false")
            self._record_skip("plan", "xui.configure_now=false")
            self._record_skip("plan_inbound_route", "xui.configure_now=false")
            self.warnings.append("X-UI setup is incomplete until an admin configures panel, inbounds, plans, and routes.")
        self._record_revenue_status()
        if self.live_check and not self.dry_run:
            self._run_live_checks()
        elif self.live_check and self.dry_run:
            self.warnings.append("Live checks are skipped in dry-run mode.")
        return redact_bootstrap_summary(self.summary)

    def _bootstrap_admin_user(self):
        admin = self._admin_config()
        username = _clean_string(admin.get("username"))
        email = _clean_string(admin.get("email"))
        User = get_user_model()
        user = User.objects.filter(username=username).first()

        defaults = {
            "email": email,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        }
        if not user:
            self._record_action("admin_user", "create", username=username)
            if self.dry_run:
                return None
            user = User(username=username, **defaults)
            user.set_password(self._resolve_secret(admin, "password", "password_env", "admin.password_env"))
            user.save()
            self.summary["objects"]["admin_user"]["pk"] = user.pk
            return user

        self.summary["objects"]["admin_user"] = {"action": "skip", "username": username, "pk": user.pk}
        if not self.update_existing:
            self._count("skipped")
            return user

        changed = self._assign_changed(user, defaults)
        password_provided = bool(_clean_string(admin.get("password")) or _clean_string(admin.get("password_env")))
        if changed or password_provided:
            self.summary["objects"]["admin_user"]["action"] = "update"
            self._count("would_update" if self.dry_run else "updated")
            if not self.dry_run:
                if password_provided:
                    user.set_password(self._resolve_secret(admin, "password", "password_env", "admin.password_env"))
                user.save()
            return user

        self._count("skipped")
        return user

    def _bootstrap_store(self):
        store_config = self._store_config()
        app = self._app_config()
        xui = self._xui_config()
        slug = _store_slug(store_config)
        domain = _clean_string(store_config.get("domain") or app.get("domain")) or None

        store = self._find_store(store_config, slug, domain)
        defaults = {
            "name": _clean_string(store_config.get("name")) or "VPN Store",
            "english_name": _clean_string(store_config.get("english_name")) or "VPN Store",
            "slug": slug,
            "domain": domain,
            "is_active": True,
            "card_number": _clean_string(store_config.get("card_number"))
            or _nested_string(store_config, "payment", "card_number")
            or SAFE_PLACEHOLDER_CARD_NUMBER,
            "card_owner": _clean_string(store_config.get("card_owner"))
            or _nested_string(store_config, "payment", "card_owner")
            or SAFE_PLACEHOLDER_CARD_OWNER,
            "receipt_image_only_payment": _bool(
                store_config.get("receipt_image_only_payment")
                if "receipt_image_only_payment" in store_config
                else _nested_value(store_config, "payment", "receipt_image_only_payment"),
                default=False,
            ),
            "bank_name": _clean_string(store_config.get("bank_name") or _nested_string(store_config, "payment", "bank_name"))
            or None,
            "sheba_number": _clean_string(store_config.get("sheba_number") or _nested_string(store_config, "payment", "sheba_number"))
            or None,
            "payment_sms_time_zone": _clean_string(store_config.get("payment_sms_time_zone") or app.get("timezone"))
            or "Asia/Tehran",
            "daily_admin_report_timezone": _clean_string(app.get("timezone")) or "Asia/Tehran",
            "revenue_engine_timezone": _clean_string(app.get("timezone")) or "Asia/Tehran",
            "plan_inbound_routing_enabled": True,
            "allow_global_inbound_fallback": not _bool(xui.get("configure_now"), default=False),
            "revenue_engine_enabled": True,
            "revenue_engine_dry_run": True,
            "renewal_engine_enabled": True,
            "upsell_engine_enabled": True,
            "retention_engine_enabled": True,
            "ai_revenue_optimizer_enabled": True,
            "revenue_optimization_enabled": True,
            "revenue_max_offers_per_user_per_day": int(self._revenue_config().get("daily_per_user", 1) or 1),
            "revenue_max_offers_per_user_per_week": int(self._revenue_config().get("weekly_per_user", 3) or 3),
            "revenue_max_total_offers_per_day": int(self._revenue_config().get("total_daily_cap", 100) or 100),
            "revenue_offer_cooldown_hours": int(self._revenue_config().get("cooldown_hours", 24) or 24),
            "retention_offer_cooldown_hours": int(self._revenue_config().get("retention_cooldown_hours", 72) or 72),
            "revenue_min_ai_confidence": Decimal(str(self._revenue_config().get("min_ai_confidence", "0.50"))),
        }

        if not store:
            self._record_action("store", "create", slug=slug, pk=None)
            if self.dry_run:
                return None
            store = Store(**defaults)
            store.full_clean()
            store.save()
            self.summary["objects"]["store"]["pk"] = store.pk
            self.store = store
            return store

        self.store = store
        self.summary["objects"]["store"] = {"action": "skip", "slug": slug, "pk": store.pk}
        if not self.update_existing:
            self._count("skipped")
            return store

        changed = self._assign_changed(store, defaults)
        if changed:
            self.summary["objects"]["store"]["action"] = "update"
            self._count("would_update" if self.dry_run else "updated")
            if not self.dry_run:
                store.full_clean()
                store.save()
            return store

        self._count("skipped")
        return store

    def _bootstrap_bot_configuration(self):
        telegram = self._telegram_config()
        if not _bool(telegram.get("enabled"), default=False):
            self._record_skip("bot_configuration", "telegram.enabled=false")
            return None

        username = _clean_string(telegram.get("bot_username")).lstrip("@")
        name = _clean_string(telegram.get("name")) or "Telegram admin bot"
        admin_ids = _admin_ids(telegram.get("admin_ids"))
        defaults = {
            "store": self.store,
            "provider": BotConfiguration.Provider.TELEGRAM,
            "name": name,
            "telegram_bot_username": username,
            "bot_token": self._secret_value(telegram, "bot_token", "bot_token_env", "telegram.bot_token_env"),
            "admin_user_id": admin_ids[0],
            "additional_admin_user_ids": "\n".join(admin_ids[1:]),
            "is_active": True,
            "force_telegram_channel_join": _bool(telegram.get("force_join_enabled"), default=False),
            "telegram_required_channel_id": _clean_string(telegram.get("force_join_channel_id")),
            "telegram_required_channel_username": _clean_string(telegram.get("force_join_channel_username")).lstrip("@"),
            "telegram_required_channel_invite_link": _clean_string(telegram.get("force_join_invite_link")),
        }

        bot_config = self._find_bot_configuration(username, name)
        if not bot_config:
            self._record_action("bot_configuration", "create", pk=None, provider="telegram")
            if self.dry_run:
                return None
            bot_config = BotConfiguration(**defaults)
            bot_config.full_clean()
            bot_config.save()
            self.summary["objects"]["bot_configuration"]["pk"] = bot_config.pk
            return bot_config

        self.summary["objects"]["bot_configuration"] = {
            "action": "skip",
            "pk": bot_config.pk,
            "provider": "telegram",
        }
        if not self.update_existing:
            self._count("skipped")
            return bot_config

        changed = self._assign_changed(bot_config, defaults)
        if changed:
            self.summary["objects"]["bot_configuration"]["action"] = "update"
            self._count("would_update" if self.dry_run else "updated")
            if not self.dry_run:
                bot_config.full_clean()
                bot_config.save()
            return bot_config

        self._count("skipped")
        return bot_config

    def _bootstrap_panel(self):
        xui = self._xui_config()
        name = _clean_string(xui.get("name")) or DEFAULT_PANEL_NAME
        url = _clean_string(xui.get("panel_url")).rstrip("/")
        defaults = {
            "store": self.store,
            "name": name,
            "url": url,
            "username": _clean_string(xui.get("username")),
            "password": self._secret_value(xui, "password", "password_env", "xui.password_env"),
            "proxy_url": _clean_string(xui.get("proxy_url") or _nested_string(xui, "proxy", "url")) or None,
            "is_active": True,
        }
        panel = self._find_panel(name, url)
        if not panel:
            self._record_action("panel", "create", name=name, pk=None)
            if self.dry_run:
                return None
            panel = Panel(**defaults)
            panel.full_clean()
            panel.save()
            self.summary["objects"]["panel"]["pk"] = panel.pk
            self.panel = panel
            return panel

        self.panel = panel
        self.summary["objects"]["panel"] = {"action": "skip", "name": name, "pk": panel.pk}
        if not self.update_existing:
            self._count("skipped")
            return panel

        changed = self._assign_changed(panel, defaults)
        if changed:
            self.summary["objects"]["panel"]["action"] = "update"
            self._count("would_update" if self.dry_run else "updated")
            if not self.dry_run:
                panel.full_clean()
                panel.save()
            return panel

        self._count("skipped")
        return panel

    def _bootstrap_inbounds(self):
        results = []
        for item in _inbound_items(self.config):
            inbound_id = int(item.get("inbound_id", item.get("xui_inbound_id", item.get("id"))))
            key = _inbound_key(item)
            defaults = {
                "panel": self.panel,
                "inbound_id": inbound_id,
                "remark": _clean_string(item.get("remark") or item.get("name")),
                "protocol": _clean_string(item.get("protocol")) or Inbound.Protocol.VLESS,
                "server_ip": _clean_string(item.get("server_ip")),
                "port": str(item.get("port", "")).strip(),
                "config_params": _clean_string(item.get("config_params")),
                "is_active": True,
                "available_for_new_orders": True,
                "health_monitor_enabled": True,
                "max_clients": item.get("max_clients"),
                "network_type": _clean_string(item.get("network_type")) or Inbound.NetworkType.TCP,
                "security": _clean_string(item.get("security")) or Inbound.Security.NONE,
                "sni": _clean_string(item.get("sni")) or None,
                "fingerprint": _clean_string(item.get("fingerprint")) or "chrome",
                "pbk": _clean_string(item.get("pbk")) or None,
                "sid": _clean_string(item.get("sid")) or None,
                "ws_path": _clean_string(item.get("ws_path")) or None,
                "ws_host": _clean_string(item.get("ws_host")) or None,
            }
            inbound = None if self.dry_run and not self.panel else Inbound.objects.filter(panel=self.panel, inbound_id=inbound_id).first()
            inbound = self._apply_model("inbound", inbound, Inbound, defaults, lookup={"inbound_id": inbound_id})
            if inbound:
                self.inbound_by_key[key] = inbound
            else:
                self.inbound_by_key[key] = {"inbound_id": inbound_id, "key": key}
            results.append({"key": key, "inbound_id": inbound_id})
        self.summary["objects"]["inbounds"] = {"count": len(results), "items": results}

    def _bootstrap_plans(self):
        results = []
        for item in _plan_items(self.config):
            slug = _plan_slug(item)
            defaults = {
                "store": self.store,
                "name": _clean_string(item.get("name")),
                "slug": slug,
                "description": _clean_string(item.get("description")),
                "volume_gb": _decimal(item.get("traffic_gb", item.get("volume_gb"))),
                "duration_days": int(item.get("duration_days")),
                "price": int(item.get("price")),
                "currency": _clean_string(item.get("currency")) or Plan.Currency.TOMAN,
                "device_limit": int(item.get("device_limit", 2) or 2),
                "is_active": True,
                "sort_order": int(item.get("sort_order", 0) or 0),
                "is_public": _bool(item.get("is_public", item.get("public")), default=True),
            }
            plan = None
            if not (self.dry_run and not self.store):
                plan = Plan.objects.filter(store=self.store, slug=slug).first()
            plan = self._apply_model("plan", plan, Plan, defaults, lookup={"slug": slug})
            for key in _plan_keys(item):
                self.plan_by_key[key] = plan or {"slug": slug, "key": key}
            results.append({"slug": slug, "name": defaults["name"]})
        self.summary["objects"]["plans"] = {"count": len(results), "items": results}

    def _bootstrap_routes(self):
        results = []
        for item in _route_items(self.config):
            plan_ref = _clean_string(item.get("plan") or item.get("plan_slug") or item.get("plan_key"))
            inbound_ref = _clean_string(item.get("inbound") or item.get("inbound_key") or item.get("inbound_id"))
            plan = self.plan_by_key.get(plan_ref)
            inbound = self.inbound_by_key.get(inbound_ref)
            priority = int(item.get("priority", 100) or 100)
            weight = int(item.get("weight", 1) or 1)
            if isinstance(plan, Plan) and isinstance(inbound, Inbound):
                existing = PlanInboundRoute.objects.filter(plan=plan, inbound=inbound, operator__isnull=True).first()
                defaults = {
                    "store": self.store,
                    "plan": plan,
                    "operator": None,
                    "inbound": inbound,
                    "is_active": True,
                    "priority": priority,
                    "weight": weight,
                    "note": _clean_string(item.get("note")) or "Created by bootstrap_install.",
                }
                self._apply_model(
                    "plan_inbound_route",
                    existing,
                    PlanInboundRoute,
                    defaults,
                    lookup={"plan": plan.slug, "inbound": inbound.inbound_id},
                )
            else:
                self._record_action("plan_inbound_route", "create", plan=plan_ref, inbound=inbound_ref)
            results.append({"plan": plan_ref, "inbound": inbound_ref})
        self.summary["objects"]["plan_inbound_routes"] = {"count": len(results), "items": results}

    def _record_revenue_status(self):
        self.summary["objects"]["revenue_engine"] = {
            "enabled": True,
            "dry_run": True,
            "daily_per_user": 1,
            "weekly_per_user": 3,
            "total_daily_cap": 100,
            "cooldown_hours": 24,
            "retention_cooldown_hours": 72,
            "min_ai_confidence": "0.50",
        }

    def _run_live_checks(self):
        self.summary["live_checks_run"] = True
        self.summary["objects"]["live_checks"] = {"telegram": "skipped", "xui": "skipped"}
        telegram = self._telegram_config()
        if _bool(telegram.get("enabled"), default=False):
            try:
                token = self._resolve_secret(telegram, "bot_token", "bot_token_env", "telegram.bot_token_env")
                self._live_check_telegram(token)
                self.summary["objects"]["live_checks"]["telegram"] = "ok"
            except Exception as exc:
                message = _sanitize_exception(exc, [_clean_string(telegram.get("bot_token")), locals().get("token", "")])
                self.summary["objects"]["live_checks"]["telegram"] = f"warning:{message}"
                if self.fail_on_live_check_error:
                    raise BootstrapInstallError(f"Telegram live check failed: {message}") from exc
                self.warnings.append(f"Telegram live check failed: {message}")

        if _bool(self._xui_config().get("configure_now"), default=False) and self.panel:
            try:
                self._live_check_xui(self.panel)
                self.summary["objects"]["live_checks"]["xui"] = "ok"
            except Exception as exc:
                message = _sanitize_exception(
                    exc,
                    [
                        getattr(self.panel, "url", ""),
                        getattr(self.panel, "username", ""),
                        getattr(self.panel, "password", ""),
                        getattr(self.panel, "proxy_url", ""),
                    ],
                )
                self.summary["objects"]["live_checks"]["xui"] = f"warning:{message}"
                if self.fail_on_live_check_error:
                    raise BootstrapInstallError(f"X-UI live check failed: {message}") from exc
                self.warnings.append(f"X-UI live check failed: {message}")

    def _live_check_telegram(self, token):
        from store.telegram_bot.client import BotClient

        config = BotConfiguration(provider=BotConfiguration.Provider.TELEGRAM, bot_token=token)
        BotClient(config).get_me()

    def _live_check_xui(self, panel):
        from store.xui_api import login_to_panel

        login_to_panel(panel)

    def _apply_model(self, label, instance, model_class, defaults, lookup):
        if instance is None:
            self._record_action(label, "create", pk=None, **lookup)
            if self.dry_run:
                return None
            instance = model_class(**defaults)
            instance.full_clean()
            instance.save()
            self.summary["objects"][label]["pk"] = instance.pk
            return instance

        self.summary["objects"].setdefault(label, {"action": "skip", "pk": instance.pk, **lookup})
        if not self.update_existing:
            self._count("skipped")
            return instance
        changed = self._assign_changed(instance, defaults)
        if changed:
            self.summary["objects"][label] = {"action": "update", "pk": instance.pk, **lookup}
            self._count("would_update" if self.dry_run else "updated")
            if not self.dry_run:
                instance.full_clean()
                instance.save()
        else:
            self._count("skipped")
        return instance

    def _find_store(self, store_config, slug, domain):
        if slug:
            store = Store.objects.filter(slug=slug).first()
            if store:
                return store
        if domain:
            store = Store.objects.filter(domain=domain).first()
            if store:
                return store
        name = _clean_string(store_config.get("name"))
        if name:
            return Store.objects.filter(name=name).first()
        return None

    def _find_bot_configuration(self, username, name):
        queryset = BotConfiguration.objects.filter(store=self.store, provider=BotConfiguration.Provider.TELEGRAM)
        if username:
            bot_config = queryset.filter(telegram_bot_username=username).first()
            if bot_config:
                return bot_config
        return queryset.filter(name=name).first()

    def _find_panel(self, name, url):
        panel = Panel.objects.filter(store=self.store, name=name).first()
        if panel:
            return panel
        return Panel.objects.filter(store=self.store, url=url).first()

    def _record_action(self, label, action, **details):
        if action == "create":
            self._count("would_create" if self.dry_run else "created")
        elif action == "update":
            self._count("would_update" if self.dry_run else "updated")
        else:
            self._count("would_skip" if self.dry_run else "skipped")
        rendered_action = f"would_{action}" if self.dry_run else action
        self.summary["objects"][label] = {"action": rendered_action, **details}

    def _record_skip(self, label, reason):
        action = "would_skip" if self.dry_run else "skip"
        self._count(action if self.dry_run else "skipped")
        self.summary["objects"][label] = {"action": action, "reason": reason}

    def _count(self, key):
        self.summary["counts"][key] = self.summary["counts"].get(key, 0) + 1

    def _assign_changed(self, instance, defaults):
        changed = False
        for field, value in defaults.items():
            if getattr(instance, field) != value:
                setattr(instance, field, value)
                changed = True
        return changed

    def _resolve_secret(self, section, value_key, env_key, path_label):
        value = _clean_string(section.get(value_key))
        if value:
            return value
        env_name = _clean_string(section.get(env_key))
        if not env_name:
            return ""
        env_value = os.environ.get(env_name, "")
        if not env_value:
            raise BootstrapInstallError(f"{path_label} is configured but the environment variable is empty or missing.")
        return env_value

    def _secret_value(self, section, value_key, env_key, path_label):
        if self.dry_run:
            return "<configured>"
        return self._resolve_secret(section, value_key, env_key, path_label)

    def _app_config(self):
        return _section(self.config, "app")

    def _admin_config(self):
        return _section(self.config, "admin")

    def _store_config(self):
        return _section(self.config, "store")

    def _telegram_config(self):
        return _section(self.config, "telegram")

    def _xui_config(self):
        return _section(self.config, "xui")

    def _revenue_config(self):
        return _section(self.config, "revenue_engine")


def _section(config, name):
    section = config.get(name) or {}
    if not isinstance(section, dict):
        raise BootstrapInstallError("Install config validation failed", errors=[f"{name} must be an object."])
    return section


def _clean_string(value):
    if value is None:
        return ""
    return str(value).strip()


def _nested_value(config, section, key):
    nested = config.get(section) or {}
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def _nested_string(config, section, key):
    return _clean_string(_nested_value(config, section, key))


def _bool(value, *, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _valid_env_name(value):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""))


def _valid_domain(value):
    value = _clean_string(value)
    if "://" in value or "/" in value or len(value) > 253:
        return False
    if value == "localhost":
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+", value)) and not value.startswith(".") and not value.endswith(".")


def _valid_url(value):
    try:
        URLValidator(schemes=["http", "https"])(value)
    except DjangoValidationError:
        return False
    return True


def _numeric_telegram_id(value):
    return bool(re.fullmatch(r"-?\d+", _clean_string(value)))


def _admin_ids(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = re.split(r"[\s,]+", str(value))
    return [_clean_string(item) for item in values if _clean_string(item)]


def _store_slug(store_config):
    explicit = _clean_string(store_config.get("slug"))
    if explicit:
        return explicit
    source = _clean_string(store_config.get("english_name") or store_config.get("name"))
    generated = slugify(source) if source else ""
    return generated or DEFAULT_STORE_SLUG


def _plan_slug(item):
    explicit = _clean_string(item.get("slug") or item.get("key"))
    if explicit:
        return explicit
    generated = slugify(_clean_string(item.get("name")))
    return generated or "default-plan"


def _plan_keys(item):
    keys = {
        _clean_string(item.get("key")),
        _clean_string(item.get("slug")),
        _clean_string(item.get("name")),
        _plan_slug(item),
    }
    return {key for key in keys if key}


def _inbound_key(item):
    for key in ("key", "slug", "name", "remark", "inbound_id", "xui_inbound_id", "id"):
        value = _clean_string(item.get(key))
        if value:
            return value
    return ""


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BootstrapInstallError("Install config validation failed", errors=["plans volume_gb must be a decimal."]) from exc


def _inbound_items(config):
    xui = _section(config, "xui")
    inbounds = xui.get("inbounds") or []
    if not isinstance(inbounds, list):
        raise BootstrapInstallError("Install config validation failed", errors=["xui.inbounds must be a list."])
    return inbounds


def _plan_items(config):
    plans = config.get("plans") or []
    if isinstance(plans, dict):
        for key in ("items", "default_plans", "plans"):
            if isinstance(plans.get(key), list):
                plans = plans[key]
                break
        else:
            raise BootstrapInstallError("Install config validation failed", errors=["plans must be a list or contain items/default_plans."])
    if not isinstance(plans, list):
        raise BootstrapInstallError("Install config validation failed", errors=["plans must be a list."])
    return plans


def _route_items(config):
    routes = config.get("plan_routes") or []
    if isinstance(routes, dict):
        for key in ("items", "routes", "mappings"):
            if isinstance(routes.get(key), list):
                routes = routes[key]
                break
            if isinstance(routes.get(key), dict):
                routes = routes[key]
                break
        if isinstance(routes, dict):
            routes = [{"plan": plan, "inbound": inbound} for plan, inbound in routes.items()]
    if not isinstance(routes, list):
        raise BootstrapInstallError("Install config validation failed", errors=["plan_routes must be a list or mapping."])
    return routes


def _validate_inbounds(config, errors):
    inbounds = _inbound_items(config)
    if not inbounds:
        errors.append("xui.inbounds must include at least one inbound when xui.configure_now=true.")
        return
    keys = set()
    for index, item in enumerate(inbounds):
        if not isinstance(item, dict):
            errors.append(f"xui.inbounds[{index}] must be an object.")
            continue
        inbound_id = item.get("inbound_id", item.get("xui_inbound_id", item.get("id")))
        if inbound_id is None:
            errors.append(f"xui.inbounds[{index}].inbound_id is required.")
        else:
            try:
                if int(inbound_id) <= 0:
                    errors.append(f"xui.inbounds[{index}].inbound_id must be positive.")
            except (TypeError, ValueError):
                errors.append(f"xui.inbounds[{index}].inbound_id must be numeric.")
        for field in ("server_ip", "port", "config_params"):
            if not _clean_string(item.get(field)):
                errors.append(f"xui.inbounds[{index}].{field} is required by the current Inbound model.")
        protocol = _clean_string(item.get("protocol") or Inbound.Protocol.VLESS)
        if protocol not in Inbound.Protocol.values:
            errors.append(f"xui.inbounds[{index}].protocol is invalid.")
        network_type = _clean_string(item.get("network_type") or Inbound.NetworkType.TCP)
        if network_type not in Inbound.NetworkType.values:
            errors.append(f"xui.inbounds[{index}].network_type is invalid.")
        security = _clean_string(item.get("security") or Inbound.Security.NONE)
        if security not in Inbound.Security.values:
            errors.append(f"xui.inbounds[{index}].security is invalid.")
        key = _inbound_key(item)
        if not key:
            errors.append(f"xui.inbounds[{index}] needs key, remark, or inbound_id for route references.")
        elif key in keys:
            errors.append(f"xui.inbounds[{index}] duplicates an inbound route key.")
        keys.add(key)


def _validate_plans(config, errors):
    plans = _plan_items(config)
    if not plans:
        errors.append("plans must include at least one plan when xui.configure_now=true.")
        return
    keys = set()
    for index, item in enumerate(plans):
        if not isinstance(item, dict):
            errors.append(f"plans[{index}] must be an object.")
            continue
        if not _clean_string(item.get("name")):
            errors.append(f"plans[{index}].name is required.")
        for field in ("traffic_gb", "volume_gb"):
            if item.get(field) is not None:
                try:
                    if Decimal(str(item.get(field))) <= 0:
                        errors.append(f"plans[{index}].{field} must be positive.")
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"plans[{index}].{field} must be numeric.")
                break
        else:
            errors.append(f"plans[{index}].traffic_gb or volume_gb is required.")
        for field in ("duration_days", "price"):
            try:
                if int(item.get(field)) <= 0:
                    errors.append(f"plans[{index}].{field} must be positive.")
            except (TypeError, ValueError):
                errors.append(f"plans[{index}].{field} must be numeric.")
        currency = _clean_string(item.get("currency") or Plan.Currency.TOMAN)
        if currency not in Plan.Currency.values:
            errors.append(f"plans[{index}].currency is invalid.")
        for key in _plan_keys(item):
            if key in keys:
                errors.append(f"plans[{index}] duplicates a plan route key.")
            keys.add(key)


def _validate_routes(config, errors):
    routes = _route_items(config)
    if not routes:
        errors.append("plan_routes must include explicit plan-to-inbound routes when xui.configure_now=true.")
        return
    known_plans = set()
    for item in _plan_items(config):
        if isinstance(item, dict):
            known_plans.update(_plan_keys(item))
    known_inbounds = {_inbound_key(item) for item in _inbound_items(config) if isinstance(item, dict)}
    for index, item in enumerate(routes):
        if not isinstance(item, dict):
            errors.append(f"plan_routes[{index}] must be an object.")
            continue
        plan_ref = _clean_string(item.get("plan") or item.get("plan_slug") or item.get("plan_key"))
        inbound_ref = _clean_string(item.get("inbound") or item.get("inbound_key") or item.get("inbound_id"))
        if not plan_ref:
            errors.append(f"plan_routes[{index}].plan is required.")
        elif plan_ref not in known_plans:
            errors.append(f"plan_routes[{index}] references an unknown plan.")
        if not inbound_ref:
            errors.append(f"plan_routes[{index}].inbound is required.")
        elif inbound_ref not in known_inbounds:
            errors.append(f"plan_routes[{index}] references an unknown inbound.")


def _redact(value, key_name=""):
    lower_key = key_name.lower()
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            redacted[key] = _redact(child, str(key))
        return redacted
    if isinstance(value, list):
        if lower_key in {"admin_ids", "admin_user_id", "additional_admin_user_ids"}:
            return [_mask_id(item) for item in value]
        return [_redact(item, key_name) for item in value]
    if any(keyword in lower_key for keyword in SECRET_KEYWORDS):
        return "<redacted>" if _clean_string(value) else ""
    if lower_key in {"admin_ids", "admin_user_id", "additional_admin_user_ids"}:
        return _mask_id(value)
    return value


def _mask_id(value):
    text = _clean_string(value)
    if not text:
        return ""
    sign = "-" if text.startswith("-") else ""
    compact = text.lstrip("-")
    if len(compact) <= 4:
        return f"{sign}****"
    return f"{sign}****{compact[-4:]}"


def _sanitize_exception(exc, secrets):
    text = str(exc) or exc.__class__.__name__
    for secret in secrets:
        secret = _clean_string(secret)
        if secret:
            text = text.replace(secret, "<redacted>")
    return text
