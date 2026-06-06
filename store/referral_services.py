from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import logging
import os
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from .models import BotConfiguration, Customer, Order, Referral, ReferralRewardLedger, Store, VPNClient
from .referrals import assign_referrer, build_referral_link, normalize_referral_code
from .xui_api import add_client_traffic, bytes_from_gb

logger = logging.getLogger(__name__)

BOT_USERNAME_MISSING_MESSAGE = "نام کاربری ربات تنظیم نشده است."


@dataclass
class ReferralRedeemResult:
    success: bool
    message: str
    reward_gb: Decimal = Decimal("0")
    reward_duration_days: int = 0
    reward_count: int = 0
    vpn_config: VPNClient | None = None


def ensure_referral_code(customer):
    if not customer:
        return ""
    if not customer.referral_code:
        customer.referral_code = Customer.generate_unique_referral_code()
        customer.save(update_fields=["referral_code", "updated_at"])
    return customer.referral_code


def apply_referral_code(customer, referral_code):
    return assign_referrer(customer, normalize_referral_code(referral_code))


def get_referral_store(store=None):
    return store or Store.objects.filter(is_active=True).first() or Store.objects.first()


def store_referral_reward_traffic_gb(store):
    value = getattr(store, "referral_reward_traffic_gb", None)
    if value is None:
        value = getattr(store, "referral_reward_gb", 0)
    return Decimal(value or 0)


def store_referral_reward_duration_days(store):
    try:
        return max(int(getattr(store, "referral_reward_duration_days", 30) or 0), 0)
    except (TypeError, ValueError):
        return 30


def reward_traffic_gb(ledger):
    return Decimal(getattr(ledger, "reward_traffic_gb", None) or getattr(ledger, "reward_gb", 0) or 0)


def reward_duration_days(ledger):
    try:
        return max(int(getattr(ledger, "reward_duration_days", 30) or 0), 0)
    except (TypeError, ValueError):
        return 30


def create_referral_reward_for_order(order):
    if not order or not getattr(order, "pk", None):
        return None

    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .select_related("customer", "customer__referred_by", "store")
            .get(pk=order.pk)
        )
        if (
            not order.customer_id
            or order.status != Order.Status.COMPLETED
            or order.verification_status != Order.VerificationStatus.VERIFIED
        ):
            return None

        invited = order.customer
        if not invited.referred_by_id or invited.referred_by_id == invited.pk:
            return None

        store = get_referral_store(order.store)
        if not store or not store.referral_system_enabled:
            return None

        reward_gb = store_referral_reward_traffic_gb(store)
        reward_duration_days = store_referral_reward_duration_days(store)
        if reward_gb <= 0:
            return None
        if reward_duration_days <= 0:
            return None

        if store.referral_min_order_required:
            has_previous_successful_order = Order.objects.filter(
                customer=invited,
                status=Order.Status.COMPLETED,
                verification_status=Order.VerificationStatus.VERIFIED,
            ).exclude(pk=order.pk).exists()
            if has_previous_successful_order:
                return None

        existing_ledger = ReferralRewardLedger.objects.filter(invited=invited).first()
        if existing_ledger:
            return existing_ledger

        referral = (
            Referral.objects.select_for_update()
            .filter(referred_customer=invited)
            .first()
        )
        if not referral:
            referral = Referral.objects.create(
                referrer=invited.referred_by,
                referred_customer=invited,
                referral_code=invited.referral_code_used or invited.referred_by.referral_code,
            )
        if referral.status != Referral.Status.PURCHASED:
            referral.status = Referral.Status.PURCHASED
            referral.first_order = order
            referral.purchased_at = timezone.now()
            referral.save(update_fields=["status", "first_order", "purchased_at", "updated_at"])

        return ReferralRewardLedger.objects.create(
            inviter=invited.referred_by,
            invited=invited,
            order=order,
            reward_gb=reward_gb,
            reward_duration_days=reward_duration_days,
            status=ReferralRewardLedger.Status.AVAILABLE,
            available_at=timezone.now(),
            notes="Referral gift package created after invited customer's successful purchase.",
        )


def get_available_referral_gb(customer):
    if not customer:
        return Decimal("0")
    return (
        ReferralRewardLedger.objects.filter(
            inviter=customer,
            status=ReferralRewardLedger.Status.AVAILABLE,
        ).aggregate(total=Sum("reward_gb"))["total"]
        or Decimal("0")
    )


def get_redeemed_referral_gb(customer):
    if not customer:
        return Decimal("0")
    return (
        ReferralRewardLedger.objects.filter(
            inviter=customer,
            status=ReferralRewardLedger.Status.REDEEMED,
        ).aggregate(total=Sum("reward_gb"))["total"]
        or Decimal("0")
    )


def get_available_referral_duration_days(customer):
    if not customer:
        return 0
    return (
        ReferralRewardLedger.objects.filter(
            inviter=customer,
            status=ReferralRewardLedger.Status.AVAILABLE,
        ).aggregate(total=Sum("reward_duration_days"))["total"]
        or 0
    )


def get_redeemed_referral_duration_days(customer):
    if not customer:
        return 0
    return (
        ReferralRewardLedger.objects.filter(
            inviter=customer,
            status=ReferralRewardLedger.Status.REDEEMED,
        ).aggregate(total=Sum("reward_duration_days"))["total"]
        or 0
    )


def get_active_referral_configs(customer):
    if not customer:
        return VPNClient.objects.none()
    return (
        VPNClient.objects.select_related("plan", "order", "inbound", "inbound__panel")
        .filter(order__customer=customer, status=VPNClient.Status.ACTIVE)
        .order_by("-created_at")
    )


def get_referral_stats(customer):
    if not customer:
        return {
            "invited_count": 0,
            "successful_referrals_count": 0,
            "available_reward_count": 0,
            "available_packages_count": 0,
            "available_gb": Decimal("0"),
            "available_duration_days": 0,
            "redeemed_reward_count": 0,
            "redeemed_packages_count": 0,
            "redeemed_gb": Decimal("0"),
            "redeemed_duration_days": 0,
        }

    successful_referrals_count = (
        Customer.objects.filter(
            referred_by=customer,
            orders__status=Order.Status.COMPLETED,
            orders__verification_status=Order.VerificationStatus.VERIFIED,
        )
        .distinct()
        .count()
    )
    available_stats = ReferralRewardLedger.objects.filter(
        inviter=customer,
        status=ReferralRewardLedger.Status.AVAILABLE,
    ).aggregate(
        count=Count("id"),
        total_gb=Sum("reward_gb"),
        total_days=Sum("reward_duration_days"),
    )
    redeemed_stats = ReferralRewardLedger.objects.filter(
        inviter=customer,
        status=ReferralRewardLedger.Status.REDEEMED,
    ).aggregate(
        count=Count("id"),
        total_gb=Sum("reward_gb"),
        total_days=Sum("reward_duration_days"),
    )
    return {
        "invited_count": Customer.objects.filter(referred_by=customer).count(),
        "successful_referrals_count": successful_referrals_count,
        "available_reward_count": available_stats["count"] or 0,
        "available_packages_count": available_stats["count"] or 0,
        "available_gb": available_stats["total_gb"] or Decimal("0"),
        "available_duration_days": available_stats["total_days"] or 0,
        "redeemed_reward_count": redeemed_stats["count"] or 0,
        "redeemed_packages_count": redeemed_stats["count"] or 0,
        "redeemed_gb": redeemed_stats["total_gb"] or Decimal("0"),
        "redeemed_duration_days": redeemed_stats["total_days"] or 0,
    }


def configured_telegram_bot_username(bot_config=None):
    username = (
        getattr(settings, "TELEGRAM_BOT_USERNAME", "")
        or os.environ.get("TELEGRAM_BOT_USERNAME", "")
        or ""
    )
    username = str(username).strip().lstrip("@")
    if username:
        return username

    username = str(getattr(bot_config, "username", "") or "").strip().lstrip("@")
    if username:
        return username

    has_username_field = any(field.name == "username" for field in BotConfiguration._meta.get_fields())
    if has_username_field:
        config = (
            BotConfiguration.objects.filter(
                provider=BotConfiguration.Provider.TELEGRAM,
                is_active=True,
            )
            .exclude(username="")
            .order_by("pk")
            .first()
        )
        username = str(getattr(config, "username", "") or "").strip().lstrip("@")
        if username:
            return username

    return ""


def build_telegram_referral_link(customer, *, bot_username=None, bot_config=None):
    code = ensure_referral_code(customer)
    username = (bot_username or configured_telegram_bot_username(bot_config=bot_config)).strip().lstrip("@")
    if not username:
        return ""
    return f"https://t.me/{username}?start=ref_{code}"


def build_referral_invite_text(referral_link):
    if not referral_link:
        return BOT_USERNAME_MISSING_MESSAGE
    return (
        "🎁 من از این ربات VPN گرفتم، تست رایگان هم داره.\n"
        "با لینک من وارد شو و سرویس بگیر:\n"
        f"{referral_link}\n\n"
        "با خرید تو، من هم جایزه می‌گیرم 🙌"
    )


def build_telegram_share_url(referral_link, invite_text):
    if not referral_link or not invite_text:
        return ""
    text_without_link = invite_text.replace(referral_link, "").strip()
    return f"https://t.me/share/url?url={quote(referral_link)}&text={quote(text_without_link)}"


def get_referral_summary(customer, *, request=None, store=None, bot_username=None, bot_config=None):
    store = get_referral_store(store)
    code = ensure_referral_code(customer)
    stats = get_referral_stats(customer)
    telegram_username = configured_telegram_bot_username(bot_config=bot_config)
    telegram_link = build_telegram_referral_link(
        customer,
        bot_username=bot_username or telegram_username,
        bot_config=bot_config,
    ) if customer else ""
    invite_text = build_referral_invite_text(telegram_link)
    return {
        "code": code,
        "site_link": build_referral_link(request, customer) if request and customer else "",
        "telegram_bot_username": telegram_username,
        "telegram_link": telegram_link,
        "telegram_link_missing_message": BOT_USERNAME_MISSING_MESSAGE,
        "invite_text": invite_text,
        "telegram_share_url": build_telegram_share_url(telegram_link, invite_text),
        "enabled": bool(store.referral_system_enabled) if store else True,
        "reward_gb": store_referral_reward_traffic_gb(store) if store else Decimal("0"),
        "reward_traffic_gb": store_referral_reward_traffic_gb(store) if store else Decimal("0"),
        "reward_duration_days": store_referral_reward_duration_days(store) if store else 30,
        "disabled_message": "سیستم دعوت فعلاً غیرفعال است؛ کد دعوت شما محفوظ است اما جایزه جدید ثبت نمی‌شود.",
        **stats,
    }


def calculate_referral_expiry(vpn_config, total_duration_days, *, now=None, xui_result=None):
    now = now or timezone.now()
    xui_result = xui_result or {}
    if xui_result.get("expiry_unlimited"):
        return None
    if xui_result.get("expiry_at"):
        return xui_result["expiry_at"]
    if not total_duration_days:
        return vpn_config.expires_at
    base_expiry = vpn_config.expires_at if vpn_config.expires_at and vpn_config.expires_at > now else now
    return base_expiry + timedelta(days=total_duration_days)


def redeem_referral_rewards(customer, vpn_config):
    if not customer:
        return ReferralRedeemResult(False, "حساب کاربری پیدا نشد.")
    if not vpn_config:
        return ReferralRedeemResult(False, "برای دریافت هدیه ابتدا یک کانفیگ فعال انتخاب کن.")

    now = timezone.now()
    with transaction.atomic():
        vpn_config = (
            VPNClient.objects.select_for_update()
            .select_related("order", "inbound", "inbound__panel")
            .filter(pk=vpn_config.pk, order__customer=customer, status=VPNClient.Status.ACTIVE)
            .first()
        )
        if not vpn_config:
            return ReferralRedeemResult(False, "این کانفیگ فعال نیست یا به این حساب تعلق ندارد.")

        rewards = list(
            ReferralRewardLedger.objects.select_for_update()
            .filter(inviter=customer, status=ReferralRewardLedger.Status.AVAILABLE)
            .order_by("created_at", "pk")
        )
        reward_gb = sum((reward_traffic_gb(item) for item in rewards), Decimal("0"))
        reward_duration = sum((reward_duration_days(item) for item in rewards), 0)
        if reward_gb <= 0:
            return ReferralRedeemResult(False, "در حال حاضر هدیه آماده دریافت نداری.")

        xui_result = add_client_traffic(vpn_config, reward_gb, extra_days=reward_duration)
        if not xui_result:
            logger.error(
                "Referral reward redeem failed on X-UI customer=%s vpn_config=%s reward_gb=%s reward_days=%s",
                customer.pk,
                vpn_config.pk,
                reward_gb,
                reward_duration,
            )
            return ReferralRedeemResult(False, "افزایش حجم روی پنل X-UI ناموفق بود. کمی بعد دوباره امتحان کن.")

        vpn_config.traffic_limit_bytes = (
            xui_result.get("total_traffic_bytes")
            or vpn_config.traffic_limit_bytes + bytes_from_gb(reward_gb)
        )
        new_expiry = calculate_referral_expiry(
            vpn_config,
            reward_duration,
            now=now,
            xui_result=xui_result,
        )
        if not xui_result.get("expiry_unlimited"):
            vpn_config.expires_at = new_expiry
            vpn_config.duration_days = (vpn_config.duration_days or 0) + reward_duration
        if xui_result.get("raw"):
            vpn_config.xui_raw = xui_result["raw"]
        vpn_config.save(update_fields=["traffic_limit_bytes", "duration_days", "expires_at", "xui_raw", "updated_at"])

        ReferralRewardLedger.objects.filter(pk__in=[item.pk for item in rewards]).update(
            status=ReferralRewardLedger.Status.REDEEMED,
            redeemed_config_id=vpn_config.pk,
            redeemed_at=timezone.now(),
            applied_traffic_gb=F("reward_gb"),
            applied_duration_days=F("reward_duration_days"),
            notes="Referral gift package redeemed onto a VPN config.",
            updated_at=timezone.now(),
        )

    return ReferralRedeemResult(
        True,
        f"{reward_gb.normalize()} گیگابایت و {reward_duration} روز هدیه روی کانفیگ انتخاب‌شده اعمال شد.",
        reward_gb=reward_gb,
        reward_duration_days=reward_duration,
        reward_count=len(rewards),
        vpn_config=vpn_config,
    )
