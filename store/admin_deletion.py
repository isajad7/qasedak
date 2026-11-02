"""Bounded panel deletion previews and explicit removal of panel-owned data."""

from collections import Counter, defaultdict

from django.db import router, transaction
from django.db.models import Q
from django.db.models.deletion import CASCADE
from django.utils.html import format_html
from django.utils.text import capfirst


def panel_deletion_scopes(panel_ids, using):
    """Keep shared customers, stores, plans and inventory; remove panel services."""
    from . import models as m

    def rows(model, condition):
        return model._base_manager.using(using).filter(condition).order_by()

    panels = rows(m.Panel, Q(pk__in=panel_ids))
    inbounds = rows(m.Inbound, Q(panel__in=panels))
    direct_clients = rows(m.VPNClient, Q(inbound__in=inbounds))
    orders = rows(m.Order, Q(inbound__in=inbounds) | Q(pk__in=direct_clients.values("order_id")))
    clients = rows(m.VPNClient, Q(inbound__in=inbounds) | Q(order__in=orders))
    cups = rows(m.SubscriptionCup, Q(order__in=orders) | Q(vpn_client__in=clients))
    sources = rows(m.PlanDeliverySource, Q(panel__in=panels) | Q(inbound__in=inbounds))
    feeds = rows(m.ExternalSubscriptionFeed, Q(panel__in=panels) | Q(vpn_client__in=clients)
                | Q(cup__in=cups) | Q(delivery_source__in=sources))
    links = rows(m.ConfigLink, Q(source_panel__in=panels) | Q(source_inbound__in=inbounds)
                | Q(vpn_client__in=clients) | Q(external_feed__in=feeds))
    # Order matters: remove explicit owners before SET_NULL would erase their scope.
    return [
        rows(m.ConfigAllocation, Q(order__in=orders) | Q(cup__in=cups)),
        rows(m.FreeTrialRequest, Q(panel__in=panels) | Q(inbound__in=inbounds) | Q(vpn_client__in=clients)),
        rows(m.VPNClientActionLog, Q(panel__in=panels) | Q(inbound__in=inbounds) | Q(vpn_client__in=clients)),
        rows(m.BotEventLog, Q(order__in=orders)),
        rows(m.RevenueOfferLog, Q(vpn_client__in=clients)),
        links, feeds, cups,
        clients, orders,
        sources,
        rows(m.PlanInboundRoute, Q(inbound__in=inbounds)),
        rows(m.CupFillerRule, Q(panel__in=panels)),
        rows(m.Expense, Q(panel__in=panels)),
        inbounds, panels,
    ]


def counted_deleted_objects(objects, request, admin_site):
    objects = list(objects)
    if not objects:
        return [], {}, set(), []
    using = router.db_for_write(objects[0]._meta.model)
    scopes = defaultdict(list)

    def visit(queryset, ancestors=frozenset()):
        model = queryset.model
        scopes[model].append(queryset)
        for relation in model._meta.related_objects:
            if relation.on_delete is CASCADE and relation.related_model not in ancestors:
                child = relation.related_model._base_manager.using(using).filter(
                    **{f"{relation.field.name}__in": queryset}
                ).order_by()
                visit(child, ancestors | {model})

    for queryset in panel_deletion_scopes([obj.pk for obj in objects], using):
        visit(queryset)
    counts, permissions, deleted = {}, set(), []
    for model, querysets in scopes.items():
        condition = Q(pk__in=querysets[0].values("pk"))
        for queryset in querysets[1:]:
            condition |= Q(pk__in=queryset.values("pk"))
        count = model._base_manager.using(using).filter(condition).count()
        if not count:
            continue
        if admin_site.is_registered(model):
            if not admin_site.get_model_admin(model).has_delete_permission(request):
                permissions.add(model._meta.verbose_name)
        name = model._meta.verbose_name_plural
        counts[name] = counts.get(name, 0) + count
        deleted.append(format_html("{}: {}", capfirst(name), count))
    return deleted, counts, permissions, []


def delete_panels_with_dependencies(objects):
    """Delete database dependencies atomically, without contacting retired panels."""
    from .models import Inbound, Panel

    objects = list(objects)
    if not objects:
        return 0, {}
    using = router.db_for_write(Panel)
    panel_ids = [obj.pk for obj in objects]
    totals = Counter()
    with transaction.atomic(using=using):
        # Lock the panel/inbounds so newly created children cannot race this removal.
        panels = Panel.objects.using(using).select_for_update().filter(pk__in=panel_ids)
        list(panels.values_list("pk", flat=True))
        list(Inbound.objects.using(using)
             .select_for_update().filter(panel_id__in=panel_ids).values_list("pk", flat=True))
        scopes = panel_deletion_scopes(panel_ids, using)
        # Freeze only business-row IDs; monitoring histories remain SQL cascades.
        frozen = [(queryset.model, list(queryset.values_list("pk", flat=True))) for queryset in scopes]
        for model, ids in frozen:
            if ids:
                _count, deleted = model._base_manager.using(using).filter(pk__in=ids).delete()
                totals.update(deleted)
    return sum(totals.values()), dict(totals)
