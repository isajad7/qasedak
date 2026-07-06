# Generated for Qasedak Orchestrator v2.

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0054_qasedakbackupjob_qasedakrestorejob"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ServerNode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True, verbose_name="name")),
                ("ip", models.GenericIPAddressField(protocol="both", unpack_ipv4=True, unique=True, verbose_name="IP address")),
                ("ssh_user", models.CharField(default="root", max_length=80, verbose_name="SSH user")),
                (
                    "ssh_port",
                    models.PositiveIntegerField(
                        default=22,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(65535),
                        ],
                        verbose_name="SSH port",
                    ),
                ),
                (
                    "credential_mode",
                    models.CharField(
                        choices=[
                            ("ssh_key", "SSH key"),
                            ("vault", "Vault reference"),
                            ("transient", "Transient credential"),
                        ],
                        default="ssh_key",
                        max_length=20,
                        verbose_name="credential mode",
                    ),
                ),
                ("ssh_key_path", models.CharField(blank=True, max_length=500, verbose_name="SSH key path")),
                ("ssh_key_vault_ref", models.CharField(blank=True, max_length=255, verbose_name="SSH key vault reference")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("provisioning", "Provisioning"),
                            ("degraded", "Degraded"),
                            ("disabled", "Disabled"),
                            ("unavailable", "Unavailable"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                ("max_instances", models.PositiveIntegerField(default=10, validators=[django.core.validators.MinValueValidator(1)], verbose_name="max instances")),
                ("current_instances", models.PositiveIntegerField(default=0, verbose_name="current instances")),
                ("last_checked_at", models.DateTimeField(blank=True, null=True, verbose_name="last checked at")),
                ("safe_metadata", models.JSONField(blank=True, default=dict, verbose_name="safe metadata")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
            ],
            options={
                "verbose_name": "orchestrator server node",
                "verbose_name_plural": "orchestrator server nodes",
                "db_table": "store_orchestrator_server_node",
                "ordering": ["name", "id"],
            },
        ),
        migrations.CreateModel(
            name="TenantInstance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "tenant_id",
                    models.SlugField(
                        help_text="Stable SaaS tenant identifier.",
                        max_length=63,
                        unique=True,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="Use 3-63 lowercase letters, numbers, and hyphens; start and end with a letter or number.",
                                regex="^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
                            )
                        ],
                        verbose_name="tenant ID",
                    ),
                ),
                ("container_name", models.CharField(max_length=128, unique=True, verbose_name="container name")),
                (
                    "port",
                    models.PositiveIntegerField(
                        db_index=True,
                        validators=[
                            django.core.validators.MinValueValidator(1024),
                            django.core.validators.MaxValueValidator(65535),
                        ],
                        verbose_name="public port",
                    ),
                ),
                ("domain", models.CharField(blank=True, max_length=255, verbose_name="domain")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("deploying", "Deploying"),
                            ("running", "Running"),
                            ("stopped", "Stopped"),
                            ("degraded", "Degraded"),
                            ("failed", "Failed"),
                            ("deleting", "Deleting"),
                            ("deleted", "Deleted"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                ("instance_url", models.URLField(blank=True, max_length=500, verbose_name="instance URL")),
                ("docker_image_version", models.CharField(default="qasedak-core:latest", max_length=255, verbose_name="Docker image version")),
                ("last_deployed_at", models.DateTimeField(blank=True, null=True, verbose_name="last deployed at")),
                ("last_health", models.JSONField(blank=True, default=dict, verbose_name="last health")),
                ("runtime_state", models.JSONField(blank=True, default=dict, verbose_name="runtime state")),
                ("error_message", models.TextField(blank=True, verbose_name="error message")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="updated at")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="orchestrator_tenant_instances",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="created by",
                    ),
                ),
                (
                    "server_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tenant_instances",
                        to="store.servernode",
                        verbose_name="server node",
                    ),
                ),
            ],
            options={
                "verbose_name": "orchestrator tenant instance",
                "verbose_name_plural": "orchestrator tenant instances",
                "db_table": "store_orchestrator_tenant_instance",
                "ordering": ["tenant_id", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="servernode",
            index=models.Index(fields=["status", "created_at"], name="store_orche_status_c100bc_idx"),
        ),
        migrations.AddIndex(
            model_name="servernode",
            index=models.Index(fields=["ip", "ssh_port"], name="store_orche_ip_d64534_idx"),
        ),
        migrations.AddIndex(
            model_name="tenantinstance",
            index=models.Index(fields=["status", "created_at"], name="store_orche_status_59fbc1_idx"),
        ),
        migrations.AddIndex(
            model_name="tenantinstance",
            index=models.Index(fields=["server_node", "status"], name="store_orche_server__e4abac_idx"),
        ),
        migrations.AddIndex(
            model_name="tenantinstance",
            index=models.Index(fields=["tenant_id", "status"], name="store_orche_tenant__923d85_idx"),
        ),
        migrations.AddConstraint(
            model_name="tenantinstance",
            constraint=models.UniqueConstraint(fields=("server_node", "port"), name="unique_orchestrator_port_per_server"),
        ),
    ]
