from contextlib import contextmanager
import threading
from urllib.parse import quote, urlsplit, urlunsplit

from django.conf import settings

from .models import BotConfiguration

_webhook_response_state = threading.local()


class TelegramWebhookResponseState:
    def __init__(self):
        self.payload = None


def telegram_proxy_url():
    legacy_url = (getattr(settings, "TELEGRAM_PROXY_URL", "") or "").strip()
    if legacy_url:
        return legacy_url

    host = (getattr(settings, "TELEGRAM_PROXY_HOST", "") or "").strip()
    port = str(getattr(settings, "TELEGRAM_PROXY_PORT", "") or "").strip()
    if not host or not port:
        return ""

    protocol = (getattr(settings, "TELEGRAM_PROXY_PROTOCOL", "") or "http").strip() or "http"
    username = (getattr(settings, "TELEGRAM_PROXY_USERNAME", "") or "").strip()
    password = (getattr(settings, "TELEGRAM_PROXY_PASSWORD", "") or "").strip()
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"
    return f"{protocol}://{auth}{host}:{port}"


def sanitized_telegram_proxy_url(proxy_url=None):
    proxy_url = proxy_url if proxy_url is not None else telegram_proxy_url()
    if not proxy_url:
        return ""

    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return proxy_url

    auth = ""
    if parsed.username:
        auth = f"{parsed.username}:****@"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{auth}{parsed.hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def bot_request_proxies(provider):
    if provider != BotConfiguration.Provider.TELEGRAM:
        return None

    proxy_url = telegram_proxy_url()
    if not proxy_url:
        return None

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def bot_request_kwargs(provider):
    proxies = bot_request_proxies(provider)
    return {"proxies": proxies} if proxies else {}


@contextmanager
def telegram_webhook_response_context(provider):
    previous = getattr(_webhook_response_state, "state", None)
    enabled = (
        provider == BotConfiguration.Provider.TELEGRAM
        and getattr(settings, "TELEGRAM_WEBHOOK_RESPONSE_ENABLED", False)
    )
    state = TelegramWebhookResponseState() if enabled else None
    _webhook_response_state.state = state or previous
    try:
        yield state
    finally:
        _webhook_response_state.state = previous


def capture_telegram_webhook_response(provider, method, payload):
    if provider != BotConfiguration.Provider.TELEGRAM:
        return False
    if not getattr(settings, "TELEGRAM_WEBHOOK_RESPONSE_ENABLED", False):
        return False
    if method != "sendMessage":
        return False
    state = getattr(_webhook_response_state, "state", None)
    if state is None:
        return False
    if state.payload is not None:
        return False

    state.payload = {"method": method, **payload}
    return True
