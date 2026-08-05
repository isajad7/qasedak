from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .config_inventory_services import get_pool_stock_summary, import_config_assets
from .cup_fulfillment_services import preview_fulfillment_recipe
from .models import (
    ConfigAllocation,
    ConfigInventoryAsset,
    ConfigInventoryPool,
    CupFillerRule,
    CupFulfillmentRecipe,
)


class ConfigInventoryImportForm(forms.Form):
    pool = forms.ModelChoiceField(label=_("Inventory pool"), queryset=ConfigInventoryPool.objects.none())
    source_batch = forms.CharField(label=_("Source batch"), max_length=150, required=False)
    raw_text = forms.CharField(
        label=_("Config links"),
        widget=forms.Textarea(attrs={"rows": 14, "dir": "ltr", "placeholder": "vless://...\nvmess://...\ntrojan://...\nss://..."}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pool"].queryset = ConfigInventoryPool.objects.filter(is_active=True).order_by("priority", "title", "pk")


def _require_any_perm(user, permissions):
    if not any(user.has_perm(permission) for permission in permissions):
        raise PermissionDenied


def _available_stock_for_pool(pool):
    summary = get_pool_stock_summary(pool)
    capacity = summary["available_capacity"]
    return _("Unlimited") if capacity is None else capacity


def config_inventory_dashboard(request):
    _require_any_perm(
        request.user,
        (
            "store.view_configinventorypool",
            "store.view_configinventoryasset",
            "store.view_cupfulfillmentrecipe",
        ),
    )
    pools = (
        ConfigInventoryPool.objects.annotate(
            asset_count=Count("assets", distinct=True),
            active_allocation_count=Count("assets__allocations", filter=Q(assets__allocations__status=ConfigAllocation.Status.ACTIVE), distinct=True),
        )
        .order_by("priority", "title", "pk")[:12]
    )
    pool_rows = [
        {
            "pool": pool,
            "asset_count": pool.asset_count,
            "active_allocation_count": pool.active_allocation_count,
            "available_stock": _available_stock_for_pool(pool),
            "import_url": f"{reverse('admin_store_config_inventory_import')}?pool={pool.pk}",
        }
        for pool in pools
    ]
    action_cards = [
        {
            "title": _("ساخت مخزن جدید"),
            "url": reverse("admin:store_configinventorypool_add"),
            "description": _("یک Pool تازه برای نگهداری کانفیگ‌های آماده بسازید."),
        },
        {
            "title": _("وارد کردن لینک کانفیگ"),
            "url": reverse("admin_store_config_inventory_import"),
            "description": _("لینک‌ها را به صورت batch داخل یک مخزن وارد کنید."),
        },
        {
            "title": _("مشاهده کانفیگ‌های آماده"),
            "url": reverse("admin:store_configinventoryasset_changelist"),
            "description": _("دارایی‌های آماده، وضعیت و ظرفیت تخصیص را ببینید."),
        },
        {
            "title": _("مشاهده موجودی مخزن‌ها"),
            "url": reverse("admin:store_configinventorypool_changelist"),
            "description": _("موجودی و حالت تخصیص هر مخزن را بررسی کنید."),
        },
        {
            "title": _("ساخت Cup سریع از پنل/استخر"),
            "url": reverse("admin_store_cup_center_quick_build"),
            "description": _("Inboundهای پنل و Poolهای آماده را کنار هم انتخاب کنید."),
        },
        {
            "title": _("ساخت Recipe برای پلن"),
            "url": reverse("admin:store_cupfulfillmentrecipe_add"),
            "description": _("برای هر Plan مشخص کنید Cup از کدام منابع پر شود."),
        },
        {
            "title": _("پیش‌نمایش Recipe"),
            "url": reverse("admin:store_cupfulfillmentrecipe_changelist"),
            "description": _("Recipeها را باز کنید و پیش‌نمایش دستور تحویل را ببینید."),
        },
    ]
    context = {
        **admin.site.each_context(request),
        "title": _("انبار کانفیگ‌ها"),
        "action_cards": action_cards,
        "pool_rows": pool_rows,
        "pool_count": ConfigInventoryPool.objects.count(),
        "asset_count": ConfigInventoryAsset.objects.count(),
        "allocation_count": ConfigAllocation.objects.count(),
        "recipe_count": CupFulfillmentRecipe.objects.count(),
        "rule_count": CupFillerRule.objects.count(),
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
            raw_text = form.cleaned_data["raw_text"]
            source_batch = form.cleaned_data.get("source_batch")
            result = import_config_assets(pool, raw_text, source_batch=source_batch)
            total_lines = len(str(raw_text or "").splitlines())
            non_empty_lines = sum(1 for line in str(raw_text or "").splitlines() if str(line or "").strip())
            invalid_count = max(non_empty_lines - result.created_count, 0)
            import_summary = {
                "pool_id": pool.pk,
                "total_lines": total_lines,
                "created": result.created_count,
                "skipped": result.skipped_count,
                "duplicates": result.duplicate_count,
                "invalid": invalid_count,
                "available_stock": _available_stock_for_pool(pool),
            }
            messages.success(
                request,
                _("وارد کردن لینک‌ها انجام شد: %(created)s ساخته شد، %(skipped)s رد شد، %(duplicates)s تکراری تشخیص داده شد.")
                % {
                    "created": result.created_count,
                    "skipped": result.skipped_count,
                    "duplicates": result.duplicate_count,
                },
            )
            form = ConfigInventoryImportForm(initial={"pool": pool.pk, "source_batch": source_batch})
    else:
        form = ConfigInventoryImportForm(initial=initial)
    context = {
        **admin.site.each_context(request),
        "opts": ConfigInventoryPool._meta,
        "title": _("وارد کردن لینک کانفیگ"),
        "form": form,
        "result": result,
        "import_summary": import_summary,
        "dashboard_url": reverse("admin_store_config_inventory"),
        "asset_list_url": reverse("admin:store_configinventoryasset_changelist"),
        "quick_build_url": reverse("admin_store_cup_center_quick_build"),
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
    context = {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": _("پیش‌نمایش دستور تحویل"),
        "recipe": recipe,
        "preview": preview,
    }
    return TemplateResponse(request, "admin/store/config_inventory/recipe_preview.html", context)
