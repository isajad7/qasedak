from django.core.management.base import BaseCommand

from store.panel_usage_services import collect_all_panel_usage_snapshots, cleanup_old_panel_usage_snapshots


class Command(BaseCommand):
    help = "Collect X-UI/Sanaei panel usage snapshots for daily usage reports."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Collect stats without writing snapshots.")
        parser.add_argument("--panel-id", type=int, help="Only collect usage for this panel ID.")
        parser.add_argument("--limit", type=int, help="Maximum number of panels to collect.")
        parser.add_argument("--cleanup", action="store_true", help="Delete old usage snapshots after collection.")
        parser.add_argument("--verbose", action="store_true", help="Print per-panel results.")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        summary = collect_all_panel_usage_snapshots(
            dry_run=dry_run,
            panel_id=options.get("panel_id"),
            limit=options.get("limit"),
        )
        cleanup_summary = {"deleted": 0}
        if options["cleanup"] and not dry_run:
            cleanup_summary = cleanup_old_panel_usage_snapshots()

        self.stdout.write(
            "Panel usage snapshot summary: "
            f"total_panels={summary['total_panels']} "
            f"checked={summary['checked']} "
            f"ok={summary['ok']} "
            f"partial={summary['partial']} "
            f"failed={summary['failed']} "
            f"skipped={summary['skipped']} "
            f"clients_snapshotted={summary['clients_snapshotted']} "
            f"online_clients={summary['online_clients']} "
            f"snapshots_created={summary['snapshots_created']} "
            f"cleanup_deleted={cleanup_summary.get('deleted', 0)} "
            f"dry_run={summary['dry_run']}"
        )

        if options.get("verbose"):
            for result in summary["results"]:
                self.stdout.write(
                    f"{result.get('status')}: panel={result.get('panel_id') or getattr(result.get('panel'), 'pk', '-')} "
                    f"name={result.get('panel_name') or getattr(result.get('panel'), 'name', '-')} "
                    f"clients={result.get('clients_snapshotted') or len(result.get('clients') or [])} "
                    f"online={result.get('online_clients_count', 0)} "
                    f"snapshot_id={result.get('snapshot_id', '-')} "
                    f"error={result.get('error_message') or '-'}"
                )
