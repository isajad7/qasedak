from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse

from .admin_forms import (
    InboundSetupForm,
    PanelSetupForm,
    PaymentSetupForm,
    PlanRouteSetupForm,
    PlanSetupForm,
    StoreIdentitySetupForm,
    TelegramProxySetupForm,
    TelegramSetupForm,
)
from .admin_setup import active_panels, active_telegram_configs, build_setup_cards, route_queryset_for_store
from .bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url
from .models import BotConfiguration, Inbound, Panel, Plan, PlanInboundRoute, Store


SKIPPED_SESSION_KEY = "qasedak_setup_wizard_skipped_steps"


@dataclass(frozen=True)
class WizardStep:
    slug: str
    url_name: str
    title: str
    description: str
    setup_card_key: str = ""
    template: str = "admin/store/setup_wizard/form_step.html"


WIZARD_STEPS = (
    WizardStep(
        "store",
        "admin_store_setup_wizard_store",
        "هویت فروشگاه",
        "نام، دامنه و اطلاعات پایه‌ای که مشتری می‌بیند.",
        "store_identity",
    ),
    WizardStep(
        "payment",
        "admin_store_setup_wizard_payment",
        "پرداخت",
        "کارت مقصد، صاحب کارت، شبا و SMSForwarder را تنظیم کن.",
        "payment",
    ),
    WizardStep(
        "telegram",
        "admin_store_setup_wizard_telegram",
        "تلگرام",
        "ربات تلگرام و admin IDها را بدون نمایش token کامل ذخیره کن.",
        "telegram_bot",
    ),
    WizardStep(
        "telegram-proxy",
        "admin_store_setup_wizard_telegram_proxy",
        "پروکسی تلگرام",
        "اگر سرور به Telegram دسترسی ندارد، وضعیت proxy را بررسی کن.",
        "telegram_proxy",
    ),
    WizardStep(
        "panel",
        "admin_store_setup_wizard_panel",
        "پنل X-UI/Sanaei",
        "اتصال پنل و credentialها را ثبت کن؛ test live خودکار اجرا نمی‌شود.",
        "xui_panel",
    ),
    WizardStep(
        "inbounds",
        "admin_store_setup_wizard_inbounds",
        "Inboundها",
        "Inbound آماده فروش را به پنل متصل کن.",
        "inbounds",
    ),
    WizardStep(
        "plans",
        "admin_store_setup_wizard_plans",
        "پلن‌ها",
        "پلن قابل فروش با حجم، مدت و قیمت بساز.",
        "plans",
    ),
    WizardStep(
        "routes",
        "admin_store_setup_wizard_routes",
        "Route پلن",
        "پلن فروش را به inbound مقصد وصل کن.",
        "plan_routes",
    ),
    WizardStep(
        "review",
        "admin_store_setup_wizard_review",
        "بررسی نهایی",
        "وضعیت نصب، هشدارها و Revenue Engine dry-run را مرور کن.",
        "revenue_engine",
        template="admin/store/setup_wizard/review.html",
    ),
)

WIZARD_STEP_BY_SLUG = {step.slug: step for step in WIZARD_STEPS}
WIZARD_STEP_ORDER = [step.slug for step in WIZARD_STEPS]


def wizard_index_url(store=None):
    url = reverse("admin_store_setup_wizard")
    if store and getattr(store, "pk", None):
        return f"{url}?store={store.pk}"
    return url


def wizard_step_url(slug, store=None):
    step = WIZARD_STEP_BY_SLUG[slug]
    url = reverse(step.url_name)
    if store and getattr(store, "pk", None):
        return f"{url}?store={store.pk}"
    return url


def next_step_slug(slug):
    try:
        index = WIZARD_STEP_ORDER.index(slug)
    except ValueError:
        return WIZARD_STEP_ORDER[0]
    if index + 1 >= len(WIZARD_STEP_ORDER):
        return ""
    return WIZARD_STEP_ORDER[index + 1]


def previous_step_slug(slug):
    try:
        index = WIZARD_STEP_ORDER.index(slug)
    except ValueError:
        return ""
    if index <= 0:
        return ""
    return WIZARD_STEP_ORDER[index - 1]


def get_skipped_steps(request):
    return set(request.session.get(SKIPPED_SESSION_KEY, []))


def mark_step_skipped(request, slug):
    skipped = get_skipped_steps(request)
    skipped.add(slug)
    request.session[SKIPPED_SESSION_KEY] = sorted(skipped)
    request.session.modified = True


def clear_step_skipped(request, slug):
    skipped = get_skipped_steps(request)
    if slug in skipped:
        skipped.remove(slug)
        request.session[SKIPPED_SESSION_KEY] = sorted(skipped)
        request.session.modified = True


def selected_store_from_id(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def setup_status_by_key(store):
    return {card.key: card for card in build_setup_cards(store)}


def status_for_step(step, store, skipped_steps):
    if step.slug == "review":
        cards = build_setup_cards(store)
        if any(card.status == "error" for card in cards):
            return "warning", "نیاز به بررسی"
        if any(card.status in {"missing", "warning"} for card in cards):
            return "warning", "هشدار"
        return "done", "آماده"

    card = setup_status_by_key(store).get(step.setup_card_key)
    status = getattr(card, "status", "missing")
    label = getattr(card, "status_label", "تکمیل نشده")
    if step.slug in skipped_steps and status != "done":
        return "skipped", "بعداً انجام می‌دهم"
    if status == "safe":
        return "done", "ایمن / اختیاری"
    if status == "error":
        return "warning", "نیاز به اصلاح"
    return status, label


def step_contexts(store, skipped_steps):
    contexts = []
    for step in WIZARD_STEPS:
        status, status_label = status_for_step(step, store, skipped_steps)
        contexts.append(
            {
                "step": step,
                "status": status,
                "status_label": status_label,
                "url": wizard_step_url(step.slug, store),
            }
        )
    return contexts


def progress_percent(step_context_list):
    setup_steps = [item for item in step_context_list if item["step"].slug != "review"]
    if not setup_steps:
        return 0
    done = sum(1 for item in setup_steps if item["status"] == "done")
    return int((done / len(setup_steps)) * 100)


def get_setup_wizard_context(request, selected_store_id=None):
    stores, store = selected_store_from_id(selected_store_id)
    skipped = get_skipped_steps(request)
    steps = step_contexts(store, skipped)
    return {
        "stores": stores,
        "selected_store": store,
        "wizard_steps": steps,
        "progress_percent": progress_percent(steps),
        "start_url": first_action_step_url(store, steps),
    }


def first_action_step_url(store, steps=None):
    steps = steps or step_contexts(store, set())
    for item in steps:
        if item["step"].slug != "review" and item["status"] != "done":
            return item["url"]
    return wizard_step_url("review", store)


def get_telegram_config(store):
    return active_telegram_configs(store).first()


def get_panel(store):
    queryset = Panel.objects.order_by("-is_active", "pk")
    if store:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.first()


def get_inbound(store):
    queryset = Inbound.objects.select_related("panel").order_by("-is_active", "pk")
    if store:
        queryset = queryset.filter(Q(panel__store=store) | Q(panel__store__isnull=True))
    return queryset.first()


def get_plan(store):
    queryset = Plan.objects.filter(is_custom_volume=False).order_by("-is_active", "-is_public", "sort_order", "pk")
    if store:
        queryset = queryset.filter(Q(store=store) | Q(store__isnull=True))
    return queryset.first()


def get_route(store):
    return route_queryset_for_store(store).filter(is_active=True).order_by("priority", "pk").first() if store else None


def get_step_form(slug, request=None, store=None, data=None):
    if slug == "store":
        return StoreIdentitySetupForm(data=data, store=store)
    if slug == "payment":
        return PaymentSetupForm(data=data, store=store)
    if slug == "telegram":
        return TelegramSetupForm(data=data, store=store, bot_config=get_telegram_config(store))
    if slug == "telegram-proxy":
        proxy_url = telegram_proxy_url()
        return TelegramProxySetupForm(
            data=data,
            initial={
                "proxy_enabled": bool(proxy_url),
                "proxy_url": sanitized_telegram_proxy_url(proxy_url) if proxy_url else "",
            },
        )
    if slug == "panel":
        return PanelSetupForm(data=data, store=store, panel=get_panel(store))
    if slug == "inbounds":
        return InboundSetupForm(data=data, store=store, inbound=get_inbound(store))
    if slug == "plans":
        return PlanSetupForm(data=data, store=store, plan=get_plan(store))
    if slug == "routes":
        return PlanRouteSetupForm(data=data, store=store, route=get_route(store))
    raise KeyError(slug)


def save_step_form(slug, form):
    return form.save()


def get_step_page_context(request, slug, selected_store_id=None, form=None):
    stores, store = selected_store_from_id(selected_store_id)
    step = WIZARD_STEP_BY_SLUG[slug]
    skipped = get_skipped_steps(request)
    steps = step_contexts(store, skipped)
    previous_slug = previous_step_slug(slug)
    next_slug = next_step_slug(slug)
    return {
        "stores": stores,
        "selected_store": store,
        "wizard_steps": steps,
        "progress_percent": progress_percent(steps),
        "current_step": step,
        "current_status": status_for_step(step, store, skipped)[0],
        "current_status_label": status_for_step(step, store, skipped)[1],
        "form": form if form is not None else get_step_form(slug, request=request, store=store),
        "previous_url": wizard_step_url(previous_slug, store) if previous_slug else wizard_index_url(store),
        "next_url": wizard_step_url(next_slug, store) if next_slug else reverse("admin_store_owner_dashboard"),
        "can_skip": slug != "store",
    }


def get_review_context(request, selected_store_id=None):
    stores, store = selected_store_from_id(selected_store_id)
    skipped = get_skipped_steps(request)
    steps = step_contexts(store, skipped)
    cards = build_setup_cards(store)
    remaining_cards = [card for card in cards if card.status in {"error", "missing", "warning"}]
    revenue_status = "dry-run امن" if store and store.revenue_engine_dry_run else "ارسال واقعی یا غیرفعال"
    return {
        "stores": stores,
        "selected_store": store,
        "wizard_steps": steps,
        "progress_percent": progress_percent(steps),
        "current_step": WIZARD_STEP_BY_SLUG["review"],
        "setup_cards": cards,
        "remaining_cards": remaining_cards,
        "ready": not remaining_cards,
        "revenue_status": revenue_status,
        "revenue_engine_enabled": bool(store and store.revenue_engine_enabled),
        "revenue_engine_dry_run": bool(store and store.revenue_engine_dry_run),
        "previous_url": wizard_step_url(previous_step_slug("review"), store),
        "dashboard_url": reverse("admin_store_owner_dashboard"),
        "setup_center_url": reverse("admin_store_setup_center"),
        "doctor_command": "/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail",
    }
