from decimal import Decimal

import django.core.validators
from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0024_alter_botuser_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="store",
            name="custom_volume_price_per_gb",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                help_text=_("When greater than zero, customers can buy custom GB volume for 30 days at this unit price."),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                verbose_name=_("custom volume price per GB"),
            ),
        ),
        migrations.AddField(
            model_name="plan",
            name="is_custom_volume",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=_("Generated internal plan for custom-volume purchases."),
                verbose_name=_("is custom volume"),
            ),
        ),
        migrations.AlterField(
            model_name="botuser",
            name="state",
            field=models.CharField(
                choices=[
                    ("idle", "Idle"),
                    ("buy_wait_custom_volume", "Buy: waiting for custom volume"),
                    ("buy_wait_quantity", "Buy: waiting for quantity"),
                    ("buy_wait_name", "Buy: waiting for payer name"),
                    ("buy_wait_last4", "Buy: waiting for card last4"),
                    ("buy_wait_time", "Buy: waiting for payment time"),
                    ("buy_wait_tracking", "Buy: waiting for bank tracking"),
                    ("buy_wait_receipt", "Buy: waiting for receipt"),
                    ("grant_wait_user", "Grant: waiting for user"),
                    ("grant_wait_reason", "Grant: waiting for reason"),
                    ("grant_confirm", "Grant: confirm"),
                ],
                db_index=True,
                default="idle",
                max_length=40,
                verbose_name="state",
            ),
        ),
    ]
