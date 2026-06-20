from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from store.legacy_wizwiz_import_services import (
    analyze_wizwiz_import_job,
    apply_wizwiz_import_job,
    build_wizwiz_import_summary,
    export_wizwiz_import_rows_csv,
)
from store.models import LegacyWizWizImportJob


class Command(BaseCommand):
    help = "Create/analyze/apply a legacy WizWiz users SQL import job."

    def add_arguments(self, parser):
        parser.add_argument("--sql-path", help="Path to the WizWiz SQL dump.")
        parser.add_argument("--create-job", action="store_true", help="Create a LegacyWizWizImportJob from --sql-path.")
        parser.add_argument("--job-id", type=int, help="Use an existing LegacyWizWizImportJob.")
        parser.add_argument("--analyze", action="store_true", help="Analyze the job and build preview rows.")
        parser.add_argument("--apply", action="store_true", help="Apply an analyzed job and create/link users.")
        parser.add_argument("--dry-run", action="store_true", help="Never apply; useful with --analyze.")
        parser.add_argument("--limit", type=int, help="Limit analyzed unique valid users.")
        parser.add_argument("--update-existing", action="store_true", help="Update safe fields on existing BotUser/Customer records.")
        parser.add_argument("--skip-admins", action="store_true", help="Skip WizWiz admin rows. Job default is already true.")
        parser.add_argument("--only-with-wallet", action="store_true", help="Import only rows with positive legacy wallet.")
        parser.add_argument("--only-agents", action="store_true", help="Import only legacy agent rows.")
        parser.add_argument("--export-csv", help="Export job rows to a CSV path after requested operations.")
        parser.add_argument("--verbose", action="store_true", help="Print extra job details.")

    def handle(self, *args, **options):
        job = self._resolve_job(options)
        self._apply_options(job, options)

        if options["analyze"]:
            analyze_wizwiz_import_job(job)
            job.refresh_from_db()

        if options["apply"]:
            if options["dry_run"]:
                self.stdout.write(self.style.WARNING("Dry-run enabled; apply skipped."))
            else:
                apply_wizwiz_import_job(job)
                job.refresh_from_db()

        if options["export_csv"]:
            export_path = Path(options["export_csv"]).expanduser()
            export_path.parent.mkdir(parents=True, exist_ok=True)
            with export_path.open("w", newline="", encoding="utf-8") as handle:
                export_wizwiz_import_rows_csv(job, handle)
            self.stdout.write(self.style.SUCCESS(f"Rows exported to {export_path}"))

        summary = build_wizwiz_import_summary(job)
        self._print_summary(summary, verbose=options["verbose"])

    def _resolve_job(self, options):
        if options["job_id"]:
            try:
                return LegacyWizWizImportJob.objects.get(pk=options["job_id"])
            except LegacyWizWizImportJob.DoesNotExist as exc:
                raise CommandError(f"Legacy WizWiz import job #{options['job_id']} was not found.") from exc

        if not options["create_job"]:
            raise CommandError("Provide --job-id or use --sql-path with --create-job.")

        sql_path_value = (options["sql_path"] or "").strip()
        if not sql_path_value:
            raise CommandError("--sql-path is required with --create-job.")
        sql_path = Path(sql_path_value).expanduser()
        if not sql_path.exists() or not sql_path.is_file():
            raise CommandError(f"SQL file not found: {sql_path}")
        if sql_path.suffix.lower() != ".sql":
            raise CommandError("Only .sql files are accepted.")
        if sql_path.stat().st_size <= 0:
            raise CommandError("SQL file is empty.")

        job = LegacyWizWizImportJob(
            title=sql_path.name,
            original_filename=sql_path.name,
            file_size=sql_path.stat().st_size,
        )
        with sql_path.open("rb") as handle:
            job.uploaded_file.save(sql_path.name, File(handle), save=False)
        job.save()
        self.stdout.write(self.style.SUCCESS(f"Created import job #{job.pk} from {sql_path.name}"))
        return job

    def _apply_options(self, job, options):
        changed_fields = []
        if options["update_existing"] and not job.update_existing:
            job.update_existing = True
            changed_fields.append("update_existing")
        if options["skip_admins"] and not job.skip_admins:
            job.skip_admins = True
            changed_fields.append("skip_admins")
        if options["only_with_wallet"] and not job.only_wallet_positive:
            job.only_wallet_positive = True
            changed_fields.append("only_wallet_positive")
        if options["only_agents"] and not job.only_agents:
            job.only_agents = True
            changed_fields.append("only_agents")
        if options["limit"] is not None:
            limit = max(int(options["limit"]), 0)
            metadata = dict(job.metadata or {})
            if metadata.get("limit") != limit:
                metadata["limit"] = limit
                job.metadata = metadata
                changed_fields.append("metadata")
        if changed_fields:
            job.save(update_fields=[*set(changed_fields), "updated_at"])

    def _print_summary(self, summary, *, verbose=False):
        self.stdout.write(f"Job #{summary['job_id']} status={summary['status']}")
        self.stdout.write(
            "Analyze: "
            f"parsed={summary['parsed_users']} valid={summary['valid_users']} "
            f"invalid={summary['invalid_rows']} duplicates={summary['duplicates']} "
            f"admins={summary['admins']} agents={summary['agents']} "
            f"wallet_positive={summary['wallet_positive']}"
        )
        self.stdout.write(
            "Estimate: "
            f"existing_bot_users={summary['existing_bot_users']} "
            f"existing_customers={summary['existing_customers']} "
            f"would_create_bot_users={summary['would_create_bot_users']} "
            f"would_create_customers={summary['would_create_customers']}"
        )
        self.stdout.write(
            "Apply: "
            f"created_bot_users={summary['created_bot_users']} "
            f"created_customers={summary['created_customers']} "
            f"linked_existing={summary['linked_existing']} "
            f"updated_existing={summary['updated_existing']} "
            f"skipped={summary['skipped']} failed={summary['failed']}"
        )
        if verbose:
            self.stdout.write("No raw SQL, tokens, passwords, or old admin credentials are printed.")
