from django import template

from store.admin_access import get_visible_admin_sections, user_has_capability


register = template.Library()


@register.filter
def has_admin_capability(user, capability):
    return user_has_capability(user, capability)


@register.simple_tag(takes_context=True)
def visible_admin_sections(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    return get_visible_admin_sections(user)
