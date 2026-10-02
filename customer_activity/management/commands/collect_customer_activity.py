import json
import os
from django.core.management.base import BaseCommand, CommandError
from customer_activity.models import ActivityCollector
from customer_activity.services import collect_activity, collector_healthy


class Command(BaseCommand):
    help = "Observe per-purchase VPN traffic; never send messages or modify panel clients."

    def add_arguments(self, parser):
        parser.add_argument("--check-running", action="store_true")
        parser.add_argument("--status", action="store_true", help="Read-only aggregate collection status; no customer identifiers.")

    def handle(self, *args, **options):
        if options["status"]:
            collector = ActivityCollector.objects.filter(pk=1).first()
            self.stdout.write(json.dumps({
                "healthy": collector_healthy(collector),
                "completed_at": collector.completed_at.isoformat() if collector and collector.completed_at else None,
                "summary": collector.summary if collector else {},
            }))
            return
        if options["check_running"]:
            if os.environ.get("QASEDAK_CUSTOMER_ACTIVITY_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
                self.stdout.write("Customer activity explicitly disabled.")
                return
            collector = ActivityCollector.objects.filter(pk=1).first()
            if not collector_healthy(collector):
                raise CommandError("Customer activity collector heartbeat is missing or stale.")
            self.stdout.write("Customer activity collector heartbeat is healthy.")
            return
        self.stdout.write(json.dumps(collect_activity(), ensure_ascii=False))
