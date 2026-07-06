import ipaddress
import json
import re
import secrets

from django.core.exceptions import ObjectDoesNotExist

from store.orchestrator_v2.models import ServerNode, TenantInstance


TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
DOMAIN_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
BASE_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
DEFAULT_BASE_DOMAIN = "panelwpvideo.ir"
RESERVED_TENANT_IDS = {"www", "admin", "api", "control", "mail", "root", "qasedak", "bots"}
SECRET_KEYS = {
    "ssh_password",
    "ssh_key",
    "telegram_bot_token",
    "TELEGRAM_BOT_TOKEN",
    "database_url",
    "DATABASE_URL",
    "django_secret_key",
    "DJANGO_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "postgres_password",
}


class OrchestratorValidationError(Exception):
    def __init__(self, errors):
        super().__init__("Invalid orchestrator payload.")
        self.errors = errors


def parse_json_request(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError) as exc:
        raise OrchestratorValidationError({"body": "Invalid JSON body."}) from exc
    if not isinstance(payload, dict):
        raise OrchestratorValidationError({"body": "Expected a JSON object."})
    return payload


def validate_server_register(payload):
    errors = {}
    data = {}
    data["name"] = _required_str(payload, "name", errors, max_length=120)
    ip = _required_str(payload, "ip", errors, max_length=64)
    if ip:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            errors["ip"] = "Enter a valid IPv4 or IPv6 address."
        data["ip"] = ip
    data["ssh_user"] = _optional_str(payload, "ssh_user", "root", max_length=80)
    data["ssh_port"] = _port(payload.get("ssh_port", 22), "ssh_port", errors, minimum=1)
    data["ssh_key_path"] = _optional_str(payload, "ssh_key_path", "", max_length=500)
    data["ssh_key_vault_ref"] = _optional_str(payload, "ssh_key_vault_ref", "", max_length=255)
    data["ssh_password"] = _optional_str(payload, "ssh_password", "", max_length=500)
    data["verify"] = bool(payload.get("verify", False))
    data["max_instances"] = _positive_int(payload.get("max_instances", 10), "max_instances", errors, minimum=1)
    credential_mode = payload.get("credential_mode") or ServerNode.CredentialMode.SSH_KEY
    if credential_mode not in ServerNode.CredentialMode.values:
        errors["credential_mode"] = "Invalid credential mode."
    data["credential_mode"] = credential_mode
    status = payload.get("status") or ServerNode.Status.ACTIVE
    if status not in ServerNode.Status.values:
        errors["status"] = "Invalid server status."
    data["status"] = status
    if errors:
        raise OrchestratorValidationError(errors)
    return data


def validate_instance_create(payload):
    errors = {}
    data = {
        "tenant_id": _tenant_id(payload.get("tenant_id"), errors),
        "server_node_id": payload.get("server_node_id"),
        "domain": _domain(payload.get("domain", ""), errors),
        "base_domain": _base_domain(payload.get("base_domain") or DEFAULT_BASE_DOMAIN, errors),
        "tenant_display_name": _optional_str(payload, "tenant_display_name", "", max_length=120),
        "customer_domain": _domain(payload.get("customer_domain", ""), errors),
        "docker_image_version": _image(payload.get("docker_image_version", "qasedak-core:latest"), errors),
    }
    if payload.get("port") not in (None, ""):
        data["port"] = _port(payload.get("port"), "port", errors)
    if data["server_node_id"] not in (None, ""):
        data["server_node_id"] = _positive_int(data["server_node_id"], "server_node_id", errors, minimum=1)
    else:
        data["server_node_id"] = None
    if errors:
        raise OrchestratorValidationError(errors)
    return data


def validate_instance_deploy(payload):
    errors = {}
    data = validate_instance_create(payload)
    data["ssh_password"] = _optional_str(payload, "ssh_password", "", max_length=500)
    data["ssh_key_path"] = _optional_str(payload, "ssh_key_path", "", max_length=500)
    admin_payload = payload.get("admin") or {}
    if not isinstance(admin_payload, dict):
        errors["admin"] = "Expected an object."
        admin_payload = {}
    admin_username = (
        _optional_str(admin_payload, "username", "", max_length=150)
        or _optional_str(payload, "admin_username", "", max_length=150)
        or "admin"
    )
    admin_password = (
        _optional_str(admin_payload, "password", "", max_length=500)
        or _optional_str(payload, "admin_password", "", max_length=500)
        or secrets.token_urlsafe(18)
    )
    runtime_env = {
        "REVENUE_ENGINE_DRY_RUN": str(payload.get("revenue_engine_dry_run", True)).lower(),
        "QASEDAK_BOT_ENABLED": "false",
        "QASEDAK_WORKER_ENABLED": "false",
        "QASEDAK_BOOTSTRAP_TENANT": "true",
        "QASEDAK_ADMIN_USERNAME": admin_username,
        "QASEDAK_ADMIN_PASSWORD": admin_password,
        "TENANT_DISPLAY_NAME": data["tenant_display_name"] or data["tenant_id"],
    }
    telegram_token = _optional_str(payload, "telegram_bot_token", "", max_length=500)
    if telegram_token:
        runtime_env["TELEGRAM_BOT_TOKEN"] = telegram_token
    django_secret_key = _optional_str(payload, "django_secret_key", "", max_length=255)
    if django_secret_key:
        runtime_env["DJANGO_SECRET_KEY"] = django_secret_key
    database_url = _optional_str(payload, "database_url", "", max_length=1000)
    if database_url:
        runtime_env["DATABASE_URL"] = database_url
    else:
        database = payload.get("database") or {}
        if not isinstance(database, dict):
            errors["database"] = "Expected an object."
            database = {}
        postgres_password = database.get("postgres_password") or payload.get("postgres_password")
        postgres_host = database.get("postgres_host") or payload.get("postgres_host")
        if not postgres_password or not postgres_host:
            errors["database_url"] = "Provide database_url or PostgreSQL host/password config."
        runtime_env.update(
            {
                "DATABASE_ENGINE": "postgres",
                "POSTGRES_DB": database.get("postgres_db") or payload.get("postgres_db") or "qasedak",
                "POSTGRES_USER": database.get("postgres_user") or payload.get("postgres_user") or "qasedak",
                "POSTGRES_PASSWORD": postgres_password or "",
                "POSTGRES_HOST": postgres_host or "",
                "POSTGRES_PORT": str(database.get("postgres_port") or payload.get("postgres_port") or 5432),
                "POSTGRES_SSLMODE": database.get("postgres_sslmode") or payload.get("postgres_sslmode") or "prefer",
            }
        )
    data["runtime_env"] = runtime_env
    if errors:
        raise OrchestratorValidationError(errors)
    return data


def validate_lifecycle(payload):
    errors = {}
    data = {
        "tenant_id": _tenant_id(payload.get("tenant_id"), errors),
        "ssh_password": _optional_str(payload, "ssh_password", "", max_length=500),
        "ssh_key_path": _optional_str(payload, "ssh_key_path", "", max_length=500),
    }
    if errors:
        raise OrchestratorValidationError(errors)
    return data


def validate_status_query(request):
    errors = {}
    data = {
        "tenant_id": _tenant_id(request.GET.get("tenant_id"), errors),
        "ssh_password": "",
        "ssh_key_path": _optional_query(request, "ssh_key_path", "", max_length=500),
    }
    if errors:
        raise OrchestratorValidationError(errors)
    return data


def server_to_dict(server):
    return {
        "id": server.pk,
        "name": server.name,
        "ip": server.ip,
        "ssh_user": server.ssh_user,
        "ssh_port": server.ssh_port,
        "status": server.status,
        "max_instances": server.max_instances,
        "current_instances": server.current_instances,
        "created_at": server.created_at.isoformat() if server.created_at else None,
    }


def instance_to_dict(instance):
    return {
        "id": instance.pk,
        "tenant_id": instance.tenant_id,
        "server_node": server_to_dict(instance.server_node),
        "container_name": instance.container_name,
        "port": instance.port,
        "domain": instance.domain,
        "subdomain": instance.subdomain,
        "customer_domain": instance.customer_domain,
        "customer_domain_dns_status": instance.customer_domain_dns_status,
        "status": instance.status,
        "setup_status": (instance.runtime_state or {}).get("setup_status", "setup_required"),
        "deployment_status": instance.deployment_status,
        "instance_url": instance.instance_url,
        "admin_url": _admin_url(instance.instance_url),
        "admin_credentials_path": (instance.runtime_state or {}).get("admin_credentials_path", ""),
        "setup_checklist_pending": (instance.runtime_state or {}).get("setup_checklist_pending", []),
        "nginx_config_path": instance.nginx_config_path,
        "container_id": instance.container_id,
        "last_deployed_at": instance.last_deployed_at.isoformat() if instance.last_deployed_at else None,
        "docker_image_version": instance.docker_image_version,
        "last_health": instance.last_health,
        "runtime_state": instance.runtime_state,
    }


def public_deploy_result(result):
    return {
        "container_id": result.container_id,
        "server_ip": result.server_ip,
        "instance_url": result.instance_url,
        "admin_url": _admin_url(result.instance_url),
        "action": result.action,
        "port": result.port,
        "health": result.health,
        "runtime_state": result.runtime_state,
        "setup_status": result.runtime_state.get("setup_status", "setup_required"),
        "admin_credentials_path": result.runtime_state.get("admin_credentials_path", ""),
        "setup_checklist_pending": result.runtime_state.get("setup_checklist_pending", []),
    }


def redact_payload(payload):
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if key in SECRET_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_payload(value)
        return redacted
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


def _required_str(payload, key, errors, *, max_length):
    value = str(payload.get(key, "")).strip()
    if not value:
        errors[key] = "This field is required."
        return ""
    if len(value) > max_length:
        errors[key] = f"Must be at most {max_length} characters."
        return ""
    if "\x00" in value or "\n" in value or "\r" in value:
        errors[key] = "Invalid control character."
        return ""
    return value


def _optional_str(payload, key, default, *, max_length):
    value = str(payload.get(key, default) or "").strip()
    if len(value) > max_length or "\x00" in value or "\n" in value or "\r" in value:
        return ""
    return value


def _optional_query(request, key, default, *, max_length):
    value = str(request.GET.get(key, default) or "").strip()
    if len(value) > max_length or "\x00" in value or "\n" in value or "\r" in value:
        return ""
    return value


def _tenant_id(value, errors):
    text = str(value or "").strip().lower()
    if not TENANT_ID_RE.match(text):
        errors["tenant_id"] = "Use 3-63 lowercase letters, numbers, and hyphens."
        return ""
    if text in RESERVED_TENANT_IDS:
        errors["tenant_id"] = "This tenant ID is reserved."
        return ""
    return text


def _base_domain(value, errors):
    text = str(value or "").strip().lower().rstrip(".")
    text = text.removeprefix("https://").removeprefix("http://").strip("/")
    if not text:
        return ""
    if not BASE_DOMAIN_RE.match(text):
        errors["base_domain"] = "Enter a DNS-safe base domain."
        return ""
    return text


def _domain(value, errors):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 255 or not DOMAIN_RE.match(text):
        errors["domain"] = "Enter a safe domain or URL."
        return ""
    return text


def _admin_url(instance_url):
    base = str(instance_url or "").rstrip("/")
    return f"{base}/admin/" if base else ""


def _image(value, errors):
    text = str(value or "").strip()
    if not IMAGE_RE.match(text):
        errors["docker_image_version"] = "Enter a safe Docker image reference."
        return ""
    return text


def _port(value, key, errors, *, minimum=1024):
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors[key] = "Enter a valid port."
        return 0
    if number < minimum or number > 65535:
        errors[key] = f"Port must be between {minimum} and 65535."
        return 0
    return number


def _positive_int(value, key, errors, *, minimum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors[key] = "Enter a valid integer."
        return 0
    if number < minimum:
        errors[key] = f"Value must be at least {minimum}."
        return 0
    return number
