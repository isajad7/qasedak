from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .config_inventory_services import inventory_allocation_mode_label
from .models import ConfigInventoryPool, CupFillerRule, CupFulfillmentRecipe, Inbound, Panel, Plan
from .plan_fulfillment_services import (
    ERROR_STATUS,
    READY_STATUS,
    WARNING_STATUS,
    active_public_plan_queryset,
    get_recipe_readiness,
    low_stock_pool_count,
    plan_fulfillment_status_for_plan,
    recipe_failure_policy_label,
    rule_source_type_label,
    simulate_recipe_dry_run,
)


class PlanFulfillmentWizardForm(forms.Form):
    FULFILLMENT_CUP = "cup"
    FULFILLMENT_LEGACY = "legacy"
    FALLBACK_NONE = ""
    FALLBACK_INVENTORY = "inventory_pool"
    FALLBACK_PANEL = "panel_inbounds"

    fulfillment_type = forms.ChoiceField(
        label=_("نوع تحویل"),
        choices=(
            (FULFILLMENT_CUP, _("تحویل با ساب اختصاصی قاصدک")),
            (FULFILLMENT_LEGACY, _("تحویل قدیمی / legacy provisioning")),
        ),
        widget=forms.RadioSelect,
        initial=FULFILLMENT_CUP,
    )
    recipe_title = forms.CharField(label=_("عنوان دستور تحویل"), max_length=150, required=False)
    failure_policy = forms.ChoiceField(
        label=_("سیاست خطا و کمبود موجودی"),
        choices=(
            (CupFulfillmentRecipe.FailurePolicy.STRICT, _("سخت‌گیرانه")),
            (CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED, _("نیمه‌سخت‌گیرانه")),
        ),
        initial=CupFulfillmentRecipe.FailurePolicy.STRICT,
    )

    panel = forms.ModelChoiceField(label=_("پنل"), queryset=Panel.objects.none(), required=False, empty_label=_("پنل را انتخاب کنید"))
    inbounds = forms.ModelMultipleChoiceField(
        label=_("اینباندها"),
        queryset=Inbound.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    panel_quantity = forms.IntegerField(label=_("تعداد کلاینت"), min_value=1, initial=1, required=False)
    panel_required = forms.BooleanField(label=_("الزامی"), required=False, initial=True)
    panel_priority = forms.IntegerField(label=_("اولویت"), min_value=1, initial=1, required=False)
    panel_label_prefix = forms.CharField(label=_("پیشوند نام"), max_length=80, required=False)

    inventory_pool = forms.ModelChoiceField(
        label=_("مخزن کانفیگ"),
        queryset=ConfigInventoryPool.objects.none(),
        required=False,
        empty_label=_("مخزن را انتخاب کنید"),
    )
    inventory_quantity = forms.IntegerField(label=_("تعداد کانفیگ"), min_value=1, initial=1, required=False)
    inventory_required = forms.BooleanField(label=_("الزامی"), required=False, initial=True)
    inventory_priority = forms.IntegerField(label=_("اولویت"), min_value=1, initial=2, required=False)
    inventory_allocation_mode = forms.ChoiceField(label=_("نوع تخصیص"), choices=(), required=False)
    inventory_fallback_allowed = forms.BooleanField(label=_("اجازه fallback"), required=False)

    fallback_type = forms.ChoiceField(
        label=_("نوع منبع fallback"),
        choices=(
            (FALLBACK_NONE, _("بدون منبع fallback")),
            (FALLBACK_INVENTORY, _("مخزن کانفیگ اختیاری")),
            (FALLBACK_PANEL, _("پنل اختیاری")),
        ),
        required=False,
        initial=FALLBACK_NONE,
    )
    fallback_pool = forms.ModelChoiceField(
        label=_("مخزن fallback"),
        queryset=ConfigInventoryPool.objects.none(),
        required=False,
        empty_label=_("مخزن fallback را انتخاب کنید"),
    )
    fallback_panel = forms.ModelChoiceField(label=_("پنل fallback"), queryset=Panel.objects.none(), required=False, empty_label=_("پنل fallback را انتخاب کنید"))
    fallback_inbounds = forms.ModelMultipleChoiceField(
        label=_("اینباندهای fallback"),
        queryset=Inbound.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    fallback_quantity = forms.IntegerField(label=_("تعداد fallback"), min_value=1, initial=1, required=False)
    fallback_priority = forms.IntegerField(label=_("اولویت fallback"), min_value=1, initial=90, required=False)

    def __init__(self, *args, **kwargs):
        self.plan = kwargs.pop("plan", None)
        super().__init__(*args, **kwargs)
        panels = Panel.objects.filter(is_active=True).order_by("name", "pk")
        inbounds = Inbound.objects.select_related("panel").filter(is_active=True).order_by("panel__name", "inbound_id", "pk")
        pools = ConfigInventoryPool.objects.filter(is_active=True).order_by("priority", "title", "pk")
        self.fields["panel"].queryset = panels
        self.fields["fallback_panel"].queryset = panels
        self.fields["inbounds"].queryset = inbounds
        self.fields["fallback_inbounds"].queryset = inbounds
        self.fields["inventory_pool"].queryset = pools
        self.fields["fallback_pool"].queryset = pools
        self.fields["inventory_allocation_mode"].choices = [
            ("", _("بر اساس قانون مخزن")),
            *[(value, inventory_allocation_mode_label(value)) for value, _label in ConfigInventoryPool.AllocationMode.choices],
        ]
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxSelectMultiple):
                css_class = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"{css_class} plan-fulfillment-field".strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("fulfillment_type") == self.FULFILLMENT_LEGACY:
            return cleaned

        panel = cleaned.get("panel")
        inbounds = list(cleaned.get("inbounds") or [])
        if inbounds and not panel:
            panel = inbounds[0].panel
            cleaned["panel"] = panel
        if panel and inbounds and any(inbound.panel_id != panel.pk for inbound in inbounds):
            self.add_error("inbounds", _("همه اینباندهای منبع پنل باید از همان پنل باشند."))

        fallback_type = cleaned.get("fallback_type") or self.FALLBACK_NONE
        fallback_panel = cleaned.get("fallback_panel")
        fallback_inbounds = list(cleaned.get("fallback_inbounds") or [])
        if fallback_type == self.FALLBACK_PANEL:
            if fallback_inbounds and not fallback_panel:
                fallback_panel = fallback_inbounds[0].panel
                cleaned["fallback_panel"] = fallback_panel
            if not fallback_panel or not fallback_inbounds:
                self.add_error("fallback_inbounds", _("برای fallback پنلی، پنل و اینباند را انتخاب کنید."))
            elif any(inbound.panel_id != fallback_panel.pk for inbound in fallback_inbounds):
                self.add_error("fallback_inbounds", _("همه اینباندهای fallback باید از همان پنل باشند."))
        if fallback_type == self.FALLBACK_INVENTORY and not cleaned.get("fallback_pool"):
            self.add_error("fallback_pool", _("مخزن fallback را انتخاب کنید."))

        has_panel_source = bool(panel or inbounds)
        has_inventory_source = bool(cleaned.get("inventory_pool"))
        has_fallback_source = fallback_type in {self.FALLBACK_INVENTORY, self.FALLBACK_PANEL}
        if not (has_panel_source or has_inventory_source or has_fallback_source):
            raise forms.ValidationError(_("حداقل یک منبع پنل، مخزن یا fallback برای ساخت Cup انتخاب کنید."))
        return cleaned


def _require_plan_fulfillment_view_perm(user):
    if not (
        user.has_perm("store.view_plan")
        or user.has_perm("store.view_cupfulfillmentrecipe")
        or user.has_perm("store.view_cupfillerrule")
    ):
        raise PermissionDenied


def _require_plan_fulfillment_change_perm(user):
    if not (
        user.has_perm("store.change_cupfulfillmentrecipe")
        or user.has_perm("store.add_cupfulfillmentrecipe")
        or user.has_perm("store.change_plan")
    ):
        raise PermissionDenied


def _plan_price_label(plan):
    return f"{plan.price:,} {plan.currency}"


def _plan_volume_label(plan):
    return _("%(volume)s گیگ") % {"volume": plan.volume_gb}


def _plan_duration_label(plan):
    return _("%(days)s روز") % {"days": plan.duration_days}


def _recipe_urls(recipe):
    if not recipe:
        return {}
    return {
        "builder": reverse("admin_store_plan_fulfillment_recipe", args=[recipe.pk]),
        "preview": reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk]),
        "simulate": reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk]),
        "admin_change": reverse("admin:store_cupfulfillmentrecipe_change", args=[recipe.pk]),
    }


def _enrich_readiness_urls(readiness):
    for source in readiness.get("sources", []):
        rule_id = source.get("rule_id")
        if not rule_id:
            continue
        source["edit_url"] = reverse("admin:store_cupfillerrule_change", args=[rule_id])
        source["delete_url"] = reverse("admin:store_cupfillerrule_delete", args=[rule_id])
        source["simulate_anchor"] = f"rule-{rule_id}"
    return readiness


def _plan_card(plan):
    status = plan_fulfillment_status_for_plan(plan)
    recipe = status["recipe"]
    readiness = status.get("readiness") or {}
    urls = _recipe_urls(recipe)
    return {
        "plan": plan,
        "price": _plan_price_label(plan),
        "duration": _plan_duration_label(plan),
        "volume": _plan_volume_label(plan),
        "status": status,
        "recipe": recipe,
        "recipe_urls": urls,
        "setup_url": reverse("admin_store_plan_fulfillment_plan", args=[plan.pk]),
        "panel_source_count": readiness.get("panel_source_count", 0),
        "inventory_pool_count": readiness.get("inventory_pool_count", 0),
        "expected_config_count": readiness.get("expected_config_count", 0),
    }


def _dashboard_context(request, *, title=None):
    plans = list(active_public_plan_queryset()[:80])
    plan_cards = [_plan_card(plan) for plan in plans]
    active_recipe_readiness = []
    for recipe in CupFulfillmentRecipe.objects.filter(is_active=True).select_related("plan").order_by("priority", "pk"):
        try:
            active_recipe_readiness.append(get_recipe_readiness(recipe))
        except Exception:
            active_recipe_readiness.append({"status_code": ERROR_STATUS})
    connected_count = sum(1 for card in plan_cards if card["recipe"])
    no_config_count = sum(1 for card in plan_cards if not card["recipe"])
    ready_recipe_count = sum(1 for item in active_recipe_readiness if item.get("status_code") == READY_STATUS)
    warning_recipe_count = sum(1 for item in active_recipe_readiness if item.get("status_code") in {WARNING_STATUS, ERROR_STATUS})
    summary_cards = [
        {"label": _("تعداد پلن‌های فعال"), "value": len(plans), "description": _("پلن‌های فعال و عمومی"), "tone": "blue", "icon": "fas fa-box-open"},
        {"label": _("پلن‌های متصل به Recipe"), "value": connected_count, "description": _("دارای دستور تحویل فعال"), "tone": "emerald", "icon": "fas fa-link"},
        {"label": _("پلن‌های بدون تنظیم تحویل"), "value": no_config_count, "description": _("هنوز با legacy کار می‌کنند"), "tone": "amber" if no_config_count else "slate", "icon": "fas fa-circle-exclamation"},
        {"label": _("Recipeهای آماده"), "value": ready_recipe_count, "description": _("بدون هشدار فروش"), "tone": "cyan", "icon": "fas fa-check"},
        {"label": _("Recipeهای دارای هشدار"), "value": warning_recipe_count, "description": _("نیازمند بررسی قبل از فروش"), "tone": "rose" if warning_recipe_count else "slate", "icon": "fas fa-triangle-exclamation"},
        {"label": _("مخزن‌های کم‌موجودی"), "value": low_stock_pool_count(), "description": _("موجودی ۳ یا کمتر"), "tone": "warning", "icon": "fas fa-boxes-stacked"},
    ]
    action_cards = [
        {"title": _("اتصال یک پلن به تحویل خودکار"), "url": reverse("admin_store_plan_fulfillment_plans"), "description": _("از کارت هر پلن وارد Wizard اتصال شوید."), "icon": "fas fa-plug", "tone": "blue"},
        {"title": _("ساخت Recipe جدید"), "url": reverse("admin:store_cupfulfillmentrecipe_add"), "description": _("دستور خام بسازید و سپس در Builder کاملش کنید."), "icon": "fas fa-mug-hot", "tone": "emerald"},
        {"title": _("تست شبیه‌سازی تحویل"), "url": reverse("admin:store_cupfulfillmentrecipe_changelist"), "description": _("Recipe را انتخاب کنید و dry-run بگیرید."), "icon": "fas fa-vial", "tone": "cyan"},
        {"title": _("مشاهده مخزن کانفیگ‌ها"), "url": reverse("admin_store_config_inventory"), "description": _("موجودی و import کانفیگ‌ها را بررسی کنید."), "icon": "fas fa-boxes-stacked", "tone": "amber"},
        {"title": _("ساخت سریع Cup تستی"), "url": reverse("admin_store_cup_center_quick_build"), "description": _("Quick Builder قبلی بدون تغییر در دسترس است."), "icon": "fas fa-wand-magic-sparkles", "tone": "slate"},
    ]
    return {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": title or _("تحویل خودکار ساب برای پلن‌ها"),
        "subtitle": _("مشخص کنید هر پلن فروش بعد از خرید از چه پنل‌ها و مخزن‌هایی Cup بسازد و لینک ساب اختصاصی مشتری را تحویل دهد."),
        "summary_cards": summary_cards,
        "action_cards": action_cards,
        "plan_cards": plan_cards,
    }


def _recipe_initial(recipe):
    initial = {
        "fulfillment_type": PlanFulfillmentWizardForm.FULFILLMENT_CUP,
        "recipe_title": recipe.title if recipe else "",
        "failure_policy": recipe.failure_policy if recipe else CupFulfillmentRecipe.FailurePolicy.STRICT,
    }
    if not recipe:
        return initial
    for rule in recipe.rules.filter(is_active=True).prefetch_related("inbounds").select_related("panel", "inventory_pool").order_by("position", "pk"):
        if rule.source_type == CupFillerRule.SourceType.PANEL_INBOUNDS and "panel" not in initial and rule.required:
            initial.update(
                {
                    "panel": rule.panel_id,
                    "inbounds": list(rule.inbounds.values_list("pk", flat=True)),
                    "panel_quantity": rule.quantity,
                    "panel_required": rule.required,
                    "panel_priority": rule.position,
                    "panel_label_prefix": (rule.metadata or {}).get("label_prefix", ""),
                }
            )
        elif rule.source_type == CupFillerRule.SourceType.INVENTORY_POOL and "inventory_pool" not in initial and rule.required:
            initial.update(
                {
                    "inventory_pool": rule.inventory_pool_id,
                    "inventory_quantity": rule.quantity,
                    "inventory_required": rule.required,
                    "inventory_priority": rule.position,
                    "inventory_allocation_mode": rule.allocation_mode,
                    "inventory_fallback_allowed": bool((rule.metadata or {}).get("fallback_allowed")),
                }
            )
        elif not rule.required and rule.source_type == CupFillerRule.SourceType.INVENTORY_POOL and "fallback_type" not in initial:
            initial.update(
                {
                    "fallback_type": PlanFulfillmentWizardForm.FALLBACK_INVENTORY,
                    "fallback_pool": rule.inventory_pool_id,
                    "fallback_quantity": rule.quantity,
                    "fallback_priority": rule.position,
                }
            )
        elif not rule.required and rule.source_type == CupFillerRule.SourceType.PANEL_INBOUNDS and "fallback_type" not in initial:
            initial.update(
                {
                    "fallback_type": PlanFulfillmentWizardForm.FALLBACK_PANEL,
                    "fallback_panel": rule.panel_id,
                    "fallback_inbounds": list(rule.inbounds.values_list("pk", flat=True)),
                    "fallback_quantity": rule.quantity,
                    "fallback_priority": rule.position,
                }
            )
    return initial


def _create_panel_rule(recipe, *, panel, inbounds, quantity, required, position, label_prefix="", fallback=False):
    rule = CupFillerRule.objects.create(
        recipe=recipe,
        position=position,
        source_type=CupFillerRule.SourceType.PANEL_INBOUNDS,
        quantity=quantity,
        required=required,
        panel=panel,
        is_active=True,
        metadata={
            "created_from": "plan_fulfillment_builder",
            "label_prefix": label_prefix,
            "fallback": fallback,
        },
    )
    rule.inbounds.add(*inbounds)
    return rule


def _create_inventory_rule(recipe, *, pool, quantity, required, position, allocation_mode="", fallback_allowed=False, fallback=False):
    return CupFillerRule.objects.create(
        recipe=recipe,
        position=position,
        source_type=CupFillerRule.SourceType.INVENTORY_POOL,
        quantity=quantity,
        required=required,
        inventory_pool=pool,
        allocation_mode=allocation_mode or "",
        is_active=True,
        metadata={
            "created_from": "plan_fulfillment_builder",
            "fallback_allowed": bool(fallback_allowed),
            "fallback": fallback,
        },
    )


def _save_wizard(plan, form, *, actor):
    cleaned = form.cleaned_data
    if cleaned["fulfillment_type"] == PlanFulfillmentWizardForm.FULFILLMENT_LEGACY:
        CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).update(is_active=False)
        return None

    with transaction.atomic():
        recipe = CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).order_by("priority", "pk").first()
        if not recipe:
            recipe = CupFulfillmentRecipe(plan=plan)
        recipe.title = cleaned.get("recipe_title") or _("تحویل خودکار %(plan)s") % {"plan": plan.name}
        recipe.is_active = True
        recipe.failure_policy = cleaned.get("failure_policy") or CupFulfillmentRecipe.FailurePolicy.STRICT
        recipe.priority = 10
        metadata = dict(recipe.metadata or {})
        metadata.update({"created_from": "plan_fulfillment_builder", "last_saved_by": getattr(actor, "pk", None)})
        recipe.metadata = metadata
        recipe.save()
        CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).exclude(pk=recipe.pk).update(is_active=False)
        recipe.rules.all().delete()

        panel = cleaned.get("panel")
        inbounds = list(cleaned.get("inbounds") or [])
        if panel or inbounds:
            _create_panel_rule(
                recipe,
                panel=panel or (inbounds[0].panel if inbounds else None),
                inbounds=inbounds,
                quantity=cleaned.get("panel_quantity") or 1,
                required=bool(cleaned.get("panel_required")),
                position=cleaned.get("panel_priority") or 1,
                label_prefix=cleaned.get("panel_label_prefix") or "",
            )
        if cleaned.get("inventory_pool"):
            _create_inventory_rule(
                recipe,
                pool=cleaned["inventory_pool"],
                quantity=cleaned.get("inventory_quantity") or 1,
                required=bool(cleaned.get("inventory_required")),
                position=cleaned.get("inventory_priority") or 2,
                allocation_mode=cleaned.get("inventory_allocation_mode") or "",
                fallback_allowed=bool(cleaned.get("inventory_fallback_allowed")),
            )
        if cleaned.get("fallback_type") == PlanFulfillmentWizardForm.FALLBACK_INVENTORY and cleaned.get("fallback_pool"):
            _create_inventory_rule(
                recipe,
                pool=cleaned["fallback_pool"],
                quantity=cleaned.get("fallback_quantity") or 1,
                required=False,
                position=cleaned.get("fallback_priority") or 90,
                fallback=True,
            )
        if cleaned.get("fallback_type") == PlanFulfillmentWizardForm.FALLBACK_PANEL:
            fallback_inbounds = list(cleaned.get("fallback_inbounds") or [])
            if cleaned.get("fallback_panel") and fallback_inbounds:
                _create_panel_rule(
                    recipe,
                    panel=cleaned["fallback_panel"],
                    inbounds=fallback_inbounds,
                    quantity=cleaned.get("fallback_quantity") or 1,
                    required=False,
                    position=cleaned.get("fallback_priority") or 90,
                    fallback=True,
                )
    return recipe


def plan_fulfillment_dashboard(request):
    _require_plan_fulfillment_view_perm(request.user)
    return TemplateResponse(request, "admin/store/plan_fulfillment/dashboard.html", _dashboard_context(request))


def plan_fulfillment_plans(request):
    _require_plan_fulfillment_view_perm(request.user)
    return TemplateResponse(request, "admin/store/plan_fulfillment/dashboard.html", _dashboard_context(request, title=_("اتصال پلن به Cup")))


def plan_fulfillment_plan(request, plan_id):
    _require_plan_fulfillment_view_perm(request.user)
    plan = get_object_or_404(Plan.objects.select_related("store"), pk=plan_id)
    active_recipe = CupFulfillmentRecipe.objects.filter(plan=plan, is_active=True).order_by("priority", "pk").first()
    if request.method == "POST":
        _require_plan_fulfillment_change_perm(request.user)
        form = PlanFulfillmentWizardForm(request.POST, plan=plan)
        if form.is_valid():
            recipe = _save_wizard(plan, form, actor=request.user)
            if recipe:
                messages.success(request, _("دستور تحویل ذخیره شد و به پلن متصل شد."))
                return redirect("admin_store_plan_fulfillment_recipe", recipe_id=recipe.pk)
            messages.success(request, _("تحویل خودکار برای این پلن غیرفعال شد و مسیر legacy حفظ می‌شود."))
            return redirect("admin_store_plan_fulfillment_plan", plan_id=plan.pk)
    else:
        initial = _recipe_initial(active_recipe)
        if not initial.get("recipe_title"):
            initial["recipe_title"] = _("تحویل خودکار %(plan)s") % {"plan": plan.name}
        form = PlanFulfillmentWizardForm(initial=initial, plan=plan)
    context = {
        **admin.site.each_context(request),
        "opts": Plan._meta,
        "title": _("تنظیم تحویل ساب پلن"),
        "plan": plan,
        "form": form,
        "recipe": active_recipe,
        "recipe_urls": _recipe_urls(active_recipe),
        "plan_summary": {
            "price": _plan_price_label(plan),
            "volume": _plan_volume_label(plan),
            "duration": _plan_duration_label(plan),
            "sales_status": _("فعال و عمومی") if plan.is_active and plan.is_public else _("نیازمند بررسی"),
        },
    }
    return TemplateResponse(request, "admin/store/plan_fulfillment/plan.html", context)


def plan_fulfillment_recipe(request, recipe_id):
    _require_plan_fulfillment_view_perm(request.user)
    recipe = get_object_or_404(CupFulfillmentRecipe.objects.select_related("plan"), pk=recipe_id)
    readiness = _enrich_readiness_urls(get_recipe_readiness(recipe))
    context = {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": _("دستورهای تحویل"),
        "recipe": recipe,
        "readiness": readiness,
        "recipe_urls": _recipe_urls(recipe),
        "rule_source_type_label": rule_source_type_label,
        "failure_policy_label": recipe_failure_policy_label(recipe.failure_policy),
    }
    return TemplateResponse(request, "admin/store/plan_fulfillment/recipe.html", context)


def plan_fulfillment_recipe_preview(request, recipe_id):
    _require_plan_fulfillment_view_perm(request.user)
    recipe = get_object_or_404(CupFulfillmentRecipe.objects.select_related("plan"), pk=recipe_id)
    readiness = _enrich_readiness_urls(get_recipe_readiness(recipe))
    context = {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": _("پیش‌نمایش و readiness دستور تحویل"),
        "recipe": recipe,
        "readiness": readiness,
        "recipe_urls": _recipe_urls(recipe),
    }
    return TemplateResponse(request, "admin/store/plan_fulfillment/preview.html", context)


def plan_fulfillment_recipe_simulate(request, recipe_id):
    _require_plan_fulfillment_view_perm(request.user)
    recipe = get_object_or_404(CupFulfillmentRecipe.objects.select_related("plan"), pk=recipe_id)
    simulation = simulate_recipe_dry_run(recipe)
    simulation["readiness"] = _enrich_readiness_urls(simulation["readiness"])
    context = {
        **admin.site.each_context(request),
        "opts": CupFulfillmentRecipe._meta,
        "title": _("تست شبیه‌سازی تحویل"),
        "recipe": recipe,
        "simulation": simulation,
        "readiness": simulation["readiness"],
        "recipe_urls": _recipe_urls(recipe),
    }
    return TemplateResponse(request, "admin/store/plan_fulfillment/simulate.html", context)
