from django.db import migrations


def backfill_orphan_inbound_panels(apps, schema_editor):
    Inbound = apps.get_model("store", "Inbound")
    Panel = apps.get_model("store", "Panel")

    orphan_inbounds = Inbound.objects.filter(panel__isnull=True)
    if not orphan_inbounds.exists():
        return

    active_panels = list(Panel.objects.filter(is_active=True).order_by("pk")[:2])
    if len(active_panels) != 1:
        return

    orphan_inbounds.update(panel=active_panels[0])


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0028_store_sales_mode_operator_order_operator_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_orphan_inbound_panels, migrations.RunPython.noop),
    ]
