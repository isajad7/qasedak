import base64
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import tests as fixtures
from .external_subscription_sources import ExternalSubscriptionRefreshError, refresh_due_external_subscription_feeds, refresh_external_subscription_feed
from .models import ConfigLink, CupItem, ExternalSubscriptionFeed, Order, SubscriptionCup
from .subscription_cups import active_cup_links, create_config_link_from_raw, rebuild_subscription_cup_for_vpn_client
from .subscription_sync import ensure_pasarguard_feeds_for_cup


class SubscriptionSyncTests(TestCase):
    setUp = fixtures.SubscriptionCupMVPTests.setUp
    direct_link = fixtures.SubscriptionCupMVPTests.direct_link
    create_vpn_client = fixtures.SubscriptionCupMVPTests.create_vpn_client
    create_pasarguard_panel_and_groups = fixtures.SubscriptionCupMVPTests.create_pasarguard_panel_and_groups
    create_dynamic_feed_with_links = fixtures.SubscriptionCupMVPTests.create_dynamic_feed_with_links

    def static_cup(self, *, client_bound=False):
        panel, groups = self.create_pasarguard_panel_and_groups()
        self.vpn_client.inbound = groups[0]
        self.vpn_client.sub_link = "https://pasarguard.example.com/s/private-old-token"
        self.vpn_client.direct_link = self.direct_link(901, host="old.example.com")
        self.vpn_client.save()
        cup = SubscriptionCup.objects.create(order=self.order, customer=self.customer, plan=self.plan,
            vpn_client=self.vpn_client if client_bound else None)
        link = create_config_link_from_raw(self.vpn_client.direct_link, source_type="panel_generated", source_panel=panel, vpn_client=self.vpn_client)
        CupItem.objects.create(cup=cup, config_link=link, position=1)
        return cup, link

    def read(self, cup):
        response = self.client.get(reverse("subscription_cup", args=[cup.token]), HTTP_USER_AGENT="v2rayNG/1.10.0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        return base64.b64decode(response.content).decode().splitlines()

    def age(self, feed):
        ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(last_attempt_at=timezone.now() - timedelta(seconds=61))

    def test_customer_update_adopts_legacy_cup_and_returns_latest_panel_list(self):
        cup, old = self.static_cup()
        token = cup.token
        latest = [self.direct_link(902), self.direct_link(903)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest) as fetch:
            self.assertEqual(self.read(cup), latest)
            self.assertEqual(self.read(cup), latest)
            fetch.assert_called_once()
        cup.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(cup.token, token)
        self.assertFalse(old.is_active)
        feed = ExternalSubscriptionFeed.objects.get(cup=cup)
        self.assertEqual(feed.protected_subscription_url, self.vpn_client.sub_link)
        self.assertEqual(feed.last_good_config_count, 2)
        self.assertEqual(feed.refresh_token, "")

    def test_update_does_not_wait_for_hourly_schedule_after_one_minute(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(910)])
        self.age(feed)
        self.assertGreater(feed.next_refresh_at, timezone.now())
        latest = [self.direct_link(911)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest):
            self.assertEqual(self.read(cup), latest)

    def test_timeout_empty_and_suspicious_drop_preserve_last_good_output(self):
        links = [self.direct_link(920 + index) for index in range(10)]
        feed, cup, _, _ = self.create_dynamic_feed_with_links(links)
        for response in (RuntimeError("secret https://private.example/token"), [], [self.direct_link(931)]):
            self.age(feed)
            kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", **kwargs):
                self.assertEqual(self.read(cup), links)
        feed.refresh_from_db()
        self.assertNotIn("private.example", str(feed.metadata))

    def test_refresh_replaces_only_its_own_source_and_keeps_manual_links(self):
        cup, _ = self.static_cup()
        manual = self.direct_link(940)
        link = create_config_link_from_raw(manual, source_type="manual")
        CupItem.objects.create(cup=cup, config_link=link, position=2)
        latest = [self.direct_link(941)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest):
            self.assertCountEqual(self.read(cup), [manual, *latest])
        link.refresh_from_db()
        self.assertTrue(link.is_active)
        self.assertIsNone(link.external_feed_id)

    def test_rebuild_cannot_restore_initial_snapshot_over_refreshed_configs(self):
        cup, _ = self.static_cup(client_bound=True)
        latest = [self.direct_link(950), self.direct_link(951)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest):
            self.assertEqual(self.read(cup), latest)
        rebuilt = rebuild_subscription_cup_for_vpn_client(self.vpn_client)
        self.assertEqual(rebuilt.pk, cup.pk)
        self.assertEqual(active_cup_links(rebuilt), latest)

    def test_repair_is_idempotent_and_first_snapshot_is_due_immediately(self):
        cup, _ = self.static_cup()
        first = ensure_pasarguard_feeds_for_cup(cup)
        second = ensure_pasarguard_feeds_for_cup(cup)
        self.assertEqual((first["created"], second["created"]), (1, 0))
        feed = ExternalSubscriptionFeed.objects.get(cup=cup)
        self.assertIsNone(feed.last_attempt_at)
        self.assertIsNone(feed.last_success_at)
        self.assertLessEqual(feed.next_refresh_at, timezone.now())

    def test_disabled_and_expired_cups_do_not_contact_provider(self):
        cup, _ = self.static_cup()
        for values in ({"status": "disabled"}, {"status": "active", "expires_at": timezone.now() - timedelta(days=1)}):
            SubscriptionCup.objects.filter(pk=cup.pk).update(**values)
            with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links") as fetch:
                response = self.client.get(reverse("subscription_cup", args=[cup.token]))
                self.assertEqual(response.status_code, 403)
                fetch.assert_not_called()
        self.assertFalse(ExternalSubscriptionFeed.objects.exists())

    def test_disabled_feed_is_not_enabled_by_client_update(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(960)])
        ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(active=False, status="disabled")
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links") as fetch:
            self.assertEqual(self.read(cup), [self.direct_link(960)])
            fetch.assert_not_called()

    def test_shared_or_foreign_owned_static_configs_are_not_adopted(self):
        cup, link = self.static_cup()
        other = SubscriptionCup.objects.create()
        CupItem.objects.create(cup=other, config_link=link)
        self.assertEqual(ensure_pasarguard_feeds_for_cup(cup)["skipped"], {"ambiguous_ownership": 1})
        CupItem.objects.filter(cup=other).delete()
        foreign = Order.objects.create(store=self.store, plan=self.plan, status="completed", verification_status="verified")
        SubscriptionCup.objects.filter(pk=cup.pk).update(order=foreign)
        self.assertEqual(ensure_pasarguard_feeds_for_cup(cup)["skipped"], {"ambiguous_ownership": 1})
        self.assertFalse(ExternalSubscriptionFeed.objects.exists())

    def test_current_cup_url_cannot_be_used_as_its_own_upstream(self):
        cup, _ = self.static_cup()
        self.vpn_client.sub_link = f"https://vpn.example.com/sub/{cup.token}"
        self.vpn_client.save()
        self.assertEqual(ensure_pasarguard_feeds_for_cup(cup)["skipped"], {"missing_upstream_url": 1})

    def test_background_refresh_includes_missing_due_date(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(970)])
        ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(next_refresh_at=None)
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=[self.direct_link(971)]):
            report = refresh_due_external_subscription_feeds()
        self.assertEqual((report["checked"], report["ok"]), (1, 1))
        self.assertEqual(active_cup_links(cup), [self.direct_link(971)])

    def test_lease_blocks_another_request_even_when_forced(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(980)])
        ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(refresh_token="other-runner", refresh_lease_until=timezone.now() + timedelta(seconds=30))
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links") as fetch:
            result = refresh_external_subscription_feed(feed.pk, force=True)
            self.assertTrue(result.skipped)
            fetch.assert_not_called()
        self.assertEqual(active_cup_links(cup), [self.direct_link(980)])

    def test_superseded_runner_cannot_commit_or_release_another_lease(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(990)])
        def replace_lease(_feed):
            ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(refresh_token="new-runner", refresh_lease_until=timezone.now() + timedelta(seconds=30))
            return [self.direct_link(991)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", side_effect=replace_lease):
            result = refresh_external_subscription_feed(feed.pk)
        self.assertEqual(result.error_code, "refresh_superseded")
        feed.refresh_from_db()
        self.assertEqual(feed.refresh_token, "new-runner")
        self.assertEqual(active_cup_links(cup), [self.direct_link(990)])

    def test_changed_upstream_during_fetch_cannot_commit_old_list(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(992)])
        def rotate(_feed):
            ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(protected_subscription_url="https://pasarguard.example.com/s/new-private-token")
            return [self.direct_link(993)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", side_effect=rotate):
            result = refresh_external_subscription_feed(feed.pk)
        self.assertEqual(result.error_code, "refresh_source_changed")
        self.assertEqual(active_cup_links(cup), [self.direct_link(992)])

    def test_repair_command_outputs_counts_and_verifies_client_serialization(self):
        cup, _ = self.static_cup()
        output = StringIO()
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=[self.direct_link(994)]):
            call_command("sync_pasarguard_subscriptions", refresh=True, stdout=output)
        self.assertIn('"feeds_created": 1', output.getvalue())
        self.assertIn('"verified_cups": 1', output.getvalue())
        self.assertNotIn(cup.token, output.getvalue())
        self.assertNotIn("private-old-token", output.getvalue())
        self.assertNotIn("vless://", output.getvalue())

    def test_disabling_feed_during_network_read_prevents_commit(self):
        feed, cup, _, _ = self.create_dynamic_feed_with_links([self.direct_link(995)])
        def disable(_feed):
            ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(active=False, status="disabled")
            return [self.direct_link(996)]
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", side_effect=disable):
            self.assertTrue(refresh_external_subscription_feed(feed.pk).skipped)
        self.assertEqual(active_cup_links(cup), [self.direct_link(995)])
        ExternalSubscriptionFeed.objects.filter(pk=feed.pk).update(active=True, status="active")
        def disable_and_fail(_feed):
            disable(_feed)
            raise ExternalSubscriptionRefreshError("Provider unavailable", code="provider_fetch_failed")
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", side_effect=disable_and_fail):
            self.assertTrue(refresh_external_subscription_feed(feed.pk).skipped)
        feed.refresh_from_db()
        self.assertEqual(feed.status, "disabled")
        self.assertEqual(active_cup_links(cup), [self.direct_link(995)])

    def test_public_verification_checks_real_client_output_and_masks_failure(self):
        cup, _ = self.static_cup()
        latest = [self.direct_link(997)]
        good = Mock(status_code=200, content=base64.b64encode((latest[0] + "\n").encode()))
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest), patch("requests.get", return_value=good) as get:
            output = StringIO()
            call_command("sync_pasarguard_subscriptions", refresh=True, public_base_url="https://vpn.example.com", stdout=output)
            self.assertIn('"public_verified": 1', output.getvalue())
            self.assertIn(cup.token, get.call_args.args[0])
            self.assertNotIn(cup.token, output.getvalue())

        bad = Mock(status_code=200, content=base64.b64encode(b"vless://old-list\n"))
        with patch("store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links", return_value=latest), patch("requests.get", return_value=bad):
            output = StringIO()
            with self.assertRaises(CommandError):
                call_command("sync_pasarguard_subscriptions", refresh=True, public_base_url="https://vpn.example.com", stdout=output)
            self.assertIn("list_mismatch", output.getvalue())
            self.assertNotIn(cup.token, output.getvalue())

    def test_bulk_timeout_reports_safe_network_counts_and_preserves_links(self):
        import requests
        cup, _ = self.static_cup()
        previous = active_cup_links(cup)
        adapter = Mock()
        adapter.client.fetch_native_links.side_effect = requests.ReadTimeout("https://private.example/s/private-old-token")
        output = StringIO()
        with patch("store.panels.get_safe_panel_adapter", return_value=adapter):
            call_command("sync_pasarguard_subscriptions", refresh=True, stdout=output)
        self.assertEqual(adapter.client.timeout, (2, 4))
        self.assertIn('"ReadTimeout": 1', output.getvalue())
        self.assertIn('"failed": 1', output.getvalue())
        self.assertNotIn("private.example", output.getvalue())
        self.assertNotIn("private-old-token", output.getvalue())
        self.assertEqual(active_cup_links(cup), previous)
