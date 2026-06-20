from django.core.management.base import BaseCommand

from store.panel_health_services import check_all_panels_health, cleanup_old_panel_health_logs


class Command(BaseCommand):
    help = "Check X-UI/Sanaei panel health and optionally send Telegram admin alerts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not write logs or send alerts.")
        parser.add_argument("--send-alerts", action="store_true", help="Send transition/cooldown alerts to Telegram admins.")
        parser.add_argument("--panel-id", type=int, help="Only check this panel ID.")
        parser.add_argument("--limit", type=int, help="Maximum number of panels to check.")
        parser.add_argument("--cleanup", action="store_true", help="Delete old panel health logs after checks.")
        parser.add_argument("--verbose", action="store_true", help="Print per-panel results.")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        summary = check_all_panels_health(
            send_alerts=bool(options["send_alerts"]) and not dry_run,
            dry_run=dry_run,
            panel_id=options.get("panel_id"),
            limit=options.get("limit"),
        )
        cleanup_summary = {"deleted": 0}
        if options["cleanup"] and not dry_run:
            cleanup_summary = cleanup_old_panel_health_logs()

        self.stdout.write(
            "Panel health summary: "
            f"total_panels={summary['total_panels']} "
            f"checked={summary['checked']} "
            f"ok={summary['ok']} "
            f"warning={summary['warning']} "
            f"error={summary['error']} "
            f"disabled={summary['disabled']} "
            f"alerts_sent={summary['alerts_sent']} "
            f"alerts_skipped={summary['alerts_skipped']} "
            f"failed={summary['failed']} "
            f"cleanup_deleted={cleanup_summary.get('deleted', 0)} "
            f"dry_run={summary['dry_run']}"
        )

        if options.get("verbose"):
            for result in summary["results"]:
                self.stdout.write(
                    f"{result.get('status')}: panel={result.get('panel_id')} "
                    f"name={result.get('panel_name')} "
                    f"response_ms={result.get('response_time_ms') if result.get('response_time_ms') is not None else '-'} "
                    f"alert_sent={result.get('alert_sent')} "
                    f"summary={result.get('summary') or result.get('error_message') or '-'}"
                )
