from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.crypto import get_random_string

from .admin_access import (
    ROLE_GROUP_PREFIX,
    ROLE_PRESETS,
    active_staff_manager_count,
    active_superuser_count,
    can_assign_role,
    can_manage_staff,
    get_primary_role_key,
    get_role_permission_summary,
    get_staff_role_presets,
    get_user_product_roles,
    log_staff_access_change,
    mask_staff_email,
    require_admin_capability,
    role_group_name,
    role_key_from_group_name,
)


User = get_user_model()


def staff_access_url():
    return reverse("admin_store_staff_access")


def staff_new_url():
    return reverse("admin_store_staff_new")


def staff_review_url(user):
    return reverse("admin_store_staff_review", args=[user.pk])


def staff_edit_url(user):
    return reverse("admin_store_staff_edit", args=[user.pk])


def staff_password_url(user):
    return reverse("admin_store_staff_password", args=[user.pk])


def staff_roles_url():
    return reverse("admin_store_staff_roles")


def staff_role_detail_url(role_key):
    return reverse("admin_store_staff_role_detail", args=[role_key])


def generated_password():
    return get_random_string(18, "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%")


def role_choices(actor):
    choices = []
    for role in get_staff_role_presets().values():
        if can_assign_role(actor, role.key):
            choices.append((role.key, role.label))
    return choices


class StaffCreateForm(forms.ModelForm):
    role_key = forms.ChoiceField(label="نقش آماده")
    password_mode = forms.ChoiceField(
        label="رمز عبور",
        choices=(("generated", "ساخت رمز موقت"), ("manual", "ورود دستی")),
        initial="generated",
        widget=forms.RadioSelect,
    )
    password1 = forms.CharField(
        label="رمز عبور دستی",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="تکرار رمز عبور دستی",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "is_active")
        labels = {
            "username": "نام کاربری",
            "email": "ایمیل اختیاری",
            "first_name": "نام",
            "last_name": "نام خانوادگی",
            "is_active": "فعال باشد",
        }

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.fields["role_key"].choices = role_choices(actor)
        self.fields["is_active"].initial = True

    def clean_role_key(self):
        role_key = self.cleaned_data["role_key"]
        if not can_assign_role(self.actor, role_key):
            raise ValidationError("شما اجازه تخصیص این نقش را ندارید.")
        return role_key

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("password_mode")
        password1 = cleaned.get("password1") or ""
        password2 = cleaned.get("password2") or ""
        if mode == "manual":
            if not password1:
                self.add_error("password1", "رمز عبور دستی لازم است.")
            if password1 != password2:
                self.add_error("password2", "تکرار رمز عبور مطابق نیست.")
            if password1 and password1 == password2:
                password_validation.validate_password(password1, user=self.instance)
        return cleaned


class StaffEditForm(forms.ModelForm):
    role_key = forms.ChoiceField(label="نقش آماده")

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "is_active")
        labels = {
            "username": "نام کاربری",
            "email": "ایمیل اختیاری",
            "first_name": "نام",
            "last_name": "نام خانوادگی",
            "is_active": "فعال باشد",
        }

    def __init__(self, *args, actor=None, target=None, **kwargs):
        self.actor = actor
        self.target = target or kwargs.get("instance")
        super().__init__(*args, **kwargs)
        self.fields["role_key"].choices = role_choices(actor)
        self.fields["role_key"].initial = get_primary_role_key(self.target)
        if self.target and self.target.is_superuser and not actor.is_superuser:
            self.fields["is_active"].disabled = True
            self.fields["role_key"].disabled = True

    def clean_role_key(self):
        role_key = self.cleaned_data["role_key"]
        if not can_assign_role(self.actor, role_key):
            raise ValidationError("شما اجازه تخصیص این نقش را ندارید.")
        return role_key

    def clean(self):
        cleaned = super().clean()
        target = self.target
        actor = self.actor
        new_active = bool(cleaned.get("is_active"))
        new_role = cleaned.get("role_key")
        old_role = get_primary_role_key(target)

        if target and target.pk == actor.pk and not actor.is_superuser:
            if new_active != target.is_active or new_role != old_role:
                raise ValidationError("برای جلوگیری از lock-out، تغییر active/role روی حساب خودتان مجاز نیست.")

        if target and target.is_superuser and target.is_active and not new_active:
            if active_superuser_count(exclude_user=target) == 0:
                raise ValidationError("آخرین superuser فعال نباید غیرفعال شود.")

        if target and target.is_active and old_role == "store_owner":
            demoting_owner = new_role != "store_owner" or not new_active
            if demoting_owner and active_staff_manager_count(exclude_user=target) == 0:
                raise ValidationError("آخرین Store Owner فعال نباید غیرفعال یا demote شود.")
        return cleaned


class StaffPasswordForm(forms.Form):
    password_mode = forms.ChoiceField(
        label="رمز عبور",
        choices=(("generated", "ساخت رمز موقت"), ("manual", "ورود دستی")),
        initial="generated",
        widget=forms.RadioSelect,
    )
    password1 = forms.CharField(
        label="رمز عبور دستی",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="تکرار رمز عبور دستی",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    confirmation = forms.CharField(label="برای تایید RESET را وارد کن", required=True)

    def __init__(self, *args, target=None, **kwargs):
        self.target = target
        super().__init__(*args, **kwargs)

    def clean_confirmation(self):
        confirmation = (self.cleaned_data.get("confirmation") or "").strip()
        if confirmation != "RESET":
            raise ValidationError("برای reset باید RESET را دقیق وارد کنی.")
        return confirmation

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("password_mode")
        password1 = cleaned.get("password1") or ""
        password2 = cleaned.get("password2") or ""
        if mode == "manual":
            if not password1:
                self.add_error("password1", "رمز عبور دستی لازم است.")
            if password1 != password2:
                self.add_error("password2", "تکرار رمز عبور مطابق نیست.")
            if password1 and password1 == password2:
                password_validation.validate_password(password1, user=self.target)
        return cleaned


def assign_product_role(user, role_key):
    role_group_names = [role.group_name for role in ROLE_PRESETS.values()]
    user.groups.remove(*Group.objects.filter(name__in=role_group_names))
    if role_key:
        group, _created = Group.objects.get_or_create(name=role_group_name(role_key))
        user.groups.add(group)


def staff_display_name(user):
    full_name = user.get_full_name().strip()
    return full_name or user.username


def staff_status(user):
    if not user.is_active:
        return "inactive"
    if not user.last_login:
        return "never logged in"
    return "active"


def staff_row(user):
    roles = get_user_product_roles(user)
    return {
        "user": user,
        "username": user.username,
        "display_name": staff_display_name(user),
        "email_masked": mask_staff_email(user.email),
        "roles": roles,
        "role_label": roles[0]["label"] if roles else "بدون نقش",
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "last_login": user.last_login,
        "status": staff_status(user),
        "review_url": staff_review_url(user),
        "edit_url": staff_edit_url(user),
    }


def staff_warnings():
    role_group_names = [role.group_name for role in ROLE_PRESETS.values()]
    staff_users = User.objects.filter(is_staff=True).prefetch_related("groups", "user_permissions").order_by("username")
    without_role = []
    unexpected_permissions = []
    for user in staff_users:
        if user.is_superuser:
            continue
        group_names = {group.name for group in user.groups.all()}
        if user.is_staff and not (group_names & set(role_group_names)):
            without_role.append(user)
        unexpected_groups = [name for name in group_names if name not in role_group_names]
        if unexpected_groups or user.user_permissions.exists():
            unexpected_permissions.append(
                {
                    "user": user,
                    "group_count": len(unexpected_groups),
                    "direct_permission_count": user.user_permissions.count(),
                }
            )
    return {
        "without_role": without_role,
        "unexpected_permissions": unexpected_permissions,
        "superuser_count": User.objects.filter(is_superuser=True).count(),
        "active_superuser_count": User.objects.filter(is_superuser=True, is_active=True).count(),
    }


def audit_entries(limit=20):
    queryset = LogEntry.objects.select_related("user", "content_type").filter(
        change_message__contains="staff_access_action"
    )
    return list(queryset.order_by("-action_time", "-pk")[:limit])


@require_admin_capability("staff.manage")
def staff_access_center(request):
    if not can_manage_staff(request.user):
        raise PermissionDenied
    users = list(
        User.objects.filter(is_staff=True)
        .prefetch_related("groups", "user_permissions")
        .order_by("-is_superuser", "-is_active", "username")
    )
    warnings = staff_warnings()
    context = {
        **admin.site.each_context(request),
        "title": "کارکنان و دسترسی ها",
        "subtitle": "نقش های آماده، وضعیت کارکنان و audit تغییرات دسترسی.",
        "stats": {
            "active_staff": User.objects.filter(is_staff=True, is_active=True).count(),
            "inactive_staff": User.objects.filter(is_staff=True, is_active=False).count(),
            "never_logged_in": User.objects.filter(is_staff=True, last_login__isnull=True).count(),
            "superuser_count": warnings["superuser_count"],
        },
        "rows": [staff_row(user) for user in users],
        "warnings": warnings,
        "roles_url": staff_roles_url(),
        "new_url": staff_new_url(),
        "audit_entries": audit_entries(limit=10),
    }
    return TemplateResponse(request, "admin/store/staff/workbench.html", context)


@require_admin_capability("staff.manage")
def staff_create(request):
    if not can_manage_staff(request.user):
        raise PermissionDenied
    form = StaffCreateForm(request.POST or None, actor=request.user)
    generated = ""
    created_user = None
    if request.method == "POST":
        if form.is_valid():
            password = form.cleaned_data.get("password1") if form.cleaned_data["password_mode"] == "manual" else generated_password()
            with transaction.atomic():
                created_user = form.save(commit=False)
                created_user.is_staff = True
                created_user.is_superuser = False
                created_user.set_password(password)
                created_user.save()
                assign_product_role(created_user, form.cleaned_data["role_key"])
            log_staff_access_change(
                request.user,
                created_user,
                "staff.created",
                {"role": form.cleaned_data["role_key"], "active": created_user.is_active},
            )
            messages.success(request, "کارمند جدید ساخته شد.")
            if form.cleaned_data["password_mode"] == "generated":
                generated = password
            else:
                return redirect(staff_review_url(created_user))
        else:
            messages.error(request, "لطفاً خطاهای فرم را بررسی کن.")
    context = {
        **admin.site.each_context(request),
        "title": "افزودن کارمند",
        "subtitle": "ساخت کارمند staff با نقش آماده؛ superuser از این مسیر ساخته نمی شود.",
        "form": form,
        "mode": "create",
        "generated_password": generated,
        "created_user": created_user,
        "back_url": staff_access_url(),
        "force_password_change_note": "زیرساخت force password change در پروژه فعلی وجود ندارد؛ در اولین ورود باید رمز را دستی تغییر دهند.",
    }
    return TemplateResponse(request, "admin/store/staff/form.html", context)


@require_admin_capability("staff.manage")
def staff_review(request, user_id):
    target = get_object_or_404(User.objects.prefetch_related("groups", "user_permissions"), pk=user_id)
    if not can_manage_staff(request.user, target):
        raise PermissionDenied
    context = {
        **admin.site.each_context(request),
        "title": f"بررسی کارمند {target.username}",
        "subtitle": "نمای امن بدون password، token یا permission خام.",
        "target": target,
        "row": staff_row(target),
        "roles": get_user_product_roles(target),
        "edit_url": staff_edit_url(target),
        "password_url": staff_password_url(target),
        "back_url": staff_access_url(),
        "audit_entries": audit_entries(limit=10),
    }
    return TemplateResponse(request, "admin/store/staff/review.html", context)


@require_admin_capability("staff.manage")
def staff_edit(request, user_id):
    target = get_object_or_404(User.objects.prefetch_related("groups", "user_permissions"), pk=user_id)
    if not can_manage_staff(request.user, target):
        raise PermissionDenied
    form = StaffEditForm(request.POST or None, instance=target, actor=request.user, target=target)
    if request.method == "POST":
        old_active = target.is_active
        old_role = get_primary_role_key(target)
        if form.is_valid():
            with transaction.atomic():
                saved = form.save(commit=False)
                saved.is_staff = True
                if not request.user.is_superuser:
                    saved.is_superuser = target.is_superuser
                saved.save()
                assign_product_role(saved, form.cleaned_data["role_key"])
            if old_role != form.cleaned_data["role_key"]:
                log_staff_access_change(
                    request.user,
                    target,
                    "role.changed",
                    {"from": old_role, "to": form.cleaned_data["role_key"]},
                )
            if old_active != target.is_active:
                log_staff_access_change(
                    request.user,
                    target,
                    "user.activated" if target.is_active else "user.deactivated",
                    {"active": target.is_active},
                )
            messages.success(request, "اطلاعات کارمند ذخیره شد.")
            return redirect(staff_review_url(target))
        messages.error(request, "لطفاً خطاهای فرم را بررسی کن.")
    context = {
        **admin.site.each_context(request),
        "title": f"ویرایش کارمند {target.username}",
        "subtitle": "ویرایش profile و role preset؛ permission خام نمایش داده نمی شود.",
        "form": form,
        "mode": "edit",
        "target": target,
        "back_url": staff_review_url(target),
        "force_password_change_note": "زیرساخت force password change در پروژه فعلی وجود ندارد.",
    }
    return TemplateResponse(request, "admin/store/staff/form.html", context)


@require_admin_capability("staff.manage")
def staff_password(request, user_id):
    target = get_object_or_404(User, pk=user_id)
    if not can_manage_staff(request.user, target):
        raise PermissionDenied
    if target.pk == request.user.pk and not request.user.is_superuser:
        raise PermissionDenied
    form = StaffPasswordForm(request.POST or None, target=target)
    generated = ""
    if request.method == "POST":
        if form.is_valid():
            password = form.cleaned_data.get("password1") if form.cleaned_data["password_mode"] == "manual" else generated_password()
            target.set_password(password)
            target.save(update_fields=["password"])
            log_staff_access_change(
                request.user,
                target,
                "password.reset",
                {"mode": form.cleaned_data["password_mode"]},
            )
            messages.success(request, "رمز عبور reset شد.")
            if form.cleaned_data["password_mode"] == "generated":
                generated = password
            else:
                return redirect(staff_review_url(target))
        else:
            messages.error(request, "لطفاً خطاهای فرم را بررسی کن.")
    context = {
        **admin.site.each_context(request),
        "title": f"Reset password برای {target.username}",
        "subtitle": "رمز هرگز log یا در URL ذخیره نمی شود؛ رمز generated فقط همین یک بار نمایش داده می شود.",
        "target": target,
        "form": form,
        "generated_password": generated,
        "back_url": staff_review_url(target),
    }
    return TemplateResponse(request, "admin/store/staff/password.html", context)


@require_admin_capability("staff.manage")
def staff_roles(request):
    role_counts = {
        role_key_from_group_name(row["groups__name"]): row["count"]
        for row in User.objects.filter(groups__name__startswith=ROLE_GROUP_PREFIX).values("groups__name").annotate(count=Count("id"))
    }
    roles = []
    for role in get_staff_role_presets().values():
        summary = get_role_permission_summary(role.key)
        roles.append(
            {
                "role": role,
                "allowed": summary["allowed"],
                "denied": summary["denied"],
                "user_count": role_counts.get(role.key, 0),
                "url": staff_role_detail_url(role.key),
            }
        )
    context = {
        **admin.site.each_context(request),
        "title": "نقش های آماده",
        "subtitle": "این صفحه فقط توضیح role presetهاست؛ تغییر source of truth از کد و sync command انجام می شود.",
        "roles": roles,
        "back_url": staff_access_url(),
    }
    return TemplateResponse(request, "admin/store/staff/roles.html", context)


@require_admin_capability("staff.manage")
def staff_role_detail(request, role_key):
    summary = get_role_permission_summary(role_key)
    if not summary:
        raise PermissionDenied
    users = User.objects.filter(groups__name=role_group_name(role_key)).order_by("username")
    context = {
        **admin.site.each_context(request),
        "title": summary["role"].label,
        "subtitle": summary["role"].description,
        "role": summary["role"],
        "allowed": summary["allowed"],
        "denied": summary["denied"],
        "users": users,
        "back_url": staff_roles_url(),
    }
    return TemplateResponse(request, "admin/store/staff/role_detail.html", context)
