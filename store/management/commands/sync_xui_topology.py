from types import SimpleNamespace
from urllib.parse import urlparse

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
        parser.add_argument("--create-missing", action="store_true", help="Create local Inbound rows for remote inbounds with no local match.")
        parser.add_argument(
            "--available-for-new-orders",
            action="store_true",
            help="Mark newly created local inbound rows as available for new orders.",
        )
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
        created = 0
        if apply:
            applied = self.apply_updates(planned)
            if options.get("create_missing"):
                created = self.create_missing_inbounds(
                    panel,
                    planned,
                    available_for_new_orders=bool(options.get("available_for_new_orders")),
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Topology sync panel={panel.pk} profile={profile.profile} version={profile.version or '-'} "
                f"remote_inbounds={len(remote_inbounds)} remote_nodes={len(remote_nodes)} "
                f"matched={len(planned['updates'])} skipped={len(planned['skipped'])} "
                f"applied={applied} created={created} dry_run={dry_run}"
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
        node_addresses = {
            node.get("external_node_id"): self._host_from_address_label(node.get("safe_address_label"))
            for node in nodes
            if node.get("external_node_id")
        }
        inbounds = []
        for inbound in self._obj_list(inbounds_payload):
            if not isinstance(inbound, dict):
                continue
            normalized = normalize_inbound(inbound, context={"panel_id": panel.pk, "node_names": node_names}).__dict__
            normalized["node_address"] = node_addresses.get(normalized.get("node_external_id")) or ""
            normalized["raw"] = inbound
            inbounds.append(normalized)
        return inbounds, nodes

    def _host_from_address_label(self, value):
        value = str(value or "").strip()
        if not value:
            return ""
        parsed = urlparse(value if "://" in value else f"//{value}")
        return parsed.hostname or value.split(":")[0]

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
                        "raw": raw.get("raw") or {},
                        "remote": raw,
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
                        "raw": raw.get("raw") or {},
                        "remote": raw,
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

    def create_missing_inbounds(self, panel, planned, *, available_for_new_orders=False):
        created = 0
        parsed = urlparse(panel.url)
        fallback_host = parsed.hostname or parsed.netloc or ""
        for item in planned.get("skipped", []):
            if item.get("reason") != "local inbound missing":
                continue
            remote_data = item.get("remote") or {}
            raw = item.get("raw") or {}
            inbound_id = str(remote_data.get("inbound_external_id") or "").strip()
            if not inbound_id.isdigit():
                continue
            node_id = str(remote_data.get("node_external_id") or "").strip()
            defaults = {
                "remark": str(remote_data.get("remark") or raw.get("remark") or f"Remote inbound {inbound_id}")[:150],
                "protocol": str(remote_data.get("protocol") or raw.get("protocol") or Inbound.Protocol.VLESS).lower()
                or Inbound.Protocol.VLESS,
                "server_ip": str(
                    remote_data.get("managed_share_address")
                    or raw.get("shareAddr")
                    or raw.get("listen")
                    or remote_data.get("node_address")
                    or fallback_host
                )[:100],
                "port": str(raw.get("port") or "")[:10],
                "config_params": "type=tcp&security=none",
                "is_active": remote_data.get("active") is not False,
                "available_for_new_orders": bool(available_for_new_orders),
                "health_monitor_enabled": True,
                "xui_node_id": node_id,
                "xui_node_name": str(remote_data.get("node_name") or "")[:150],
                "xui_source": remote_data.get("source") or Inbound.XUISource.LOCAL,
                "xui_remote_key": remote_data.get("remote_key") or item.get("remote_key") or "",
                "is_synced_from_node": bool(node_id),
                "last_synced_at": timezone.now(),
            }
            _inbound, was_created = Inbound.objects.get_or_create(
                panel=panel,
                inbound_id=int(inbound_id),
                xui_node_id=node_id,
                defaults=defaults,
            )
            if was_created:
                created += 1
        return created
