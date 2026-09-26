import django.core.validators
from django.db import migrations, models
from django.utils import timezone


def set_existing_dynamic_feeds_hourly(apps, schema_editor):
    PlanDeliverySource = apps.get_model("store", "PlanDeliverySource")
    ExternalSubscriptionFeed = apps.get_model("store", "ExternalSubscriptionFeed")
    db_alias = schema_editor.connection.alias

    updated_sources = []
    for source in PlanDeliverySource.objects.using(db_alias).all().iterator():
        metadata = dict(source.metadata or {})
        policy = metadata.get("dynamic_subscription_policy")
        if not isinstance(policy, dict) or policy.get("refresh_interval_hours") not in (12, "12"):
            continue
        policy["refresh_interval_hours"] = 1
        metadata["dynamic_subscription_policy"] = policy
        source.metadata = metadata
        updated_sources.append(source)
    if updated_sources:
        PlanDeliverySource.objects.using(db_alias).bulk_update(updated_sources, ["metadata"], batch_size=200)

    now = timezone.now()
    updated_feeds = []
    for feed in ExternalSubscriptionFeed.objects.using(db_alias).all().iterator():
        policy = dict(feed.resolved_filter_policy or {})
        if policy.get("refresh_interval_hours") in (12, "12"):
            policy["refresh_interval_hours"] = 1
            feed.resolved_filter_policy = policy
        feed.refresh_interval_hours = 1
        feed.next_refresh_at = now
        updated_feeds.append(feed)
    if updated_feeds:
        ExternalSubscriptionFeed.objects.using(db_alias).bulk_update(
            updated_feeds,
            ["resolved_filter_policy", "refresh_interval_hours", "next_refresh_at"],
            batch_size=200,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0066_inbound_last_verification_error_code_and_more"),
    ]

    operations = [
        migrations.RunPython(set_existing_dynamic_feeds_hourly, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="externalsubscriptionfeed",
            name="refresh_interval_hours",
            field=models.PositiveIntegerField(
                default=1,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="refresh interval hours",
            ),
        ),
    ]
