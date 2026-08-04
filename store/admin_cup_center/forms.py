import re
from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils import timezone

from store.models import ConfigLink, Inbound, Panel, SubscriptionCup


SUPPORTED_INBOUND_PROTOCOLS = (
    Inbound.Protocol.VLESS,
    Inbound.Protocol.VMESS,
    Inbound.Protocol.TROJAN,
)


class CupCenterFormMixin:
    def _style_fields(self):
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css_class} cup-field".strip()


class ManualCupForm(CupCenterFormMixin, forms.ModelForm):
    metadata = forms.JSONField(required=False, label="metadata", initial=dict)

    class Meta:
        model = SubscriptionCup
        fields = ("title", "status", "expires_at", "metadata")
        widgets = {
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "metadata": forms.Textarea(attrs={"rows": 5, "dir": "ltr"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].initial = SubscriptionCup.Status.ACTIVE
        for name in ("expires_at", "metadata"):
            self.fields[name].required = False
        self._style_fields()

    def clean_metadata(self):
        return self.cleaned_data.get("metadata") or {}


class ExistingConfigLinkFilterForm(CupCenterFormMixin, forms.Form):
    q = forms.CharField(label="جستجو", required=False)
    protocol = forms.ChoiceField(label="Protocol", required=False)
    source_type = forms.ChoiceField(label="Source", required=False)
    panel = forms.ModelChoiceField(label="Panel", queryset=Panel.objects.none(), required=False)
    inbound = forms.ModelChoiceField(label="Inbound", queryset=Inbound.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["protocol"].choices = [("", "همه protocolها"), *ConfigLink.Protocol.choices]
        self.fields["source_type"].choices = [("", "همه sourceها"), *ConfigLink.SourceType.choices]
        self.fields["panel"].queryset = Panel.objects.order_by("name", "pk")
        self.fields["inbound"].queryset = Inbound.objects.select_related("panel").order_by("panel__name", "inbound_id", "pk")
        self._style_fields()

    def filter_queryset(self, queryset):
        if not self.is_bound:
            return queryset
        if not self.is_valid():
            return queryset.none()
        cleaned = self.cleaned_data
        query = str(cleaned.get("q") or "").strip()
        if query:
            queryset = queryset.filter(
                Q(normalized_hash__icontains=query)
                | Q(remark__icontains=query)
                | Q(host__icontains=query)
                | Q(raw_link__icontains=query)
            )
        if cleaned.get("protocol"):
            queryset = queryset.filter(protocol=cleaned["protocol"])
        if cleaned.get("source_type"):
            queryset = queryset.filter(source_type=cleaned["source_type"])
        if cleaned.get("panel"):
            queryset = queryset.filter(source_panel=cleaned["panel"])
        if cleaned.get("inbound"):
            queryset = queryset.filter(source_inbound=cleaned["inbound"])
        return queryset


class ManualLinksForm(CupCenterFormMixin, forms.Form):
    raw_links = forms.CharField(
        label="لینک‌ها",
        widget=forms.Textarea(attrs={"rows": 12, "dir": "ltr", "placeholder": "vless://...\nvmess://...\ntrojan://...\nss://..."}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class PanelConfigIntoCupForm(CupCenterFormMixin, forms.Form):
    panel = forms.ModelChoiceField(label="Panel", queryset=Panel.objects.none())
    inbound = forms.ModelChoiceField(label="Inbound", queryset=Inbound.objects.none())
    total_gb = forms.DecimalField(label="Volume GB", min_value=Decimal("0.001"), max_digits=8, decimal_places=3)
    duration_days = forms.IntegerField(label="Duration days", min_value=1, max_value=3650)
    device_limit = forms.IntegerField(label="Device limit", min_value=1, max_value=100)
    email_prefix = forms.CharField(label="Remark / email prefix", max_length=80)
    confirm_remote_create = forms.BooleanField(
        label="این کار یک کانفیگ واقعی روی پنل می‌سازد.",
        required=True,
    )

    def __init__(self, *args, cup=None, **kwargs):
        self.cup = cup
        super().__init__(*args, **kwargs)
        plan = getattr(cup, "plan", None)
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        self.fields["panel"].queryset = Panel.objects.filter(is_active=True).order_by("name", "pk")
        self.fields["inbound"].queryset = (
            Inbound.objects.select_related("panel")
            .filter(is_active=True, panel__is_active=True)
            .order_by("panel__name", "inbound_id", "pk")
        )
        self.fields["total_gb"].initial = getattr(plan, "volume_gb", None) or Decimal("1")
        self.fields["duration_days"].initial = getattr(plan, "duration_days", None) or 30
        self.fields["device_limit"].initial = getattr(cup, "device_limit", None) or getattr(plan, "device_limit", None) or 2
        self.fields["email_prefix"].initial = f"qasedak-cup-{getattr(cup, 'pk', 'new')}-{timestamp}"
        self._style_fields()

    def clean_email_prefix(self):
        value = str(self.cleaned_data.get("email_prefix") or "").strip()
        value = re.sub(r"\s+", "-", value)
        if not re.match(r"^[A-Za-z0-9._-]+$", value):
            raise forms.ValidationError("فقط حروف لاتین، عدد، نقطه، خط تیره و زیرخط مجاز است.")
        return value[:80]

    def clean(self):
        cleaned = super().clean()
        panel = cleaned.get("panel")
        inbound = cleaned.get("inbound")
        if panel and inbound and inbound.panel_id != panel.pk:
            self.add_error("inbound", "Inbound انتخاب‌شده به این Panel وصل نیست.")
        return cleaned


class QuickSubscriptionBuilderForm(CupCenterFormMixin, forms.Form):
    title = forms.CharField(label="عنوان", max_length=255)
    inbounds = forms.ModelMultipleChoiceField(
        label="Inboundها",
        queryset=Inbound.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    volume_gb = forms.DecimalField(label="Volume GB", min_value=Decimal("0.001"), max_digits=8, decimal_places=3)
    duration_days = forms.IntegerField(label="Duration days", min_value=1, max_value=3650)
    device_limit = forms.IntegerField(label="Device limit", min_value=1, max_value=100)
    remark_prefix = forms.CharField(label="Remark / email prefix", max_length=80)
    confirm_remote_create = forms.BooleanField(
        label="این عملیات کانفیگ واقعی روی پنل می‌سازد.",
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        self.fields["title"].initial = f"Quick Sub {timestamp}"
        self.fields["inbounds"].queryset = (
            Inbound.objects.select_related("panel")
            .all()
            .order_by("panel__name", "inbound_id", "pk")
        )
        self.fields["volume_gb"].initial = Decimal("10")
        self.fields["duration_days"].initial = 30
        self.fields["device_limit"].initial = 2
        self.fields["remark_prefix"].initial = f"qasedak-cup-{timestamp}"
        self._style_fields()

    def clean_remark_prefix(self):
        value = str(self.cleaned_data.get("remark_prefix") or "").strip()
        value = re.sub(r"\s+", "-", value)
        if not re.match(r"^[A-Za-z0-9._-]+$", value):
            raise forms.ValidationError("فقط حروف لاتین، عدد، نقطه، خط تیره و زیرخط مجاز است.")
        return value[:80]

    def clean(self):
        cleaned = super().clean()
        inbounds = list(cleaned.get("inbounds") or [])
        if not inbounds:
            self.add_error("inbounds", "حداقل یک inbound انتخاب کنید.")
        return cleaned
