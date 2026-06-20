from datetime import date

from django.core.management.base import BaseCommand, CommandError

from store.panel_usage_services import calculate_all_panels_daily_usage, format_bytes_fa


class Command(BaseCommand):
    help = "Calculate daily panel usage deltas from X-UI/Sanaei usage snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Usage date in YYYY-MM-DD format. Defaults to yesterday per panel timezone.")
        parser.add_argument("--timezone", default=None, help="IANA timezone, for example Asia/Tehran.")
        parser.add_argument("--dry-run", action="store_true", help="Calculate without writing PanelDailyUsage rows.")
        parser.add_argument("--panel-id", type=int, help="Only calculate this panel ID.")
        parser.add_argument("--verbose", action="store_true", help="Print per-panel results.")

    def handle(self, *args, **options):
        usage_date = None
        if options.get("date"):
            try:
                usage_date = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be in YYYY-MM-DD format.") from exc

        summary = calculate_all_panels_daily_usage(
            usage_date=usage_date,
            timezone=options.get("timezone"),
            save=not bool(options["dry_run"]),
            panel_id=options.get("panel_id"),
        )
        self.stdout.write(
            "Panel daily usage summary: "
            f"total_panels={summary['total_panels']} "
            f"calculated={summary['calculated']} "
            f"complete={summary['complete']} "
            f"partial={summary['partial']} "
            f"insufficient={summary['insufficient']} "
            f"estimated={summary['estimated']} "
            f"dry_run={summary['dry_run']}"
        )

        if options.get("verbose"):
            for usage in summary["results"]:
                self.stdout.write(
                    f"{usage.data_quality}: panel={usage.panel_id} "
                    f"name={usage.panel.name} "
                    f"date={usage.usage_date} "
                    f"used={format_bytes_fa(usage.used_bytes)} "
                    f"active_users={usage.active_users_count} "
                    f"start={usage.snapshot_start_at or '-'} "
                    f"end={usage.snapshot_end_at or '-'}"
                )
