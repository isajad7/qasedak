import base64

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from .external_subscription_sources import DEFAULT_REFRESH_INTERVAL_HOURS, default_external_subscription_filter_policy
from .models import ConfigLink, CupItem, ExternalSubscriptionFeed, SubscriptionCup


class ExternalSubscriptionIntervalTests(SimpleTestCase):
    def test_dynamic_subscription_refresh_defaults_to_one_hour(self):
        self.assertEqual(DEFAULT_REFRESH_INTERVAL_HOURS, 1)
        self.assertEqual(default_external_subscription_filter_policy()["refresh_interval_hours"], 1)
        self.assertEqual(ExternalSubscriptionFeed._meta.get_field("refresh_interval_hours").default, 1)


class ClientSubscriptionResponseTests(TestCase):
    def test_v2rayng_gets_base64_subscription_by_default(self):
        cup = SubscriptionCup.objects.create()
        link = "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000001@node.example.com:443#Node"
        config = ConfigLink.objects.create(
            raw_link=link,
            normalized_link=link,
            protocol=ConfigLink.Protocol.VLESS,
        )
        CupItem.objects.create(cup=cup, config_link=config, position=1, is_active=True)

        response = Client().get(
            reverse("subscription_cup", args=[cup.token]),
            HTTP_USER_AGENT="v2rayNG/1.10.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertEqual(base64.b64decode(response.content).decode("utf-8"), f"{link}\n")

    def test_raw_subscription_remains_available_explicitly(self):
        cup = SubscriptionCup.objects.create()
        link = "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000002@node.example.com:443#Raw"
        config = ConfigLink.objects.create(raw_link=link, normalized_link=link, protocol=ConfigLink.Protocol.VLESS)
        CupItem.objects.create(cup=cup, config_link=config, position=1, is_active=True)

        response = Client().get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})

        self.assertEqual(response.content.decode("utf-8"), link)
