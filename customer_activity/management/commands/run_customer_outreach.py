import json
import os
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from customer_activity.models import OutreachSettings
from customer_activity.outreach import run_outreach


class Command(BaseCommand):
    help = "Run purchase-bound journeys in their configured mode; preview by default."

    def add_arguments(self, parser):
        parser.add_argument("--store", type=int)
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--check-running", action="store_true")
        parser.add_argument("--set-mode", choices=["off", "preview", "live"])

    def handle(self, *args, **options):
        if options["check_running"]:
            if os.environ.get("QASEDAK_CUSTOMER_OUTREACH_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
                self.stdout.write("Purchase outreach explicitly disabled.")
                return
            from store.models import Store
            active = Store.objects.filter(is_active=True)
            fresh = OutreachSettings.objects.filter(last_run_at__gte=timezone.now() - timedelta(minutes=45))
            if active.exclude(pk__in=fresh.values("store_id")).exists():
                raise CommandError("Purchase outreach heartbeat is missing or stale.")
            self.stdout.write("Purchase outreach heartbeat is healthy.")
            return
        if options["set_mode"]:
            if not options["store"]:
                raise CommandError("An explicit --store is required to change delivery mode.")
            from store.models import Store
            store = Store.objects.filter(pk=options["store"], is_active=True).first()
            if not store:
                raise CommandError("Active store not found.")
            config, _ = OutreachSettings.objects.get_or_create(store=store)
            config.mode = options["set_mode"]
            if config.mode == "live" and not config.activated_at:
                config.activated_at = timezone.now()
            config.save()
            self.stdout.write(json.dumps({"store_id": store.pk, "mode": config.mode}))
            return
        if options["status"]:
            query = OutreachSettings.objects.all()
            if options["store"]:
                query = query.filter(store_id=options["store"])
            self.stdout.write(json.dumps(list(query.values("store_id", "mode", "last_run_at", "summary")), default=str))
            return
        if os.environ.get("QASEDAK_CUSTOMER_OUTREACH_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
            self.stdout.write("Purchase outreach explicitly disabled.")
            return
        self.stdout.write(json.dumps(run_outreach(store_id=options["store"])))
