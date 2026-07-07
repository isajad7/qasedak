from django.core.management.base import BaseCommand, CommandError

from store.deployment.postgres_bridge import run_checks


class Command(BaseCommand):
    help = "Check host PostgreSQL access required by Docker tenant containers."

    def add_arguments(self, parser):
        parser.add_argument("--no-fail", action="store_true", help="Exit 0 even when bridge checks fail.")
        parser.add_argument("--pg-hba-file", help="Explicit pg_hba.conf path for tests or non-Debian layouts.")
        parser.add_argument(
            "--docker-image",
            default="postgres:16-alpine",
            help="Image used for the container-side pg_isready probe.",
        )

    def handle(self, *args, **options):
        checks = run_checks(hba_file=options.get("pg_hba_file"), docker_image=options["docker_image"])
        failed = [check for check in checks if not check.ok]
        for check in checks:
            level = "PASS" if check.ok else "FAIL"
            self.stdout.write(f"[{level}] {check.name}: {check.message}")
            if not check.ok:
                self.stdout.write("Remediation:")
                for command in check.remediation:
                    self.stdout.write(f"  {command}")
        if failed and not options["no_fail"]:
            raise CommandError("PostgreSQL Docker bridge preflight failed.")
