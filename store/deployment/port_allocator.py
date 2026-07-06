from django.db import IntegrityError, transaction

from store.orchestrator_v2.models import TenantInstance


class PortAllocationError(Exception):
    pass


class PortAllocator:
    start_port = 8001
    end_port = 65535

    def used_ports(self, server_node, *, exclude_instance=None):
        queryset = (
            TenantInstance.objects.filter(server_node=server_node, port__isnull=False)
            .exclude(status=TenantInstance.Status.DELETED)
            .exclude(deployment_status=TenantInstance.DeploymentStatus.ROLLED_BACK)
        )
        if exclude_instance and exclude_instance.pk:
            queryset = queryset.exclude(pk=exclude_instance.pk)
        return set(queryset.values_list("port", flat=True))

    def next_free_port(self, server_node, *, preferred_port=None, exclude_instance=None):
        used = self.used_ports(server_node, exclude_instance=exclude_instance)
        port = int(preferred_port or self.start_port)
        if port < self.start_port:
            raise PortAllocationError(f"Port must be {self.start_port} or greater.")
        while port in used:
            port += 1
        if port > self.end_port:
            raise PortAllocationError("No free port available for this server.")
        return port

    def allocate(self, instance, *, preferred_port=None):
        with transaction.atomic():
            locked = TenantInstance.objects.select_for_update().select_related("server_node").get(pk=instance.pk)
            port = self.next_free_port(
                locked.server_node,
                preferred_port=preferred_port or locked.port,
                exclude_instance=locked,
            )
            locked.port = port
            try:
                locked.save(update_fields=["port", "updated_at"])
            except IntegrityError as exc:
                raise PortAllocationError("Port collision detected.") from exc
        instance.port = locked.port
        return locked.port

    def release(self, instance):
        with transaction.atomic():
            locked = TenantInstance.objects.select_for_update().get(pk=instance.pk)
            locked.port = None
            locked.deployment_status = TenantInstance.DeploymentStatus.ROLLED_BACK
            locked.save(update_fields=["port", "deployment_status", "updated_at"])
        instance.port = None
        instance.deployment_status = TenantInstance.DeploymentStatus.ROLLED_BACK
