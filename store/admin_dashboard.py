from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from payments.models import IncomingPaymentSMS

from .admin_setup import (
    active_panels,
    active_sales_inbounds,
    active_sellable_plans,
    active_telegram_configs,
    add_query,
    build_setup_cards,
    changelist_url,
    missing_route_labels,
    setup_wizard_index_url,
    setup_wizard_step_url,
    setup_wizard_url_for_card_key,
    store_change_url,
)
from .admin_support_services import support_workbench_url
from .models import BotConfiguration, Order, PanelHealthCheckLog, PanelHealthStatus, Plan, RevenueOfferLog, Store, SupportConversation, VPNClient


PENDING_ORDER_STATUSES = (
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
)
REVENUE_ORDER_STATUSES = (Order.Status.COMPLETED,)
PENDING_RECEIPT_STATUSES = (Order.Status.PENDING_VERIFICATION, Order.Status.CONFIRMED)


@dataclass(frozen=True)
class DashboardLink:
    label: str
    url: str = ""
    command: str = ""
    tone: str = "primary"


@dataclass(frozen=True)
class MetricCard:
    key: str
    title: str
    value: str
    subtitle: str
    tone: str = "info"
    details: list[str] = field(default_factory=list)
    links: list[DashboardLink] = field(default_factory=list)


@dataclass(frozen=True)
class ActionItem:
    title: str
    description: str
    tone: str
    url: str = ""
    command: str = ""


def admin_url(name, *args):
    return reverse(f"admin:{name}", args=args)


def local_day_bounds(day=None):
    current_day = day or timezone.localdate()
    start = timezone.make_aware(datetime.combine(current_day, time.min), timezone.get_current_timezone())
    end = start + timedelta(days=1)
    return start, end


def selected_store_from_id(selected_store_id=None):
    stores = list(Store.objects.order_by("-is_active", "name", "pk"))
    selected_store = None
    if selected_store_id:
        selected_store = next((store for store in stores if str(store.pk) == str(selected_store_id)), None)
    if not selected_store:
        selected_store = next((store for store in stores if store.is_active), None) or (stores[0] if stores else None)
    return stores, selected_store


def for_store(queryset, store, field="store"):
    if store and store.pk:
        return queryset.filter(**{f"{field}_id": store.pk})
    return queryset


def format_count(value, singular="", plural=""):
    label = singular if value == 1 else plural
    return f"{value:,} {label}".strip()


def format_money(amount, currency):
    labels = {
        Plan.Currency.TOMAN: "تومان",
        Plan.Currency.IRR: "ریال",
        Plan.Currency.USD: "USD",
    }
    return f"{int(amount or 0):,} {labels.get(currency, currency)}"


def revenue_by_currency(queryset):
    rows = queryset.values("currency").annotate(total=Sum("amount"), count=Count("id")).order_by("currency")
    by_currency = {row["currency"]: int(row["total"] or 0) for row in rows}
    display = [format_money(total, currency) for currency, total in by_currency.items()]
    return {
        "by_currency": by_currency,
        "display": display or ["0 تومان"],
        "order_count": sum(int(row["count"] or 0) for row in rows),
    }


def order_changelist(params=None):
    return add_query(admin_url("store_order_changelist"), params or {})


def order_workbench_url(section="", store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    if section:
        params["section"] = section
    url = reverse("admin_store_order_workbench")
    if params:
        url = add_query(url, params)
    if section:
        url = f"{url}#{section}"
    return url


def service_workbench_url(section="", store=None):
    params = {}
    if store and getattr(store, "pk", None):
        params["store"] = store.pk
    if section:
        params["section"] = section
    url = reverse("admin_store_service_workbench")
    if params:
        url = add_query(url, params)
    if section:
        url = f"{url}#{section}"
    return url


def vpn_client_changelist(params=None):
    return add_query(admin_url("store_vpnclient_changelist"), params or {})


def get_support_metrics(store=None):
    today_start, today_end = local_day_bounds()
    conversations = for_store(SupportConversation.objects.all(), store)
    open_count = conversations.exclude(status=SupportConversation.Status.CLOSED).count()
    needs_reply_count = conversations.filter(status=SupportConversation.Status.WAITING_ADMIN).count()
    today_count = conversations.filter(updated_at__gte=today_start, updated_at__lt=today_end).count()
    problematic_count = (
        conversations.annotate(
            telegram_target_count=Count(
                "customer__bot_users",
                filter=Q(
                    customer__bot_users__is_active=True,
                    customer__bot_users__bot_config__is_active=True,
                    customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                )
                & ~Q(customer__bot_users__chat_id=""),
                distinct=True,
            )
        )
        .filter(Q(customer__isnull=True) | Q(telegram_target_count=0))
        .count()
    )
    return {
        "open": open_count,
        "needs_reply": needs_reply_count,
        "today": today_count,
        "problematic": problematic_count,
        "card": MetricCard(
            key="support",
            title="پشتیبانی",
            value=format_count(open_count),
            subtitle=f"{needs_reply_count:,} گفتگو نیازمند پاسخ",
            tone="warning" if needs_reply_count else ("info" if open_count else "success"),
            details=[
                f"امروز: {today_count:,}",
                f"مشکل‌دار: {problematic_count:,}",
            ],
            links=[
                DashboardLink("میز کار پشتیبانی", support_workbench_url(store=store), tone="warning" if needs_reply_count else "primary"),
                DashboardLink("نیازمند پاسخ", support_workbench_url("needs-reply", store), tone="warning"),
            ],
        ),
    }


def get_order_metrics(store=None):
    today_start, today_end = local_day_bounds()
    orders = for_store(Order.objects.all(), store)
    today_orders = orders.filter(created_at__gte=today_start, created_at__lt=today_end)

    pending_count = orders.filter(status__in=PENDING_ORDER_STATUSES).count()
    completed_today = today_orders.filter(status=Order.Status.COMPLETED, is_paid=True).count()
    rejected_today = today_orders.filter(status__in=[Order.Status.REJECTED, Order.Status.CANCELLED]).count()

    return {
        "today_total": today_orders.count(),
        "pending": pending_count,
        "completed_today": completed_today,
        "rejected_today": rejected_today,
        "card": MetricCard(
            key="orders_today",
            title="سفارش‌های امروز",
            value=format_count(today_orders.count()),
            subtitle=f"{pending_count:,} سفارش در انتظار پیگیری",
            tone="warning" if pending_count else "success",
            details=[
                f"تکمیل‌شده امروز: {completed_today:,}",
                f"رد/لغو امروز: {rejected_today:,}",
            ],
            links=[
                DashboardLink("میز کار سفارش‌ها", order_workbench_url("today", store)),
                DashboardLink("سفارش‌های در انتظار", order_workbench_url("needs-review", store), tone="warning"),
                DashboardLink("همه سفارش‌ها", admin_url("store_order_changelist"), tone="secondary"),
            ],
        ),
    }


def get_payment_metrics(store=None):
    today_start, today_end = local_day_bounds()
    orders = for_store(Order.objects.all(), store)
    pending_receipts = orders.filter(
        payment_method=Order.PaymentMethod.MANUAL_CARD,
        verification_status=Order.VerificationStatus.PENDING,
        status__in=PENDING_RECEIPT_STATUSES,
    ).filter(Q(payment_receipt_image__isnull=False) | Q(payment_submitted_at__isnull=False) | Q(is_paid=True))
    confirmed_today = orders.filter(
        verification_status=Order.VerificationStatus.VERIFIED,
        verified_at__gte=today_start,
        verified_at__lt=today_end,
    )
    pending_sms = IncomingPaymentSMS.objects.filter(
        status__in=[IncomingPaymentSMS.Status.NEW, IncomingPaymentSMS.Status.MATCHED, IncomingPaymentSMS.Status.NO_MATCH]
    ).count()

    pending_receipt_count = pending_receipts.count()
    confirmed_today_count = confirmed_today.count()
    return {
        "pending_receipts": pending_receipt_count,
        "confirmed_today": confirmed_today_count,
        "pending_sms": pending_sms,
        "card": MetricCard(
            key="payments",
            title="پرداخت و رسیدها",
            value=format_count(pending_receipt_count),
            subtitle="رسید منتظر بررسی",
            tone="warning" if pending_receipt_count else "success",
            details=[
                f"پرداخت تاییدشده امروز: {confirmed_today_count:,}",
                f"SMS پرداخت نیازمند بررسی: {pending_sms:,}",
            ],
            links=[
                DashboardLink(
                    "بررسی رسیدها",
                    order_workbench_url("needs-review", store),
                ),
                DashboardLink("گزارش کارت‌ها", reverse("admin_card_receipts_report"), tone="secondary"),
            ],
        ),
    }


def get_revenue_metrics(store=None):
    today_start, today_end = local_day_bounds()
    week_start = timezone.now() - timedelta(days=7)
    orders = for_store(Order.objects.filter(is_paid=True, status__in=REVENUE_ORDER_STATUSES), store)
    today = revenue_by_currency(orders.filter(created_at__gte=today_start, created_at__lt=today_end))
    week = revenue_by_currency(orders.filter(created_at__gte=week_start))
    return {
        "today": today,
        "week": week,
        "card": MetricCard(
            key="revenue",
            title="درآمد",
            value="، ".join(today["display"]),
            subtitle="درآمد امروز بر اساس orderهای paid/completed",
            tone="success" if today["order_count"] else "info",
            details=[
                "۷ روز اخیر: " + "، ".join(week["display"]),
                f"تعداد سفارش تکمیل‌شده ۷ روز اخیر: {week['order_count']:,}",
            ],
            links=[DashboardLink("سفارش‌های تکمیل‌شده", order_changelist({"status__exact": Order.Status.COMPLETED}))],
        ),
    }


def get_client_metrics(store=None):
    now = timezone.now()
    soon = now + timedelta(days=3)
    clients = for_store(VPNClient.objects.all(), store)
    active = clients.filter(status=VPNClient.Status.ACTIVE)
    expiring = active.filter(expires_at__gte=now, expires_at__lte=soon)
    expired = clients.filter(Q(status=VPNClient.Status.EXPIRED) | Q(status=VPNClient.Status.ACTIVE, expires_at__lt=now))
    needs_attention = clients.filter(
        Q(status__in=[VPNClient.Status.ERROR, VPNClient.Status.CREATED])
        | Q(inbound__isnull=True)
        | Q(inbound__panel__isnull=True)
        | Q(inbound__is_active=False)
        | Q(inbound__panel__is_active=False)
        | Q(order__metadata__panel_provisioning_deferred=True)
    ).distinct()
    needs_telegram = (
        clients.filter(order__customer__isnull=False)
        .annotate(
            telegram_target_count=Count(
                "order__customer__bot_users",
                filter=Q(
                    order__customer__bot_users__is_active=True,
                    order__customer__bot_users__bot_config__is_active=True,
                    order__customer__bot_users__bot_config__provider=BotConfiguration.Provider.TELEGRAM,
                ),
                distinct=True,
            )
        )
        .filter(telegram_target_count=0)
    )
    low_traffic = 0
    for limit, used in active.exclude(traffic_limit_bytes=0).values_list("traffic_limit_bytes", "used_traffic_bytes"):
        remaining = max(int(limit or 0) - int(used or 0), 0)
        if limit and remaining <= int(limit) * 0.2:
            low_traffic += 1

    active_count = active.count()
    expiring_count = expiring.count()
    expired_count = expired.count()
    attention_count = needs_attention.count()
    needs_telegram_count = needs_telegram.count()
    return {
        "active": active_count,
        "expiring_soon": expiring_count,
        "expired": expired_count,
        "low_traffic": low_traffic,
        "needs_attention": attention_count,
        "needs_telegram": needs_telegram_count,
        "card": MetricCard(
            key="active_services",
            title="سرویس‌های فعال",
            value=format_count(active_count),
            subtitle=f"{expiring_count:,} سرویس تا ۳ روز آینده منقضی می‌شود",
            tone="warning" if expiring_count or low_traffic or needs_telegram_count else "success",
            details=[
                f"کم‌ترافیک: {low_traffic:,}",
                f"منقضی/نیازمند رسیدگی: {expired_count:,}",
                f"بدون مقصد تلگرام: {needs_telegram_count:,}",
                f"خطادار/route ناقص: {attention_count:,}",
            ],
            links=[
                DashboardLink("میز کار سرویس‌ها", service_workbench_url("active", store)),
                DashboardLink("در حال انقضا", service_workbench_url("expiring", store), tone="warning"),
                DashboardLink("همه سرویس‌ها", admin_url("store_vpnclient_changelist"), tone="secondary"),
            ],
        ),
    }


def get_panel_health_summary(store=None):
    panels = active_panels(store)
    panel_count = panels.count()
    latest_status = PanelHealthStatus.objects.filter(panel__in=panels).select_related("panel").order_by("-last_checked_at", "-updated_at").first()
    latest_log = PanelHealthCheckLog.objects.filter(panel__in=panels).select_related("panel").order_by("-checked_at").first()
    error_count = PanelHealthStatus.objects.filter(panel__in=panels, status=PanelHealthStatus.Status.ERROR).count()
    warning_count = PanelHealthStatus.objects.filter(panel__in=panels, status=PanelHealthStatus.Status.WARNING).count()
    sales_inbound_count = active_sales_inbounds(store).count()

    if not panel_count:
        tone = "warning"
        value = "پنل ندارد"
        subtitle = "برای فروش باید پنل X-UI/Sanaei تنظیم شود"
    elif error_count:
        tone = "error"
        value = f"{error_count:,} خطا"
        subtitle = "آخرین health DB خطا دارد"
    elif warning_count:
        tone = "warning"
        value = f"{warning_count:,} هشدار"
        subtitle = "آخرین health DB نیاز به بررسی دارد"
    elif latest_status:
        tone = "success" if latest_status.status == PanelHealthStatus.Status.OK else "info"
        value = latest_status.get_status_display()
        subtitle = f"آخرین وضعیت: {latest_status.panel.name}"
    else:
        tone = "warning"
        value = "نامشخص"
        subtitle = "هنوز health check ذخیره نشده است"

    details = [
        f"پنل فعال: {panel_count:,}",
        f"Inbound آماده فروش: {sales_inbound_count:,}",
    ]
    if latest_log:
        details.append(f"آخرین log: {latest_log.get_status_display()} در {timezone.localtime(latest_log.checked_at):%Y-%m-%d %H:%M}")

    return {
        "panel_count": panel_count,
        "error_count": error_count,
        "warning_count": warning_count,
        "latest_status": latest_status,
        "card": MetricCard(
            key="panel_health",
            title="سلامت پنل",
            value=value,
            subtitle=subtitle,
            tone=tone,
            details=details,
            links=[
                DashboardLink("مدیریت Panel", changelist_url("store_panel_changelist", store)),
                DashboardLink("Health logs", changelist_url("store_panelhealthchecklog_changelist", store, store_filter="panel__store__id__exact"), tone="secondary"),
            ],
        ),
    }


def get_telegram_summary(store=None):
    configs = list(active_telegram_configs(store))
    username_count = sum(1 for config in configs if (config.telegram_bot_username or "").strip())
    admin_ready_count = sum(1 for config in configs if config.get_admin_user_ids())
    token_ready_count = sum(1 for config in configs if (config.bot_token or "").strip())
    active_count = len(configs)

    if not active_count:
        tone = "warning"
        value = "تنظیم نشده"
        subtitle = "BotConfiguration فعال وجود ندارد"
    elif token_ready_count and admin_ready_count:
        tone = "success"
        value = f"{active_count:,} فعال"
        subtitle = "token و admin IDs در DB تنظیم شده‌اند"
    else:
        tone = "warning"
        value = "ناقص"
        subtitle = "token یا admin IDs کامل نیست"

    return {
        "active": active_count,
        "username_configured": username_count,
        "admin_ready": admin_ready_count,
        "token_ready": token_ready_count,
        "card": MetricCard(
            key="telegram",
            title="ربات تلگرام",
            value=value,
            subtitle=subtitle,
            tone=tone,
            details=[
                f"username تنظیم‌شده: {username_count:,}",
                f"admin IDs آماده: {admin_ready_count:,}",
            ],
            links=[DashboardLink("مدیریت BotConfiguration", changelist_url("store_botconfiguration_changelist", store))],
        ),
    }


def get_revenue_engine_summary(store=None):
    week_start = timezone.now() - timedelta(days=7)
    logs = RevenueOfferLog.objects.filter(created_at__gte=week_start)
    if store and store.pk:
        logs = logs.filter(Q(store=store) | Q(store__isnull=True))

    status_counts = {row["status"]: row["count"] for row in logs.values("status").annotate(count=Count("id"))}
    store_enabled = bool(store and store.revenue_engine_enabled)
    store_dry_run = bool(store and store.revenue_engine_dry_run)

    if not store:
        tone = "warning"
        value = "Store ندارد"
        subtitle = "برای Revenue Engine ابتدا Store بساز"
    elif not store_enabled:
        tone = "warning"
        value = "غیرفعال"
        subtitle = "Revenue Engine خاموش است"
    elif store_dry_run:
        tone = "safe"
        value = "Dry-run"
        subtitle = "پیام واقعی ارسال نمی‌شود"
    else:
        tone = "warning"
        value = "Real send"
        subtitle = "ارسال واقعی فعال است"

    return {
        "enabled": store_enabled,
        "dry_run": store_dry_run,
        "status_counts": status_counts,
        "card": MetricCard(
            key="revenue_engine",
            title="Revenue Engine",
            value=value,
            subtitle=subtitle,
            tone=tone,
            details=[
                f"sent در ۷ روز: {status_counts.get(RevenueOfferLog.Status.SENT, 0):,}",
                f"dry-run در ۷ روز: {status_counts.get(RevenueOfferLog.Status.DRY_RUN, 0):,}",
                f"failed در ۷ روز: {status_counts.get(RevenueOfferLog.Status.FAILED, 0):,}",
            ],
            links=[
                DashboardLink(
                    "کنترل درآمد هوشمند",
                    add_query(reverse("admin_store_revenue_control"), {"store": getattr(store, "pk", None)}),
                    tone="primary",
                ),
                DashboardLink("RevenueOfferLog", changelist_url("store_revenueofferlog_changelist", store), tone="secondary"),
            ],
        ),
    }


def get_setup_summary(store=None):
    cards = build_setup_cards(store)
    blocking_cards = [card for card in cards if card.status in {"error", "missing"}]
    warning_cards = [card for card in cards if card.status == "warning"]
    if blocking_cards:
        tone = "error"
        value = "ناقص"
        subtitle = "چند مورد راه‌اندازی باید تکمیل شود"
    elif warning_cards:
        tone = "warning"
        value = "نیاز به بررسی"
        subtitle = "نصب قابل ادامه است، اما چند هشدار دارد"
    else:
        tone = "success"
        value = "آماده"
        subtitle = "تنظیمات اصلی کامل به نظر می‌رسد"

    return {
        "cards": cards,
        "blocking_cards": blocking_cards,
        "warning_cards": warning_cards,
        "card": MetricCard(
            key="setup_status",
            title="وضعیت راه‌اندازی",
            value=value,
            subtitle=subtitle,
            tone=tone,
            details=[f"{len(blocking_cards):,} مورد ناقص/خطا", f"{len(warning_cards):,} هشدار"],
            links=[
                DashboardLink("Setup Wizard", setup_wizard_index_url(store)),
                DashboardLink("Setup Center", add_query(reverse("admin_store_setup_center"), {"store": getattr(store, "pk", None)}), tone="secondary"),
            ],
        ),
    }


def get_action_items(store, setup_summary, order_metrics, payment_metrics, client_metrics, panel_summary, telegram_summary, support_metrics=None):
    items = []
    if support_metrics and support_metrics.get("needs_reply"):
        items.append(
            ActionItem(
                "پیام‌های پشتیبانی را پاسخ بده",
                f"{support_metrics['needs_reply']:,} گفتگو منتظر پاسخ owner است.",
                "warning",
                support_workbench_url("needs-reply", store),
            )
        )
    if payment_metrics["pending_receipts"]:
        items.append(
            ActionItem(
                "رسیدهای پرداخت را بررسی کن",
                f"{payment_metrics['pending_receipts']:,} رسید منتظر تایید است.",
                "warning",
                order_workbench_url("needs-review", store),
            )
        )
    missing_routes = missing_route_labels(store) if store else []
    if missing_routes:
        items.append(
            ActionItem(
                "Route پلن‌ها ناقص است",
                f"{len(missing_routes):,} پلن/اپراتور route معتبر ندارد.",
                "error",
                setup_wizard_step_url("routes", store),
            )
        )
    if not panel_summary["panel_count"]:
        items.append(
            ActionItem(
                "پنل X-UI/Sanaei را اضافه کن",
                "هیچ پنل فعالی برای فروشگاه پیدا نشد.",
                "warning",
                setup_wizard_step_url("panel", store),
            )
        )
    if not telegram_summary["active"] or not telegram_summary["admin_ready"] or not telegram_summary["token_ready"]:
        items.append(
            ActionItem(
                "BotConfiguration تلگرام را کامل کن",
                "ربات فعال، token یا admin IDs کامل نیست.",
                "warning",
                setup_wizard_step_url("telegram", store),
            )
        )
    if client_metrics["expiring_soon"]:
        items.append(
            ActionItem(
                "سرویس‌های در حال انقضا را پیگیری کن",
                f"{client_metrics['expiring_soon']:,} سرویس تا ۳ روز آینده منقضی می‌شود.",
                "warning",
                service_workbench_url("expiring", store),
            )
        )
    if client_metrics.get("needs_telegram") and len(items) < 5:
        items.append(
            ActionItem(
                "مشتری‌های بدون مقصد تلگرام",
                f"{client_metrics['needs_telegram']:,} سرویس resend امن ندارد چون Telegram target ثبت نشده است.",
                "warning",
                service_workbench_url("needs-telegram", store),
            )
        )
    if client_metrics.get("needs_attention") and len(items) < 5:
        items.append(
            ActionItem(
                "سرویس‌های نیازمند بررسی",
                f"{client_metrics['needs_attention']:,} سرویس خطا یا route/panel/inbound ناقص دارد.",
                "error",
                service_workbench_url("attention", store),
            )
        )
    if setup_summary["blocking_cards"] and len(items) < 5:
        first = setup_summary["blocking_cards"][0]
        items.append(
            ActionItem(
                "Setup Center را تکمیل کن",
                f"بخش «{first.title}» هنوز کامل نیست.",
                "error" if first.status == "error" else "warning",
                setup_wizard_url_for_card_key(first.key, store),
            )
        )
    if len(items) < 5:
        items.append(
            ActionItem(
                "Doctor غیرزنده را در سرور اجرا کن",
                "Dashboard جایگزین doctor نیست؛ این command live call انجام نمی‌دهد مگر flag صریح بدهی.",
                "info",
                command="/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail",
            )
        )
    return items[:5]


def get_quick_actions(store=None):
    return [
        DashboardLink("میز کار سفارش‌ها", order_workbench_url(store=store)),
        DashboardLink("میز کار سرویس‌ها", service_workbench_url(store=store)),
        DashboardLink("میز کار پشتیبانی", support_workbench_url(store=store)),
        DashboardLink("سفارش‌های در انتظار", order_workbench_url("needs-review", store), tone="warning"),
        DashboardLink("بررسی رسیدها", order_workbench_url("needs-review", store), tone="warning"),
        DashboardLink("Setup Wizard", setup_wizard_index_url(store)),
        DashboardLink("Setup Center", add_query(reverse("admin_store_setup_center"), {"store": getattr(store, "pk", None)})),
        DashboardLink("مدیریت پلن‌ها", changelist_url("store_plan_changelist", store)),
        DashboardLink("مدیریت routeها", changelist_url("store_planinboundroute_changelist", store)),
        DashboardLink("BotConfiguration", changelist_url("store_botconfiguration_changelist", store)),
        DashboardLink("Panel", changelist_url("store_panel_changelist", store)),
        DashboardLink("RevenueOfferLog", changelist_url("store_revenueofferlog_changelist", store)),
        DashboardLink("Doctor command", command="/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail", tone="secondary"),
    ]


def get_owner_dashboard_context(user=None, selected_store_id=None):
    stores, selected_store = selected_store_from_id(selected_store_id)
    setup_summary = get_setup_summary(selected_store)
    order_metrics = get_order_metrics(selected_store)
    payment_metrics = get_payment_metrics(selected_store)
    revenue_metrics = get_revenue_metrics(selected_store)
    client_metrics = get_client_metrics(selected_store)
    panel_summary = get_panel_health_summary(selected_store)
    telegram_summary = get_telegram_summary(selected_store)
    revenue_engine_summary = get_revenue_engine_summary(selected_store)
    support_metrics = get_support_metrics(selected_store)

    metric_cards = [
        setup_summary["card"],
        order_metrics["card"],
        support_metrics["card"],
        payment_metrics["card"],
        revenue_metrics["card"],
        client_metrics["card"],
        panel_summary["card"],
        telegram_summary["card"],
        revenue_engine_summary["card"],
    ]
    return {
        "stores": stores,
        "selected_store": selected_store,
        "metric_cards": metric_cards,
        "setup_summary": setup_summary,
        "order_metrics": order_metrics,
        "payment_metrics": payment_metrics,
        "revenue_metrics": revenue_metrics,
        "client_metrics": client_metrics,
        "panel_summary": panel_summary,
        "telegram_summary": telegram_summary,
        "revenue_engine_summary": revenue_engine_summary,
        "support_metrics": support_metrics,
        "action_items": get_action_items(
            selected_store,
            setup_summary,
            order_metrics,
            payment_metrics,
            client_metrics,
            panel_summary,
            telegram_summary,
            support_metrics,
        ),
        "quick_actions": get_quick_actions(selected_store),
        "generated_at": timezone.now(),
    }
