import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.core.cache import cache

from store.jalali import persian_digits
from store.models import VPNClient
from store.referral_services import get_available_referral_gb
from store.vpn_client_management_services import (
    VPNClientManagementError,
    VPNClientPendingRenewalError,
    build_delete_confirmation_text,
    delete_vpn_client_for_user,
)
from store.xui_api import refresh_vpn_client_links, sync_vpn_client_stats

from .config_delivery import send_config_links_message
from .constants import (
    CONFIG_MANAGEMENT_CACHE_SECONDS,
    USER_CLIENT_DELETE_CALLBACK_PREFIX,
    USER_CLIENT_DELETE_CONFIRM_CALLBACK_PREFIX,
)
from .formatting import bot_datetime, bot_gb_from_bytes
from .user_menu import main_menu_keyboard

logger = logging.getLogger(__name__)


BOT_CLIENT_STATUS_LABELS = {
    VPNClient.Status.CREATED: "ساخته شده",
    VPNClient.Status.INACTIVE: "غیرفعال",
    VPNClient.Status.ACTIVE: "فعال",
    VPNClient.Status.SUSPENDED: "متوقف شده",
    VPNClient.Status.EXPIRED: "تمام شده",
    VPNClient.Status.DELETED: "حذف شده",
    VPNClient.Status.ERROR: "خطا",
}


def _usage_percent_from_stats(vpn_client, stats):
    stats = stats or {}
    total = stats.get("total_traffic_bytes") or vpn_client.traffic_limit_bytes or 0
    used = stats.get("used_traffic_bytes") or vpn_client.used_traffic_bytes or 0
    if not total:
        return Decimal("0")
    try:
        return (Decimal(int(used)) * Decimal("100")) / Decimal(int(total))
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return Decimal("0")


def _emit_high_usage_revenue_event(vpn_client, bot_user, stats, *, chat_id):
    usage_percent = _usage_percent_from_stats(vpn_client, stats)
    if usage_percent <= Decimal("80"):
        return None
    try:
        from store.revenue_engine.triggers import HIGH_USAGE_USER, safe_emit_event

        return safe_emit_event(
            HIGH_USAGE_USER,
            vpn_client,
            {
                "bot_user": bot_user,
                "chat_id": chat_id,
                "usage_percent": usage_percent,
                "stats": stats or {},
                "source": "bot_usage_check",
            },
        )
    except Exception as exc:
        logger.warning("Revenue HIGH_USAGE_USER hook skipped vpn_client=%s: %s", vpn_client.pk, exc)
        return None


def bot_client_status(client):
    return BOT_CLIENT_STATUS_LABELS.get(client.status, client.get_status_display())


def bot_client_label(client):
    return client.xui_email or client.username


def client_config_links(vpn_client):
    links = []
    if vpn_client.sub_link:
        links.append(("لینک اشتراک", vpn_client.sub_link))
    if vpn_client.direct_link:
        links.append(("لینک مستقیم", vpn_client.direct_link))
    return links


def bot_subscription_clients(bot_user):
    if not bot_user.customer_id:
        return VPNClient.objects.none()
    return (
        VPNClient.objects.select_related("plan", "order", "inbound", "inbound__panel")
        .filter(
            order__customer=bot_user.customer,
            status__in=[
                VPNClient.Status.ACTIVE,
                VPNClient.Status.INACTIVE,
                VPNClient.Status.CREATED,
                VPNClient.Status.EXPIRED,
            ],
        )
        .order_by("-created_at")
    )


def bot_user_has_available_referral_reward(bot_user):
    if not bot_user.customer_id:
        return False
    try:
        return get_available_referral_gb(bot_user.customer) > 0
    except Exception:
        logger.exception("Could not load referral reward total for bot_user=%s", bot_user.pk)
        return False


def client_has_delete_identifier(client):
    return bool(client and client.inbound_id and (client.uuid or client.xui_email or client.username))


def bot_user_cache_identity(bot_user):
    return bot_user.provider_user_id or bot_user.chat_id or "unknown"


def user_client_delete_cache_key(config, bot_user, token):
    return f"bot:client-delete:{config.pk}:{bot_user_cache_identity(bot_user)}:{token}"


def create_user_client_delete_token(bot_user, vpn_client):
    if not bot_user or not vpn_client or not vpn_client.pk:
        return ""
    token = secrets.token_urlsafe(12).rstrip("=")
    payload = {
        "vpn_client_id": vpn_client.pk,
        "public_id": str(vpn_client.public_id),
    }
    try:
        cache.set(
            user_client_delete_cache_key(bot_user.bot_config, bot_user, token),
            payload,
            CONFIG_MANAGEMENT_CACHE_SECONDS,
        )
    except Exception as exc:
        logger.warning("Could not cache user client delete token: %s", exc)
        return ""
    return token


def get_user_client_delete_payload(config, bot_user, token):
    return cache.get(user_client_delete_cache_key(config, bot_user, token))


def user_client_delete_button(bot_user, vpn_client, *, suffix=""):
    if not client_has_delete_identifier(vpn_client) or vpn_client.is_deleted:
        return None
    token = create_user_client_delete_token(bot_user, vpn_client)
    if not token:
        return None
    return {
        "text": f"حذف کانفیگ {suffix} 🗑".strip(),
        "callback_data": f"{USER_CLIENT_DELETE_CALLBACK_PREFIX}{token}",
    }


def user_client_delete_confirmation_keyboard(token):
    return {
        "inline_keyboard": [
            [{"text": "بله، حذف شود 🗑", "callback_data": f"{USER_CLIENT_DELETE_CONFIRM_CALLBACK_PREFIX}{token}"}],
            [{"text": "انصراف", "callback_data": "user:client_delete_cancel"}],
        ]
    }


def subscription_management_keyboard(bot_user, *, renew_mode=False):
    rows = []
    clients = list(bot_subscription_clients(bot_user)[:10])
    has_referral_reward = bot_user_has_available_referral_reward(bot_user)
    for index, client in enumerate(clients, start=1):
        suffix = persian_digits(index) if len(clients) > 1 else ""
        label = bot_client_label(client)
        if renew_mode:
            rows.append(
                [
                    {
                        "text": f"تمدید {suffix} {label}".strip()[:60],
                        "callback_data": f"user:client_renew:{client.public_id}",
                    }
                ]
            )
        else:
            rows.append(
                [
                    {
                        "text": f"دریافت لینک {suffix}".strip(),
                        "callback_data": f"user:client_config:{client.public_id}",
                    },
                    {
                        "text": f"تمدید {suffix}".strip(),
                        "callback_data": f"user:client_renew:{client.public_id}",
                    },
                ]
            )
            if has_referral_reward and client.status == VPNClient.Status.ACTIVE:
                rows.append(
                    [
                        {
                            "text": f"دریافت هدیه دعوت {suffix}".strip(),
                            "callback_data": f"user:referral_redeem:{client.public_id}",
                        }
                    ]
                )
            delete_button = user_client_delete_button(bot_user, client, suffix=suffix)
            if delete_button:
                rows.append([delete_button])
    rows.append([{"text": "خرید اشتراک", "callback_data": "user:buy"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def active_subscription_lines(
    bot_user,
    *,
    title="اشتراک‌های شما",
    force_refresh=False,
    sync_stats=sync_vpn_client_stats,
):
    clients = bot_subscription_clients(bot_user) if bot_user.customer_id else []
    lines = [title, "━━━━━━━━━━━━━━", ""]
    found = False
    for client in clients:
        found = True
        stats = sync_stats(client, force=force_refresh)
        remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", 0))
        used_gb = bot_gb_from_bytes(stats.get("used_traffic_bytes", 0))
        total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
        expires_at = stats.get("expiry_at") or client.expires_at
        expiry_label = bot_datetime(expires_at)
        lines.extend(
            [
                f"{client.plan.name if client.plan_id else bot_client_label(client)}",
                f"وضعیت: {bot_client_status(client)}",
                f"حجم کل: {total_gb} گیگابایت",
                f"حجم مصرف‌شده: {used_gb} گیگابایت",
                f"حجم باقی‌مانده: {remaining_gb} گیگابایت",
                f"تاریخ پایان: {expiry_label}",
            ]
        )
        if client.sub_link or client.direct_link:
            lines.append("برای دریافت لینک، دکمه «دریافت لینک» را بزنید.")
        else:
            lines.append("لینک کانفیگ هنوز آماده نیست.")
        lines.append("")
    if not found:
        lines.append("هنوز اشتراک فعالی برای این حساب پیدا نشد.")
    return "\n".join(lines).strip()


def show_user_services(
    client,
    bot_user,
    *,
    chat_id,
    title="اشتراک‌های شما",
    force_refresh=False,
    renew_mode=False,
    sync_stats=sync_vpn_client_stats,
):
    client.send_message(
        active_subscription_lines(bot_user, title=title, force_refresh=force_refresh, sync_stats=sync_stats),
        chat_id=chat_id,
        reply_markup=subscription_management_keyboard(bot_user, renew_mode=renew_mode),
    )
    return {"ok": True, "handled": True}


def get_bot_client(bot_user, public_id):
    if not bot_user.customer_id:
        return None
    return (
        VPNClient.objects.select_related("plan", "order", "order__plan", "order__store", "inbound", "inbound__panel")
        .filter(order__customer=bot_user.customer, public_id=public_id)
        .filter(deleted_at__isnull=True)
        .exclude(status=VPNClient.Status.DELETED)
        .first()
    )


def client_config_keyboard(client, bot_user=None):
    rows = [
        [{"text": "بروزرسانی از ثنایی", "callback_data": f"user:client_refresh:{client.public_id}"}],
        [{"text": "مشاهده حجم", "callback_data": f"user:client_usage:{client.public_id}"}],
        [{"text": "تمدید این کانفیگ", "callback_data": f"user:client_renew:{client.public_id}"}],
    ]
    customer = client.order.customer if client.order_id and client.order.customer_id else None
    if customer and get_available_referral_gb(customer) > 0 and client.status == VPNClient.Status.ACTIVE:
        rows.append([{"text": "دریافت هدیه دعوت", "callback_data": f"user:referral_redeem:{client.public_id}"}])
    if bot_user:
        delete_button = user_client_delete_button(bot_user, client)
        if delete_button:
            rows.append([delete_button])
    if client.order_id:
        rows.append([{"text": "بازگشت به سفارش", "callback_data": f"user:order:{client.order.order_tracking_code}"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_client_config(
    client,
    *,
    stats=None,
    refreshed=False,
    include_config_notice=True,
    sync_stats=sync_vpn_client_stats,
):
    stats = stats or sync_stats(client)
    remaining_gb = bot_gb_from_bytes(stats.get("remaining_traffic_bytes", client.remaining_traffic_bytes))
    used_gb = bot_gb_from_bytes(stats.get("used_traffic_bytes", client.used_traffic_bytes))
    total_gb = bot_gb_from_bytes(stats.get("total_traffic_bytes", client.traffic_limit_bytes))
    lines = [
        "کانفیگ شما" if not refreshed else "کانفیگ بروزرسانی شد",
        "━━━━━━━━━━━━━━",
        f"نام کانفیگ: {bot_client_label(client)}",
        f"وضعیت: {bot_client_status(client)}",
        f"حجم باقی‌مانده: {remaining_gb} گیگابایت",
        f"مصرف‌شده: {used_gb} از {total_gb} گیگابایت",
        f"انقضا: {bot_datetime(stats.get('expiry_at') or client.expires_at)}",
    ]
    if include_config_notice and (client.sub_link or client.direct_link):
        lines.extend(["", "کانفیگ در پیام بعدی ارسال می‌شود."])
    elif not client.sub_link and not client.direct_link:
        lines.extend(["", "لینک کانفیگ هنوز برای این سفارش آماده نیست."])
    if stats.get("panel_available") is False:
        lines.extend(["", "دسترسی به پنل ثنایی موقتاً برقرار نشد؛ آخرین اطلاعات ذخیره‌شده نمایش داده شد."])
    return "\n".join(lines)


def send_client_config_messages(
    client,
    vpn_client,
    *,
    chat_id,
    stats=None,
    refreshed=False,
    bot_user=None,
    sync_stats=sync_vpn_client_stats,
):
    links = client_config_links(vpn_client)
    summary_text = format_client_config(
        vpn_client,
        stats=stats,
        refreshed=refreshed,
        include_config_notice=False,
        sync_stats=sync_stats,
    )
    if not links:
        client.send_message(
            summary_text,
            chat_id=chat_id,
            reply_markup=client_config_keyboard(vpn_client, bot_user),
        )
        return {"ok": True, "handled": True}

    summary_lines = summary_text.splitlines()
    title = f"✅ {summary_lines[0]}" if summary_lines else "✅ کانفیگ شما آماده شد"
    detail_lines = [line for line in summary_lines[2:] if line != "━━━━━━━━━━━━━━"]
    send_config_links_message(
        client,
        chat_id,
        subscription_link=vpn_client.sub_link,
        direct_link=vpn_client.direct_link,
        title=title,
        detail_lines=detail_lines,
    )
    return {"ok": True, "handled": True}


def handle_user_client_usage(
    client,
    bot_user,
    public_id,
    *,
    chat_id,
    is_admin=False,
    get_client=get_bot_client,
    sync_stats=sync_vpn_client_stats,
):
    vpn_client = get_client(bot_user, public_id)
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin))
        return {"ok": True, "success": False}
    stats = sync_stats(vpn_client, force=True)
    vpn_client.refresh_from_db()
    _emit_high_usage_revenue_event(vpn_client, bot_user, stats, chat_id=chat_id)
    client.send_message(
        format_client_config(vpn_client, stats=stats, include_config_notice=False, sync_stats=sync_stats),
        chat_id=chat_id,
        reply_markup=client_config_keyboard(vpn_client, bot_user),
    )
    return {"ok": True, "handled": True}


def handle_user_client_config(
    client,
    bot_user,
    public_id,
    *,
    chat_id,
    is_admin=False,
    get_client=get_bot_client,
    sync_stats=sync_vpn_client_stats,
):
    vpn_client = get_client(bot_user, public_id)
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin))
        return {"ok": True, "success": False}
    return send_client_config_messages(client, vpn_client, chat_id=chat_id, bot_user=bot_user, sync_stats=sync_stats)


def handle_user_client_refresh(
    client,
    bot_user,
    public_id,
    *,
    chat_id,
    is_admin=False,
    get_client=get_bot_client,
    refresh_links=refresh_vpn_client_links,
    sync_stats=sync_vpn_client_stats,
):
    vpn_client = get_client(bot_user, public_id)
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard(is_admin=is_admin))
        return {"ok": True, "success": False}
    refreshed = refresh_links(vpn_client)
    if not refreshed:
        client.send_message(
            "بروزرسانی کانفیگ از پنل ثنایی انجام نشد. کمی بعد دوباره امتحان کنید یا به پشتیبانی پیام بدهید.",
            chat_id=chat_id,
            reply_markup=client_config_keyboard(vpn_client, bot_user),
        )
        return {"ok": True, "success": False}
    stats = sync_stats(vpn_client, force=True)
    vpn_client.refresh_from_db()
    send_client_config_messages(
        client,
        vpn_client,
        chat_id=chat_id,
        stats=stats,
        refreshed=True,
        bot_user=bot_user,
        sync_stats=sync_stats,
    )
    return {"ok": True, "success": True}


def cancel_user_client_delete_flow(client, *, chat_id, is_admin=False, bot_user=None):
    if bot_user:
        bot_user.reset_state()
    client.send_message(
        "حذف کانفیگ لغو شد.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )
    return {"ok": True, "cancelled": True}


def start_user_client_delete_flow(
    client,
    config,
    bot_user,
    token,
    *,
    chat_id,
    is_admin=False,
    get_client=get_bot_client,
    pending_renewal_order=None,
    order_management_keyboard=None,
):
    payload = get_user_client_delete_payload(config, bot_user, token)
    if not payload:
        client.send_message(
            "مهلت حذف این کانفیگ تمام شده. دوباره از بخش سرویس‌های من اقدام کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
        return {"ok": True, "expired": True}
    vpn_client = get_client(bot_user, payload.get("public_id"))
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد یا قبلاً حذف شده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}
    pending_order = pending_renewal_order(bot_user, vpn_client) if pending_renewal_order else None
    if pending_order and order_management_keyboard:
        client.send_message(
            "برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید وجود دارد. بعد از تعیین تکلیف تمدید می‌توانید حذف را انجام دهید.",
            chat_id=chat_id,
            reply_markup=order_management_keyboard(pending_order, bot_user),
        )
        return {"ok": True, "success": False, "pending": True}
    client.send_message(
        build_delete_confirmation_text(vpn_client),
        chat_id=chat_id,
        reply_markup=user_client_delete_confirmation_keyboard(token),
    )
    return {"ok": True, "confirm_delete": True}


def confirm_user_client_delete_flow(
    client,
    config,
    bot_user,
    token,
    *,
    chat_id,
    is_admin=False,
    get_client=get_bot_client,
    delete_client_for_user=delete_vpn_client_for_user,
):
    payload = get_user_client_delete_payload(config, bot_user, token)
    if not payload:
        client.send_message(
            "مهلت حذف این کانفیگ تمام شده. دوباره از بخش سرویس‌های من اقدام کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
        return {"ok": True, "expired": True}
    vpn_client = get_client(bot_user, payload.get("public_id"))
    if not vpn_client:
        client.send_message("این کانفیگ پیدا نشد یا قبلاً حذف شده است.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}
    try:
        delete_client_for_user(
            bot_user.customer,
            vpn_client,
            reason="Deleted by user from bot",
            actor_telegram_id=bot_user.provider_user_id or bot_user.chat_id,
        )
    except VPNClientPendingRenewalError:
        client.send_message(
            "برای این کانفیگ یک تمدید در انتظار پرداخت یا تایید وجود دارد. ابتدا آن سفارش را تعیین تکلیف کنید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
        return {"ok": True, "success": False, "pending": True}
    except VPNClientManagementError as exc:
        logger.warning(
            "User client delete failed bot_user=%s client_public=%s error=%s",
            bot_user.pk,
            payload.get("public_id"),
            exc,
        )
        client.send_message(
            "حذف کانفیگ انجام نشد. لطفاً چند دقیقه دیگر تلاش کنید یا به پشتیبانی پیام بدهید.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )
        return {"ok": True, "success": False}
    cache.delete(user_client_delete_cache_key(config, bot_user, token))
    client.send_message(
        "✅ کانفیگ حذف شد.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )
    return {"ok": True, "success": True}
