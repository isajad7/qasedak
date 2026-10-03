import json
import os
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from customer_activity.models import ActivityCollector, OutreachSettings, PurchaseActivity
from customer_activity.outreach import personal_target, run_outreach
from customer_activity.services import MAX_GAP, collector_healthy


class Command(BaseCommand):
    help = "Run purchase-bound journeys in their configured mode; preview by default."

    def add_arguments(self, parser):
        parser.add_argument("--store", type=int)
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--check-running", action="store_true")
        parser.add_argument("--set-mode", choices=["off", "preview", "live"])
        parser.add_argument("--activate-initial", action="store_true", help="Activate an untouched preview once, after a healthy rollout.")
        parser.add_argument("--only-active-store", action="store_true", help="Require exactly one active store for initial activation; never choose the first of several.")

    def handle(self, *args, **options):
        if options["activate_initial"]:
            if bool(options["store"]) == bool(options["only_active_store"]) or options["set_mode"]:
                raise CommandError("Initial activation requires --store or --only-active-store exclusively.")
            store_id = options["store"]
            if options["only_active_store"]:
                from store.models import Store
                ids = list(Store.objects.filter(is_active=True).values_list("pk", flat=True)[:2])
                if len(ids) != 1:
                    raise CommandError("Exactly one active store is required; use a reviewed explicit --store otherwise.")
                store_id = ids[0]
            self.stdout.write(json.dumps(self.activate_initial(store_id)))
            return
        if options["only_active_store"]:
            raise CommandError("--only-active-store is only supported with --activate-initial.")
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

    @transaction.atomic
    def activate_initial(self, store_id):
        config = OutreachSettings.objects.select_for_update().filter(store_id=store_id, store__is_active=True).first()
        if not config:
            raise CommandError("Active store with an existing preview is required.")
        # A later owner decision or previous activation is never overridden by deployment.
        if config.activated_at or config.changed_by_id or config.mode != "preview":
            return {"store_id": store_id, "mode": config.mode, "activation": "preserved_existing_choice"}
        for flag in ("QASEDAK_CUSTOMER_ACTIVITY_ENABLED", "QASEDAK_CUSTOMER_OUTREACH_ENABLED"):
            if os.environ.get(flag, "true").lower() in {"0", "false", "no", "off"}:
                raise CommandError("The activity or outreach scheduler is explicitly disabled.")
        now = timezone.now()
        collector = ActivityCollector.objects.filter(pk=1).first()
        if (not collector_healthy(collector, now=now) or not collector.completed_at
                or not timedelta(0) <= now - collector.completed_at <= MAX_GAP
                or not config.last_run_at or not timedelta(0) <= now - config.last_run_at <= MAX_GAP):
            raise CommandError("Fresh completed collection and preview runs are required.")
        purchases = PurchaseActivity.objects.filter(order__store_id=store_id, order__status__in=("completed", "confirmed"),
            order__verification_status="verified", reason="ok", observed_at__gte=now - MAX_GAP,
            observed_at__lte=now).select_related("order")
        if not any(personal_target(activity.order) for activity in purchases):
            raise CommandError("No fully observed purchase with a scoped private Telegram recipient.")
        config.mode, config.activated_at = "live", now
        config.save(update_fields=["mode", "activated_at", "updated_at"])
        return {"store_id": store_id, "mode": "live", "activation": "initial_rollout"}
