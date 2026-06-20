from __future__ import annotations

import csv
import hashlib
import re
import time
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    BotConfiguration,
    BotUser,
    Customer,
    LegacyWizWizImportJob,
    LegacyWizWizImportMessageBatch,
    LegacyWizWizImportMessageRecipient,
    LegacyWizWizImportRow,
)


WIZWIZ_SOURCE = "wizwiz"
DEFAULT_WIZWIZ_USER_COLUMNS = [
    "id",
    "userid",
    "name",
    "username",
    "refcode",
    "wallet",
    "date",
    "phone",
    "refered_by",
    "step",
    "freetrial",
    "isAdmin",
    "first_start",
    "temp",
    "is_agent",
    "discount_percent",
    "agent_date",
    "spam_info",
]
USERS_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+`?users`?\b", re.IGNORECASE)
INSERT_VALUES_RE = re.compile(
    r"INSERT\s+INTO\s+`?users`?\s*(?P<columns>\([^)]*\))?\s*VALUES\s*",
    re.IGNORECASE | re.DOTALL,
)
PERSIAN_ARABIC_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
EMPTY_USERNAME_VALUES = {"", "-", "_", "none", "null", "nil", "n/a", "no", "ندارد", "بدون"}
APPLYABLE_ANALYZE_STATUSES = {
    LegacyWizWizImportRow.Status.WOULD_CREATE,
    LegacyWizWizImportRow.Status.WOULD_LINK_EXISTING,
    LegacyWizWizImportRow.Status.EXISTING,
    LegacyWizWizImportRow.Status.PENDING,
    LegacyWizWizImportRow.Status.FAILED,
}
MESSAGEABLE_ROW_STATUSES = {
    LegacyWizWizImportRow.Status.CREATED,
    LegacyWizWizImportRow.Status.LINKED,
    LegacyWizWizImportRow.Status.EXISTING,
    LegacyWizWizImportRow.Status.UPDATED,
}
MAX_LEGACY_IMPORT_MESSAGE_LENGTH = 4096
SECRET_OR_CONFIG_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:token|password|passwd|secret)\s*[:=]", re.IGNORECASE),
    re.compile(r"\b(?:vless|vmess|trojan|ss|ssr)://", re.IGNORECASE),
)


def calculate_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _statement_has_terminating_semicolon(statement):
    quote = ""
    escaped = False
    pos = 0
    while pos < len(statement):
        char = statement[pos]
        if escaped:
            escaped = False
            pos += 1
            continue
        if quote:
            if char == "\\":
                escaped = True
                pos += 1
                continue
            if char == quote:
                if pos + 1 < len(statement) and statement[pos + 1] == quote:
                    pos += 2
                    continue
                quote = ""
            pos += 1
            continue
        if char in {"'", '"'}:
            quote = char
            pos += 1
            continue
        if char == ";":
            return True
        pos += 1
    return False


def _parse_column_names(value):
    if not value:
        return DEFAULT_WIZWIZ_USER_COLUMNS[:]
    raw_columns = value.strip()[1:-1]
    columns = []
    for raw_column in raw_columns.split(","):
        column = raw_column.strip().strip("`").strip()
        if column:
            columns.append(column)
    return columns or DEFAULT_WIZWIZ_USER_COLUMNS[:]


def _decode_mysql_escape(char):
    return {
        "0": "\0",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "Z": "\x1a",
        "\\": "\\",
        "'": "'",
        '"': '"',
    }.get(char, char)


def _parse_sql_string(statement, pos):
    quote = statement[pos]
    pos += 1
    chars = []
    while pos < len(statement):
        char = statement[pos]
        if char == "\\":
            if pos + 1 >= len(statement):
                chars.append("\\")
                pos += 1
                continue
            chars.append(_decode_mysql_escape(statement[pos + 1]))
            pos += 2
            continue
        if char == quote:
            if pos + 1 < len(statement) and statement[pos + 1] == quote:
                chars.append(quote)
                pos += 2
                continue
            return "".join(chars), pos + 1
        chars.append(char)
        pos += 1
    raise ValueError("unterminated SQL string in users INSERT")


def _convert_sql_token(token):
    token = token.strip()
    if not token:
        return ""
    if token.upper() == "NULL":
        return None
    if re.fullmatch(r"-?\d+", token):
        try:
            return int(token)
        except ValueError:
            return token
    return token


def parse_mysql_insert_values(insert_statement):
    match = INSERT_VALUES_RE.search(insert_statement)
    if not match:
        raise ValueError("not a supported users INSERT statement")

    columns = _parse_column_names(match.group("columns"))
    rows = []
    pos = match.end()
    length = len(insert_statement)
    while pos < length:
        while pos < length and insert_statement[pos].isspace():
            pos += 1
        if pos < length and insert_statement[pos] == ",":
            pos += 1
            continue
        if pos >= length or insert_statement[pos] == ";":
            break
        if insert_statement[pos] != "(":
            raise ValueError("expected row tuple in users INSERT")
        pos += 1
        row = []
        while pos < length:
            while pos < length and insert_statement[pos].isspace():
                pos += 1
            if pos >= length:
                raise ValueError("unterminated row tuple in users INSERT")
            if insert_statement[pos] in {"'", '"'}:
                value, pos = _parse_sql_string(insert_statement, pos)
            else:
                start = pos
                while pos < length and insert_statement[pos] not in {",", ")"}:
                    pos += 1
                value = _convert_sql_token(insert_statement[start:pos])
            row.append(value)
            while pos < length and insert_statement[pos].isspace():
                pos += 1
            if pos < length and insert_statement[pos] == ",":
                pos += 1
                continue
            if pos < length and insert_statement[pos] == ")":
                pos += 1
                rows.append(row)
                break
            raise ValueError("expected comma or closing parenthesis in users INSERT")
    return columns, rows


def parse_wizwiz_users_from_sql_file(path):
    statement_parts = []
    statement_start_line = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not statement_parts and not USERS_INSERT_RE.search(line):
                continue
            if not statement_parts:
                statement_start_line = line_number
            statement_parts.append(line)
            statement = "".join(statement_parts)
            if not _statement_has_terminating_semicolon(statement):
                continue
            try:
                columns, value_rows = parse_mysql_insert_values(statement)
            except ValueError as exc:
                yield {
                    "__parse_error__": str(exc),
                    "__line_number__": statement_start_line,
                    "__row_number__": 0,
                }
            else:
                for row_number, values in enumerate(value_rows, start=1):
                    row = {column: values[index] if index < len(values) else None for index, column in enumerate(columns)}
                    row["__line_number__"] = statement_start_line
                    row["__row_number__"] = row_number
                    yield row
            statement_parts = []
            statement_start_line = 0
    if statement_parts:
        yield {
            "__parse_error__": "unterminated users INSERT statement",
            "__line_number__": statement_start_line,
            "__row_number__": 0,
        }


def _safe_text(value, max_length=255):
    if value is None:
        return ""
    value = str(value).replace("\x00", "").strip()
    if len(value) > max_length:
        return f"{value[: max_length - 3]}..."
    return value


def _clean_digits(value):
    return str(value or "").translate(PERSIAN_ARABIC_DIGIT_TRANSLATION).strip()


def is_valid_telegram_user_id(value):
    cleaned = _clean_digits(value)
    if not re.fullmatch(r"\d{1,32}", cleaned):
        return False
    try:
        return int(cleaned) > 0
    except ValueError:
        return False


def mask_telegram_user_id(value):
    cleaned = _clean_digits(value)
    if not cleaned:
        return ""
    if len(cleaned) <= 4:
        return "***"
    return f"{cleaned[:2]}***{cleaned[-3:]}"


def mask_name(value):
    cleaned = _safe_text(value, 80)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return "*"
    return f"{cleaned[0]}***"


def mask_phone(value):
    cleaned = normalize_phone(value)
    if not cleaned:
        return ""
    if len(cleaned) <= 5:
        return "***"
    return f"{cleaned[:3]}***{cleaned[-2:]}"


def normalize_username(value):
    cleaned = _safe_text(value, 120).strip().lstrip("@")
    if cleaned.lower() in EMPTY_USERNAME_VALUES:
        return ""
    if cleaned in EMPTY_USERNAME_VALUES:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", cleaned):
        return ""
    return cleaned


def normalize_phone(value):
    raw = _clean_digits(value)
    if not raw or raw.lower() in EMPTY_USERNAME_VALUES:
        return ""
    has_plus = raw.startswith("+")
    digits = "".join(char for char in raw if char.isdigit())
    if len(digits) < 7 or len(digits) > 15:
        return ""
    return f"+{digits}" if has_plus else digits


def _to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).translate(PERSIAN_ARABIC_DIGIT_TRANSLATION).strip())
    except (TypeError, ValueError):
        return None


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, int):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_wizwiz_user_row(row):
    if row.get("__parse_error__"):
        return {
            "valid": False,
            "reason": "parse_error",
            "parse_error": _safe_text(row.get("__parse_error__"), 160),
            "line_number": row.get("__line_number__"),
            "row_number": row.get("__row_number__"),
        }

    telegram_user_id = _clean_digits(row.get("userid"))
    if not is_valid_telegram_user_id(telegram_user_id):
        return {
            "valid": False,
            "reason": "invalid_userid",
            "telegram_user_id": telegram_user_id,
            "legacy_pk": _safe_text(row.get("id"), 80),
            "line_number": row.get("__line_number__"),
            "row_number": row.get("__row_number__"),
        }

    wallet = _to_int(row.get("wallet"))
    normalized = {
        "valid": True,
        "legacy_pk": _safe_text(row.get("id"), 80),
        "telegram_user_id": telegram_user_id,
        "name": _safe_text(row.get("name"), 120),
        "username": normalize_username(row.get("username")),
        "phone": normalize_phone(row.get("phone")),
        "wallet": wallet,
        "legacy_date": _safe_text(row.get("date"), 80),
        "freetrial": _safe_text(row.get("freetrial"), 120),
        "refcode": _safe_text(row.get("refcode"), 120),
        "refered_by": _safe_text(row.get("refered_by"), 120),
        "is_admin": _to_bool(row.get("isAdmin")),
        "is_agent": _to_bool(row.get("is_agent")),
        "spam_info_summary": _safe_text(row.get("spam_info"), 200),
        "line_number": row.get("__line_number__"),
        "row_number": row.get("__row_number__"),
    }
    return normalized


def _legacy_metadata(normalized, job, *, imported_at=None):
    telegram_user_id = normalized.get("telegram_user_id") or ""
    metadata = {
        "source": WIZWIZ_SOURCE,
        "job_id": str(job.pk or ""),
        "legacy_pk": normalized.get("legacy_pk") or "",
        "legacy_userid_masked": mask_telegram_user_id(telegram_user_id),
        "legacy_date": normalized.get("legacy_date") or "",
        "legacy_wallet": normalized.get("wallet"),
        "legacy_is_agent": bool(normalized.get("is_agent")),
        "legacy_is_admin": bool(normalized.get("is_admin")),
        "legacy_freetrial": normalized.get("freetrial") or "",
        "legacy_refcode": normalized.get("refcode") or "",
        "legacy_refered_by": normalized.get("refered_by") or "",
        "spam_info_summary": normalized.get("spam_info_summary") or "",
    }
    if imported_at:
        metadata["imported_at"] = imported_at.isoformat()
    return metadata


def _row_metadata(normalized, job):
    return {
        "legacy_import": _legacy_metadata(normalized, job),
        "parser": {
            "line_number": normalized.get("line_number"),
            "row_number": normalized.get("row_number"),
        },
    }


def _default_telegram_bot_config():
    queryset = BotConfiguration.objects.filter(
        provider=BotConfiguration.Provider.TELEGRAM,
        is_active=True,
    ).order_by("pk")
    with_token = queryset.exclude(bot_token="").first()
    return with_token or queryset.first()


def _find_existing_bot_user(telegram_user_id, *, bot_config=None):
    queryset = (
        BotUser.objects.select_related("bot_config", "customer")
        .filter(
            provider_user_id=str(telegram_user_id),
            bot_config__provider=BotConfiguration.Provider.TELEGRAM,
        )
        .order_by("-is_active", "-last_seen_at", "-updated_at", "pk")
    )
    if bot_config:
        preferred = queryset.filter(bot_config=bot_config).first()
        if preferred:
            return preferred
    return queryset.first()


def _find_customer_for_normalized(normalized, bot_user=None):
    if bot_user and bot_user.customer_id:
        return bot_user.customer
    phone = normalized.get("phone") or ""
    if phone:
        customer = Customer.objects.filter(phone_number=phone).first()
        if customer:
            return customer
    username = normalized.get("username") or ""
    if username:
        return Customer.objects.filter(username=username).first()
    return None


def _customer_field_available(field, value, *, excluding_customer=None):
    if not value:
        return True
    queryset = Customer.objects.filter(**{field: value})
    if excluding_customer:
        queryset = queryset.exclude(pk=excluding_customer.pk)
    return not queryset.exists()


def _build_customer_defaults(normalized):
    username = normalized.get("username") or ""
    phone = normalized.get("phone") or ""
    return {
        "username": username if _customer_field_available("username", username) else "",
        "phone_number": phone if _customer_field_available("phone_number", phone) else "",
        "display_name": normalized.get("name") or username or phone or "",
        "is_active": True,
    }


def _set_if_changed(instance, field, value, changed_fields, *, overwrite=False):
    value = value or ""
    if not value:
        return
    current = getattr(instance, field)
    if current and not overwrite:
        return
    if current != value:
        setattr(instance, field, value)
        changed_fields.append(field)


def _apply_job_file_fingerprint(job):
    path = job.uploaded_file.path
    file_path = Path(path)
    metadata = dict(job.metadata or {})
    if file_path.exists():
        job.file_size = file_path.stat().st_size
        job.file_sha256 = calculate_file_sha256(path)
        duplicate_jobs = list(
            LegacyWizWizImportJob.objects.filter(file_sha256=job.file_sha256)
            .exclude(pk=job.pk)
            .order_by("-created_at")
            .values("id", "status", "created_at")[:10]
        )
        if duplicate_jobs:
            metadata["duplicate_file_jobs"] = [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "created_at": item["created_at"].isoformat() if item["created_at"] else "",
                }
                for item in duplicate_jobs
            ]
    job.metadata = metadata


def _row_status_for_normalized(job, normalized, *, bot_config=None):
    if job.skip_admins and normalized.get("is_admin"):
        return LegacyWizWizImportRow.Status.SKIPPED, "skipped_admin", None, None, False, False
    if normalized.get("is_agent") and not job.import_agents:
        return LegacyWizWizImportRow.Status.SKIPPED, "skipped_agent", None, None, False, False
    if getattr(job, "only_agents", False) and not normalized.get("is_agent"):
        return LegacyWizWizImportRow.Status.SKIPPED, "skipped_filter_only_agents", None, None, False, False
    wallet = normalized.get("wallet")
    if job.only_wallet_positive and (wallet is None or wallet <= 0):
        return LegacyWizWizImportRow.Status.SKIPPED, "skipped_filter_wallet", None, None, False, False

    bot_user = _find_existing_bot_user(normalized["telegram_user_id"], bot_config=bot_config)
    customer = _find_customer_for_normalized(normalized, bot_user=bot_user)
    would_create_bot_user = bot_user is None
    would_create_customer = customer is None and job.create_customers
    if bot_user and bot_user.customer_id:
        status = LegacyWizWizImportRow.Status.EXISTING
        reason = "existing_bot_user"
    elif bot_user:
        status = LegacyWizWizImportRow.Status.WOULD_LINK_EXISTING if job.create_customers else LegacyWizWizImportRow.Status.EXISTING
        reason = "existing_bot_user_without_customer"
    else:
        status = LegacyWizWizImportRow.Status.WOULD_CREATE
        reason = "new_bot_user"
    return status, reason, bot_user, customer, would_create_bot_user, would_create_customer


def analyze_wizwiz_import_job(job):
    if job.status == LegacyWizWizImportJob.Status.APPLYING:
        raise ValidationError("Cannot analyze a job while it is applying.")
    if not job.uploaded_file:
        raise ValidationError("No SQL file is attached to this import job.")

    now = timezone.now()
    job.status = LegacyWizWizImportJob.Status.ANALYZING
    job.error_message = ""
    job.failed_at = None
    job.applied_at = None
    job.analyzed_at = None
    _apply_job_file_fingerprint(job)
    job.save(
        update_fields=[
            "status",
            "error_message",
            "failed_at",
            "applied_at",
            "analyzed_at",
            "file_size",
            "file_sha256",
            "metadata",
            "updated_at",
        ]
    )

    counters = {
        "parsed": 0,
        "valid": 0,
        "invalid": 0,
        "duplicates": 0,
        "existing_bot_users": 0,
        "existing_customers": 0,
        "would_create_bot_users": 0,
        "would_create_customers": 0,
        "admins": 0,
        "agents": 0,
        "wallet_positive": 0,
        "skipped": 0,
    }
    seen_user_ids = set()
    bot_config = _default_telegram_bot_config()
    limit = _metadata_limit(job)

    try:
        with transaction.atomic():
            job.rows.all().delete()

        for raw_row in parse_wizwiz_users_from_sql_file(job.uploaded_file.path):
            if limit and counters["valid"] >= limit:
                break
            counters["parsed"] += 1
            normalized = normalize_wizwiz_user_row(raw_row)
            if not normalized.get("valid"):
                counters["invalid"] += 1
                _create_invalid_row(job, normalized, counters["parsed"])
                continue

            telegram_user_id = normalized["telegram_user_id"]
            if telegram_user_id in seen_user_ids:
                counters["duplicates"] += 1
                continue
            seen_user_ids.add(telegram_user_id)
            counters["valid"] += 1
            if normalized.get("is_admin"):
                counters["admins"] += 1
            if normalized.get("is_agent"):
                counters["agents"] += 1
            wallet = normalized.get("wallet")
            if wallet is not None and wallet > 0:
                counters["wallet_positive"] += 1

            status, reason, bot_user, customer, would_create_bot_user, would_create_customer = _row_status_for_normalized(
                job,
                normalized,
                bot_config=bot_config,
            )
            if status == LegacyWizWizImportRow.Status.SKIPPED:
                counters["skipped"] += 1
            if bot_user:
                counters["existing_bot_users"] += 1
            if customer:
                counters["existing_customers"] += 1
            if would_create_bot_user:
                counters["would_create_bot_users"] += 1
            if would_create_customer:
                counters["would_create_customers"] += 1

            LegacyWizWizImportRow.objects.create(
                job=job,
                legacy_pk=normalized.get("legacy_pk") or "",
                telegram_user_id=telegram_user_id,
                telegram_user_id_masked=mask_telegram_user_id(telegram_user_id),
                old_name_masked=mask_name(normalized.get("name")),
                old_username=normalized.get("username") or "",
                old_phone_masked=mask_phone(normalized.get("phone")),
                old_wallet=normalized.get("wallet"),
                old_is_admin=bool(normalized.get("is_admin")),
                old_is_agent=bool(normalized.get("is_agent")),
                old_freetrial=normalized.get("freetrial") or "",
                old_refcode=normalized.get("refcode") or "",
                old_refered_by=normalized.get("refered_by") or "",
                status=status,
                reason=reason,
                bot_user=bot_user,
                customer=customer,
                metadata=_row_metadata(normalized, job),
            )

        metadata = dict(job.metadata or {})
        metadata["analyze"] = {
            "completed_at": now.isoformat(),
            "parser": "limited_mysql_insert_users",
            "telegram_bot_config_id": getattr(bot_config, "pk", None),
            "limit": limit or None,
        }
        if not bot_config:
            metadata["analyze"]["warning"] = "No active Telegram BotConfiguration exists; apply will fail until one is configured."

        job.parsed_users_count = counters["parsed"]
        job.valid_users_count = counters["valid"]
        job.invalid_rows_count = counters["invalid"]
        job.duplicate_in_file_count = counters["duplicates"]
        job.existing_bot_users_count = counters["existing_bot_users"]
        job.existing_customers_count = counters["existing_customers"]
        job.would_create_bot_users_count = counters["would_create_bot_users"]
        job.would_create_customers_count = counters["would_create_customers"]
        job.admins_count = counters["admins"]
        job.agents_count = counters["agents"]
        job.wallet_positive_count = counters["wallet_positive"]
        job.created_bot_users_count = 0
        job.created_customers_count = 0
        job.linked_existing_count = 0
        job.updated_existing_count = 0
        job.skipped_count = counters["skipped"]
        job.failed_count = 0
        job.status = LegacyWizWizImportJob.Status.ANALYZED
        job.analyzed_at = timezone.now()
        job.metadata = metadata
        job.save()
    except Exception as exc:
        job.status = LegacyWizWizImportJob.Status.FAILED
        job.error_message = _safe_text(exc, 1000)
        job.failed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "failed_at", "updated_at"])
        raise

    return build_wizwiz_import_summary(job)


def _metadata_limit(job):
    try:
        limit = int((job.metadata or {}).get("limit") or 0)
    except (TypeError, ValueError):
        return 0
    return max(limit, 0)


def _create_invalid_row(job, normalized, sequence):
    telegram_user_id = normalized.get("telegram_user_id") or f"invalid:{sequence}"
    if not telegram_user_id or telegram_user_id in {"0", "None"}:
        telegram_user_id = f"invalid:{sequence}"
    LegacyWizWizImportRow.objects.create(
        job=job,
        legacy_pk=normalized.get("legacy_pk") or "",
        telegram_user_id=telegram_user_id,
        telegram_user_id_masked=mask_telegram_user_id(telegram_user_id),
        status=LegacyWizWizImportRow.Status.INVALID,
        reason=normalized.get("reason") or "invalid",
        metadata={
            "parser": {
                "line_number": normalized.get("line_number"),
                "row_number": normalized.get("row_number"),
            },
            "error": normalized.get("parse_error") or normalized.get("reason") or "invalid",
        },
    )


def _normalized_rows_by_user_id(job):
    rows = {}
    for raw_row in parse_wizwiz_users_from_sql_file(job.uploaded_file.path):
        normalized = normalize_wizwiz_user_row(raw_row)
        if not normalized.get("valid"):
            continue
        rows.setdefault(normalized["telegram_user_id"], normalized)
    return rows


def apply_wizwiz_import_job(job):
    if job.status == LegacyWizWizImportJob.Status.APPLIED:
        return build_wizwiz_import_summary(job)
    if job.status != LegacyWizWizImportJob.Status.ANALYZED:
        raise ValidationError("Only analyzed legacy WizWiz import jobs can be applied.")

    job.status = LegacyWizWizImportJob.Status.APPLYING
    job.error_message = ""
    job.failed_at = None
    job.save(update_fields=["status", "error_message", "failed_at", "updated_at"])

    normalized_by_user_id = _normalized_rows_by_user_id(job)
    bot_config = _default_telegram_bot_config()
    try:
        for row in job.rows.select_related("bot_user", "customer").filter(status__in=APPLYABLE_ANALYZE_STATUSES):
            normalized = normalized_by_user_id.get(row.telegram_user_id)
            try:
                import_wizwiz_row(
                    row,
                    update_existing=job.update_existing,
                    normalized=normalized,
                    bot_config=bot_config,
                )
            except Exception as exc:
                row.status = LegacyWizWizImportRow.Status.FAILED
                row.reason = _safe_text(exc, 255)
                metadata = dict(row.metadata or {})
                metadata["apply_error"] = _safe_text(exc, 300)
                row.metadata = metadata
                row.save(update_fields=["status", "reason", "metadata", "updated_at"])

        _recompute_apply_summary(job)
        job.status = LegacyWizWizImportJob.Status.APPLIED
        job.applied_at = timezone.now()
        metadata = dict(job.metadata or {})
        metadata["apply"] = {
            "completed_at": job.applied_at.isoformat(),
            "telegram_bot_config_id": getattr(bot_config, "pk", None),
        }
        job.metadata = metadata
        job.save()
    except Exception as exc:
        job.status = LegacyWizWizImportJob.Status.FAILED
        job.error_message = _safe_text(exc, 1000)
        job.failed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "failed_at", "updated_at"])
        raise
    return build_wizwiz_import_summary(job)


def build_wizwiz_simple_restore_result(job):
    job.refresh_from_db()
    return {
        "job_id": job.pk,
        "users_imported": job.created_bot_users_count,
        "users_existing": job.existing_bot_users_count,
        "users_skipped": job.skipped_count,
        "users_failed": job.failed_count,
    }


def wizwiz_simple_restore(
    uploaded_file,
    *,
    created_by=None,
    title="",
    skip_admins=True,
    import_agents=True,
    only_wallet_positive=False,
    update_existing=False,
    create_customers=True,
):
    if not uploaded_file:
        raise ValidationError("A WizWiz SQL backup file is required.")
    original_filename = Path(getattr(uploaded_file, "name", "") or "wizwiz.sql").name
    if original_filename and Path(original_filename).suffix.lower() != ".sql":
        raise ValidationError("Only .sql backup files are accepted.")
    file_size = getattr(uploaded_file, "size", 0) or 0
    if file_size <= 0:
        raise ValidationError("Uploaded file is empty.")

    job = LegacyWizWizImportJob(
        title=title or original_filename,
        original_filename=original_filename,
        file_size=file_size,
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
        skip_admins=skip_admins,
        import_agents=import_agents,
        only_wallet_positive=only_wallet_positive,
        update_existing=update_existing,
        create_customers=create_customers,
    )
    job.uploaded_file.save(original_filename, uploaded_file, save=True)
    analyze_wizwiz_import_job(job)
    apply_wizwiz_import_job(job)
    return build_wizwiz_simple_restore_result(job)


def import_wizwiz_row(row, update_existing=False, normalized=None, bot_config=None):
    if row.status in {
        LegacyWizWizImportRow.Status.SKIPPED,
        LegacyWizWizImportRow.Status.INVALID,
        LegacyWizWizImportRow.Status.CREATED,
        LegacyWizWizImportRow.Status.LINKED,
        LegacyWizWizImportRow.Status.UPDATED,
    }:
        return row

    job = row.job
    if not normalized:
        normalized = _normalized_rows_by_user_id(job).get(row.telegram_user_id)
    if not normalized:
        row.status = LegacyWizWizImportRow.Status.FAILED
        row.reason = "source_row_not_found"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row

    if row.old_is_admin and job.skip_admins:
        row.status = LegacyWizWizImportRow.Status.SKIPPED
        row.reason = "skipped_admin"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row
    if row.old_is_agent and not job.import_agents:
        row.status = LegacyWizWizImportRow.Status.SKIPPED
        row.reason = "skipped_agent"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row
    if getattr(job, "only_agents", False) and not row.old_is_agent:
        row.status = LegacyWizWizImportRow.Status.SKIPPED
        row.reason = "skipped_filter_only_agents"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row
    if job.only_wallet_positive and (row.old_wallet is None or row.old_wallet <= 0):
        row.status = LegacyWizWizImportRow.Status.SKIPPED
        row.reason = "skipped_filter_wallet"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row

    bot_config = bot_config or _default_telegram_bot_config()
    if not bot_config:
        row.status = LegacyWizWizImportRow.Status.FAILED
        row.reason = "missing_telegram_bot_config"
        row.save(update_fields=["status", "reason", "updated_at"])
        return row

    with transaction.atomic():
        bot_user = _find_existing_bot_user(row.telegram_user_id, bot_config=bot_config)
        customer = _find_customer_for_normalized(normalized, bot_user=bot_user)
        created_customer = False
        created_bot_user = False
        linked_existing = False
        updated_existing = False

        if not customer and job.create_customers:
            defaults = _build_customer_defaults(normalized)
            try:
                customer = Customer.objects.create(**defaults)
                created_customer = True
            except IntegrityError:
                customer = _find_customer_for_normalized(normalized, bot_user=bot_user)
                if not customer:
                    raise

        if not bot_user:
            bot_user = BotUser.objects.create(
                bot_config=bot_config,
                customer=customer,
                provider_user_id=row.telegram_user_id,
                chat_id=row.telegram_user_id,
                username=normalized.get("username") or "",
                first_name=normalized.get("name")[:120] if normalized.get("name") else "",
                display_name=normalized.get("name") or normalized.get("username") or "",
                is_active=True,
            )
            created_bot_user = True
        else:
            changed_fields = []
            if customer and not bot_user.customer_id:
                bot_user.customer = customer
                changed_fields.append("customer")
                linked_existing = True
            _set_if_changed(bot_user, "username", normalized.get("username"), changed_fields, overwrite=update_existing)
            _set_if_changed(bot_user, "first_name", normalized.get("name"), changed_fields, overwrite=update_existing)
            _set_if_changed(bot_user, "display_name", normalized.get("name") or normalized.get("username"), changed_fields, overwrite=update_existing)
            if update_existing and not bot_user.is_active:
                bot_user.is_active = True
                changed_fields.append("is_active")
            if changed_fields:
                bot_user.save(update_fields=[*set(changed_fields), "updated_at"])
                updated_existing = not linked_existing

        if customer:
            customer_changes = []
            username = normalized.get("username") or ""
            phone = normalized.get("phone") or ""
            if username and _customer_field_available("username", username, excluding_customer=customer):
                _set_if_changed(customer, "username", username, customer_changes, overwrite=update_existing)
            if phone and _customer_field_available("phone_number", phone, excluding_customer=customer):
                _set_if_changed(customer, "phone_number", phone, customer_changes, overwrite=update_existing)
            _set_if_changed(
                customer,
                "display_name",
                normalized.get("name") or username or phone,
                customer_changes,
                overwrite=update_existing,
            )
            if customer_changes:
                customer.save(update_fields=[*set(customer_changes), "updated_at"])
                if not created_bot_user and not linked_existing:
                    updated_existing = True

        metadata = dict(row.metadata or {})
        metadata["legacy_import"] = _legacy_metadata(normalized, job, imported_at=timezone.now())
        metadata["apply"] = {
            "created_customer": created_customer,
            "created_bot_user": created_bot_user,
            "linked_existing": linked_existing,
            "updated_existing": updated_existing,
        }
        row.bot_user = bot_user
        row.customer = customer
        row.metadata = metadata
        if created_bot_user:
            row.status = LegacyWizWizImportRow.Status.CREATED
            row.reason = "created_bot_user"
        elif linked_existing:
            row.status = LegacyWizWizImportRow.Status.LINKED
            row.reason = "linked_existing_bot_user"
        elif updated_existing:
            row.status = LegacyWizWizImportRow.Status.UPDATED
            row.reason = "updated_existing"
        else:
            row.status = LegacyWizWizImportRow.Status.EXISTING
            row.reason = "already_imported"
        row.save(update_fields=["status", "reason", "bot_user", "customer", "metadata", "updated_at"])
    return row


def _recompute_apply_summary(job):
    rows = list(job.rows.all())
    job.created_bot_users_count = sum(1 for row in rows if row.status == LegacyWizWizImportRow.Status.CREATED)
    job.created_customers_count = sum(
        1 for row in rows if (row.metadata or {}).get("apply", {}).get("created_customer")
    )
    job.linked_existing_count = sum(1 for row in rows if row.status == LegacyWizWizImportRow.Status.LINKED)
    job.updated_existing_count = sum(1 for row in rows if row.status == LegacyWizWizImportRow.Status.UPDATED)
    job.skipped_count = sum(
        1
        for row in rows
        if row.status
        in {
            LegacyWizWizImportRow.Status.SKIPPED,
            LegacyWizWizImportRow.Status.INVALID,
        }
    )
    job.failed_count = sum(1 for row in rows if row.status == LegacyWizWizImportRow.Status.FAILED)


def build_wizwiz_import_summary(job):
    job.refresh_from_db()
    return {
        "job_id": job.pk,
        "status": job.status,
        "parsed_users": job.parsed_users_count,
        "valid_users": job.valid_users_count,
        "invalid_rows": job.invalid_rows_count,
        "duplicates": job.duplicate_in_file_count,
        "admins": job.admins_count,
        "agents": job.agents_count,
        "wallet_positive": job.wallet_positive_count,
        "existing_bot_users": job.existing_bot_users_count,
        "existing_customers": job.existing_customers_count,
        "would_create_bot_users": job.would_create_bot_users_count,
        "would_create_customers": job.would_create_customers_count,
        "created_bot_users": job.created_bot_users_count,
        "created_customers": job.created_customers_count,
        "linked_existing": job.linked_existing_count,
        "updated_existing": job.updated_existing_count,
        "skipped": job.skipped_count,
        "failed": job.failed_count,
    }


def export_wizwiz_import_rows_csv(job, fileobj):
    writer = csv.writer(fileobj)
    writer.writerow(
        [
            "job_id",
            "telegram_user_id_masked",
            "old_username",
            "status",
            "reason",
            "bot_user_id",
            "customer_id",
            "old_wallet",
            "old_is_agent",
            "old_is_admin",
            "created_at",
        ]
    )
    for row in job.rows.select_related("bot_user", "customer").order_by("created_at", "pk"):
        writer.writerow(
            [
                job.pk,
                row.telegram_user_id_masked,
                row.old_username,
                row.status,
                row.reason,
                row.bot_user_id or "",
                row.customer_id or "",
                row.old_wallet if row.old_wallet is not None else "",
                row.old_is_agent,
                row.old_is_admin,
                timezone.localtime(row.created_at).isoformat() if row.created_at else "",
            ]
        )


def validate_legacy_import_message_text(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValidationError("Message text is required.")
    if len(cleaned) > MAX_LEGACY_IMPORT_MESSAGE_LENGTH:
        raise ValidationError(f"Message text must be {MAX_LEGACY_IMPORT_MESSAGE_LENGTH} characters or fewer.")
    for pattern in SECRET_OR_CONFIG_PATTERNS:
        if pattern.search(cleaned):
            raise ValidationError("Message text must not include tokens, passwords, secrets, or VPN config links.")
    return cleaned


def _messageable_rows(job):
    return (
        job.rows.select_related("bot_user", "bot_user__bot_config", "customer")
        .filter(status__in=MESSAGEABLE_ROW_STATUSES)
        .order_by("created_at", "pk")
    )


def preview_legacy_import_message_batch(job, text):
    cleaned = validate_legacy_import_message_text(text)
    rows = list(_messageable_rows(job))
    skipped_no_chat_id = 0
    pending = 0
    for row in rows:
        bot_user = row.bot_user
        if (
            bot_user
            and bot_user.is_active
            and bot_user.chat_id
            and bot_user.bot_config_id
            and bot_user.bot_config.provider == BotConfiguration.Provider.TELEGRAM
            and bot_user.bot_config.is_active
            and bot_user.bot_config.bot_token
        ):
            pending += 1
        else:
            skipped_no_chat_id += 1
    return {
        "text_length": len(cleaned),
        "recipients_total": len(rows),
        "sendable": pending,
        "skipped_no_chat_id": skipped_no_chat_id,
        "requires_large_send_confirmation": len(rows) > 1000,
    }


def create_legacy_import_message_batch(job, text, created_by=None):
    if job.status != LegacyWizWizImportJob.Status.APPLIED:
        raise ValidationError("Messages can only be sent after the import job is applied.")
    cleaned = validate_legacy_import_message_text(text)
    with transaction.atomic():
        batch = LegacyWizWizImportMessageBatch.objects.create(
            job=job,
            text=cleaned,
            created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
            metadata={
                "source": WIZWIZ_SOURCE,
                "job_id": str(job.pk),
                "text_length": len(cleaned),
            },
        )
        for row in _messageable_rows(job):
            bot_user = row.bot_user
            status = LegacyWizWizImportMessageRecipient.Status.PENDING
            error_message = ""
            if not bot_user or not bot_user.chat_id:
                status = LegacyWizWizImportMessageRecipient.Status.SKIPPED
                error_message = "No BotUser/chat_id was resolved for this import row."
            elif not bot_user.is_active:
                status = LegacyWizWizImportMessageRecipient.Status.SKIPPED
                error_message = "BotUser is inactive."
            elif bot_user.bot_config.provider != BotConfiguration.Provider.TELEGRAM:
                status = LegacyWizWizImportMessageRecipient.Status.SKIPPED
                error_message = "BotUser is not a Telegram user."
            elif not bot_user.bot_config.is_active or not bot_user.bot_config.bot_token:
                status = LegacyWizWizImportMessageRecipient.Status.SKIPPED
                error_message = "Telegram BotConfiguration is inactive or missing a token."
            LegacyWizWizImportMessageRecipient.objects.create(
                batch=batch,
                row=row,
                bot_user=bot_user,
                customer=row.customer,
                telegram_user_id_masked=row.telegram_user_id_masked,
                status=status,
                error_message=error_message,
                metadata={
                    "row_id": row.pk,
                    "bot_user_id": bot_user.pk if bot_user else None,
                    "customer_id": row.customer_id,
                },
            )
        refresh_legacy_import_message_batch_counts(batch)
    return batch


def refresh_legacy_import_message_batch_counts(batch):
    recipients = LegacyWizWizImportMessageRecipient.objects.filter(batch=batch)
    batch.total_recipients = recipients.count()
    batch.sent_count = recipients.filter(status=LegacyWizWizImportMessageRecipient.Status.SENT).count()
    batch.failed_count = recipients.filter(status=LegacyWizWizImportMessageRecipient.Status.FAILED).count()
    batch.skipped_count = recipients.filter(status=LegacyWizWizImportMessageRecipient.Status.SKIPPED).count()
    batch.blocked_count = recipients.filter(status=LegacyWizWizImportMessageRecipient.Status.BLOCKED).count()
    batch.save(
        update_fields=[
            "total_recipients",
            "sent_count",
            "failed_count",
            "skipped_count",
            "blocked_count",
            "updated_at",
        ]
    )
    return {
        "recipients_total": batch.total_recipients,
        "sent": batch.sent_count,
        "failed": batch.failed_count,
        "skipped_no_chat_id": batch.skipped_count,
        "blocked": batch.blocked_count,
        "rate_limited": 0,
    }


def _normalize_import_message_delivery_error(error):
    message = _safe_text(error, 1000)
    lowered = message.lower()
    if "too many requests" in lowered or "retry after" in lowered or "429" in lowered:
        return "rate_limited", message or "rate limited"
    if "blocked" in lowered or "forbidden" in lowered:
        return "blocked", message or "bot blocked"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout", message or "timeout"
    return "failed", message or "Bot API delivery failed."


def _message_batch_rate_delay(batch):
    rate_limit = 5
    config_ids = batch.recipients.exclude(bot_user__bot_config_id__isnull=True).values("bot_user__bot_config_id")
    first_config = (
        BotConfiguration.objects.filter(
            pk__in=config_ids,
            provider=BotConfiguration.Provider.TELEGRAM,
            is_active=True,
        )
        .exclude(bot_token="")
        .select_related("store")
        .order_by("pk")
        .first()
    )
    if first_config and first_config.store:
        try:
            rate_limit = max(int(first_config.store.broadcast_rate_limit_per_second or 5), 1)
        except (TypeError, ValueError):
            rate_limit = 5
    return 1 / rate_limit if rate_limit else 0


def send_legacy_import_message_batch(batch, limit=None, dry_run=False):
    validate_legacy_import_message_text(batch.text)
    if batch.status == LegacyWizWizImportMessageBatch.Status.CANCELLED:
        return refresh_legacy_import_message_batch_counts(batch)
    if batch.status == LegacyWizWizImportMessageBatch.Status.SENT:
        return refresh_legacy_import_message_batch_counts(batch)

    batch.status = LegacyWizWizImportMessageBatch.Status.SENDING
    batch.save(update_fields=["status", "updated_at"])

    from .telegram_bot.client import BotClient, BotDeliveryError

    pending = (
        batch.recipients.select_related("bot_user", "bot_user__bot_config", "customer", "row")
        .filter(status=LegacyWizWizImportMessageRecipient.Status.PENDING)
        .order_by("created_at", "pk")
    )
    if limit is not None:
        pending = pending[: max(int(limit), 0)]
    delay = _message_batch_rate_delay(batch)
    rate_limited = 0

    for recipient in pending:
        bot_user = recipient.bot_user
        if not bot_user or not bot_user.chat_id:
            recipient.status = LegacyWizWizImportMessageRecipient.Status.SKIPPED
            recipient.error_message = "No BotUser/chat_id was resolved for this import row."
            recipient.save(update_fields=["status", "error_message", "updated_at"])
            continue
        if dry_run:
            continue
        try:
            BotClient(bot_user.bot_config).send_message(
                batch.text,
                chat_id=bot_user.chat_id,
                parse_mode=None,
            )
        except BotDeliveryError as exc:
            kind, message = _normalize_import_message_delivery_error(exc)
            if kind == "blocked":
                recipient.status = LegacyWizWizImportMessageRecipient.Status.BLOCKED
            else:
                recipient.status = LegacyWizWizImportMessageRecipient.Status.FAILED
            if kind == "rate_limited":
                rate_limited += 1
            recipient.error_message = message
            metadata = dict(recipient.metadata or {})
            metadata["delivery_error_type"] = kind
            recipient.metadata = metadata
            recipient.save(update_fields=["status", "error_message", "metadata", "updated_at"])
        else:
            recipient.status = LegacyWizWizImportMessageRecipient.Status.SENT
            recipient.error_message = ""
            recipient.sent_at = timezone.now()
            recipient.save(update_fields=["status", "error_message", "sent_at", "updated_at"])
        if delay:
            time.sleep(delay)

    counts = refresh_legacy_import_message_batch_counts(batch)
    counts["rate_limited"] = rate_limited
    batch.refresh_from_db()
    pending_exists = batch.recipients.filter(status=LegacyWizWizImportMessageRecipient.Status.PENDING).exists()
    if dry_run:
        batch.status = LegacyWizWizImportMessageBatch.Status.DRAFT
    elif pending_exists:
        batch.status = LegacyWizWizImportMessageBatch.Status.PARTIAL
    elif batch.failed_count or batch.blocked_count or batch.skipped_count:
        batch.status = LegacyWizWizImportMessageBatch.Status.PARTIAL
    elif batch.sent_count:
        batch.status = LegacyWizWizImportMessageBatch.Status.SENT
    else:
        batch.status = LegacyWizWizImportMessageBatch.Status.FAILED
    if batch.status in {
        LegacyWizWizImportMessageBatch.Status.SENT,
        LegacyWizWizImportMessageBatch.Status.PARTIAL,
        LegacyWizWizImportMessageBatch.Status.FAILED,
    }:
        batch.sent_at = timezone.now()
    metadata = dict(batch.metadata or {})
    metadata["last_send"] = {
        "completed_at": timezone.now().isoformat(),
        "dry_run": bool(dry_run),
        "limit": limit,
        "rate_limited": rate_limited,
    }
    batch.metadata = metadata
    batch.save(update_fields=["status", "sent_at", "metadata", "updated_at"])
    counts["status"] = batch.status
    return counts
