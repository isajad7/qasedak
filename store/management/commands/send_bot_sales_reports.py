from django.core.management.base import BaseCommand

from store.bots import send_due_sales_reports


class Command(BaseCommand):
    help = "Send due admin bot sales reports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send reports even if the configured interval has not passed.",
        )

    def handle(self, *args, **options):
        sent = send_due_sales_reports(force=options["force"])
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} bot sales report(s)."))
