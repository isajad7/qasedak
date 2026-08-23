from django.core.management.base import BaseCommand, CommandError

from store.panel_health_services import (
    PanelHealthAlertService,
    check_all_panels_health,
    get_panels_for_health_check,
    safe_panel_alert_label,
)


class Command(BaseCommand):
    help = "Run admin-controlled panel health Telegram alerts."

    def add_arguments(self, parser):
        parser.add_argument("--panel-id", type=int, help="Only check this panel ID.")
        parser.add_argument("--dry-run", action="store_true", help="Do not write logs or send Telegram messages.")
        parser.add_argument("--no-send", action="store_true", help="Write health state but do not send Telegram messages.")
        parser.add_argument(
            "--force-test-message",
            action="store_true",
            help="Send one safe test message for the selected or first active panel to configured admins only.",
        )
        parser.add_argument("--limit", type=int, help="Maximum number of active panels to check.")
        parser.add_argument("--verbose", action="store_true", help="Print per-panel safe alert decisions.")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        no_send = bool(options["no_send"])
        panel_id = options.get("panel_id")
        limit = options.get("limit")

        if options["force_test_message"]:
            panels = list(get_panels_for_health_check(panel_id=panel_id, limit=limit))
            if panel_id is None:
                panels = [panel for panel in panels if panel.is_active]
            if not panels:
                raise CommandError("No matching active panel was found for the test message.")
            panel = panels[0]
            result = PanelHealthAlertService().send_test_message(panel, dry_run=dry_run, no_send=no_send)
            self.stdout.write(
                "Panel health alert test: "
                f"panel={panel.pk} "
                f"would_send={bool(result.get('would_send_alert'))} "
                f"sent={result.get('alert_sent_count', 0)} "
                f"failed={result.get('alert_failed_count', 0)} "
                f"skip_reason={result.get('alert_skip_reason') or '-'} "
                f"dry_run={dry_run} "
                f"no_send={no_send}"
            )
            if options.get("verbose"):
                self.stdout.write(f"test_panel={safe_panel_alert_label(panel)}")
            return

        summary = check_all_panels_health(
            send_alerts=True,
            dry_run=dry_run,
            no_send=no_send,
            panel_id=panel_id,
            limit=limit,
            active_only=True,
        )

        self.stdout.write(
            "Panel health alerts summary: "
            f"total_panels={summary['total_panels']} "
            f"checked={summary['checked']} "
            f"ok={summary['ok']} "
            f"warning={summary['warning']} "
            f"error={summary['error']} "
            f"disabled={summary['disabled']} "
            f"would_send={summary['would_send']} "
            f"alerts_sent={summary['alerts_sent']} "
            f"alerts_skipped={summary['alerts_skipped']} "
            f"failed={summary['failed']} "
            f"dry_run={summary['dry_run']} "
            f"no_send={no_send}"
        )

        if options.get("verbose"):
            for result in summary["results"]:
                self.stdout.write(
                    f"{result.get('status')}: panel={result.get('panel_id')} "
                    f"decision={result.get('alert_decision') or '-'} "
                    f"would_send={bool(result.get('would_send_alert'))} "
                    f"sent={result.get('alert_sent_count', 0)} "
                    f"skip_reason={result.get('alert_skip_reason') or '-'} "
                    f"consecutive_failures={result.get('consecutive_failure_count', 0)} "
                    f"error_code={result.get('error_code') or '-'} "
                    f"message={result.get('error_message') or result.get('summary') or '-'}"
                )
