import pathlib
import subprocess
import tempfile
import json

from django.core.management import call_command
from django.test import TestCase
from io import StringIO
from unittest.mock import patch

from store.deployment.nginx_generator import NginxConfigGenerator
from store.deployment.owner_toolkit import OwnerToolkitError, TenantCommandResult, TenantOwnerToolkit
from store.deployment.port_allocator import PortAllocator
from store.deployment.service import DockerRunner, SubdomainDeploymentError, SubdomainDeploymentService
from store.deployment.subdomain_manager import SubdomainError, SubdomainManager
from store.orchestrator_v2.models import ServerNode, TenantInstance


class FakeCommandRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, timeout=60, input=None):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class FakeOwnerCommandRunner(FakeCommandRunner):
    def __init__(self, *, dns_ip="127.0.0.1", shell_state=None, existing_bot_id="", webhook_status="deleted"):
        super().__init__()
        self.dns_ip = dns_ip
        self.existing_bot_id = existing_bot_id
        self.webhook_status = webhook_status
        self.shell_state = shell_state or {
            "db_connectivity": "postgresql",
            "setup_status": "setup_required",
            "bot_configured": False,
            "xui_configured": False,
            "revenue_dry_run": True,
        }

    def run(self, argv, *, timeout=60, input=None):
        self.calls.append(list(argv))
        if argv[:4] == ["docker", "exec", "qasedak_tenant-a", "python"] and "migrate" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:4] == ["docker", "exec", "qasedak_tenant-a", "python"] and "shell" in argv:
            script = argv[-1]
            if "getWebhookInfo" in script:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"webhook_status": self.webhook_status}) + "\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.shell_state) + "\n", stderr="")
        if argv[:4] == ["docker", "ps", "-a", "--filter"]:
            name_filter = argv[4] if len(argv) > 4 else ""
            if "qasedak_tenant-a_bot" in name_filter:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{self.existing_bot_id}\n" if self.existing_bot_id else "", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["docker", "run", "-d"]:
            return subprocess.CompletedProcess(argv, 0, stdout="new-bot-worker-id\n", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="running\n", stderr="")
        if argv[:2] in (["docker", "restart"], ["docker", "stop"], ["docker", "start"]):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv and argv[0] == "curl":
            return subprocess.CompletedProcess(argv, 0, stdout="200", stderr="")
        if argv and argv[0] == "dig":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.dns_ip}\n" if self.dns_ip else "", stderr="")
        if argv[0] in {"nginx", "systemctl", "certbot"}:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class FakeDockerRunner:
    def __init__(self, *, fail_run=False):
        self.fail_run = fail_run
        self.runs = []
        self.removed = []
        self.bootstraps = []

    def run_container(self, *, container_name, port, env_file, image):
        self.runs.append(
            {
                "container_name": container_name,
                "port": port,
                "env_file": str(env_file),
                "image": image,
            }
        )
        if self.fail_run:
            raise SubdomainDeploymentError("docker failed")
        return f"container-{container_name}"

    def bootstrap_tenant(self, *, container_name, config):
        self.bootstraps.append({"container_name": container_name, "config": config})

    def remove_container(self, container_name):
        self.removed.append(container_name)


class FakeDatabaseProvisioner:
    def __init__(self):
        self.created = []
        self.rolled_back = []

    def preview(self, tenant_id):
        return {
            "database": f"qasedak_{tenant_id.replace('-', '_')}",
            "user": f"qasedak_{tenant_id.replace('-', '_')}_app",
            "host": "host.docker.internal",
            "port": "5432",
            "sslmode": "prefer",
        }

    def create(self, tenant_id):
        info = {
            **self.preview(tenant_id),
            "password": f"tenant-password-{tenant_id}",
            "created": True,
        }
        self.created.append(info)
        return info

    def rollback(self, info):
        self.rolled_back.append(info)


class FailingNginxGenerator(NginxConfigGenerator):
    def reload(self):
        raise RuntimeError("nginx reload failed")


class SubdomainDeploymentTests(TestCase):
    def setUp(self):
        self.server = ServerNode.objects.create(
            name="local",
            ip="127.0.0.1",
            ssh_user="root",
            max_instances=100,
        )

    def tenant(self, tenant_id, port=None):
        return TenantInstance.objects.create(
            tenant_id=tenant_id,
            server_node=self.server,
            container_name=f"qasedak_{tenant_id}",
            port=port,
            docker_image_version="qasedak-core:latest",
        )

    def test_port_allocation_uniqueness_and_collision_prevention(self):
        allocator = PortAllocator()
        first = self.tenant("tenant-a")
        second = self.tenant("tenant-b")

        self.assertEqual(allocator.allocate(first), 8001)
        self.assertEqual(allocator.allocate(second), 8002)
        self.assertEqual(allocator.next_free_port(self.server), 8003)

    def test_subdomain_generation_and_mapping(self):
        instance = self.tenant("tenant-a", port=8001)
        manager = SubdomainManager()

        subdomain = manager.assign(instance, "yourdomain.com")
        instance.refresh_from_db()

        self.assertEqual(subdomain, "tenant-a.yourdomain.com")
        self.assertEqual(
            manager.mapping(instance),
            {
                "tenant_id": "tenant-a",
                "subdomain": "tenant-a.yourdomain.com",
                "port": 8001,
                "container_id": "",
            },
        )

    def test_subdomain_generation_sanitizes_and_rejects_unsafe_slugs(self):
        manager = SubdomainManager()

        self.assertEqual(manager.generate("client1", "example.com"), "client1.example.com")
        self.assertEqual(manager.generate("dry_run-tenant", "example.com"), "dry-run-tenant.example.com")
        self.assertEqual(manager.generate(" Client_One!! ", "example.com"), "client-one.example.com")
        self.assertNotIn("_", manager.generate("dry_run-tenant", "example.com"))
        with self.assertRaises(SubdomainError):
            manager.generate("admin", "example.com")
        with self.assertRaises(SubdomainError):
            manager.generate("a" * 51, "example.com")

    def test_subdomain_duplicate_is_rejected_after_slug_normalization(self):
        first = self.tenant("client-one", port=8001)
        second = self.tenant("client_one", port=8002)
        manager = SubdomainManager()

        manager.assign(first, "example.com")
        with self.assertRaises(SubdomainError):
            manager.assign(second, "example.com")

    def test_nginx_config_generation_and_enablement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runner = FakeCommandRunner()
            generator = NginxConfigGenerator(
                available_root=root / "available" / "qasedak",
                enabled_root=root / "enabled",
                command_runner=runner,
            )

            path = generator.write_config(
                tenant_id="tenant-a",
                subdomain="tenant-a.yourdomain.com",
                port=8001,
            )
            enabled = generator.enable_site("tenant-a")
            generator.reload()

            content = pathlib.Path(path).read_text(encoding="utf-8")
            self.assertIn("server_name tenant-a.yourdomain.com;", content)
            self.assertIn("proxy_pass http://127.0.0.1:8001;", content)
            self.assertTrue(pathlib.Path(enabled).is_symlink())
            self.assertEqual(runner.calls[0], ["nginx", "-t"])

    def test_docker_runner_binds_tenant_port_to_loopback(self):
        runner = FakeCommandRunner()
        docker = DockerRunner(command_runner=runner)

        docker.run_container(
            container_name="qasedak_tenant-a",
            port=8123,
            env_file=pathlib.Path("/tmp/tenant.env"),
            image="qasedak-core:latest",
        )

        run_argv = runner.calls[1]
        self.assertIn("--add-host", run_argv)
        self.assertIn("host.docker.internal:host-gateway", run_argv)
        self.assertIn("127.0.0.1:8123:8000", run_argv)
        self.assertNotIn("8123:8000", run_argv)

    def test_preview_instance_is_non_mutating_and_dns_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            service = SubdomainDeploymentService(
                nginx_generator=NginxConfigGenerator(
                    available_root=root / "available" / "qasedak",
                    enabled_root=root / "enabled",
                    command_runner=FakeCommandRunner(),
                ),
                instance_root=root / "instances",
            )

            before = TenantInstance.objects.count()
            preview = service.preview_instance(
                "dry_run-tenant",
                base_domain="example.com",
                server_node_id=self.server.pk,
            )

            self.assertEqual(TenantInstance.objects.count(), before)
            self.assertEqual(preview["tenant_id"], "dry-run-tenant")
            self.assertEqual(preview["subdomain"], "dry-run-tenant.example.com")
            self.assertEqual(preview["port"], 8001)
            self.assertEqual(preview["container"], "qasedak_dry-run-tenant")
            self.assertEqual(preview["database"]["database"], "qasedak_dry_run_tenant")
            self.assertEqual(preview["database"]["user"], "qasedak_dry_run_tenant_app")
            self.assertTrue(preview["env_path"].endswith("/dry-run-tenant/.env"))

    def test_deploy_instance_creates_container_and_nginx_without_secret_command_leakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runner = FakeCommandRunner()
            docker = FakeDockerRunner()
            service = SubdomainDeploymentService(
                docker_runner=docker,
                nginx_generator=NginxConfigGenerator(
                    available_root=root / "available" / "qasedak",
                    enabled_root=root / "enabled",
                    command_runner=runner,
                ),
                instance_root=root / "instances",
            )

            result = service.deploy_instance(
                "tenant-a",
                base_domain="yourdomain.com",
                database_url="postgres://user:db-secret@db/qasedak",
                server_node_id=self.server.pk,
                bootstrap_tenant=True,
                tenant_display_name="Tenant A Store",
            )

            instance = TenantInstance.objects.get(tenant_id="tenant-a")
            self.assertEqual(result["subdomain"], "tenant-a.yourdomain.com")
            self.assertEqual(result["port"], 8001)
            self.assertEqual(result["container"], "qasedak_tenant-a")
            self.assertEqual(instance.deployment_status, TenantInstance.DeploymentStatus.DEPLOYED)
            self.assertEqual(instance.container_id, "container-qasedak_tenant-a")
            env_content = pathlib.Path(docker.runs[0]["env_file"]).read_text(encoding="utf-8")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env_content)
            self.assertIn("DOMAIN=tenant-a.yourdomain.com", env_content)
            self.assertIn("QASEDAK_BOT_ENABLED=false", env_content)
            self.assertIn("QASEDAK_WORKER_ENABLED=false", env_content)
            self.assertNotIn("db-secret", str(docker.runs))
            self.assertEqual(len(docker.bootstraps), 1)
            bootstrap_config = docker.bootstraps[0]["config"]
            self.assertFalse(bootstrap_config["telegram"]["enabled"])
            self.assertTrue(bootstrap_config["telegram"]["create_inactive_placeholder"])

    def test_provisioned_database_env_does_not_inherit_control_database_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            docker = FakeDockerRunner()
            database = FakeDatabaseProvisioner()
            service = SubdomainDeploymentService(
                docker_runner=docker,
                database_provisioner=database,
                nginx_generator=NginxConfigGenerator(
                    available_root=root / "available" / "qasedak",
                    enabled_root=root / "enabled",
                    command_runner=FakeCommandRunner(),
                ),
                instance_root=root / "instances",
            )

            service.deploy_instance(
                "tenant-a",
                base_domain="yourdomain.com",
                database_url="postgres://control:control-secret@db/qasedak_control",
                server_node_id=self.server.pk,
                provision_database=True,
            )

            env_content = pathlib.Path(docker.runs[0]["env_file"]).read_text(encoding="utf-8")
            self.assertNotIn("DATABASE_URL=", env_content)
            self.assertNotIn("control-secret", env_content)
            self.assertIn("DATABASE_ENGINE=postgres", env_content)
            self.assertIn("POSTGRES_DB=qasedak_tenant_a", env_content)
            self.assertIn("POSTGRES_USER=qasedak_tenant_a_app", env_content)
            self.assertIn("POSTGRES_PASSWORD=tenant-password-tenant-a", env_content)
            self.assertIn("POSTGRES_HOST=host.docker.internal", env_content)

    def test_rollback_on_nginx_failure_removes_container_config_and_frees_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            docker = FakeDockerRunner()
            service = SubdomainDeploymentService(
                docker_runner=docker,
                nginx_generator=FailingNginxGenerator(
                    available_root=root / "available" / "qasedak",
                    enabled_root=root / "enabled",
                    command_runner=FakeCommandRunner(),
                ),
                instance_root=root / "instances",
            )

            with self.assertRaises(SubdomainDeploymentError):
                service.deploy_instance(
                    "tenant-a",
                    base_domain="yourdomain.com",
                    database_url="postgres://user:db-secret@db/qasedak",
                    server_node_id=self.server.pk,
                )

            instance = TenantInstance.objects.get(tenant_id="tenant-a")
            self.assertEqual(instance.status, TenantInstance.Status.FAILED)
            self.assertEqual(instance.deployment_status, TenantInstance.DeploymentStatus.FAILED)
            self.assertIsNone(instance.port)
            self.assertEqual(docker.removed, ["qasedak_tenant-a"])
            self.assertFalse((root / "available" / "qasedak" / "qasedak_tenant-a.conf").exists())

    def test_tenant_isolation_unique_ports_subdomains_and_env_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            docker = FakeDockerRunner()
            service = SubdomainDeploymentService(
                docker_runner=docker,
                nginx_generator=NginxConfigGenerator(
                    available_root=root / "available" / "qasedak",
                    enabled_root=root / "enabled",
                    command_runner=FakeCommandRunner(),
                ),
                instance_root=root / "instances",
            )

            first = service.deploy_instance(
                "tenant-a",
                base_domain="yourdomain.com",
                database_url="postgres://user:a@db/qasedak",
                server_node_id=self.server.pk,
            )
            second = service.deploy_instance(
                "tenant-b",
                base_domain="yourdomain.com",
                database_url="postgres://user:b@db/qasedak",
                server_node_id=self.server.pk,
            )

            self.assertNotEqual(first["port"], second["port"])
            self.assertNotEqual(first["subdomain"], second["subdomain"])
            first_env = pathlib.Path(docker.runs[0]["env_file"]).read_text(encoding="utf-8")
            second_env = pathlib.Path(docker.runs[1]["env_file"]).read_text(encoding="utf-8")
            self.assertIn("TENANT_ID=tenant-a", first_env)
            self.assertIn("TENANT_ID=tenant-b", second_env)
            self.assertNotIn("TELEGRAM_BOT_TOKEN", first_env)
            self.assertNotIn("TELEGRAM_BOT_TOKEN", second_env)


class TenantOwnerToolkitTests(TestCase):
    def setUp(self):
        self.server = ServerNode.objects.create(
            name="local",
            ip="127.0.0.1",
            ssh_user="root",
            max_instances=100,
        )

    def toolkit(self, *, tmp, docker=None, database=None, runner=None):
        root = pathlib.Path(tmp)
        service = SubdomainDeploymentService(
            docker_runner=docker or FakeDockerRunner(),
            database_provisioner=database or FakeDatabaseProvisioner(),
            nginx_generator=NginxConfigGenerator(
                available_root=root / "available" / "qasedak",
                enabled_root=root / "enabled",
                command_runner=FakeCommandRunner(),
            ),
            instance_root=root / "instances",
        )
        return TenantOwnerToolkit(
            deployment_service=service,
            command_runner=runner or FakeOwnerCommandRunner(),
            sleep=lambda _seconds: None,
        ), service

    def instance(self, tenant_id="tenant-a", **overrides):
        data = {
            "tenant_id": tenant_id,
            "server_node": self.server,
            "container_name": f"qasedak_{tenant_id}",
            "port": 8001,
            "subdomain": f"{tenant_id}.example.com",
            "domain": f"{tenant_id}.example.com",
            "instance_url": f"https://{tenant_id}.example.com",
            "status": TenantInstance.Status.RUNNING,
            "deployment_status": TenantInstance.DeploymentStatus.DEPLOYED,
            "runtime_state": {"admin_credentials_path": f"/opt/qasedak-tenants/{tenant_id}/admin-credentials.txt"},
        }
        data.update(overrides)
        return TenantInstance.objects.create(**data)

    def test_create_tenant_is_tokenless_and_prints_only_safe_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            docker = FakeDockerRunner()
            runner = FakeOwnerCommandRunner()
            toolkit, _service = self.toolkit(tmp=tmp, docker=docker, runner=runner)

            result = toolkit.create_tenant(
                tenant_id="tenant-a",
                base_domain="example.com",
                display_name="Tenant A",
                server_node_id=self.server.pk,
            )

            safe = json.dumps(result.safe_dict(), sort_keys=True)
            self.assertIn("tenant-a", safe)
            self.assertIn("credential_file", safe)
            self.assertNotIn("password", safe.lower())
            self.assertNotIn("token", safe.lower())
            env_content = pathlib.Path(docker.runs[0]["env_file"]).read_text(encoding="utf-8")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env_content)
            self.assertNotIn("DATABASE_URL=", env_content)
            self.assertIn("QASEDAK_BOT_ENABLED=false", env_content)
            bootstrap_config = docker.bootstraps[0]["config"]
            self.assertFalse(bootstrap_config["telegram"]["enabled"])
            self.assertTrue(bootstrap_config["telegram"]["create_inactive_placeholder"])
            commands = "\n".join(" ".join(call) for call in runner.calls)
            self.assertNotIn("qasedak_tenant-a_bot", commands)
            instance = TenantInstance.objects.get(tenant_id="tenant-a")
            self.assertEqual(instance.status, TenantInstance.Status.RUNNING)
            self.assertTrue(instance.runtime_state["env_path"].endswith("/tenant-a/.env"))
            self.assertTrue((pathlib.Path(tmp) / "instances" / "tenant-a" / "admin-credentials.txt").exists())

    def test_create_tenant_rejects_duplicate_tenant(self):
        self.instance()
        with tempfile.TemporaryDirectory() as tmp:
            toolkit, _service = self.toolkit(tmp=tmp)
            with self.assertRaises(OwnerToolkitError):
                toolkit.create_tenant(tenant_id="tenant-a", base_domain="example.com", display_name="Tenant A")

    def test_create_tenant_rejects_invalid_and_reserved_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            toolkit, _service = self.toolkit(tmp=tmp)
            with self.assertRaises(OwnerToolkitError):
                toolkit.create_tenant(tenant_id="admin", base_domain="example.com", display_name="Admin")
            with self.assertRaises(OwnerToolkitError):
                toolkit.create_tenant(tenant_id="x", base_domain="example.com", display_name="X")

    def test_tenant_status_masks_secrets(self):
        self.instance()
        runner = FakeOwnerCommandRunner(
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "setup_required",
                "bot_configured": False,
                "xui_configured": False,
                "revenue_dry_run": True,
            }
        )
        result = TenantOwnerToolkit(command_runner=runner).tenant_status(tenant_id="tenant-a")

        safe = json.dumps(result.safe_dict(), sort_keys=True)
        self.assertIn("setup_required", safe)
        self.assertIn("bot_worker_status", result.safe_dict())
        self.assertEqual(result.safe_dict()["bot_worker_status"], "not_configured")
        self.assertEqual(result.safe_dict()["webhook_status"], "not_configured")
        self.assertNotIn("POSTGRES_PASSWORD", safe)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", safe)
        self.assertNotIn("admin-password", safe)

    def test_tenant_status_reports_bot_worker_and_webhook_status(self):
        self.instance(runtime_state={"bot_configured": True})
        runner = FakeOwnerCommandRunner(
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "ready",
                "bot_configured": True,
                "xui_configured": True,
                "revenue_dry_run": True,
            },
            existing_bot_id="bot-worker-id",
            webhook_status="deleted",
        )

        result = TenantOwnerToolkit(command_runner=runner).tenant_status(tenant_id="tenant-a")
        safe = result.safe_dict()

        self.assertEqual(safe["bot_worker_status"], "running")
        self.assertEqual(safe["webhook_status"], "deleted")
        self.assertNotIn("telegram-secret", json.dumps(safe))

    def test_start_or_restart_bot_worker_uses_tenant_env_and_host_gateway(self):
        self.instance(
            runtime_state={
                "env_path": "/opt/qasedak-tenants/tenant-a/.env",
                "bot_configured": True,
            }
        )
        runner = FakeOwnerCommandRunner(
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "ready",
                "bot_configured": True,
                "xui_configured": True,
                "revenue_dry_run": True,
            }
        )

        result = TenantOwnerToolkit(command_runner=runner).start_or_restart_bot_worker(tenant_id="tenant-a")

        run_argv = next(call for call in runner.calls if call[:3] == ["docker", "run", "-d"])
        self.assertIn("--name", run_argv)
        self.assertIn("qasedak_tenant-a_bot", run_argv)
        self.assertIn("--add-host", run_argv)
        self.assertIn("host.docker.internal:host-gateway", run_argv)
        self.assertIn("--env-file", run_argv)
        self.assertIn("/opt/qasedak-tenants/tenant-a/.env", run_argv)
        self.assertIn("/app/docker/start-bot.sh", run_argv)
        self.assertNotIn("-p", run_argv)
        self.assertEqual(result.safe_dict()["bot_worker_status"], "running")

    def test_restart_affects_only_target_tenant(self):
        self.instance("tenant-a")
        self.instance("tenant-b", port=8002)
        runner = FakeOwnerCommandRunner()

        TenantOwnerToolkit(command_runner=runner).restart_tenant(tenant_id="tenant-a")

        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertIn("docker restart qasedak_tenant-a", commands)
        self.assertNotIn("qasedak_tenant-b", commands)
        self.assertNotIn("qasedak_tenant-a_bot", commands)

    def test_restart_tenant_restarts_existing_bot_worker_when_configured(self):
        self.instance("tenant-a", runtime_state={"bot_configured": True})
        runner = FakeOwnerCommandRunner(
            existing_bot_id="bot-worker-id",
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "ready",
                "bot_configured": True,
                "xui_configured": True,
                "revenue_dry_run": True,
            },
        )

        TenantOwnerToolkit(command_runner=runner).restart_tenant(tenant_id="tenant-a")

        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertIn("docker restart qasedak_tenant-a", commands)
        self.assertIn("docker restart qasedak_tenant-a_bot", commands)

    def test_suspend_does_not_delete_database_env_or_credentials(self):
        instance = self.instance()
        runner = FakeOwnerCommandRunner()

        TenantOwnerToolkit(command_runner=runner).suspend_tenant(tenant_id="tenant-a", reason="subscription overdue")

        instance.refresh_from_db()
        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertEqual(instance.status, TenantInstance.Status.STOPPED)
        self.assertTrue(instance.runtime_state["suspended"])
        self.assertIn("subscription overdue", instance.runtime_state["suspend_reason"])
        self.assertIn("docker stop qasedak_tenant-a", commands)
        self.assertNotIn("docker rm", commands)
        self.assertNotIn("DROP DATABASE", commands)
        self.assertIn("admin_credentials_path", instance.runtime_state)

    def test_suspend_stops_bot_worker_when_configured(self):
        instance = self.instance(runtime_state={"bot_configured": True})
        runner = FakeOwnerCommandRunner(
            existing_bot_id="bot-worker-id",
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "ready",
                "bot_configured": True,
                "xui_configured": True,
                "revenue_dry_run": True,
            },
        )

        TenantOwnerToolkit(command_runner=runner).suspend_tenant(tenant_id="tenant-a", reason="subscription overdue")

        instance.refresh_from_db()
        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertIn("docker stop qasedak_tenant-a_bot", commands)
        self.assertIn("docker stop qasedak_tenant-a", commands)
        self.assertEqual(instance.runtime_state["bot_worker_status"], "stopped")

    def test_resume_starts_target_tenant(self):
        instance = self.instance(status=TenantInstance.Status.STOPPED, runtime_state={"suspended": True})
        runner = FakeOwnerCommandRunner()

        TenantOwnerToolkit(command_runner=runner).resume_tenant(tenant_id="tenant-a")

        instance.refresh_from_db()
        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertEqual(instance.status, TenantInstance.Status.RUNNING)
        self.assertFalse(instance.runtime_state["suspended"])
        self.assertIn("docker start qasedak_tenant-a", commands)

    def test_resume_starts_existing_bot_worker_when_configured(self):
        instance = self.instance(
            status=TenantInstance.Status.STOPPED,
            runtime_state={"suspended": True, "bot_configured": True},
        )
        runner = FakeOwnerCommandRunner(
            existing_bot_id="bot-worker-id",
            shell_state={
                "db_connectivity": "postgresql",
                "setup_status": "ready",
                "bot_configured": True,
                "xui_configured": True,
                "revenue_dry_run": True,
            },
        )

        TenantOwnerToolkit(command_runner=runner).resume_tenant(tenant_id="tenant-a")

        instance.refresh_from_db()
        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertIn("docker start qasedak_tenant-a", commands)
        self.assertIn("docker start qasedak_tenant-a_bot", commands)
        self.assertEqual(instance.runtime_state["bot_worker_status"], "running")

    def test_enable_https_handles_dns_not_ready_without_certbot(self):
        self.instance()
        runner = FakeOwnerCommandRunner(dns_ip="")

        with self.assertRaises(OwnerToolkitError):
            TenantOwnerToolkit(command_runner=runner).enable_https(
                tenant_id="tenant-a",
                email="admin@example.com",
                server_ip="203.0.113.10",
            )

        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertNotIn("certbot", commands)

    def test_management_command_output_does_not_print_secret_fields(self):
        class FakeToolkit:
            def tenant_status(self, *, tenant_id):
                return TenantCommandResult(
                    tenant_id=tenant_id,
                    status="running",
                    url="https://tenant-a.example.com",
                    container="qasedak_tenant-a",
                    port=8001,
                    credential_file="/opt/qasedak-tenants/tenant-a/admin-credentials.txt",
                    details={"setup_status": "setup_required", "bot_configured": False},
                )

        out = StringIO()

        with patch("store.management.commands._tenant_command.TenantOwnerToolkit", return_value=FakeToolkit()):
            call_command("tenant_status", "--tenant-id", "tenant-a", stdout=out)

        output = out.getvalue()
        self.assertIn("tenant-a", output)
        self.assertNotIn("POSTGRES_PASSWORD", output)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", output)
        self.assertNotIn("admin-password", output)
