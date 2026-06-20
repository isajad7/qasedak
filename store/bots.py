"""Telegram bot compatibility facade and webhook/polling entrypoint.

Implementations live in ``store.telegram_bot.*``. This module keeps legacy
imports and monkeypatch targets working by re-exporting selected public names,
and keeps webhook/polling entrypoints as thin delegates into the router.

Modules inside ``store.telegram_bot`` must not import ``store.bots``. Callback
data and state names are compatibility contracts and must not change without
an explicit migration/compatibility path.
"""

import logging
from datetime import timedelta

import requests
from django.core.exceptions import ValidationError
from django.utils import timezone

from .jalali import persian_digits
from .models import (
    BotConfiguration,
    BotEventLog,
    BotPendingAction,
    BotUser,
    Order,
    validate_payment_receipt_image,
)
from .order_services import (
    MAX_ORDER_QUANTITY,
    OPERATOR_REQUIRED_MESSAGE,
    create_manual_payment_order,
    create_renewal_payment_order,
    get_active_operators,
    get_current_store,
    sales_mode_requires_operator,
    validate_order_quantity,
)
from .referral_services import (
    apply_referral_code,
)
from .telegram_link_services import (
    WEB_TELEGRAM_INVALID_MESSAGE,
    link_bot_user_to_customer,
)
from .telegram_membership import CHECK_MEMBERSHIP_CALLBACK, ensure_telegram_membership
from .config_lookup import (
    check_config_usage,
)
from .xui_api import build_config_link_for_identifier, refresh_vpn_client_links, sync_vpn_client_stats
from .vpn_client_management_services import (
    VPNClientManagementError,
    build_admin_delete_confirmation_text,
    build_admin_edit_summary,
    delete_vpn_client_by_admin_lookup,
    delete_vpn_client_for_user,
    gb_to_bytes,
    refresh_vpn_client_link_by_admin,
    update_vpn_client_limits_by_admin,
)
from .telegram_bot.constants import (
    BOT_DIGIT_TRANSLATION,
    CONFIG_COPY_CACHE_SECONDS,
    CONFIG_COPY_CALLBACK_PREFIX,
    CONFIG_COPY_HELP_TEXT,
    PAYMENT_NAME_CALLBACK,
    PAYMENT_NAME_LABEL,
    PAYMENT_RECEIPT_LABEL,
    PAYMENT_RECEIPT_ONLY_CALLBACK,
    TELEGRAM_MESSAGE_SAFE_LIMIT,
    USER_CLIENT_DELETE_CALLBACK_PREFIX,
    USER_CLIENT_DELETE_CONFIRM_CALLBACK_PREFIX,
)
from .telegram_bot.client import BotClient, BotDeliveryError, bot_api_timeout
from .telegram_bot.formatting import (
    bot_datetime,
    bot_gb_from_bytes,
    bot_money,
    bot_volume_label,
    clean_decimal_label,
    escape_for_telegram_code,
    format_card_for_copy,
    format_card_for_display,
    format_money_for_copy,
    format_money_for_display,
    money,
    normalize_bot_number,
    telegram_code,
)
from .telegram_bot.keyboards import (
    build_copy_text_button,
    build_payment_keyboard,
    copy_text_is_supported,
    empty_inline_keyboard,
    merge_inline_keyboards,
)
from .telegram_bot.redaction import (
    CONFIG_LOOKUP_LINK_LOG_RE,
    CONFIG_LOOKUP_UUID_LOG_RE,
    CONFIG_SUBSCRIPTION_LINK_LOG_RE,
    WEB_TELEGRAM_LINK_TOKEN_LOG_RE,
    sanitize_bot_event_log_value,
    sanitize_bot_text_for_logging,
    sanitize_bot_update_for_logging,
)
from .telegram_bot.config_delivery import (
    CONFIG_COPY_EXPIRED_MESSAGE,
    CONFIG_LINK_KIND_META,
    build_config_link_copy_button,
    cached_config_copy_link,
    config_copy_cache_key,
    config_copy_navigation_keyboard,
    config_delivery_keyboard,
    config_link_sections,
    config_send_result_count,
    create_config_copy_token,
    default_config_links_title,
    format_config_links_text,
    format_copyable_config_text,
    handle_config_copy_callback,
    normalize_config_link,
    parse_config_copy_callback_data,
    send_config_links_message,
    send_copyable_config_message,
)
from .telegram_bot.admin_broadcast import (
    BROADCAST_AUDIENCE_LABELS as _BROADCAST_AUDIENCE_LABELS,
    BROADCAST_CHANNEL_LABELS as _BROADCAST_CHANNEL_LABELS,
    broadcast_audience_keyboard as _broadcast_audience_keyboard,
    broadcast_campaign_preview as _broadcast_campaign_preview,
    broadcast_confirm_keyboard as _broadcast_confirm_keyboard,
    broadcast_menu_text as _broadcast_menu_text,
    format_broadcast_preview as _format_broadcast_preview,
    format_broadcast_result as _format_broadcast_result,
    handle_broadcast_callback_update as _handle_broadcast_callback_update,
    handle_broadcast_text as _handle_broadcast_text,
    is_broadcast_callback_data as _is_broadcast_callback_data,
    select_broadcast_audience as _select_broadcast_audience,
    send_confirmed_broadcast as _send_confirmed_broadcast,
    start_broadcast_flow as _start_broadcast_flow,
)
from .telegram_bot.admin_config_management import (
    admin_config_delete_confirmation_keyboard as _admin_config_delete_confirmation_keyboard,
    admin_config_expiry_keyboard as _admin_config_expiry_keyboard,
    admin_config_traffic_confirm_keyboard as _admin_config_traffic_confirm_keyboard,
    admin_config_traffic_keyboard as _admin_config_traffic_keyboard,
    handle_admin_config_management_callback_update as _handle_admin_config_management_callback_update,
    handle_admin_config_waiting_text as _handle_admin_config_waiting_text,
)
from .telegram_bot.admin_orders import (
    admin_customer_label,
    admin_customer_phone,
    admin_customer_telegram_id,
    admin_order_type_label,
    admin_pending_orders_keyboard as _admin_pending_orders_keyboard,
    admin_pending_orders_text as _admin_pending_orders_text,
    admin_receipt_label,
    bot_order_status,
    bot_verification_status,
    format_order_message,
    handle_admin_order_callback_update as _handle_admin_order_callback_update,
    handle_admin_orders_menu_callback as _handle_admin_orders_menu_callback,
    order_admin_keyboard,
)
from .telegram_bot.admin_reports import (
    CUSTOMER_ANALYTICS_REPORTS as _CUSTOMER_ANALYTICS_REPORTS,
    admin_quick_settings_text as _admin_quick_settings_text,
    admin_sales_report_text as _admin_sales_report_text,
    customer_analytics_contact as _customer_analytics_contact,
    customer_analytics_customer_name as _customer_analytics_customer_name,
    customer_analytics_empty_message as _customer_analytics_empty_message,
    customer_analytics_keyboard as _customer_analytics_keyboard,
    customer_analytics_menu_text as _customer_analytics_menu_text,
    format_customer_analytics_report as _format_customer_analytics_report,
    handle_admin_report_menu_callback as _handle_admin_report_menu_callback,
    handle_customer_analytics_callback_update as _handle_customer_analytics_callback_update,
    is_admin_report_menu_callback_data as _is_admin_report_menu_callback_data,
    is_customer_analytics_callback_data as _is_customer_analytics_callback_data,
)
from .telegram_bot.admin_support import (
    handle_pending_support_reply as _handle_pending_support_reply,
    handle_support_callback_update as _handle_support_callback_update,
    is_support_callback_data as _is_support_callback_data,
    parse_support_callback_data as _parse_support_callback_data,
)
from .telegram_bot.buy_flow import (
    apply_discount_code_for_bot as _apply_discount_code_for_bot,
    bot_discount_error_message as _bot_discount_error_message,
    calculate_bot_pricing as _calculate_bot_pricing,
    continue_purchase_after_custom_volume as _continue_purchase_after_custom_volume,
    continue_purchase_after_quantity as _continue_purchase_after_quantity,
    custom_volume_prompt_text as _custom_volume_prompt_text,
    discount_code_keyboard as _discount_code_keyboard,
    format_operator_lines as _format_operator_lines,
    format_plan_lines as _format_plan_lines,
    format_purchase_summary as _format_purchase_summary,
    get_purchase_operator_from_state as _get_purchase_operator_from_state,
    get_selected_purchase_plan as _get_selected_purchase_plan,
    handle_buy_callback as _handle_buy_callback,
    is_buy_callback_data as _is_buy_callback_data,
    operator_keyboard as _operator_keyboard,
    parse_buy_custom_callback as _parse_buy_custom_callback,
    parse_buy_plan_callback as _parse_buy_plan_callback,
    plan_button_label as _plan_button_label,
    plan_keyboard as _plan_keyboard,
    pricing_from_state as _pricing_from_state,
    purchase_context_from_state as _purchase_context_from_state,
    purchase_summary_keyboard as _purchase_summary_keyboard,
    quantity_keyboard as _quantity_keyboard,
    send_current_order_summary as _send_current_order_summary,
    send_operator_list as _send_operator_list,
    send_plan_list as _send_plan_list,
    send_purchase_summary as _send_purchase_summary,
    skip_discount_for_bot as _skip_discount_for_bot,
    start_custom_volume_flow as _start_custom_volume_flow,
    start_discount_code_flow as _start_discount_code_flow,
    start_purchase_flow as _start_purchase_flow,
)
from .telegram_bot.config_lookup_flow import (
    admin_config_management_cache_key as _admin_config_management_cache_key,
    append_config_lookup_panel_errors as _append_config_lookup_panel_errors,
    config_lookup_keyboard as _config_lookup_keyboard,
    config_lookup_rate_key as _config_lookup_rate_key,
    config_lookup_rate_limited as _config_lookup_rate_limited,
    config_lookup_result_keyboard as _config_lookup_result_keyboard,
    config_lookup_update_cache_key as _config_lookup_update_cache_key,
    config_lookup_update_rate_key as _config_lookup_update_rate_key,
    config_lookup_update_rate_limited as _config_lookup_update_rate_limited,
    create_admin_config_management_token as _create_admin_config_management_token,
    create_config_lookup_update_token as _create_config_lookup_update_token,
    get_admin_config_management_payload as _get_admin_config_management_payload,
    handle_config_lookup_text as _handle_config_lookup_text,
    handle_config_lookup_update_callback as _handle_config_lookup_update_callback,
    start_config_lookup_flow as _start_config_lookup_flow,
)
from .telegram_bot.free_trial_flow import (
    cancel_free_trial_flow as _cancel_free_trial_flow,
    confirm_free_trial_flow as _confirm_free_trial_flow,
    free_trial_keyboard as _free_trial_keyboard,
    start_free_trial_flow as _start_free_trial_flow,
)
from .telegram_bot.profile_flow import (
    extract_contact_phone as _extract_contact_phone,
    handle_profile_callback as _handle_profile_callback,
    is_valid_bot_phone_number as _is_valid_bot_phone_number,
    normalize_bot_phone_number as _normalize_bot_phone_number,
    phone_request_keyboard as _phone_request_keyboard,
    save_bot_user_phone as _save_bot_user_phone,
    start_profile_phone_flow as _start_profile_phone_flow,
)
from .telegram_bot.referral_flow import (
    format_referral_panel as _format_referral_panel,
    handle_referral_callback as _handle_referral_callback,
    is_referral_callback_data as _is_referral_callback_data,
    redeem_referral_reward_for_bot as _redeem_referral_reward_for_bot,
    referral_config_keyboard as _referral_config_keyboard,
    referral_help_text as _referral_help_text,
    referral_keyboard as _referral_keyboard,
)
from .telegram_bot.renewal_flow import (
    pending_renewal_order as _pending_renewal_order,
    renewal_context_from_state as _renewal_context_from_state,
    renewal_payment_prompt as _renewal_payment_prompt,
    send_renewal_summary as _send_renewal_summary,
    start_renewal_flow as _start_renewal_flow,
)
from .telegram_bot.support_flow import (
    bot_support_contact_value as _bot_support_contact_value,
    create_support_ticket_from_bot as _create_support_ticket_from_bot,
    is_support_user_callback_data as _is_support_user_callback_data,
    select_support_category as _select_support_category,
    start_support_flow as _start_support_flow,
    support_category_keyboard as _support_category_keyboard,
    support_wait_keyboard as _support_wait_keyboard,
)
from .telegram_bot.user_orders_flow import (
    format_user_order_detail as _format_user_order_detail,
    format_user_orders_list as _format_user_orders_list,
    get_bot_order as _get_bot_order,
    handle_user_order_callback as _handle_user_order_callback,
    order_management_keyboard as _order_management_keyboard,
    user_order_summary as _user_order_summary,
    user_orders_keyboard as _user_orders_keyboard,
    visible_bot_orders as _visible_bot_orders,
)
from .telegram_bot.services_flow import (
    active_subscription_lines as _active_subscription_lines,
    bot_client_label,
    bot_client_status,
    bot_subscription_clients,
    bot_user_cache_identity,
    cancel_user_client_delete_flow,
    client_config_keyboard,
    client_config_links,
    client_has_delete_identifier,
    confirm_user_client_delete_flow as _confirm_user_client_delete_flow,
    create_user_client_delete_token,
    format_client_config as _format_client_config,
    get_bot_client,
    get_user_client_delete_payload,
    handle_user_client_config as _handle_user_client_config,
    handle_user_client_refresh as _handle_user_client_refresh,
    handle_user_client_usage as _handle_user_client_usage,
    send_client_config_messages as _send_client_config_messages,
    show_user_services as _show_user_services,
    start_user_client_delete_flow as _start_user_client_delete_flow,
    subscription_management_keyboard,
    user_client_delete_button,
    user_client_delete_cache_key,
    user_client_delete_confirmation_keyboard,
)
from .telegram_bot.user_menu import (
    format_profile,
    help_text,
    main_menu_keyboard,
    profile_keyboard,
    send_help as _send_help,
    send_main_menu as _send_main_menu,
    send_profile,
)
from .telegram_bot.payments import (
    attach_bot_receipt as _attach_bot_receipt,
    bot_order_metadata,
    bot_payment_sender_name,
    build_bot_receipt_metadata,
    copy_payment_value_from_state as _copy_payment_value_from_state,
    download_receipt_content as _download_receipt_content,
    extract_receipt_file,
    format_payment_prompt,
    optional_config_name_keyboard,
    payment_step_keyboard,
    receipt_file_type_error,
    safe_receipt_filename,
    store_payment_lines,
)
from .telegram_bot.notifications import (
    active_bot_configs as _active_bot_configs,
    build_sales_report as _build_sales_report,
    edit_admin_order_message as _edit_admin_order_message,
    extract_sent_message_id as _extract_sent_message_id,
    fit_photo_caption as _fit_photo_caption,
    format_support_message as _format_support_message,
    is_sales_report_due as _is_sales_report_due,
    maybe_send_due_sales_report as _maybe_send_due_sales_report,
    notify_duplicate_order_attempt as _notify_duplicate_order_attempt,
    notify_new_order as _notify_new_order,
    notify_order_event as _notify_order_event,
    notify_support_message as _notify_support_message,
    remember_admin_order_message as _remember_admin_order_message,
    send_due_sales_reports as _send_due_sales_reports,
    send_new_order_to_config as _send_new_order_to_config,
    send_to_config as _send_to_config,
    sync_admin_order_messages as _sync_admin_order_messages,
    support_admin_keyboard as _support_admin_keyboard,
)
from .telegram_bot.order_delivery import (
    approved_order_detail_lines as _approved_order_detail_lines,
    format_customer_order_event as _format_customer_order_event,
    notify_customer_order_event as _notify_customer_order_event,
    order_config_link_groups as _order_config_link_groups,
    order_config_links as _order_config_links,
    send_customer_order_event_message as _send_customer_order_event_message,
)
from .telegram_bot.router import (
    WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_COUNT,
    WEB_TELEGRAM_LINK_INVALID_RATE_LIMIT_WINDOW_SECONDS,
    customer_username_is_available,
    delete_callback_message,
    dispatch_bot_update as _dispatch_bot_update,
    dispatch_user_callback as _dispatch_user_callback,
    dispatch_user_message as _dispatch_user_message,
    extract_callback_message_reference,
    extract_chat_id,
    extract_user_id,
    first_present,
    get_callback_data,
    get_callback_id,
    get_callback_update,
    get_chat_object,
    get_message_id,
    get_message_text,
    get_message_update,
    get_or_create_bot_customer,
    get_sender_object,
    is_web_telegram_link_start_text,
    normalize_bot_username,
    normalize_id,
    referral_code_from_start_text,
    sender_display_name,
    update_bot_user_from_update,
    update_customer_identity_from_bot,
    web_telegram_link_invalid_rate_limited,
    web_telegram_link_token_from_start_text,
)
from .telegram_bot.payment_flow import (
    continue_with_receipt_only as _continue_with_receipt_only,
    handle_buy_wait_name_message as _handle_buy_wait_name_message,
    handle_buy_wait_receipt_message as _handle_buy_wait_receipt_message,
    handle_legacy_receipt_state_message as _handle_legacy_receipt_state_message,
    is_legacy_receipt_state as _is_legacy_receipt_state,
    payment_prompt_after_name as _payment_prompt_after_name,
    show_payment_step as _show_payment_step,
    start_optional_config_name_flow as _start_optional_config_name_flow,
)
from .telegram_bot import order_finalizers as _order_finalizers

logger = logging.getLogger(__name__)

BOT_TIMEOUT_SECONDS = 12
BOT_STATE_CONFIG_LOOKUP_WAIT_LINK = "config_lookup_wait_link"
CONFIG_LOOKUP_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CACHE_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_COUNT = 5
CONFIG_LOOKUP_UPDATE_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
CONFIG_LOOKUP_UPDATE_CALLBACK_PREFIX = "user:config_lookup_update:"
ADMIN_CONFIG_DELETE_CALLBACK_PREFIX = "admin:config_delete:"
ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX = "admin:config_delete_confirm:"
ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX = "admin:config_edit_traffic:"
ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX = "admin:config_traffic_add:"
ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX = "admin:config_traffic_set:"
ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX = "admin:config_traffic_confirm_set:"
ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX = "admin:config_edit_expiry:"
ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX = "admin:config_expiry_add:"
ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX = "admin:config_expiry_set_days:"
ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX = "admin:config_refresh_link:"
ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX = "admin:config_cancel:"
BOT_STATE_ADMIN_CONFIG_WAIT_TRAFFIC_GB = "admin_config_wait_traffic_gb"
BOT_STATE_ADMIN_CONFIG_WAIT_EXPIRY_DAYS = "admin_config_wait_expiry_days"
CONFIG_LOOKUP_RATE_FALLBACK = {}
CONFIG_LOOKUP_UPDATE_RATE_FALLBACK = {}


def active_bot_configs(order=None, *, store=None, reports=False):
    return _active_bot_configs(order, store=store, reports=reports)


def is_sales_report_due(config, *, now=None):
    return _is_sales_report_due(config, now=now)


def maybe_send_due_sales_report(config):
    return _maybe_send_due_sales_report(
        config,
        is_sales_report_due_func=is_sales_report_due,
        build_sales_report_func=build_sales_report,
        send_to_config_func=send_to_config,
        log_event_func=log_event,
    )


def copy_payment_value_from_state(config, bot_user, kind):
    return _copy_payment_value_from_state(
        config,
        bot_user,
        kind,
        get_current_store=get_current_store,
        renewal_context_from_state=renewal_context_from_state,
        purchase_context_from_state=purchase_context_from_state,
        pricing_from_state=pricing_from_state,
        validation_error_cls=ValidationError,
    )

def log_event(config, *, event_type, status, order=None, message="", raw_payload=None):
    return BotEventLog.objects.create(
        bot_config=config,
        order=order,
        event_type=event_type,
        status=status,
        message=sanitize_bot_event_log_value(message),
        raw_payload=sanitize_bot_event_log_value(raw_payload or {}),
    )


def log_callback(config, *, status, message="", order=None, raw_payload=None):
    logger.info("Bot callback: %s", message)
    return log_event(
        config,
        event_type=BotEventLog.EventType.CALLBACK,
        status=status,
        order=order,
        message=message,
        raw_payload=raw_payload or {},
    )


def send_to_config(config, *, text, event_type, order=None, reply_markup=None, chat_id=None):
    return _send_to_config(
        config,
        text=text,
        event_type=event_type,
        order=order,
        reply_markup=reply_markup,
        chat_id=chat_id,
        client_cls=BotClient,
        delivery_error_cls=BotDeliveryError,
        log_event_func=log_event,
    )


def fit_photo_caption(text, *, limit=1000):
    return _fit_photo_caption(text, limit=limit)


def extract_sent_message_id(payload):
    return _extract_sent_message_id(payload)


def remember_admin_order_message(config, order, *, admin_user_id, chat_id, message_id, message_kind, metadata=None):
    return _remember_admin_order_message(
        config,
        order,
        admin_user_id=admin_user_id,
        chat_id=chat_id,
        message_id=message_id,
        message_kind=message_kind,
        metadata=metadata,
    )


def edit_admin_order_message(client, message_ref, text, *, reply_markup=None):
    return _edit_admin_order_message(
        client,
        message_ref,
        text,
        reply_markup=reply_markup,
        delivery_error_cls=BotDeliveryError,
    )


def sync_admin_order_messages(order, *, title, event_type, prefix_message="", respect_notify=True, configs=None):
    return _sync_admin_order_messages(
        order,
        title=title,
        event_type=event_type,
        prefix_message=prefix_message,
        respect_notify=respect_notify,
        configs=configs,
        format_order_message_func=format_order_message,
        active_bot_configs_func=active_bot_configs,
        client_cls=BotClient,
        delivery_error_cls=BotDeliveryError,
        log_event_func=log_event,
        send_to_config_func=send_to_config,
        edit_admin_order_message_func=edit_admin_order_message,
        empty_inline_keyboard_func=empty_inline_keyboard,
    )


def send_new_order_to_config(config, order, *, title="سفارش جدید VPN"):
    return _send_new_order_to_config(
        config,
        order,
        title=title,
        format_order_message_func=format_order_message,
        order_admin_keyboard_func=order_admin_keyboard,
        client_cls=BotClient,
        delivery_error_cls=BotDeliveryError,
        log_event_func=log_event,
        send_to_config_func=send_to_config,
        fit_photo_caption_func=fit_photo_caption,
        extract_sent_message_id_func=extract_sent_message_id,
        remember_admin_order_message_func=remember_admin_order_message,
    )


def notify_new_order(order):
    return _notify_new_order(order)


def notify_duplicate_order_attempt(order):
    return _notify_duplicate_order_attempt(order)


def notify_order_event(order, *, event_type):
    return _notify_order_event(order, event_type=event_type)


def format_customer_order_event(order, *, event_type):
    return _format_customer_order_event(
        order,
        event_type=event_type,
        format_order_message_func=format_order_message,
    )


def order_config_links(order):
    return _order_config_links(order)


def order_config_link_groups(order):
    return _order_config_link_groups(order)


def approved_order_detail_lines(order, *, config_label=""):
    return _approved_order_detail_lines(order, config_label=config_label)


def send_customer_order_event_message(client, order, *, event_type, chat_id, reply_markup=None):
    return _send_customer_order_event_message(
        client,
        order,
        event_type=event_type,
        chat_id=chat_id,
        reply_markup=reply_markup,
        format_customer_order_event_func=format_customer_order_event,
    )


def notify_customer_order_event(order, *, event_type):
    return _notify_customer_order_event(
        order,
        event_type=event_type,
        client_cls=BotClient,
        delivery_error_cls=BotDeliveryError,
        log_event_func=log_event,
        send_customer_order_event_message_func=send_customer_order_event_message,
    )


def support_admin_keyboard(conversation):
    return _support_admin_keyboard(conversation)


def format_support_message(conversation, message=None, *, title="پیام پشتیبانی"):
    return _format_support_message(conversation, message, title=title)


def notify_support_message(conversation, message):
    return _notify_support_message(conversation, message)

def is_admin_callback_data(data):
    action, tracking_code = parse_callback_data(data)
    return bool(action and tracking_code)


def parse_support_callback_data(data):
    return _parse_support_callback_data(data)


def is_support_callback_data(data):
    return _is_support_callback_data(data)

CUSTOMER_ANALYTICS_CALLBACK_PREFIX = "admin:ca:"
BROADCAST_CALLBACK_PREFIX = "admin:bc:"
BOT_STATE_BUY_WAIT_DISCOUNT = "buy_wait_discount"
BOT_STATE_SUPPORT_WAIT_MESSAGE = "support_wait_message"
BOT_SUPPORT_CATEGORIES = {
    "payment": "مشکل پرداخت",
    "connection": "مشکل اتصال",
    "renewal": "تمدید",
    "other": "سایر",
}


CUSTOMER_ANALYTICS_REPORTS = _CUSTOMER_ANALYTICS_REPORTS


def is_customer_analytics_callback_data(data):
    return _is_customer_analytics_callback_data(data)


def customer_analytics_keyboard():
    return _customer_analytics_keyboard()


def customer_analytics_menu_text():
    return _customer_analytics_menu_text()


def customer_analytics_customer_name(customer):
    return _customer_analytics_customer_name(customer)


def customer_analytics_contact(customer):
    return _customer_analytics_contact(customer)


def customer_analytics_empty_message(title):
    return _customer_analytics_empty_message(title)


def format_customer_analytics_report(report_key, *, config=None):
    return _format_customer_analytics_report(report_key, config=config)


BROADCAST_AUDIENCE_LABELS = _BROADCAST_AUDIENCE_LABELS
BROADCAST_CHANNEL_LABELS = _BROADCAST_CHANNEL_LABELS


def is_broadcast_callback_data(data):
    return _is_broadcast_callback_data(data)


def broadcast_audience_keyboard():
    return _broadcast_audience_keyboard()


def broadcast_confirm_keyboard():
    return _broadcast_confirm_keyboard()


def broadcast_menu_text(config):
    return _broadcast_menu_text(config)


def broadcast_campaign_preview(config, *, audience_type, message_text, channel=None):
    return _broadcast_campaign_preview(
        config,
        audience_type=audience_type,
        message_text=message_text,
        channel=channel,
    )


def format_broadcast_preview(*, audience_type, channel, message_text, counts):
    return _format_broadcast_preview(
        audience_type=audience_type,
        channel=channel,
        message_text=message_text,
        counts=counts,
    )


def format_broadcast_result(campaign, counts):
    return _format_broadcast_result(campaign, counts)


def start_broadcast_flow(client, config, bot_user, *, chat_id):
    return _start_broadcast_flow(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        get_current_store_func=get_current_store,
    )


def select_broadcast_audience(client, config, bot_user, audience_type, *, chat_id):
    return _select_broadcast_audience(
        client,
        config,
        bot_user,
        audience_type,
        chat_id=chat_id,
        cancel_keyboard_func=cancel_keyboard,
    )


def handle_broadcast_text(config, bot_user, message_text, *, chat_id):
    return _handle_broadcast_text(
        config,
        bot_user,
        message_text,
        chat_id=chat_id,
        client_cls=BotClient,
        is_admin_bot_user_func=is_admin_bot_user,
        cancel_keyboard_func=cancel_keyboard,
    )


def send_confirmed_broadcast(client, config, bot_user, *, chat_id):
    return _send_confirmed_broadcast(client, config, bot_user, chat_id=chat_id)


def handle_broadcast_callback_update(config, callback_query, *, chat_id):
    return _handle_broadcast_callback_update(
        config,
        callback_query,
        chat_id=chat_id,
        client_cls=BotClient,
        get_callback_id_func=get_callback_id,
        get_callback_data_func=get_callback_data,
        normalize_id_func=normalize_id,
        get_sender_object_func=get_sender_object,
        delete_callback_message_func=delete_callback_message,
        update_bot_user_from_update_func=update_bot_user_from_update,
        cancel_keyboard_func=cancel_keyboard,
    )


ADMIN_MENU_CALLBACKS = {"admin:orders:pending", "admin:sales_report", "admin:quick_settings"}


def is_admin_menu_callback_data(data):
    return str(data or "") in ADMIN_MENU_CALLBACKS


ADMIN_CONFIG_CALLBACK_PREFIXES = (
    ADMIN_CONFIG_DELETE_CALLBACK_PREFIX,
    ADMIN_CONFIG_DELETE_CONFIRM_CALLBACK_PREFIX,
    ADMIN_CONFIG_EDIT_TRAFFIC_CALLBACK_PREFIX,
    ADMIN_CONFIG_TRAFFIC_ADD_CALLBACK_PREFIX,
    ADMIN_CONFIG_TRAFFIC_SET_CALLBACK_PREFIX,
    ADMIN_CONFIG_TRAFFIC_CONFIRM_SET_CALLBACK_PREFIX,
    ADMIN_CONFIG_EDIT_EXPIRY_CALLBACK_PREFIX,
    ADMIN_CONFIG_EXPIRY_ADD_CALLBACK_PREFIX,
    ADMIN_CONFIG_EXPIRY_SET_DAYS_CALLBACK_PREFIX,
    ADMIN_CONFIG_REFRESH_LINK_CALLBACK_PREFIX,
    ADMIN_CONFIG_CANCEL_CALLBACK_PREFIX,
)


def is_admin_config_management_callback_data(data):
    data = str(data or "")
    return any(data.startswith(prefix) for prefix in ADMIN_CONFIG_CALLBACK_PREFIXES)


def admin_pending_orders_keyboard(orders):
    return _admin_pending_orders_keyboard(orders)


def admin_pending_orders_text(config):
    return _admin_pending_orders_text(config)


def admin_sales_report_text(config):
    return _admin_sales_report_text(config)


def admin_quick_settings_text(config):
    return _admin_quick_settings_text(config, get_current_store_func=get_current_store)


def handle_admin_menu_callback_update(config, callback_query, *, chat_id):
    callback_id = get_callback_id(callback_query)
    data = get_callback_data(callback_query)
    admin_user_id = normalize_id(get_sender_object(callback_query))
    client = BotClient(config)
    client.answer_callback(callback_id, "دریافت شد")
    if not (config.is_admin_user(admin_user_id) or config.is_admin_user(chat_id)):
        client.send_message("شما اجازه دسترسی به منوی ادمین را ندارید.", chat_id=chat_id)
        return {"ok": True, "success": False, "permission_denied": True}

    if data == "admin:orders:pending":
        return _handle_admin_orders_menu_callback(client, config, data, chat_id=chat_id)
    if _is_admin_report_menu_callback_data(data):
        return _handle_admin_report_menu_callback(
            client,
            config,
            data,
            chat_id=chat_id,
            get_current_store_func=get_current_store,
        )
    return {"ok": True, "ignored": True}


def referral_keyboard(bot_user, config=None):
    return _referral_keyboard(bot_user, config, get_current_store_func=get_current_store)


def free_trial_keyboard():
    return _free_trial_keyboard()


def referral_config_keyboard(bot_user):
    return _referral_config_keyboard(bot_user)


def format_referral_panel(bot_user, config):
    return _format_referral_panel(bot_user, config, get_current_store_func=get_current_store)


def referral_help_text():
    return _referral_help_text()


def support_category_keyboard():
    return _support_category_keyboard()


def support_wait_keyboard():
    return _support_wait_keyboard()


def start_support_flow(client, config, bot_user, *, chat_id):
    return _start_support_flow(client, config, bot_user, chat_id=chat_id)


def select_support_category(client, bot_user, category, *, chat_id):
    return _select_support_category(client, bot_user, category, chat_id=chat_id)


def bot_support_contact_value(bot_user):
    return _bot_support_contact_value(bot_user)


def create_support_ticket_from_bot(config, bot_user, text, message, *, chat_id):
    return _create_support_ticket_from_bot(
        config,
        bot_user,
        text,
        message,
        chat_id=chat_id,
        client_cls=BotClient,
        notify_support_message_func=notify_support_message,
        get_message_id_func=get_message_id,
        is_admin_bot_user_func=is_admin_bot_user,
        get_current_store_func=get_current_store,
    )


def redeem_referral_reward_for_bot(client, bot_user, *, chat_id, public_id=""):
    return _redeem_referral_reward_for_bot(client, bot_user, chat_id=chat_id, public_id=public_id)


def start_free_trial_flow(client, config, bot_user, *, chat_id):
    return _start_free_trial_flow(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        is_admin_bot_user_func=is_admin_bot_user,
    )


def confirm_free_trial_flow(client, config, bot_user, *, chat_id):
    return _confirm_free_trial_flow(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        is_admin_bot_user_func=is_admin_bot_user,
    )


def cancel_free_trial_flow(client, config, bot_user, *, chat_id):
    return _cancel_free_trial_flow(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        is_admin_bot_user_func=is_admin_bot_user,
    )


def cancel_keyboard():
    return {"inline_keyboard": [[{"text": "انصراف", "callback_data": "user:cancel"}]]}


def config_lookup_keyboard():
    return _config_lookup_keyboard()


def purchase_summary_keyboard(*, has_discount=False):
    return _purchase_summary_keyboard(has_discount=has_discount)


def discount_code_keyboard():
    return _discount_code_keyboard()


def remove_reply_keyboard():
    return {"remove_keyboard": True}


def phone_request_keyboard():
    return _phone_request_keyboard()


def quantity_keyboard():
    return _quantity_keyboard()


def operator_keyboard(operators):
    return _operator_keyboard(operators)


def normalize_bot_phone_number(value):
    return _normalize_bot_phone_number(value)


def is_valid_bot_phone_number(phone_number):
    return _is_valid_bot_phone_number(phone_number)


def extract_contact_phone(message):
    return _extract_contact_phone(message)


def save_bot_user_phone(bot_user, raw_phone):
    return _save_bot_user_phone(
        bot_user,
        raw_phone,
        get_or_create_bot_customer_func=get_or_create_bot_customer,
        update_customer_identity_from_bot_func=update_customer_identity_from_bot,
        customer_username_is_available_func=customer_username_is_available,
        normalize_bot_username_func=normalize_bot_username,
    )


def custom_volume_prompt_text(store):
    return _custom_volume_prompt_text(store)


def plan_button_label(plan):
    return _plan_button_label(plan)


def plan_keyboard(plans, *, prefix="user:buyplan", custom_volume=False, operator=None):
    return _plan_keyboard(plans, prefix=prefix, custom_volume=custom_volume, operator=operator)


def format_operator_lines(operators):
    return _format_operator_lines(operators)


def format_plan_lines(plans, *, store=None, custom_volume=False, operator=None):
    return _format_plan_lines(plans, store=store, custom_volume=custom_volume, operator=operator)


def bot_discount_error_message(message):
    return _bot_discount_error_message(message)


def calculate_bot_pricing(plan, quantity=1, *, customer=None, discount_code=""):
    return _calculate_bot_pricing(plan, quantity, customer=customer, discount_code=discount_code)


def pricing_from_state(plan, quantity=1, *, customer=None, data=None):
    return _pricing_from_state(plan, quantity, customer=customer, data=data)


def format_purchase_summary(store, plan, *, quantity=1, operator=None, pricing=None, flow="purchase", vpn_client=None):
    return _format_purchase_summary(
        store,
        plan,
        quantity=quantity,
        operator=operator,
        pricing=pricing,
        flow=flow,
        vpn_client=vpn_client,
    )


def user_order_summary(order):
    return _user_order_summary(order)


def start_profile_phone_flow(client, bot_user, *, chat_id):
    return _start_profile_phone_flow(client, bot_user, chat_id=chat_id)


def start_config_lookup_flow(client, bot_user, *, chat_id):
    return _start_config_lookup_flow(client, bot_user, chat_id=chat_id)


def config_lookup_rate_key(config, bot_user):
    return _config_lookup_rate_key(config, bot_user)


def config_lookup_rate_limited(config, bot_user):
    return _config_lookup_rate_limited(config, bot_user)


def config_lookup_update_rate_key(config, bot_user):
    return _config_lookup_update_rate_key(config, bot_user)


def config_lookup_update_rate_limited(config, bot_user):
    return _config_lookup_update_rate_limited(config, bot_user)


def config_lookup_update_cache_key(config, bot_user, token):
    return _config_lookup_update_cache_key(config, bot_user, token)


def admin_config_management_cache_key(config, admin_user_id, token):
    return _admin_config_management_cache_key(config, admin_user_id, token)


def create_admin_config_management_token(config, bot_user, result):
    return _create_admin_config_management_token(config, bot_user, result)


def get_admin_config_management_payload(config, admin_user_id, token):
    return _get_admin_config_management_payload(config, admin_user_id, token)


CONFIG_LOOKUP_NO_UPDATE_MESSAGE = "این کانفیگ آپدیت ندارد."


def create_config_lookup_update_token(config, bot_user, result, *, original_config_text=""):
    return _create_config_lookup_update_token(
        config,
        bot_user,
        result,
        original_config_text=original_config_text,
    )


def config_lookup_result_keyboard(
    config,
    bot_user,
    result,
    *,
    original_config_text="",
    is_admin_bot_user_func=None,
    create_admin_config_management_token_func=None,
    create_config_lookup_update_token_func=None,
):
    return _config_lookup_result_keyboard(
        config,
        bot_user,
        result,
        original_config_text=original_config_text,
        is_admin_bot_user_func=is_admin_bot_user_func or is_admin_bot_user,
        create_admin_config_management_token_func=(
            create_admin_config_management_token_func or create_admin_config_management_token
        ),
        create_config_lookup_update_token_func=(
            create_config_lookup_update_token_func or create_config_lookup_update_token
        ),
    )


def admin_config_delete_confirmation_keyboard(token):
    return _admin_config_delete_confirmation_keyboard(token)


def admin_config_traffic_keyboard(token):
    return _admin_config_traffic_keyboard(token)


def admin_config_expiry_keyboard(token):
    return _admin_config_expiry_keyboard(token)


def admin_config_traffic_confirm_keyboard(token, gb):
    return _admin_config_traffic_confirm_keyboard(token, gb)


def append_config_lookup_panel_errors(text, result, *, is_admin=False):
    return _append_config_lookup_panel_errors(text, result, is_admin=is_admin)


def handle_config_lookup_text(config, bot_user, text, *, chat_id):
    return _handle_config_lookup_text(
        config,
        bot_user,
        text,
        chat_id=chat_id,
        client_cls=BotClient,
        is_admin_bot_user_func=is_admin_bot_user,
        check_config_usage_func=check_config_usage,
        get_current_store_func=get_current_store,
        config_lookup_result_keyboard_func=config_lookup_result_keyboard,
    )


def handle_config_lookup_update_callback(config, bot_user, data, *, client, chat_id):
    return _handle_config_lookup_update_callback(
        config,
        bot_user,
        data,
        client=client,
        chat_id=chat_id,
        is_admin_bot_user_func=is_admin_bot_user,
        build_config_link_for_identifier_func=build_config_link_for_identifier,
    )


def _token_after_prefix(data, prefix):
    return str(data or "")[len(prefix) :].strip()


def _split_token_value(data, prefix):
    remainder = _token_after_prefix(data, prefix)
    if ":" not in remainder:
        return remainder, ""
    token, value = remainder.rsplit(":", 1)
    return token, value


def _admin_config_payload_or_message(config, bot_user, token, client, *, chat_id):
    payload = get_admin_config_management_payload(config, bot_user_cache_identity(bot_user), token)
    if payload:
        return payload
    client.send_message(
        "مهلت مدیریت این کانفیگ تمام شده. دوباره از مسیر مشاهده باقی‌مانده لینک را ارسال کنید.",
        chat_id=chat_id,
        reply_markup=main_menu_keyboard(is_admin=True),
    )
    return None


def _admin_config_error_message(client, message, *, chat_id, token=""):
    client.send_message(
        message or "عملیات انجام نشد. چند دقیقه دیگر تلاش کنید.",
        chat_id=chat_id,
        reply_markup=admin_config_traffic_keyboard(token) if token else main_menu_keyboard(is_admin=True),
    )


def handle_admin_config_management_callback_update(config, callback_query, *, chat_id):
    return _handle_admin_config_management_callback_update(
        config,
        callback_query,
        chat_id=chat_id,
        client_cls=BotClient,
        get_callback_id_func=get_callback_id,
        get_callback_data_func=get_callback_data,
        normalize_id_func=normalize_id,
        get_sender_object_func=get_sender_object,
        delete_callback_message_func=delete_callback_message,
        update_bot_user_from_update_func=update_bot_user_from_update,
        get_admin_config_management_payload_func=get_admin_config_management_payload,
        bot_user_cache_identity_func=bot_user_cache_identity,
        delete_vpn_client_by_admin_lookup_func=delete_vpn_client_by_admin_lookup,
        update_vpn_client_limits_by_admin_func=update_vpn_client_limits_by_admin,
        refresh_vpn_client_link_by_admin_func=refresh_vpn_client_link_by_admin,
        build_admin_delete_confirmation_text_func=build_admin_delete_confirmation_text,
        build_admin_edit_summary_func=build_admin_edit_summary,
        vpn_client_management_error_cls=VPNClientManagementError,
    )


def handle_admin_config_waiting_text(config, bot_user, text, *, chat_id):
    return _handle_admin_config_waiting_text(
        config,
        bot_user,
        text,
        chat_id=chat_id,
        client_cls=BotClient,
        get_admin_config_management_payload_func=get_admin_config_management_payload,
        bot_user_cache_identity_func=bot_user_cache_identity,
        normalize_bot_number_func=normalize_bot_number,
        gb_to_bytes_func=gb_to_bytes,
        update_vpn_client_limits_by_admin_func=update_vpn_client_limits_by_admin,
        build_admin_edit_summary_func=build_admin_edit_summary,
        vpn_client_management_error_cls=VPNClientManagementError,
    )


def active_subscription_lines(bot_user, *, title="اشتراک‌های شما", force_refresh=False):
    return _active_subscription_lines(
        bot_user,
        title=title,
        force_refresh=force_refresh,
        sync_stats=sync_vpn_client_stats,
    )


def visible_bot_orders(bot_user, *, limit=10):
    return _visible_bot_orders(bot_user, limit=limit)


def user_orders_keyboard(orders):
    return _user_orders_keyboard(orders, bot_order_status_func=bot_order_status)


def order_management_keyboard(order, bot_user=None):
    return _order_management_keyboard(
        order,
        bot_user,
        user_client_delete_button_func=user_client_delete_button,
    )


def format_user_orders_list(bot_user):
    return _format_user_orders_list(bot_user, bot_order_status_func=bot_order_status)


def format_user_order_detail(order):
    return _format_user_order_detail(
        order,
        bot_verification_status_func=bot_verification_status,
        bot_order_status_func=bot_order_status,
        bot_client_label_func=bot_client_label,
        bot_client_status_func=bot_client_status,
        sync_vpn_client_stats_func=sync_vpn_client_stats,
    )


def get_bot_order(bot_user, tracking_code):
    return _get_bot_order(bot_user, tracking_code)


PENDING_RENEWAL_STATUSES = {
    Order.Status.PENDING_PAYMENT,
    Order.Status.PENDING_VERIFICATION,
    Order.Status.CONFIRMED,
}


def pending_renewal_order(bot_user, vpn_client):
    return _pending_renewal_order(bot_user, vpn_client)


def renewal_payment_prompt(store, vpn_client, *, pricing=None):
    return _renewal_payment_prompt(store, vpn_client, pricing=pricing)


def start_renewal_flow(client, config, bot_user, public_id, *, chat_id):
    return _start_renewal_flow(
        client,
        config,
        bot_user,
        public_id,
        chat_id=chat_id,
        format_user_order_detail_func=format_user_order_detail,
        order_management_keyboard_func=order_management_keyboard,
    )


def format_client_config(client, *, stats=None, refreshed=False, include_config_notice=True):
    return _format_client_config(
        client,
        stats=stats,
        refreshed=refreshed,
        include_config_notice=include_config_notice,
        sync_stats=sync_vpn_client_stats,
    )


def send_client_config_messages(client, vpn_client, *, chat_id, stats=None, refreshed=False, bot_user=None):
    return _send_client_config_messages(
        client,
        vpn_client,
        chat_id=chat_id,
        stats=stats,
        refreshed=refreshed,
        bot_user=bot_user,
        sync_stats=sync_vpn_client_stats,
    )


def start_user_client_delete_flow(client, config, bot_user, token, *, chat_id):
    return _start_user_client_delete_flow(
        client,
        config,
        bot_user,
        token,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(config, bot_user),
        get_client=get_bot_client,
        pending_renewal_order=pending_renewal_order,
        order_management_keyboard=order_management_keyboard,
    )


def confirm_user_client_delete_flow(client, config, bot_user, token, *, chat_id):
    return _confirm_user_client_delete_flow(
        client,
        config,
        bot_user,
        token,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(config, bot_user),
        get_client=get_bot_client,
        delete_client_for_user=delete_vpn_client_for_user,
    )


def download_receipt_content(client, file_info, metadata):
    return _download_receipt_content(client, file_info, metadata, delivery_error_cls=BotDeliveryError)


def attach_bot_receipt(client, config, file_info, metadata):
    return _attach_bot_receipt(client, config, file_info, metadata, delivery_error_cls=BotDeliveryError)


def send_final_order_status(client, order, *, title, chat_id, callback_query=None, prefix_message=""):
    text = format_order_message(order, title=title)
    if prefix_message:
        text = f"{prefix_message}\n\n{text}"

    message_ref = extract_callback_message_reference(callback_query or {}, fallback_chat_id=chat_id)
    try:
        if message_ref["chat_id"] and message_ref["message_id"]:
            client.edit_message(
                chat_id=message_ref["chat_id"],
                message_id=message_ref["message_id"],
                text=text,
                reply_markup=empty_inline_keyboard(),
            )
            return
    except BotDeliveryError:
        pass

    client.send_message(text, chat_id=chat_id)


def handle_bot_update(provider, webhook_secret, update, *, source="webhook"):
    config = BotConfiguration.objects.filter(
        provider=provider,
        webhook_secret=webhook_secret,
        is_active=True,
    ).first()
    if not config:
        logger.warning("Bot update ignored: config not found provider=%s secret=%s source=%s", provider, webhook_secret, source)
        return {"ok": False, "error": "Bot configuration not found."}

    safe_update = sanitize_bot_update_for_logging(update)
    logger.info("Incoming bot update provider=%s config=%s source=%s payload=%s", provider, config.pk, source, safe_update)
    log_event(
        config,
        event_type=BotEventLog.EventType.WEBHOOK,
        status=BotEventLog.Status.RECEIVED,
        message=f"Incoming {source} update for provider={provider}",
        raw_payload=safe_update,
    )
    maybe_send_due_sales_report(config)

    callback_query = get_callback_update(update)
    message = get_message_update(update)
    user_id = extract_user_id(update)
    chat_id = extract_chat_id(update)
    is_admin = config.is_admin_user(user_id) or config.is_admin_user(chat_id)
    logger.info("Bot update extraction config=%s source=%s user_id=%s chat_id=%s is_admin=%s", config.pk, source, user_id, chat_id, is_admin)

    callback_data = get_callback_data(callback_query) if callback_query else ""
    return _dispatch_bot_update(
        config=config,
        update=update,
        callback_query=callback_query,
        message=message,
        user_id=user_id,
        chat_id=chat_id,
        is_admin=is_admin,
        callback_data=callback_data,
        deps=globals(),
    )


def parse_callback_data(data):
    data = (data or "").strip()
    parts = data.split(":")
    if len(parts) == 2 and parts[0] in {"approve", "reject", "detail"}:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[0] == "order" and parts[1] in {"approve", "reject", "detail"}:
        return parts[1], parts[2]
    return "", ""


def handle_user_update(config, update, *, chat_id, user_id):
    if not user_id or not chat_id:
        return {"ok": True, "ignored": True}

    message = get_message_update(update)
    attach_customer = not (
        message and is_web_telegram_link_start_text(get_message_text(message))
    )
    previous_bot_user = (
        BotUser.objects.filter(bot_config=config, provider_user_id=str(user_id))
        .only("pk", "last_seen_at")
        .first()
    )
    previous_last_seen_at = getattr(previous_bot_user, "last_seen_at", None)
    bot_user = update_bot_user_from_update(
        config,
        update,
        chat_id=chat_id,
        user_id=user_id,
        attach_customer=attach_customer,
    )
    try:
        from .revenue_engine.triggers import USER_ACTIVE, safe_emit_event

        safe_emit_event(
            USER_ACTIVE,
            bot_user,
            {
                "bot_user": bot_user,
                "chat_id": chat_id,
                "user_id": user_id,
                "source": "bot_update",
            },
        )
    except Exception as exc:
        logger.warning("Revenue USER_ACTIVE hook skipped bot_user=%s: %s", bot_user.pk, exc)
    if previous_last_seen_at and previous_last_seen_at <= timezone.now() - timedelta(hours=72):
        try:
            from .revenue_engine.retention.triggers import USER_RETURNED_AFTER_ABSENCE, safe_emit_event

            safe_emit_event(
                USER_RETURNED_AFTER_ABSENCE,
                bot_user,
                {
                    "bot_user": bot_user,
                    "chat_id": chat_id,
                    "bot_config": config,
                    "customer": bot_user.customer,
                    "previous_last_active_at": previous_last_seen_at,
                    "source": "bot_update",
                },
            )
        except Exception as exc:
            logger.warning("Retention return hook skipped bot_user=%s: %s", bot_user.pk, exc)
    callback_query = get_callback_update(update)
    if callback_query:
        return handle_user_callback(config, bot_user, callback_query, chat_id=chat_id)

    if message:
        return handle_user_message(config, bot_user, message, chat_id=chat_id)

    return {"ok": True, "ignored": True}


def is_admin_bot_user(config, bot_user):
    return config.is_admin_user(bot_user.provider_user_id) or config.is_admin_user(bot_user.chat_id)


def telegram_membership_required_response(config, bot_user, client, *, chat_id, force_refresh=False):
    if ensure_telegram_membership(
        config,
        bot_user,
        client=client,
        chat_id=chat_id,
        force_refresh=force_refresh,
    ):
        return None
    return {"ok": True, "membership_required": True}


def send_main_menu(client, bot_user, *, chat_id):
    return _send_main_menu(
        client,
        bot_user,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(client.config, bot_user),
    )


def send_help(client, config, bot_user, *, chat_id):
    return _send_help(client, chat_id=chat_id, is_admin=is_admin_bot_user(config, bot_user))


def show_user_services(client, bot_user, *, chat_id, title="اشتراک‌های شما", force_refresh=False, renew_mode=False):
    return _show_user_services(
        client,
        bot_user,
        chat_id=chat_id,
        title=title,
        force_refresh=force_refresh,
        renew_mode=renew_mode,
        sync_stats=sync_vpn_client_stats,
    )


def handle_user_client_usage(client, config, bot_user, public_id, *, chat_id):
    return _handle_user_client_usage(
        client,
        bot_user,
        public_id,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(config, bot_user),
        get_client=get_bot_client,
        sync_stats=sync_vpn_client_stats,
    )


def handle_user_client_config(client, config, bot_user, public_id, *, chat_id):
    return _handle_user_client_config(
        client,
        bot_user,
        public_id,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(config, bot_user),
        get_client=get_bot_client,
        sync_stats=sync_vpn_client_stats,
    )


def handle_user_client_refresh(client, config, bot_user, public_id, *, chat_id):
    return _handle_user_client_refresh(
        client,
        bot_user,
        public_id,
        chat_id=chat_id,
        is_admin=is_admin_bot_user(config, bot_user),
        get_client=get_bot_client,
        refresh_links=refresh_vpn_client_links,
        sync_stats=sync_vpn_client_stats,
    )


def parse_buy_plan_callback(data):
    return _parse_buy_plan_callback(data)


def parse_buy_custom_callback(data):
    return _parse_buy_custom_callback(data)


def get_purchase_operator_from_state(store, data):
    return _get_purchase_operator_from_state(store, data)


def send_operator_list(client, store, *, chat_id):
    return _send_operator_list(client, store, chat_id=chat_id)


def send_plan_list(client, config, *, chat_id, buy_mode=False, operator=None):
    return _send_plan_list(client, config, chat_id=chat_id, buy_mode=buy_mode, operator=operator)


def get_selected_purchase_plan(store, plan_id, *, operator=None):
    return _get_selected_purchase_plan(store, plan_id, operator=operator)


def purchase_context_from_state(config, bot_user):
    return _purchase_context_from_state(config, bot_user)


def renewal_context_from_state(config, bot_user):
    return _renewal_context_from_state(config, bot_user)


def send_purchase_summary(client, config, bot_user, *, chat_id):
    return _send_purchase_summary(client, config, bot_user, chat_id=chat_id)


def send_renewal_summary(client, config, bot_user, *, chat_id):
    return _send_renewal_summary(client, config, bot_user, chat_id=chat_id)


def send_current_order_summary(client, config, bot_user, *, chat_id):
    return _send_current_order_summary(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        send_renewal_summary_func=send_renewal_summary,
    )


def show_payment_step(client, config, bot_user, *, chat_id):
    return _show_payment_step(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        renewal_context_from_state_func=renewal_context_from_state,
        purchase_context_from_state_func=purchase_context_from_state,
        pricing_from_state_func=pricing_from_state,
        main_menu_keyboard_func=main_menu_keyboard,
        renewal_payment_prompt_func=renewal_payment_prompt,
        payment_step_keyboard_func=payment_step_keyboard,
        format_payment_prompt_func=format_payment_prompt,
    )


def start_optional_config_name_flow(client, config, bot_user, *, chat_id):
    return _start_optional_config_name_flow(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        renewal_context_from_state_func=renewal_context_from_state,
        purchase_context_from_state_func=purchase_context_from_state,
        main_menu_keyboard_func=main_menu_keyboard,
        optional_config_name_keyboard_func=optional_config_name_keyboard,
    )


def continue_with_receipt_only(client, config, bot_user, *, chat_id):
    return _continue_with_receipt_only(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        show_payment_step_func=show_payment_step,
    )


def payment_prompt_after_name(config, bot_user):
    return _payment_prompt_after_name(
        config,
        bot_user,
        renewal_context_from_state_func=renewal_context_from_state,
        purchase_context_from_state_func=purchase_context_from_state,
        pricing_from_state_func=pricing_from_state,
        renewal_payment_prompt_func=renewal_payment_prompt,
        format_payment_prompt_func=format_payment_prompt,
    )


def start_discount_code_flow(client, config, bot_user, *, chat_id):
    return _start_discount_code_flow(client, config, bot_user, chat_id=chat_id)


def apply_discount_code_for_bot(client, config, bot_user, code, *, chat_id):
    return _apply_discount_code_for_bot(
        client,
        config,
        bot_user,
        code,
        chat_id=chat_id,
        renewal_context_from_state_func=renewal_context_from_state,
        send_renewal_summary_func=send_renewal_summary,
    )


def skip_discount_for_bot(client, config, bot_user, *, chat_id):
    return _skip_discount_for_bot(
        client,
        config,
        bot_user,
        chat_id=chat_id,
        show_payment_step_func=show_payment_step,
    )


def start_purchase_flow(client, config, bot_user, plan_id, *, chat_id, operator_id=None):
    return _start_purchase_flow(client, config, bot_user, plan_id, chat_id=chat_id, operator_id=operator_id)


def continue_purchase_after_quantity(client, config, bot_user, quantity, *, chat_id):
    return _continue_purchase_after_quantity(client, config, bot_user, quantity, chat_id=chat_id)


def start_custom_volume_flow(client, config, bot_user, *, chat_id, operator_id=None):
    return _start_custom_volume_flow(client, config, bot_user, chat_id=chat_id, operator_id=operator_id)


def continue_purchase_after_custom_volume(client, config, bot_user, volume_value, *, chat_id):
    return _continue_purchase_after_custom_volume(client, config, bot_user, volume_value, chat_id=chat_id)


def track_offer_click(config, bot_user, *, chat_id, offer_type=None, source="callback"):
    try:
        from .revenue_engine.optimization.tracker import OfferTracker, resolve_offer_user_id

        OfferTracker().user_clicked_offer(
            resolve_offer_user_id(bot_user, {"bot_user": bot_user, "chat_id": chat_id, "bot_config": config}),
            offer_type=offer_type,
            bot_config=config,
            metadata={"source": source},
        )
    except Exception as exc:
        logger.warning("Offer click tracking skipped bot_user=%s: %s", getattr(bot_user, "pk", None), exc)


def handle_user_callback(config, bot_user, callback_query, *, chat_id):
    data = get_callback_data(callback_query)
    if data == "user:upsell_skip":
        client = BotClient(config)
        callback_id = get_callback_id(callback_query)
        client.answer_callback(callback_id, "باشه")
        delete_callback_message(client, callback_query, fallback_chat_id=chat_id)
        track_offer_click(config, bot_user, chat_id=chat_id, offer_type="upsell", source="upsell_skip")
        try:
            from .revenue_engine.upsell.actions import mark_upsell_skipped

            mark_upsell_skipped(bot_user, {"bot_user": bot_user, "chat_id": chat_id, "bot_config": config})
        except Exception as exc:
            logger.warning("Could not mark upsell skip bot_user=%s: %s", bot_user.pk, exc)
        client.send_message("باشه، فعلاً همین پلن را ادامه می‌دهیم.", chat_id=chat_id)
        return {"ok": True, "handled": True, "upsell_skipped": True}
    if data == "user:retention_ignore":
        client = BotClient(config)
        callback_id = get_callback_id(callback_query)
        client.answer_callback(callback_id, "باشه")
        delete_callback_message(client, callback_query, fallback_chat_id=chat_id)
        track_offer_click(config, bot_user, chat_id=chat_id, offer_type="retention", source="retention_ignore")
        try:
            from .revenue_engine.retention.actions import mark_retention_ignored

            mark_retention_ignored(bot_user, {"bot_user": bot_user, "chat_id": chat_id, "bot_config": config})
        except Exception as exc:
            logger.warning("Could not mark retention ignored bot_user=%s: %s", bot_user.pk, exc)
        client.send_message("باشه، بعداً مزاحم نمی‌شویم.", chat_id=chat_id)
        return {"ok": True, "handled": True, "retention_ignored": True}
    if data == "user:buy":
        track_offer_click(config, bot_user, chat_id=chat_id, source="offer_cta")

    return _dispatch_user_callback(
        config,
        bot_user,
        callback_query,
        chat_id=chat_id,
        deps=globals(),
    )


def handle_user_message(config, bot_user, message, *, chat_id):
    return _dispatch_user_message(
        config,
        bot_user,
        message,
        chat_id=chat_id,
        deps=globals(),
    )


def _sync_order_finalizer_compat_deps():
    _order_finalizers.BotClient = BotClient
    _order_finalizers.BotDeliveryError = BotDeliveryError
    _order_finalizers.OPERATOR_REQUIRED_MESSAGE = OPERATOR_REQUIRED_MESSAGE
    _order_finalizers.create_manual_payment_order = create_manual_payment_order
    _order_finalizers.create_renewal_payment_order = create_renewal_payment_order
    _order_finalizers.get_active_operators = get_active_operators
    _order_finalizers.get_current_store = get_current_store
    _order_finalizers.sales_mode_requires_operator = sales_mode_requires_operator
    _order_finalizers.validate_payment_receipt_image = validate_payment_receipt_image
    _order_finalizers.attach_bot_receipt = attach_bot_receipt
    _order_finalizers.cancel_keyboard = cancel_keyboard
    _order_finalizers.format_user_order_detail = format_user_order_detail
    _order_finalizers.get_bot_client = get_bot_client
    _order_finalizers.get_purchase_operator_from_state = get_purchase_operator_from_state
    _order_finalizers.get_selected_purchase_plan = get_selected_purchase_plan
    _order_finalizers.main_menu_keyboard = main_menu_keyboard
    _order_finalizers.operator_keyboard = operator_keyboard
    _order_finalizers.order_management_keyboard = order_management_keyboard
    _order_finalizers.pending_renewal_order = pending_renewal_order
    _order_finalizers.send_customer_order_event_message = send_customer_order_event_message
    _order_finalizers.sync_vpn_client_stats = sync_vpn_client_stats
    _order_finalizers.user_order_summary = user_order_summary
    _order_finalizers.log_event = log_event


def create_bot_payment_order(*, config, bot_user, plan, metadata, receipt_image=None, require_receipt_image=False):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.create_bot_payment_order(
        config=config,
        bot_user=bot_user,
        plan=plan,
        metadata=metadata,
        receipt_image=receipt_image,
        require_receipt_image=require_receipt_image,
    )


def submit_renewal_payment(order, bot_user, *, receipt_image=None, require_receipt_image=False):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.submit_renewal_payment(
        order,
        bot_user,
        receipt_image=receipt_image,
        require_receipt_image=require_receipt_image,
    )


def create_bot_renewal_order(config, bot_user, vpn_client, metadata):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.create_bot_renewal_order(config, bot_user, vpn_client, metadata)


def finalize_admin_direct_renewal(config, bot_user, *, chat_id):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.finalize_admin_direct_renewal(config, bot_user, chat_id=chat_id)


def finalize_admin_direct_purchase(config, bot_user, *, chat_id):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.finalize_admin_direct_purchase(config, bot_user, chat_id=chat_id)


def finalize_bot_purchase(config, bot_user, message, file_info, *, chat_id):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.finalize_bot_purchase(config, bot_user, message, file_info, chat_id=chat_id)


def finalize_bot_renewal(config, bot_user, message, file_info, *, chat_id):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.finalize_bot_renewal(config, bot_user, message, file_info, chat_id=chat_id)


def forward_receipt_to_admin(client, order, file_info, *, from_chat_id):
    _sync_order_finalizer_compat_deps()
    return _order_finalizers.forward_receipt_to_admin(client, order, file_info, from_chat_id=from_chat_id)


def handle_support_callback_update(config, callback_query, *, chat_id):
    return _handle_support_callback_update(
        config,
        callback_query,
        chat_id=chat_id,
        client_cls=BotClient,
        get_callback_id_func=get_callback_id,
        get_callback_data_func=get_callback_data,
        normalize_id_func=normalize_id,
        get_sender_object_func=get_sender_object,
        log_callback_func=log_callback,
        extract_callback_message_reference_func=extract_callback_message_reference,
        empty_inline_keyboard_func=empty_inline_keyboard,
        delivery_error_cls=BotDeliveryError,
    )


def handle_customer_analytics_callback_update(config, callback_query, *, chat_id):
    return _handle_customer_analytics_callback_update(
        config,
        callback_query,
        chat_id=chat_id,
        client_cls=BotClient,
        get_callback_id_func=get_callback_id,
        get_callback_data_func=get_callback_data,
    )


def handle_callback_update(config, callback_query, *, chat_id):
    return _handle_admin_order_callback_update(
        config,
        callback_query,
        chat_id=chat_id,
        client_cls=BotClient,
        get_callback_id_func=get_callback_id,
        get_callback_data_func=get_callback_data,
        normalize_id_func=normalize_id,
        get_sender_object_func=get_sender_object,
        parse_callback_data_func=parse_callback_data,
        log_callback_func=log_callback,
        format_order_message_func=format_order_message,
        order_admin_keyboard_func=order_admin_keyboard,
        sync_admin_order_messages_func=sync_admin_order_messages,
        send_final_order_status_func=send_final_order_status,
    )


def handle_message_update(config, message, *, chat_id, admin_user_id=""):
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True, "ignored": True}
    admin_user_id = str(admin_user_id or config.admin_user_id)

    pending = (
        BotPendingAction.objects.select_related(
            "order",
            "order__plan",
            "order__store",
            "support_conversation",
            "support_conversation__store",
            "support_conversation__customer",
        )
        .filter(
            bot_config=config,
            admin_user_id=admin_user_id,
            status=BotPendingAction.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )
    if not pending:
        return {"ok": True, "ignored": True}

    if pending.action == BotPendingAction.Action.SUPPORT_REPLY:
        return _handle_pending_support_reply(
            config,
            pending,
            message,
            text,
            chat_id=chat_id,
            admin_user_id=admin_user_id,
            client_cls=BotClient,
            get_message_id_func=get_message_id,
            log_event_func=log_event,
        )

    if pending.action != BotPendingAction.Action.REJECT_ORDER:
        return {"ok": True, "ignored": True}

    from .order_actions import reject_order

    log_callback(
        config,
        status=BotEventLog.Status.RECEIVED,
        order=pending.order,
        message=f"Reject reason received for order {pending.order.order_tracking_code}; reason={text!r}",
        raw_payload={"reason": text, "chat_id": chat_id},
    )
    result = reject_order(pending.order, reason=text, user=None, notify=True)
    pending.mark_completed()
    pending.order.refresh_from_db()
    updated_count = sync_admin_order_messages(
        pending.order,
        title="Order rejected" if result.success else "Order rejection failed",
        event_type=BotEventLog.EventType.ORDER_REJECTED if result.success else BotEventLog.EventType.ERROR,
        prefix_message=result.message,
        respect_notify=False,
        configs=[config],
    )
    log_callback(
        config,
        status=BotEventLog.Status.SUCCESS if result.success else BotEventLog.Status.FAILED,
        order=pending.order,
        message=f"reject_order finished for {pending.order.order_tracking_code}: success={result.success}; message={result.message}",
        raw_payload={
            "reason": text,
            "success": result.success,
            "message": result.message,
            "status": pending.order.status,
            "verification_status": pending.order.verification_status,
        },
    )
    if not updated_count:
        BotClient(config).send_message(
            f"{result.message}\n\n{format_order_message(pending.order, title='Order rejected')}",
            chat_id=chat_id,
        )
    return {"ok": True, "success": result.success, "message": result.message}


def build_sales_report(config):
    return _build_sales_report(config)


def send_due_sales_reports(*, force=False):
    return _send_due_sales_reports(force=force)
