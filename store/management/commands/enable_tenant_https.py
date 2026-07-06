from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Enable HTTPS for a tenant subdomain using certbot/nginx."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--server-ip", default="")

    def handle(self, *args, **options):
        result = self.run_safely(
            lambda: self.toolkit().enable_https(
                tenant_id=options["tenant_id"],
                email=options["email"],
                server_ip=options["server_ip"] or None,
            )
        )
        self.write_result(result)
