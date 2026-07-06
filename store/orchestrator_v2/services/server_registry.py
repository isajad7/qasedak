import logging

from django.db import transaction

from store.orchestrator_v2.models import ServerNode, TenantInstance
from store.orchestrator_v2.services.deploy_engine import DeployEngine
from store.orchestrator_v2.services.ssh_client import SSHClient


logger = logging.getLogger(__name__)


class ServerCapacityError(Exception):
    pass


class ServerRegistry:
    def __init__(self, ssh_client_factory=None):
        self.ssh_client_factory = ssh_client_factory or SSHClient

    def register(self, payload):
        password = payload.pop("ssh_password", "")
        verify = bool(payload.pop("verify", False))
        with transaction.atomic():
            server, _created = ServerNode.objects.update_or_create(
                ip=payload["ip"],
                defaults={
                    "name": payload["name"],
                    "ssh_user": payload.get("ssh_user") or "root",
                    "ssh_port": payload.get("ssh_port") or 22,
                    "ssh_key_path": payload.get("ssh_key_path", ""),
                    "ssh_key_vault_ref": payload.get("ssh_key_vault_ref", ""),
                    "credential_mode": payload.get("credential_mode") or ServerNode.CredentialMode.SSH_KEY,
                    "max_instances": payload.get("max_instances") or 10,
                    "status": payload.get("status") or ServerNode.Status.ACTIVE,
                },
            )
            server.refresh_current_instances(save=True)

        connection_status = None
        if verify:
            credentials = DeployEngine().credentials_for_server(
                server,
                password=password,
                key_path=payload.get("ssh_key_path", ""),
            )
            connection_status = self.ssh_client_factory(credentials).verify_connection()
            server.mark_checked(
                status=ServerNode.Status.ACTIVE if connection_status.ok else ServerNode.Status.UNAVAILABLE,
                metadata={"connection": connection_status.message},
            )
        logger.info("orchestrator_v2_server_registered server=%s verify=%s", server.pk, verify)
        return server, connection_status

    def available_server(self, preferred_server_id=None):
        queryset = ServerNode.objects.filter(status=ServerNode.Status.ACTIVE).order_by("current_instances", "id")
        if preferred_server_id:
            queryset = queryset.filter(pk=preferred_server_id)
        for server in queryset:
            server.refresh_current_instances(save=True)
            if server.has_capacity:
                return server
        raise ServerCapacityError("No active server node has available capacity.")

    def refresh_capacity(self, server):
        active_statuses = [
            TenantInstance.Status.PENDING,
            TenantInstance.Status.DEPLOYING,
            TenantInstance.Status.RUNNING,
            TenantInstance.Status.STOPPED,
            TenantInstance.Status.DEGRADED,
            TenantInstance.Status.FAILED,
        ]
        server.current_instances = server.tenant_instances.filter(status__in=active_statuses).count()
        server.save(update_fields=["current_instances", "updated_at"])
        return server.current_instances
