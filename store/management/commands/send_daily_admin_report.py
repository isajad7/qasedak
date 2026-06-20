from datetime import date

from django.core.management.base import BaseCommand, CommandError

from store.daily_report_services import send_daily_admin_report


class Command(BaseCommand):
    help = "Send the daily Telegram admin operations report."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print report text without sending or logging.")
        parser.add_argument("--date", help="Report date in YYYY-MM-DD format. Defaults to yesterday in the store timezone.")
        parser.add_argument("--force", action="store_true", help="Send again even if this report date was already sent.")
        parser.add_argument("--verbose", action="store_true", help="Print per-store report details.")

    def handle(self, *args, **options):
        report_date = None
        if options.get("date"):
            try:
                report_date = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be in YYYY-MM-DD format.") from exc

        summary = send_daily_admin_report(
            report_date=report_date,
            dry_run=options["dry_run"],
            force=options["force"],
        )
        self.stdout.write(
            "Daily admin report summary: "
            f"total_stores={summary['total_stores']} "
            f"sent={summary['sent']} "
            f"skipped={summary['skipped']} "
            f"failed={summary['failed']} "
            f"would_send={summary['would_send']} "
            f"dry_run={summary['dry_run']} "
            f"force={summary['force']}"
        )

        if options.get("verbose"):
            for report in summary["reports"]:
                self.stdout.write(
                    f"{report.get('status')}: store={report.get('store_id')} "
                    f"date={report.get('report_date', '-')} "
                    f"sent={report.get('sent', 0)} failed={report.get('failed', 0)} "
                    f"reason={report.get('reason', '-')}"
                )
                if options["dry_run"] and report.get("message"):
                    self.stdout.write(report["message"])
