import secrets
from html import escape

from django.core.cache import cache

from .constants import (
    CONFIG_COPY_CACHE_SECONDS,
    CONFIG_COPY_CALLBACK_PREFIX,
    CONFIG_COPY_HELP_TEXT,
    COPY_TEXT_MAX_LENGTH,
    TELEGRAM_MESSAGE_SAFE_LIMIT,
)
from .formatting import telegram_code
from .keyboards import build_copy_text_button, copy_text_is_supported, merge_inline_keyboards


CONFIG_LINK_KIND_META = {
    "sub": {
        "heading": "🔗 لینک اشتراک",
        "copy_label": "کپی لینک اشتراک 🔗",
    },
    "direct": {
        "heading": "⚡ لینک مستقیم",
        "copy_label": "کپی لینک مستقیم ⚡",
    },
}
CONFIG_COPY_EXPIRED_MESSAGE = "این لینک منقضی شده، دوباره از بخش سرویس‌های من دریافت کنید."


def normalize_config_link(value):
    return str(value or "").strip()


def config_link_sections(*, subscription_link="", direct_link=""):
    sections = []
    subscription_link = normalize_config_link(subscription_link)
    direct_link = normalize_config_link(direct_link)
    if subscription_link:
        sections.append(("sub", subscription_link))
    if direct_link:
        sections.append(("direct", direct_link))
    return sections


def config_copy_cache_key(config, token):
    config_id = getattr(config, "pk", None) or "global"
    return f"bot:copy-config:{config_id}:{token}"


def create_config_copy_token(config, kind, link):
    token = secrets.token_urlsafe(12).rstrip("=")
    cache.set(
        config_copy_cache_key(config, token),
        {"kind": kind, "link": normalize_config_link(link)},
        CONFIG_COPY_CACHE_SECONDS,
    )
    return token


def parse_config_copy_callback_data(data):
    data = str(data or "")
    if not data.startswith(CONFIG_COPY_CALLBACK_PREFIX):
        return "", ""
    kind, separator, token = data[len(CONFIG_COPY_CALLBACK_PREFIX) :].partition(":")
    if not separator or kind not in CONFIG_LINK_KIND_META or not token:
        return "", ""
    return kind, token


def cached_config_copy_link(config, kind, token):
    payload = cache.get(config_copy_cache_key(config, token))
    if not payload or payload.get("kind") != kind:
        return ""
    return normalize_config_link(payload.get("link"))


def build_config_link_copy_button(config, kind, link):
    link = normalize_config_link(link)
    if not link:
        return None
    meta = CONFIG_LINK_KIND_META[kind]
    label = meta["copy_label"]
    if 0 < len(link) <= COPY_TEXT_MAX_LENGTH and copy_text_is_supported(config):
        return build_copy_text_button(label, link, config=config)
    token = create_config_copy_token(config, kind, link)
    return build_copy_text_button(
        label,
        link,
        config=config,
        fallback_callback_data=f"{CONFIG_COPY_CALLBACK_PREFIX}{kind}:{token}",
        fallback_label=label,
    )


def config_copy_navigation_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "سرویس‌های من 🔐", "callback_data": "user:subs"},
                {"text": "آموزش اتصال ❓", "callback_data": "user:help"},
            ]
        ]
    }


def config_delivery_keyboard(config=None, config_link="", *, subscription_link="", direct_link="", extra_keyboard=None):
    if config_link and not subscription_link and not direct_link:
        direct_link = config_link
    rows = []
    for kind, link in config_link_sections(subscription_link=subscription_link, direct_link=direct_link):
        copy_button = build_config_link_copy_button(config, kind, link)
        if copy_button:
            rows.append([copy_button])
    rows.extend(config_copy_navigation_keyboard()["inline_keyboard"])
    return merge_inline_keyboards({"inline_keyboard": rows}, extra_keyboard)


def default_config_links_title(subscription_link="", direct_link=""):
    if subscription_link and direct_link:
        return "✅ سرویس شما آماده شد"
    if subscription_link:
        return "✅ لینک اشتراک شما آماده شد"
    return "✅ کانفیگ شما آماده شد"


def format_config_links_text(*, subscription_link="", direct_link="", title=None, detail_lines=None):
    sections = config_link_sections(subscription_link=subscription_link, direct_link=direct_link)
    title = title or default_config_links_title(subscription_link, direct_link)
    lines = [escape(str(title))]
    for line in detail_lines or []:
        if line is None:
            continue
        if line == "":
            lines.append("")
        else:
            lines.append(escape(str(line)))
    for kind, link in sections:
        lines.extend(["", f"{CONFIG_LINK_KIND_META[kind]['heading']}:", telegram_code(link, block=True)])
    lines.extend(["", CONFIG_COPY_HELP_TEXT])
    return "\n".join(lines)


def format_copyable_config_text(config_link, *, title=None):
    return format_config_links_text(direct_link=config_link, title=title)


def config_send_result_count(result):
    if not result:
        return 0
    if isinstance(result, list):
        return sum(config_send_result_count(item) for item in result)
    return 1


def send_config_links_message(
    client,
    chat_id,
    *,
    subscription_link="",
    direct_link="",
    title=None,
    detail_lines=None,
    keyboard=None,
):
    subscription_link = normalize_config_link(subscription_link)
    direct_link = normalize_config_link(direct_link)
    if not config_link_sections(subscription_link=subscription_link, direct_link=direct_link):
        return None

    text = format_config_links_text(
        subscription_link=subscription_link,
        direct_link=direct_link,
        title=title,
        detail_lines=detail_lines,
    )
    if len(text) > TELEGRAM_MESSAGE_SAFE_LIMIT and subscription_link and direct_link:
        return [
            send_config_links_message(
                client,
                chat_id,
                subscription_link=subscription_link,
                title=title or "✅ لینک اشتراک شما آماده شد",
                detail_lines=detail_lines,
                keyboard=keyboard,
            ),
            send_config_links_message(
                client,
                chat_id,
                direct_link=direct_link,
                title=title or "✅ لینک مستقیم شما آماده شد",
                detail_lines=detail_lines,
                keyboard=keyboard,
            ),
        ]

    return client.send_message(
        text,
        chat_id=chat_id,
        reply_markup=config_delivery_keyboard(
            client.config,
            subscription_link=subscription_link,
            direct_link=direct_link,
            extra_keyboard=keyboard,
        ),
        parse_mode="HTML",
        force_parse_mode=True,
    )


def send_copyable_config_message(client, chat_id, config_link, *, title=None, keyboard=None):
    config_link = str(config_link or "").strip()
    if not config_link:
        return None
    return send_config_links_message(client, chat_id, direct_link=config_link, title=title, keyboard=keyboard)


def handle_config_copy_callback(config, data, *, client, chat_id):
    kind, token = parse_config_copy_callback_data(data)
    link = cached_config_copy_link(config, kind, token) if token else ""
    if not link:
        client.send_message(
            CONFIG_COPY_EXPIRED_MESSAGE,
            chat_id=chat_id,
            reply_markup=config_copy_navigation_keyboard(),
        )
        return {"ok": True, "success": False, "expired": True}
    client.send_message(
        f"{CONFIG_LINK_KIND_META[kind]['heading']}:\n{telegram_code(link, block=True)}",
        chat_id=chat_id,
        reply_markup=config_copy_navigation_keyboard(),
        parse_mode="HTML",
        force_parse_mode=True,
    )
    return {"ok": True, "handled": True}
