from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0021_alter_botconfiguration_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="store",
            name="receipt_image_only_payment",
            field=models.BooleanField(
                default=False,
                help_text=_("When enabled, checkout only asks for a receipt image for this card."),
                verbose_name=_("receipt image only payment"),
            ),
        ),
    ]
