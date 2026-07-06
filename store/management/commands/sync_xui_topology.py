from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from store.models import Inbound, Panel
from store.xui_api import XUIService, mask_xui_value, sanitize_xui_operational_text
from store.xui_compat import discover_xui_capabilities
from store.xui_compat.normalizers import normalize_inbound, normalize_node


CONFIRM_TEXT = "APPLY_XUI_TOPOLOGY_SYNC"


class Command(BaseCommand):
    help = "Preview or apply local node/inbound topology metadata from read-only 3X-UI APIs."

    def add_arguments(self, parser):
        parser.add_argument("--panel-id", type=int, required=True, help="Panel ID to sync.")
        parser.add_argument("--dry-run", action="store_true", help="Preview only. This is the default.")
        parser.add_argument("--apply", action="store_true", help="Persist safe local metadata updates.")
        parser.add_argument("--confirm", default="", help=f"Required with --apply: {CONFIRM_TEXT}")
        parser.add_argument("--verbose", action="store_true", help="Print skipped inbound details.")

    def handle(self, *args, **options):
        panel = Panel.objects.filter(pk=options["panel_id"]).first()
        if not panel:
            raise CommandError("Panel was not found.")
        apply = bool(options["apply"])
        dry_run = bool(options["dry_run"]) or not apply
        if apply and options.get("confirm") != CONFIRM_TEXT:
            raise CommandError(f"--apply requires --confirm {CONFIRM_TEXT}")

        service = XUIService(panel)
        try:
            profile = discover_xui_capabilities(panel, live=True, service=service, write=apply, use_cache=False)
            remote_inbounds, remote_nodes = self.fetch_topology(service, panel)
        except Exception as exc:
            raise CommandError(sanitize_xui_operational_text(exc, panel=panel)) from exc

        planned = self.plan_updates(panel, remote_inbounds)
        applied = 0
        if apply:
            applied = self.apply_updates(planned)

        self.stdout.write(
            self.style.SUCCESS(
                f"Topology sync panel={panel.pk} profile={profile.profile} version={profile.version or '-'} "
                f"remote_inbounds={len(remote_inbounds)} remote_nodes={len(remote_nodes)} "
                f"matched={len(planned['updates'])} skipped={len(planned['skipped'])} "
                f"applied={applied} dry_run={dry_run}"
            )
        )
        if options["verbose"]:
            for item in planned["updates"]:
                inbound = item["inbound"]
                remote = item["remote"]
                self.stdout.write(
                    f"  update inbound_pk={inbound.pk} inbound_id={inbound.inbound_id} "
                    f"node={mask_xui_value(remote.node_external_id) or 'local'} source={remote.source}"
                )
            for item in planned["skipped"]:
                self.stdout.write(f"  skip remote={mask_xui_value(item.get('remote_key'))} reason={item.get('reason')}")

    def _obj_list(self, payload):
        if not isinstance(payload, dict):
            return []
        obj = payload.get("obj")
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("items", "inbounds", "nodes"):
                value = obj.get(key)
                if isinstance(value, list):
                    return value
        return []

    def fetch_topology(self, service, panel):
        inbounds_payload = service.authenticated_json("GET", "/panel/api/inbounds/list")
        try:
            nodes_payload = service.authenticated_json("GET", "/panel/api/nodes/list")
        except Exception:
            nodes_payload = {}
        nodes = [normalize_node(node).__dict__ for node in self._obj_list(nodes_payload) if isinstance(node, dict)]
        node_names = {node.get("external_node_id"): node.get("name") for node in nodes if node.get("external_node_id")}
        inbounds = [
            normalize_inbound(inbound, context={"panel_id": panel.pk, "node_names": node_names})
            for inbound in self._obj_list(inbounds_payload)
            if isinstance(inbound, dict)
        ]
        return [inbound.__dict__ for inbound in inbounds], nodes

    def plan_updates(self, panel, remote_inbounds):
        updates = []
        skipped = []
        local_by_inbound_id = {}
        local_by_scope = {}
        for inbound in Inbound.objects.filter(panel=panel).order_by("pk"):
            local_by_inbound_id.setdefault(str(inbound.inbound_id), []).append(inbound)
            local_by_scope.setdefault(
                (str(inbound.inbound_id), str(inbound.xui_node_id or "")),
                [],
            ).append(inbound)

        remote_inbound_counts = {}
        for raw in remote_inbounds:
            inbound_id = str(raw.get("inbound_external_id") or "")
            remote_inbound_counts[inbound_id] = remote_inbound_counts.get(inbound_id, 0) + 1

        for raw in remote_inbounds:
            remote_key = raw.get("remote_key") or ""
            inbound_id = str(raw.get("inbound_external_id") or "")
            node_id = str(raw.get("node_external_id") or "")
            exact_matches = local_by_scope.get((inbound_id, node_id)) or []
            if len(exact_matches) == 1:
                matches = exact_matches
            elif remote_inbound_counts.get(inbound_id, 0) > 1:
                skipped.append(
                    {
                        "remote_key": remote_key,
                        "reason": "remote inbound ID is ambiguous across node scopes",
                    }
                )
                continue
            else:
                matches = local_by_inbound_id.get(inbound_id) or []
            if len(matches) != 1:
                skipped.append(
                    {
                        "remote_key": remote_key,
                        "reason": "no unique local inbound match" if matches else "local inbound missing",
                    }
                )
                continue
            remote = SimpleNamespace(**raw)
            updates.append({"inbound": matches[0], "remote": remote})
        return {"updates": updates, "skipped": skipped}

    def apply_updates(self, planned):
        applied = 0
        now = timezone.now()
        for item in planned["updates"]:
            inbound = item["inbound"]
            remote = item["remote"]
            inbound.xui_node_id = remote.node_external_id or ""
            inbound.xui_node_name = remote.node_name or ""
            inbound.xui_source = remote.source or Inbound.XUISource.LOCAL
            inbound.xui_remote_key = remote.remote_key or inbound.xui_remote_scope_key
            inbound.is_synced_from_node = bool(remote.node_external_id)
            inbound.last_synced_at = now
            inbound.save(
                update_fields=[
                    "xui_node_id",
                    "xui_node_name",
                    "xui_source",
                    "xui_remote_key",
                    "is_synced_from_node",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            applied += 1
        return applied
