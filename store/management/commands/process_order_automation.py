from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from store.models import Store
from store.order_automation import process_order_automation


class Command(BaseCommand):
    help = "Follow up receipt reviews and process explicitly enabled five-minute approvals."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--check-running", action="store_true", help="Read-only check of the scheduler heartbeat.")

    def handle(self, *args, **options):
        if options["check_running"]:
            stale = timezone.now() - timedelta(minutes=3)
            if Store.objects.filter(is_active=True).filter(Q(order_automation_last_run_at__isnull=True) | Q(order_automation_last_run_at__lt=stale)).exists():
                raise CommandError("Order automation heartbeat is missing or stale.")
            self.stdout.write("Order automation heartbeat is healthy.")
            return
        if options["limit"] < 1:
            raise CommandError("limit must be positive")
        summary = process_order_automation(limit=options["limit"])
        self.stdout.write("Order automation: " + " ".join(f"{key}={value}" for key, value in summary.items()))
