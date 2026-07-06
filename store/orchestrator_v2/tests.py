import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from store.deployment.subdomain_manager import SubdomainError, SubdomainManager
from store.orchestrator_v2.models import ServerNode, TenantInstance
from store.orchestrator_v2.services.deploy_engine import DeployEngine, DeploymentError
from store.orchestrator_v2.services.instance_manager import InstanceManager
from store.orchestrator_v2.services.ssh_client import SSHCommandResult, SSHConnectionStatus


class FakeSSH:
    def __init__(self, credentials, *, existing_id="", ports_output="", fail_pull=False):
        self.credentials = credentials
        self.existing_id = existing_id
        self.ports_output = ports_output
        self.fail_pull = fail_pull
        self.commands = []
        self.files = {}

    def verify_connection(self):
        return SSHConnectionStatus(
            ok=True,
            host=self.credentials.host,
            user=self.credentials.user,
            port=self.credentials.port,
            message="connected",
        )

    def run(self, command, *, timeout=60, sensitive=False):
        self.commands.append(command)
        if command == "printf qasedak-ok":
            return SSHCommandResult(0, "qasedak-ok", "")
        if "docker pull" in command and self.fail_pull:
            return SSHCommandResult(1, "", "pull failed")
        if command.startswith("docker ps -a"):
            return SSHCommandResult(0, f"{self.existing_id}\n" if self.existing_id else "", "")
        if command.startswith("docker ps --format"):
            return SSHCommandResult(0, self.ports_output, "")
        if command.startswith("docker run -d"):
            return SSHCommandResult(0, "new-container-id\n", "")
        if command.startswith("docker inspect -f"):
            return SSHCommandResult(0, "running\n", "")
        if "curl -fsS" in command:
            return SSHCommandResult(
                0,
                json.dumps(
                    {
                        "service": "alive",
                        "database": {"reachable": True},
                        "bot": {"configured": True, "api_check": "skipped"},
                    }
                ),
                "",
            )
        return SSHCommandResult(0, "", "")

    def put_file(self, remote_path, content, *, mode="600"):
        self.files[remote_path] = content

    def close(self):
        pass


class OrchestratorV2Tests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="secret", is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def post_json(self, path, payload):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def server(self, **overrides):
        data = {
            "name": "node-1",
            "ip": "127.0.0.1",
            "ssh_user": "root",
            "ssh_port": 22,
            "ssh_key_path": "/home/qasedak/.ssh/id_ed25519",
            "max_instances": 10,
        }
        data.update(overrides)
        return ServerNode.objects.create(**data)

    def instance(self, server=None, **overrides):
        server = server or self.server()
        data = {
            "tenant_id": "tenant-a",
            "server_node": server,
            "container_name": "qasedak_tenant-a",
            "port": 8001,
            "domain": "tenant-a.example.com",
            "docker_image_version": "qasedak-core:latest",
        }
        data.update(overrides)
        return TenantInstance.objects.create(**data)

    def runtime_env(self, token="", admin_password="generated-admin-secret"):
        env = {
            "DATABASE_URL": "postgres://qasedak:db-secret@db.example.com:5432/qasedak",
            "DJANGO_SECRET_KEY": "django-secret",
            "REVENUE_ENGINE_DRY_RUN": "true",
            "QASEDAK_BOT_ENABLED": "false",
            "QASEDAK_WORKER_ENABLED": "false",
            "QASEDAK_BOOTSTRAP_TENANT": "true",
            "QASEDAK_ADMIN_USERNAME": "admin",
            "QASEDAK_ADMIN_PASSWORD": admin_password,
        }
        if token:
            env["TELEGRAM_BOT_TOKEN"] = token
        return env

    def test_server_registration_does_not_store_or_return_password(self):
        response = self.post_json(
            "/orchestrator/v2/server/register",
            {
                "name": "node-1",
                "ip": "127.0.0.1",
                "ssh_user": "root",
                "ssh_password": "super-secret-password",
                "max_instances": 4,
            },
        )
        self.assertEqual(response.status_code, 200)
        server = ServerNode.objects.get()
        self.assertEqual(server.ip, "127.0.0.1")
        self.assertFalse(hasattr(server, "ssh_password"))
        self.assertNotIn("super-secret-password", response.content.decode())

    def test_orchestrator_api_requires_staff_user(self):
        self.client.logout()
        response = self.post_json(
            "/orchestrator/v2/server/register",
            {"name": "node-1", "ip": "127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_instance_create_allocates_ports_and_is_idempotent(self):
        server = self.server(max_instances=3)
        manager = InstanceManager()
        first = manager.create_instance({"tenant_id": "tenant-a", "server_node_id": server.pk, "port": 8001}, actor=self.user)
        second = manager.create_instance({"tenant_id": "tenant-b", "server_node_id": server.pk, "port": 8001}, actor=self.user)
        again = manager.create_instance({"tenant_id": "tenant-a", "server_node_id": server.pk, "port": 8010}, actor=self.user)
        self.assertEqual(first.port, 8001)
        self.assertEqual(second.port, 8002)
        self.assertEqual(again.pk, first.pk)
        server.refresh_from_db()
        self.assertEqual(server.current_instances, 2)

    def test_deploy_simulation_creates_container_without_telegram_token(self):
        server = self.server()
        instance = self.instance(server=server)
        fake = FakeSSH(None)
        admin_password = "generated-admin-secret"

        def factory(credentials):
            fake.credentials = credentials
            return fake

        result = DeployEngine(ssh_client_factory=factory).deploy(instance, self.runtime_env(admin_password=admin_password))
        instance.refresh_from_db()
        self.assertEqual(instance.status, TenantInstance.Status.RUNNING)
        self.assertEqual(result.container_id, "new-container-id")
        self.assertIn("/opt/qasedak/tenant-a/.env", fake.files)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", fake.files["/opt/qasedak/tenant-a/.env"])
        self.assertIn("QASEDAK_BOT_ENABLED=false", fake.files["/opt/qasedak/tenant-a/.env"])
        self.assertIn("QASEDAK_WORKER_ENABLED=false", fake.files["/opt/qasedak/tenant-a/.env"])
        self.assertIn("QASEDAK_BOOTSTRAP_TENANT=true", fake.files["/opt/qasedak/tenant-a/.env"])
        self.assertIn("/opt/qasedak/tenant-a/admin-credentials.txt", fake.files)
        credentials_file = fake.files["/opt/qasedak/tenant-a/admin-credentials.txt"]
        self.assertIn("ADMIN_URL=https://tenant-a.example.com/admin/", credentials_file)
        self.assertIn(f"ADMIN_PASSWORD={admin_password}", credentials_file)
        self.assertEqual(result.runtime_state["setup_status"], "setup_required")
        self.assertIn("telegram", result.runtime_state["setup_checklist_pending"])
        all_commands = "\n".join(fake.commands)
        self.assertIn("docker network create --driver bridge qasedak_tenant-a_net", all_commands)
        self.assertIn("-p 127.0.0.1:8001:8000", all_commands)
        self.assertNotIn(admin_password, all_commands)
        self.assertNotIn("db-secret", all_commands)
        self.assertNotIn(admin_password, json.dumps(result.__dict__))

    def test_deploy_existing_container_restarts_instead_of_recreating(self):
        server = self.server()
        instance = self.instance(server=server)
        fake = FakeSSH(None, existing_id="existing-container-id")

        result = DeployEngine(ssh_client_factory=lambda credentials: fake).deploy(instance, self.runtime_env())
        all_commands = "\n".join(fake.commands)
        self.assertEqual(result.action, "restarted")
        self.assertIn("docker restart qasedak_tenant-a", all_commands)
        self.assertNotIn("docker run -d", all_commands)

    def test_deploy_failure_marks_instance_failed_without_delete(self):
        server = self.server()
        instance = self.instance(server=server)
        fake = FakeSSH(None, fail_pull=True)

        with self.assertRaises(DeploymentError):
            DeployEngine(ssh_client_factory=lambda credentials: fake).deploy(instance, self.runtime_env())
        instance.refresh_from_db()
        self.assertEqual(instance.status, TenantInstance.Status.FAILED)
        self.assertNotIn("docker rm", "\n".join(fake.commands))

    def test_lifecycle_commands_use_remote_docker_and_update_status(self):
        server = self.server()
        instance = self.instance(server=server)
        fake = FakeSSH(None)
        manager = InstanceManager(ssh_client_factory=lambda credentials: fake)

        manager.stop_instance(instance.tenant_id)
        instance.refresh_from_db()
        self.assertEqual(instance.status, TenantInstance.Status.STOPPED)
        manager.restart_instance(instance.tenant_id)
        instance.refresh_from_db()
        self.assertEqual(instance.status, TenantInstance.Status.RUNNING)
        status = manager.status_instance(instance.tenant_id)
        self.assertEqual(status["runtime_state"], "running")
        manager.delete_instance(instance.tenant_id)
        instance.refresh_from_db()
        self.assertEqual(instance.status, TenantInstance.Status.DELETED)
        all_commands = "\n".join(fake.commands)
        self.assertIn("docker stop qasedak_tenant-a", all_commands)
        self.assertIn("docker restart qasedak_tenant-a", all_commands)
        self.assertIn("docker rm -f qasedak_tenant-a", all_commands)

    def test_multi_tenant_deploy_uses_separate_env_files_and_networks(self):
        server = self.server()
        first = self.instance(server=server, tenant_id="tenant-a", container_name="qasedak_tenant-a", port=8001)
        second = self.instance(server=server, tenant_id="tenant-b", container_name="qasedak_tenant-b", port=8002)
        fake = FakeSSH(None)
        engine = DeployEngine(ssh_client_factory=lambda credentials: fake)

        engine.deploy(first, self.runtime_env(token="token-a"))
        engine.deploy(second, self.runtime_env(token="token-b"))

        self.assertIn("/opt/qasedak/tenant-a/.env", fake.files)
        self.assertIn("/opt/qasedak/tenant-b/.env", fake.files)
        self.assertIn("TELEGRAM_BOT_TOKEN=token-a", fake.files["/opt/qasedak/tenant-a/.env"])
        self.assertIn("TELEGRAM_BOT_TOKEN=token-b", fake.files["/opt/qasedak/tenant-b/.env"])
        all_commands = "\n".join(fake.commands)
        self.assertIn("qasedak_tenant-a_net", all_commands)
        self.assertIn("qasedak_tenant-b_net", all_commands)

    def test_subdomain_generation_uses_plain_tenant_domain_and_reserved_slugs(self):
        manager = SubdomainManager()
        self.assertEqual(manager.generate("Tenant-A"), "tenant-a.panelwpvideo.ir")
        self.assertNotIn("bot-", manager.generate("Tenant-A"))
        with self.assertRaises(SubdomainError):
            manager.generate("admin")
