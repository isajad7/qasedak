from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0026_multi_admin_bot_notifications"),
    ]

    operations = [
        migrations.AlterField(
            model_name="botuser",
            name="state",
            field=models.CharField(
                choices=[
                    ("idle", "Idle"),
                    ("profile_wait_phone", "Profile: waiting for phone"),
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
