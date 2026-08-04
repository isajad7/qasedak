from django.core.management.base import BaseCommand, CommandError

from store.models import Order, VPNClient
from store.subscription_cups import (
    build_subscription_cup_path,
    build_subscription_cup_url,
    cup_protocols,
    mask_subscription_url,
    rebuild_subscription_cup_for_vpn_client,
    rebuild_subscription_cups_for_order,
)


class Command(BaseCommand):
    help = "Rebuild Subscription Cup rows for one VPN client or one order without printing config links."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--vpn-client-id", type=int)
        target.add_argument("--order-id", type=int)
        parser.add_argument("--base-url", default="", help="Optional public base URL used only for the masked summary.")

    def handle(self, *args, **options):
        base_url = str(options.get("base_url") or "").strip().rstrip("/")
        if options.get("vpn_client_id"):
            try:
                vpn_client = VPNClient.objects.get(pk=options["vpn_client_id"])
            except VPNClient.DoesNotExist as exc:
                raise CommandError("VPN client not found.") from exc
            cups = [
                rebuild_subscription_cup_for_vpn_client(
                    vpn_client,
                    force_active=False,
                    added_reason="management_rebuild",
                )
            ]
        else:
            try:
                order = Order.objects.get(pk=options["order_id"])
            except Order.DoesNotExist as exc:
                raise CommandError("Order not found.") from exc
            cups = rebuild_subscription_cups_for_order(
                order,
                force_active=False,
                added_reason="management_rebuild",
            )

        if not cups:
            self.stdout.write("cups=0")
            return

        for cup in cups:
            item_count = cup.items.filter(is_active=True, config_link__is_active=True).count()
            protocols = ",".join(cup_protocols(cup)) or "-"
            url = f"{base_url}{build_subscription_cup_path(cup)}" if base_url else build_subscription_cup_url(cup)
            masked_url = mask_subscription_url(url, cup.token)
            self.stdout.write(
                f"cup id={cup.pk} item_count={item_count} protocols={protocols} subscription_url={masked_url}"
            )
