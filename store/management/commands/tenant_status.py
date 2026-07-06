from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Show a safe SaaS tenant status summary."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)

    def handle(self, *args, **options):
        result = self.run_safely(lambda: self.toolkit().tenant_status(tenant_id=options["tenant_id"]))
        self.write_result(result)
