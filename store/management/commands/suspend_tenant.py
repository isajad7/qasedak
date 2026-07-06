from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Suspend a SaaS tenant without deleting its DB, env, or credentials."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)
        parser.add_argument("--reason", default="")

    def handle(self, *args, **options):
        result = self.run_safely(
            lambda: self.toolkit().suspend_tenant(tenant_id=options["tenant_id"], reason=options["reason"])
        )
        self.write_result(result)
