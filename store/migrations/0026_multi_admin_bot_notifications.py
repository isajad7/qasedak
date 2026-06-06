from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0025_custom_volume_purchase"),
    ]

    operations = [
        migrations.AddField(
            model_name="botconfiguration",
            name="additional_admin_user_ids",
            field=models.TextField(
                blank=True,
                help_text=_("Optional extra admin chat/user IDs, separated by comma, space, or new line."),
                verbose_name=_("additional admin user IDs"),
            ),
        ),
        migrations.CreateModel(
            name="BotAdminOrderMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now, editable=False, verbose_name=_("created at"))),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name=_("updated at"))),
                ("admin_user_id", models.CharField(db_index=True, max_length=80, verbose_name=_("admin user ID"))),
                ("chat_id", models.CharField(db_index=True, max_length=80, verbose_name=_("chat ID"))),
                ("message_id", models.CharField(db_index=True, max_length=80, verbose_name=_("message ID"))),
                ("message_kind", models.CharField(choices=[("text", _("Text")), ("photo", _("Photo"))], default="text", max_length=20, verbose_name=_("message kind"))),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name=_("metadata"))),
                ("bot_config", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="admin_order_messages", to="store.botconfiguration", verbose_name=_("bot configuration"))),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bot_admin_messages", to="store.order", verbose_name=_("order"))),
            ],
            options={
                "verbose_name": _("bot admin order message"),
                "verbose_name_plural": _("bot admin order messages"),
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="botadminordermessage",
            index=models.Index(fields=["bot_config", "order", "admin_user_id"], name="store_botad_bot_con_674810_idx"),
        ),
        migrations.AddIndex(
            model_name="botadminordermessage",
            index=models.Index(fields=["order", "message_kind"], name="store_botad_order_i_e2871d_idx"),
        ),
    ]
