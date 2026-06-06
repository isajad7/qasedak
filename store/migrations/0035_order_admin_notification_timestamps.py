from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0034_panel_proxy_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="admin_notified_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name=_("admin notified at")),
        ),
        migrations.AddField(
            model_name="order",
            name="admin_receipt_notified_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name=_("admin receipt notified at")),
        ),
    ]
