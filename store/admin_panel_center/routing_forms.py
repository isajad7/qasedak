from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from store.models import Inbound, Panel


ROUTE_MODE_NONE = "none"
ROUTE_MODE_SINGLE = "single"
ROUTE_MODE_MULTI = "multi"

ROUTE_MODE_CHOICES = (
    (ROUTE_MODE_NONE, _("بدون route / غیرفعال")),
    (ROUTE_MODE_SINGLE, _("تک‌سرور")),
    (ROUTE_MODE_MULTI, _("چندسرور")),
)


class PlanRoutingForm(forms.Form):
    delivery_mode = forms.ChoiceField(label=_("نوع تحویل"), choices=ROUTE_MODE_CHOICES)
    panel = forms.ModelChoiceField(
        label=_("پنل"),
        queryset=Panel.objects.none(),
        required=False,
        help_text=_("پنل مقصد route. پنل‌های unsupported در مرحله اعتبارسنجی رد می‌شوند."),
    )
    inbound = forms.ModelChoiceField(
        label=_("اینباند"),
        queryset=Inbound.objects.none(),
        required=False,
        help_text=_("برای حالت تک‌سرور یک inbound انتخاب کنید."),
    )
    inbounds = forms.ModelMultipleChoiceField(
        label=_("اینباندها"),
        queryset=Inbound.objects.none(),
        required=False,
        help_text=_("برای حالت چندسرور حداقل دو inbound از یک پنل انتخاب کنید."),
    )

    def __init__(self, *args, store=None, initial_panel=None, **kwargs):
        super().__init__(*args, **kwargs)
        panel_qs = Panel.objects.filter(is_active=True).order_by("store__name", "name", "pk")
        inbound_qs = Inbound.objects.filter(panel__is_active=True).select_related("panel").order_by(
            "panel__name",
            "inbound_id",
            "pk",
        )
        if store:
            panel_qs = panel_qs.filter(Q(store=store) | Q(store__isnull=True))
            inbound_qs = inbound_qs.filter(Q(panel__store=store) | Q(panel__store__isnull=True))
        selected_panel = self._selected_panel(initial_panel)
        if selected_panel and not self.data:
            inbound_qs = inbound_qs.filter(panel=selected_panel)
        elif not self.data:
            inbound_qs = Inbound.objects.none()
        self.fields["panel"].queryset = panel_qs
        self.fields["inbound"].queryset = inbound_qs
        self.fields["inbounds"].queryset = inbound_qs

    def _selected_panel(self, initial_panel):
        raw_panel_id = None
        if self.data:
            raw_panel_id = self.data.get("panel")
        elif initial_panel:
            raw_panel_id = getattr(initial_panel, "pk", initial_panel)
        try:
            raw_panel_id = int(raw_panel_id or 0)
        except (TypeError, ValueError):
            raw_panel_id = 0
        if not raw_panel_id:
            return None
        return Panel.objects.filter(pk=raw_panel_id).first()
