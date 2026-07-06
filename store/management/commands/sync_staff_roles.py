from django.core.management.base import BaseCommand, CommandError

from store.admin_access import log_staff_access_change, sync_staff_role_presets


class Command(BaseCommand):
    help = "Sync Qasedak staff role presets into Django Groups and built-in model permissions."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show intended changes without writing to the DB.")
        parser.add_argument("--apply", action="store_true", help="Create/update role groups and add missing permissions.")
        parser.add_argument("--verbose", action="store_true", help="Print role capability details.")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        apply = bool(options["apply"])
        if dry_run and apply:
            raise CommandError("Use only one of --dry-run or --apply.")
        if not dry_run and not apply:
            dry_run = True

        summary = sync_staff_role_presets(
            dry_run=dry_run,
            apply=apply,
            verbose=bool(options["verbose"]),
        )
        title = "Staff role sync dry-run" if dry_run else "Staff role sync applied"
        self.stdout.write(title)
        self.stdout.write(
            "summary: "
            f"groups_created={summary['groups_created']} "
            f"groups_updated={summary['groups_updated']} "
            f"permissions_added={summary['permissions_added']} "
            f"warnings={len(summary['warnings'])}"
        )
        for role in summary["roles"]:
            self.stdout.write(
                f"role={role['key']} group={role['group']} "
                f"created={role['created']} "
                f"permissions_expected={role['permissions_expected']} "
                f"permissions_added={role['permissions_added']}"
            )
            if options["verbose"]:
                self.stdout.write("  capabilities=" + ",".join(role.get("capabilities") or []))
        for warning in summary["warnings"]:
            self.stdout.write(self.style.WARNING(f"warning: {warning}"))
        log_staff_access_change(
            None,
            None,
            "permission_sync_command_run",
            {
                "dry_run": dry_run,
                "apply": apply,
                "groups_created": summary["groups_created"],
                "groups_updated": summary["groups_updated"],
                "permissions_added": summary["permissions_added"],
                "warning_count": len(summary["warnings"]),
            },
        )
        self.stdout.write(self.style.SUCCESS("staff_role_sync=complete"))
