from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


tenant_id_validator = RegexValidator(
    regex=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
    message=_("Use 3-63 lowercase letters, numbers, and hyphens; start and end with a letter or number."),
)


class ServerNode(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PROVISIONING = "provisioning", _("Provisioning")
        DEGRADED = "degraded", _("Degraded")
        DISABLED = "disabled", _("Disabled")
        UNAVAILABLE = "unavailable", _("Unavailable")

    class CredentialMode(models.TextChoices):
        SSH_KEY = "ssh_key", _("SSH key")
        VAULT = "vault", _("Vault reference")
        TRANSIENT = "transient", _("Transient credential")

    name = models.CharField(_("name"), max_length=120, unique=True)
    ip = models.GenericIPAddressField(_("IP address"), protocol="both", unpack_ipv4=True, unique=True)
    ssh_user = models.CharField(_("SSH user"), max_length=80, default="root")
    ssh_port = models.PositiveIntegerField(
        _("SSH port"),
        default=22,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    credential_mode = models.CharField(
        _("credential mode"),
        max_length=20,
        choices=CredentialMode.choices,
        default=CredentialMode.SSH_KEY,
    )
    ssh_key_path = models.CharField(_("SSH key path"), max_length=500, blank=True)
    ssh_key_vault_ref = models.CharField(_("SSH key vault reference"), max_length=255, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    max_instances = models.PositiveIntegerField(_("max instances"), default=10, validators=[MinValueValidator(1)])
    current_instances = models.PositiveIntegerField(_("current instances"), default=0)
    last_checked_at = models.DateTimeField(_("last checked at"), null=True, blank=True)
    safe_metadata = models.JSONField(_("safe metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        app_label = "store"
        db_table = "store_orchestrator_server_node"
        verbose_name = _("orchestrator server node")
        verbose_name_plural = _("orchestrator server nodes")
        ordering = ["name", "id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["ip", "ssh_port"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.ip})"

    def refresh_current_instances(self, save=True):
        active_statuses = [
            TenantInstance.Status.PENDING,
            TenantInstance.Status.DEPLOYING,
            TenantInstance.Status.RUNNING,
            TenantInstance.Status.STOPPED,
            TenantInstance.Status.DEGRADED,
            TenantInstance.Status.FAILED,
        ]
        self.current_instances = self.tenant_instances.filter(status__in=active_statuses).count()
        if save:
            self.save(update_fields=["current_instances", "updated_at"])
        return self.current_instances

    @property
    def has_capacity(self):
        return self.current_instances < self.max_instances

    def mark_checked(self, *, status=None, metadata=None):
        if status:
            self.status = status
        if metadata is not None:
            self.safe_metadata = metadata
        self.last_checked_at = timezone.now()
        self.save(update_fields=["status", "safe_metadata", "last_checked_at", "updated_at"])


class TenantInstance(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        DEPLOYING = "deploying", _("Deploying")
        RUNNING = "running", _("Running")
        STOPPED = "stopped", _("Stopped")
        DEGRADED = "degraded", _("Degraded")
        FAILED = "failed", _("Failed")
        DELETING = "deleting", _("Deleting")
        DELETED = "deleted", _("Deleted")

    class DeploymentStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        DEPLOYING = "deploying", _("Deploying")
        CONTAINER_CREATED = "container_created", _("Container created")
        NGINX_CONFIGURED = "nginx_configured", _("Nginx configured")
        DEPLOYED = "deployed", _("Deployed")
        FAILED = "failed", _("Failed")
        ROLLED_BACK = "rolled_back", _("Rolled back")

    class DNSStatus(models.TextChoices):
        NOT_CONFIGURED = "not_configured", _("Not configured")
        PENDING = "pending", _("Pending")
        VERIFIED = "verified", _("Verified")
        FAILED = "failed", _("Failed")

    tenant_id = models.SlugField(
        _("tenant ID"),
        max_length=63,
        unique=True,
        validators=[tenant_id_validator],
        help_text=_("Stable SaaS tenant identifier."),
    )
    server_node = models.ForeignKey(
        ServerNode,
        verbose_name=_("server node"),
        on_delete=models.PROTECT,
        related_name="tenant_instances",
    )
    container_name = models.CharField(_("container name"), max_length=128, unique=True)
    port = models.PositiveIntegerField(
        _("public port"),
        validators=[MinValueValidator(1024), MaxValueValidator(65535)],
        db_index=True,
        null=True,
        blank=True,
    )
    domain = models.CharField(_("domain"), max_length=255, blank=True)
    subdomain = models.CharField(_("subdomain"), max_length=255, unique=True, null=True, blank=True, db_index=True)
    customer_domain = models.CharField(_("customer custom domain"), max_length=255, blank=True)
    customer_domain_dns_status = models.CharField(
        _("customer custom domain DNS status"),
        max_length=32,
        choices=DNSStatus.choices,
        default=DNSStatus.NOT_CONFIGURED,
        db_index=True,
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    deployment_status = models.CharField(
        _("deployment status"),
        max_length=32,
        choices=DeploymentStatus.choices,
        default=DeploymentStatus.PENDING,
        db_index=True,
    )
    instance_url = models.URLField(_("instance URL"), max_length=500, blank=True)
    nginx_config_path = models.CharField(_("Nginx config path"), max_length=600, blank=True)
    container_id = models.CharField(_("container ID"), max_length=128, blank=True, db_index=True)
    docker_image_version = models.CharField(_("Docker image version"), max_length=255, default="qasedak-core:latest")
    last_deployed_at = models.DateTimeField(_("last deployed at"), null=True, blank=True)
    last_health = models.JSONField(_("last health"), default=dict, blank=True)
    runtime_state = models.JSONField(_("runtime state"), default=dict, blank=True)
    error_message = models.TextField(_("error message"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        related_name="orchestrator_tenant_instances",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        app_label = "store"
        db_table = "store_orchestrator_tenant_instance"
        verbose_name = _("orchestrator tenant instance")
        verbose_name_plural = _("orchestrator tenant instances")
        ordering = ["tenant_id", "id"]
        constraints = [
            models.UniqueConstraint(fields=["server_node", "port"], name="unique_orchestrator_port_per_server"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["server_node", "status"]),
            models.Index(fields=["tenant_id", "status"]),
        ]

    def __str__(self):
        return self.tenant_id
