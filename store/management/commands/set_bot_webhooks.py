import requests
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from store.models import BotConfiguration


class Command(BaseCommand):
    help = "Register Bale/Telegram webhook URLs for active bot configurations."

    BASE_URLS = {
        BotConfiguration.Provider.BALE: "https://tapi.bale.ai/bot{token}",
        BotConfiguration.Provider.TELEGRAM: "https://api.telegram.org/bot{token}",
    }

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            required=True,
            help="Public HTTPS base URL, e.g. https://panel.example.com",
        )
        parser.add_argument(
            "--provider",
            choices=BotConfiguration.Provider.values,
            help="Only set webhooks for one provider.",
        )

    def handle(self, *args, **options):
        base_url = options["base_url"].rstrip("/")
        if not base_url.startswith("https://"):
            raise CommandError("Bot webhooks must use an HTTPS base URL.")

        configs = BotConfiguration.objects.filter(is_active=True)
        if options.get("provider"):
            configs = configs.filter(provider=options["provider"])

        updated = 0
        for config in configs:
            webhook_path = reverse("bot_webhook", args=[config.provider, config.webhook_secret])
            webhook_url = f"{base_url}{webhook_path}"
            api_base = self.BASE_URLS[config.provider].format(token=config.bot_token).rstrip("/")
            response = requests.post(
                f"{api_base}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("ok") is False:
                raise CommandError(payload.get("description") or f"Webhook failed for {config}.")
            updated += 1
            self.stdout.write(self.style.SUCCESS(f"{config}: {webhook_url}"))

        self.stdout.write(self.style.SUCCESS(f"Registered {updated} webhook(s)."))
