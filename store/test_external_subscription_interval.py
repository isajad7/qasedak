from django.test import SimpleTestCase

from .external_subscription_sources import DEFAULT_REFRESH_INTERVAL_HOURS, default_external_subscription_filter_policy
from .models import ExternalSubscriptionFeed


class ExternalSubscriptionIntervalTests(SimpleTestCase):
    def test_dynamic_subscription_refresh_defaults_to_one_hour(self):
        self.assertEqual(DEFAULT_REFRESH_INTERVAL_HOURS, 1)
        self.assertEqual(default_external_subscription_filter_policy()["refresh_interval_hours"], 1)
        self.assertEqual(ExternalSubscriptionFeed._meta.get_field("refresh_interval_hours").default, 1)
