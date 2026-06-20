import json
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from store.productization.bootstrap import (
    BootstrapInstallError,
    BootstrapInstaller,
    load_install_config,
    redact_bootstrap_summary,
)


class Command(BaseCommand):
    help = "Bootstrap initial VPN Store database objects from an install JSON config."

    def add_arguments(self, parser):
        parser.add_argument("--config", required=True, help="Path to install.config.json.")
        parser.add_argument("--dry-run", action="store_true", help="Validate and show the plan without DB writes.")
        parser.add_argument(
            "--no-update-existing",
            action="store_true",
            help="Create missing objects but leave matching existing objects unchanged.",
        )
        parser.add_argument("--live-check", action="store_true", help="Run explicit Telegram/X-UI live checks.")
        parser.add_argument(
            "--fail-on-live-check-error",
            action="store_true",
            help="Treat live-check failures as command failures.",
        )
        parser.add_argument("--yes", action="store_true", help="Run non-interactively without confirmation.")

    def handle(self, *args, **options):
        try:
            config = load_install_config(options["config"])
            if options["dry_run"]:
                summary = BootstrapInstaller(
                    config,
                    dry_run=True,
                    update_existing=not options["no_update_existing"],
                    live_check=options["live_check"],
                    fail_on_live_check_error=options["fail_on_live_check_error"],
                ).run()
                self._write_summary("Bootstrap install dry-run", summary)
                return

            plan = BootstrapInstaller(
                config,
                dry_run=True,
                update_existing=not options["no_update_existing"],
                live_check=False,
            ).run()
            self._write_summary("Bootstrap install plan", plan)
            self._confirm(options["yes"])

            with transaction.atomic():
                summary = BootstrapInstaller(
                    config,
                    dry_run=False,
                    update_existing=not options["no_update_existing"],
                    live_check=options["live_check"],
                    fail_on_live_check_error=options["fail_on_live_check_error"],
                ).run()
        except BootstrapInstallError as exc:
            raise CommandError(str(exc)) from exc

        self._write_summary("Bootstrap install result", summary)

    def _confirm(self, assume_yes):
        if assume_yes:
            return
        if not sys.stdin.isatty():
            raise CommandError("Refusing real bootstrap in non-interactive mode without --yes.")
        answer = input("Type 'bootstrap' to write install objects: ").strip()
        if answer != "bootstrap":
            raise CommandError("Bootstrap cancelled.")

    def _write_summary(self, title, summary):
        summary = redact_bootstrap_summary(summary)
        counts = summary.get("counts", {})
        self.stdout.write(title)
        self.stdout.write(
            "counts: "
            f"created={counts.get('created', 0)} "
            f"updated={counts.get('updated', 0)} "
            f"skipped={counts.get('skipped', 0)} "
            f"would_create={counts.get('would_create', 0)} "
            f"would_update={counts.get('would_update', 0)} "
            f"would_skip={counts.get('would_skip', 0)}"
        )

        objects = summary.get("objects", {})
        for key in sorted(objects):
            self.stdout.write(f"{key}: {json.dumps(objects[key], ensure_ascii=True, sort_keys=True, default=str)}")

        warnings = summary.get("warnings") or []
        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"warning: {warning}"))

        self.stdout.write(self.style.SUCCESS("install_status=complete"))
        if summary.get("business_setup_incomplete"):
            self.stdout.write(
                self.style.WARNING(
                    "business_setup=incomplete; open Django Admin to configure Telegram, Panel, Plans, and Payment."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("business_setup=complete"))

        live = "yes" if summary.get("live_checks_run") else "no"
        self.stdout.write(f"live_checks_run={live}")
