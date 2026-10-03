"""Order → Cup/items → exact panel/inbound/client. Fail closed on shared identity."""
from collections import Counter, defaultdict
import base64
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import unquote, urlsplit

from store.config_lookup import ConfigLookupError, extract_client_identifier_from_config
from store.models import Order, Panel, SubscriptionCup, VPNClient
from store.xui_api import hash_xui_identifier


FULFILLED = {Order.Status.CONFIRMED, Order.Status.COMPLETED}


@dataclass(frozen=True)
class Source:
    panel_id: int | None
    inbound_id: int | None
    node: str
    hashes: tuple
    required_hash: str = ""


@dataclass
class PurchaseMap:
    order: object
    sources: set = field(default_factory=set)
    cups: list = field(default_factory=list)
    issues: set = field(default_factory=set)
    superseded: bool = False


def source_for_client(client):
    inbound = client.inbound
    if inbound and inbound.panel.family == Panel.Family.PASARGUARD:
        # PasarGuard's local UUID is synthetic. Its username is the remote account identity.
        raw = client.xui_raw if isinstance(client.xui_raw, dict) else {}
        username = str(raw.get("username") or client.xui_email or client.username or "").strip()
        return Source(inbound.panel_id, client.inbound_id, "pasarguard",
                      (hash_xui_identifier(username),) if username else ())
    hashes = tuple(sorted({hash_xui_identifier(value) for value in (
        client.uuid, client.xui_email, client.username, client.sub_id,
    ) if value}))
    return Source(inbound.panel_id if inbound else None, client.inbound_id,
                  str(client.xui_node_id or (inbound.xui_node_id if inbound else "") or ""), hashes)


def credential_from_link(raw_link):
    """Extract only the credential; never use a server address as an identity.

    PasarGuard native subscriptions also contain Shadowsocks links, which the
    existing customer lookup parser does not support. SIP002 permits Base64URL
    or percent-encoded method:password userinfo; legacy links encode the URI body.
    """
    raw = str(raw_link or "").strip()
    if not raw.lower().startswith("ss://"):
        return extract_client_identifier_from_config(raw)

    def decode(value):
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")

    body = raw[5:].split("#", 1)[0].split("?", 1)[0]
    if "@" in body:
        userinfo, server = body.rsplit("@", 1)
        userinfo = unquote(userinfo)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        else:
            method, password = decode(userinfo).split(":", 1)
    else:
        userinfo, server = decode(body).rsplit("@", 1)
        method, password = userinfo.split(":", 1)
    address = urlsplit("ss://" + server)
    if not method or not password or not address.hostname or not address.port:
        raise ConfigLookupError("Invalid Shadowsocks credential")
    return password


def source_for_link(link):
    try:
        identifier = credential_from_link(link.raw_link)
    except (ConfigLookupError, ValueError, TypeError):
        identifier = ""
    if link.vpn_client_id:
        source = source_for_client(link.vpn_client)
        digest = hash_xui_identifier(identifier)
        if source.node == "pasarguard":
            # Both the delivered credential and its declared owner must match the remote account.
            return Source(source.panel_id, source.inbound_id, source.node,
                          (digest,) if digest else (), source.hashes[0] if source.hashes else "")
        if digest and digest in source.hashes:
            return source
        # A stale FK must not attribute the delivered link to an unrelated client.
        return Source(None, None, "", tuple(sorted(set(source.hashes) | ({digest} if digest else set()))))
    inbound = link.source_inbound
    panel_id = link.source_panel_id or (inbound.panel_id if inbound else None)
    if inbound and inbound.panel_id != panel_id:
        return Source(None, None, "", ())
    panel = link.source_panel or (inbound.panel if inbound else None)
    if panel and panel.family == Panel.Family.PASARGUARD:
        return Source(panel_id, link.source_inbound_id, "pasarguard", (hash_xui_identifier(identifier),) if identifier else ())
    return Source(panel_id, link.source_inbound_id, str(inbound.xui_node_id or "") if inbound else "",
                  (hash_xui_identifier(identifier),) if identifier else ())


def load_purchase_maps():
    orders = list(Order.objects.select_related("customer", "plan", "store").order_by("created_at", "pk"))
    maps = {order.pk: PurchaseMap(order) for order in orders}
    clients = list(VPNClient.objects.select_related("inbound__panel", "order"))
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
    cups = SubscriptionCup.objects.select_related("vpn_client__inbound__panel").prefetch_related(
        "items__config_link__vpn_client__inbound__panel", "items__config_link__source_inbound__panel", "items__config_link__source_panel",
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


def resolve_maps(maps, orphan_sources, rows, *, diagnostics=None):
    outcomes = Counter()

    def resolve(source):
        if not source.panel_id or (not source.inbound_id and source.node != "pasarguard") or not source.hashes:
            outcomes["no_panel" if not source.panel_id else "no_identity" if not source.hashes else "no_inbound"] += 1
            return None
        candidates = [row for row in rows if row["panel_id"] == source.panel_id
                      and (not source.required_hash or source.required_hash in row["aliases"])
                      and set(source.hashes).intersection(row["aliases"])]
        matches = [row for row in candidates
                   if (source.node == "pasarguard" or row["inbound_id"] == source.inbound_id)
                   and str(row.get("node_id") or "") == source.node]
        outcomes["matched" if len(matches) == 1 else "ambiguous_remote" if matches else "scope_mismatch" if candidates else "identity_not_found"] += 1
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
        if reason and not mapping.issues:
            # Incomplete coverage cannot prove inactivity. A dedicated, measurable
            # subset can still prove positive traffic without attributing shared bytes.
            exclusive = {key: row for key, row in known.items()
                         if len(claims[key]) == 1 and row.get("stats_available")}
            if exclusive:
                known, reason = exclusive, "partial_coverage"
        result[mapping.order.pk] = (known, reason)
    if diagnostics is not None:
        diagnostics.update(source_outcomes=dict(outcomes),
                           purchases_with_matches=sum(bool(value[0]) for value in result.values()),
                           partial_purchases=sum(value[1] == "partial_coverage" for value in result.values()))
    return result
