from store.management.commands._tenant_command import SafeTenantCommand


class Command(SafeTenantCommand):
    help = "Create a tokenless SaaS tenant instance and print a safe summary."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True)
        parser.add_argument("--base-domain", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument("--docker-image", default="qasedak-core:latest")
        parser.add_argument("--server-node-id", type=int)

    def handle(self, *args, **options):
        result = self.run_safely(
            lambda: self.toolkit().create_tenant(
                tenant_id=options["tenant_id"],
                base_domain=options["base_domain"],
                display_name=options["display_name"],
                tenant_admin_username=options["admin_username"],
                docker_image=options["docker_image"],
                server_node_id=options.get("server_node_id"),
            )
        )
        self.write_result(result)
