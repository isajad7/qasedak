from django.core.management.base import BaseCommand, CommandError

from store.models import Panel
from store.xui_api import XUIService, mask_xui_value, sanitize_xui_operational_text
from store.xui_compat import discover_xui_capabilities


CONFIRM_TEXT = "TEST_ONLY_XUI_NODE_COMPATIBILITY"


class Command(BaseCommand):
    help = "Run a read-only 3X-UI node compatibility smoke test. No client mutation is performed."

    def add_arguments(self, parser):
        parser.add_argument("--panel-id", type=int, required=True, help="Panel ID to test.")
        parser.add_argument("--inbound-id", type=int, help="Optional remote inbound ID to read.")
        parser.add_argument("--node-id", default="", help="Optional node ID/guid label for reporting only.")
        parser.add_argument("--confirm", default="", help=f"Required confirmation: {CONFIRM_TEXT}")
        parser.add_argument("--verbose", action="store_true", help="Print endpoint counts.")

    def handle(self, *args, **options):
        if options.get("confirm") != CONFIRM_TEXT:
            raise CommandError(f"Refusing live compatibility smoke test without --confirm {CONFIRM_TEXT}")

        panel = Panel.objects.filter(pk=options["panel_id"]).first()
        if not panel:
            raise CommandError("Panel was not found.")

        service = XUIService(panel)
        try:
            service.login()
            profile = discover_xui_capabilities(panel, live=True, service=service, write=False, use_cache=False)
            inbound_result = self.read_optional_inbound(service, options.get("inbound_id"))
            online_count = len(service.get_online_clients(suppress_errors=True))
        except Exception as exc:
            raise CommandError(sanitize_xui_operational_text(exc, panel=panel)) from exc

        node_label = mask_xui_value(options.get("node_id") or "") or "-"
        self.stdout.write(
            self.style.SUCCESS(
                f"X-UI node compatibility smoke test OK panel={panel.pk} "
                f"profile={profile.profile} version={profile.version or '-'} node={node_label} "
                f"inbound_read={inbound_result['read']} online_count={online_count}"
            )
        )
        if options["verbose"]:
            metadata = profile.metadata or {}
            self.stdout.write(
                f"  endpoints nodes={metadata.get('node_count', 0)} hosts={metadata.get('host_count', 0)} "
                f"inbounds={metadata.get('inbound_count', 0)} source={metadata.get('source', '-')}"
            )
            if inbound_result.get("message"):
                self.stdout.write(f"  inbound={inbound_result['message']}")

    def read_optional_inbound(self, service, inbound_id):
        if not inbound_id:
            return {"read": False, "message": ""}
        data = service.get_inbound(inbound_id, use_cache=False)
        protocol = str(data.get("protocol") or "-")
        remark = sanitize_xui_operational_text(data.get("remark") or "-", panel=service.panel, max_length=80)
        return {"read": True, "message": f"id={inbound_id} protocol={protocol} remark={remark}"}
