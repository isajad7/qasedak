import time

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.utils import timezone

from store.broadcast_services import (
    classify_delivery_error,
    create_campaign_recipients,
    get_campaign_store,
    is_retryable_delivery_error,
    normalize_delivery_error,
    refresh_campaign_counts,
    resolve_campaign_recipients,
    send_message_to_customer,
)
from store.models import BroadcastMessage, BroadcastRecipient


DEFAULT_BATCH_SIZE = 50
MAX_BATCH_SIZE = 200
MAX_CONSECUTIVE_TRANSIENT_FAILURES = 3


class Command(BaseCommand):
    help = "Process queued BroadcastMessage campaigns in bounded batches."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, help="Process one campaign only.")
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--limit", type=int, help="Maximum recipients to process across all selected campaigns.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be processed without sending or writing.")
        parser.add_argument("--resume-failed", action="store_true", help="Move retryable failed recipients back to pending before processing.")
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, **options):
        batch_size = self._bounded_batch_size(options["batch_size"])
        remaining = options["limit"] if options.get("limit") is not None else None
        if remaining is not None and remaining < 1:
            raise CommandError("--limit must be positive.")

        campaigns = self._campaign_queryset(options.get("campaign_id"))
        dry_run = bool(options["dry_run"])
        summary = {
            "campaigns": 0,
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "rate_limited": 0,
            "dry_run": dry_run,
        }

        for campaign in campaigns:
            if remaining is not None and remaining <= 0:
                break
            result = self._process_campaign(
                campaign,
                batch_size=batch_size,
                limit=remaining,
                dry_run=dry_run,
                resume_failed=bool(options["resume_failed"]),
                verbose=bool(options["verbose"]),
            )
            summary["campaigns"] += 1
            for key in ("processed", "sent", "failed", "skipped", "rate_limited"):
                summary[key] += result.get(key, 0)
            if remaining is not None:
                remaining -= result.get("processed", 0)

        self.stdout.write(
            "Broadcast queue summary: "
            f"campaigns={summary['campaigns']} "
            f"processed={summary['processed']} "
            f"sent={summary['sent']} "
            f"failed={summary['failed']} "
            f"skipped={summary['skipped']} "
            f"rate_limited={summary['rate_limited']} "
            f"dry_run={summary['dry_run']}"
        )

    def _bounded_batch_size(self, value):
        try:
            size = int(value or DEFAULT_BATCH_SIZE)
        except (TypeError, ValueError) as exc:
            raise CommandError("--batch-size must be an integer.") from exc
        if size < 1:
            raise CommandError("--batch-size must be positive.")
        return min(size, MAX_BATCH_SIZE)

    def _campaign_queryset(self, campaign_id):
        now = timezone.now()
        statuses = [BroadcastMessage.Status.QUEUED, BroadcastMessage.Status.SENDING]
        campaigns = BroadcastMessage.objects.select_related("store").filter(status__in=statuses)
        campaigns = campaigns.filter(scheduled_at__isnull=True) | campaigns.filter(scheduled_at__lte=now)
        if campaign_id:
            campaigns = campaigns.filter(pk=campaign_id)
        return campaigns.order_by("scheduled_at", "created_at", "pk")

    def _process_campaign(self, campaign, *, batch_size, limit, dry_run, resume_failed, verbose):
        campaign.refresh_from_db()
        if campaign.status == BroadcastMessage.Status.CANCELLED:
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "rate_limited": 0}

        if not (campaign.message_text or "").strip():
            if not dry_run:
                campaign.status = BroadcastMessage.Status.FAILED
                campaign.metadata = {**(campaign.metadata or {}), "error": "Message text is required."}
                campaign.save(update_fields=["status", "metadata", "updated_at"])
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "rate_limited": 0}

        store = get_campaign_store(campaign)
        if not getattr(store, "broadcast_enabled", True):
            if not dry_run:
                campaign.status = BroadcastMessage.Status.FAILED
                campaign.metadata = {**(campaign.metadata or {}), "error": "Broadcast is disabled for this store."}
                campaign.save(update_fields=["status", "metadata", "updated_at"])
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "rate_limited": 0}

        if dry_run:
            existing = BroadcastRecipient.objects.filter(campaign=campaign).count()
            pending = BroadcastRecipient.objects.filter(campaign=campaign, status=BroadcastRecipient.Status.PENDING).count()
            would_materialize = 0 if existing else len(resolve_campaign_recipients(campaign))
            self.stdout.write(
                f"dry-run campaign={campaign.pk} existing_recipients={existing} "
                f"pending={pending} would_materialize={would_materialize}"
            )
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "rate_limited": 0}

        if not BroadcastRecipient.objects.filter(campaign=campaign).exists():
            create_campaign_recipients(campaign)

        if resume_failed:
            self._resume_retryable_failed(campaign)

        campaign.status = BroadcastMessage.Status.SENDING
        campaign.save(update_fields=["status", "updated_at"])

        rate_limit = self._positive_int(getattr(store, "broadcast_rate_limit_per_second", None), 5)
        delay = 1 / rate_limit if rate_limit else 0
        selected_limit = batch_size if limit is None else min(batch_size, limit)
        pending = (
            BroadcastRecipient.objects.select_related("customer")
            .filter(campaign=campaign, status=BroadcastRecipient.Status.PENDING)
            .order_by("created_at", "pk")[:selected_limit]
        )

        result = {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "rate_limited": 0}
        consecutive_transient = 0
        for recipient in pending:
            campaign.refresh_from_db(fields=["status"])
            if campaign.status == BroadcastMessage.Status.CANCELLED:
                break

            before_status = recipient.status
            try:
                updated = send_message_to_customer(campaign, recipient)
            except (ValidationError, Exception) as exc:
                updated = self._mark_failed(recipient, normalize_delivery_error(exc))

            result["processed"] += 1
            if updated.status == BroadcastRecipient.Status.SENT:
                result["sent"] += 1
                consecutive_transient = 0
            elif updated.status == BroadcastRecipient.Status.SKIPPED:
                result["skipped"] += 1
                consecutive_transient = 0
            elif updated.status == BroadcastRecipient.Status.FAILED:
                result["failed"] += 1
                category = classify_delivery_error(updated.error_message)
                if category == "rate_limited":
                    result["rate_limited"] += 1
                    break
                if category == "timeout_network":
                    consecutive_transient += 1
                    if consecutive_transient >= MAX_CONSECUTIVE_TRANSIENT_FAILURES:
                        break
                else:
                    consecutive_transient = 0

            if verbose:
                self.stdout.write(
                    f"campaign={campaign.pk} recipient={recipient.pk} "
                    f"{before_status}->{updated.status} error={classify_delivery_error(updated.error_message)}"
                )
            if delay:
                time.sleep(delay)

        self._finalize_campaign(campaign)
        return result

    def _resume_retryable_failed(self, campaign):
        retryable_ids = []
        failed = BroadcastRecipient.objects.filter(campaign=campaign, status=BroadcastRecipient.Status.FAILED).only("pk", "error_message")
        for recipient in failed.iterator():
            if is_retryable_delivery_error(recipient.error_message):
                retryable_ids.append(recipient.pk)
        if retryable_ids:
            BroadcastRecipient.objects.filter(pk__in=retryable_ids).update(
                status=BroadcastRecipient.Status.PENDING,
                error_message="",
                sent_at=None,
                updated_at=timezone.now(),
            )

    def _mark_failed(self, recipient, message):
        recipient.status = BroadcastRecipient.Status.FAILED
        recipient.error_message = message[:1000] or "Unknown delivery error."
        recipient.sent_at = None
        recipient.save(update_fields=["status", "error_message", "sent_at", "updated_at"])
        return recipient

    def _finalize_campaign(self, campaign):
        campaign.refresh_from_db()
        counts = refresh_campaign_counts(campaign)
        if campaign.status == BroadcastMessage.Status.CANCELLED:
            return counts
        pending_exists = BroadcastRecipient.objects.filter(campaign=campaign, status=BroadcastRecipient.Status.PENDING).exists()
        if pending_exists:
            campaign.status = BroadcastMessage.Status.QUEUED
            campaign.save(update_fields=["status", "updated_at"])
            return counts
        campaign.status = BroadcastMessage.Status.FAILED if counts["success"] == 0 and counts["failed"] else BroadcastMessage.Status.SENT
        if campaign.status == BroadcastMessage.Status.SENT:
            campaign.sent_at = timezone.now()
            campaign.save(update_fields=["status", "sent_at", "updated_at"])
        else:
            campaign.save(update_fields=["status", "updated_at"])
        return counts

    def _positive_int(self, value, default):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default
