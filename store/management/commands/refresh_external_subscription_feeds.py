from django.core.management.base import BaseCommand

from store.external_subscription_sources import (
    refresh_due_external_subscription_feeds,
    refresh_external_subscription_feed,
)


class Command(BaseCommand):
    help = "Refresh dynamic external subscription feeds without printing upstream URLs or raw config links."

    def add_arguments(self, parser):
        parser.add_argument("--feed-id", type=int, help="Refresh only this feed.")
        parser.add_argument("--force", action="store_true", help="Refresh even when not due.")
        parser.add_argument("--limit", type=int, help="Maximum number of due feeds to refresh.")
        parser.add_argument("--dry-run", action="store_true", help="Fetch and filter only; do not mutate Cup items.")

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        force = bool(options.get("force"))
        if options.get("feed_id"):
            result = refresh_external_subscription_feed(
                options["feed_id"],
                force=force,
                dry_run=dry_run,
            ).to_safe_dict()
            self.stdout.write(
                "External subscription feed: "
                f"feed_id={result['feed_id']} "
                f"ok={result['ok']} "
                f"status={result['status']} "
                f"dry_run={result['dry_run']} "
                f"kept_last_good={result['kept_last_good']} "
                f"error_code={result['error_code'] or '-'} "
                f"input={result['upstream_count']} "
                f"parsed={result['parsed_count']} "
                f"invalid={result['invalid_count']} "
                f"deduplicated={result['deduplicated_count']} "
                f"filtered={result['filtered_count']} "
                f"selected={result['selected_count']} "
                f"current_items={result['current_item_count']}"
            )
            return

        summary = refresh_due_external_subscription_feeds(
            force=force,
            limit=options.get("limit"),
            dry_run=dry_run,
        )
        self.stdout.write(
            "External subscription feeds summary: "
            f"checked={summary['checked']} "
            f"ok={summary['ok']} "
            f"failed={summary['failed']} "
            f"skipped={summary['skipped']} "
            f"dry_run={summary['dry_run']}"
        )
        for result in summary["results"]:
            self.stdout.write(
                "feed "
                f"id={result['feed_id']} "
                f"ok={result['ok']} "
                f"status={result['status']} "
                f"error_code={result['error_code'] or '-'} "
                f"input={result['upstream_count']} "
                f"selected={result['selected_count']} "
                f"current_items={result['current_item_count']} "
                f"kept_last_good={result['kept_last_good']}"
            )
