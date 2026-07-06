# Generated for Qasedak subdomain multi-tenant deployment.

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0055_orchestrator_v2"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenantinstance",
            name="port",
            field=models.PositiveIntegerField(
                blank=True,
                db_index=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1024),
                    django.core.validators.MaxValueValidator(65535),
                ],
                verbose_name="public port",
            ),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="subdomain",
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True, unique=True, verbose_name="subdomain"),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="nginx_config_path",
            field=models.CharField(blank=True, max_length=600, verbose_name="Nginx config path"),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="container_id",
            field=models.CharField(blank=True, db_index=True, max_length=128, verbose_name="container ID"),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="deployment_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("deploying", "Deploying"),
                    ("container_created", "Container created"),
                    ("nginx_configured", "Nginx configured"),
                    ("deployed", "Deployed"),
                    ("failed", "Failed"),
                    ("rolled_back", "Rolled back"),
                ],
                db_index=True,
                default="pending",
                max_length=32,
                verbose_name="deployment status",
            ),
        ),
    ]
