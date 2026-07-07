import json
import os
import pathlib
import re
import secrets
import subprocess
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from store.deployment.nginx_generator import LocalCommandRunner, NginxConfigGenerator
from store.deployment.port_allocator import PortAllocator
from store.deployment.subdomain_manager import SubdomainManager
from store.orchestrator_v2.models import ServerNode, TenantInstance
from store.productization.bootstrap import SAFE_PLACEHOLDER_CARD_NUMBER, SAFE_PLACEHOLDER_CARD_OWNER


class SubdomainDeploymentError(Exception):
    pass


PG_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]+$")
DEFAULT_NO_PROXY_HOSTS = (
    "127.0.0.1",
    "localhost",
    "::1",
    "host.docker.internal",
)


def _quote_pg_identifier(value):
    if not PG_IDENTIFIER_RE.match(value or ""):
        raise SubdomainDeploymentError("Unsafe PostgreSQL identifier.")
    return '"' + value.replace('"', '""') + '"'


def _quote_pg_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _append_unique(items, value):
    text = str(value or "").strip()
    if not text:
        return
    key = text.lower()
    if key not in {item.lower() for item in items}:
        items.append(text)


def _split_no_proxy(value):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _host_from_url_or_host(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname or text.split("/", 1)[0].split(":", 1)[0].strip("[]")
    return host.strip()


def _merge_no_proxy_hosts(instance, values):
    hosts = []
    for source in (
        os.environ.get("NO_PROXY", ""),
        os.environ.get("no_proxy", ""),
        values.get("NO_PROXY", ""),
    ):
        for host in _split_no_proxy(source):
            _append_unique(hosts, host)
    for host in DEFAULT_NO_PROXY_HOSTS:
        _append_unique(hosts, host)

    for key in ("DOMAIN", "DJANGO_ALLOWED_HOSTS", "POSTGRES_HOST", "DATABASE_HOST"):
        for value in str(values.get(key, "") or "").split(","):
            _append_unique(hosts, _host_from_url_or_host(value))
    _append_unique(hosts, getattr(instance, "subdomain", ""))

    for key, value in values.items():
        upper_key = str(key).upper()
        if "PROXY" in upper_key:
            continue
        if "PANEL" not in upper_key and "XUI" not in upper_key:
            continue
        if not any(marker in upper_key for marker in ("URL", "HOST")):
            continue
        _append_unique(hosts, _host_from_url_or_host(value))
    return ",".join(hosts)


class TenantDatabaseProvisioner:
    default_host = "host.docker.internal"
    default_port = "5432"
    default_sslmode = "prefer"

    def __init__(self, *, command_runner=None, subdomain_manager=None, host=None, port=None, sslmode=None):
        self.command_runner = command_runner or LocalCommandRunner()
        self.subdomain_manager = subdomain_manager or SubdomainManager()
        self.host = host or os.environ.get("QASEDAK_TENANT_POSTGRES_HOST") or self.default_host
        self.port = str(port or os.environ.get("QASEDAK_TENANT_POSTGRES_PORT") or self.default_port)
        self.sslmode = sslmode or os.environ.get("QASEDAK_TENANT_POSTGRES_SSLMODE") or self.default_sslmode

    def names(self, tenant_id):
        tenant = self.subdomain_manager.validate_tenant_id(tenant_id)
        slug = tenant.replace("-", "_")
        database = f"qasedak_{slug}"
        user = f"{database}_app"
        if len(database) > 63 or len(user) > 63:
            raise SubdomainDeploymentError("Tenant PostgreSQL identifiers are too long.")
        if not PG_IDENTIFIER_RE.match(database) or not PG_IDENTIFIER_RE.match(user):
            raise SubdomainDeploymentError("Tenant PostgreSQL identifiers are invalid.")
        return {"database": database, "user": user}

    def preview(self, tenant_id):
        return {
            **self.names(tenant_id),
            "host": self.host,
            "port": self.port,
            "sslmode": self.sslmode,
        }

    def create(self, tenant_id, *, password=None):
        info = self.preview(tenant_id)
        if self._exists("database", info["database"]):
            raise SubdomainDeploymentError("Tenant database already exists.")
        if self._exists("role", info["user"]):
            raise SubdomainDeploymentError("Tenant database user already exists.")

        password = password or secrets.token_urlsafe(32)
        sql = "\n".join(
            [
                f"CREATE ROLE {_quote_pg_identifier(info['user'])} LOGIN PASSWORD {_quote_pg_literal(password)};",
                f"CREATE DATABASE {_quote_pg_identifier(info['database'])} OWNER {_quote_pg_identifier(info['user'])};",
                "",
            ]
        )
        result = self._psql(sql, timeout=120)
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not create tenant PostgreSQL database.")
        return {**info, "password": password, "created": True}

    def rollback(self, info):
        database = (info or {}).get("database")
        user = (info or {}).get("user")
        if not database or not user:
            return
        sql = "\n".join(
            [
                f"REVOKE CONNECT ON DATABASE {_quote_pg_identifier(database)} FROM PUBLIC;",
                (
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = {_quote_pg_literal(database)} AND pid <> pg_backend_pid();"
                ),
                f"DROP DATABASE IF EXISTS {_quote_pg_identifier(database)};",
                f"DROP ROLE IF EXISTS {_quote_pg_identifier(user)};",
                "",
            ]
        )
        result = self._psql(sql, timeout=120)
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not roll back tenant PostgreSQL database.")

    def _exists(self, kind, name):
        if kind == "database":
            sql = f"SELECT 1 FROM pg_database WHERE datname = {_quote_pg_literal(name)};"
        elif kind == "role":
            sql = f"SELECT 1 FROM pg_roles WHERE rolname = {_quote_pg_literal(name)};"
        else:
            raise SubdomainDeploymentError("Invalid PostgreSQL existence check.")
        result = self._psql(sql, timeout=60, tuples_only=True)
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not inspect PostgreSQL tenant identifiers.")
        return bool(result.stdout.strip())

    def _psql(self, sql, *, timeout=60, tuples_only=False):
        argv = ["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-q"]
        if tuples_only:
            argv.append("-At")
        return self.command_runner.run(argv, timeout=timeout, input=sql)


class DockerRunner:
    def __init__(self, command_runner=None):
        self.command_runner = command_runner or LocalCommandRunner()

    def existing_container_id(self, container_name):
        result = self.command_runner.run(
            ["docker", "ps", "-a", "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}"],
            timeout=60,
        )
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not inspect Docker containers.")
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def run_container(self, *, container_name, port, env_file, image):
        existing = self.existing_container_id(container_name)
        if existing:
            result = self.command_runner.run(["docker", "restart", container_name], timeout=120)
            if result.returncode != 0:
                raise SubdomainDeploymentError("Could not restart existing container.")
            return existing

        result = self.command_runner.run(
            [
                "docker",
                "run",
                "-d",
                "--restart",
                "unless-stopped",
                "--name",
                container_name,
                "--add-host",
                "host.docker.internal:host-gateway",
                "-p",
                f"127.0.0.1:{int(port)}:8000",
                "--env-file",
                str(env_file),
                image,
            ],
            timeout=180,
        )
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not create Docker container.")
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def bootstrap_tenant(self, *, container_name, config):
        payload = json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        script = (
            "set -e; "
            "tmp=$(mktemp); "
            "cat > \"$tmp\"; "
            "python manage.py bootstrap_install --config \"$tmp\" --yes; "
            "rm -f \"$tmp\""
        )
        result = self.command_runner.run(
            ["docker", "exec", "-i", container_name, "sh", "-c", script],
            timeout=180,
            input=payload,
        )
        if result.returncode != 0:
            raise SubdomainDeploymentError("Could not bootstrap tenant database objects.")

    def remove_container(self, container_name):
        result = self.command_runner.run(["docker", "rm", "-f", container_name], timeout=180)
        if result.returncode != 0 and "No such container" not in (result.stderr or ""):
            raise SubdomainDeploymentError("Could not roll back Docker container.")


class SubdomainDeploymentService:
    default_image = "qasedak-core:latest"
    default_instance_root = pathlib.Path("/opt/qasedak-tenants")

    def __init__(
        self,
        *,
        subdomain_manager=None,
        port_allocator=None,
        nginx_generator=None,
        docker_runner=None,
        database_provisioner=None,
        instance_root=None,
    ):
        self.subdomain_manager = subdomain_manager or SubdomainManager()
        self.port_allocator = port_allocator or PortAllocator()
        self.nginx_generator = nginx_generator or NginxConfigGenerator()
        self.docker_runner = docker_runner or DockerRunner()
        self.database_provisioner = database_provisioner or TenantDatabaseProvisioner(
            subdomain_manager=self.subdomain_manager
        )
        if instance_root is None:
            instance_root = (
                getattr(settings, "QASEDAK_TENANT_ROOT", None)
                or os.environ.get("QASEDAK_TENANT_ROOT")
                or self.default_instance_root
            )
        self.instance_root = pathlib.Path(instance_root)

    def preview_instance(
        self,
        tenant_id,
        *,
        base_domain=None,
        server_node_id=None,
        requested_port=None,
        docker_image=None,
    ):
        base_domain = base_domain or getattr(settings, "QASEDAK_BASE_DOMAIN", "") or os.environ.get("QASEDAK_BASE_DOMAIN", "")
        docker_image = docker_image or self.default_image
        tenant_id = self.subdomain_manager.validate_tenant_id(tenant_id)
        subdomain = self.subdomain_manager.generate(tenant_id, base_domain)
        server = self._preview_server_node(server_node_id)
        port = (
            self.port_allocator.next_free_port(server, preferred_port=requested_port)
            if server
            else int(requested_port or self.port_allocator.start_port)
        )
        container_name = f"qasedak_{tenant_id}"
        env_path = self.instance_root / tenant_id / ".env"
        database = self.database_provisioner.preview(tenant_id)
        return {
            "tenant_id": tenant_id,
            "subdomain": subdomain,
            "port": port,
            "container": container_name,
            "docker_image": docker_image,
            "tenant_root": str(self.instance_root / tenant_id),
            "env_path": str(env_path),
            "database": {
                "database": database["database"],
                "user": database["user"],
                "host": database["host"],
                "port": database["port"],
                "sslmode": database["sslmode"],
            },
            "nginx_config_path": str(self.nginx_generator.config_path(tenant_id)),
            "url": f"https://{subdomain}",
        }

    def deploy_instance(
        self,
        tenant_id,
        *,
        base_domain=None,
        database_url=None,
        telegram_bot_token=None,
        django_secret_key=None,
        server_node_id=None,
        requested_port=None,
        docker_image=None,
        extra_env=None,
        provision_database=False,
        bootstrap_tenant=False,
        tenant_display_name=None,
        tenant_admin_username="admin",
        tenant_admin_password=None,
        enable_bot_runtime=False,
    ):
        base_domain = base_domain or getattr(settings, "QASEDAK_BASE_DOMAIN", "") or os.environ.get("QASEDAK_BASE_DOMAIN", "")
        database_url = database_url or os.environ.get("DATABASE_URL", "")
        telegram_bot_token = str(telegram_bot_token or "").strip()
        docker_image = docker_image or self.default_image
        if not database_url and not provision_database:
            raise SubdomainDeploymentError("DATABASE_URL is required for tenant deployment.")
        if enable_bot_runtime and not telegram_bot_token:
            raise SubdomainDeploymentError("TELEGRAM_BOT_TOKEN is required when bot runtime is enabled.")

        tenant_id = self.subdomain_manager.validate_tenant_id(tenant_id)
        server = self._server_node(server_node_id)
        instance, created = self._instance_for_tenant(tenant_id, server, docker_image=docker_image)
        original_port = instance.port
        container_created = False
        nginx_written = False
        database_info = None
        database_created = False
        env_file = None
        admin_credentials_path = ""

        try:
            instance.status = TenantInstance.Status.DEPLOYING
            instance.deployment_status = TenantInstance.DeploymentStatus.DEPLOYING
            instance.error_message = ""
            instance.save(update_fields=["status", "deployment_status", "error_message", "updated_at"])

            port = self.port_allocator.allocate(instance, preferred_port=requested_port or instance.port)
            subdomain = self.subdomain_manager.assign(instance, base_domain)
            instance.refresh_from_db()

            db_env = {}
            if provision_database:
                database_info = self.database_provisioner.create(tenant_id)
                database_created = True
                db_env = {
                    "DATABASE_ENGINE": "postgres",
                    "POSTGRES_DB": database_info["database"],
                    "POSTGRES_USER": database_info["user"],
                    "POSTGRES_PASSWORD": database_info["password"],
                    "POSTGRES_HOST": database_info["host"],
                    "POSTGRES_PORT": database_info["port"],
                    "POSTGRES_SSLMODE": database_info["sslmode"],
                }

            if bootstrap_tenant and not tenant_admin_password:
                tenant_admin_password = secrets.token_urlsafe(24)
            if bootstrap_tenant:
                admin_credentials_path = self._write_admin_credentials_file(
                    instance,
                    username=tenant_admin_username,
                    password=tenant_admin_password,
                )

            env_file = self._write_env_file(
                instance,
                database_url="" if provision_database else database_url,
                telegram_bot_token=telegram_bot_token,
                django_secret_key=django_secret_key,
                admin_password=tenant_admin_password if bootstrap_tenant else None,
                enable_bot_runtime=enable_bot_runtime,
                extra_env={**db_env, **(extra_env or {})},
            )
            container_id = self.docker_runner.run_container(
                container_name=instance.container_name,
                port=port,
                env_file=env_file,
                image=docker_image,
            )
            container_created = True
            instance.container_id = container_id
            instance.deployment_status = TenantInstance.DeploymentStatus.CONTAINER_CREATED
            instance.save(update_fields=["container_id", "deployment_status", "updated_at"])

            if bootstrap_tenant:
                self.docker_runner.bootstrap_tenant(
                    container_name=instance.container_name,
                    config=self._bootstrap_config(
                        instance,
                        display_name=tenant_display_name,
                        admin_username=tenant_admin_username,
                    ),
                )

            nginx_config_path = self.nginx_generator.write_config(
                tenant_id=tenant_id,
                subdomain=subdomain,
                port=port,
            )
            nginx_written = True
            self.nginx_generator.enable_site(tenant_id)
            self.nginx_generator.reload()

            instance.status = TenantInstance.Status.RUNNING
            instance.deployment_status = TenantInstance.DeploymentStatus.DEPLOYED
            instance.nginx_config_path = nginx_config_path
            instance.instance_url = f"https://{subdomain}"
            instance.last_deployed_at = timezone.now()
            instance.runtime_state = {
                "container_name": instance.container_name,
                "subdomain": subdomain,
                "port": port,
                "nginx_enabled": True,
                "env_path": str(env_file),
                "database": {
                    "database": database_info["database"] if database_info else "",
                    "user": database_info["user"] if database_info else "",
                    "host": database_info["host"] if database_info else "",
                    "port": database_info["port"] if database_info else "",
                    "sslmode": database_info["sslmode"] if database_info else "",
                },
                "admin_credentials_path": admin_credentials_path,
            }
            instance.error_message = ""
            instance.save(
                update_fields=[
                    "status",
                    "deployment_status",
                    "nginx_config_path",
                    "instance_url",
                    "last_deployed_at",
                    "runtime_state",
                    "error_message",
                    "updated_at",
                ]
            )
            server.refresh_current_instances()
            return {
                "tenant_id": tenant_id,
                "subdomain": subdomain,
                "port": port,
                "container": instance.container_name,
                "container_id": container_id,
                "url": instance.instance_url,
                "env_path": str(env_file),
                "admin_credentials_path": admin_credentials_path,
                "database": {
                    "database": database_info["database"] if database_info else "",
                    "user": database_info["user"] if database_info else "",
                },
            }
        except Exception as exc:
            self._rollback(
                instance,
                created=created,
                original_port=original_port,
                container_created=container_created,
                nginx_written=nginx_written,
                database_created=database_created,
                database_info=database_info,
            )
            instance.status = TenantInstance.Status.FAILED
            instance.deployment_status = TenantInstance.DeploymentStatus.FAILED
            instance.error_message = exc.__class__.__name__
            instance.save(update_fields=["status", "deployment_status", "error_message", "updated_at"])
            raise SubdomainDeploymentError(str(exc) or exc.__class__.__name__) from exc

    def _instance_for_tenant(self, tenant_id, server, *, docker_image):
        container_name = f"qasedak_{tenant_id}"
        with transaction.atomic():
            instance = TenantInstance.objects.select_for_update().filter(tenant_id=tenant_id).first()
            if instance and instance.status != TenantInstance.Status.DELETED:
                return instance, False
            if instance:
                instance.server_node = server
                instance.container_name = container_name
                instance.docker_image_version = docker_image
                instance.status = TenantInstance.Status.PENDING
                instance.deployment_status = TenantInstance.DeploymentStatus.PENDING
                instance.save()
                return instance, False
            instance = TenantInstance.objects.create(
                tenant_id=tenant_id,
                server_node=server,
                container_name=container_name,
                docker_image_version=docker_image,
            )
            return instance, True

    def _server_node(self, server_node_id):
        if server_node_id:
            return ServerNode.objects.get(pk=server_node_id)
        server = ServerNode.objects.filter(status=ServerNode.Status.ACTIVE).order_by("current_instances", "id").first()
        if server:
            return server
        return ServerNode.objects.create(
            name="local-qasedak",
            ip="127.0.0.1",
            ssh_user="root",
            max_instances=1000,
        )

    def _preview_server_node(self, server_node_id):
        if server_node_id:
            return ServerNode.objects.filter(pk=server_node_id).first()
        return ServerNode.objects.filter(status=ServerNode.Status.ACTIVE).order_by("current_instances", "id").first()

    def _write_env_file(
        self,
        instance,
        *,
        database_url=None,
        telegram_bot_token="",
        django_secret_key=None,
        admin_password=None,
        enable_bot_runtime=False,
        extra_env=None,
    ):
        tenant_root = self.instance_root / instance.tenant_id
        tenant_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        env_file = tenant_root / ".env"
        values = {
            "TENANT_ID": instance.tenant_id,
            "DOMAIN": instance.subdomain or "",
            "PORT": "8000",
            "REVENUE_ENGINE_DRY_RUN": "true",
            "QASEDAK_BOT_ENABLED": "true" if enable_bot_runtime else "false",
            "QASEDAK_WORKER_ENABLED": "false",
            "DJANGO_SETTINGS_MODULE": "core.settings.production",
            "DJANGO_SECRET_KEY": django_secret_key or secrets.token_urlsafe(50),
        }
        for key in (
            "TELEGRAM_PROXY_URL",
            "TELEGRAM_PROXY_PROTOCOL",
            "TELEGRAM_PROXY_HOST",
            "TELEGRAM_PROXY_PORT",
            "TELEGRAM_PROXY_USERNAME",
            "TELEGRAM_PROXY_PASSWORD",
        ):
            value = os.environ.get(key, "").strip()
            if value:
                values[key] = value
        if telegram_bot_token:
            values["TELEGRAM_BOT_TOKEN"] = telegram_bot_token
        if database_url:
            values["DATABASE_URL"] = database_url
        if admin_password:
            values["QASEDAK_TENANT_ADMIN_PASSWORD"] = admin_password
        if instance.subdomain:
            values["DJANGO_ALLOWED_HOSTS"] = f"{instance.subdomain},127.0.0.1,localhost"
            values["DJANGO_CSRF_TRUSTED_ORIGINS"] = f"https://{instance.subdomain},http://{instance.subdomain}"
        values.update(extra_env or {})
        values["NO_PROXY"] = _merge_no_proxy_hosts(instance, values)
        lines = []
        for key, value in values.items():
            if not key.replace("_", "").isalnum() or key.upper() != key:
                raise SubdomainDeploymentError(f"Invalid env key: {key}")
            text = str(value)
            if "\x00" in text or "\n" in text or "\r" in text:
                raise SubdomainDeploymentError(f"Invalid env value for {key}")
            lines.append(f"{key}={text}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        return env_file

    def _write_admin_credentials_file(self, instance, *, username, password):
        tenant_root = self.instance_root / instance.tenant_id
        tenant_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = tenant_root / "admin-credentials.txt"
        text = (
            f"tenant_id={instance.tenant_id}\n"
            f"admin_url=https://{instance.subdomain}/admin/\n"
            f"username={username}\n"
            f"password={password}\n"
        )
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return str(path)

    def _bootstrap_config(self, instance, *, display_name=None, admin_username="admin"):
        name = str(display_name or instance.tenant_id).strip() or instance.tenant_id
        return {
            "app": {
                "domain": instance.subdomain or "",
                "timezone": "Asia/Tehran",
                "language": "fa",
            },
            "admin": {
                "username": admin_username,
                "password_env": "QASEDAK_TENANT_ADMIN_PASSWORD",
            },
            "store": {
                "name": name,
                "english_name": name,
                "slug": "default-store",
                "domain": instance.subdomain or "",
                "card_number": SAFE_PLACEHOLDER_CARD_NUMBER,
                "card_owner": SAFE_PLACEHOLDER_CARD_OWNER,
            },
            "telegram": {
                "enabled": False,
                "create_inactive_placeholder": True,
                "name": f"{name} Telegram setup placeholder",
            },
            "xui": {
                "configure_now": False,
            },
            "revenue_engine": {
                "enabled": True,
                "dry_run": True,
            },
        }

    def _rollback(
        self,
        instance,
        *,
        created,
        original_port,
        container_created,
        nginx_written,
        database_created=False,
        database_info=None,
    ):
        rollback_errors = []
        if created or nginx_written:
            try:
                self.nginx_generator.remove(instance.tenant_id)
            except Exception as exc:
                rollback_errors.append(exc.__class__.__name__)
        if container_created:
            try:
                self.docker_runner.remove_container(instance.container_name)
            except Exception as exc:
                rollback_errors.append(exc.__class__.__name__)
        if database_created:
            try:
                self.database_provisioner.rollback(database_info)
            except Exception as exc:
                rollback_errors.append(exc.__class__.__name__)
        if created or original_port is None:
            try:
                self.port_allocator.release(instance)
                instance.container_id = ""
                instance.nginx_config_path = ""
                instance.runtime_state = {"rollback_errors": rollback_errors}
                instance.save(update_fields=["container_id", "nginx_config_path", "runtime_state", "updated_at"])
            except Exception as exc:
                rollback_errors.append(exc.__class__.__name__)


def deploy_instance(tenant_id, **kwargs):
    return SubdomainDeploymentService().deploy_instance(tenant_id, **kwargs)
