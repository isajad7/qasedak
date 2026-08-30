from dataclasses import dataclass

from django.db.models import Q

from .models import BotConfiguration, Inbound, Panel, Plan, PlanInboundRoute, Store


SETUP_NOT_READY_MESSAGE = "فروشگاه هنوز کامل راه‌اندازی نشده است. لطفاً بعد از تکمیل setup دوباره تلاش کنید."
SAFE_PLACEHOLDER_CARD_NUMBER = "0000000000000000"
SAFE_PLACEHOLDER_CARD_OWNER = "Configure Payment Owner"


@dataclass(frozen=True)
class ReadinessCheck:
    key: str
    label: str
    passed: bool
    message: str


def store_is_setup_blocked(store):
    if not store:
        return True
    return getattr(store, "setup_status", "") in {
        Store.SetupStatus.PROVISIONED,
        Store.SetupStatus.SETUP_REQUIRED,
        Store.SetupStatus.TELEGRAM_CONFIGURED,
        Store.SetupStatus.PAYMENT_CONFIGURED,
        Store.SetupStatus.XUI_CONFIGURED,
        Store.SetupStatus.PLANS_CONFIGURED,
        Store.SetupStatus.SUSPENDED,
        Store.SetupStatus.ERROR,
    }


def store_is_sellable(store):
    return bool(
        store
        and store.is_active
        and getattr(store, "setup_status", Store.SetupStatus.READY) == Store.SetupStatus.READY
    )


def has_real_payment_card(store):
    number = str(getattr(store, "card_number", "") or "").strip()
    owner = str(getattr(store, "card_owner", "") or "").strip()
    return (
        bool(number)
        and bool(owner)
        and number != SAFE_PLACEHOLDER_CARD_NUMBER
        and owner != SAFE_PLACEHOLDER_CARD_OWNER
    )


def telegram_configured(store):
    return BotConfiguration.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        provider=BotConfiguration.Provider.TELEGRAM,
        is_active=True,
    ).exclude(bot_token="").filter(admin_user_id__gt="").exists()


def payment_configured(store):
    return bool(store and has_real_payment_card(store))


def xui_configured(store):
    panels = Panel.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        is_active=True,
    ).exclude(url="").exclude(password="")
    return panels.filter(
        Q(family=Panel.Family.PASARGUARD)
        | Q(family=Panel.Family.XUI, username__gt="")
        | Q(family="")
    ).exists()


def inbound_configured(store):
    return Inbound.objects.filter(
        Q(panel__store=store) | Q(panel__store__isnull=True),
        panel__is_active=True,
        is_active=True,
        available_for_new_orders=True,
    ).exists()


def active_public_plans(store):
    plans = Plan.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        is_active=True,
        is_public=True,
        is_custom_volume=False,
    )
    return plans


def plans_configured(store):
    return active_public_plans(store).exists()


def legacy_routes_configured(store):
    routes = PlanInboundRoute.objects.filter(
        Q(store=store) | Q(store__isnull=True),
        Q(plan__store=store) | Q(plan__store__isnull=True),
        Q(inbound__panel__store=store) | Q(inbound__panel__store__isnull=True),
        is_active=True,
        plan__is_active=True,
        plan__is_public=True,
        plan__is_custom_volume=False,
        inbound__is_active=True,
        inbound__available_for_new_orders=True,
        inbound__panel__is_active=True,
    )
    return routes.exists()


def plan_has_ready_canonical_delivery(plan, store=None):
    from .plan_delivery_services import (
        MODE_DIRECT_LINKS,
        MODE_SUBSCRIPTION,
        READINESS_CONFLICT,
        READINESS_INCOMPLETE,
        SOURCE_V2_CONFIG,
        resolve_plan_delivery_configuration,
    )

    if not plan:
        return False
    delivery = resolve_plan_delivery_configuration(plan, store)
    return bool(
        delivery.source_of_truth == SOURCE_V2_CONFIG
        and delivery.effective_mode in {MODE_DIRECT_LINKS, MODE_SUBSCRIPTION}
        and delivery.readiness_status not in {READINESS_INCOMPLETE, READINESS_CONFLICT}
        and delivery.source_count > 0
        and delivery.expected_output_count > 0
    )


def canonical_delivery_configured_count(store):
    return sum(
        1
        for plan in active_public_plans(store).select_related("store").iterator()
        if plan_has_ready_canonical_delivery(plan, store)
    )


def routes_configured(store):
    return legacy_routes_configured(store) or canonical_delivery_configured_count(store) > 0


def revenue_safe(store):
    return bool(store and store.revenue_engine_enabled and store.revenue_engine_dry_run)


def build_store_readiness_checklist(store):
    if not store:
        return [
            ReadinessCheck("store", "Store", False, "Store ساخته نشده است."),
        ]
    return [
        ReadinessCheck(
            "store",
            "Store identity",
            bool(store.name and store.english_name and store.is_active),
            "نام، نام انگلیسی و وضعیت فعال فروشگاه لازم است.",
        ),
        ReadinessCheck("telegram", "Telegram", telegram_configured(store), "ربات Telegram فعال با token و admin ID لازم است."),
        ReadinessCheck("payment", "Payment", payment_configured(store), "اطلاعات کارت واقعی لازم است."),
        ReadinessCheck("xui", "Operational panel", xui_configured(store), "پنل عملیاتی فعال لازم است؛ X-UI به username/password و PasarGuard به API key نیاز دارد."),
        ReadinessCheck("inbound", "Inbound", inbound_configured(store), "حداقل یک inbound فعال و قابل فروش لازم است."),
        ReadinessCheck("plans", "Plans", plans_configured(store), "حداقل یک پلن عمومی فعال لازم است."),
        ReadinessCheck(
            "routes",
            "Plan delivery",
            routes_configured(store),
            "حداقل یک route معتبر یا تنظیم canonical آماده برای تحویل پلن لازم است.",
        ),
        ReadinessCheck("revenue", "Revenue dry-run", revenue_safe(store), "Revenue Engine باید enabled و dry_run باشد."),
    ]


def setup_checklist_passes(store):
    return all(item.passed for item in build_store_readiness_checklist(store))


def setup_readiness_blockers(store):
    return [item for item in build_store_readiness_checklist(store) if not item.passed]


def setup_not_ready_details(store):
    if not store:
        return {
            "setup_status": "",
            "pending": ["store"],
            "reasons": ["store_missing"],
        }

    pending = [item.key for item in setup_readiness_blockers(store)]
    setup_status = getattr(store, "setup_status", "") or ""
    reasons = []
    if not getattr(store, "is_active", False):
        reasons.append("store_inactive")
    if setup_status != Store.SetupStatus.READY:
        reasons.append(f"setup_status={setup_status or 'missing'}")
    if pending and setup_status != Store.SetupStatus.READY:
        reasons.append(f"pending={','.join(pending)}")
    return {
        "setup_status": setup_status,
        "pending": pending,
        "reasons": reasons,
    }


def setup_not_ready_reason(store):
    details = setup_not_ready_details(store)
    return "; ".join(details["reasons"]) or "unknown"


def derive_setup_status(store, *, allow_ready=False):
    if not store:
        return Store.SetupStatus.ERROR
    if store.setup_status in {Store.SetupStatus.SUSPENDED, Store.SetupStatus.ERROR}:
        return store.setup_status

    checklist = {item.key: item.passed for item in build_store_readiness_checklist(store)}
    if all(checklist.values()):
        return Store.SetupStatus.READY if allow_ready else Store.SetupStatus.PLANS_CONFIGURED
    if checklist.get("telegram") and not checklist.get("payment"):
        return Store.SetupStatus.TELEGRAM_CONFIGURED
    if checklist.get("telegram") and checklist.get("payment") and not checklist.get("xui"):
        return Store.SetupStatus.PAYMENT_CONFIGURED
    if checklist.get("telegram") and checklist.get("payment") and checklist.get("xui") and not (
        checklist.get("inbound") and checklist.get("plans") and checklist.get("routes")
    ):
        return Store.SetupStatus.XUI_CONFIGURED
    if checklist.get("telegram") and checklist.get("payment") and checklist.get("xui") and checklist.get("inbound"):
        return Store.SetupStatus.PLANS_CONFIGURED
    return Store.SetupStatus.SETUP_REQUIRED


def sync_store_setup_status(store, *, allow_ready=False, save=True):
    if not store:
        return None
    status = derive_setup_status(store, allow_ready=allow_ready)
    if store.setup_status == Store.SetupStatus.READY and status != Store.SetupStatus.READY and not allow_ready:
        return store.setup_status
    if store.setup_status != status:
        store.setup_status = status
        if save:
            store.save(update_fields=["setup_status", "updated_at"])
    return store.setup_status
