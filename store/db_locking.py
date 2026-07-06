from django.db import connections


def select_for_update_self(queryset):
    """Lock only the queryset's base rows when the backend supports OF clauses."""
    connection = connections[queryset.db]
    if connection.features.has_select_for_update_of:
        return queryset.select_for_update(of=("self",))
    return queryset.select_for_update()
