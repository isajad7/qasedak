from store.orchestrator_v2.services.instance_manager import InstanceManager


def deploy_tenant_instance(payload):
    return InstanceManager().deploy_instance(payload)


def refresh_tenant_status(tenant_id):
    return InstanceManager().status_instance(tenant_id)
