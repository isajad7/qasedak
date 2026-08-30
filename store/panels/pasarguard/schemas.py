from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone


PASARGUARD_NOTE_PREFIX = "qasedak:"
PASARGUARD_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
CONFIG_LINK_RE = re.compile(r"^(?:vless|vmess|trojan|ss|ssr|wireguard|hysteria2|hy2|tuic)://", re.IGNORECASE)


def normalize_pasarguard_username(value, *, context=""):
    base = str(value or "").strip().lower()
    base = re.sub(r"[^a-z0-9_]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base or len(base) < 3:
        base = "qasedak"
    if not base[0].isalpha():
        base = f"q_{base}"
    suffix = hashlib.sha256(str(context or base).encode("utf-8")).hexdigest()[:8]
    max_base = 32 - len(suffix) - 1
    username = f"{base[:max_base].rstrip('_')}_{suffix}"
    if len(username) < 3:
        username = f"qasedak_{suffix}"
    return username[:32]


def pasarguard_note_marker(context_key):
    context_hash = hashlib.sha256(str(context_key or "").encode("utf-8")).hexdigest()[:24]
    return f"{PASARGUARD_NOTE_PREFIX}{context_hash}"


def user_note_matches_context(user, marker):
    note = ""
    if isinstance(user, dict):
        note = str(user.get("note") or "")
    return bool(marker and marker in note)


def merge_qasedak_note(existing_note, marker):
    existing_note = str(existing_note or "").strip()
    if marker and marker in existing_note:
        return existing_note
    if existing_note:
        return f"{existing_note} {marker}".strip()
    return marker


def bytes_from_gb(value):
    try:
        number = Decimal(str(value or 0))
    except Exception:
        number = Decimal("0")
    if number <= 0:
        return 0
    return int(number * Decimal(1024**3))


def expires_at_from_days(days):
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return None
    return timezone.now() + timedelta(days=days)


def pasarguard_datetime(value):
    if not value:
        return 0
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def subscription_raw_url(subscription_url):
    value = str(subscription_url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if not path.endswith("/raw"):
        path = f"{path}/raw"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def subscription_format_url(subscription_url, format_name):
    value = str(subscription_url or "").strip()
    format_name = str(format_name or "").strip().strip("/")
    if not value or not format_name:
        return ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if not path.endswith(f"/{format_name}"):
        path = f"{path}/{format_name}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def raw_links_from_payload(payload):
    if isinstance(payload, dict):
        candidates = []
        body = payload.get("body")
        if isinstance(body, dict):
            candidates.append(body.get("links"))
        candidates.append(payload.get("links"))
        for links in candidates:
            if isinstance(links, list):
                return [str(link) for link in links if isinstance(link, str) and CONFIG_LINK_RE.match(link)]
    return []


def parse_raw_subscription(text):
    raw_links = []
    for line in str(text or "").splitlines():
        candidate = line.rstrip("\r")
        if candidate and CONFIG_LINK_RE.match(candidate):
            raw_links.append(candidate)
    return raw_links


def safe_pasarguard_group(group):
    group = dict(group or {})
    group_id = group.get("id")
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        group_id = None
    inbound_tags_known = "inbound_tags" in group
    inbound_tags = group.get("inbound_tags") if inbound_tags_known else []
    if not isinstance(inbound_tags, list):
        inbound_tags = []
    disabled_known = "is_disabled" in group
    is_disabled = bool(group.get("is_disabled")) if disabled_known else None
    return {
        "id": group_id,
        "name": str(group.get("name") or "").strip(),
        "inbound_tags": [str(tag) for tag in inbound_tags[:100]],
        "is_disabled": is_disabled,
        "disabled_known": disabled_known,
        "inbound_tags_known": inbound_tags_known,
        "source": str(group.get("_pasarguard_group_source") or "").strip(),
    }
