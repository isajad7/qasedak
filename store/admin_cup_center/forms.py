import re
from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils import timezone

from store.models import ConfigInventoryAsset, ConfigInventoryPool, ConfigLink, Inbound, Panel, SubscriptionCup


SUPPORTED_INBOUND_PROTOCOLS = (
    Inbound.Protocol.VLESS,
    Inbound.Protocol.VMESS,
    Inbound.Protocol.TROJAN,
)

INVENTORY_SOURCE_AUTO = "auto_pick_from_pool"
INVENTORY_SOURCE_MANUAL = "manual_select_assets"
INVENTORY_SOURCE_MODE_CHOICES = (
    (INVENTORY_SOURCE_AUTO, "برداشت خودکار از مخزن"),
    (INVENTORY_SOURCE_MANUAL, "انتخاب دستی کانفیگ‌ها"),
)


def _data_values(data, key):
    if hasattr(data, "getlist"):
        return data.getlist(key)
    value = data.get(key) if hasattr(data, "get") else None
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _coerce_positive_asset_ids(values):
    asset_ids = []
    for value in values or []:
        try:
            asset_id = int(value)
        except (TypeError, ValueError):
            continue
        if asset_id > 0:
            asset_ids.append(asset_id)
    return asset_ids


def _dedupe_ints(values):
    seen = set()
    deduped = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def inventory_asset_ids_from_data(data, pool):
    pool_id = getattr(pool, "pk", pool)
    scoped_keys = (
        f"inventory_asset_ids_{pool_id}",
        f"asset_ids_{pool_id}",
        f"selected_asset_ids_{pool_id}",
    )
    scoped_ids = []
    for key in scoped_keys:
        scoped_ids.extend(_coerce_positive_asset_ids(_data_values(data, key)))
    if scoped_ids:
        return _dedupe_ints(scoped_ids)

    generic_ids = []
    for key in ("asset_ids", "inventory_asset_ids", "selected_asset_ids"):
        generic_ids.extend(_coerce_positive_asset_ids(_data_values(data, key)))
    if not generic_ids:
        return []

    allowed_ids = set(
        ConfigInventoryAsset.objects.filter(pool_id=pool_id, pk__in=generic_ids).values_list("pk", flat=True)
    )
    return _dedupe_ints(asset_id for asset_id in generic_ids if asset_id in allowed_ids)


def inventory_pool_ids_from_asset_data(data):
    pool_ids = []
    scoped_key_pattern = re.compile(r"^(?:inventory_asset_ids|asset_ids|selected_asset_ids)_(\d+)$")
    keys = data.keys() if hasattr(data, "keys") else []
    for key in keys:
        match = scoped_key_pattern.match(str(key))
        if not match:
            continue
        asset_ids = _coerce_positive_asset_ids(_data_values(data, key))
        if asset_ids:
            pool_ids.append(int(match.group(1)))

    generic_ids = []
    for key in ("asset_ids", "inventory_asset_ids", "selected_asset_ids"):
        generic_ids.extend(_coerce_positive_asset_ids(_data_values(data, key)))
    if generic_ids:
        pool_ids.extend(
            ConfigInventoryAsset.objects.filter(pk__in=generic_ids).values_list("pool_id", flat=True)
        )
    return _dedupe_ints(pool_id for pool_id in pool_ids if pool_id)


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
    protocol = forms.ChoiceField(label="پروتکل", required=False)
    source_type = forms.ChoiceField(label="منبع", required=False)
    panel = forms.ModelChoiceField(label="پنل", queryset=Panel.objects.none(), required=False)
    inbound = forms.ModelChoiceField(label="اینباند", queryset=Inbound.objects.none(), required=False)

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
    panel = forms.ModelChoiceField(label="پنل", queryset=Panel.objects.none())
    inbound = forms.ModelChoiceField(label="اینباند", queryset=Inbound.objects.none())
    total_gb = forms.DecimalField(label="حجم (GB)", min_value=Decimal("0.001"), max_digits=8, decimal_places=3)
    duration_days = forms.IntegerField(label="مدت (روز)", min_value=1, max_value=3650)
    device_limit = forms.IntegerField(label="تعداد دستگاه", min_value=1, max_value=100)
    email_prefix = forms.CharField(label="پیشوند نام کانفیگ / ایمیل", max_length=80)
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
            self.add_error("inbound", "اینباند انتخاب‌شده به این پنل وصل نیست.")
        return cleaned


class QuickSubscriptionBuilderForm(CupCenterFormMixin, forms.Form):
    title = forms.CharField(label="عنوان", max_length=255)
    inbounds = forms.ModelMultipleChoiceField(
        label="اینباندها",
        queryset=Inbound.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    inventory_pools = forms.ModelMultipleChoiceField(
        label="مخزن‌های کانفیگ آماده",
        queryset=ConfigInventoryPool.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="از هر مخزن به تعداد مشخص‌شده لینک برداشته می‌شود و نوع تخصیص همان مخزن رعایت می‌شود.",
    )
    inventory_quantity = forms.IntegerField(
        label="تعداد پیش‌فرض از هر مخزن",
        min_value=1,
        max_value=50,
        required=False,
        initial=1,
    )
    volume_gb = forms.DecimalField(label="حجم (GB)", min_value=Decimal("0.001"), max_digits=8, decimal_places=3)
    duration_days = forms.IntegerField(label="مدت (روز)", min_value=1, max_value=3650)
    device_limit = forms.IntegerField(label="تعداد دستگاه", min_value=1, max_value=100)
    remark_prefix = forms.CharField(label="پیشوند نام کانفیگ / ایمیل", max_length=80)
    confirm_remote_create = forms.BooleanField(
        label="اگر اینباند انتخاب شود، این عملیات کانفیگ واقعی روی پنل می‌سازد.",
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        self.fields["title"].initial = f"اشتراک سریع {timestamp}"
        self.fields["inbounds"].queryset = (
            Inbound.objects.select_related("panel")
            .all()
            .order_by("panel__name", "inbound_id", "pk")
        )
        self.fields["inventory_pools"].queryset = ConfigInventoryPool.objects.filter(is_active=True).order_by("priority", "title", "pk")
        self.fields["volume_gb"].initial = Decimal("10")
        self.fields["duration_days"].initial = 30
        self.fields["device_limit"].initial = 2
        self.fields["remark_prefix"].initial = f"qasedak-cup-{timestamp}"
        self._style_fields()

    def _data_values(self, key):
        return _data_values(self.data, key)

    def _data_value(self, key, default=""):
        values = self._data_values(key)
        return values[-1] if values else default

    def _clean_inventory_quantity_for_pool(self, pool, default_quantity):
        raw_quantity = self._data_value(f"inventory_quantity_{pool.pk}", default_quantity or 1)
        try:
            quantity = int(raw_quantity or 1)
        except (TypeError, ValueError):
            self.add_error("inventory_pools", f"تعداد مخزن «{pool.title}» معتبر نیست.")
            return 1
        if quantity < 1 or quantity > 50:
            self.add_error("inventory_pools", f"تعداد مخزن «{pool.title}» باید بین ۱ تا ۵۰ باشد.")
            return max(1, min(quantity, 50))
        return quantity

    def _clean_inventory_asset_ids_for_pool(self, pool):
        asset_ids = inventory_asset_ids_from_data(self.data, pool)
        for value in asset_ids:
            try:
                asset_id = int(value)
            except (TypeError, ValueError):
                self.add_error("inventory_pools", f"شناسه کانفیگ انتخاب‌شده برای مخزن «{pool.title}» معتبر نیست.")
                continue
        return asset_ids

    def clean_remark_prefix(self):
        value = str(self.cleaned_data.get("remark_prefix") or "").strip()
        value = re.sub(r"\s+", "-", value)
        if not re.match(r"^[A-Za-z0-9._-]+$", value):
            raise forms.ValidationError("فقط حروف لاتین، عدد، نقطه، خط تیره و زیرخط مجاز است.")
        return value[:80]

    def clean(self):
        cleaned = super().clean()
        inbounds = list(cleaned.get("inbounds") or [])
        inventory_pools = list(cleaned.get("inventory_pools") or [])
        inferred_pool_ids = inventory_pool_ids_from_asset_data(self.data)
        existing_pool_ids = {pool.pk for pool in inventory_pools}
        missing_pool_ids = [pool_id for pool_id in inferred_pool_ids if pool_id not in existing_pool_ids]
        if missing_pool_ids:
            inferred_pools = list(
                ConfigInventoryPool.objects.filter(pk__in=missing_pool_ids).order_by("priority", "title", "pk")
            )
            pools_by_id = {pool.pk: pool for pool in inferred_pools}
            inventory_pools.extend(pool for pool_id in missing_pool_ids for pool in [pools_by_id.get(pool_id)] if pool)
            cleaned["inventory_pools"] = inventory_pools
        if not inbounds and not inventory_pools:
            self.add_error("inbounds", "حداقل یک اینباند یا یک مخزن کانفیگ آماده انتخاب کنید.")
            self.add_error("inventory_pools", "حداقل یک اینباند یا یک مخزن کانفیگ آماده انتخاب کنید.")
        if inbounds and not cleaned.get("confirm_remote_create"):
            self.add_error("confirm_remote_create", "برای ساخت کانفیگ روی پنل، تایید این گزینه لازم است.")
        if inventory_pools and not cleaned.get("inventory_quantity"):
            cleaned["inventory_quantity"] = 1
        inventory_selections = []
        default_quantity = cleaned.get("inventory_quantity") or 1
        for pool in inventory_pools:
            mode = str(self._data_value(f"inventory_mode_{pool.pk}", INVENTORY_SOURCE_AUTO) or INVENTORY_SOURCE_AUTO).strip()
            if pool.pk in missing_pool_ids and inventory_asset_ids_from_data(self.data, pool):
                mode = INVENTORY_SOURCE_MANUAL
            if mode not in {INVENTORY_SOURCE_AUTO, INVENTORY_SOURCE_MANUAL}:
                self.add_error("inventory_pools", f"روش تخصیص مخزن «{pool.title}» معتبر نیست.")
                mode = INVENTORY_SOURCE_AUTO
            asset_ids = self._clean_inventory_asset_ids_for_pool(pool)
            quantity = self._clean_inventory_quantity_for_pool(pool, default_quantity)
            if mode == INVENTORY_SOURCE_MANUAL:
                quantity = len(asset_ids)
                if not asset_ids:
                    self.add_error("inventory_pools", f"برای مخزن «{pool.title}» حداقل یک کانفیگ را انتخاب کنید.")
            inventory_selections.append(
                {
                    "pool": pool,
                    "mode": mode,
                    "quantity": quantity,
                    "asset_ids": asset_ids,
                }
            )
        cleaned["inventory_selections"] = inventory_selections
        return cleaned
