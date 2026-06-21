from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from .admin_catalog import catalog_url
from .bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url
from .models import BotConfiguration, Inbound, Panel, Plan, PlanInboundRoute, RevenueOfferLog, Store


SAFE_PLACEHOLDER_CARD_NUMBER = "0000000000000000"
SAFE_PLACEHOLDER_CARD_OWNER = "Configure Payment Owner"

WIZARD_STEP_URL_NAMES = {
    "store": "admin_store_setup_wizard_store",
    "payment": "admin_store_setup_wizard_payment",
    "telegram": "admin_store_setup_wizard_telegram",
    "telegram-proxy": "admin_store_setup_wizard_telegram_proxy",
    "panel": "admin_store_setup_wizard_panel",
    "inbounds": "admin_store_setup_wizard_inbounds",
    "plans": "admin_store_setup_wizard_plans",
    "routes": "admin_store_setup_wizard_routes",
    "review": "admin_store_setup_wizard_review",
}

SETUP_CARD_WIZARD_STEPS = {
    "store_identity": "store",
    "payment": "payment",
    "telegram_bot": "telegram",
    "telegram_proxy": "telegram-proxy",
    "xui_panel": "panel",
    "inbounds": "inbounds",
    "plans": "plans",
    "plan_routes": "routes",
    "integration_check": "review",
    "revenue_engine": "review",
}


@dataclass(frozen=True)
class SetupAction:
    label: str
    url: str
    style: str = "primary"


@dataclass(frozen=True)
class SetupCard:
    key: str
    title: str
    description: str
    status: str
    status_label: str
    details: list[str] = field(default_factory=list)
    actions: list[SetupAction] = field(default_factory=list)
    command: str = ""


def admin_url(name, *args):
    return reverse(f"admin:{name}", args=args)


def add_query(url, params):
    cleaned = {key: value for key, value in params.items() if value not in ("", None)}
    if not cleaned:
        return url
    return f"{url}?{urlencode(cleaned)}"


def setup_wizard_index_url(store=None):
    return add_query(reverse("admin_store_setup_wizard"), {"store": getattr(store, "pk", None)})


def setup_wizard_step_url(slug, store=None):
    return add_query(reverse(WIZARD_STEP_URL_NAMES[slug]), {"store": getattr(store, "pk", None)})


def setup_wizard_url_for_card_key(key, store=None):
    slug = SETUP_CARD_WIZARD_STEPS.get(key, "review")
    return setup_wizard_step_url(slug, store)


def setup_wizard_action(key, store=None, label="باز کردن مرحله wizard"):
    return SetupAction(label, setup_wizard_url_for_card_key(key, store), "info")


def with_wizard_action(key, store=None, actions=None):
    return [setup_wizard_action(key, store), *(actions or [])]


def store_change_url(store):
    if store and store.pk:
        return admin_url("store_store_change", store.pk)
    return admin_url("store_store_add")


def changelist_url(name, store=None, *, store_filter="store__id__exact"):
    url = admin_url(name)
    if store and store.pk and store_filter:
        return add_query(url, {store_filter: store.pk})
    return url


def status_label(status):
    return {
        "done": "انجام شده",
        "warning": "نیاز به بررسی",
        "missing": "تکمیل نشده",
        "safe": "ایمن / dry-run",
        "skipped": "بعداً",
        "error": "خطا",
    }.get(status, status)


def card(key, title, description, status, details=None, actions=None, command=""):
    return SetupCard(
        key=key,
        title=title,
        description=description,
        status=status,
        status_label=status_label(status),
        details=list(details or []),
        actions=list(actions or []),
        command=command,
    )


def configured_text(value):
    return "تنظیم شده" if str(value or "").strip() else "تنظیم نشده"


def has_real_payment_card(store):
    number = str(getattr(store, "card_number", "") or "").strip()
    owner = str(getattr(store, "card_owner", "") or "").strip()
    return (
        number
        and owner
        and number != SAFE_PLACEHOLDER_CARD_NUMBER
        and owner != SAFE_PLACEHOLDER_CARD_OWNER
    )


def active_telegram_configs(store):
    queryset = BotConfiguration.objects.filter(
        provider=BotConfiguration.Provider.TELEGRAM,
        is_active=True,
    )
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.order_by("store_id", "pk")


def active_panels(store):
    queryset = Panel.objects.filter(is_active=True)
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.order_by("name", "pk")


def active_sales_inbounds(store):
    queryset = Inbound.objects.filter(
        panel__is_active=True,
        is_active=True,
        available_for_new_orders=True,
    ).select_related("panel")
    if store and store.pk:
        queryset = queryset.filter(Q(panel__store=store) | Q(panel__store__isnull=True))
    return queryset.order_by("panel__name", "inbound_id", "pk")


def active_sellable_plans(store):
    queryset = Plan.objects.filter(
        is_active=True,
        is_public=True,
        is_custom_volume=False,
    )
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.order_by("sort_order", "price", "pk")


def route_queryset_for_store(store):
    queryset = PlanInboundRoute.objects.select_related("store", "plan", "operator", "inbound", "inbound__panel")
    if store and store.pk:
        queryset = queryset.filter(
            Q(store=store) | Q(store__isnull=True),
            Q(inbound__panel__store=store) | Q(inbound__panel__store__isnull=True),
        )
    return queryset


def route_is_usable(route, store):
    inbound = route.inbound
    panel = getattr(inbound, "panel", None)
    if not route.is_active:
        return False
    if not route.plan.is_active or not route.plan.is_public or route.plan.is_custom_volume:
        return False
    if not inbound.is_active or not inbound.available_for_new_orders:
        return False
    if not panel or not panel.is_active:
        return False
    if store and store.pk:
        if route.store_id and route.store_id != store.pk:
            return False
        if panel.store_id and panel.store_id != store.pk:
            return False
        if route.plan.store_id and route.plan.store_id != store.pk:
            return False
    if route.operator_id:
        if not route.operator.is_active:
            return False
        if not route.plan.operators.filter(pk=route.operator_id).exists():
            return False
    if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
        return False
    return True


def plan_active_operators(plan, store):
    queryset = plan.operators.filter(is_active=True)
    if store and store.pk:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.order_by("sort_order", "name", "pk")


def plan_has_general_route(plan, store):
    routes = route_queryset_for_store(store).filter(plan=plan, operator__isnull=True, is_active=True)
    return any(route_is_usable(route, store) for route in routes)


def plan_has_operator_route(plan, operator, store):
    routes = route_queryset_for_store(store).filter(plan=plan, operator=operator, is_active=True)
    return any(route_is_usable(route, store) for route in routes)


def missing_route_labels(store):
    missing = []
    for plan in active_sellable_plans(store).prefetch_related("operators"):
        has_general = plan_has_general_route(plan, store)
        if has_general:
            continue
        if store and store.sales_mode == Store.SalesMode.OPERATOR_BASED:
            operators = list(plan_active_operators(plan, store))
            if not operators:
                missing.append(f"{plan.name}")
                continue
            for operator in operators:
                if not plan_has_operator_route(plan, operator, store):
                    missing.append(f"{plan.name} / {operator.name}")
            continue
        missing.append(f"{plan.name}")
    return missing


def invalid_route_count(store):
    return sum(
        1
        for route in route_queryset_for_store(store).filter(is_active=True)
        if not route_is_usable(route, store)
    )


def setup_store_identity_card(store):
    details = []
    if not store:
        return card(
            "store_identity",
            "هویت فروشگاه",
            "نام، دامنه و وضعیت فعال بودن فروشگاه.",
            "missing",
            ["هیچ Storeای ساخته نشده است."],
            with_wizard_action("store_identity", store, [SetupAction("ساخت Store خام", admin_url("store_store_add"))]),
        )

    if store.name:
        details.append(f"نام: {store.name}")
    if store.english_name:
        details.append(f"نام انگلیسی: {store.english_name}")
    details.append(f"دامنه: {configured_text(store.domain)}")
    details.append("فروشگاه فعال است." if store.is_active else "فروشگاه غیرفعال است.")
    status = "done" if store.name and store.english_name and store.is_active else "warning"
    if not store.domain:
        status = "warning"
        details.append("دامنه هنوز تنظیم نشده؛ برای نصب محلی یا minimal می‌تواند موقتاً خالی باشد.")
    return card(
        "store_identity",
        "هویت فروشگاه",
        "نام، دامنه، زبان و اطلاعات برند.",
        status,
        details,
        with_wizard_action("store_identity", store, [SetupAction("برو به Store", store_change_url(store))]),
    )


def setup_payment_card(store):
    if not store:
        return card(
            "payment",
            "پرداخت",
            "کارت و تنظیمات SMS پرداخت.",
            "missing",
            actions=[setup_wizard_action("store_identity", store)],
        )

    details = [
        "شماره کارت: تنظیم شده" if store.card_number else "شماره کارت: تنظیم نشده",
        f"صاحب کارت: {configured_text(store.card_owner)}",
        f"منطقه زمانی SMS: {store.payment_sms_time_zone or 'پیش‌فرض'}",
    ]
    if getattr(store, "smsforwarder_webhook_token_hash", ""):
        hint = store.smsforwarder_webhook_token_hint or "----"
        details.append(f"SMSForwarder webhook token تنظیم شده؛ انتها: {hint}")
    else:
        details.append("SMSForwarder webhook token تنظیم نشده یا از fallback قدیمی استفاده می‌شود.")

    status = "done" if has_real_payment_card(store) else "warning"
    if not store.card_number or not store.card_owner:
        status = "missing"
    if store.card_number == SAFE_PLACEHOLDER_CARD_NUMBER or store.card_owner == SAFE_PLACEHOLDER_CARD_OWNER:
        details.append("مقادیر پرداخت هنوز placeholder نصب minimal هستند.")
    return card(
        "payment",
        "پرداخت",
        "کارت، صاحب کارت، رفتار رسید و SMSForwarder.",
        status,
        details,
        with_wizard_action("payment", store, [SetupAction("تنظیم پرداخت خام", store_change_url(store))]),
    )


def setup_telegram_card(store):
    configs = list(active_telegram_configs(store))
    if not configs:
        return card(
            "telegram_bot",
            "ربات تلگرام",
            "BotConfiguration فعال برای پیام‌ها و عملیات تلگرام.",
            "warning",
            ["برای نصب minimal نبودن ربات خطای نصب نیست؛ قبل از فروش تلگرامی آن را کامل کن."],
            with_wizard_action(
                "telegram_bot",
                store,
                [SetupAction("برو به BotConfiguration", changelist_url("store_botconfiguration_changelist", store))],
            ),
        )

    configured = 0
    details = []
    for config in configs[:3]:
        token_ok = bool(str(config.bot_token or "").strip())
        admin_ok = bool(config.get_admin_user_ids())
        username_ok = bool(str(config.telegram_bot_username or "").strip())
        if token_ok and admin_ok:
            configured += 1
        details.append(
            f"{config.name}: token {configured_text(token_ok)}، admin IDs {configured_text(admin_ok)}، username {configured_text(username_ok)}"
        )
    status = "done" if configured else "warning"
    return card(
        "telegram_bot",
        "ربات تلگرام",
        "وضعیت BotConfiguration بدون تست live.",
        status,
        details,
        with_wizard_action(
            "telegram_bot",
            store,
            [SetupAction("برو به BotConfiguration", changelist_url("store_botconfiguration_changelist", store))],
        ),
    )


def setup_telegram_proxy_card(store=None):
    raw_proxy = telegram_proxy_url()
    details = []
    if raw_proxy:
        details.append(f"Proxy تلگرام فعال است: {sanitized_telegram_proxy_url(raw_proxy)}")
        status = "done"
    else:
        details.append("Proxy تلگرام تنظیم نشده؛ فقط اگر سرور به Telegram دسترسی ندارد لازم است.")
        status = "safe"
    return card(
        "telegram_proxy",
        "Proxy تلگرام",
        "وضعیت proxy از settings/env، بدون نمایش مقدار حساس.",
        status,
        details,
        [setup_wizard_action("telegram_proxy", store)],
        command="python manage.py test_telegram_proxy --help",
    )


def setup_panel_card(store):
    panels = list(active_panels(store))
    if not panels:
        return card(
            "xui_panel",
            "پنل X-UI/Sanaei",
            "اتصال پنل فقط از روی DB بررسی می‌شود.",
            "warning",
            ["هیچ پنل فعال ثبت نشده است."],
            with_wizard_action("xui_panel", store, [SetupAction("برو به Panel", changelist_url("store_panel_changelist", store))]),
        )
    usable = [panel for panel in panels if panel.url and panel.username and panel.password]
    details = [f"{len(panels)} پنل فعال، {len(usable)} پنل با URL/username/password کامل."]
    status = "done" if usable else "warning"
    return card(
        "xui_panel",
        "پنل X-UI/Sanaei",
        "پنل‌های فعال و credential status، بدون login live.",
        status,
        details,
        with_wizard_action("xui_panel", store, [SetupAction("برو به Panel", changelist_url("store_panel_changelist", store))]),
    )


def setup_inbounds_card(store):
    available = active_sales_inbounds(store)
    total_active = Inbound.objects.filter(is_active=True)
    if store and store.pk:
        total_active = total_active.filter(Q(panel__store=store) | Q(panel__store__isnull=True))
    available_count = available.count()
    total_count = total_active.count()
    if available_count:
        status = "done"
        details = [f"{available_count} inbound فعال و آماده فروش از {total_count} inbound فعال."]
    elif total_count:
        status = "warning"
        details = ["Inbound فعال وجود دارد، ولی هیچ‌کدام برای فروش جدید آماده نیست."]
    else:
        status = "warning"
        details = ["بعد از نصب minimal، نبودن inbound هشدار setup است نه خطای نصب."]
    return card(
        "inbounds",
        "Inboundها",
        "وضعیت inboundهای قابل استفاده برای سفارش جدید.",
        status,
        details,
        with_wizard_action(
            "inbounds",
            store,
            [SetupAction("برو به Inbound", changelist_url("store_inbound_changelist", store, store_filter="panel__store__id__exact"))],
        ),
    )


def setup_plans_card(store):
    plans = active_sellable_plans(store)
    count = plans.count()
    custom_volume_enabled = bool(store and getattr(store, "custom_volume_price_per_gb", 0))
    if count or custom_volume_enabled:
        details = [f"{count} پلن عمومی فعال."]
        if custom_volume_enabled:
            details.append("فروش حجم دلخواه فعال است.")
        status = "done"
    else:
        details = ["هنوز پلن عمومی فعال برای فروش ساخته نشده است."]
        status = "warning"
    return card(
        "plans",
        "پلن‌ها",
        "پلن‌های عمومی فعال و فروش حجم دلخواه.",
        status,
        details,
        with_wizard_action(
            "plans",
            store,
            [
                SetupAction("مدیریت محصولات", catalog_url(store), "primary"),
                SetupAction("برو به Plans", changelist_url("store_plan_changelist", store)),
            ],
        ),
    )


def setup_routes_card(store):
    missing = missing_route_labels(store) if store else []
    invalid_count = invalid_route_count(store) if store else 0
    active_route_count = route_queryset_for_store(store).filter(is_active=True).count() if store else 0
    if missing:
        status = "error"
        details = [
            f"{len(missing)} پلن/اپراتور فعال route معتبر ندارد.",
            "نمونه‌ها: " + "، ".join(missing[:5]),
        ]
    elif invalid_count:
        status = "error"
        details = [f"{invalid_count} route فعال به inbound/panel نامعتبر یا unavailable اشاره می‌کند."]
    elif active_sellable_plans(store).exists():
        status = "done"
        details = ["همه پلن‌های sellable route معتبر دارند."]
    else:
        status = "warning"
        details = ["برای نصب minimal، نبودن route تا قبل از ساخت پلن قابل انتظار است."]
    details.append(f"Route فعال: {active_route_count}")
    return card(
        "plan_routes",
        "Route پلن‌ها",
        "اتصال پلن‌های فروش به inboundهای قابل فروش.",
        status,
        details,
        with_wizard_action(
            "plan_routes",
            store,
            [
                SetupAction("مدیریت محصولات و route", catalog_url(store), "primary"),
                SetupAction("برو به Routes", changelist_url("store_planinboundroute_changelist", store)),
            ],
        ),
    )


def setup_integration_check_card(store=None):
    return card(
        "integration_check",
        "Integration check",
        "این صفحه live call نمی‌زند؛ check غیرزنده را از سرور اجرا کن.",
        "safe",
        ["Live Telegram/X-UI فقط با flag صریح اجرا می‌شود."],
        [setup_wizard_action("integration_check", store, "Review wizard")],
        command="/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail",
    )


def setup_revenue_card(store):
    if not store:
        return card(
            "revenue_engine",
            "Revenue Engine",
            "وضعیت dry-run یا ارسال واقعی.",
            "missing",
            actions=[setup_wizard_action("revenue_engine", store, "Review wizard")],
        )
    latest = RevenueOfferLog.objects.filter(Q(store=store) | Q(store__isnull=True)).order_by("-created_at").first()
    details = []
    if latest:
        details.append(f"آخرین log: {latest.status} / {latest.engine_type}")
    else:
        details.append("هنوز RevenueOfferLog ثبت نشده است.")

    if not store.revenue_engine_enabled:
        status = "warning"
        details.append("Revenue Engine غیرفعال است.")
    elif store.revenue_engine_dry_run:
        status = "safe"
        details.append("dry_run فعال است؛ پیام واقعی ارسال نمی‌شود.")
    else:
        status = "warning"
        details.append("ارسال واقعی فعال است؛ قبل از rollout محدود، گزارش‌ها را بررسی کن.")
    return card(
        "revenue_engine",
        "Revenue Engine",
        "وضعیت dry-run، real-send و آخرین لاگ.",
        status,
        details,
        with_wizard_action("revenue_engine", store, [
            SetupAction("تنظیم Revenue", store_change_url(store)),
            SetupAction("Revenue logs", changelist_url("store_revenueofferlog_changelist", store)),
        ]),
    )


def build_setup_cards(store):
    return [
        setup_store_identity_card(store),
        setup_payment_card(store),
        setup_telegram_card(store),
        setup_telegram_proxy_card(store),
        setup_panel_card(store),
        setup_inbounds_card(store),
        setup_plans_card(store),
        setup_routes_card(store),
        setup_integration_check_card(store),
        setup_revenue_card(store),
    ]


def build_setup_center_context(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)

    cards = build_setup_cards(selected_store)
    counts = {
        "done": sum(1 for item in cards if item.status == "done"),
        "warning": sum(1 for item in cards if item.status == "warning"),
        "missing": sum(1 for item in cards if item.status == "missing"),
        "safe": sum(1 for item in cards if item.status == "safe"),
        "error": sum(1 for item in cards if item.status == "error"),
    }
    return {
        "stores": stores,
        "selected_store": selected_store,
        "cards": cards,
        "counts": counts,
        "wizard_url": setup_wizard_index_url(selected_store),
    }


def build_store_admin_summary(store):
    if not store:
        return []
    cards = {item.key: item for item in build_setup_cards(store)}
    return [
        ("Business setup", cards["store_identity"].status_label, cards["store_identity"].status),
        ("Telegram", cards["telegram_bot"].status_label, cards["telegram_bot"].status),
        ("X-UI panel", cards["xui_panel"].status_label, cards["xui_panel"].status),
        ("Sellable plans", cards["plans"].details[0], cards["plans"].status),
        ("Plan routes", cards["plan_routes"].status_label, cards["plan_routes"].status),
        ("Revenue", cards["revenue_engine"].status_label, cards["revenue_engine"].status),
    ]
