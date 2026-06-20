from decimal import Decimal

from django.core.exceptions import ValidationError

from store.free_trial_services import (
    create_free_trial_for_customer,
    format_free_trial_preview,
    format_free_trial_result,
    get_next_free_trial_available_at,
    validate_free_trial_settings,
)
from store.jalali import format_jalali_datetime, persian_digits

from .config_delivery import send_config_links_message
from .formatting import bot_volume_label
from .user_menu import main_menu_keyboard


def default_is_admin_bot_user(config, bot_user):
    return bool(
        bot_user
        and (
            config.is_admin_user(getattr(bot_user, "provider_user_id", ""))
            or config.is_admin_user(getattr(bot_user, "chat_id", ""))
        )
    )


def free_trial_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "دریافت تست ✅", "callback_data": "user:free_trial_confirm"}],
            [{"text": "لغو", "callback_data": "user:free_trial_cancel"}],
        ]
    }


def start_free_trial_flow(client, config, bot_user, *, chat_id, is_admin_bot_user_func=default_is_admin_bot_user):
    try:
        settings = validate_free_trial_settings(bot_config=config)
    except ValidationError:
        client.send_message(
            "در حال حاضر تست رایگان فعال نیست.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False}

    next_available_at = get_next_free_trial_available_at(
        bot_user.customer if bot_user.customer_id else None,
        telegram_user_id=bot_user.provider_user_id,
        store=settings["store"],
    )
    if next_available_at:
        client.send_message(
            f"شما قبلاً تست رایگان دریافت کرده‌اید. امکان دریافت بعدی از تاریخ {format_jalali_datetime(next_available_at)} وجود دارد.",
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
        return {"ok": True, "success": False, "next_available_at": next_available_at}

    client.send_message(
        format_free_trial_preview(settings),
        chat_id=chat_id,
        reply_markup=free_trial_keyboard(),
    )
    return {"ok": True, "handled": True}


def confirm_free_trial_flow(client, config, bot_user, *, chat_id, is_admin_bot_user_func=default_is_admin_bot_user):
    result = create_free_trial_for_customer(
        bot_user.customer if bot_user.customer_id else None,
        telegram_user_id=bot_user.provider_user_id,
        bot_config=config,
    )
    bot_user.reset_state()
    if not result.success:
        client.send_message(
            format_free_trial_result(result),
            chat_id=chat_id,
            reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
        )
    else:
        trial_request = result.request
        subscription_link = getattr(result.vpn_client, "sub_link", "")
        direct_link = getattr(result.vpn_client, "direct_link", "") or (trial_request.config_link if trial_request else "")
        if not direct_link and not subscription_link and trial_request:
            direct_link = trial_request.config_link
        traffic_gb = trial_request.traffic_gb if trial_request else Decimal("0")
        duration_hours = trial_request.duration_hours if trial_request else 0
        send_config_links_message(
            client,
            chat_id=chat_id,
            subscription_link=subscription_link,
            direct_link=direct_link,
            title="🎁 تست رایگان شما آماده شد",
            detail_lines=[
                f"حجم: {bot_volume_label(traffic_gb)}",
                f"مدت اعتبار: {persian_digits(duration_hours)} ساعت",
            ],
        )
    return {
        "ok": True,
        "success": result.success,
        "free_trial_request": result.request.pk if result.request else None,
        "vpn_client": result.vpn_client.pk if result.vpn_client else None,
    }


def cancel_free_trial_flow(client, config, bot_user, *, chat_id, is_admin_bot_user_func=default_is_admin_bot_user):
    bot_user.reset_state()
    client.send_message(
        "دریافت تست رایگان لغو شد.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=is_admin_bot_user_func(config, bot_user)),
    )
    return {"ok": True, "cancelled": True}
