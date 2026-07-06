import logging

from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from store.orchestrator_v2.serializers import (
    OrchestratorValidationError,
    instance_to_dict,
    parse_json_request,
    public_deploy_result,
    redact_payload,
    server_to_dict,
    validate_instance_create,
    validate_instance_deploy,
    validate_lifecycle,
    validate_server_register,
    validate_status_query,
)
from store.orchestrator_v2.services.deploy_engine import DeploymentError
from store.orchestrator_v2.services.instance_manager import InstanceError, InstanceManager
from store.orchestrator_v2.services.server_registry import ServerCapacityError, ServerRegistry
from store.orchestrator_v2.models import TenantInstance


logger = logging.getLogger(__name__)


def staff_json_required(view_func):
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        return view_func(request, *args, **kwargs)

    return wrapper


@require_POST
@staff_json_required
def register_server(request):
    try:
        raw_payload = parse_json_request(request)
        payload = validate_server_register(raw_payload)
        server, connection_status = ServerRegistry().register(payload)
    except OrchestratorValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except Exception as exc:
        logger.exception("orchestrator_v2_server_register_failed payload=%s", redact_payload(locals().get("raw_payload", {})))
        return JsonResponse({"ok": False, "error": exc.__class__.__name__}, status=500)

    response = {"ok": True, "server": server_to_dict(server)}
    if connection_status is not None:
        response["connection"] = {
            "ok": connection_status.ok,
            "host": connection_status.host,
            "user": connection_status.user,
            "port": connection_status.port,
            "message": connection_status.message,
        }
    return JsonResponse(response)


@require_POST
@staff_json_required
def create_instance(request):
    try:
        raw_payload = parse_json_request(request)
        payload = validate_instance_create(raw_payload)
        instance = InstanceManager().create_instance(payload, actor=request.user)
    except OrchestratorValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except ServerCapacityError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    except Exception as exc:
        logger.exception("orchestrator_v2_instance_create_failed payload=%s", redact_payload(locals().get("raw_payload", {})))
        return JsonResponse({"ok": False, "error": exc.__class__.__name__}, status=500)
    return JsonResponse({"ok": True, "instance": instance_to_dict(instance)})


@require_POST
@staff_json_required
def deploy_instance(request):
    try:
        raw_payload = parse_json_request(request)
        payload = validate_instance_deploy(raw_payload)
        manager = InstanceManager()
        manager.create_instance(payload, actor=request.user)
        result = manager.deploy_instance(payload)
    except OrchestratorValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except ObjectDoesNotExist:
        return JsonResponse({"ok": False, "error": "Tenant instance not found."}, status=404)
    except (DeploymentError, InstanceError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    except Exception as exc:
        logger.exception("orchestrator_v2_instance_deploy_failed payload=%s", redact_payload(locals().get("raw_payload", {})))
        return JsonResponse({"ok": False, "error": exc.__class__.__name__}, status=500)
    tenant = TenantInstance.objects.select_related("server_node").get(tenant_id=payload["tenant_id"])
    return JsonResponse(
        {
            "ok": True,
            "deploy": public_deploy_result(result),
            "instance": instance_to_dict(tenant),
        }
    )


@require_POST
@staff_json_required
def stop_instance(request):
    return _lifecycle_response(request, "stop")


@require_POST
@staff_json_required
def restart_instance(request):
    return _lifecycle_response(request, "restart")


@require_POST
@staff_json_required
def delete_instance(request):
    return _lifecycle_response(request, "delete")


@require_GET
@staff_json_required
def instance_status(request):
    try:
        payload = validate_status_query(request)
        status = InstanceManager().status_instance(**payload)
    except OrchestratorValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except ObjectDoesNotExist:
        return JsonResponse({"ok": False, "error": "Tenant instance not found."}, status=404)
    except Exception as exc:
        logger.warning("orchestrator_v2_instance_status_failed tenant=%s error=%s", request.GET.get("tenant_id", ""), exc.__class__.__name__)
        return JsonResponse({"ok": False, "error": exc.__class__.__name__}, status=502)
    return JsonResponse(
        {
            "ok": True,
            "runtime_state": status["runtime_state"],
            "instance": instance_to_dict(status["instance"]),
        }
    )


def _lifecycle_response(request, action):
    try:
        raw_payload = parse_json_request(request)
        payload = validate_lifecycle(raw_payload)
        manager = InstanceManager()
        if action == "stop":
            instance = manager.stop_instance(**payload)
        elif action == "restart":
            instance = manager.restart_instance(**payload)
        elif action == "delete":
            instance = manager.delete_instance(**payload)
        else:
            return JsonResponse({"ok": False, "error": "Unsupported action."}, status=400)
    except OrchestratorValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except ObjectDoesNotExist:
        return JsonResponse({"ok": False, "error": "Tenant instance not found."}, status=404)
    except InstanceError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    except Exception as exc:
        logger.exception("orchestrator_v2_lifecycle_failed action=%s payload=%s", action, redact_payload(locals().get("raw_payload", {})))
        return JsonResponse({"ok": False, "error": exc.__class__.__name__}, status=500)
    return JsonResponse({"ok": True, "instance": instance_to_dict(instance)})
