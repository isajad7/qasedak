from django.core.management.base import BaseCommand

from store.renewal_reminder_services import run_renewal_reminders


class Command(BaseCommand):
    help = "Send Telegram renewal and low-traffic reminders for VPN clients."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not send messages or write reminder logs.")
        parser.add_argument("--limit", type=int, help="Maximum number of VPN clients to scan.")
        parser.add_argument("--customer-id", type=int, help="Only scan VPN clients for this customer ID.")
        parser.add_argument("--client-id", type=int, help="Only scan this VPNClient ID.")
        parser.add_argument(
            "--type",
            choices=("expiry", "traffic", "all"),
            default="all",
            help="Reminder type to send.",
        )
        parser.add_argument("--verbose", action="store_true", help="Print per-reminder results.")

    def handle(self, *args, **options):
        summary = run_renewal_reminders(
            dry_run=options["dry_run"],
            limit=options.get("limit"),
            customer_id=options.get("customer_id"),
            client_id=options.get("client_id"),
            reminder_type=options.get("type") or "all",
        )

        self.stdout.write(
            "Renewal reminders summary: "
            f"total_clients_seen={summary['total_clients_seen']} "
            f"ignored_before_start_at={summary['ignored_before_start_at']} "
            f"candidates={summary['candidates']} "
            f"due={summary['due']} "
            f"sent={summary['sent']} "
            f"skipped={summary['skipped']} "
            f"failed={summary['failed']} "
            f"would_send={summary['would_send']} "
            f"dry_run={summary['dry_run']}"
        )

        if options.get("verbose"):
            for result in summary["results"]:
                self.stdout.write(
                    f"{result.status}: vpn_client={result.vpn_client_id} "
                    f"customer={result.customer_id or '-'} "
                    f"type={result.reminder_type} trigger={result.trigger_key} "
                    f"telegram_id={result.telegram_id or '-'} "
                    f"message={result.error or result.message or '-'}"
                )
