import json
import logging
from dataclasses import dataclass
from functools import wraps

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone


logger = logging.getLogger(__name__)


ROLE_GROUP_PREFIX = "Qasedak "


@dataclass(frozen=True)
class StaffRolePreset:
    key: str
    label: str
    description: str
    capabilities: tuple[str, ...]

    @property
    def group_name(self):
        return f"{ROLE_GROUP_PREFIX}{self.label_en}"

    @property
    def label_en(self):
        return {
            "store_owner": "Store Owner",
            "order_operator": "Order Operator",
            "support_agent": "Support Agent",
            "finance": "Finance",
            "catalog_manager": "Catalog Manager",
            "technical_operator": "Technical Operator",
            "marketing_manager": "Marketing Manager",
            "analyst": "Analyst Read Only",
        }[self.key]


CAPABILITY_LABELS = {
    "dashboard.view": "مشاهده داشبورد",
    "setup.manage": "مدیریت راه اندازی",
    "orders.view": "مشاهده سفارش ها",
    "orders.approve": "تایید سفارش/پرداخت",
    "orders.reject": "رد سفارش/پرداخت",
    "payments.review": "بررسی رسید و پرداخت",
    "services.view": "مشاهده سرویس ها",
    "services.modify": "تغییر وضعیت سرویس",
    "services.send_config": "ارسال مجدد کانفیگ",
    "support.view": "مشاهده پشتیبانی",
    "support.reply": "پاسخ پشتیبانی",
    "catalog.view": "مشاهده کاتالوگ",
    "catalog.manage": "مدیریت کاتالوگ و route",
    "panels.view": "مشاهده پنل و inbound",
    "panels.manage": "مدیریت پنل و inbound",
    "reports.view": "مشاهده گزارش ها",
    "reports.export": "خروجی CSV گزارش ها",
    "campaigns.view": "مشاهده کمپین ها",
    "campaigns.create": "ساخت/ویرایش کمپین",
    "campaigns.queue": "queue/cancel/retry کمپین",
    "revenue.view": "مشاهده Revenue Control",
    "revenue.manage_safe": "تغییر safe mode درآمد",
    "revenue.enable_real_send": "فعال سازی real-send درآمد",
    "backup.view": "مشاهده پشتیبان‌ها",
    "backup.create": "ساخت پشتیبان",
    "backup.download": "دانلود پشتیبان",
    "backup.download_env": "دانلود پشتیبان شامل env",
    "backup.delete": "حذف پشتیبان",
    "restore.upload": "Upload بسته بازیابی",
    "restore.validate": "اعتبارسنجی بسته بازیابی",
    "restore.command": "تولید دستور بازیابی",
    "staff.view": "مشاهده کارکنان",
    "staff.manage": "مدیریت کارکنان و نقش ها",
}


ROLE_PRESETS = {
    "store_owner": StaffRolePreset(
        key="store_owner",
        label="صاحب فروشگاه",
        description=(
            "همه workflowهای محصولی و Staff Access Center را می بیند، اما superuser سیستم "
            "یا permission خام دلخواه مدیریت نمی کند."
        ),
        capabilities=tuple(CAPABILITY_LABELS),
    ),
    "order_operator": StaffRolePreset(
        key="order_operator",
        label="اپراتور سفارش",
        description="رسید، سفارش، مشتری و سرویس مرتبط را برای تایید/رد پرداخت بررسی می کند.",
        capabilities=(
            "dashboard.view",
            "orders.view",
            "orders.approve",
            "orders.reject",
            "payments.review",
            "services.view",
            "support.view",
        ),
    ),
    "support_agent": StaffRolePreset(
        key="support_agent",
        label="پشتیبان",
        description="گفتگوهای پشتیبانی و review مشتری/سرویس را بدون دسترسی مالی یا campaign مدیریت می کند.",
        capabilities=(
            "dashboard.view",
            "support.view",
            "support.reply",
            "services.view",
            "services.send_config",
        ),
    ),
    "finance": StaffRolePreset(
        key="finance",
        label="مالی",
        description="سفارش ها، پرداخت ها و گزارش های مالی را می بیند و رسید را verify/reject می کند.",
        capabilities=(
            "dashboard.view",
            "orders.view",
            "orders.approve",
            "orders.reject",
            "payments.review",
            "reports.view",
            "reports.export",
        ),
    ),
    "catalog_manager": StaffRolePreset(
        key="catalog_manager",
        label="مدیر کاتالوگ",
        description="پلن، route و آمادگی inbound فروش را مدیریت می کند؛ credential پنل نمایش داده نمی شود.",
        capabilities=(
            "dashboard.view",
            "catalog.view",
            "catalog.manage",
            "panels.view",
            "reports.view",
        ),
    ),
    "technical_operator": StaffRolePreset(
        key="technical_operator",
        label="اپراتور فنی",
        description="پنل، inbound، سرویس ها، usage و health را مدیریت می کند؛ دسترسی مالی یا campaign ندارد.",
        capabilities=(
            "dashboard.view",
            "services.view",
            "services.modify",
            "services.send_config",
            "catalog.view",
            "panels.view",
            "panels.manage",
            "backup.view",
            "backup.create",
            "restore.validate",
            "restore.command",
        ),
    ),
    "marketing_manager": StaffRolePreset(
        key="marketing_manager",
        label="مدیر مارکتینگ",
        description="کمپین و گزارش های کلی را می بیند؛ دسترسی خام به targetهای حساس ندارد.",
        capabilities=(
            "dashboard.view",
            "campaigns.view",
            "campaigns.create",
            "campaigns.queue",
            "reports.view",
        ),
    ),
    "analyst": StaffRolePreset(
        key="analyst",
        label="تحلیلگر / فقط خواندنی",
        description="داشبورد و گزارش های خلاصه را بدون action mutation یا export حساس می بیند.",
        capabilities=(
            "dashboard.view",
            "reports.view",
        ),
    ),
}


CAPABILITY_PERMISSIONS = {
    "dashboard.view": ("store.view_store",),
    "setup.manage": (
        "store.view_store",
        "store.change_store",
        "store.view_botconfiguration",
        "store.change_botconfiguration",
        "store.view_panel",
        "store.change_panel",
        "store.view_inbound",
        "store.change_inbound",
        "store.view_plan",
        "store.change_plan",
        "store.view_planinboundroute",
        "store.change_planinboundroute",
    ),
    "orders.view": (
        "store.view_order",
        "store.view_customer",
        "store.view_vpnclient",
    ),
    "orders.approve": ("store.change_order",),
    "orders.reject": ("store.change_order",),
    "payments.review": (
        "store.view_order",
        "store.change_order",
        "payments.view_incomingpaymentsms",
        "payments.change_incomingpaymentsms",
    ),
    "services.view": (
        "store.view_vpnclient",
        "store.view_customer",
        "store.view_order",
    ),
    "services.modify": ("store.change_vpnclient",),
    "services.send_config": ("store.view_vpnclient",),
    "support.view": (
        "store.view_supportconversation",
        "store.view_supportmessage",
        "store.view_customer",
    ),
    "support.reply": (
        "store.change_supportconversation",
        "store.add_supportmessage",
        "store.view_supportmessage",
    ),
    "catalog.view": (
        "store.view_plan",
        "store.view_planinboundroute",
        "store.view_inbound",
        "store.view_panel",
    ),
    "catalog.manage": (
        "store.add_plan",
        "store.change_plan",
        "store.add_planinboundroute",
        "store.change_planinboundroute",
        "store.view_inbound",
        "store.change_inbound",
    ),
    "panels.view": (
        "store.view_panel",
        "store.view_inbound",
        "store.view_panelhealthstatus",
        "store.view_panelhealthchecklog",
        "store.view_panelusagesnapshot",
        "store.view_panelclientusagesnapshot",
        "store.view_paneldailyusage",
    ),
    "panels.manage": (
        "store.change_panel",
        "store.change_inbound",
        "store.view_panel",
        "store.view_inbound",
    ),
    "reports.view": (
        "store.view_order",
        "store.view_customer",
        "store.view_vpnclient",
        "store.view_revenueofferlog",
        "store.view_broadcastmessage",
        "store.view_broadcastrecipient",
    ),
    "reports.export": (
        "store.view_order",
        "store.view_customer",
        "store.view_vpnclient",
        "store.view_revenueofferlog",
    ),
    "campaigns.view": (
        "store.view_broadcastmessage",
        "store.view_broadcastrecipient",
    ),
    "campaigns.create": (
        "store.add_broadcastmessage",
        "store.change_broadcastmessage",
        "store.view_broadcastmessage",
    ),
    "campaigns.queue": (
        "store.change_broadcastmessage",
        "store.add_broadcastrecipient",
        "store.change_broadcastrecipient",
    ),
    "revenue.view": (
        "store.view_store",
        "store.view_revenueofferlog",
    ),
    "revenue.manage_safe": ("store.change_store",),
    "revenue.enable_real_send": ("store.change_store",),
    "backup.view": ("store.view_qasedakbackupjob",),
    "backup.create": ("store.add_qasedakbackupjob",),
    "backup.download": ("store.view_qasedakbackupjob",),
    "backup.download_env": ("store.view_qasedakbackupjob",),
    "backup.delete": ("store.delete_qasedakbackupjob",),
    "restore.upload": ("store.add_qasedakrestorejob",),
    "restore.validate": ("store.change_qasedakrestorejob",),
    "restore.command": ("store.view_qasedakrestorejob", "store.change_qasedakrestorejob"),
    "staff.view": (
        "auth.view_user",
        "auth.view_group",
    ),
    "staff.manage": ("auth.view_user",),
}


CAPABILITY_SECTIONS = {
    "dashboard": "dashboard.view",
    "setup": "setup.manage",
    "orders": "orders.view",
    "services": "services.view",
    "support": "support.view",
    "catalog": "catalog.view",
    "reports": "reports.view",
    "campaigns": "campaigns.view",
    "revenue": "revenue.view",
    "backups": "backup.view",
    "staff": "staff.manage",
}


SENSITIVE_DETAIL_KEYS = (
    "password",
    "token",
    "secret",
    "card",
    "config",
    "link",
    "uuid",
    "permission",
)


def get_staff_role_presets():
    return ROLE_PRESETS.copy()


def get_all_capabilities():
    return tuple(CAPABILITY_LABELS)


def role_group_name(role_key):
    return ROLE_PRESETS[role_key].group_name


def role_key_from_group_name(group_name):
    for role in ROLE_PRESETS.values():
        if role.group_name == group_name:
            return role.key
    return ""


def get_role_permission_summary(role_key):
    role = ROLE_PRESETS.get(role_key)
    if not role:
        return None
    allowed = list(role.capabilities)
    denied = [capability for capability in CAPABILITY_LABELS if capability not in allowed]
    return {
        "role": role,
        "allowed": [{"key": key, "label": CAPABILITY_LABELS[key]} for key in allowed],
        "denied": [{"key": key, "label": CAPABILITY_LABELS[key]} for key in denied],
        "permission_codenames": sorted(
            {
                permission
                for capability in allowed
                for permission in CAPABILITY_PERMISSIONS.get(capability, ())
            }
        ),
    }


def get_user_product_roles(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []
    group_names = set(user.groups.values_list("name", flat=True))
    return [
        {
            "key": role.key,
            "label": role.label,
            "group_name": role.group_name,
            "description": role.description,
        }
        for role in ROLE_PRESETS.values()
        if role.group_name in group_names
    ]


def get_primary_role_key(user):
    roles = get_user_product_roles(user)
    return roles[0]["key"] if roles else ""


def get_user_capabilities(user):
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    if getattr(user, "is_superuser", False):
        return set(CAPABILITY_LABELS)
    if not getattr(user, "is_active", False) or not getattr(user, "is_staff", False):
        return set()
    capabilities = set()
    role_keys = [role["key"] for role in get_user_product_roles(user)]
    for role_key in role_keys:
        role = ROLE_PRESETS.get(role_key)
        if role:
            capabilities.update(role.capabilities)
    return capabilities


def user_has_capability(user, capability):
    return capability in get_user_capabilities(user)


def user_has_any_capability(user, *capabilities):
    user_capabilities = get_user_capabilities(user)
    return any(capability in user_capabilities for capability in capabilities)


def ensure_admin_capability(user, capability):
    if not user_has_capability(user, capability):
        raise PermissionDenied


def ensure_any_admin_capability(user, *capabilities):
    if not user_has_any_capability(user, *capabilities):
        raise PermissionDenied


def require_admin_capability(capability):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            ensure_admin_capability(request.user, capability)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def require_any_admin_capability(*capabilities):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            ensure_any_admin_capability(request.user, *capabilities)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def get_visible_admin_sections(user):
    section_defs = [
        ("dashboard", "داشبورد قاصدک", "admin_store_owner_dashboard", "dashboard.view"),
        ("setup", "راه اندازی", "admin_store_setup_center", "setup.manage"),
        ("orders", "میز کار سفارش ها", "admin_store_order_workbench", "orders.view"),
        ("services", "میز کار سرویس ها", "admin_store_service_workbench", "services.view"),
        ("support", "میز کار پشتیبانی", "admin_store_support_workbench", "support.view"),
        ("catalog", "محصولات و مسیر فروش", "admin_store_catalog", "catalog.view"),
        ("reports", "گزارش ها", "admin_store_reports_center", "reports.view"),
        ("campaigns", "کمپین ها", "admin_store_campaign_workbench", "campaigns.view"),
        ("revenue", "کنترل درآمد", "admin_store_revenue_control", "revenue.view"),
        ("backups", "پشتیبان‌گیری و انتقال سرور", "admin_store_backup_center", "backup.view"),
        ("staff", "کارکنان و دسترسی ها", "admin_store_staff_access", "staff.manage"),
    ]
    visible = []
    for key, label, url_name, capability in section_defs:
        if user_has_capability(user, capability):
            visible.append(
                {
                    "key": key,
                    "label": label,
                    "url": reverse(url_name),
                    "capability": capability,
                }
            )
    return visible


def can_manage_staff(actor, target=None):
    if not actor or not getattr(actor, "is_authenticated", False):
        return False
    if not getattr(actor, "is_active", False) or not getattr(actor, "is_staff", False):
        return False
    if getattr(actor, "is_superuser", False):
        return True
    if not user_has_capability(actor, "staff.manage"):
        return False
    if target is not None and getattr(target, "is_superuser", False):
        return False
    return True


def can_assign_role(actor, role_key):
    if role_key not in ROLE_PRESETS:
        return False
    return can_manage_staff(actor)


def mask_staff_email(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return text[:2] + "***" if len(text) > 2 else "***"
    local, domain = text.split("@", 1)
    return f"{local[:2]}***@{domain}" if local else f"***@{domain}"


def mask_staff_identifier(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "@" in text:
        return mask_staff_email(text)
    if len(text) <= 6:
        return f"{text[:2]}***"
    return f"{text[:3]}...{text[-2:]}"


def safe_detail_value(key, value):
    key_text = str(key or "").lower()
    if any(marker in key_text for marker in SENSITIVE_DETAIL_KEYS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): safe_detail_value(child_key, child_value) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_detail_value(key, item) for item in value[:20]]
    text = str(value)
    if len(text) > 240:
        return f"{text[:237]}..."
    return text


def safe_details(details):
    if not details:
        return {}
    return {str(key): safe_detail_value(key, value) for key, value in dict(details).items()}


def log_staff_access_change(actor, target, action, details=None):
    payload = {
        "staff_access_action": str(action),
        "details": safe_details(details or {}),
        "at": timezone.now().isoformat(),
    }
    if target is not None:
        payload["target_user_id"] = getattr(target, "pk", None)
        payload["target_username"] = mask_staff_identifier(getattr(target, "username", ""))
    logger.info("staff_access_change %s", json.dumps(payload, ensure_ascii=True, sort_keys=True))

    if not actor or not getattr(actor, "pk", None) or target is None:
        return
    try:
        content_type = ContentType.objects.get_for_model(target, for_concrete_model=False)
        flag = ADDITION if action == "staff.created" else CHANGE
        LogEntry.objects.create(
            user_id=actor.pk,
            content_type=content_type,
            object_id=str(target.pk),
            object_repr=str(target)[:200],
            action_flag=flag,
            change_message=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        )
    except Exception:
        logger.exception("staff_access_logentry_failed action=%s target=%s", action, getattr(target, "pk", None))


def sync_staff_role_presets(*, dry_run=True, apply=False, verbose=False):
    if dry_run and apply:
        raise ValueError("dry_run and apply cannot both be true")

    summary = {
        "dry_run": bool(dry_run),
        "apply": bool(apply),
        "groups_created": 0,
        "groups_updated": 0,
        "permissions_added": 0,
        "warnings": [],
        "roles": [],
    }
    permission_cache = {}

    def resolve_permission(label):
        if label in permission_cache:
            return permission_cache[label]
        try:
            app_label, codename = label.split(".", 1)
        except ValueError:
            summary["warnings"].append(f"invalid_permission_label={label}")
            permission_cache[label] = None
            return None
        permission = Permission.objects.filter(
            content_type__app_label=app_label,
            codename=codename,
        ).first()
        if not permission:
            summary["warnings"].append(f"missing_permission={label}")
        permission_cache[label] = permission
        return permission

    for role in ROLE_PRESETS.values():
        permission_labels = sorted(
            {
                permission_label
                for capability in role.capabilities
                for permission_label in CAPABILITY_PERMISSIONS.get(capability, ())
            }
        )
        permissions = [permission for permission in (resolve_permission(label) for label in permission_labels) if permission]
        group = Group.objects.filter(name=role.group_name).first()
        created = False
        if not group:
            created = True
            if apply:
                group = Group.objects.create(name=role.group_name)
            summary["groups_created"] += 1
        existing_ids = set(group.permissions.values_list("id", flat=True)) if group else set()
        to_add = [permission for permission in permissions if permission.pk not in existing_ids]
        if to_add:
            summary["permissions_added"] += len(to_add)
            if not created:
                summary["groups_updated"] += 1
            if apply and group:
                group.permissions.add(*to_add)
        role_summary = {
            "key": role.key,
            "group": role.group_name,
            "created": created,
            "permissions_expected": len(permission_labels),
            "permissions_added": len(to_add),
        }
        if verbose:
            role_summary["capabilities"] = list(role.capabilities)
        summary["roles"].append(role_summary)

    logger.info("staff_role_sync %s", json.dumps(safe_details(summary), ensure_ascii=True, sort_keys=True))
    return summary


def active_staff_manager_count(exclude_user=None):
    User = get_user_model()
    queryset = User.objects.filter(
        is_active=True,
        is_staff=True,
        groups__name=ROLE_PRESETS["store_owner"].group_name,
    )
    if exclude_user is not None and getattr(exclude_user, "pk", None):
        queryset = queryset.exclude(pk=exclude_user.pk)
    return queryset.distinct().count()


def active_superuser_count(exclude_user=None):
    User = get_user_model()
    queryset = User.objects.filter(is_active=True, is_superuser=True)
    if exclude_user is not None and getattr(exclude_user, "pk", None):
        queryset = queryset.exclude(pk=exclude_user.pk)
    return queryset.count()
