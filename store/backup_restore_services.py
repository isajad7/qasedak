import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.db import connection
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.text import get_valid_filename

from .models import QasedakBackupJob, QasedakRestoreJob, Store


SUPPORTED_BACKUP_VERSIONS = {"1"}
BACKUP_ARCHIVE_SUFFIX = ".tar.gz"
RESTORE_CONFIRM_PREFIX = "RESTORE_QASEDAK_BACKUP_"
SECRET_KEY_RE = re.compile(
    r"(secret|token|password|credential|api[_-]?key|private|webhook|card|uuid|config|sub_?link|phone|email|chat)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(vless|vmess|trojan)://|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"\b\d{6,}:[A-Za-z0-9_-]{16,}\b|(?:\d[ -]?){16,19}"
)


class BackupValidationError(ValidationError):
    pass


def _private_dir(path):
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root.resolve()


def get_private_backup_root():
    return _private_dir(settings.QASEDAK_PRIVATE_BACKUP_ROOT)


def get_private_restore_upload_root():
    return _private_dir(settings.QASEDAK_RESTORE_UPLOAD_ROOT)


def ensure_path_under(path, base):
    resolved = Path(path).resolve()
    base = Path(base).resolve()
    if resolved == base or base in resolved.parents:
        return resolved
    raise SuspiciousFileOperation("Path is outside the private backup directory.")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_scalar(key, value):
    if value in (None, ""):
        return value
    if SECRET_KEY_RE.search(str(key)):
        return "<redacted>"
    text = str(value)
    if SECRET_VALUE_RE.search(text):
        return "<redacted>"
    if len(text) > 240:
        return f"{text[:237]}..."
    return value


def sanitize_manifest(value, key=""):
    if isinstance(value, dict):
        return {str(child_key): sanitize_manifest(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_manifest(item, key) for item in value[:100]]
    return sanitize_scalar(key, value)


def redact_backup_summary(summary):
    return sanitize_manifest(summary or {})


def sanitize_restore_plan(plan):
    return sanitize_manifest(plan or {})


def safe_output(text):
    lines = []
    for line in str(text or "").splitlines()[:80]:
        redacted = re.sub(
            r"(?i)(password|secret|token|credential|api[_-]?key)([=\s:]+)(\S+)",
            r"\1\2<redacted>",
            line,
        )
        lines.append(redacted[:500])
    return "\n".join(lines)


def database_engine_from_connection():
    return "postgres" if connection.vendor == "postgresql" else connection.vendor


def create_backup_job(actor, backup_type, options=None):
    if backup_type not in QasedakBackupJob.BackupType.values:
        raise BackupValidationError("Invalid backup type.")
    options = options or {}
    include_media = bool(options.get("include_media") or backup_type in {
        QasedakBackupJob.BackupType.DB_AND_MEDIA,
        QasedakBackupJob.BackupType.FULL_TRANSFER,
    })
    include_env = bool(options.get("include_env") or backup_type == QasedakBackupJob.BackupType.FULL_TRANSFER)
    include_system = bool(options.get("include_system") or backup_type == QasedakBackupJob.BackupType.FULL_TRANSFER)
    job = QasedakBackupJob.objects.create(
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        backup_type=backup_type,
        includes_media=include_media,
        includes_env=include_env,
        includes_system=include_system,
        safe_summary={
            "requested_options": {
                "include_media": include_media,
                "include_env": include_env,
                "include_system": include_system,
            },
            "database_engine": database_engine_from_connection(),
        },
    )
    log_backup_restore_action(actor, job, "backup.created", {"backup_type": backup_type})
    return job


def run_backup_job(job):
    job.status = QasedakBackupJob.Status.RUNNING
    job.error_message = ""
    job.save(update_fields=["status", "error_message"])
    root = get_private_backup_root()
    command = [
        str(Path(settings.BASE_DIR) / "scripts" / "backup.sh"),
        "--install-dir",
        str(settings.BASE_DIR),
        "--output-dir",
        str(root),
        "--yes",
    ]
    if job.includes_media:
        command.append("--include-media")
    else:
        command.append("--exclude-media")
    if job.includes_env:
        command.append("--include-env")
    else:
        command.append("--exclude-env")
    if job.includes_system:
        command.append("--include-system")
    else:
        command.append("--exclude-system")
    try:
        result = subprocess.run(
            command,
            cwd=str(settings.BASE_DIR),
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
    except Exception as exc:
        job.status = QasedakBackupJob.Status.FAILED
        job.error_message = safe_output(str(exc))
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        log_backup_restore_action(job.created_by, job, "backup.failed", {"error": job.error_message})
        return job

    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        job.status = QasedakBackupJob.Status.FAILED
        job.error_message = safe_output(output)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        log_backup_restore_action(job.created_by, job, "backup.failed", {"error": job.error_message})
        return job

    archive_path = parse_backup_path(output)
    if not archive_path:
        job.status = QasedakBackupJob.Status.FAILED
        job.error_message = "Backup completed but archive path was not reported."
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
        log_backup_restore_action(job.created_by, job, "backup.failed", {"error": job.error_message})
        return job

    archive_path = ensure_path_under(archive_path, root)
    validation = validate_backup_archive(archive_path)
    stat_result = archive_path.stat()
    job.status = QasedakBackupJob.Status.COMPLETED
    job.file_path = str(archive_path)
    job.file_name = archive_path.name
    job.file_size = stat_result.st_size
    job.sha256 = sha256_file(archive_path)
    job.includes_media = bool(validation.get("includes_media"))
    job.includes_env = bool(validation.get("includes_env"))
    job.includes_system = bool(validation.get("includes_system"))
    job.safe_summary = redact_backup_summary(
        {
            "archive": archive_path.name,
            "database_engine": validation.get("database_engine"),
            "database_vendor": validation.get("database_vendor"),
            "backup_type": validation.get("backup_type"),
            "includes_media": validation.get("includes_media"),
            "includes_env": validation.get("includes_env"),
            "includes_system": validation.get("includes_system"),
            "warnings": validation.get("warnings", []),
            "script_output": safe_output(output),
        }
    )
    job.completed_at = timezone.now()
    job.save()
    log_backup_restore_action(job.created_by, job, "backup.completed", {"file": job.file_name, "sha256": job.sha256})
    return job


def parse_backup_path(output):
    for line in str(output or "").splitlines():
        if line.startswith("Backup created:"):
            return line.split(":", 1)[1].strip()
    return ""


def build_backup_manifest(job, options=None):
    options = options or {}
    return sanitize_manifest(
        {
            "qasedak_backup_version": "1",
            "created_at": timezone.now().isoformat(),
            "app_version": "",
            "git_commit": git_commit(),
            "django_settings_module": os.environ.get("DJANGO_SETTINGS_MODULE", ""),
            "database_engine": database_engine_from_connection(),
            "database_vendor": connection.vendor,
            "backup_type": job.backup_type if job else options.get("backup_type", "db_only"),
            "includes_media": bool(options.get("include_media")),
            "includes_env": bool(options.get("include_env")),
            "includes_system": bool(options.get("include_system")),
            "created_by": getattr(getattr(job, "created_by", None), "username", "") if job else "",
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "install_dir": str(settings.BASE_DIR),
            "redaction_policy": "secrets and direct identifiers are redacted from metadata",
        }
    )


def git_commit():
    try:
        result = subprocess.run(
            ["git", "-C", str(settings.BASE_DIR), "rev-parse", "--short", "HEAD"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def collect_media_files(options=None):
    media_root = Path(settings.MEDIA_ROOT)
    if not media_root.exists():
        return []
    return [path for path in media_root.rglob("*") if path.is_file()]


def write_checksums(staging_dir):
    staging_dir = Path(staging_dir)
    checksum_path = staging_dir / "checksums.sha256"
    lines = []
    for path in sorted(staging_dir.rglob("*")):
        if not path.is_file() or path.is_symlink() or path == checksum_path:
            continue
        rel = path.relative_to(staging_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    checksum_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return checksum_path


def create_archive(staging_dir, output_path):
    staging_dir = Path(staging_dir)
    output_path = Path(output_path)
    with tarfile.open(output_path, "w:gz") as tar:
        for path in sorted(staging_dir.rglob("*")):
            tar.add(path, arcname=path.relative_to(staging_dir).as_posix(), recursive=False)
    return output_path


def create_restore_job(actor, uploaded_file):
    if not uploaded_file:
        raise BackupValidationError("Backup file is required.")
    original_name = get_valid_filename(Path(uploaded_file.name).name)
    if not original_name.endswith(BACKUP_ARCHIVE_SUFFIX):
        raise BackupValidationError("Only .tar.gz Qasedak backup packages are accepted.")
    max_size = int(settings.QASEDAK_BACKUP_MAX_UPLOAD_SIZE)
    if getattr(uploaded_file, "size", 0) and uploaded_file.size > max_size:
        raise BackupValidationError("Uploaded backup is too large.")

    root = get_private_restore_upload_root()
    target_name = f"restore-{timezone.now():%Y%m%d-%H%M%S}-{get_random_string(8)}-{original_name}"
    target = ensure_path_under(root / target_name, root)
    with target.open("wb") as handle:
        size = 0
        for chunk in uploaded_file.chunks():
            size += len(chunk)
            if size > max_size:
                target.unlink(missing_ok=True)
                raise BackupValidationError("Uploaded backup is too large.")
            handle.write(chunk)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    job = QasedakRestoreJob.objects.create(
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        uploaded_file_path=str(target),
        uploaded_file_name=original_name,
        uploaded_sha256=sha256_file(target),
        status=QasedakRestoreJob.Status.UPLOADED,
    )
    log_backup_restore_action(actor, job, "restore.uploaded", {"file": original_name, "sha256": job.uploaded_sha256})
    return job


def detect_path_traversal(member):
    name = str(getattr(member, "name", member))
    path = PurePosixPath(name)
    return path.is_absolute() or any(part in {"..", ""} for part in path.parts)


def safe_extract_archive(path, target_dir):
    path = Path(path)
    target_dir = Path(target_dir)
    total_size = 0
    max_size = int(settings.QASEDAK_BACKUP_MAX_EXTRACTED_SIZE)
    try:
        tar = tarfile.open(path, "r:gz")
    except tarfile.TarError as exc:
        raise BackupValidationError("Backup archive is not a valid .tar.gz file.") from exc
    with tar:
        members = tar.getmembers()
        for member in members:
            if detect_path_traversal(member):
                raise BackupValidationError("Archive contains an unsafe path.")
            mode = member.mode or 0
            if member.issym() or member.islnk():
                raise BackupValidationError("Archive contains a link entry, which is not allowed.")
            if member.isdev() or stat.S_ISFIFO(mode):
                raise BackupValidationError("Archive contains a special file, which is not allowed.")
            if member.isfile():
                total_size += int(member.size or 0)
                if total_size > max_size:
                    raise BackupValidationError("Archive extracted size exceeds the configured limit.")
        tar.extractall(target_dir, members=members)
    return target_dir


def backup_payload_root(extracted_dir):
    extracted_dir = Path(extracted_dir)
    if (extracted_dir / "manifest.json").exists():
        return extracted_dir
    children = [path for path in extracted_dir.iterdir() if path.is_dir()]
    for child in children:
        if (child / "manifest.json").exists():
            return child
    raise BackupValidationError("Backup manifest.json is missing.")


def read_manifest(extracted_dir):
    root = backup_payload_root(extracted_dir)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackupValidationError("Backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise BackupValidationError("Backup manifest must be a JSON object.")
    return manifest


def checksums_from_file(root):
    path = Path(root) / "checksums.sha256"
    if not path.exists():
        return None
    checksums = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            digest, rel = line.split(None, 1)
        except ValueError as exc:
            raise BackupValidationError("checksums.sha256 contains an invalid line.") from exc
        rel = rel.strip().lstrip("*")
        if detect_path_traversal(rel):
            raise BackupValidationError("checksums.sha256 contains an unsafe path.")
        checksums[rel] = digest
    return checksums


def checksums_from_manifest(manifest):
    checksums = manifest.get("checksums") or manifest.get("file_checksums_sha256")
    return checksums if isinstance(checksums, dict) else None


def verify_checksums(extracted_dir):
    root = backup_payload_root(extracted_dir)
    manifest = read_manifest(root)
    checksums = checksums_from_file(root)
    source = "checksums.sha256"
    if checksums is None:
        checksums = checksums_from_manifest(manifest)
        source = "manifest"
    if not checksums:
        raise BackupValidationError("Backup checksums are missing.")
    for rel, expected in checksums.items():
        if detect_path_traversal(rel):
            raise BackupValidationError("Backup checksum path is unsafe.")
        target = root / rel
        if not target.exists() or not target.is_file() or target.is_symlink():
            raise BackupValidationError(f"Checksum target is missing or unsafe: {rel}")
        actual = sha256_file(target)
        if actual.lower() != str(expected).lower():
            raise BackupValidationError(f"Checksum mismatch for {rel}.")
    return {"verified_files": len(checksums), "checksum_source": source}


def manifest_database_engine(manifest):
    engine = manifest.get("database_engine")
    if not engine:
        database = manifest.get("database") or {}
        engine = database.get("engine")
    engine = str(engine or "").lower()
    if engine in {"postgresql", "postgres"}:
        return "postgres"
    if engine in {"sqlite", "sqlite3"}:
        return "sqlite"
    return engine or "unknown"


def validate_backup_archive(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise BackupValidationError("Backup archive was not found.")
    if path.stat().st_size > int(settings.QASEDAK_BACKUP_MAX_UPLOAD_SIZE):
        raise BackupValidationError("Backup archive exceeds the configured size limit.")

    temp_parent = get_private_restore_upload_root() / "validation"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="extract-", dir=temp_parent))
    try:
        safe_extract_archive(path, temp_dir)
        root = backup_payload_root(temp_dir)
        manifest = read_manifest(root)
        checksum_summary = verify_checksums(root)
        warnings = []
        version = str(manifest.get("qasedak_backup_version") or "")
        if version and version not in SUPPORTED_BACKUP_VERSIONS:
            raise BackupValidationError("Unsupported Qasedak backup version.")
        if not version:
            warnings.append("Legacy backup manifest without qasedak_backup_version.")
        engine = manifest_database_engine(manifest)
        db_dir = root / "database"
        if engine == "postgres":
            dump = db_dir / "db.postgres.dump"
            if not dump.exists() or not dump.is_file():
                raise BackupValidationError("PostgreSQL backup is missing database/db.postgres.dump.")
            if shutil.which("pg_restore") is None:
                raise BackupValidationError("pg_restore is not available on this server.")
        elif engine == "sqlite":
            sqlite_path = db_dir / "db.sqlite3"
            if not sqlite_path.exists() or not sqlite_path.is_file():
                raise BackupValidationError("SQLite backup is missing database/db.sqlite3.")
            with sqlite_path.open("rb") as handle:
                if not handle.read(16).startswith(b"SQLite format 3"):
                    raise BackupValidationError("database/db.sqlite3 is not a valid SQLite file.")
            if connection.vendor == "postgresql":
                warnings.append("SQLite backup uploaded while production target is PostgreSQL; migration may be required.")
        else:
            raise BackupValidationError("Backup database engine is unsupported or missing.")

        files = [p for p in root.rglob("*") if p.is_file()]
        summary = {
            "valid": True,
            "manifest": sanitize_manifest(manifest),
            "database_engine": engine,
            "database_vendor": manifest.get("database_vendor") or ("postgresql" if engine == "postgres" else "sqlite"),
            "backup_type": manifest.get("backup_type") or "legacy",
            "includes_media": (root / "media").exists() or bool(manifest.get("includes_media")),
            "includes_env": (root / "env" / "production.env").exists() or (root / "runtime" / ".env").exists() or bool(manifest.get("includes_env")),
            "includes_system": (root / "system").exists() or (root / "systemd").exists() or bool(manifest.get("includes_system")),
            "warnings": warnings + list(manifest.get("warnings") or []),
            "file_count": len(files),
            "archive_size_bytes": path.stat().st_size,
            **checksum_summary,
        }
        return redact_backup_summary(summary)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def build_restore_plan(job):
    summary = job.validation_summary or validate_backup_archive(job.uploaded_file_path)
    manifest = summary.get("manifest") or {}
    engine = summary.get("database_engine")
    current_engine = database_engine_from_connection()
    plan = {
        "backup_created_at": manifest.get("created_at") or manifest.get("timestamp"),
        "backup_type": summary.get("backup_type"),
        "source_database_engine": engine,
        "target_database_engine": current_engine,
        "includes_media": bool(summary.get("includes_media")),
        "includes_env": bool(summary.get("includes_env")),
        "source_app_version": manifest.get("app_version") or "",
        "database_schema_migrations": manifest.get("database_schema_migrations") or {},
        "current_migration_status": "checked during command restore dry-run",
        "warnings": summary.get("warnings", []),
        "estimated_steps": [
            "validate archive and checksums",
            "create pre-restore backup",
            "stop writers and timers",
            "restore database using restore.sh",
            "optionally restore media/env",
            "run Django checks, migrations, collectstatic, doctor",
            "restart services and timers",
        ],
        "confirmation_phrase": restore_confirmation_phrase(job.pk),
    }
    return sanitize_restore_plan(plan)


def restore_confirmation_phrase(restore_id):
    return f"{RESTORE_CONFIRM_PREFIX}{restore_id}"


def generate_restore_command(job, options=None):
    options = options or {}
    backup_path = ensure_path_under(job.uploaded_file_path, get_private_restore_upload_root())
    command = [
        "sudo",
        str(Path(settings.BASE_DIR) / "scripts" / "restore.sh"),
        "--install-dir",
        str(settings.BASE_DIR),
        "--restore-job-id",
        str(job.pk),
        "--backup-file",
        str(backup_path),
        "--confirm",
        restore_confirmation_phrase(job.pk),
    ]
    if options.get("include_media"):
        command.append("--include-media")
    if options.get("restore_env"):
        command.append("--restore-env")
    return " ".join(shlex.quote(part) for part in command)


def mark_restore_command_generated(job, actor=None, options=None):
    if job.status not in {QasedakRestoreJob.Status.VALIDATED, QasedakRestoreJob.Status.READY, QasedakRestoreJob.Status.RESTORE_COMMAND_GENERATED}:
        raise BackupValidationError("Restore job must be validated before generating a command.")
    command = generate_restore_command(job, options=options)
    plan = build_restore_plan(job)
    plan["restore_command"] = command
    plan["web_restore_apply"] = "disabled"
    job.restore_plan = sanitize_restore_plan(plan)
    job.status = QasedakRestoreJob.Status.RESTORE_COMMAND_GENERATED
    job.save(update_fields=["restore_plan", "status"])
    log_backup_restore_action(actor or job.created_by, job, "restore.command_generated", {"restore_job_id": job.pk})
    return command


def validate_restore_job(job, actor=None):
    try:
        summary = validate_backup_archive(job.uploaded_file_path)
        job.validation_summary = summary
        job.restore_plan = build_restore_plan(job)
        job.includes_env = bool(summary.get("includes_env"))
        job.includes_media = bool(summary.get("includes_media"))
        job.status = QasedakRestoreJob.Status.VALIDATED
        job.error_message = ""
        job.validated_at = timezone.now()
        job.save()
        log_backup_restore_action(actor or job.created_by, job, "restore.validated", {"restore_job_id": job.pk})
    except Exception as exc:
        job.status = QasedakRestoreJob.Status.VALIDATION_FAILED
        job.error_message = safe_output(str(exc))
        job.validation_summary = {"valid": False, "error": job.error_message}
        job.validated_at = timezone.now()
        job.save(update_fields=["status", "error_message", "validation_summary", "validated_at"])
        log_backup_restore_action(actor or job.created_by, job, "restore.validation_failed", {"error": job.error_message})
        raise
    return job


def log_backup_restore_action(actor, obj, action, details=None):
    if not actor or not getattr(actor, "pk", None) or obj is None:
        return
    details = redact_backup_summary(details or {})
    payload = {
        "backup_restore_action": str(action),
        "details": details,
        "at": timezone.now().isoformat(),
    }
    try:
        content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        if str(action).endswith(".created") or str(action).endswith(".uploaded"):
            flag = ADDITION
        elif str(action).endswith(".deleted"):
            flag = DELETION
        else:
            flag = CHANGE
        LogEntry.objects.create(
            user_id=actor.pk,
            content_type=content_type,
            object_id=str(obj.pk),
            object_repr=str(obj)[:200],
            action_flag=flag,
            change_message=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        )
    except Exception:
        return
