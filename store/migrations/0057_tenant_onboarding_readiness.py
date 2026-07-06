# Generated for Qasedak SaaS tenant onboarding readiness.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0056_subdomain_deployment_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="store",
            name="setup_status",
            field=models.CharField(
                choices=[
                    ("provisioned", "Provisioned"),
                    ("setup_required", "Setup required"),
                    ("telegram_configured", "Telegram configured"),
                    ("payment_configured", "Payment configured"),
                    ("xui_configured", "X-UI configured"),
                    ("plans_configured", "Plans configured"),
                    ("ready", "Ready"),
                    ("suspended", "Suspended"),
                    ("error", "Error"),
                ],
                db_index=True,
                default="ready",
                help_text="Tenant onboarding lifecycle. Existing/manual stores default to ready for backwards compatibility.",
                max_length=32,
                verbose_name="setup status",
            ),
        ),
        migrations.AlterField(
            model_name="botconfiguration",
            name="admin_user_id",
            field=models.CharField(blank=True, help_text="Admin chat/user ID that receives notifications.", max_length=80, verbose_name="admin user ID"),
        ),
        migrations.AlterField(
            model_name="botconfiguration",
            name="bot_token",
            field=models.CharField(blank=True, help_text="Bot token from Bale or Telegram.", max_length=255, verbose_name="bot token"),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="customer_domain",
            field=models.CharField(blank=True, max_length=255, verbose_name="customer custom domain"),
        ),
        migrations.AddField(
            model_name="tenantinstance",
            name="customer_domain_dns_status",
            field=models.CharField(
                choices=[
                    ("not_configured", "Not configured"),
                    ("pending", "Pending"),
                    ("verified", "Verified"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="not_configured",
                max_length=32,
                verbose_name="customer custom domain DNS status",
            ),
        ),
    ]
