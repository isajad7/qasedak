from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Resume a suspended SaaS tenant without changing customer setup."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)

    def handle(self, *args, **options):
        result = self.run_safely(lambda: self.toolkit().resume_tenant(tenant_id=options["tenant_id"]))
        self.write_result(result)
