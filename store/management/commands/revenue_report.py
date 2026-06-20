from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import RevenueOfferLog


class Command(BaseCommand):
    help = "Report Revenue Engine audit and conversion metrics."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1, choices=(1, 7, 30), help="Reporting window.")
        parser.add_argument(
            "--engine",
            choices=("renewal", "upsell", "retention", "silent_active", "ai_optimizer", "all"),
            default="all",
            help="Engine filter.",
        )
        parser.add_argument("--verbose", action="store_true", help="Print top variants and source breakdown.")

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=int(options["days"]))
        logs = RevenueOfferLog.objects.filter(created_at__gte=since)
        if options["engine"] != "all":
            logs = logs.filter(engine_type=options["engine"])

        sent = logs.filter(status__in=[RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED]).count()
        dry_run = logs.filter(status=RevenueOfferLog.Status.DRY_RUN).count()
        suppressed = logs.filter(status=RevenueOfferLog.Status.SUPPRESSED).count()
        failed = logs.filter(status=RevenueOfferLog.Status.FAILED).count()
        conversions = logs.filter(status=RevenueOfferLog.Status.CONVERTED).count()
        conversion_rate = (conversions / sent * 100) if sent else 0

        self.stdout.write(
            "Revenue report: "
            f"days={options['days']} engine={options['engine']} "
            f"offers_sent={sent} dry_run={dry_run} suppressed={suppressed} "
            f"failed={failed} conversions={conversions} conversion_rate={conversion_rate:.2f}%"
        )

        if not options["verbose"]:
            return

        variants = {}
        sources = {}
        for log in logs.only("variant", "decision_source", "status").iterator():
            variant = log.variant or "control"
            source = log.decision_source or "unknown"
            variants.setdefault(variant, {"sent": 0, "converted": 0})
            sources.setdefault(source, {"sent": 0, "converted": 0})
            if log.status in {RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED}:
                variants[variant]["sent"] += 1
                sources[source]["sent"] += 1
            if log.status == RevenueOfferLog.Status.CONVERTED:
                variants[variant]["converted"] += 1
                sources[source]["converted"] += 1

        for variant, counts in sorted(variants.items()):
            rate = (counts["converted"] / counts["sent"] * 100) if counts["sent"] else 0
            self.stdout.write(f"variant={variant} sent={counts['sent']} converted={counts['converted']} rate={rate:.2f}%")
        for source, counts in sorted(sources.items()):
            rate = (counts["converted"] / counts["sent"] * 100) if counts["sent"] else 0
            self.stdout.write(f"source={source} sent={counts['sent']} converted={counts['converted']} rate={rate:.2f}%")
