import json
import secrets
import socket
import time
from dataclasses import dataclass

from django.utils import timezone

from store.deployment.nginx_generator import LocalCommandRunner
from store.deployment.service import SubdomainDeploymentError, SubdomainDeploymentService
from store.deployment.subdomain_manager import SubdomainError
from store.orchestrator_v2.models import TenantInstance


class OwnerToolkitError(Exception):
    pass


@dataclass
class TenantCommandResult:
    tenant_id: str
    status: str
    url: str = ""
    container: str = ""
    port: int | None = None
    credential_file: str = ""
    details: dict | None = None

    def safe_dict(self):
        data = {
            "tenant_id": self.tenant_id,
            "status": self.status,
            "url": self.url,
            "container": self.container,
            "port": self.port,
            "credential_file": self.credential_file,
        }
        if self.details:
            data.update(self.details)
        return {key: value for key, value in data.items() if value not in ("", None, {})}


class TenantOwnerToolkit:
    def __init__(self, *, deployment_service=None, command_runner=None, sleep=time.sleep):
        self.deployment_service = deployment_service or SubdomainDeploymentService()
        self.command_runner = command_runner or LocalCommandRunner()
        self.sleep = sleep

    def create_tenant(
        self,
        *,
        tenant_id,
        base_domain,
        display_name,
        tenant_admin_username="admin",
        docker_image="qasedak-core:latest",
        server_node_id=None,
    ):
        self._validate_new_tenant(tenant_id)
        try:
            result = self.deployment_service.deploy_instance(
                tenant_id,
                base_domain=base_domain,
                docker_image=docker_image,
                provision_database=True,
                bootstrap_tenant=False,
                tenant_display_name=display_name,
                tenant_admin_username=tenant_admin_username,
                enable_bot_runtime=False,
                server_node_id=server_node_id,
            )
        except (SubdomainDeploymentError, SubdomainError) as exc:
            raise OwnerToolkitError(str(exc)) from exc

        instance = TenantInstance.objects.get(tenant_id=result["tenant_id"])
        self._wait_for_migrations(instance)
        admin_password = secrets.token_urlsafe(24)
        credential_file = self.deployment_service._write_admin_credentials_file(
            instance,
            username=tenant_admin_username,
            password=admin_password,
        )
        config = self.deployment_service._bootstrap_config(
            instance,
            display_name=display_name,
            admin_username=tenant_admin_username,
        )
        config["admin"].pop("password_env", None)
        config["admin"]["password"] = admin_password
        self.deployment_service.docker_runner.bootstrap_tenant(container_name=instance.container_name, config=config)
        runtime_state = dict(instance.runtime_state or {})
        runtime_state["admin_credentials_path"] = credential_file
        runtime_state["bootstrap"] = "owner-toolkit-after-migrate"
        instance.runtime_state = runtime_state
        instance.save(update_fields=["runtime_state", "updated_at"])
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=credential_file,
        )

    def tenant_status(self, *, tenant_id):
        instance = self._instance(tenant_id)
        container_status = self._docker_inspect(instance.container_name)
        health = self._http_status(f"http://127.0.0.1:{instance.port}/health/") if instance.port else {"status": "unavailable"}
        tenant_state = self._tenant_db_state(instance)
        bot_configured = self._is_bot_configured(instance, tenant_state)
        bot_worker_status = (
            self._docker_inspect(self._bot_worker_container_name(instance)) if bot_configured else "not_configured"
        )
        webhook_status = self._telegram_webhook_status(instance) if bot_configured else "not_configured"
        https = self._http_status(f"https://{instance.subdomain}/health/") if instance.subdomain else {"status": "unavailable"}
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=(instance.runtime_state or {}).get("admin_credentials_path", ""),
            details={
                "subdomain": instance.subdomain,
                "container_status": container_status,
                "health_status": health.get("status"),
                "db_connectivity": tenant_state.get("db_connectivity", "unknown"),
                "setup_status": tenant_state.get("setup_status", "unknown"),
                "bot_configured": bot_configured,
                "bot_worker_status": bot_worker_status,
                "webhook_status": webhook_status,
                "xui_configured": tenant_state.get("xui_configured", False),
                "revenue_dry_run": tenant_state.get("revenue_dry_run", None),
                "https_status": https.get("status"),
            },
        )

    def restart_tenant(self, *, tenant_id):
        instance = self._instance(tenant_id)
        self._run(["docker", "restart", instance.container_name], timeout=120)
        tenant_state = self._tenant_db_state(instance)
        bot_configured = self._is_bot_configured(instance, tenant_state)
        bot_worker_status = (
            self._run_bot_worker(instance, existing_action="restart") if bot_configured else "not_configured"
        )
        instance.status = TenantInstance.Status.RUNNING
        instance.runtime_state = self._with_bot_runtime_state(
            instance,
            last_action="restarted",
            bot_configured=bot_configured,
            bot_worker_status=bot_worker_status,
        )
        instance.save(update_fields=["status", "runtime_state", "updated_at"])
        health = self._http_status(f"http://127.0.0.1:{instance.port}/health/") if instance.port else {"status": "unavailable"}
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=(instance.runtime_state or {}).get("admin_credentials_path", ""),
            details={"health_status": health.get("status"), "bot_worker_status": bot_worker_status},
        )

    def suspend_tenant(self, *, tenant_id, reason=""):
        instance = self._instance(tenant_id)
        bot_configured = self._is_bot_configured(instance)
        bot_worker_status = self._stop_bot_worker(instance) if bot_configured else "not_configured"
        result = self.command_runner.run(["docker", "stop", instance.container_name], timeout=120)
        if result.returncode != 0 and "No such container" not in (result.stderr or ""):
            raise OwnerToolkitError((result.stderr or "Could not stop tenant container.").strip())
        runtime_state = {
            **(instance.runtime_state or {}),
            "suspended": True,
            "suspend_reason": str(reason or "").strip(),
            "suspended_at": timezone.now().isoformat(),
            "last_action": "suspended",
            "bot_configured": bot_configured,
            "bot_worker_container": self._bot_worker_container_name(instance) if bot_configured else "",
            "bot_worker_status": bot_worker_status,
        }
        instance.status = TenantInstance.Status.STOPPED
        instance.runtime_state = runtime_state
        instance.save(update_fields=["status", "runtime_state", "updated_at"])
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=runtime_state.get("admin_credentials_path", ""),
            details={"suspended": True, "bot_worker_status": bot_worker_status},
        )

    def resume_tenant(self, *, tenant_id):
        instance = self._instance(tenant_id)
        self._run(["docker", "start", instance.container_name], timeout=120)
        tenant_state = self._tenant_db_state(instance)
        bot_configured = self._is_bot_configured(instance, tenant_state)
        bot_worker_status = self._run_bot_worker(instance, existing_action="start") if bot_configured else "not_configured"
        runtime_state = dict(instance.runtime_state or {})
        runtime_state["suspended"] = False
        runtime_state["last_action"] = "resumed"
        runtime_state["bot_configured"] = bot_configured
        runtime_state["bot_worker_container"] = self._bot_worker_container_name(instance) if bot_configured else ""
        runtime_state["bot_worker_status"] = bot_worker_status
        instance.status = TenantInstance.Status.RUNNING
        instance.runtime_state = runtime_state
        instance.save(update_fields=["status", "runtime_state", "updated_at"])
        health = self._http_status(f"http://127.0.0.1:{instance.port}/health/") if instance.port else {"status": "unavailable"}
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=runtime_state.get("admin_credentials_path", ""),
            details={"health_status": health.get("status"), "suspended": False, "bot_worker_status": bot_worker_status},
        )

    def start_or_restart_bot_worker(self, *, tenant_id):
        instance = self._instance(tenant_id)
        tenant_state = self._tenant_db_state(instance)
        if not self._is_bot_configured(instance, tenant_state):
            raise OwnerToolkitError("Tenant bot is not configured.")
        bot_worker_status = self._run_bot_worker(instance, existing_action="restart")
        instance.runtime_state = self._with_bot_runtime_state(
            instance,
            last_action="bot_worker_started",
            bot_configured=True,
            bot_worker_status=bot_worker_status,
        )
        instance.save(update_fields=["runtime_state", "updated_at"])
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=(instance.runtime_state or {}).get("admin_credentials_path", ""),
            details={
                "bot_worker_container": self._bot_worker_container_name(instance),
                "bot_worker_status": bot_worker_status,
            },
        )

    def enable_https(self, *, tenant_id, email, server_ip=None):
        instance = self._instance(tenant_id)
        if not instance.subdomain or not instance.nginx_config_path:
            raise OwnerToolkitError("Tenant must have a subdomain and Nginx config before enabling HTTPS.")
        expected_ip = server_ip or instance.server_node.ip
        resolved = self._resolve_domain(instance.subdomain)
        if expected_ip and expected_ip not in resolved:
            raise OwnerToolkitError(f"DNS is not ready for {instance.subdomain}.")
        self._run(["nginx", "-t"], timeout=60)
        certbot = self.command_runner.run(
            [
                "certbot",
                "--nginx",
                "-d",
                instance.subdomain,
                "--non-interactive",
                "--agree-tos",
                "--redirect",
                "--email",
                email,
            ],
            timeout=240,
        )
        if certbot.returncode != 0:
            self.command_runner.run(["nginx", "-t"], timeout=60)
            self.command_runner.run(["systemctl", "reload", "nginx"], timeout=60)
            raise OwnerToolkitError((certbot.stderr or certbot.stdout or "certbot failed").strip())
        self._run(["nginx", "-t"], timeout=60)
        self._run(["systemctl", "reload", "nginx"], timeout=60)
        https = self._http_status(f"https://{instance.subdomain}/health/")
        return TenantCommandResult(
            tenant_id=instance.tenant_id,
            status=instance.status,
            url=instance.instance_url,
            container=instance.container_name,
            port=instance.port,
            credential_file=(instance.runtime_state or {}).get("admin_credentials_path", ""),
            details={"https_status": https.get("status"), "dns": ",".join(resolved)},
        )

    def _validate_new_tenant(self, tenant_id):
        try:
            normalized = self.deployment_service.subdomain_manager.validate_tenant_id(tenant_id)
        except SubdomainError as exc:
            raise OwnerToolkitError(str(exc)) from exc
        if TenantInstance.objects.filter(tenant_id=normalized).exclude(status=TenantInstance.Status.DELETED).exists():
            raise OwnerToolkitError("Tenant already exists.")
        return normalized

    def _wait_for_migrations(self, instance, attempts=60):
        for _attempt in range(attempts):
            result = self.command_runner.run(
                ["docker", "exec", instance.container_name, "python", "manage.py", "migrate", "--check"],
                timeout=60,
            )
            if result.returncode == 0:
                return
            self.sleep(2)
        raise OwnerToolkitError("Tenant migrations did not become ready.")

    def _bot_worker_container_name(self, instance):
        return f"{instance.container_name}_bot"

    def _tenant_env_file(self, instance):
        runtime_state = instance.runtime_state or {}
        return runtime_state.get("env_path") or str(self.deployment_service.instance_root / instance.tenant_id / ".env")

    def _with_bot_runtime_state(self, instance, *, last_action, bot_configured, bot_worker_status):
        runtime_state = dict(instance.runtime_state or {})
        runtime_state["last_action"] = last_action
        runtime_state["bot_configured"] = bot_configured
        runtime_state["bot_worker_container"] = self._bot_worker_container_name(instance) if bot_configured else ""
        runtime_state["bot_worker_status"] = bot_worker_status
        return runtime_state

    def _is_bot_configured(self, instance, tenant_state=None):
        tenant_state = tenant_state if tenant_state is not None else self._tenant_db_state(instance)
        configured = bool(tenant_state.get("bot_configured"))
        if tenant_state.get("db_connectivity") in {"unavailable", "unknown"}:
            configured = configured or bool((instance.runtime_state or {}).get("bot_configured"))
        return configured

    def _docker_container_id(self, container_name):
        result = self.command_runner.run(
            ["docker", "ps", "-a", "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}"],
            timeout=60,
        )
        if result.returncode != 0:
            raise OwnerToolkitError("Could not inspect tenant bot worker container.")
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def _run_bot_worker(self, instance, *, existing_action):
        bot_container = self._bot_worker_container_name(instance)
        existing = self._docker_container_id(bot_container)
        if existing:
            result = self.command_runner.run(["docker", existing_action, bot_container], timeout=120)
            if result.returncode != 0:
                raise OwnerToolkitError("Could not start tenant bot worker.")
            return self._docker_inspect(bot_container)

        result = self.command_runner.run(
            [
                "docker",
                "run",
                "-d",
                "--restart",
                "unless-stopped",
                "--name",
                bot_container,
                "--add-host",
                "host.docker.internal:host-gateway",
                "--env-file",
                self._tenant_env_file(instance),
                instance.docker_image_version or "qasedak-core:latest",
                "/app/docker/start-bot.sh",
            ],
            timeout=180,
        )
        if result.returncode != 0:
            raise OwnerToolkitError("Could not create tenant bot worker.")
        return self._docker_inspect(bot_container)

    def _stop_bot_worker(self, instance):
        bot_container = self._bot_worker_container_name(instance)
        result = self.command_runner.run(["docker", "stop", bot_container], timeout=120)
        if result.returncode != 0 and "No such container" not in (result.stderr or ""):
            raise OwnerToolkitError("Could not stop tenant bot worker.")
        return "unavailable" if result.returncode != 0 else "stopped"

    def _tenant_db_state(self, instance):
        script = """
import json
import os
from django.db import connection
from store.models import BotConfiguration, Inbound, Panel, Store
store = Store.objects.order_by('id').first()
active_telegram = BotConfiguration.objects.filter(provider=BotConfiguration.Provider.TELEGRAM, is_active=True)
runtime_token_configured = bool(os.environ.get('TELEGRAM_BOT_TOKEN', '').strip())
print(json.dumps({
    'db_connectivity': connection.vendor,
    'setup_status': getattr(store, 'setup_status', ''),
    'revenue_dry_run': getattr(store, 'revenue_engine_dry_run', None),
    'bot_configured': any(runtime_token_configured or bool(cfg.bot_token) for cfg in active_telegram),
    'xui_configured': Panel.objects.exists() or Inbound.objects.exists(),
}, sort_keys=True))
"""
        result = self.command_runner.run(
            ["docker", "exec", instance.container_name, "python", "manage.py", "shell", "-c", script],
            timeout=90,
        )
        if result.returncode != 0:
            return {"db_connectivity": "unavailable"}
        try:
            return json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {"db_connectivity": "unknown"}

    def _telegram_webhook_status(self, instance):
        script = """
import json
import os
from store.models import BotConfiguration
from store.telegram_bot.client import BotClient
configs = list(BotConfiguration.objects.filter(provider=BotConfiguration.Provider.TELEGRAM, is_active=True).order_by('pk'))
runtime_token_configured = bool(os.environ.get('TELEGRAM_BOT_TOKEN', '').strip())
config = next((cfg for cfg in configs if runtime_token_configured or cfg.bot_token), None)
status = 'not_configured'
if config:
    try:
        payload = BotClient(config).call('getWebhookInfo', {}, timeout=(3, 8))
        info = payload.get('result') or {}
        status = 'set' if info.get('url') else 'deleted'
    except Exception:
        status = 'unavailable'
print(json.dumps({'webhook_status': status}, sort_keys=True))
"""
        result = self.command_runner.run(
            ["docker", "exec", instance.container_name, "python", "manage.py", "shell", "-c", script],
            timeout=30,
        )
        if result.returncode != 0:
            return "unavailable"
        try:
            payload = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return "unknown"
        return payload.get("webhook_status") or "unknown"

    def _docker_inspect(self, container_name):
        result = self.command_runner.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_name],
            timeout=60,
        )
        if result.returncode != 0:
            return "unavailable"
        return (result.stdout or "").strip() or "unknown"

    def _http_status(self, url):
        result = self.command_runner.run(
            ["curl", "-k", "-fsS", "-o", "/dev/null", "-w", "%{http_code}", url],
            timeout=30,
        )
        if result.returncode != 0:
            return {"status": "unavailable"}
        return {"status": (result.stdout or "").strip()}

    def _resolve_domain(self, domain):
        resolved = set()
        for resolver in ("1.1.1.1", "8.8.8.8", "9.9.9.9"):
            result = self.command_runner.run(["dig", "+short", domain, f"@{resolver}"], timeout=20)
            if result.returncode == 0:
                resolved.update(line.strip() for line in (result.stdout or "").splitlines() if line.strip())
        if not resolved:
            try:
                resolved.update(socket.gethostbyname_ex(domain)[2])
            except OSError:
                pass
        return sorted(resolved)

    def _instance(self, tenant_id):
        try:
            return TenantInstance.objects.select_related("server_node").get(tenant_id=tenant_id)
        except TenantInstance.DoesNotExist as exc:
            raise OwnerToolkitError("Tenant was not found.") from exc

    def _run(self, argv, *, timeout=60):
        result = self.command_runner.run(argv, timeout=timeout)
        if result.returncode != 0:
            raise OwnerToolkitError((result.stderr or result.stdout or f"Command failed: {argv[0]}").strip())
        return result
