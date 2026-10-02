"""Observation-only data. Never use a sync timestamp as proof of VPN activity."""
from django.db import models


class ActivityCollector(models.Model):
    # A fenced lease keeps overlapping containers from writing competing baselines.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.CharField(max_length=32, blank=True)
    lease_until = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    summary = models.JSONField(default=dict)


class PurchaseActivity(models.Model):
    order = models.OneToOneField("store.Order", on_delete=models.CASCADE, related_name="activity")
    observed_at = models.DateTimeField(null=True, db_index=True)
    entitlement = models.CharField(max_length=24, default="unknown")
    reason = models.CharField(max_length=48, default="not_observed")
    # Keys are scoped hashes; values contain counters/timestamps, never credentials.
    counters = models.JSONField(default=dict)
    continuous_since = models.DateTimeField(null=True)
    last_activity_start = models.DateTimeField(null=True)
    last_activity_at = models.DateTimeField(null=True)
    source_count = models.PositiveIntegerField(default=0)
    ended_at = models.DateTimeField(null=True)


class ActivityObservation(models.Model):
    purchase = models.ForeignKey(PurchaseActivity, on_delete=models.CASCADE, related_name="observations")
    observed_at = models.DateTimeField()
    interval_start = models.DateTimeField(null=True)
    delta_bytes = models.PositiveBigIntegerField(null=True)
    quality = models.CharField(max_length=48)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["purchase", "observed_at"], name="activity_purchase_sample_unique")]
        indexes = [models.Index(fields=["observed_at"], name="activity_observed_idx")]
