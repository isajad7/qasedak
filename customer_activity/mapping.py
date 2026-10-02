"""Order → Cup/items → exact panel/inbound/client. Fail closed on shared identity."""
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256

from store.config_lookup import ConfigLookupError, extract_client_identifier_from_config
from store.models import Order, SubscriptionCup, VPNClient
from store.xui_api import hash_xui_identifier


FULFILLED = {Order.Status.CONFIRMED, Order.Status.COMPLETED}


@dataclass(frozen=True)
class Source:
    panel_id: int | None
    inbound_id: int | None
    node: str
    hashes: tuple


@dataclass
class PurchaseMap:
    order: object
    sources: set = field(default_factory=set)
    cups: list = field(default_factory=list)
    issues: set = field(default_factory=set)
    superseded: bool = False


def source_for_client(client):
    inbound = client.inbound
    hashes = tuple(sorted({hash_xui_identifier(value) for value in (
        client.uuid, client.xui_email, client.username, client.sub_id,
    ) if value}))
    return Source(inbound.panel_id if inbound else None, client.inbound_id,
                  str(client.xui_node_id or (inbound.xui_node_id if inbound else "") or ""), hashes)


def source_for_link(link):
    try:
        identifier = extract_client_identifier_from_config(link.raw_link)
    except (ConfigLookupError, ValueError, TypeError):
        identifier = ""
    if link.vpn_client_id:
        source = source_for_client(link.vpn_client)
        digest = hash_xui_identifier(identifier)
        if digest and digest in source.hashes:
            return source
        # A stale FK must not attribute the delivered link to an unrelated client.
        return Source(None, None, "", tuple(sorted(set(source.hashes) | ({digest} if digest else set()))))
    inbound = link.source_inbound
    panel_id = link.source_panel_id or (inbound.panel_id if inbound else None)
    if inbound and inbound.panel_id != panel_id:
        return Source(None, None, "", ())
    return Source(panel_id, link.source_inbound_id, str(inbound.xui_node_id or "") if inbound else "",
                  (hash_xui_identifier(identifier),) if identifier else ())


def load_purchase_maps():
    orders = list(Order.objects.select_related("customer", "plan", "store").order_by("created_at", "pk"))
    maps = {order.pk: PurchaseMap(order) for order in orders}
    clients = list(VPNClient.objects.select_related("inbound", "order"))
    client_by_id = {client.pk: client for client in clients}
    # A successful explicit renewal transfers this same physical service to a new purchase cycle.
    owner = {client.pk: client.order_id for client in clients}
    renewals = defaultdict(list)
    for order in sorted(orders, key=lambda item: (item.verified_at or item.created_at, item.pk)):
        try:
            target = client_by_id.get(int((order.metadata or {}).get("renewal_client_pk")))
        except (TypeError, ValueError):
            target = None
        if target and order.status in FULFILLED and order.verification_status == Order.VerificationStatus.VERIFIED:
            if (target.order_id and target.order.customer_id == order.customer_id
                    and target.store_id == order.store_id):
                owner[target.pk] = order.pk
                renewals[target.pk].append(order.pk)
            else:
                maps[order.pk].issues.add("ownership_conflict")

    transferred = {pk for client_id, purchase_ids in renewals.items() for pk in purchase_ids if pk != owner[client_id]}
    for client in clients:
        if owner.get(client.pk) in maps:
            maps[owner[client.pk]].sources.add(source_for_client(client))
        if client.order_id in maps and owner.get(client.pk) != client.order_id:
            transferred.add(client.order_id)

    orphan_sources = [source_for_client(client) for client in clients if owner.get(client.pk) not in maps]
    cups = SubscriptionCup.objects.select_related("vpn_client__inbound").prefetch_related(
        "items__config_link__vpn_client__inbound", "items__config_link__source_inbound",
    )
    for cup in cups:
        mapping = maps.get(cup.order_id)
        sources = set()
        if mapping:
            mapping.cups.append(cup)
            if cup.customer_id and cup.customer_id != mapping.order.customer_id:
                mapping.issues.add("ownership_conflict")

        def add(source, client_id=None):
            original = client_by_id.get(client_id)
            if (mapping and original and owner.get(client_id) != cup.order_id
                    and owner.get(client_id) != original.order_id
                    and (original.order_id == cup.order_id or cup.order_id in renewals[client_id])):
                transferred.add(cup.order_id)
                return
            sources.add(source)

        if cup.vpn_client_id:
            add(source_for_client(cup.vpn_client), cup.vpn_client_id)
        for item in cup.items.all():
            # Disabled Cups retain credential claims: disabling a URL does not revoke imported configs.
            if item.is_active and item.config_link.is_active:
                add(source_for_link(item.config_link), item.config_link.vpn_client_id)
        if mapping:
            mapping.sources.update(sources)
        else:
            orphan_sources.extend(sources)

    for pk in transferred:
        maps[pk].superseded = not maps[pk].sources
    return [item for item in maps.values() if item.sources or item.cups or item.order.status in FULFILLED], orphan_sources


def row_key(row):
    scoped = (row["panel_id"], row.get("node_id") or "", row.get("remote_inbound_id"), row["identifier_hash"])
    return sha256(repr(scoped).encode()).hexdigest()


def resolve_maps(maps, orphan_sources, rows):
    def resolve(source):
        if not source.panel_id or not source.inbound_id or not source.hashes:
            return None
        matches = [row for row in rows if row["panel_id"] == source.panel_id
                   and row["inbound_id"] == source.inbound_id
                   and str(row.get("node_id") or "") == source.node
                   and set(source.hashes).intersection(row["aliases"])]
        return matches[0] if len(matches) == 1 else None

    claims, unresolved = defaultdict(set), []
    resolved = {}
    for mapping in maps:
        results = [resolve(source) for source in mapping.sources]
        resolved[mapping.order.pk] = results
        for source, row in zip(mapping.sources, results):
            if row:
                claims[row_key(row)].add(mapping.order.pk)
            else:
                unresolved.append((mapping.order.pk, source))
    unresolved.extend((None, source) for source in orphan_sources)
    for claimant, source in unresolved:
        for row in rows:
            if (not source.panel_id or source.panel_id == row["panel_id"]) and set(source.hashes).intersection(row["aliases"]):
                claims[row_key(row)].add(claimant)
    result = {}
    for mapping in maps:
        matched = resolved[mapping.order.pk]
        known = {row_key(row): row for row in matched if row}
        reason = ""
        if mapping.issues:
            reason = sorted(mapping.issues)[0]
        elif any(len(claims[key]) > 1 for key in known):
            reason = "shared_identity"
        elif not matched or any(row is None for row in matched):
            reason = "unmapped_source"
        elif any(not row.get("stats_available") for row in known.values()):
            reason = "missing_counters"
        result[mapping.order.pk] = (known, reason)
    return result
