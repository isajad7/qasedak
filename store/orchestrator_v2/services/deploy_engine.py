import json
import logging
import secrets
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.utils import timezone

from store.orchestrator_v2.models import TenantInstance
from store.orchestrator_v2.services.ssh_client import SSHClient, SSHClientError, SSHCredentials


logger = logging.getLogger(__name__)


class DeploymentError(Exception):
    pass


@dataclass
class DeploymentResult:
    container_id: str = ""
    server_ip: str = ""
    instance_url: str = ""
    action: str = ""
    port: int = 0
    health: dict = field(default_factory=dict)
    runtime_state: dict = field(default_factory=dict)


class DeployEngine:
    BASE_REMOTE_ROOT = "/opt/qasedak"

    def __init__(self, ssh_client_factory=None):
        self.ssh_client_factory = ssh_client_factory or SSHClient

    def deploy(self, instance, runtime_env, credentials=None):
        credentials = credentials or self.credentials_for_server(instance.server_node)
        client = self.ssh_client_factory(credentials)
        container_name = instance.container_name
        tenant_id = instance.tenant_id
        image = self._validate_image(instance.docker_image_version or "qasedak-core:latest")
        remote_dir = f"{self.BASE_REMOTE_ROOT}/{tenant_id}"
        env_path = f"{remote_dir}/.env"
        admin_credentials_path = f"{remote_dir}/admin-credentials.txt"
        network_name = f"{container_name}_net"

        instance.status = TenantInstance.Status.DEPLOYING
        instance.deployment_status = TenantInstance.DeploymentStatus.DEPLOYING
        instance.error_message = ""
        instance.save(update_fields=["status", "deployment_status", "error_message", "updated_at"])

        try:
            self._ensure_ok(client.run(f"install -d -m 700 {shlex.quote(remote_dir)}", timeout=60), "create tenant directory")
            self.ensure_docker(client)
            self.pull_image(client, image)
            self.ensure_network(client, network_name)

            env_content = self.build_env_file(instance, runtime_env)
            client.put_file(env_path, env_content, mode="600")
            if runtime_env.get("QASEDAK_ADMIN_USERNAME") and runtime_env.get("QASEDAK_ADMIN_PASSWORD"):
                client.put_file(
                    admin_credentials_path,
                    self.build_admin_credentials_file(instance, runtime_env),
                    mode="600",
                )

            existing_id = self.container_id(client, container_name)
            if existing_id:
                action = "restarted"
                self._ensure_ok(client.run(f"docker restart {shlex.quote(container_name)}", timeout=120), "restart container")
                container_id = existing_id
            else:
                allocated_port = self.allocate_remote_port(client, instance.port, container_name)
                if allocated_port != instance.port:
                    instance.port = allocated_port
                    instance.save(update_fields=["port", "updated_at"])
                container_id = self.run_container(
                    client,
                    container_name=container_name,
                    network_name=network_name,
                    env_path=env_path,
                    port=instance.port,
                    image=image,
                )
                action = "created"

            instance.container_id = container_id
            instance.deployment_status = TenantInstance.DeploymentStatus.CONTAINER_CREATED
            instance.save(update_fields=["container_id", "deployment_status", "updated_at"])

            health = self.check_health(client, instance)
            instance_url = self.instance_url(instance)
            runtime_state = {
                "container_name": container_name,
                "network_name": network_name,
                "remote_dir": remote_dir,
                "docker_image": image,
                "admin_credentials_path": admin_credentials_path,
                "setup_status": "setup_required",
                "setup_checklist_pending": [
                    "store",
                    "telegram",
                    "payment",
                    "xui",
                    "inbound",
                    "plans",
                    "routes",
                    "revenue_dry_run",
                ],
            }
            instance.status = TenantInstance.Status.RUNNING
            instance.deployment_status = TenantInstance.DeploymentStatus.DEPLOYED
            instance.instance_url = instance_url
            instance.last_deployed_at = timezone.now()
            instance.last_health = health
            instance.runtime_state = runtime_state
            instance.error_message = ""
            instance.save(
                update_fields=[
                    "status",
                    "deployment_status",
                    "port",
                    "container_id",
                    "instance_url",
                    "last_deployed_at",
                    "last_health",
                    "runtime_state",
                    "error_message",
                    "updated_at",
                ]
            )
            instance.server_node.refresh_current_instances()
            logger.info("orchestrator_v2_deploy_success tenant=%s server=%s action=%s", tenant_id, instance.server_node_id, action)
            return DeploymentResult(
                container_id=container_id,
                server_ip=instance.server_node.ip,
                instance_url=instance_url,
                action=action,
                port=instance.port,
                health=health,
                runtime_state=runtime_state,
            )
        except Exception as exc:
            instance.status = TenantInstance.Status.FAILED
            instance.deployment_status = TenantInstance.DeploymentStatus.FAILED
            instance.error_message = exc.__class__.__name__
            instance.save(update_fields=["status", "deployment_status", "error_message", "updated_at"])
            logger.warning("orchestrator_v2_deploy_failed tenant=%s server=%s error=%s", tenant_id, instance.server_node_id, exc.__class__.__name__)
            raise DeploymentError(str(exc) or exc.__class__.__name__) from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def credentials_for_server(self, server_node, *, password="", key_path=""):
        return SSHCredentials(
            host=server_node.ip,
            user=server_node.ssh_user,
            port=server_node.ssh_port,
            key_path=key_path or server_node.ssh_key_path,
            password=password,
        )

    def ensure_docker(self, client):
        command = (
            "if command -v docker >/dev/null 2>&1; then exit 0; fi; "
            "if command -v apt-get >/dev/null 2>&1; then "
            "sudo apt-get update -y && sudo apt-get install -y docker.io; "
            "elif command -v dnf >/dev/null 2>&1; then "
            "sudo dnf install -y docker; "
            "elif command -v yum >/dev/null 2>&1; then "
            "sudo yum install -y docker; "
            "else echo 'docker installer unsupported' >&2; exit 42; fi"
        )
        self._ensure_ok(client.run(command, timeout=600), "ensure docker")

    def pull_image(self, client, image):
        quoted = shlex.quote(image)
        command = f"docker pull {quoted} || docker image inspect {quoted} >/dev/null"
        self._ensure_ok(client.run(command, timeout=900), "pull docker image")

    def ensure_network(self, client, network_name):
        quoted = shlex.quote(network_name)
        command = f"docker network inspect {quoted} >/dev/null 2>&1 || docker network create --driver bridge {quoted}"
        self._ensure_ok(client.run(command, timeout=60), "create tenant network")

    def container_id(self, client, container_name):
        template = "{{.ID}}"
        command = f"docker ps -a --filter name=^/{shlex.quote(container_name)}$ --format {shlex.quote(template)}"
        result = client.run(command, timeout=60)
        self._ensure_ok(result, "inspect container")
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def allocate_remote_port(self, client, requested_port, container_name):
        port = int(requested_port)
        while port <= 65535:
            if not self.remote_port_in_use(client, port, container_name):
                return port
            port += 1
        raise DeploymentError("No available remote port found.")

    def remote_port_in_use(self, client, port, container_name):
        template = "{{.Names}} {{.Ports}}"
        command = f"docker ps --format {shlex.quote(template)}"
        result = client.run(command, timeout=60)
        self._ensure_ok(result, "list docker ports")
        needle = f":{int(port)}->"
        for line in result.stdout.splitlines():
            if line.startswith(f"{container_name} "):
                continue
            if needle in line:
                return True
        return False

    def run_container(self, client, *, container_name, network_name, env_path, port, image):
        command = " ".join(
            [
                "docker run -d",
                "--restart unless-stopped",
                "--name",
                shlex.quote(container_name),
                "--network",
                shlex.quote(network_name),
                "--add-host",
                "host.docker.internal:host-gateway",
                "-p",
                shlex.quote(f"127.0.0.1:{int(port)}:8000"),
                "--env-file",
                shlex.quote(env_path),
                shlex.quote(image),
            ]
        )
        result = client.run(command, timeout=180)
        self._ensure_ok(result, "run container")
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def check_health(self, client, instance):
        local_url = f"http://127.0.0.1:{int(instance.port)}/health"
        public_url = f"http://{instance.server_node.ip}:{int(instance.port)}/health"
        command = (
            f"curl -fsS --max-time 15 {shlex.quote(local_url)} "
            f"|| curl -fsS --max-time 15 {shlex.quote(public_url)}"
        )
        result = client.run(command, timeout=30)
        if not result.ok:
            return {"status": "degraded", "error": result.stderr[:300]}
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            payload = {"raw": result.stdout[:500]}
        return {
            "status": "ok",
            "url": public_url,
            "payload": payload,
        }

    def instance_url(self, instance):
        if instance.domain:
            parsed = urlparse(instance.domain)
            if parsed.scheme:
                return instance.domain.rstrip("/")
            return f"https://{instance.domain}".rstrip("/")
        return f"http://{instance.server_node.ip}:{int(instance.port)}"

    def build_env_file(self, instance, runtime_env):
        values = {
            "TENANT_ID": instance.tenant_id,
            "PORT": "8000",
            "DOMAIN": instance.domain or "",
            "REVENUE_ENGINE_DRY_RUN": str(runtime_env.get("REVENUE_ENGINE_DRY_RUN", "true")).lower(),
            "QASEDAK_BOT_ENABLED": str(runtime_env.get("QASEDAK_BOT_ENABLED", "false")).lower(),
            "QASEDAK_WORKER_ENABLED": str(runtime_env.get("QASEDAK_WORKER_ENABLED", "false")).lower(),
            "QASEDAK_BOOTSTRAP_TENANT": str(runtime_env.get("QASEDAK_BOOTSTRAP_TENANT", "false")).lower(),
            "DJANGO_SETTINGS_MODULE": "core.settings.production",
            "DJANGO_SECRET_KEY": runtime_env.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50),
        }
        for key in ("QASEDAK_ADMIN_USERNAME", "QASEDAK_ADMIN_PASSWORD", "TENANT_DISPLAY_NAME"):
            if runtime_env.get(key):
                values[key] = runtime_env[key]
        if runtime_env.get("TELEGRAM_BOT_TOKEN"):
            values["TELEGRAM_BOT_TOKEN"] = runtime_env["TELEGRAM_BOT_TOKEN"]
        if runtime_env.get("DATABASE_URL"):
            values["DATABASE_URL"] = runtime_env["DATABASE_URL"]
        else:
            for key in ("DATABASE_ENGINE", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_SSLMODE"):
                if runtime_env.get(key):
                    values[key] = runtime_env[key]
        allowed_hosts = [instance.server_node.ip, "127.0.0.1", "localhost"]
        public_host = instance.subdomain or instance.domain
        if public_host:
            allowed_hosts.insert(0, public_host.replace("https://", "").replace("http://", "").strip("/"))
        values["DJANGO_ALLOWED_HOSTS"] = ",".join(dict.fromkeys(filter(None, allowed_hosts)))
        if public_host:
            host = public_host.rstrip("/")
            values["DJANGO_CSRF_TRUSTED_ORIGINS"] = host if host.startswith(("http://", "https://")) else f"https://{host},http://{host}"
        return self._format_env(values)

    def build_admin_credentials_file(self, instance, runtime_env):
        instance_url = self.instance_url(instance).rstrip("/")
        values = {
            "TENANT_ID": instance.tenant_id,
            "TENANT_URL": instance_url,
            "ADMIN_URL": f"{instance_url}/admin/",
            "ADMIN_USERNAME": runtime_env.get("QASEDAK_ADMIN_USERNAME", ""),
            "ADMIN_PASSWORD": runtime_env.get("QASEDAK_ADMIN_PASSWORD", ""),
            "SETUP_STATUS": "setup_required",
        }
        return self._format_env(values)

    def _format_env(self, values):
        lines = []
        for key, value in values.items():
            if not key.replace("_", "").isalnum() or not key.upper() == key:
                raise DeploymentError(f"Invalid env key: {key}")
            text = str(value)
            if "\x00" in text or "\n" in text or "\r" in text:
                raise DeploymentError(f"Invalid env value for {key}")
            lines.append(f"{key}={text}")
        return "\n".join(lines) + "\n"

    def _validate_image(self, image):
        text = str(image or "").strip()
        if not text or len(text) > 255:
            raise DeploymentError("Invalid Docker image.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/:@-")
        if any(char not in allowed for char in text):
            raise DeploymentError("Invalid Docker image.")
        return text

    def _ensure_ok(self, result, step):
        if not getattr(result, "ok", False):
            stderr = getattr(result, "stderr", "") or getattr(result, "stdout", "")
            raise DeploymentError(f"Remote step failed: {step}: {stderr[:300]}")
