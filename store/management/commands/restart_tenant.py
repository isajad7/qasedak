from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Restart only the target SaaS tenant container."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)

    def handle(self, *args, **options):
        result = self.run_safely(lambda: self.toolkit().restart_tenant(tenant_id=options["tenant_id"]))
        self.write_result(result)
