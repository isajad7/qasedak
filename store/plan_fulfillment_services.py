from __future__ import annotations

from django.db.models import Count
from django.utils.translation import gettext as _

from .config_inventory_services import get_pool_stock_summary, inventory_allocation_mode_label
from .models import ConfigInventoryPool, CupFillerRule, CupFulfillmentRecipe, Inbound, Plan


READY_STATUS = "ready"
WARNING_STATUS = "warning"
ERROR_STATUS = "error"
NOT_CONFIGURED_STATUS = "not_configured"
INACTIVE_STATUS = "inactive"


STATUS_LABELS = {
    READY_STATUS: _("آماده فروش"),
    WARNING_STATUS: _("آماده با هشدار"),
    ERROR_STATUS: _("غیرقابل فروش"),
    NOT_CONFIGURED_STATUS: _("بدون تنظیم"),
    INACTIVE_STATUS: _("غیرفعال"),
}

STATUS_TONES = {
    READY_STATUS: "success",
    WARNING_STATUS: "warning",
    ERROR_STATUS: "danger",
    NOT_CONFIGURED_STATUS: "slate",
    INACTIVE_STATUS: "slate",
}


def recipe_failure_policy_label(value):
    return {
        CupFulfillmentRecipe.FailurePolicy.STRICT: _("سخت‌گیرانه"),
        CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED: _("نیمه‌سخت‌گیرانه"),
    }.get(value, value or "-")


def rule_source_type_label(value):
    return {
        CupFillerRule.SourceType.PANEL_INBOUNDS: _("منبع پنل"),
        CupFillerRule.SourceType.INVENTORY_POOL: _("مخزن کانفیگ"),
    }.get(value, value or "-")


def _status_label(status):
    return STATUS_LABELS.get(status, status or "-")


def _status_tone(status):
    return STATUS_TONES.get(status, "slate")


def _diagnostic(label, status, detail=""):
    return {
        "label": label,
        "status": status,
        "status_label": _status_label(status),
        "tone": _status_tone(status),
        "detail": detail,
    }


def _available_capacity_label(capacity):
    return _("نامحدود") if capacity is None else capacity


def _rule_inbounds(rule):
    inbounds = list(rule.inbounds.all())
    if inbounds or not rule.panel_id:
        return inbounds, False
    fallback = list(
        Inbound.objects.filter(
            panel=rule.panel,
            is_active=True,
            available_for_new_orders=True,
        ).order_by("pk")[:1]
    )
    return fallback, bool(fallback)


def _inbound_missing_reality_pbk(inbound):
    return inbound.security == Inbound.Security.REALITY and not str(inbound.pbk or "").strip()


def _panel_source_name(rule, inbounds):
    panel_name = str(rule.panel) if rule.panel_id else _("بدون پنل")
    if not inbounds:
        return panel_name
    return _("%(panel)s / %(count)s اینباند") % {"panel": panel_name, "count": len(inbounds)}


def _capacity_issue_for_inbound(inbound, quantity):
    if inbound.max_clients is None:
        return ""
    free_slots = int(inbound.max_clients or 0) - int(inbound.current_users or 0)
    if free_slots >= int(quantity or 1):
        return ""
    return _("ظرفیت اینباند کافی نیست: ظرفیت آزاد %(free)s از %(needed)s کمتر است.") % {
        "free": max(free_slots, 0),
        "needed": int(quantity or 1),
    }


def _add_rule_issue(source, *, required, severity, message):
    if severity == ERROR_STATUS and not required:
        severity = WARNING_STATUS
    source["diagnostics"].append(_diagnostic(message, severity))
    source["status_code"] = ERROR_STATUS if severity == ERROR_STATUS else (
        WARNING_STATUS if source["status_code"] != ERROR_STATUS else source["status_code"]
    )
    source["status_label"] = _status_label(source["status_code"])
    source["status_tone"] = _status_tone(source["status_code"])
    return {"severity": severity, "message": message}


def _panel_rule_diagnostics(rule):
    inbounds, used_default_inbound = _rule_inbounds(rule)
    source = {
        "rule": rule,
        "rule_id": rule.pk,
        "position": rule.position,
        "source_type": rule.source_type,
        "source_type_label": rule_source_type_label(rule.source_type),
        "source_name": _panel_source_name(rule, inbounds),
        "quantity": int(rule.quantity or 1),
        "required": rule.required,
        "required_label": _("الزامی") if rule.required else _("اختیاری"),
        "expected_config_count": int(rule.quantity or 1) * max(len(inbounds), 1 if rule.panel_id else 0),
        "status_code": READY_STATUS,
        "status_label": _status_label(READY_STATUS),
        "status_tone": _status_tone(READY_STATUS),
        "diagnostics": [],
        "panel": rule.panel,
        "inbounds": inbounds,
        "uses_default_inbound": used_default_inbound,
    }
    issues = []

    panel = rule.panel
    if not panel:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("برای منبع پنل، پنل انتخاب نشده است.")))
        return source, issues
    if not panel.is_active:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("پنل انتخاب‌شده غیرفعال است.")))
    if not (str(panel.url or "").strip() and str(panel.username or "").strip() and str(panel.password or "").strip()):
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("اطلاعات اتصال پنل کامل نیست.")))
    if not str(panel.capability_profile or "").strip() or panel.capability_profile == panel.CapabilityProfile.UNKNOWN_SAFE:
        issues.append(_add_rule_issue(source, required=rule.required, severity=WARNING_STATUS, message=_("پروفایل قابلیت پنل هنوز قطعی نیست؛ قبل از فروش تست readiness پنل را اجرا کنید.")))
    if not inbounds:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("برای منبع پنل هیچ اینباند قابل فروشی انتخاب نشده است.")))
        return source, issues
    if any(inbound.panel_id != panel.pk for inbound in inbounds):
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("همه اینباندهای یک قانون پنل باید متعلق به همان پنل باشند.")))
    if len(inbounds) > 1 and panel.capability_profile != panel.CapabilityProfile.MODERN_MULTI_NODE:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("ساخت چنداینباندی فقط برای پنل modern multi-node مجاز است.")))
    if used_default_inbound:
        issues.append(_add_rule_issue(source, required=rule.required, severity=WARNING_STATUS, message=_("برای این قانون اینباند دستی انتخاب نشده و اولین اینباند قابل فروش پنل استفاده می‌شود.")))

    for inbound in inbounds:
        if not inbound.is_active:
            issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("یک اینباند انتخاب‌شده غیرفعال است.")))
        if not inbound.available_for_new_orders:
            issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("یک اینباند انتخاب‌شده برای فروش جدید فعال نیست.")))
        if _inbound_missing_reality_pbk(inbound):
            issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("کلید عمومی Reality برای یک اینباند لازم است و خالی مانده است.")))
        capacity_issue = _capacity_issue_for_inbound(inbound, rule.quantity)
        if capacity_issue:
            issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=capacity_issue))

    if source["status_code"] == READY_STATUS:
        source["diagnostics"].append(_diagnostic(_("پنل، اینباندها و ظرفیت محلی آماده هستند."), READY_STATUS))
    return source, issues


def _inventory_rule_diagnostics(rule):
    pool = rule.inventory_pool
    stock_summary = get_pool_stock_summary(pool) if pool else None
    source = {
        "rule": rule,
        "rule_id": rule.pk,
        "position": rule.position,
        "source_type": rule.source_type,
        "source_type_label": rule_source_type_label(rule.source_type),
        "source_name": str(pool) if pool else _("بدون مخزن"),
        "quantity": int(rule.quantity or 1),
        "required": rule.required,
        "required_label": _("الزامی") if rule.required else _("اختیاری"),
        "expected_config_count": int(rule.quantity or 1),
        "status_code": READY_STATUS,
        "status_label": _status_label(READY_STATUS),
        "status_tone": _status_tone(READY_STATUS),
        "diagnostics": [],
        "pool": pool,
        "stock_summary": stock_summary,
        "allocation_mode_label": inventory_allocation_mode_label(rule.allocation_mode or getattr(pool, "allocation_mode", "")),
    }
    issues = []
    if not pool:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("برای منبع مخزن، مخزن کانفیگ انتخاب نشده است.")))
        return source, issues
    if not pool.is_active:
        issues.append(_add_rule_issue(source, required=rule.required, severity=ERROR_STATUS, message=_("مخزن کانفیگ غیرفعال است.")))
    capacity = (stock_summary or {}).get("available_capacity")
    if capacity is not None and int(capacity or 0) < int(rule.quantity or 1):
        issues.append(
            _add_rule_issue(
                source,
                required=rule.required,
                severity=ERROR_STATUS,
                message=_("موجودی مخزن کافی نیست: %(available)s از %(needed)s آماده است.") % {
                    "available": int(capacity or 0),
                    "needed": int(rule.quantity or 1),
                },
            )
        )
    if stock_summary:
        source["available_capacity_label"] = _available_capacity_label(capacity)
        source["usable_asset_count"] = stock_summary.get("usable_asset_count", 0)
        source["asset_count"] = stock_summary.get("asset_count", 0)
    if source["status_code"] == READY_STATUS:
        source["diagnostics"].append(_diagnostic(_("موجودی مخزن برای سفارش بعدی کافی است."), READY_STATUS))
    return source, issues


def get_recipe_readiness(recipe):
    recipe = (
        CupFulfillmentRecipe.objects.select_related("plan")
        .prefetch_related("rules__inbounds", "rules__inbounds__panel")
        .get(pk=getattr(recipe, "pk", recipe))
    )
    rules = list(recipe.rules.select_related("panel", "inventory_pool").prefetch_related("inbounds").filter(is_active=True).order_by("position", "pk"))
    sources = []
    errors = []
    warnings = []
    checks = []

    plan = recipe.plan
    if plan.is_active and plan.is_public:
        checks.append(_diagnostic(_("پلن فعال و عمومی است."), READY_STATUS))
    else:
        message = _("پلن باید هم فعال باشد و هم در فروش عمومی نمایش داده شود.")
        checks.append(_diagnostic(message, ERROR_STATUS))
        errors.append(message)

    if recipe.is_active:
        checks.append(_diagnostic(_("دستور تحویل فعال است."), READY_STATUS))
    else:
        message = _("دستور تحویل غیرفعال است.")
        checks.append(_diagnostic(message, ERROR_STATUS))
        errors.append(message)

    if rules:
        checks.append(_diagnostic(_("حداقل یک قانون پرکننده فعال وجود دارد."), READY_STATUS))
    else:
        message = _("هیچ قانون پرکننده فعالی برای این دستور تعریف نشده است.")
        checks.append(_diagnostic(message, ERROR_STATUS))
        errors.append(message)

    for rule in rules:
        if rule.source_type == CupFillerRule.SourceType.PANEL_INBOUNDS:
            source, issues = _panel_rule_diagnostics(rule)
        elif rule.source_type == CupFillerRule.SourceType.INVENTORY_POOL:
            source, issues = _inventory_rule_diagnostics(rule)
        else:
            source = {
                "rule": rule,
                "rule_id": rule.pk,
                "position": rule.position,
                "source_type": rule.source_type,
                "source_type_label": rule_source_type_label(rule.source_type),
                "source_name": _("منبع ناشناخته"),
                "quantity": int(rule.quantity or 1),
                "required": rule.required,
                "required_label": _("الزامی") if rule.required else _("اختیاری"),
                "expected_config_count": 0,
                "status_code": ERROR_STATUS if rule.required else WARNING_STATUS,
                "status_label": _status_label(ERROR_STATUS if rule.required else WARNING_STATUS),
                "status_tone": _status_tone(ERROR_STATUS if rule.required else WARNING_STATUS),
                "diagnostics": [_diagnostic(_("نوع منبع این قانون پشتیبانی نمی‌شود."), ERROR_STATUS if rule.required else WARNING_STATUS)],
            }
            issues = [{"severity": ERROR_STATUS if rule.required else WARNING_STATUS, "message": _("نوع منبع این قانون پشتیبانی نمی‌شود.")}]
        for issue in issues:
            if issue["severity"] == ERROR_STATUS:
                errors.append(issue["message"])
            elif issue["severity"] == WARNING_STATUS:
                warnings.append(issue["message"])
        if source["status_code"] == ERROR_STATUS and not source["required"]:
            warnings.append(source["diagnostics"][-1]["label"])
        sources.append(source)

    if errors:
        status = ERROR_STATUS
    elif warnings or any(source["status_code"] == WARNING_STATUS for source in sources):
        status = WARNING_STATUS
    else:
        status = READY_STATUS

    panel_source_count = sum(1 for source in sources if source["source_type"] == CupFillerRule.SourceType.PANEL_INBOUNDS)
    inventory_pool_count = sum(1 for source in sources if source["source_type"] == CupFillerRule.SourceType.INVENTORY_POOL)
    expected_config_count = sum(int(source.get("expected_config_count") or 0) for source in sources)
    ready_expected_config_count = sum(
        int(source.get("expected_config_count") or 0)
        for source in sources
        if source["status_code"] != ERROR_STATUS
    )
    return {
        "recipe": recipe,
        "recipe_id": recipe.pk,
        "title": recipe.title,
        "plan": plan,
        "plan_label": str(plan),
        "is_active": recipe.is_active,
        "failure_policy": recipe.failure_policy,
        "failure_policy_label": recipe_failure_policy_label(recipe.failure_policy),
        "status_code": status,
        "status_label": _status_label(status),
        "status_tone": _status_tone(status),
        "checks": checks,
        "sources": sources,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "expected_config_count": expected_config_count,
        "ready_expected_config_count": ready_expected_config_count,
        "panel_source_count": panel_source_count,
        "inventory_pool_count": inventory_pool_count,
        "required_rule_count": sum(1 for source in sources if source["required"]),
        "optional_rule_count": sum(1 for source in sources if not source["required"]),
    }


def get_active_recipe_for_plan(plan):
    plan_id = getattr(plan, "pk", plan)
    if not plan_id:
        return None
    return CupFulfillmentRecipe.objects.filter(plan_id=plan_id, is_active=True).order_by("priority", "pk").first()


def plan_fulfillment_status_for_plan(plan):
    active_recipe = get_active_recipe_for_plan(plan)
    if active_recipe:
        readiness = get_recipe_readiness(active_recipe)
        return {
            "status_code": readiness["status_code"],
            "status_label": _("آماده") if readiness["status_code"] == READY_STATUS else (
                _("دارای هشدار") if readiness["status_code"] == WARNING_STATUS else _("خطا")
            ),
            "status_tone": readiness["status_tone"],
            "recipe": active_recipe,
            "readiness": readiness,
        }
    has_inactive_recipe = CupFulfillmentRecipe.objects.filter(plan=plan).exists()
    if has_inactive_recipe:
        return {
            "status_code": INACTIVE_STATUS,
            "status_label": _("غیرفعال"),
            "status_tone": _status_tone(INACTIVE_STATUS),
            "recipe": None,
            "readiness": None,
        }
    return {
        "status_code": NOT_CONFIGURED_STATUS,
        "status_label": _("بدون تنظیم"),
        "status_tone": _status_tone(NOT_CONFIGURED_STATUS),
        "recipe": None,
        "readiness": None,
    }


def simulate_recipe_dry_run(recipe):
    readiness = get_recipe_readiness(recipe)
    recipe = readiness["recipe"]
    sources = []
    for source in readiness["sources"]:
        if source["source_type"] == CupFillerRule.SourceType.PANEL_INBOUNDS:
            action = _("ساخت %(quantity)s کلاینت روی %(source)s") % {
                "quantity": source["quantity"],
                "source": source["source_name"],
            }
        elif source["source_type"] == CupFillerRule.SourceType.INVENTORY_POOL:
            action = _("برداشت %(quantity)s کانفیگ از %(source)s") % {
                "quantity": source["quantity"],
                "source": source["source_name"],
            }
        else:
            action = _("منبع ناشناخته بررسی می‌شود.")
        sources.append({**source, "dry_run_action": action})

    has_blocking_errors = bool(readiness["errors"])
    if has_blocking_errors and recipe.failure_policy == CupFulfillmentRecipe.FailurePolicy.STRICT:
        final_status = ERROR_STATUS
        final_label = _("would fail")
        final_fa = _("شکست می‌خورد")
    elif has_blocking_errors and readiness["ready_expected_config_count"] > 0:
        final_status = WARNING_STATUS
        final_label = _("would partially succeed")
        final_fa = _("به‌صورت ناقص موفق می‌شود")
    elif has_blocking_errors:
        final_status = ERROR_STATUS
        final_label = _("would fail")
        final_fa = _("شکست می‌خورد")
    else:
        final_status = READY_STATUS
        final_label = _("would succeed")
        final_fa = _("موفق می‌شود")

    return {
        "recipe": recipe,
        "readiness": readiness,
        "sources": sources,
        "final_status": final_status,
        "final_status_label": final_label,
        "final_status_fa": final_fa,
        "final_status_tone": _status_tone(final_status),
        "predicted_cup_item_count": readiness["ready_expected_config_count"] if final_status != ERROR_STATUS else 0,
        "would_create_allocation": False,
        "would_create_cup_item": False,
        "would_mutate_order": False,
    }


def active_public_plan_queryset():
    return (
        Plan.objects.filter(is_active=True, is_public=True, is_custom_volume=False)
        .select_related("store")
        .annotate(active_recipe_count=Count("cup_fulfillment_recipes"))
        .order_by("sort_order", "price", "pk")
    )


def low_stock_pool_count(threshold=3):
    count = 0
    for pool in ConfigInventoryPool.objects.filter(is_active=True).order_by("pk"):
        summary = get_pool_stock_summary(pool)
        capacity = summary.get("available_capacity")
        if capacity is not None and int(capacity or 0) <= int(threshold):
            count += 1
    return count
