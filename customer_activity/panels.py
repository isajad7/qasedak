"""One login and one read per inbound; no reconciliation or remote writes."""
from store.models import Inbound, Panel
from store.xui_api import (
    XUIService, first_xui_value, hash_xui_identifier, lookup_values,
    normalize_xui_usage_client, parse_xui_client_stats, parse_xui_json_object,
    related_client_stats, xui_int_or_none,
)
from django.utils import timezone


def read_panel(panel):
    if not panel.is_active or (panel.store_id and not panel.store.panel_usage_tracking_enabled):
        return [], "tracking_disabled"
    if panel.family != Panel.Family.XUI:
        return [], "unsupported_panel"
    service = XUIService(panel)
    try:
        service.login()
    except Exception:
        return [], "panel_unreachable"
    rows, failed = [], False
    for inbound in Inbound.objects.filter(panel=panel, is_active=True).order_by("pk"):
        try:
            payload = service.get_inbound(inbound, use_cache=False)
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
        except Exception:
            # Do not expose response bodies, panel URLs, credentials or tokens in logs.
            failed = True
    return rows, "partial" if failed else "ok"
