import re

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .config_inventory_services import (
    DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT,
    DIRECT_LINKS_IMPORT_MODE,
    SUBSCRIPTION_URL_IMPORT_MODE,
    get_pool_stock_summary,
    import_config_assets,
    import_config_assets_from_subscription_url,
    inventory_allocation_mode_label,
)
from .cup_fulfillment_services import preview_fulfillment_recipe
from .models import (
    ConfigAllocation,
    ConfigInventoryAsset,
    ConfigInventoryPool,
    CupFillerRule,
    CupFulfillmentRecipe,
)


class ConfigInventoryImportForm(forms.Form):
    import_mode = forms.ChoiceField(
        label=_("روش وارد کردن"),
        choices=(
            (DIRECT_LINKS_IMPORT_MODE, _("وارد کردن لینک‌های کانفیگ")),
            (SUBSCRIPTION_URL_IMPORT_MODE, _("وارد کردن از لینک Subscription")),
        ),
        required=False,
        widget=forms.RadioSelect,
        initial=DIRECT_LINKS_IMPORT_MODE,
    )
    pool = forms.ModelChoiceField(
        label=_("مخزن کانفیگ"),
        queryset=ConfigInventoryPool.objects.none(),
        empty_label=_("یک مخزن کانفیگ انتخاب کنید"),
        help_text=_("لینک‌هایی که وارد می‌کنید داخل این مخزن ذخیره می‌شوند و بعداً می‌توانند برای ساخت Cup یا فروش پلن استفاده شوند."),
    )
    source_batch = forms.CharField(
        label=_("نام دسته / Batch"),
        max_length=150,
        required=False,
        help_text=_("برای ردگیری گروهی لینک‌ها استفاده می‌شود و می‌تواند خالی بماند."),
    )
    raw_text = forms.CharField(
        label=_("لینک‌های کانفیگ"),
        required=False,
        help_text=_("هر خط یک کانفیگ جداگانه است. لینک‌های تکراری مجازند."),
        widget=forms.Textarea(attrs={"rows": 14, "dir": "ltr", "placeholder": "vless://...\nvmess://...\ntrojan://...\nss://..."}),
    )
    subscription_url = forms.CharField(
        label=_("لینک Subscription تامین‌کننده"),
        max_length=2048,
        required=False,
        help_text=_("لینک کامل subscription تامین‌کننده را وارد کنید. در نتیجه فقط نسخه masked نمایش داده می‌شود."),
        widget=forms.TextInput(attrs={"dir": "ltr", "placeholder": "https://supplier.example.com/sub/..."}),
    )
    fetch_timeout = forms.IntegerField(
        label=_("مهلت دریافت (ثانیه)"),
        min_value=1,
        max_value=60,
        required=False,
        initial=DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT,
        help_text=_("پیش‌فرض ۱۰ ثانیه است."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pool"].queryset = ConfigInventoryPool.objects.filter(is_active=True).order_by("priority", "title", "pk")
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{css_class} inventory-field".strip()

    def clean(self):
        cleaned = super().clean()
        import_mode = cleaned.get("import_mode") or DIRECT_LINKS_IMPORT_MODE
        cleaned["import_mode"] = import_mode
        if import_mode == SUBSCRIPTION_URL_IMPORT_MODE:
            if not str(cleaned.get("subscription_url") or "").strip():
                self.add_error("subscription_url", _("لینک Subscription را وارد کنید."))
            cleaned["raw_text"] = ""
        else:
            if not str(cleaned.get("raw_text") or "").strip():
                self.add_error("raw_text", _("حداقل یک لینک کانفیگ وارد کنید."))
            cleaned["subscription_url"] = ""
        if not cleaned.get("fetch_timeout"):
            cleaned["fetch_timeout"] = DEFAULT_SUBSCRIPTION_FETCH_TIMEOUT
        return cleaned


def _require_any_perm(user, permissions):
    if not any(user.has_perm(permission) for permission in permissions):
        raise PermissionDenied


def _available_stock_for_pool(pool):
    summary = get_pool_stock_summary(pool)
    capacity = summary["available_capacity"]
    return _("نامحدود") if capacity is None else capacity


def _pool_import_capacity_behavior(pool):
    if pool.allocation_mode == ConfigInventoryPool.AllocationMode.EXCLUSIVE:
        return _("اختصاصی: هر کانفیگ فقط یک بار قابل فروش است")
    if pool.allocation_mode == ConfigInventoryPool.AllocationMode.SHARED_LIMITED:
        if pool.max_allocations_per_asset:
            return _("اشتراکی محدود: هر کانفیگ تا ظرفیت تعیین‌شده قابل استفاده است")
        return _("اشتراکی محدود: ظرفیت خالی است و بدون محدودیت قابل استفاده است")
    return _("اشتراکی نامحدود: هر کانفیگ بدون محدودیت قابل استفاده است")


def _pool_import_capacity_rows():
    return [
        {
            "pool_id": pool.pk,
            "pool_title": pool.title,
            "allocation_mode_label": inventory_allocation_mode_label(pool.allocation_mode),
            "max_allocations": pool.max_allocations_per_asset if pool.max_allocations_per_asset is not None else _("نامحدود"),
            "behavior": _pool_import_capacity_behavior(pool),
        }
        for pool in ConfigInventoryPool.objects.filter(is_active=True).order_by("priority", "title", "pk")
    ]


def _friendly_import_warning(warning):
    if "returned HTML" in str(warning or ""):
        return _("پاسخ Subscription شبیه صفحه HTML بود و کانفیگ پشتیبانی‌شده‌ای داخل آن پیدا نشد.")
    if "Supplier subscription did not contain supported config links" in str(warning or ""):
        return _("لینک Subscription دریافتی کانفیگ پشتیبانی‌شده‌ای نداشت.")
    match = re.search(r"Line\s+(\d+)\s+skipped", str(warning or ""))
    if match:
        return _("خط %(line)s رد شد: نوع لینک پشتیبانی نمی‌شود.") % {"line": match.group(1)}
    return _("یک خط رد شد؛ نوع لینک یا قالب آن معتبر نبود.")


def _friendly_import_error(error):
    text = str(error or "")
    if "http or https" in text:
        return _("لینک Subscription باید با http یا https شروع شود.")
    if "HTTP" in text:
        return _("دریافت لینک Subscription از تامین‌کننده ناموفق بود.")
    if "too large" in text:
        return _("پاسخ Subscription بیش از حد بزرگ بود.")
    if "fetch failed" in text or "fetch" in text:
        return _("دریافت لینک Subscription در زمان تعیین‌شده ناموفق بود.")
    return _("وارد کردن از لینک Subscription انجام نشد.")


def _friendly_recipe_warning(warning):
    match = re.search(r"Rule\s+(\d+)", str(warning or ""))
    position = match.group(1) if match else "-"
    if "no selected inbounds" in str(warning or ""):
        return _("قانون %(position)s اینباند انتخاب‌شده ندارد.") % {"position": position}
    if "inventory stock may be insufficient" in str(warning or ""):
        return _("موجودی مخزن کانفیگ برای قانون %(position)s ممکن است کافی نباشد.") % {"position": position}
    return _("یک هشدار در دستور پر کردن Cup وجود دارد.")


def _recipe_failure_policy_label(value):
    return {
        CupFulfillmentRecipe.FailurePolicy.STRICT: _("سخت‌گیرانه"),
        CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED: _("اجازه تحویل ناقص"),
    }.get(value, value or "-")


def _rule_source_type_label(value):
    return {
        CupFillerRule.SourceType.PANEL_INBOUNDS: _("پنل‌ها و اینباندها"),
        CupFillerRule.SourceType.INVENTORY_POOL: _("مخزن کانفیگ"),
    }.get(value, value or "-")


def _stock_display_from_capacity(capacity):
    return _("نامحدود") if capacity is None else capacity


def _dashboard_stock_state(pool_summaries):
    finite_available = 0
    unlimited_pool_count = 0
    low_stock_count = 0
    for summary in pool_summaries:
        if not summary["is_active"]:
            continue
        capacity = summary["available_capacity"]
        if capacity is None:
            if summary["usable_asset_count"]:
                unlimited_pool_count += 1
            continue
        finite_available += int(capacity or 0)
        if int(capacity or 0) <= 3:
            low_stock_count += 1
    if unlimited_pool_count and finite_available:
        sellable_stock = _("%(count)s + نامحدود") % {"count": finite_available}
    elif unlimited_pool_count:
        sellable_stock = _("نامحدود")
    else:
        sellable_stock = finite_available
    return sellable_stock, low_stock_count


def _pool_dashboard_row(pool, summary, active_allocation_count):
    status_counts = summary["status_counts"]
    inactive_count = (
        status_counts.get(ConfigInventoryAsset.Status.DISABLED, 0)
        + status_counts.get(ConfigInventoryAsset.Status.BURNED, 0)
        + status_counts.get(ConfigInventoryAsset.Status.EXPIRED, 0)
    )
    return {
        "pool": pool,
        "allocation_mode_label": inventory_allocation_mode_label(pool.allocation_mode),
        "asset_count": summary["asset_count"],
        "available_count": status_counts.get(ConfigInventoryAsset.Status.AVAILABLE, 0),
        "assigned_count": status_counts.get(ConfigInventoryAsset.Status.ASSIGNED, 0),
        "inactive_count": inactive_count,
        "active_allocation_count": active_allocation_count,
        "available_stock": _stock_display_from_capacity(summary["available_capacity"]),
        "import_url": f"{reverse('admin_store_config_inventory_import')}?pool={pool.pk}",
        "quick_build_url": f"{reverse('admin_store_cup_center_quick_build')}?inventory_pool={pool.pk}",
        "assets_url": f"{reverse('admin:store_configinventoryasset_changelist')}?pool__id__exact={pool.pk}",
    }


def config_inventory_dashboard(request):
    _require_any_perm(
        request.user,
        (
            "store.view_configinventorypool",
            "store.view_configinventoryasset",
            "store.view_cupfulfillmentrecipe",
        ),
    )
    pools = list(
        ConfigInventoryPool.objects.annotate(
            asset_count=Count("assets", distinct=True),
            active_allocation_count=Count("assets__allocations", filter=Q(assets__allocations__status=ConfigAllocation.Status.ACTIVE), distinct=True),
        )
        .order_by("priority", "title", "pk")
    )
    pool_summaries = [(pool, get_pool_stock_summary(pool)) for pool in pools]
    sellable_stock, low_stock_count = _dashboard_stock_state([summary for _, summary in pool_summaries])
    pool_rows = [
        _pool_dashboard_row(pool, summary, getattr(pool, "active_allocation_count", 0))
        for pool, summary in pool_summaries[:12]
    ]
    action_cards = [
        {
            "title": _("ساخت مخزن جدید"),
            "url": reverse("admin:store_configinventorypool_add"),
            "description": _("یک مخزن کانفیگ آماده برای فروش یا ساخت Cup تعریف کنید."),
            "icon": "fas fa-plus",
            "tone": "blue",
        },
        {
            "title": _("وارد کردن کانفیگ"),
            "url": reverse("admin_store_config_inventory_import"),
            "description": _("چند لینک را یک‌جا داخل مخزن کانفیگ ذخیره کنید."),
            "icon": "fas fa-file-import",
            "tone": "emerald",
        },
        {
            "title": _("ساخت سریع Cup از مخزن/پنل"),
            "url": reverse("admin_store_cup_center_quick_build"),
            "description": _("منابع پنل و مخزن کانفیگ را در یک لینک اشتراک ترکیب کنید."),
            "icon": "fas fa-wand-magic-sparkles",
            "tone": "amber",
        },
        {
            "title": _("ساخت Recipe برای پلن"),
            "url": reverse("admin:store_cupfulfillmentrecipe_add"),
            "description": _("برای هر Plan مشخص کنید Cup از کدام منابع پر شود."),
            "icon": "fas fa-mug-hot",
            "tone": "cyan",
        },
        {
            "title": _("پیش‌نمایش Recipe"),
            "url": reverse("admin:store_cupfulfillmentrecipe_changelist"),
            "description": _("Recipeها را باز کنید و پیش‌نمایش دستور تحویل را ببینید."),
            "icon": "fas fa-eye",
            "tone": "slate",
        },
    ]
    summary_cards = [
        {
            "label": _("تعداد مخزن‌ها"),
            "value": ConfigInventoryPool.objects.count(),
            "description": _("همه مخزن‌های تعریف‌شده"),
            "tone": "blue",
            "icon": "fas fa-boxes-stacked",
        },
        {
            "label": _("کانفیگ‌های آماده"),
            "value": ConfigInventoryAsset.objects.filter(status=ConfigInventoryAsset.Status.AVAILABLE).count(),
            "description": _("قابل برداشت برای فروش یا Cup"),
            "tone": "emerald",
            "icon": "fas fa-list-check",
        },
        {
            "label": _("کانفیگ‌های تخصیص‌داده‌شده"),
            "value": ConfigInventoryAsset.objects.filter(status=ConfigInventoryAsset.Status.ASSIGNED).count(),
            "description": _("دارای تخصیص فعال یا مصرف‌شده"),
            "tone": "amber",
            "icon": "fas fa-share-nodes",
        },
        {
            "label": _("موجودی قابل فروش"),
            "value": sellable_stock,
            "description": _("ظرفیت مخزن‌های فعال"),
            "tone": "cyan",
            "icon": "fas fa-store",
        },
        {
            "label": _("هشدار کمبود موجودی"),
            "value": low_stock_count,
            "description": _("مخزن فعال با موجودی ۳ یا کمتر"),
            "tone": "rose" if low_stock_count else "slate",
            "icon": "fas fa-triangle-exclamation",
        },
    ]
    context = {
        **admin.site.each_context(request),
        "title": _("انبار کانفیگ‌ها"),
        "subtitle": _("مدیریت مخزن‌های کانفیگ آماده، import لینک‌ها، و اتصال آن‌ها به ساخت Cup"),
        "action_cards": action_cards,
        "summary_cards": summary_cards,
        "pool_rows": pool_rows,
        "add_pool_url": reverse("admin:store_configinventorypool_add"),
        "pool_list_url": reverse("admin:store_configinventorypool_changelist"),
        "active_pool_count": ConfigInventoryPool.objects.filter(is_active=True).count(),
    }
    return TemplateResponse(request, "admin/store/config_inventory/dashboard.html", context)


def config_inventory_import(request):
    if not request.user.has_perm("store.add_configinventoryasset"):
        raise PermissionDenied
    initial = {}
    pool_id = request.GET.get("pool")
    if pool_id:
        initial["pool"] = pool_id
    result = None
    import_summary = None
    if request.method == "POST":
        form = ConfigInventoryImportForm(request.POST)
        if form.is_valid():
            pool = form.cleaned_data["pool"]
            source_batch = form.cleaned_data.get("source_batch")
            import_mode = form.cleaned_data.get("import_mode") or DIRECT_LINKS_IMPORT_MODE
            if import_mode == SUBSCRIPTION_URL_IMPORT_MODE:
                result = import_config_assets_from_subscription_url(
                    pool,
                    form.cleaned_data.get("subscription_url"),
                    source_batch=source_batch,
                    timeout=form.cleaned_data.get("fetch_timeout"),
                )
                total_lines = result.total_count + result.skipped_count
            else:
                raw_text = form.cleaned_data["raw_text"]
                result = import_config_assets(pool, raw_text, source_batch=source_batch, import_mode=DIRECT_LINKS_IMPORT_MODE)
                total_lines = len(str(raw_text or "").splitlines())
            import_summary = {
                "pool_id": pool.pk,
                "pool_title": pool.title,
                "import_mode": import_mode,
                "is_subscription_url": import_mode == SUBSCRIPTION_URL_IMPORT_MODE,
                "fetched": result.fetched,
                "fetched_label": _("بله") if result.fetched else (_("خیر") if result.fetched is False else "-"),
                "source_url_masked": result.source_url_masked,
                "decoded_as_base64": result.decoded_as_base64,
                "response_type": result.response_type or "-",
                "total_lines": total_lines,
                "total_configs_found": result.configs_found or result.total_count,
                "created": result.created_count,
                "skipped": result.skipped_count,
                "duplicates": result.duplicate_count,
                "invalid": result.skipped_count,
                "available_stock": _available_stock_for_pool(pool),
                "warnings": [_friendly_import_warning(warning) for warning in result.warnings],
                "errors": [_friendly_import_error(error) for error in result.errors],
            }
            if result.errors:
                messages.error(request, _("وارد کردن کانفیگ انجام نشد یا ناقص ماند. خلاصه پایین صفحه را ببینید."))
            else:
                messages.success(
                    request,
                    _("وارد کردن لینک‌ها انجام شد: %(created)s ساخته شد، %(skipped)s رد شد، %(duplicates)s تکراری تشخیص داده شد.")
                    % {
                        "created": result.created_count,
                        "skipped": result.skipped_count,
                        "duplicates": result.duplicate_count,
                    },
                )
            form = ConfigInventoryImportForm(initial={"pool": pool.pk, "source_batch": source_batch, "import_mode": import_mode})
    else:
        form = ConfigInventoryImportForm(initial=initial)
    context = {
        **admin.site.each_context(request),
        "opts": ConfigInventoryPool._meta,
        "title": _("وارد کردن کانفیگ"),
        "form": form,
        "result": result,
        "import_summary": import_summary,
        "dashboard_url": reverse("admin_store_config_inventory"),
        "asset_list_url": reverse("admin:store_configinventoryasset_changelist"),
        "quick_build_url": reverse("admin_store_cup_center_quick_build"),
        "recipe_add_url": reverse("admin:store_cupfulfillmentrecipe_add"),
        "add_pool_url": reverse("admin:store_configinventorypool_add"),
        "has_pools": ConfigInventoryPool.objects.filter(is_active=True).exists(),
        "pool_capacity_rows": _pool_import_capacity_rows(),
    }
    return TemplateResponse(request, "admin/store/config_inventory/import_assets.html", context)


def fulfillment_recipe_preview(request, recipe_id):
    if not request.user.has_perm("store.view_cupfulfillmentrecipe"):
        raise PermissionDenied
    recipe = CupFulfillmentRecipe.objects.select_related("plan").filter(pk=recipe_id).first()
    if not recipe:
        from django.http import Http404

        raise Http404
    preview = preview_fulfillment_recipe(recipe)
    preview["failure_policy_label"] = _recipe_failure_policy_label(preview.get("failure_policy"))
    preview["warnings"] = [_friendly_recipe_warning(warning) for warning in preview.get("warnings", [])]
    for rule in preview.get("rules", []):
        rule["source_type_label"] = _rule_source_type_label(rule.get("source_type"))
        stock_summary = rule.get("stock_summary") or {}
        if stock_summary:
            stock_summary["allocation_mode_label"] = inventory_allocation_mode_label(stock_summary.get("allocation_mode"))
            capacity = stock_summary.get("available_capacity")
            stock_summary["available_capacity_label"] = _("نامحدود") if capacity is None else capacity
    context = {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": _("پیش‌نمایش دستور تحویل"),
        "recipe": recipe,
        "preview": preview,
    }
    return TemplateResponse(request, "admin/store/config_inventory/recipe_preview.html", context)
