import json
from django.core.management.base import BaseCommand, CommandError
from urllib.parse import urlsplit
from store.subscription_sync import repair_and_refresh_pasarguard_subscriptions


class Command(BaseCommand):
    help = "Connect owned PasarGuard Cups to dynamic feeds and optionally refresh/verify; safe counts only."

    def add_arguments(self, parser):
        parser.add_argument("--refresh", action="store_true")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--public-base-url", help="Verify up to five real client URLs after refreshing; prints counts only.")

    def handle(self, *args, **options):
        base = options["public_base_url"]
        if base:
            parsed = urlsplit(base)
            if not options["refresh"] or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                raise CommandError("Public verification requires --refresh and an HTTPS base URL.")
        result = repair_and_refresh_pasarguard_subscriptions(refresh=options["refresh"], limit=options["limit"], public_base_url=base)
        self.stdout.write(json.dumps(result))
        if result.get("public_errors"):
            raise CommandError("Public subscription verification failed; inspect the safe error counts above.")
