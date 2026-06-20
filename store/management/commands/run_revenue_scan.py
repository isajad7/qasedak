from django.core.management.base import BaseCommand

from store.revenue_engine.scheduler import run_retention_scan, run_revenue_learning_loop, run_revenue_scan


class Command(BaseCommand):
    help = "Run the Smart Renewal Revenue Engine scan."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Log revenue offers without sending Telegram messages.")
        parser.add_argument(
            "--engine",
            choices=("renewal", "upsell", "retention", "all"),
            default="all",
            help="Engine scope to scan. Upsell is event-driven, so scan mode reports zero candidates.",
        )
        parser.add_argument("--customer-id", type=int, help="Limit scan to a customer id.")
        parser.add_argument("--client-id", type=int, help="Limit renewal scan to a VPN client id.")
        parser.add_argument("--limit", type=int, help="Maximum records to scan.")
        parser.add_argument("--verbose", action="store_true", help="Print per-engine counts.")

    def handle(self, *args, **options):
        engine = options["engine"]
        if engine == "retention":
            summary = run_retention_scan(
                dry_run=options["dry_run"],
                customer_id=options.get("customer_id"),
                limit=options.get("limit"),
            )
        elif engine == "upsell":
            summary = {
                "scanned": 0,
                "candidates": 0,
                "events": 0,
                "handled": 0,
                "sent": 0,
                "dry_run": 0,
                "suppressed": 0,
                "skipped": 0,
                "skipped_no_target": 0,
                "failed": 0,
                "errors": 0,
                "converted_recent": 0,
                "per_engine": {"upsell": {"sent": 0, "dry_run": 0, "suppressed": 0, "skipped": 0, "failed": 0}},
            }
        else:
            summary = run_revenue_scan(
                dry_run=options["dry_run"],
                customer_id=options.get("customer_id"),
                client_id=options.get("client_id"),
                limit=options.get("limit"),
            )
            if engine == "all":
                retention_summary = run_retention_scan(
                    dry_run=options["dry_run"],
                    customer_id=options.get("customer_id"),
                    limit=options.get("limit"),
                )
                for key in (
                    "scanned",
                    "candidates",
                    "events",
                    "handled",
                    "sent",
                    "dry_run",
                    "suppressed",
                    "skipped",
                    "skipped_no_target",
                    "failed",
                    "errors",
                    "converted_recent",
                ):
                    summary[key] = int(summary.get(key) or 0) + int(retention_summary.get(key) or 0)
                for engine_name, counts in (retention_summary.get("per_engine") or {}).items():
                    target = summary.setdefault("per_engine", {}).setdefault(
                        engine_name,
                        {"sent": 0, "dry_run": 0, "suppressed": 0, "skipped": 0, "failed": 0},
                    )
                    for count_key, value in counts.items():
                        target[count_key] = int(target.get(count_key) or 0) + int(value or 0)
        learning = run_revenue_learning_loop()
        learned_variants = sum(len(variants) for variants in learning.values())
        self.stdout.write(
            "Revenue scan summary: "
            f"scanned={summary['scanned']} "
            f"candidates={summary['candidates']} "
            f"events={summary['events']} "
            f"handled={summary['handled']} "
            f"sent={summary['sent']} "
            f"dry_run={summary['dry_run']} "
            f"suppressed={summary['suppressed']} "
            f"skipped_no_target={summary['skipped_no_target']} "
            f"skipped={summary['skipped']} "
            f"failed={summary['failed']} "
            f"converted_recent={summary['converted_recent']} "
            f"errors={summary['errors']} "
            f"learning_variants={learned_variants}"
        )
        if options["verbose"]:
            for engine_name, counts in sorted((summary.get("per_engine") or {}).items()):
                self.stdout.write(
                    f"{engine_name}: "
                    f"sent={counts.get('sent', 0)} dry_run={counts.get('dry_run', 0)} "
                    f"suppressed={counts.get('suppressed', 0)} skipped={counts.get('skipped', 0)} "
                    f"failed={counts.get('failed', 0)}"
                )
