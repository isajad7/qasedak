from django.core.management.base import BaseCommand, CommandError

from store.models import Order, VPNClient
from store.provisioning_services import (
    lookup_existing_remote_client,
    order_identity,
    approve_and_provision_order,
    resolve_order_provisioning_strategy,
)
from store.xui_api import mask_xui_value, sanitize_xui_operational_text


class Command(BaseCommand):
    help = "Reconcile pending/failed order provisioning. Dry-run by default; --apply may create enabled clients."

    def add_arguments(self, parser):
        parser.add_argument("--order-id", type=int, help="Single order ID to inspect.")
        parser.add_argument("--all-failed", action="store_true", help="Inspect failed/pending provisioning orders.")
        parser.add_argument("--limit", type=int, default=50, help="Maximum orders for --all-failed.")
        parser.add_argument("--dry-run", action="store_true", help="Do not mutate remote/local state. This is the default.")
        parser.add_argument("--apply", action="store_true", help="Retry provisioning and repair local state when safe.")
        parser.add_argument("--retry-delivery", action="store_true", help="Retry approved order delivery for completed orders.")
        parser.add_argument("--verbose", action="store_true", help="Print masked identity/scope details.")

    def handle(self, *args, **options):
        if not options["order_id"] and not options["all_failed"]:
            raise CommandError("Use --order-id or --all-failed.")
        if options["dry_run"] and options["apply"]:
            raise CommandError("Choose either --dry-run or --apply, not both.")
        apply = bool(options["apply"])
        orders = self.get_orders(options)
        if not orders:
            self.stdout.write(self.style.WARNING("No matching orders."))
            return

        for order in orders:
            report = self.inspect_order(order, verbose=options["verbose"])
            self.stdout.write(self.format_report(report))
            if apply and report["actionable"]:
                result = approve_and_provision_order(order, actor=None, source="reconcile_order_provisioning", notify=False)
                order.refresh_from_db()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  apply ok={result.ok} order_status={order.status} provisioning={order.provisioning_status} "
                        f"error={result.safe_error or '-'}"
                    )
                )
            if options["retry_delivery"] and apply and order.status == Order.Status.COMPLETED:
                from store.telegram_bot.notifications import notify_order_event

                notify_order_event(order, event_type="approved")
                self.stdout.write(self.style.SUCCESS("  delivery retry queued/executed."))

    def get_orders(self, options):
        qs = Order.objects.select_related("plan", "store", "inbound", "inbound__panel").order_by("created_at", "pk")
        if options["order_id"]:
            return list(qs.filter(pk=options["order_id"]))
        return list(
            qs.filter(
                provisioning_status__in=[
                    Order.ProvisioningStatus.PENDING,
                    Order.ProvisioningStatus.PROVISIONING,
                    Order.ProvisioningStatus.FAILED,
                ]
            )[: max(int(options["limit"] or 50), 1)]
        )

    def inspect_order(self, order, *, verbose=False):
        report = {
            "order_id": order.pk,
            "tracking": order.order_tracking_code,
            "status": order.status,
            "provisioning_status": order.provisioning_status,
            "strategy": resolve_order_provisioning_strategy(order),
            "state": "unknown",
            "actionable": False,
            "error": "",
            "details": "",
        }
        local_client = order.vpn_clients.exclude(status=VPNClient.Status.DELETED).order_by("created_at", "pk").first()
        if not order.inbound_id or not getattr(order.inbound, "panel_id", None):
            report["state"] = "missing_scope"
            report["error"] = "Order has no exact panel/inbound scope."
            return report

        inbound = order.inbound
        panel = inbound.panel
        try:
            identity = order_identity(order, inbound)
            remote = lookup_existing_remote_client(panel, inbound, identity)
        except Exception as exc:
            report["state"] = "ambiguous" if "ambiguous" in str(exc).lower() else "remote_lookup_failed"
            report["error"] = sanitize_xui_operational_text(exc, panel=panel)
            return report

        if remote and local_client:
            report["state"] = "already_consistent"
        elif remote and not local_client:
            report["state"] = "remote_exists_local_missing"
            report["actionable"] = True
        elif local_client and not remote:
            report["state"] = "local_exists_remote_missing"
            report["actionable"] = order.status != Order.Status.COMPLETED
        else:
            report["state"] = "remote_missing"
            report["actionable"] = order.status != Order.Status.COMPLETED

        if verbose:
            node = getattr(inbound, "xui_node_id", "") or "local"
            report["details"] = (
                f" panel={panel.pk} inbound_pk={inbound.pk} xui_inbound={inbound.inbound_id}"
                f" node={mask_xui_value(node)} uuid={mask_xui_value(identity['uuid'])}"
                f" email={mask_xui_value(identity['email'])}"
            )
        return report

    def format_report(self, report):
        line = (
            f"order={report['order_id']} tracking={report['tracking']} "
            f"status={report['status']} provisioning={report['provisioning_status']} "
            f"strategy={report['strategy']} state={report['state']} actionable={report['actionable']}"
        )
        if report["error"]:
            line += f" error={report['error']}"
        if report["details"]:
            line += report["details"]
        return line
