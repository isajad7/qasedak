from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from store.deployment.postgres_bridge import (
    POSTGRES_HBA_BRIDGE_RULE,
    POSTGRES_LISTEN_ADDRESSES,
    PostgresBridgeConfigurationError,
    configure_postgres_bridge,
)


class Command(BaseCommand):
    help = "Safely configure host PostgreSQL access for Docker tenant containers."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write config files, restart PostgreSQL, and verify.")
        parser.add_argument("--yes", action="store_true", help="Confirm the PostgreSQL restart without prompting.")
        parser.add_argument("--no-fail", action="store_true", help="Exit 0 even when post-restart bridge checks fail.")
        parser.add_argument(
            "--docker-image",
            default="postgres:16-alpine",
            help="Image used for the post-restart container-side pg_isready probe.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        yes = options["yes"]
        try:
            if apply and not yes and not self._confirm_restart():
                raise CommandError("PostgreSQL bridge configuration cancelled.")

            result = configure_postgres_bridge(
                apply=apply,
                restart=apply,
                check_after_restart=False,
                docker_image=options["docker_image"],
            )
        except PostgresBridgeConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        except OSError as exc:
            raise CommandError(f"Could not update PostgreSQL bridge configuration: {exc}") from exc

        self.stdout.write(f"postgresql.conf: {result.files.config_file}")
        self.stdout.write(f"pg_hba.conf: {result.files.hba_file}")
        self.stdout.write(f"listen_addresses target: '{POSTGRES_LISTEN_ADDRESSES}'")
        self.stdout.write(f"pg_hba target rule: {POSTGRES_HBA_BRIDGE_RULE}")

        if not apply:
            self.stdout.write("DRY-RUN: no files changed and PostgreSQL was not restarted.")
            self.stdout.write("Run again with --apply and confirm the restart, or pass --apply --yes.")
            return

        self.stdout.write(f"backup: {result.config_backup}")
        self.stdout.write(f"backup: {result.hba_backup}")
        self.stdout.write(
            "postgresql.conf updated" if result.config_changed else "postgresql.conf already matched target"
        )
        self.stdout.write("pg_hba.conf updated" if result.hba_changed else "pg_hba.conf already had target rule")
        self.stdout.write("PostgreSQL restarted.")

        self._run_bridge_check(options["docker_image"], no_fail=options["no_fail"])

    def _confirm_restart(self):
        question = "Restart PostgreSQL after updating Docker bridge access? [y/N]: "
        try:
            answer = input(question)
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _run_bridge_check(self, docker_image, *, no_fail=False):
        output = StringIO()
        args = ["--docker-image", docker_image]
        if no_fail:
            args.append("--no-fail")
        try:
            call_command("check_postgres_bridge", *args, stdout=output)
        except CommandError as exc:
            self.stdout.write(output.getvalue().rstrip())
            raise CommandError("PostgreSQL Docker bridge verification failed after restart.") from exc
        self.stdout.write(output.getvalue().rstrip())
