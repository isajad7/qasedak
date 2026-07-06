from django.core.management.base import BaseCommand, CommandError

from store.models import VPNClient
from store.vpn_client_reconciliation_services import (
    get_reconciliation_summary,
    reconcile_vpn_clients,
    remote_status_label,
    soft_delete_remote_missing_clients,
)


CONFIRM_TEXT = "SOFT_DELETE_REMOTE_MISSING"


class Command(BaseCommand):
    help = "Read-only reconcile local VPNClient records with X-UI scopes; optional local soft-delete for confirmed missing clients."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview only. This is the default without apply flags.")
        parser.add_argument("--panel-id", type=int, help="Limit to a panel ID.")
        parser.add_argument("--inbound-id", type=int, help="Limit to an X-UI inbound ID.")
        parser.add_argument("--client-id", type=int, action="append", help="Limit to one local VPNClient PK. Repeatable.")
        parser.add_argument("--all", action="store_true", help="Include all non-deleted VPN clients.")
        parser.add_argument("--apply-check-results", action="store_true", help="Persist latest check fields on VPNClient.")
        parser.add_argument(
            "--soft-delete-confirmed-missing",
            action="store_true",
            help="After revalidation, soft-delete clients still confirmed remote_missing locally.",
        )
        parser.add_argument("--confirm", default="", help=f"Required for soft-delete: {CONFIRM_TEXT}")
        parser.add_argument("--limit", type=int, help="Limit candidate count.")
        parser.add_argument("--verbose", action="store_true", help="Print per-status details.")

    def handle(self, *args, **options):
        if not options["all"] and not options.get("panel_id") and not options.get("inbound_id") and not options.get("client_id"):
            raise CommandError("Choose --all or at least one of --panel-id, --inbound-id, --client-id.")

        explicit_dry_run = bool(options["dry_run"])
        dry_run = explicit_dry_run or not (options["apply_check_results"] or options["soft_delete_confirmed_missing"])
        if options["soft_delete_confirmed_missing"] and not dry_run and options.get("confirm") != CONFIRM_TEXT:
            raise CommandError(f"--soft-delete-confirmed-missing requires --confirm {CONFIRM_TEXT}")

        queryset = (
            VPNClient.objects.select_related("store", "inbound", "inbound__panel")
            .exclude(status=VPNClient.Status.DELETED)
            .filter(deleted_at__isnull=True)
            .order_by("pk")
        )
        if options.get("panel_id"):
            queryset = queryset.filter(inbound__panel_id=options["panel_id"])
        if options.get("inbound_id"):
            queryset = queryset.filter(inbound__inbound_id=options["inbound_id"])
        if options.get("client_id"):
            queryset = queryset.filter(pk__in=options["client_id"])
        if options.get("limit"):
            queryset = queryset[: max(int(options["limit"]), 0)]

        candidate_ids = list(queryset.values_list("pk", flat=True))
        if not candidate_ids:
            self.stdout.write("No non-deleted VPN clients matched.")
            return

        self.stdout.write(
            f"Reconciling candidates={len(candidate_ids)} dry_run={dry_run} "
            f"apply_check_results={bool(options['apply_check_results'] and not dry_run)}"
        )
        result = reconcile_vpn_clients(
            VPNClient.objects.filter(pk__in=candidate_ids),
            actor="management-command",
            persist=bool(options["apply_check_results"] and not dry_run),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"batch={result.batch_id[:8]} checked={result.checked} scopes={result.scopes} api_calls={result.api_calls}"
            )
        )
        for status, count in sorted(result.statuses.items()):
            self.stdout.write(f"  {status}: {count} ({remote_status_label(status)})")

        if options["verbose"] and result.errors:
            for error in result.errors[:20]:
                self.stdout.write(f"  error: {error}")

        if not options["soft_delete_confirmed_missing"]:
            return

        summary = get_reconciliation_summary(VPNClient.objects.filter(pk__in=candidate_ids))
        cleanup_ids = summary["cleanup_candidate_ids"]
        if dry_run:
            self.stdout.write(f"Dry-run: would soft-delete eligible remote_missing clients={len(cleanup_ids)}")
            return
        cleanup = soft_delete_remote_missing_clients(cleanup_ids, actor="management-command")
        self.stdout.write(
            self.style.SUCCESS(
                f"soft_delete requested={cleanup['requested']} deleted={cleanup['deleted']} blocked={cleanup['blocked']}"
            )
        )
