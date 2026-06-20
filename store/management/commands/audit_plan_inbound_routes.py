from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from django.db.models import Q

from store.models import Inbound, Plan, PlanInboundRoute, Store


class Command(BaseCommand):
    help = "Audit PlanInboundRoute coverage and optionally seed safe default routes."

    def add_arguments(self, parser):
        parser.add_argument("--fix-default", action="store_true", help="Create a general route for missing plans when safe.")
        parser.add_argument("--store-id", type=int, help="Limit audit to one store.")
        parser.add_argument("--plan-id", type=int, help="Limit audit to one plan.")
        parser.add_argument("--dry-run", action="store_true", help="Show fixes without writing them.")

    def handle(self, *args, **options):
        self.fix_default = bool(options["fix_default"])
        self.dry_run = bool(options["dry_run"])
        self.plan_id = options.get("plan_id")

        stores = Store.objects.filter(is_active=True).order_by("pk")
        if options.get("store_id"):
            stores = stores.filter(pk=options["store_id"])
        stores = list(stores)
        if not stores:
            self.stdout.write(self.style.WARNING("No active store matched the audit scope."))
            return

        total_missing = 0
        total_invalid = 0
        total_created = 0
        for store in stores:
            missing, invalid, created = self.audit_store(store)
            total_missing += missing
            total_invalid += invalid
            total_created += created

        self.stdout.write(
            self.style.SUCCESS(
                f"Summary: stores={len(stores)} missing_routes={total_missing} "
                f"invalid_routes={total_invalid} created_routes={total_created} dry_run={self.dry_run}"
            )
        )

    def plans_for_store(self, store):
        plans = Plan.objects.filter(
            Q(store=store) | Q(store__isnull=True),
            is_active=True,
        ).order_by("sort_order", "price", "pk")
        if self.plan_id:
            plans = plans.filter(pk=self.plan_id)
        return plans

    def routes_for_store(self, store):
        return PlanInboundRoute.objects.filter(
            Q(store=store) | Q(store__isnull=True),
            Q(inbound__panel__store=store) | Q(inbound__panel__store__isnull=True),
        )

    def healthy_inbounds_for_store(self, store):
        return Inbound.objects.filter(
            Q(panel__store=store) | Q(panel__store__isnull=True),
            is_active=True,
            available_for_new_orders=True,
            panel__is_active=True,
        ).select_related("panel").order_by("pk")

    def route_is_valid(self, route, store):
        try:
            route.full_clean()
        except ValidationError as exc:
            return False, "; ".join(exc.messages)
        inbound = route.inbound
        if inbound.panel.store_id and inbound.panel.store_id != store.pk:
            return False, "Inbound belongs to a different store."
        if inbound.max_clients is not None and inbound.current_users >= inbound.max_clients:
            return False, "Inbound capacity is full."
        return True, ""

    def has_valid_general_route(self, plan, store):
        routes = (
            self.routes_for_store(store)
            .filter(plan=plan, operator__isnull=True, is_active=True)
            .select_related("store", "plan", "operator", "inbound", "inbound__panel")
        )
        for route in routes:
            valid, _message = self.route_is_valid(route, store)
            if valid:
                return True
        return False

    def audit_store(self, store):
        self.stdout.write(f"Store #{store.pk} {store.name}")
        self.stdout.write("  Bulk Assign tool: Admin > Plan Inbound Routes > Bulk Assign")
        plans = list(self.plans_for_store(store))
        routes = list(
            self.routes_for_store(store)
            .filter(is_active=True)
            .select_related("store", "plan", "operator", "inbound", "inbound__panel")
            .order_by("plan_id", "operator_id", "priority", "pk")
        )
        self.stdout.write(f"  active_plans={len(plans)} active_routes={len(routes)}")

        invalid_count = 0
        for route in routes:
            valid, message = self.route_is_valid(route, store)
            if valid:
                continue
            invalid_count += 1
            self.stdout.write(
                self.style.ERROR(
                    f"  INVALID route #{route.pk}: plan={route.plan_id} "
                    f"operator={route.operator_id or '-'} inbound={route.inbound_id} reason={message}"
                )
            )

        missing_plans = [plan for plan in plans if not self.has_valid_general_route(plan, store)]
        if missing_plans:
            self.stdout.write(self.style.WARNING(f"  missing_general_routes={len(missing_plans)}"))
            for plan in missing_plans:
                self.stdout.write(f"    - plan #{plan.pk} {plan.name}")
            self.stdout.write(
                self.style.WARNING(
                    "  برای تنظیم گروهی از Admin > Plan Inbound Routes > Bulk Assign استفاده کنید."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("  all active plans have a valid general route."))

        created_count = 0
        if self.fix_default and missing_plans:
            created_count = self.fix_default_routes(store, missing_plans)

        return len(missing_plans), invalid_count, created_count

    def fix_default_routes(self, store, missing_plans):
        healthy_inbounds = list(self.healthy_inbounds_for_store(store))
        if len(healthy_inbounds) != 1:
            self.stdout.write(
                self.style.WARNING(
                    f"  --fix-default skipped: healthy_inbounds={len(healthy_inbounds)}; exactly one is required."
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    "  برای انتخاب inbound مقصد از Admin > Plan Inbound Routes > Bulk Assign استفاده کنید."
                )
            )
            return 0

        inbound = healthy_inbounds[0]
        created = 0
        for plan in missing_plans:
            route = PlanInboundRoute(
                store=store,
                plan=plan,
                inbound=inbound,
                operator=None,
                is_active=True,
                priority=100,
                note="Created by audit_plan_inbound_routes --fix-default.",
            )
            try:
                route.full_clean()
            except ValidationError as exc:
                self.stdout.write(
                    self.style.ERROR(f"  Could not create route for plan #{plan.pk}: {'; '.join(exc.messages)}")
                )
                continue
            if self.dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"  DRY-RUN create route: plan #{plan.pk} -> inbound #{inbound.pk} ({inbound})"
                    )
                )
                continue
            route.save()
            created += 1
            self.stdout.write(self.style.SUCCESS(f"  created route #{route.pk}: plan #{plan.pk} -> inbound #{inbound.pk}"))
        return created
