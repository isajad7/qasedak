"""Batched reads per panel; no reconciliation or remote writes."""
from collections import Counter
from datetime import datetime, timezone as dt_timezone
import re
from django.utils.dateparse import parse_datetime
from store.models import Inbound, Panel
from store.panels.pasarguard.client import PasarGuardClient
from store.xui_api import (
    XUIService, first_xui_value, hash_xui_identifier, lookup_values,
    normalize_xui_usage_client, parse_xui_client_stats, parse_xui_json_object,
    related_client_stats, xui_int_or_none,
)
from django.utils import timezone


def _error_code(exc):
    code = str(getattr(exc, "error_code", "") or getattr(exc, "category", "") or type(exc).__name__)
    match = re.search(r"\b(?:HTTP\s*|status[=: ]+)([45]\d\d)\b", str(exc), re.I)
    return f"http_{match.group(1)}" if match else code[:80]


def read_panel(panel, *, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    if not panel.is_active or (panel.store_id and not panel.store.panel_usage_tracking_enabled):
        return [], "tracking_disabled"
    if panel.family == Panel.Family.PASARGUARD:
        return read_pasarguard(panel, diagnostics=diagnostics)
    if panel.family != Panel.Family.XUI:
        return [], "unsupported_panel"
    service = XUIService(panel)
    try:
        service.login()
    except Exception as exc:
        diagnostics["errors"] = {_error_code(exc): 1}
        return [], "panel_unreachable"
    rows, failed = [], False
    errors = Counter()
    checked = 0
    for inbound in Inbound.objects.filter(panel=panel, is_active=True).order_by("pk"):
        try:
            payload = service.get_inbound(inbound, use_cache=False)
            checked += 1
            clients = parse_xui_json_object(payload.get("settings")).get("clients") or []
            stats = parse_xui_client_stats(payload)
            if not isinstance(clients, list):
                raise ValueError("Invalid client list")
            captured = timezone.now()
            for client in clients:
                if not isinstance(client, dict):
                    continue
                traffic = related_client_stats(stats, client)
                row = normalize_xui_usage_client(inbound=inbound, inbound_data=payload, client=client, stats=traffic)
                if not row:
                    continue
                usage = traffic if row["source"] == "clientStats" else client
                used = xui_int_or_none(first_xui_value(usage.get("used"), usage.get("usedTraffic"), usage.get("used_traffic")))
                up = xui_int_or_none(first_xui_value(usage.get("up"), usage.get("upload")))
                down = xui_int_or_none(first_xui_value(usage.get("down"), usage.get("download")))
                # The legacy normalizer substitutes 0 for malformed/missing values.
                # Absence of telemetry must not become evidence of inactivity.
                row["stats_available"] = (used is not None and used >= 0) or (
                    up is not None and down is not None and up >= 0 and down >= 0
                )
                if traffic:
                    identifiers = {value.lower() for value in lookup_values(client)}
                    matches = [item for item in stats if isinstance(item, dict)
                               and identifiers.intersection(value.lower() for value in lookup_values(item))]
                    owners = [item for item in clients if isinstance(item, dict)
                              and set(lookup_values(traffic)).intersection(lookup_values(item))]
                    if len(matches) != 1 or len(owners) != 1:
                        row["stats_available"] = False
                # Inbound quota is NOT this customer's quota (normalizer's legacy fallback).
                row["total_bytes"] = xui_int_or_none(first_xui_value(
                    traffic.get("total"), traffic.get("totalGB"), client.get("totalGB"), client.get("total"),
                ))
                row["aliases"] = {hash_xui_identifier(value) for value in lookup_values(client)}
                row["aliases"].add(row["identifier_hash"])
                row["panel_id"] = panel.pk
                row["captured_at"] = captured
                rows.append(row)
        except Exception as exc:
            # Do not expose response bodies, panel URLs, credentials or tokens in logs.
            failed = True
            errors[_error_code(exc)] += 1
    diagnostics.update(checked_inbounds=checked, rows=len(rows), errors=dict(errors))
    return rows, "partial" if failed else "ok"


def _expiry(value):
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
        parsed = parse_datetime(str(value))
        return timezone.make_aware(parsed, dt_timezone.utc) if parsed and timezone.is_naive(parsed) else parsed
    except (ValueError, OverflowError, TypeError):
        return None


def read_pasarguard(panel, *, diagnostics):
    client = PasarGuardClient(panel)
    rows, seen = [], set()
    offset = 0
    # Bounded pagination, including ended users. No per-customer requests or subscription downloads.
    for page in range(50):
        try:
            payload = client.request("GET", f"/api/users?offset={offset}&limit=200&sort=username")
            users = payload.get("users")
            total_users = xui_int_or_none(payload.get("total"))
            if not isinstance(users, list) or total_users is None or total_users < 0:
                raise ValueError("Invalid user list")
            captured = timezone.now()
            for user in users:
                if not isinstance(user, dict) or not user.get("username"):
                    raise ValueError("Invalid user identity")
                username = str(user["username"])
                if username in seen:
                    raise ValueError("Repeated user in pagination")
                seen.add(username)
                aliases = {hash_xui_identifier(username)}
                proxies = user.get("proxy_settings") or user.get("proxies") or {}
                if isinstance(proxies, dict):
                    for proxy in proxies.values():
                        if isinstance(proxy, dict):
                            aliases.update(hash_xui_identifier(proxy[key]) for key in ("id", "uuid", "password") if proxy.get(key))
                used = xui_int_or_none(user.get("used_traffic"))
                total = xui_int_or_none(user.get("data_limit"))
                status = user.get("status")
                expiry = _expiry(user.get("expire"))
                rows.append({
                    "panel_id": panel.pk, "inbound_id": None, "remote_inbound_id": "user", "node_id": "pasarguard",
                    "identifier_hash": hash_xui_identifier(f'{user.get("id", "")}:{username}'), "aliases": aliases,
                    "used_bytes": used, "upload_bytes": 0, "download_bytes": used,
                    "total_bytes": total, "expiry_time": expiry, "remote_status": status,
                    "enabled": True if status in {"active", "on_hold"} else False if status in {"expired", "limited", "disabled"} else None,
                    "stats_available": used is not None and used >= 0,
                    "captured_at": captured,
                })
            diagnostics.update(pages=page + 1, rows=len(rows))
            if len(seen) >= total_users:
                return rows, "ok"
            if not users:
                raise ValueError("Incomplete user list")
            offset += len(users)
        except Exception as exc:
            diagnostics["errors"] = {_error_code(exc): 1}
            # An incomplete user list cannot establish credential uniqueness across accounts.
            return [], "partial" if rows else "panel_unreachable"
    diagnostics["errors"] = {"page_limit": 1}
    return [], "partial"
