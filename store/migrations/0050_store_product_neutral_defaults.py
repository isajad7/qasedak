# Generated during Productization P1 on 2026-06-20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0049_store_ai_revenue_optimizer_enabled_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="store",
            name="english_name",
            field=models.CharField(default="VPN Store", max_length=100, verbose_name="English name"),
        ),
        migrations.AlterField(
            model_name="store",
            name="name",
            field=models.CharField(default="VPN Store", max_length=100, verbose_name="name"),
        ),
    ]
