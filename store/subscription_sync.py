"""Connect each Cup to its own PasarGuard upstream without changing customer URLs."""
import base64
import time
from collections import Counter
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .external_subscription_sources import refresh_external_subscription_feed, resolved_filter_policy_for_source
from .models import ConfigLink, CupItem, ExternalSubscriptionFeed, Panel, PlanDeliverySource, SubscriptionCup, VPNClient

READ_REFRESH_INTERVAL_SECONDS = 60
READ_REFRESH_BUDGET_SECONDS = 8


def _owned_client(cup, client, panel):
    if not client or not client.inbound_id or client.inbound.panel_id != panel.pk:
        return False
    if panel.store_id and panel.store_id != client.store_id:
        return False
    order = cup.order or client.order
    if not order or order.status not in {"completed", "confirmed"} or order.verification_status != "verified":
        return False
    if order.store_id != client.store_id or (cup.customer_id and cup.customer_id != order.customer_id):
        return False
    if cup.order_id and client.order_id != cup.order_id:
        if (not client.order or client.order.customer_id != order.customer_id
                or str((order.metadata or {}).get("renewal_client_pk", "")) != str(client.pk)):
            return False
    return client.status != "deleted"


def _upstream_url(cup, client):
    url = str(client.sub_link or "").strip()
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path.rstrip("/").endswith(f"/sub/{cup.token}"):
            return ""
    except ValueError:
        return ""
    return url


@transaction.atomic
def ensure_pasarguard_feeds_for_cup(cup):
    """Adopt only unshared panel-generated links with a provable owner. No HTTP."""
    from .db_locking import select_for_update_self
    cup = select_for_update_self(SubscriptionCup.objects.select_related("order", "vpn_client__inbound__panel", "vpn_client__order")).get(pk=cup.pk)
    report = {"created": 0, "adopted_items": 0, "skipped": {}}
    if not cup.is_accessible:
        return report
    items = list(cup.items.filter(is_active=True, config_link__is_active=True, config_link__external_feed__isnull=True,
        config_link__source_type__in=("panel_generated", "external_subscription"))
        .select_related("config_link__source_panel", "config_link__source_inbound__panel",
                        "config_link__vpn_client__inbound__panel", "config_link__vpn_client__order"))
    if not items:
        return report
    clients = list(VPNClient.objects.filter(order_id=cup.order_id).select_related("inbound__panel", "order")) if cup.order_id else []
    shared = set(CupItem.objects.filter(config_link_id__in=[item.config_link_id for item in items]).exclude(cup=cup).values_list("config_link_id", flat=True))
    skipped, groups = Counter(), {}
    for item in items:
        link = item.config_link
        client = link.vpn_client or cup.vpn_client
        panel = link.source_panel or (link.source_inbound.panel if link.source_inbound_id else None)
        if not panel and client and client.inbound_id:
            panel = client.inbound.panel
        if not panel or panel.family != Panel.Family.PASARGUARD:
            continue
        if not client:
            possible = [value for value in clients if value.inbound_id and value.inbound.panel_id == panel.pk]
            client = possible[0] if len(possible) == 1 else None
        if link.pk in shared or not _owned_client(cup, client, panel):
            skipped["ambiguous_ownership"] += 1
            continue
        url = _upstream_url(cup, client)
        if not url:
            skipped["missing_upstream_url"] += 1
            continue
        groups.setdefault((panel.pk, client.pk), {"panel": panel, "client": client, "url": url, "items": []})["items"].append(item)
    for group in groups.values():
        panel, client = group["panel"], group["client"]
        feed = ExternalSubscriptionFeed.objects.filter(cup=cup, panel=panel, vpn_client=client, provider="pasarguard").order_by("pk").first()
        if feed and (not feed.active or feed.status == "disabled"):
            skipped["feed_disabled"] += len(group["items"])
            continue
        if not feed:
            source_ids = set()
            for item in group["items"]:
                metadata = item.config_link.metadata or {}
                source_ids.update(str(value) for value in metadata.get("plan_delivery_source_ids", []) if str(value).isdigit())
                if str(metadata.get("plan_delivery_source_id", "")).isdigit():
                    source_ids.add(str(metadata["plan_delivery_source_id"]))
            source = PlanDeliverySource.objects.filter(pk__in=source_ids, panel=panel, delivery_config__plan_id=cup.plan_id).order_by("pk").first()
            feed = ExternalSubscriptionFeed.objects.create(cup=cup, panel=panel, vpn_client=client, delivery_source=source,
                provider="pasarguard", protected_subscription_url=group["url"], remote_identity_ref=client.xui_email or client.username or "",
                resolved_filter_policy=resolved_filter_policy_for_source(source), next_refresh_at=timezone.now(),
                last_good_config_count=len(group["items"]), metadata={"source": "legacy_cup_sync", "awaiting_first_refresh": True})
            report["created"] += 1
        link_ids = [item.config_link_id for item in group["items"]]
        ConfigLink.objects.filter(pk__in=link_ids, external_feed__isnull=True).update(external_feed=feed, vpn_client=client)
        report["adopted_items"] += len(link_ids)
    report["skipped"] = dict(skipped)
    return report


def _read_adapter(panel, remaining):
    from .panels import get_safe_panel_adapter
    adapter = get_safe_panel_adapter(panel)
    adapter.client.timeout = (min(1, remaining / 4), min(2, remaining / 4))
    return adapter


def refresh_subscription_cup_on_read(cup):
    try:
        ensure_pasarguard_feeds_for_cup(cup)
    except Exception:
        return []  # A binding failure must not take an existing customer subscription offline.
    deadline = time.monotonic() + READ_REFRESH_BUDGET_SECONDS
    feeds = cup.external_feeds.filter(active=True, provider="pasarguard").exclude(status="disabled").order_by("last_attempt_at", "pk")
    results = []
    for feed in feeds:
        remaining = deadline - time.monotonic()
        if remaining <= .2:
            break
        try:
            results.append(refresh_external_subscription_feed(feed.pk, min_interval_seconds=READ_REFRESH_INTERVAL_SECONDS,
                adapter_factory=lambda panel, remaining=remaining: _read_adapter(panel, remaining)))
        except Exception:
            results.append(None)  # Preserve the last-good output on provider/refresh failure.
    return results


def repair_and_refresh_pasarguard_subscriptions(*, refresh=False, limit=None, public_base_url=None):
    now = timezone.now()
    cups = SubscriptionCup.objects.filter(status="active").filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).order_by("pk")
    if limit:
        cups = cups[:limit]
    skipped, created, adopted, checked = Counter(), 0, 0, 0
    for cup in cups:
        report = ensure_pasarguard_feeds_for_cup(cup)
        checked += 1
        created += report["created"]
        adopted += report["adopted_items"]
        skipped.update(report["skipped"])
    result = {"cups_checked": checked, "feeds_created": created, "items_adopted": adopted, "unresolved": dict(skipped),
              "refreshed": 0, "failed": 0, "skipped_refresh": 0, "error_codes": {}, "verified_cups": 0}
    if not refresh:
        return result
    errors, successful_cups = Counter(), set()
    feeds = ExternalSubscriptionFeed.objects.filter(provider="pasarguard", active=True, cup_id__in=cups.values("pk")).exclude(status="disabled")
    for feed in feeds.order_by("pk"):
        summary = refresh_external_subscription_feed(feed.pk)
        if summary.ok:
            result["refreshed"] += 1
            successful_cups.add(feed.cup_id)
        elif summary.skipped:
            result["skipped_refresh"] += 1
        else:
            result["failed"] += 1
        if summary.error_code:
            errors[summary.error_code] += 1
    from .subscription_cups import active_cup_links, render_subscription_cup_base64
    for cup in SubscriptionCup.objects.filter(pk__in=successful_cups):
        decoded = base64.b64decode(render_subscription_cup_base64(cup)).decode("utf-8").splitlines()
        if decoded != active_cup_links(cup):
            raise RuntimeError("Cup client serialization differs from current active items.")
        result["verified_cups"] += 1
    result["error_codes"] = dict(errors)
    if public_base_url:
        import requests
        from .subscription_cups import build_subscription_cup_client_path
        result["public_verified"] = 0
        public_errors = Counter()
        for cup in SubscriptionCup.objects.filter(pk__in=successful_cups).order_by("pk")[:5]:
            try:
                response = requests.get(public_base_url.rstrip("/") + build_subscription_cup_client_path(cup),
                    headers={"User-Agent": "v2rayNG/1.10.0", "Cache-Control": "no-cache"}, timeout=10)
                if response.status_code != 200:
                    public_errors[f"http_{response.status_code}"] += 1
                elif base64.b64decode(response.content, validate=True).decode("utf-8").splitlines() != active_cup_links(cup):
                    public_errors["list_mismatch"] += 1
                else:
                    result["public_verified"] += 1
            except Exception:
                public_errors["unreachable_or_invalid_response"] += 1
        result["public_errors"] = dict(public_errors)
    return result
