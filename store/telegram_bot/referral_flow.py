from store.jalali import persian_digits
from store.order_services import get_current_store
from store.referral_services import (
    get_active_referral_configs,
    get_referral_summary,
    redeem_referral_rewards,
)

from .formatting import bot_volume_label
from .services_flow import bot_client_label
from .user_menu import main_menu_keyboard


REFERRAL_CALLBACKS = {
    "user:referrals",
    "user:referral_invite_text",
    "user:referral_help",
    "user:referral_choose",
    "user:referral_redeem",
}


def is_referral_callback_data(data):
    data = str(data or "")
    return data in REFERRAL_CALLBACKS or data.startswith("user:referral_redeem:")


def referral_keyboard(bot_user, config=None, *, get_current_store_func=get_current_store):
    rows = []
    active_configs = list(get_active_referral_configs(bot_user.customer)) if bot_user.customer_id else []
    if bot_user.customer_id:
        callback_data = "user:referral_redeem"
        if len(active_configs) > 1:
            callback_data = "user:referral_choose"
        rows.append([{"text": "دریافت جایزه 🎁", "callback_data": callback_data}])
        rows.append([{"text": "متن آماده دعوت 📨", "callback_data": "user:referral_invite_text"}])
        try:
            summary = get_referral_summary(
                bot_user.customer,
                store=(config.store if config else None) or get_current_store_func(),
                bot_config=config,
            )
        except Exception:
            summary = {}
        if summary.get("telegram_share_url"):
            rows.append([{"text": "اشتراک‌گذاری لینک", "url": summary["telegram_share_url"]}])
    rows.append([{"text": "راهنمای دعوت ❓", "callback_data": "user:referral_help"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def referral_config_keyboard(bot_user):
    rows = []
    active_configs = list(get_active_referral_configs(bot_user.customer)) if bot_user.customer_id else []
    for index, vpn_config in enumerate(active_configs, start=1):
        label = f"{persian_digits(index)}. {bot_client_label(vpn_config)}"
        rows.append([{"text": label[:60], "callback_data": f"user:referral_redeem:{vpn_config.public_id}"}])
    rows.append([{"text": "بازگشت", "callback_data": "user:referrals"}])
    rows.append([{"text": "منوی اصلی", "callback_data": "user:menu"}])
    return {"inline_keyboard": rows}


def format_referral_panel(bot_user, config, *, get_current_store_func=get_current_store):
    customer = bot_user.customer
    if not customer:
        return "برای استفاده از دعوت دوستان، ابتدا از منوی اصلی پروفایل خود را بسازید."

    summary = get_referral_summary(customer, store=config.store or get_current_store_func(), bot_config=config)
    telegram_link = summary["telegram_link"] or summary["telegram_link_missing_message"]
    lines = [
        "🎁 دعوت دوستان",
        "━━━━━━━━━━━━━━",
        "کد دعوت شما:",
        summary["code"],
        "",
        "لینک اختصاصی شما:",
        telegram_link,
        "",
        "آمار:",
        f"• دوستان دعوت‌شده: {persian_digits(summary['invited_count'])}",
        f"• خریدهای موفق: {persian_digits(summary['successful_referrals_count'])}",
        f"• بسته‌های آماده دریافت: {persian_digits(summary['available_reward_count'])}",
        f"• حجم آماده دریافت: {bot_volume_label(summary['available_gb'])}",
        f"• مدت آماده دریافت: {persian_digits(summary['available_duration_days'])} روز",
        f"• حجم دریافت‌شده: {bot_volume_label(summary['redeemed_gb'])}",
    ]
    if summary["telegram_link"]:
        lines.extend(["", "متن آماده دعوت:", summary["invite_text"]])
    if not summary["enabled"]:
        lines.extend(["", summary["disabled_message"]])
    return "\n".join(lines)


def referral_help_text():
    return (
        "راهنمای دعوت دوستان 🎁\n"
        "━━━━━━━━━━━━━━\n"
        "کد یا لینک دعوت خود را برای دوستتان بفرستید. اگر دوست شما با همان لینک وارد شود و اولین خرید موفقش تایید شود، "
        "یک بسته هدیه ۲ گیگ و ۳۰ روزه برای شما آماده می‌شود. بسته‌ها با هم جمع می‌شوند و هنگام دریافت روی کانفیگ انتخابی اعمال می‌شوند."
    )


def redeem_referral_reward_for_bot(client, bot_user, *, chat_id, public_id=""):
    if not bot_user.customer_id:
        client.send_message("حساب کاربری پیدا نشد.", chat_id=chat_id, reply_markup=main_menu_keyboard())
        return {"ok": True, "success": False}

    active_configs = list(get_active_referral_configs(bot_user.customer))
    if not active_configs:
        client.send_message(
            "برای دریافت هدیه، ابتدا باید یک کانفیگ فعال داشته باشید.",
            chat_id=chat_id,
            reply_markup=referral_keyboard(bot_user, client.config),
        )
        return {"ok": True, "success": False}
    if public_id:
        vpn_config = next((item for item in active_configs if str(item.public_id) == str(public_id)), None)
        if not vpn_config:
            client.send_message("این کانفیگ فعال نیست یا پیدا نشد.", chat_id=chat_id, reply_markup=referral_keyboard(bot_user, client.config))
            return {"ok": True, "success": False}
    elif len(active_configs) == 1:
        vpn_config = active_configs[0]
    else:
        client.send_message(
            "کدام کانفیگ را برای دریافت هدیه انتخاب می‌کنید؟",
            chat_id=chat_id,
            reply_markup=referral_config_keyboard(bot_user),
        )
        return {"ok": True, "select_config": True}

    result = redeem_referral_rewards(bot_user.customer, vpn_config)
    client.send_message(
        result.message,
        chat_id=chat_id,
        reply_markup=referral_keyboard(bot_user, client.config),
    )
    return {"ok": True, "success": result.success}


def handle_referral_callback(client, config, bot_user, data, *, chat_id, get_current_store_func=get_current_store):
    if data == "user:referrals":
        client.send_message(
            format_referral_panel(bot_user, config, get_current_store_func=get_current_store_func),
            chat_id=chat_id,
            reply_markup=referral_keyboard(bot_user, config, get_current_store_func=get_current_store_func),
        )
        return {"ok": True, "handled": True}
    if data == "user:referral_invite_text":
        if bot_user.customer_id:
            summary = get_referral_summary(
                bot_user.customer,
                store=config.store or get_current_store_func(),
                bot_config=config,
            )
            text = summary["invite_text"]
        else:
            text = "برای استفاده از دعوت دوستان، ابتدا از منوی اصلی پروفایل خود را بسازید."
        client.send_message(
            text,
            chat_id=chat_id,
            reply_markup=referral_keyboard(bot_user, config, get_current_store_func=get_current_store_func),
            parse_mode=None,
        )
        return {"ok": True, "handled": True}
    if data == "user:referral_help":
        client.send_message(
            referral_help_text(),
            chat_id=chat_id,
            reply_markup=referral_keyboard(bot_user, config, get_current_store_func=get_current_store_func),
        )
        return {"ok": True, "handled": True}
    if data == "user:referral_choose":
        client.send_message(
            "کدام کانفیگ را برای دریافت هدیه انتخاب می‌کنید؟",
            chat_id=chat_id,
            reply_markup=referral_config_keyboard(bot_user),
        )
        return {"ok": True, "handled": True}
    if data == "user:referral_redeem":
        return redeem_referral_reward_for_bot(client, bot_user, chat_id=chat_id)
    if data.startswith("user:referral_redeem:"):
        public_id = data.rsplit(":", 1)[-1]
        return redeem_referral_reward_for_bot(client, bot_user, chat_id=chat_id, public_id=public_id)
    return {"ok": True, "ignored": True}
