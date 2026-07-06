from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from urllib.parse import quote

from .admin_access import ensure_admin_capability, require_admin_capability, user_has_capability
from .backup_restore_services import (
    BackupValidationError,
    create_backup_job,
    create_restore_job,
    ensure_path_under,
    generate_restore_command,
    get_private_backup_root,
    get_private_restore_upload_root,
    log_backup_restore_action,
    mark_restore_command_generated,
    restore_confirmation_phrase,
    run_backup_job,
    validate_restore_job,
)
from .models import QasedakBackupJob, QasedakRestoreJob


BACKUP_CENTER_TITLE = "پشتیبان‌گیری و انتقال سرور"


class BackupCreateForm(forms.Form):
    backup_type = forms.ChoiceField(
        label="نوع پشتیبان",
        choices=QasedakBackupJob.BackupType.choices,
        initial=QasedakBackupJob.BackupType.DB_ONLY,
        widget=forms.RadioSelect,
    )
    include_media = forms.BooleanField(label="include media", required=False)
    include_env = forms.BooleanField(label="include env", required=False)
    include_system = forms.BooleanField(label="include system reference", required=False)
    include_logs = forms.BooleanField(label="include logs", required=False, disabled=True)

    def clean_include_logs(self):
        if self.cleaned_data.get("include_logs"):
            raise ValidationError("Include logs is disabled in P1.")
        return False


class RestoreUploadForm(forms.Form):
    backup_file = forms.FileField(label="فایل پشتیبان (.tar.gz)")


class BackupDeleteForm(forms.Form):
    confirmation = forms.CharField(label="تایید حذف")

    def __init__(self, *args, job=None, **kwargs):
        self.job = job
        super().__init__(*args, **kwargs)

    def clean_confirmation(self):
        value = (self.cleaned_data.get("confirmation") or "").strip()
        expected = f"DELETE_QASEDAK_BACKUP_{self.job.pk}"
        if value != expected:
            raise ValidationError(f"برای حذف باید دقیقاً {expected} را وارد کنی.")
        return value


class RestoreCommandForm(forms.Form):
    confirmation = forms.CharField(label="تایید تولید دستور")
    include_media = forms.BooleanField(label="restore media", required=False)
    restore_env = forms.BooleanField(label="restore env", required=False)

    def __init__(self, *args, job=None, **kwargs):
        self.job = job
        super().__init__(*args, **kwargs)

    def clean_confirmation(self):
        value = (self.cleaned_data.get("confirmation") or "").strip()
        expected = restore_confirmation_phrase(self.job.pk)
        if value != expected:
            raise ValidationError(f"برای تولید دستور باید دقیقاً {expected} را وارد کنی.")
        return value


def backup_center_url():
    return reverse("admin_store_backup_center")


@require_admin_capability("backup.view")
def backup_center(request):
    context = {
        **admin.site.each_context(request),
        "title": BACKUP_CENTER_TITLE,
        "subtitle": "ساخت، دانلود، اعتبارسنجی و آماده‌سازی دستور انتقال امن سرور.",
        "backup_form": BackupCreateForm(),
        "restore_upload_form": RestoreUploadForm(),
        "backup_jobs": QasedakBackupJob.objects.select_related("created_by").order_by("-created_at")[:20],
        "restore_jobs": QasedakRestoreJob.objects.select_related("created_by").order_by("-created_at")[:20],
        "can_create_backup": user_has_capability(request.user, "backup.create"),
        "can_upload_restore": user_has_capability(request.user, "restore.upload"),
        "can_download_backup": user_has_capability(request.user, "backup.download"),
        "can_delete_backup": user_has_capability(request.user, "backup.delete"),
        "can_validate_restore": user_has_capability(request.user, "restore.validate"),
        "can_generate_restore_command": user_has_capability(request.user, "restore.command"),
    }
    return TemplateResponse(request, "admin/store/backups/index.html", context)


@require_admin_capability("backup.create")
def backup_create(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = BackupCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "گزینه‌های پشتیبان معتبر نیست.")
        return redirect(backup_center_url())
    includes_env = (
        form.cleaned_data["include_env"]
        or form.cleaned_data["backup_type"] == QasedakBackupJob.BackupType.FULL_TRANSFER
    )
    if includes_env and not (request.user.is_superuser or user_has_capability(request.user, "backup.download_env")):
        raise PermissionDenied
    job = create_backup_job(
        request.user,
        form.cleaned_data["backup_type"],
        {
            "include_media": form.cleaned_data["include_media"],
            "include_env": form.cleaned_data["include_env"],
            "include_system": form.cleaned_data["include_system"],
        },
    )
    job = run_backup_job(job)
    if job.status == QasedakBackupJob.Status.COMPLETED:
        messages.success(request, f"پشتیبان ساخته شد: {job.file_name}")
    else:
        messages.error(request, "ساخت پشتیبان ناموفق بود؛ خلاصه خطا در Job ذخیره شد.")
    return redirect(backup_center_url())


@require_admin_capability("backup.download")
def backup_download(request, job_id):
    job = get_object_or_404(QasedakBackupJob, pk=job_id)
    if job.status != QasedakBackupJob.Status.COMPLETED or not job.file_path:
        raise Http404("Backup file is not available.")
    if job.includes_env and not (request.user.is_superuser or user_has_capability(request.user, "backup.download_env")):
        raise PermissionDenied
    path = ensure_path_under(job.file_path, get_private_backup_root())
    if not path.exists() or not path.is_file():
        raise Http404("Backup file is missing.")
    log_backup_restore_action(request.user, job, "backup.downloaded", {"file": job.file_name})
    response = FileResponse(path.open("rb"), content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{quote(job.file_name or path.name)}"'
    return response


@require_admin_capability("backup.delete")
def backup_delete(request, job_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    job = get_object_or_404(QasedakBackupJob, pk=job_id)
    form = BackupDeleteForm(request.POST, job=job)
    if not form.is_valid():
        messages.error(request, "تایید حذف معتبر نیست.")
        return redirect(backup_center_url())
    if job.file_path:
        try:
            path = ensure_path_under(job.file_path, get_private_backup_root())
            path.unlink(missing_ok=True)
        except Exception:
            messages.warning(request, "فایل پشتیبان حذف نشد، اما Job علامت حذف خورد.")
    job.status = QasedakBackupJob.Status.DELETED
    job.save(update_fields=["status"])
    log_backup_restore_action(request.user, job, "backup.deleted", {"backup_job_id": job.pk})
    messages.warning(request, "پشتیبان حذف شد.")
    return redirect(backup_center_url())


@require_admin_capability("restore.upload")
def restore_upload(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = RestoreUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "فایل پشتیبان معتبر نیست.")
        return redirect(backup_center_url())
    try:
        job = create_restore_job(request.user, form.cleaned_data["backup_file"])
    except BackupValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(backup_center_url())
    messages.success(request, "فایل پشتیبان Upload شد. حالا Validate را اجرا کن.")
    return redirect("admin_store_restore_detail", restore_id=job.pk)


@require_admin_capability("restore.validate")
def restore_detail(request, restore_id):
    job = get_object_or_404(QasedakRestoreJob, pk=restore_id)
    command_form = RestoreCommandForm(job=job)
    context = {
        **admin.site.each_context(request),
        "title": BACKUP_CENTER_TITLE,
        "subtitle": "Restore از داخل request اجرا نمی‌شود؛ این صفحه فقط اعتبارسنجی و دستور امن تولید می‌کند.",
        "job": job,
        "command_form": command_form,
        "confirmation_phrase": restore_confirmation_phrase(job.pk),
        "can_validate_restore": user_has_capability(request.user, "restore.validate"),
        "can_generate_restore_command": user_has_capability(request.user, "restore.command"),
        "center_url": backup_center_url(),
    }
    return TemplateResponse(request, "admin/store/backups/restore_detail.html", context)


@require_admin_capability("restore.validate")
def restore_validate(request, restore_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    job = get_object_or_404(QasedakRestoreJob, pk=restore_id)
    try:
        validate_restore_job(job, actor=request.user)
    except Exception as exc:
        messages.error(request, f"اعتبارسنجی ناموفق بود: {exc}")
    else:
        messages.success(request, "Backup معتبر است و Restore Plan ساخته شد.")
    return redirect("admin_store_restore_detail", restore_id=job.pk)


@require_admin_capability("restore.command")
def restore_command(request, restore_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    job = get_object_or_404(QasedakRestoreJob, pk=restore_id)
    form = RestoreCommandForm(request.POST, job=job)
    if not form.is_valid():
        messages.error(request, "تایید تولید دستور معتبر نیست.")
        return redirect("admin_store_restore_detail", restore_id=job.pk)
    if form.cleaned_data["restore_env"] and not request.user.is_superuser:
        raise PermissionDenied
    try:
        command = mark_restore_command_generated(
            job,
            actor=request.user,
            options={
                "include_media": form.cleaned_data["include_media"],
                "restore_env": form.cleaned_data["restore_env"],
            },
        )
    except BackupValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "دستور Restore تولید شد؛ آن را فقط در SSH سرور اجرا کن.")
        job.refresh_from_db()
        job.restore_plan["restore_command"] = command
        job.save(update_fields=["restore_plan"])
    return redirect("admin_store_restore_detail", restore_id=job.pk)
