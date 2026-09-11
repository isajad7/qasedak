from dataclasses import dataclass

from store.jalali import persian_digits

from .models import CupItem, Order, PlanDeliveryConfig, VPNClient
from .subscription_cups import (
    build_subscription_cup_base64_url,
    build_subscription_cup_dashboard_url,
    build_subscription_cup_url,
    get_subscription_cup_for_order,
    get_subscription_cup_for_vpn_client,
)


MODE_DIRECT_LINKS = PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS
MODE_SUBSCRIPTION = PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION
MODE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CustomerDelivery:
    delivery_mode: str
    customer_subscription_url: str = ""
    customer_direct_links: tuple[str, ...] = ()
    config_count: int = 0
    is_ready: bool = False
    diagnostic: str = ""
    subscription_cup_id: int | None = None
    legacy: bool = False

    @property
    def subscription_mode(self):
        return self.delivery_mode == MODE_SUBSCRIPTION

    @property
    def direct_links_mode(self):
        return self.delivery_mode == MODE_DIRECT_LINKS

    @property
    def has_subscription_url(self):
        return bool(self.customer_subscription_url)

    @property
    def has_direct_links(self):
        return bool(self.customer_direct_links)

    @property
    def primary_direct_link(self):
        return self.customer_direct_links[0] if self.customer_direct_links else ""

    def to_safe_dict(self):
        return {
            "delivery_mode": self.delivery_mode,
            "customer_subscription_url": self.customer_subscription_url,
            "customer_direct_links": list(self.customer_direct_links),
            "config_count": self.config_count,
            "is_ready": self.is_ready,
            "diagnostic": self.diagnostic,
            "subscription_cup_id": self.subscription_cup_id,
            "legacy": self.legacy,
        }


def _clean_links(values):
    cleaned = []
    seen = set()
    for value in values or []:
        link = str(value or "").strip()
        if not link or link in seen:
            continue
        cleaned.append(link)
        seen.add(link)
    return tuple(cleaned)


def plan_delivery_metadata(order):
    metadata = getattr(order, "metadata", None) or {}
    plan_delivery = metadata.get("plan_delivery_v2")
    return plan_delivery if isinstance(plan_delivery, dict) else {}


def plan_delivery_mode(order):
    metadata = plan_delivery_metadata(order)
    return str(metadata.get("delivery_mode") or "").strip().upper()


def order_uses_plan_delivery_v2(order):
    return bool(plan_delivery_metadata(order))


def metadata_direct_delivery_links(order):
    metadata = getattr(order, "metadata", None) or {}
    return _clean_links(metadata.get("direct_delivery_links") or [])


def _cup_active_item_count(cup):
    if not cup:
        return 0
    return CupItem.objects.filter(cup=cup, is_active=True, config_link__is_active=True).count()


def _project_subscription_urls(cup, store):
    if not cup:
        return "", ""
    return (
        build_subscription_cup_dashboard_url(cup, store=store),
        build_subscription_cup_base64_url(cup, store=store),
    )


def _link_group(*, label="", subscription_link="", direct_link="", project_subscription_link="", project_client_link=""):
    return {
        "label": label,
        "subscription_link": subscription_link,
        "direct_link": direct_link,
        "project_subscription_link": project_subscription_link,
        "project_client_link": project_client_link,
    }


def _direct_link_groups(links):
    total = len(links)
    return [
        _link_group(
            label=f"کانفیگ {persian_digits(index)}" if total > 1 else "",
            direct_link=link,
        )
        for index, link in enumerate(links, start=1)
    ]


class CustomerDeliveryResolver:
    def __init__(self, *, store=None):
        self.store = store

    def _store_for(self, order=None, vpn_client=None):
        return (
            self.store
            or getattr(order, "store", None)
            or getattr(vpn_client, "store", None)
            or getattr(getattr(vpn_client, "order", None), "store", None)
        )

    def _cup_url(self, cup, *, order=None, vpn_client=None):
        if not cup:
            return ""
        return build_subscription_cup_url(cup, store=self._store_for(order=order, vpn_client=vpn_client))

    def resolve_order(self, order):
        mode = plan_delivery_mode(order)
        if mode == MODE_SUBSCRIPTION:
            cup = get_subscription_cup_for_order(order)
            if not cup:
                return CustomerDelivery(
                    delivery_mode=MODE_SUBSCRIPTION,
                    diagnostic="v2_subscription_cup_missing",
                )
            config_count = _cup_active_item_count(cup)
            subscription_url = self._cup_url(cup, order=order)
            is_ready = bool(subscription_url and config_count and cup.is_accessible)
            diagnostic = ""
            if not cup.is_accessible:
                diagnostic = "subscription_cup_inaccessible"
            elif not config_count:
                diagnostic = "subscription_cup_empty"
            return CustomerDelivery(
                delivery_mode=MODE_SUBSCRIPTION,
                customer_subscription_url=subscription_url,
                customer_direct_links=(),
                config_count=config_count,
                is_ready=is_ready,
                diagnostic=diagnostic,
                subscription_cup_id=cup.pk,
            )
        if mode == MODE_DIRECT_LINKS:
            links = metadata_direct_delivery_links(order)
            if not links:
                links = _clean_links([getattr(order, "direct_link", "")])
            return CustomerDelivery(
                delivery_mode=MODE_DIRECT_LINKS,
                customer_direct_links=links,
                config_count=len(links),
                is_ready=bool(links),
                diagnostic="" if links else "v2_direct_links_missing",
            )
        if order_uses_plan_delivery_v2(order):
            return CustomerDelivery(
                delivery_mode=mode or MODE_UNKNOWN,
                diagnostic="unsupported_v2_delivery_mode",
            )
        return self._resolve_legacy_order(order)

    def resolve_result(self, delivery_result, *, order=None):
        if order is not None:
            return self.resolve_order(order)
        mode = str(getattr(delivery_result, "mode", "") or "").strip().upper()
        if mode == MODE_SUBSCRIPTION:
            cups = list(getattr(delivery_result, "subscription_cups", None) or [])
            cup = cups[0] if cups else None
            if not cup:
                return CustomerDelivery(delivery_mode=MODE_SUBSCRIPTION, diagnostic="v2_subscription_cup_missing")
            config_count = _cup_active_item_count(cup)
            subscription_url = self._cup_url(cup)
            return CustomerDelivery(
                delivery_mode=MODE_SUBSCRIPTION,
                customer_subscription_url=subscription_url,
                config_count=config_count,
                is_ready=bool(subscription_url and config_count and cup.is_accessible),
                diagnostic="" if config_count and cup.is_accessible else "subscription_cup_empty",
                subscription_cup_id=cup.pk,
            )
        if mode == MODE_DIRECT_LINKS:
            links = _clean_links(getattr(delivery_result, "direct_links", None) or [])
            return CustomerDelivery(
                delivery_mode=MODE_DIRECT_LINKS,
                customer_direct_links=links,
                config_count=len(links),
                is_ready=bool(links),
                diagnostic="" if links else "v2_direct_links_missing",
            )
        return CustomerDelivery(delivery_mode=mode or MODE_UNKNOWN, diagnostic="unsupported_delivery_result")

    def resolve_client(self, vpn_client, *, order=None):
        order = order or getattr(vpn_client, "order", None)
        if order and order_uses_plan_delivery_v2(order):
            return self.resolve_order(order)

        cup = get_subscription_cup_for_vpn_client(vpn_client)
        config_count = _cup_active_item_count(cup) if cup else 0
        subscription_url = str(getattr(vpn_client, "sub_link", "") or "").strip()
        if not subscription_url and cup:
            subscription_url = self._cup_url(cup, order=order, vpn_client=vpn_client)
        direct_links = _clean_links([getattr(vpn_client, "direct_link", "")])
        is_ready = bool(subscription_url or direct_links)
        return CustomerDelivery(
            delivery_mode=MODE_UNKNOWN,
            customer_subscription_url=subscription_url,
            customer_direct_links=direct_links,
            config_count=config_count or len(direct_links),
            is_ready=is_ready,
            diagnostic="" if is_ready else "legacy_client_links_missing",
            subscription_cup_id=getattr(cup, "pk", None),
            legacy=True,
        )

    def _resolve_legacy_order(self, order):
        cup = get_subscription_cup_for_order(order)
        config_count = _cup_active_item_count(cup) if cup else 0
        subscription_url = str(getattr(order, "sub_link", "") or "").strip()
        if not subscription_url and cup:
            subscription_url = self._cup_url(cup, order=order)
        direct_links = metadata_direct_delivery_links(order) or _clean_links([getattr(order, "direct_link", "")])
        fallback_count = len(direct_links) or int(getattr(order, "quantity", 1) or 1)
        is_ready = bool(subscription_url or direct_links)
        return CustomerDelivery(
            delivery_mode=MODE_UNKNOWN,
            customer_subscription_url=subscription_url,
            customer_direct_links=direct_links,
            config_count=config_count or fallback_count,
            is_ready=is_ready,
            diagnostic="" if is_ready else "legacy_order_links_missing",
            subscription_cup_id=getattr(cup, "pk", None),
            legacy=True,
        )

    def order_link_groups(self, order):
        delivery = self.resolve_order(order)
        if delivery.delivery_mode == MODE_SUBSCRIPTION:
            if delivery.is_ready and delivery.customer_subscription_url:
                return [_link_group(subscription_link=delivery.customer_subscription_url)]
            return []
        if delivery.delivery_mode == MODE_DIRECT_LINKS:
            return _direct_link_groups(delivery.customer_direct_links)
        if order_uses_plan_delivery_v2(order):
            return []
        return self._legacy_order_link_groups(order)

    def client_link_groups(self, vpn_client, *, order=None):
        delivery = self.resolve_client(vpn_client, order=order)
        if delivery.delivery_mode == MODE_SUBSCRIPTION:
            if delivery.is_ready and delivery.customer_subscription_url:
                return [_link_group(subscription_link=delivery.customer_subscription_url)]
            return []
        if delivery.delivery_mode == MODE_DIRECT_LINKS:
            return _direct_link_groups(delivery.customer_direct_links)
        if delivery.customer_subscription_url or delivery.primary_direct_link:
            return [
                _link_group(
                    subscription_link=delivery.customer_subscription_url,
                    direct_link=delivery.primary_direct_link,
                )
            ]
        return []

    def _legacy_order_link_groups(self, order):
        direct_delivery_links = metadata_direct_delivery_links(order)
        if direct_delivery_links:
            return _direct_link_groups(direct_delivery_links)

        clients = list(order.get_vpn_clients())
        if clients:
            expanded = []
            for vpn_client in clients:
                cup = get_subscription_cup_for_vpn_client(vpn_client)
                project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
                bundle_results = (vpn_client.xui_raw or {}).get("bundle_inbound_results") or []
                if bundle_results:
                    for index, result in enumerate(bundle_results, start=1):
                        expanded.append(
                            {
                                "subscription_link": result.get("sub_link") or vpn_client.sub_link,
                                "direct_link": result.get("direct_link") or "",
                                "project_subscription_link": project_subscription_link if index == 1 else "",
                                "project_client_link": project_client_link if index == 1 else "",
                            }
                        )
                else:
                    expanded.append(
                        {
                            "subscription_link": vpn_client.sub_link,
                            "direct_link": vpn_client.direct_link,
                            "project_subscription_link": project_subscription_link,
                            "project_client_link": project_client_link,
                        }
                    )
            return self._dedupe_legacy_groups(expanded)

        if getattr(order, "sub_link", "") or getattr(order, "direct_link", ""):
            cup = get_subscription_cup_for_order(order)
            project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
            return [
                _link_group(
                    subscription_link=order.sub_link,
                    direct_link=order.direct_link,
                    project_subscription_link=project_subscription_link,
                    project_client_link=project_client_link,
                )
            ]

        cup = get_subscription_cup_for_order(order)
        if cup:
            project_subscription_link, project_client_link = _project_subscription_urls(cup, order.store)
            return [
                _link_group(
                    project_subscription_link=project_subscription_link,
                    project_client_link=project_client_link,
                )
            ]
        return []

    def _dedupe_legacy_groups(self, expanded):
        total = len(expanded)
        groups = []
        seen_subscription_links = set()
        seen_project_subscription_links = set()
        seen_project_client_links = set()
        for index, item in enumerate(expanded, start=1):
            subscription_link = item["subscription_link"]
            if subscription_link and subscription_link in seen_subscription_links:
                subscription_link = ""
            elif subscription_link:
                seen_subscription_links.add(subscription_link)
            project_subscription_link = item.get("project_subscription_link") or ""
            if project_subscription_link and project_subscription_link in seen_project_subscription_links:
                project_subscription_link = ""
            elif project_subscription_link:
                seen_project_subscription_links.add(project_subscription_link)
            project_client_link = item.get("project_client_link") or ""
            if project_client_link and project_client_link in seen_project_client_links:
                project_client_link = ""
            elif project_client_link:
                seen_project_client_links.add(project_client_link)
            groups.append(
                _link_group(
                    label=f"کانفیگ {persian_digits(index)}" if total > 1 else "",
                    subscription_link=subscription_link,
                    direct_link=item["direct_link"],
                    project_subscription_link=project_subscription_link,
                    project_client_link=project_client_link,
                )
            )
        return groups

    def order_flat_links(self, order):
        groups = self.order_link_groups(order)
        links = []
        for index, group in enumerate(groups, start=1):
            prefix = f"کانفیگ {persian_digits(index)}" if len(groups) > 1 else "کانفیگ"
            for label, link in (
                ("لینک اشتراک", group.get("subscription_link")),
                ("لینک مستقیم", group.get("direct_link")),
                ("لینک مدیریت و ورود به برنامه", group.get("project_subscription_link")),
                ("لینک سازگار جایگزین", group.get("project_client_link")),
            ):
                if link:
                    links.append((f"{prefix} - {label}", link))
        return links


def resolve_customer_order_delivery(order):
    return CustomerDeliveryResolver().resolve_order(order)


def resolve_customer_delivery_result(delivery_result, *, order=None):
    return CustomerDeliveryResolver().resolve_result(delivery_result, order=order)


def resolve_customer_client_delivery(vpn_client, *, order=None):
    return CustomerDeliveryResolver().resolve_client(vpn_client, order=order)


def customer_delivery_link_groups(order):
    return CustomerDeliveryResolver().order_link_groups(order)


def customer_delivery_link_groups_for_client(vpn_client, *, order=None):
    return CustomerDeliveryResolver().client_link_groups(vpn_client, order=order)


def customer_delivery_flat_links(order):
    return CustomerDeliveryResolver().order_flat_links(order)


def customer_visible_config_count(order):
    delivery = resolve_customer_order_delivery(order)
    if order_uses_plan_delivery_v2(order):
        return delivery.config_count
    return int(getattr(order, "quantity", 1) or 1)
