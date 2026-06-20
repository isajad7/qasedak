from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone as django_timezone

from .jalali import persian_digits
from .models import Panel, PanelClientUsageSnapshot, PanelDailyUsage, PanelUsageSnapshot, Store
from .xui_api import collect_panel_usage_stats


SNAPSHOT_BOUNDARY_TOLERANCE = timedelta(hours=3)
MIN_WEEK_AVERAGE_DAYS = 3


def _positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _timezone_name(panel=None, timezone=None):
    timezone_name = str(timezone or "").strip()
    if timezone_name:
        return timezone_name
    store = getattr(panel, "store", None)
    timezone_name = str(getattr(store, "daily_admin_report_timezone", "") or "").strip()
    return timezone_name or "Asia/Tehran"


def _zoneinfo(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Tehran")


def _parse_usage_date(value, *, timezone_name):
    if isinstance(value, datetime):
        return django_timezone.localtime(value, _zoneinfo(timezone_name)).date()
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return django_timezone.localtime(django_timezone.now(), _zoneinfo(timezone_name)).date() - timedelta(days=1)


def _usage_period(usage_date, timezone_name):
    usage_date = _parse_usage_date(usage_date, timezone_name=timezone_name)
    tz = _zoneinfo(timezone_name)
    period_start = datetime.combine(usage_date, time.min, tzinfo=tz)
    period_end = period_start + timedelta(days=1)
    return usage_date, period_start, period_end


def _valid_snapshots(panel):
    return PanelUsageSnapshot.objects.filter(
        panel=panel,
        status__in=[PanelUsageSnapshot.Status.OK, PanelUsageSnapshot.Status.PARTIAL],
    )


def _snapshot_distance(snapshot, target):
    if not snapshot:
        return None
    return abs((snapshot.captured_at - target).total_seconds())


def _nearest_start_snapshot(panel, period_start, period_end):
    snapshots = _valid_snapshots(panel)
    before = (
        snapshots.filter(
            captured_at__lte=period_start,
            captured_at__gte=period_start - SNAPSHOT_BOUNDARY_TOLERANCE,
        )
        .order_by("-captured_at")
        .first()
    )
    after = (
        snapshots.filter(
            captured_at__gte=period_start,
            captured_at__lte=period_start + SNAPSHOT_BOUNDARY_TOLERANCE,
        )
        .order_by("captured_at")
        .first()
    )
    candidates = [snapshot for snapshot in (before, after) if snapshot]
    if candidates:
        return min(candidates, key=lambda snapshot: _snapshot_distance(snapshot, period_start)), "near_boundary"
    return (
        snapshots.filter(captured_at__gte=period_start, captured_at__lt=period_end)
        .order_by("captured_at")
        .first(),
        "first_inside_day",
    )


def _nearest_end_snapshot(panel, period_start, period_end):
    snapshots = _valid_snapshots(panel)
    near_end = (
        snapshots.filter(
            captured_at__lte=period_end,
            captured_at__gte=period_end - SNAPSHOT_BOUNDARY_TOLERANCE,
        )
        .order_by("-captured_at")
        .first()
    )
    if near_end:
        return near_end, "near_boundary"
    return (
        snapshots.filter(captured_at__gte=period_start, captured_at__lt=period_end)
        .order_by("-captured_at")
        .first(),
        "last_inside_day",
    )


def _panel_active_user_method(panel):
    store = getattr(panel, "store", None)
    method = getattr(store, "panel_usage_active_user_method", None) or Store.PanelUsageActiveUserMethod.MIXED
    if method not in Store.PanelUsageActiveUserMethod.values:
        return Store.PanelUsageActiveUserMethod.MIXED
    return method


def save_panel_usage_snapshot(panel, result):
    captured_at = result.get("captured_at") or django_timezone.now()
    clients = list(result.get("clients") or [])
    with transaction.atomic():
        snapshot = PanelUsageSnapshot.objects.create(
            panel=panel,
            captured_at=captured_at,
            status=result.get("status") or PanelUsageSnapshot.Status.FAILED,
            total_upload_bytes=int(result.get("total_upload_bytes") or 0),
            total_download_bytes=int(result.get("total_download_bytes") or 0),
            total_used_bytes=int(result.get("total_used_bytes") or 0),
            clients_count=int(result.get("clients_count") or len(clients)),
            online_clients_count=int(result.get("online_clients_count") or 0),
            active_inbounds_count=int(result.get("active_inbounds_count") or 0),
            checked_inbounds_count=int(result.get("checked_inbounds_count") or 0),
            error_message=str(result.get("error_message") or "")[:4000],
            metadata=result.get("metadata") or {},
        )
        PanelClientUsageSnapshot.objects.bulk_create(
            [
                PanelClientUsageSnapshot(
                    panel=panel,
                    inbound_id=client.get("inbound_id"),
                    captured_at=captured_at,
                    client_identifier_hash=client["identifier_hash"],
                    client_identifier_masked=client.get("identifier_masked") or "",
                    email_masked=client.get("email_masked") or "",
                    upload_bytes=int(client.get("upload_bytes") or 0),
                    download_bytes=int(client.get("download_bytes") or 0),
                    used_bytes=int(client.get("used_bytes") or 0),
                    total_bytes=client.get("total_bytes"),
                    expiry_time=client.get("expiry_time"),
                    enabled=client.get("enabled"),
                    online=client.get("online"),
                    source=client.get("source") or "",
                    metadata=client.get("metadata") or {},
                )
                for client in clients
                if client.get("identifier_hash")
            ],
            batch_size=1000,
        )
    result["snapshot_id"] = snapshot.pk
    result["clients_snapshotted"] = len(clients)
    return snapshot


def collect_panel_usage_snapshot(panel, dry_run=False):
    result = collect_panel_usage_stats(panel)
    result["dry_run"] = bool(dry_run)
    if dry_run:
        result["clients_snapshotted"] = len(result.get("clients") or [])
        result["snapshots_created"] = 0
        return result
    snapshot = save_panel_usage_snapshot(panel, result)
    result["snapshots_created"] = 1
    result["snapshot_id"] = snapshot.pk
    return result


def collect_all_panel_usage_snapshots(dry_run=False, panel_id=None, limit=None):
    panels = Panel.objects.filter(is_active=True).select_related("store").order_by("pk")
    if panel_id:
        panels = panels.filter(pk=panel_id)
    if limit:
        panels = panels[: max(int(limit), 0)]

    summary = {
        "total_panels": len(panels),
        "checked": 0,
        "ok": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "clients_snapshotted": 0,
        "online_clients": 0,
        "snapshots_created": 0,
        "dry_run": bool(dry_run),
        "results": [],
    }
    for panel in panels:
        try:
            result = collect_panel_usage_snapshot(panel, dry_run=dry_run)
        except Exception as exc:
            result = {
                "panel_id": panel.pk,
                "panel_name": panel.name,
                "status": PanelUsageSnapshot.Status.FAILED,
                "error_message": str(exc),
                "clients_snapshotted": 0,
                "online_clients_count": 0,
                "snapshots_created": 0,
            }
        status = result.get("status") or PanelUsageSnapshot.Status.FAILED
        if status in summary:
            summary[status] += 1
        if status != PanelUsageSnapshot.Status.SKIPPED:
            summary["checked"] += 1
        summary["clients_snapshotted"] += int(result.get("clients_snapshotted") or len(result.get("clients") or []))
        summary["online_clients"] += int(result.get("online_clients_count") or 0)
        summary["snapshots_created"] += int(result.get("snapshots_created") or 0)
        summary["results"].append(result)
    return summary


def cleanup_old_panel_usage_snapshots(retention_days=None):
    now = django_timezone.now()
    deleted = 0
    details = []

    def delete_for_queryset(panel_filter, cutoff, store_id):
        snapshot_count, _snapshot_details = PanelUsageSnapshot.objects.filter(
            panel_filter,
            captured_at__lt=cutoff,
        ).delete()
        client_count, _client_details = PanelClientUsageSnapshot.objects.filter(
            panel_filter,
            captured_at__lt=cutoff,
        ).delete()
        details.append(
            {
                "store_id": store_id,
                "cutoff": cutoff.isoformat(),
                "snapshots_deleted": snapshot_count,
                "client_snapshots_deleted": client_count,
            }
        )
        return snapshot_count + client_count

    if retention_days is not None:
        cutoff = now - timedelta(days=_positive_int(retention_days, 45))
        deleted += delete_for_queryset(models_q_all(), cutoff, None)
    else:
        for store in Store.objects.order_by("pk"):
            cutoff = now - timedelta(days=_positive_int(store.panel_usage_snapshot_retention_days, 45))
            deleted += delete_for_queryset(models_q_store(store), cutoff, store.pk)
        cutoff = now - timedelta(days=45)
        deleted += delete_for_queryset(models_q_orphan_store(), cutoff, None)

    return {"deleted": deleted, "details": details}


def models_q_all():
    from django.db.models import Q

    return Q()


def models_q_store(store):
    from django.db.models import Q

    return Q(panel__store=store)


def models_q_orphan_store():
    from django.db.models import Q

    return Q(panel__store__isnull=True)


def _client_used_map(panel, captured_at):
    rows = PanelClientUsageSnapshot.objects.filter(panel=panel, captured_at=captured_at).values_list(
        "client_identifier_hash",
        "used_bytes",
    )
    return {identifier_hash: int(used_bytes or 0) for identifier_hash, used_bytes in rows}


def _traffic_delta_active_users(panel, start_snapshot, end_snapshot):
    start_usage = _client_used_map(panel, start_snapshot.captured_at)
    end_usage = _client_used_map(panel, end_snapshot.captured_at)
    return {
        identifier_hash
        for identifier_hash, end_used in end_usage.items()
        if end_used > start_usage.get(identifier_hash, 0)
    }


def _online_active_users(panel, period_start, period_end):
    return set(
        PanelClientUsageSnapshot.objects.filter(
            panel=panel,
            captured_at__gte=period_start,
            captured_at__lt=period_end,
            online=True,
        )
        .values_list("client_identifier_hash", flat=True)
        .distinct()
    )


def _has_online_snapshot_data(panel, period_start, period_end):
    return PanelClientUsageSnapshot.objects.filter(
        panel=panel,
        captured_at__gte=period_start,
        captured_at__lt=period_end,
        online__isnull=False,
    ).exists()


def _daily_usage_instance(panel, usage_date, timezone_name, **values):
    return PanelDailyUsage(panel=panel, usage_date=usage_date, timezone=timezone_name, **values)


def _persist_daily_usage(instance):
    defaults = {
        "upload_bytes": instance.upload_bytes,
        "download_bytes": instance.download_bytes,
        "used_bytes": instance.used_bytes,
        "active_users_count": instance.active_users_count,
        "online_users_count": instance.online_users_count,
        "clients_count_start": instance.clients_count_start,
        "clients_count_end": instance.clients_count_end,
        "snapshot_start_at": instance.snapshot_start_at,
        "snapshot_end_at": instance.snapshot_end_at,
        "data_quality": instance.data_quality,
        "metadata": instance.metadata,
    }
    obj, _created = PanelDailyUsage.objects.update_or_create(
        panel=instance.panel,
        usage_date=instance.usage_date,
        timezone=instance.timezone,
        defaults=defaults,
    )
    return obj


def calculate_panel_daily_usage(panel, usage_date, timezone=None, save=True):
    timezone_name = _timezone_name(panel, timezone)
    usage_date, period_start, period_end = _usage_period(usage_date, timezone_name)
    start_snapshot, start_source = _nearest_start_snapshot(panel, period_start, period_end)
    end_snapshot, end_source = _nearest_end_snapshot(panel, period_start, period_end)
    method = _panel_active_user_method(panel)
    warnings = []
    metadata = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "timezone": timezone_name,
        "active_user_method": method,
        "warnings": warnings,
        "snapshot_start_source": start_source,
        "snapshot_end_source": end_source,
    }

    if not start_snapshot or not end_snapshot:
        warnings.append("snapshot کافی برای محاسبه مصرف روزانه وجود ندارد.")
        instance = _daily_usage_instance(
            panel,
            usage_date,
            timezone_name,
            upload_bytes=0,
            download_bytes=0,
            used_bytes=0,
            active_users_count=0,
            online_users_count=0,
            clients_count_start=getattr(start_snapshot, "clients_count", 0) or 0,
            clients_count_end=getattr(end_snapshot, "clients_count", 0) or 0,
            snapshot_start_at=getattr(start_snapshot, "captured_at", None),
            snapshot_end_at=getattr(end_snapshot, "captured_at", None),
            data_quality=PanelDailyUsage.DataQuality.INSUFFICIENT,
            metadata=metadata,
        )
        return _persist_daily_usage(instance) if save else instance

    if start_snapshot.pk == end_snapshot.pk or start_snapshot.captured_at >= end_snapshot.captured_at:
        warnings.append("snapshot ابتدا و انتهای روز یکسان یا نامعتبر است.")
        instance = _daily_usage_instance(
            panel,
            usage_date,
            timezone_name,
            upload_bytes=0,
            download_bytes=0,
            used_bytes=0,
            active_users_count=0,
            online_users_count=0,
            clients_count_start=start_snapshot.clients_count,
            clients_count_end=end_snapshot.clients_count,
            snapshot_start_at=start_snapshot.captured_at,
            snapshot_end_at=end_snapshot.captured_at,
            data_quality=PanelDailyUsage.DataQuality.INSUFFICIENT,
            metadata=metadata,
        )
        return _persist_daily_usage(instance) if save else instance

    if start_snapshot.status != PanelUsageSnapshot.Status.OK or end_snapshot.status != PanelUsageSnapshot.Status.OK:
        warnings.append("یکی از snapshotهای مبنا partial است.")
    if start_source != "near_boundary":
        warnings.append("snapshot دقیق ابتدای روز پیدا نشد و اولین snapshot داخل روز استفاده شد.")
    if end_source != "near_boundary":
        warnings.append("snapshot نزدیک پایان روز پیدا نشد و آخرین snapshot داخل روز استفاده شد.")

    upload_delta = int(end_snapshot.total_upload_bytes or 0) - int(start_snapshot.total_upload_bytes or 0)
    download_delta = int(end_snapshot.total_download_bytes or 0) - int(start_snapshot.total_download_bytes or 0)
    used_delta = int(end_snapshot.total_used_bytes or 0) - int(start_snapshot.total_used_bytes or 0)
    raw_deltas = {
        "upload_bytes": upload_delta,
        "download_bytes": download_delta,
        "used_bytes": used_delta,
    }
    metadata["raw_deltas"] = raw_deltas
    if any(value < 0 for value in raw_deltas.values()):
        warnings.append("delta مصرف منفی است؛ احتمال reset آمار X-UI یا تغییر پنل وجود دارد.")
        upload_delta = max(upload_delta, 0)
        download_delta = max(download_delta, 0)
        used_delta = max(used_delta, 0)

    traffic_active = set()
    online_active = _online_active_users(panel, period_start, period_end)
    if method in {Store.PanelUsageActiveUserMethod.TRAFFIC_DELTA, Store.PanelUsageActiveUserMethod.MIXED}:
        traffic_active = _traffic_delta_active_users(panel, start_snapshot, end_snapshot)
        if end_snapshot.clients_count and not PanelClientUsageSnapshot.objects.filter(
            panel=panel,
            captured_at=end_snapshot.captured_at,
        ).exists():
            warnings.append("snapshotهای client برای محاسبه active users کافی نیست.")
    if method in {Store.PanelUsageActiveUserMethod.ONLINE_API, Store.PanelUsageActiveUserMethod.MIXED}:
        if not _has_online_snapshot_data(panel, period_start, period_end):
            warnings.append("داده online در snapshotهای این روز موجود نیست.")

    if method == Store.PanelUsageActiveUserMethod.TRAFFIC_DELTA:
        active_users = traffic_active
    elif method == Store.PanelUsageActiveUserMethod.ONLINE_API:
        active_users = online_active
    else:
        active_users = traffic_active | online_active

    data_quality = PanelDailyUsage.DataQuality.COMPLETE if not warnings else PanelDailyUsage.DataQuality.PARTIAL
    instance = _daily_usage_instance(
        panel,
        usage_date,
        timezone_name,
        upload_bytes=upload_delta,
        download_bytes=download_delta,
        used_bytes=used_delta,
        active_users_count=len(active_users),
        online_users_count=len(online_active),
        clients_count_start=start_snapshot.clients_count,
        clients_count_end=end_snapshot.clients_count,
        snapshot_start_at=start_snapshot.captured_at,
        snapshot_end_at=end_snapshot.captured_at,
        data_quality=data_quality,
        metadata=metadata,
    )
    return _persist_daily_usage(instance) if save else instance


def calculate_all_panels_daily_usage(usage_date=None, timezone=None, save=True, panel_id=None):
    panels = Panel.objects.filter(is_active=True).select_related("store").order_by("pk")
    if panel_id:
        panels = panels.filter(pk=panel_id)
    summary = {
        "total_panels": panels.count(),
        "calculated": 0,
        "complete": 0,
        "partial": 0,
        "insufficient": 0,
        "estimated": 0,
        "dry_run": not save,
        "results": [],
    }
    for panel in panels:
        usage = calculate_panel_daily_usage(panel, usage_date, timezone=timezone, save=save)
        summary["calculated"] += 1
        if usage.data_quality in summary:
            summary[usage.data_quality] += 1
        summary["results"].append(usage)
    return summary


def _get_or_calculate_daily_usage(panel, usage_date, timezone_name, *, save=True):
    usage = PanelDailyUsage.objects.filter(panel=panel, usage_date=usage_date, timezone=timezone_name).first()
    if usage:
        return usage
    return calculate_panel_daily_usage(panel, usage_date, timezone=timezone_name, save=save)


def get_panel_usage_comparison(panel, report_date, timezone=None, *, save=True):
    timezone_name = _timezone_name(panel, timezone)
    report_date = _parse_usage_date(report_date, timezone_name=timezone_name)
    current = _get_or_calculate_daily_usage(panel, report_date, timezone_name, save=save)
    previous_date = report_date - timedelta(days=1)
    previous = _get_or_calculate_daily_usage(panel, previous_date, timezone_name, save=save)
    week_usages = [
        _get_or_calculate_daily_usage(panel, report_date - timedelta(days=offset), timezone_name, save=save)
        for offset in range(1, 8)
    ]
    valid_week = [
        usage
        for usage in week_usages
        if usage.data_quality != PanelDailyUsage.DataQuality.INSUFFICIENT
    ]
    week_average = None
    if valid_week:
        week_average = sum(int(usage.used_bytes or 0) for usage in valid_week) // len(valid_week)
    warnings = []
    if len(valid_week) < MIN_WEEK_AVERAGE_DAYS:
        warnings.append("داده کافی برای میانگین ۷ روز قبل وجود ندارد.")

    return {
        "panel": panel,
        "current": current,
        "previous": previous,
        "week_usages": week_usages,
        "week_average_used_bytes": week_average,
        "week_average_days": len(valid_week),
        "warnings": warnings,
        "previous_change": format_percent_change(current.used_bytes, previous.used_bytes),
        "week_change": format_percent_change(current.used_bytes, week_average),
    }


def get_all_panels_usage_comparison(report_date, timezone=None, store=None, *, save=True):
    panels = Panel.objects.filter(is_active=True).select_related("store").order_by("name", "pk")
    if store is not None:
        panels = panels.filter(store=store)
    rows = [get_panel_usage_comparison(panel, report_date, timezone=timezone, save=save) for panel in panels]
    return sorted(
        rows,
        key=lambda row: (
            row["current"].data_quality == PanelDailyUsage.DataQuality.INSUFFICIENT,
            -int(row["current"].used_bytes or 0),
            row["panel"].name,
        ),
    )


def format_bytes_fa(bytes_value):
    if bytes_value is None:
        return "نامشخص"
    value = float(bytes_value or 0)
    sign = "-" if value < 0 else ""
    value = abs(value)
    units = [
        (1024**4, "ترابایت"),
        (1024**3, "گیگ"),
        (1024**2, "مگ"),
        (1024, "کیلوبایت"),
    ]
    for factor, label in units:
        if value >= factor:
            number = value / factor
            rendered = f"{number:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{persian_digits(rendered)} {label}"
    return f"{sign}{persian_digits(int(value))} بایت"


def format_percent_change(current, previous):
    current = int(current or 0)
    if previous is None:
        return "نامشخص"
    previous = int(previous or 0)
    if previous <= 0:
        if current > 0:
            return "🔼 جدید"
        return "➖ ۰٪"
    percent = ((current - previous) / previous) * 100
    if abs(percent) < 0.1:
        return "➖ ۰٪"
    icon = "🔼" if percent > 0 else "🔽"
    sign = "+" if percent > 0 else ""
    rendered = f"{sign}{percent:.1f}".rstrip("0").rstrip(".")
    return f"{icon} {persian_digits(rendered)}٪"
