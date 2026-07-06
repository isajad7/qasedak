import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from store.models import Inbound, Panel
from store.xui_api import sanitize_xui_operational_text
from store.xui_compat import discover_xui_capabilities
from store.xui_compat.normalizers import redact_sensitive


class Command(BaseCommand):
    help = "Audit local and optional live 3X-UI/Sanaei compatibility without remote mutation."

    def add_arguments(self, parser):
        parser.add_argument("--panel-id", type=int, help="Audit only one panel.")
        parser.add_argument("--all-panels", action="store_true", help="Audit all panels, including inactive panels.")
        parser.add_argument("--live", action="store_true", help="Run read-only live discovery against panel APIs.")
        parser.add_argument("--write", action="store_true", help="Persist detected local compatibility metadata.")
        parser.add_argument("--no-write", action="store_true", help="Keep audit read-only locally. This is the default.")
        parser.add_argument("--export-json", help="Write a redacted JSON audit report to this path.")
        parser.add_argument("--verbose", action="store_true", help="Print per-panel details.")

    def handle(self, *args, **options):
        live = bool(options["live"])
        write = bool(options["write"]) and not bool(options["no_write"])
        queryset = Panel.objects.select_related("store").order_by("pk")
        if options.get("panel_id"):
            queryset = queryset.filter(pk=options["panel_id"])
        elif not options["all_panels"]:
            queryset = queryset.filter(is_active=True)
        panels = list(queryset)
        if not panels:
            raise CommandError("No panel matched the audit scope.")

        results = []
        for panel in panels:
            result = self.audit_panel(panel, live=live, write=write)
            results.append(result)
            self.print_panel_result(result, verbose=options["verbose"])

        summary = {
            "panels": len(results),
            "live": live,
            "write": write,
            "profiles": {},
            "errors": sum(1 for item in results if item.get("error")),
        }
        for item in results:
            profile = item.get("profile") or "unknown"
            summary["profiles"][profile] = summary["profiles"].get(profile, 0) + 1
        report = {"summary": summary, "panels": results}

        export_path = (options.get("export_json") or "").strip()
        if export_path:
            path = Path(export_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(redact_sensitive(report), ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Redacted audit report written to {path}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"X-UI compatibility audit: panels={summary['panels']} live={live} write={write} "
                f"errors={summary['errors']} profiles={summary['profiles']}"
            )
        )

    def audit_panel(self, panel, *, live, write):
        duplicates = list(
            Inbound.objects.filter(panel=panel)
            .values("inbound_id")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
            .order_by("inbound_id")
        )
        local_inbounds = Inbound.objects.filter(panel=panel).count()
        scoped_inbounds = Inbound.objects.filter(panel=panel).exclude(xui_node_id="").count()
        try:
            profile = discover_xui_capabilities(panel, live=live, write=write, use_cache=not live)
            error = ""
        except Exception as exc:
            profile = discover_xui_capabilities(panel, live=False)
            error = sanitize_xui_operational_text(exc, panel=panel)
        return redact_sensitive(
            {
                "panel_id": panel.pk,
                "panel_name": panel.name,
                "active": panel.is_active,
                "profile": profile.profile,
                "version": profile.version,
                "capabilities": profile.to_dict().get("capabilities", {}),
                "metadata": profile.metadata,
                "local": {
                    "inbounds": local_inbounds,
                    "node_scoped_inbounds": scoped_inbounds,
                    "duplicate_inbound_ids": duplicates[:20],
                },
                "error": error,
            }
        )

    def print_panel_result(self, result, *, verbose=False):
        style = self.style.WARNING if result.get("error") else self.style.SUCCESS
        self.stdout.write(
            style(
                f"panel={result['panel_id']} profile={result['profile']} version={result.get('version') or '-'} "
                f"local_inbounds={result['local']['inbounds']} node_scoped={result['local']['node_scoped_inbounds']} "
                f"duplicates={len(result['local']['duplicate_inbound_ids'])} error={result.get('error') or '-'}"
            )
        )
        if verbose:
            metadata = result.get("metadata") or {}
            self.stdout.write(
                f"  live_counts nodes={metadata.get('node_count', 0)} hosts={metadata.get('host_count', 0)} "
                f"inbounds={metadata.get('inbound_count', 0)} source={metadata.get('source', '-')}"
            )
