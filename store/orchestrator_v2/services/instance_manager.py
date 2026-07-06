import logging
import shlex

from django.db import IntegrityError, transaction
from django.utils import timezone

from store.deployment.port_allocator import PortAllocator
from store.deployment.subdomain_manager import SubdomainManager
from store.orchestrator_v2.models import ServerNode, TenantInstance
from store.orchestrator_v2.services.deploy_engine import DeployEngine, DeploymentError
from store.orchestrator_v2.services.server_registry import ServerRegistry
from store.orchestrator_v2.services.ssh_client import SSHClient, SSHCredentials


logger = logging.getLogger(__name__)


class InstanceError(Exception):
    pass


class InstanceManager:
    BASE_PORT = 8001

    def __init__(self, deploy_engine=None, server_registry=None, ssh_client_factory=None, port_allocator=None, subdomain_manager=None):
        self.deploy_engine = deploy_engine or DeployEngine(ssh_client_factory=ssh_client_factory)
        self.server_registry = server_registry or ServerRegistry(ssh_client_factory=ssh_client_factory)
        self.ssh_client_factory = ssh_client_factory or SSHClient
        self.port_allocator = port_allocator or PortAllocator()
        self.subdomain_manager = subdomain_manager or SubdomainManager()

    def create_instance(self, payload, *, actor=None):
        tenant_id = payload["tenant_id"]
        existing = TenantInstance.objects.filter(tenant_id=tenant_id).select_related("server_node").first()
        if existing and existing.status != TenantInstance.Status.DELETED:
            changed_fields = []
            if payload.get("customer_domain") and existing.customer_domain != payload["customer_domain"]:
                existing.customer_domain = payload["customer_domain"]
                existing.customer_domain_dns_status = TenantInstance.DNSStatus.PENDING
                changed_fields.extend(["customer_domain", "customer_domain_dns_status"])
            if changed_fields:
                existing.save(update_fields=[*changed_fields, "updated_at"])
            if payload.get("base_domain") and not existing.subdomain:
                self.subdomain_manager.assign(existing, payload["base_domain"])
            logger.info("orchestrator_v2_instance_create_idempotent tenant=%s server=%s", tenant_id, existing.server_node_id)
            return existing

        server = self.server_registry.available_server(payload.get("server_node_id"))
        port = self.allocate_port(server, payload.get("port"))
        container_name = self.container_name(tenant_id)
        defaults = {
            "server_node": server,
            "container_name": container_name,
            "port": port,
            "domain": payload.get("domain", ""),
            "customer_domain": payload.get("customer_domain", ""),
            "customer_domain_dns_status": TenantInstance.DNSStatus.PENDING
            if payload.get("customer_domain")
            else TenantInstance.DNSStatus.NOT_CONFIGURED,
            "docker_image_version": payload.get("docker_image_version") or "qasedak-core:latest",
            "created_by": actor if getattr(actor, "is_authenticated", False) else None,
        }
        try:
            with transaction.atomic():
                instance, created = TenantInstance.objects.get_or_create(tenant_id=tenant_id, defaults=defaults)
                if not created and instance.status == TenantInstance.Status.DELETED:
                    for field, value in defaults.items():
                        setattr(instance, field, value)
                    instance.status = TenantInstance.Status.PENDING
                    instance.error_message = ""
                    instance.save()
                if payload.get("base_domain"):
                    self.subdomain_manager.assign(instance, payload["base_domain"])
                server.refresh_current_instances(save=True)
        except IntegrityError as exc:
            raise InstanceError("Could not allocate tenant instance safely.") from exc
        logger.info("orchestrator_v2_instance_created tenant=%s server=%s", tenant_id, instance.server_node_id)
        return instance

    def deploy_instance(self, payload):
        instance = TenantInstance.objects.select_related("server_node").get(tenant_id=payload["tenant_id"])
        if instance.status == TenantInstance.Status.DELETED:
            raise InstanceError("Cannot deploy a deleted tenant instance.")
        if payload.get("domain"):
            instance.domain = payload["domain"]
        elif payload.get("base_domain") and not instance.subdomain:
            self.subdomain_manager.assign(instance, payload["base_domain"])
            instance.refresh_from_db()
        if payload.get("port"):
            instance.port = int(payload["port"])
        if payload.get("customer_domain"):
            instance.customer_domain = payload["customer_domain"]
            instance.customer_domain_dns_status = TenantInstance.DNSStatus.PENDING
        if payload.get("docker_image_version"):
            instance.docker_image_version = payload["docker_image_version"]
        instance.save(
            update_fields=[
                "domain",
                "port",
                "customer_domain",
                "customer_domain_dns_status",
                "docker_image_version",
                "updated_at",
            ]
        )

        credentials = self.deploy_engine.credentials_for_server(
            instance.server_node,
            password=payload.get("ssh_password", ""),
            key_path=payload.get("ssh_key_path", ""),
        )
        return self.deploy_engine.deploy(instance, payload["runtime_env"], credentials=credentials)

    def stop_instance(self, tenant_id, *, ssh_password="", ssh_key_path=""):
        instance = self._instance(tenant_id)
        client = self._client(instance, ssh_password=ssh_password, ssh_key_path=ssh_key_path)
        try:
            result = client.run(f"docker stop {shlex.quote(instance.container_name)}", timeout=120)
            if not result.ok and "No such container" not in result.stderr:
                raise InstanceError(result.stderr or "Could not stop container.")
            instance.status = TenantInstance.Status.STOPPED
            instance.runtime_state = {**(instance.runtime_state or {}), "last_action": "stopped"}
            instance.save(update_fields=["status", "runtime_state", "updated_at"])
            logger.info("orchestrator_v2_instance_stopped tenant=%s", tenant_id)
            return instance
        finally:
            self._close(client)

    def restart_instance(self, tenant_id, *, ssh_password="", ssh_key_path=""):
        instance = self._instance(tenant_id)
        client = self._client(instance, ssh_password=ssh_password, ssh_key_path=ssh_key_path)
        try:
            result = client.run(f"docker restart {shlex.quote(instance.container_name)}", timeout=120)
            if not result.ok:
                raise InstanceError(result.stderr or "Could not restart container.")
            instance.status = TenantInstance.Status.RUNNING
            instance.runtime_state = {**(instance.runtime_state or {}), "last_action": "restarted"}
            instance.save(update_fields=["status", "runtime_state", "updated_at"])
            logger.info("orchestrator_v2_instance_restarted tenant=%s", tenant_id)
            return instance
        finally:
            self._close(client)

    def delete_instance(self, tenant_id, *, ssh_password="", ssh_key_path=""):
        instance = self._instance(tenant_id)
        client = self._client(instance, ssh_password=ssh_password, ssh_key_path=ssh_key_path)
        try:
            instance.status = TenantInstance.Status.DELETING
            instance.save(update_fields=["status", "updated_at"])
            result = client.run(f"docker rm -f {shlex.quote(instance.container_name)}", timeout=180)
            if not result.ok and "No such container" not in result.stderr:
                raise InstanceError(result.stderr or "Could not delete container.")
            instance.status = TenantInstance.Status.DELETED
            instance.runtime_state = {**(instance.runtime_state or {}), "last_action": "deleted"}
            instance.save(update_fields=["status", "runtime_state", "updated_at"])
            instance.server_node.refresh_current_instances()
            logger.info("orchestrator_v2_instance_deleted tenant=%s", tenant_id)
            return instance
        finally:
            self._close(client)

    def status_instance(self, tenant_id, *, ssh_password="", ssh_key_path=""):
        instance = self._instance(tenant_id)
        client = self._client(instance, ssh_password=ssh_password, ssh_key_path=ssh_key_path)
        try:
            template = "{{.State.Status}}"
            result = client.run(
                f"docker inspect -f {shlex.quote(template)} {shlex.quote(instance.container_name)}",
                timeout=60,
            )
            if not result.ok:
                instance.status = TenantInstance.Status.DEGRADED
                instance.error_message = "remote_status_unavailable"
                instance.save(update_fields=["status", "error_message", "updated_at"])
                return {"runtime_state": "unavailable", "instance": instance}
            docker_status = result.stdout.strip()
            if docker_status == "running":
                health = self.deploy_engine.check_health(client, instance)
                instance.status = TenantInstance.Status.RUNNING if health.get("status") == "ok" else TenantInstance.Status.DEGRADED
                instance.last_health = health
                instance.error_message = ""
                instance.save(update_fields=["status", "last_health", "error_message", "updated_at"])
            elif docker_status in {"exited", "created"}:
                instance.status = TenantInstance.Status.STOPPED
                instance.save(update_fields=["status", "updated_at"])
            else:
                instance.status = TenantInstance.Status.DEGRADED
                instance.save(update_fields=["status", "updated_at"])
            return {"runtime_state": docker_status, "instance": instance}
        except Exception:
            instance.status = TenantInstance.Status.DEGRADED
            instance.error_message = "server_unreachable"
            instance.save(update_fields=["status", "error_message", "updated_at"])
            raise
        finally:
            self._close(client)

    def allocate_port(self, server, requested_port=None):
        return self.port_allocator.next_free_port(server, preferred_port=requested_port)

    def container_name(self, tenant_id):
        return f"qasedak_{tenant_id}"

    def _instance(self, tenant_id):
        return TenantInstance.objects.select_related("server_node").get(tenant_id=tenant_id)

    def _client(self, instance, *, ssh_password="", ssh_key_path=""):
        credentials = SSHCredentials(
            host=instance.server_node.ip,
            user=instance.server_node.ssh_user,
            port=instance.server_node.ssh_port,
            key_path=ssh_key_path or instance.server_node.ssh_key_path,
            password=ssh_password,
        )
        return self.ssh_client_factory(credentials)

    def _close(self, client):
        close = getattr(client, "close", None)
        if callable(close):
            close()
