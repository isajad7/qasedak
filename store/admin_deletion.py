"""Summarize large cascades without materializing monitoring rows."""

from collections import defaultdict

from django.contrib.admin.utils import NestedObjects, quote
from django.db import router
from django.db.models import Q
from django.db.models.deletion import Collector
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.text import capfirst


class CountedDeleteCollector(NestedObjects):
    def can_fast_delete(self, *args, **kwargs):
        # NestedObjects normally disables this solely to render every row.
        # Keep Django's signal, cascade and protection checks for leaf tables.
        return Collector.can_fast_delete(self, *args, **kwargs)


def counted_deleted_objects(objects, request, admin_site):
    objects = list(objects)
    if not objects:
        return [], {}, set(), []
    using = router.db_for_write(objects[0]._meta.model)
    collector = CountedDeleteCollector(using=using, origin=objects)
    collector.collect(objects)
    permissions = set()

    def check_permission(model, obj=None):
        if admin_site.is_registered(model):
            if not admin_site.get_model_admin(model).has_delete_permission(request, obj):
                permissions.add(model._meta.verbose_name)

    def label(obj):
        model = type(obj)
        opts = model._meta
        check_permission(model, obj)
        if admin_site.is_registered(model):
            try:
                url = reverse(f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change", args=[quote(obj.pk)])
                return format_html('{}: <a href="{}">{}</a>', capfirst(opts.verbose_name), url, obj)
            except NoReverseMatch:
                pass
        return format_html("{}: {}", capfirst(opts.verbose_name), obj)

    deleted = collector.nested(label)
    protected = [label(obj) for obj in collector.protected]
    counts = {model._meta.verbose_name_plural: len(rows) for model, rows in collector.model_objs.items()}
    fast_queries = defaultdict(list)
    for queryset in collector.fast_deletes:
        fast_queries[queryset.model].append(queryset)
    for model, querysets in fast_queries.items():
        scope = Q(pk__in=querysets[0].values("pk"))
        for queryset in querysets[1:]:
            scope |= Q(pk__in=queryset.values("pk"))
        count = model._base_manager.using(using).filter(scope).count()
        if not count:
            continue
        check_permission(model)
        name = model._meta.verbose_name_plural
        counts[name] = counts.get(name, 0) + count
        deleted.append(format_html("{}: {}", capfirst(name), count))
    return deleted, counts, permissions, protected
