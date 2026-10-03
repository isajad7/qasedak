import base64
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from store.models import ConfigLink, CupItem, Customer, Inbound, Order, Panel, Plan, Store, SubscriptionCup, VPNClient
from store.xui_api import hash_xui_identifier
from .mapping import credential_from_link, load_purchase_maps, resolve_maps
from .models import ActivityCollector, ActivityObservation, PurchaseActivity
from .panels import read_panel
from .services import LEASE, _claim, _heartbeat, activity_status, collect_activity, record_purchase
from .views import customer_rows, daily_counts


class ActivityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = Store.objects.create(name="Activity", card_number="0000000000000000", card_owner="Owner")
        cls.plan = Plan.objects.create(store=cls.store, name="10 GB", volume_gb=10, price=100000, duration_days=30)
        cls.customer = Customer.objects.create(display_name="Buyer")
        cls.panel = Panel.objects.create(store=cls.store, name="Panel", url="https://panel.invalid", username="test", password="secret")
        cls.inbound = Inbound.objects.create(panel=cls.panel, inbound_id=1, xui_node_id="node-1", server_ip="example.com", port="443")
        cls.admin = get_user_model().objects.create_superuser("activity-owner", "owner@example.com", "secret")

    def setUp(self):
        self.now = datetime(2026, 10, 2, 10, tzinfo=ZoneInfo("UTC"))

    def purchase(self, **overrides):
        data = dict(store=self.store, customer=self.customer, plan=self.plan, status="completed", verification_status="verified", verified_at=self.now)
        data.update(overrides)
        return Order.objects.create(**data)

    def vpn(self, order, **overrides):
        data = dict(store=self.store, order=order, plan=self.plan, inbound=self.inbound, uuid=f"uuid-{order.pk}",
                    username=f"user-{order.pk}", xui_email=f"user-{order.pk}", xui_node_id="node-1", status="active")
        data.update(overrides)
        return VPNClient.objects.create(**data)

    def sample(self, vpn, used=0, at=None, **overrides):
        digest = hash_xui_identifier(vpn.uuid)
        row = dict(panel_id=vpn.inbound.panel_id, inbound_id=vpn.inbound_id, remote_inbound_id=vpn.inbound.inbound_id,
                   node_id=vpn.xui_node_id, identifier_hash=digest, aliases={digest, hash_xui_identifier(vpn.xui_email)},
                   used_bytes=used, upload_bytes=0, download_bytes=used, total_bytes=10 * 1024**3,
                   expiry_time=self.now + timedelta(days=30), enabled=True, stats_available=True, captured_at=at or self.now)
        row.update(overrides)
        return row

    def record(self, order, vpn, *, used=0, at=None, **overrides):
        maps, orphans = load_purchase_maps()
        mapping = next(item for item in maps if item.order.pk == order.pk)
        rows = [self.sample(vpn, used, at, **overrides)]
        matched, reason = resolve_maps(maps, orphans, rows)[order.pk]
        return record_purchase(mapping, matched, reason, now=at or self.now)

    def test_first_snapshot_and_sync_time_are_not_usage(self):
        order = self.purchase()
        vpn = self.vpn(order, used_traffic_bytes=9000000, last_synced_at=self.now, last_online_at=self.now)
        state = self.record(order, vpn, used=9000000)
        self.assertEqual(activity_status(state, now=self.now)[0], "collecting")
        self.assertIsNone(state.last_activity_at)
        state = self.record(order, vpn, used=9000001, at=self.now + timedelta(minutes=15))
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "active")

    def test_inactivity_requires_full_48_hours_continuous_observation(self):
        order = self.purchase()
        vpn = self.vpn(order)
        for minute in range(0, 48 * 60, 30):
            state = self.record(order, vpn, at=self.now + timedelta(minutes=minute))
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "collecting")
        state = self.record(order, vpn, at=self.now + timedelta(hours=48))
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "inactive")
        self.assertEqual(activity_status(state, now=state.observed_at + timedelta(minutes=46)), ("unknown", "stale"))

    def test_gaps_and_counter_resets_cannot_prove_inactivity_or_fresh_usage(self):
        order = self.purchase()
        vpn = self.vpn(order)
        self.record(order, vpn, used=1000)
        state = self.record(order, vpn, used=1001, at=self.now + timedelta(days=3))
        self.assertEqual(state.reason, "gap")
        self.assertIsNone(state.last_activity_at)
        state = self.record(order, vpn, used=2, at=state.observed_at + timedelta(minutes=15))
        self.assertEqual(state.reason, "counter_reset")
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "collecting")

    def test_missing_negative_and_stale_counters_never_become_zero_use(self):
        for overrides in ({"stats_available": False}, {"used_bytes": -1}, {"captured_at": self.now - timedelta(hours=2)}):
            order = self.purchase()
            vpn = self.vpn(order)
            state = self.record(order, vpn, **overrides)
            self.assertEqual(activity_status(state, now=self.now)[0], "unknown")
            self.assertFalse(state.counters)

    def test_changed_quota_or_mapping_starts_new_baseline(self):
        order = self.purchase()
        vpn = self.vpn(order)
        self.record(order, vpn, used=100)
        state = self.record(order, vpn, used=200, at=self.now + timedelta(minutes=15), total_bytes=9999)
        self.assertEqual(state.reason, "cycle_changed")
        vpn.uuid = "new-uuid"
        vpn.save(update_fields=["uuid"])
        state = self.record(order, vpn, used=300, at=self.now + timedelta(minutes=30))
        self.assertEqual(state.reason, "mapping_changed")
        self.assertIsNone(state.last_activity_at)

    def test_multiple_purchases_are_independent_and_daily_customers_are_unique(self):
        orders = [self.purchase(), self.purchase()]
        for order in orders:
            vpn = self.vpn(order)
            self.record(order, vpn)
            self.record(order, vpn, used=300, at=self.now + timedelta(minutes=15))
        self.assertEqual(PurchaseActivity.objects.count(), 2)
        days = daily_counts(self.store, now=self.now + timedelta(minutes=15))
        self.assertEqual((days[-1]["active"], days[-1]["measured"]), (1, 1))
        rows = customer_rows(self.store, now=self.now + timedelta(minutes=15))
        self.assertEqual(rows[0]["state"], "active")
        self.assertEqual(len(rows[0]["purchases"]), 2)
        self.assertIn("loyal", rows[0]["tags"])

    def test_shared_cup_identity_even_in_separate_link_rows_is_unknown(self):
        order = self.purchase()
        vpn = self.vpn(order)
        second = self.purchase(customer=Customer.objects.create(display_name="Other"))
        cup = SubscriptionCup.objects.create(order=second, customer=second.customer, plan=self.plan)
        link = ConfigLink.objects.create(raw_link=f"vless://{vpn.uuid}@example.com:443", normalized_hash="second",
                                         source_panel=self.panel, source_inbound=self.inbound)
        CupItem.objects.create(cup=cup, config_link=link)
        maps, orphans = load_purchase_maps()
        resolved = resolve_maps(maps, orphans, [self.sample(vpn)])
        self.assertEqual(resolved[order.pk][1], "shared_identity")
        self.assertEqual(resolved[second.pk][1], "shared_identity")

    def test_unscoped_and_orphan_shared_cups_block_false_exclusivity(self):
        order = self.purchase()
        vpn = self.vpn(order)
        cup = SubscriptionCup.objects.create(customer=Customer.objects.create(display_name="Other"))
        link = ConfigLink.objects.create(raw_link=f"vless://{vpn.uuid}@example.com:443", normalized_hash="orphan")
        CupItem.objects.create(cup=cup, config_link=link)
        state = self.record(order, vpn)
        self.assertEqual(state.reason, "shared_identity")

    def test_orphan_cup_with_direct_vpn_reference_is_still_a_shared_claim(self):
        order = self.purchase()
        vpn = self.vpn(order)
        SubscriptionCup.objects.create(customer=Customer.objects.create(display_name="Other"), vpn_client=vpn)
        self.assertEqual(self.record(order, vpn).reason, "shared_identity")

    def test_mismatched_link_and_vpn_reference_is_not_trusted(self):
        order = self.purchase()
        vpn = self.vpn(order)
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer)
        link = ConfigLink.objects.create(raw_link="vless://unrelated@example.com:443", normalized_hash="wrong", vpn_client=vpn)
        CupItem.objects.create(cup=cup, config_link=link)
        state = self.record(order, vpn)
        self.assertEqual(state.reason, "partial_baseline")
        self.assertEqual(state.entitlement, "unknown")

    def test_closed_cup_with_working_credentials_is_not_lost(self):
        order = self.purchase()
        vpn = self.vpn(order)
        SubscriptionCup.objects.create(order=order, customer=self.customer, vpn_client=vpn, status="disabled")
        self.assertEqual(self.record(order, vpn).reason, "closed_cup_active")

    def test_unknown_quota_does_not_prove_service_is_usable(self):
        order = self.purchase()
        vpn = self.vpn(order)
        state = self.record(order, vpn, total_bytes=None)
        self.assertEqual(state.entitlement, "unknown")
        self.assertNotIn("at_risk", customer_rows(self.store, now=self.now)[0]["tags"])

    def test_orphan_vpn_client_identity_claim_blocks_attribution(self):
        order = self.purchase()
        vpn = self.vpn(order)
        VPNClient.objects.create(store=self.store, inbound=self.inbound, username="orphan", xui_email=vpn.xui_email,
                                 xui_node_id="node-1", status="active")
        self.assertEqual(self.record(order, vpn).reason, "shared_identity")

    def test_renewal_transfers_only_target_service_and_new_cycle_has_no_history(self):
        first = self.purchase()
        vpn = self.vpn(first)
        cup = SubscriptionCup.objects.create(order=first, customer=self.customer, vpn_client=vpn)
        self.record(first, vpn)
        renewal = self.purchase(verified_at=self.now + timedelta(hours=1), metadata={"renewal_client_pk": vpn.pk})
        maps, orphans = load_purchase_maps()
        older = next(m for m in maps if m.order.pk == first.pk)
        self.assertTrue(older.superseded)
        resolved = resolve_maps(maps, orphans, [self.sample(vpn)])
        self.assertEqual(resolved[renewal.pk][1], "")
        state = self.record(renewal, vpn)
        self.assertEqual(state.reason, "baseline")
        self.assertIsNone(state.last_activity_at)
        # An additional independent account from the original purchase must remain monitored.
        self.vpn(first, uuid="second-account", username="second-account", xui_email="second-account")
        maps, _ = load_purchase_maps()
        self.assertFalse(next(m for m in maps if m.order.pk == first.pk).superseded)

    def test_cancelled_order_active_remote_service_is_discrepancy(self):
        order = self.purchase(status="cancelled", verification_status="rejected")
        vpn = self.vpn(order)
        state = self.record(order, vpn)
        self.assertEqual((state.entitlement, state.reason), ("conflict", "closed_order_active"))
        self.assertIn("conflict", customer_rows(self.store, now=self.now)[0]["tags"])

    def test_wrong_node_and_duplicate_remote_identity_fail_closed(self):
        order = self.purchase()
        vpn = self.vpn(order)
        state = self.record(order, vpn, node_id="other-node")
        self.assertEqual(state.reason, "unmapped_source")
        maps, orphans = load_purchase_maps()
        row = self.sample(vpn)
        self.assertEqual(resolve_maps(maps, orphans, [row, row])[order.pk][1], "unmapped_source")

    def test_unfinished_or_foreign_renewal_does_not_transfer_ownership(self):
        order = self.purchase()
        vpn = self.vpn(order)
        renewal = self.purchase(status="pending_verification", verification_status="pending", metadata={"renewal_client_pk": vpn.pk})
        maps, _ = load_purchase_maps()
        self.assertTrue(next(m for m in maps if m.order.pk == order.pk).sources)
        self.assertFalse(any(m.order.pk == renewal.pk for m in maps))
        renewal.status = "completed"
        renewal.verification_status = "verified"
        renewal.customer = Customer.objects.create(display_name="Foreign")
        renewal.save()
        maps, orphans = load_purchase_maps()
        self.assertEqual(resolve_maps(maps, orphans, [self.sample(vpn)])[renewal.pk][1], "ownership_conflict")

    def test_multiple_successive_renewals_supersede_all_prior_cycles(self):
        order = self.purchase()
        vpn = self.vpn(order)
        renewals = [self.purchase(verified_at=self.now + timedelta(hours=i), metadata={"renewal_client_pk": vpn.pk}) for i in (1, 2)]
        maps, _ = load_purchase_maps()
        by_id = {item.order.pk: item for item in maps}
        self.assertTrue(by_id[order.pk].superseded)
        self.assertTrue(by_id[renewals[0].pk].superseded)
        self.assertFalse(by_id[renewals[1].pk].superseded)

    def test_idempotent_observation_and_older_samples_do_not_rewind(self):
        order = self.purchase()
        vpn = self.vpn(order)
        self.record(order, vpn)
        self.record(order, vpn)
        self.record(order, vpn, at=self.now - timedelta(minutes=15))
        self.assertEqual(ActivityObservation.objects.count(), 1)
        self.assertEqual(PurchaseActivity.objects.get(order=order).observed_at, self.now)

    def test_cross_midnight_interval_is_not_assigned_to_wrong_day(self):
        order = self.purchase()
        vpn = self.vpn(order)
        before = datetime(2026, 10, 2, 23, 55, tzinfo=ZoneInfo("Asia/Tehran"))
        after = before + timedelta(minutes=15)
        self.record(order, vpn, at=before)
        self.record(order, vpn, at=after, used=10)
        self.assertEqual(daily_counts(self.store, now=after)[-1]["active"], 0)

    def test_lost_requires_all_services_ended_and_an_active_purchase_prevents_it(self):
        order = self.purchase()
        vpn = self.vpn(order)
        state = self.record(order, vpn, expiry_time=self.now - timedelta(days=10))
        self.assertEqual(state.entitlement, "expired")
        self.assertEqual(customer_rows(self.store, now=self.now)[0]["state"], "lost")
        second = self.purchase()
        other = self.vpn(second)
        self.record(second, other)
        self.record(second, other, at=self.now + timedelta(minutes=15), used=100)
        self.assertEqual(customer_rows(self.store, now=self.now + timedelta(minutes=15))[0]["state"], "active")

    @patch("customer_activity.services.read_panel")
    def test_collection_reads_panel_once_for_many_orders_and_records_failure(self, reader):
        first, second = self.purchase(), self.purchase()
        clients = [self.vpn(first), self.vpn(second)]
        reader.return_value = ([self.sample(vpn, at=timezone.now()) for vpn in clients], "ok")
        result = collect_activity()
        self.assertEqual(result["purchases"], 2)
        reader.assert_called_once()
        self.assertIsNotNone(ActivityCollector.objects.get(pk=1).completed_at)
        reader.return_value = ([], "panel_unreachable")
        collect_activity()
        self.assertEqual(set(PurchaseActivity.objects.values_list("reason", flat=True)), {"panel_unreachable"})

    def test_fenced_lease_prevents_overlap_and_expired_owner_writes(self):
        now = timezone.now()
        token = _claim(now)
        self.assertTrue(token)
        self.assertIsNone(_claim(now))
        other = _claim(now + LEASE + timedelta(seconds=1))
        self.assertNotEqual(token, other)
        with self.assertRaises(RuntimeError):
            _heartbeat(token)

    def test_heartbeat_command_detects_missing_and_stale_scheduler(self):
        with self.assertRaises(CommandError):
            call_command("collect_customer_activity", check_running=True, stdout=StringIO())
        ActivityCollector.objects.create(pk=1, heartbeat_at=timezone.now(), completed_at=timezone.now())
        call_command("collect_customer_activity", check_running=True, stdout=StringIO())
        ActivityCollector.objects.update(heartbeat_at=timezone.now() - timedelta(hours=1))
        with self.assertRaises(CommandError):
            call_command("collect_customer_activity", check_running=True, stdout=StringIO())

    @patch("customer_activity.panels.XUIService")
    def test_adapter_uses_client_quota_and_scoped_inbound_without_online_calls(self, service_cls):
        order = self.purchase()
        vpn = self.vpn(order)
        service = service_cls.return_value
        service.get_inbound.return_value = {"settings": {"clients": [{"id": vpn.uuid, "email": vpn.xui_email, "enable": True}]},
                                           "clientStats": [{"email": vpn.xui_email, "up": 0, "down": 12}], "totalGB": 999999}
        rows, status = read_panel(self.panel)
        self.assertEqual(status, "ok")
        self.assertEqual(rows[0]["used_bytes"], 12)
        self.assertIsNone(rows[0]["total_bytes"])
        service.login.assert_called_once()
        service.get_online_clients.assert_not_called()
        self.assertEqual(rows[0]["node_id"], "node-1")

    @patch("customer_activity.panels.XUIService")
    def test_unsupported_panel_is_not_queried(self, service):
        self.panel.family = Panel.Family.MARZBAN
        self.assertEqual(read_panel(self.panel), ([], "unsupported_panel"))
        service.assert_not_called()

    def pasar_user(self, username="buyer", **overrides):
        data = {"id": 1, "username": username, "status": "active", "used_traffic": 100,
                "data_limit": 10000, "expire": (self.now + timedelta(days=30)).isoformat(),
                "proxy_settings": {"vless": {"id": "real-remote-uuid"}, "trojan": {"password": "remote-password"}}}
        data.update(overrides)
        return data

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_uses_batched_pagination_and_hashed_account_identity(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        client_cls.return_value.request.side_effect = [
            {"total": 2, "users": [self.pasar_user()]},
            {"total": 2, "users": [self.pasar_user("other", id=2)]},
        ]
        diagnostic = {}
        rows, status = read_panel(self.panel, diagnostics=diagnostic)
        self.assertEqual(status, "ok")
        self.assertEqual(len(rows), 2)
        self.assertEqual(diagnostic["pages"], 2)
        self.assertIn("offset=1", client_cls.return_value.request.call_args.args[1])
        self.assertIn(hash_xui_identifier("real-remote-uuid"), rows[0]["aliases"])
        self.assertEqual(rows[0]["used_bytes"], 100)
        self.assertEqual(rows[0]["node_id"], "pasarguard")

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_native_links_and_synthetic_uuid_deduplicate_one_account(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.save(update_fields=["family"])
        order = self.purchase()
        vpn = self.vpn(order, uuid="synthetic-local-uuid", username="local-label", xui_email="buyer", xui_node_id="",
                       xui_raw={"family": "pasarguard", "username": "buyer", "remote_user_id": 1})
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer, vpn_client=vpn)
        for i, raw in enumerate(["vless://real-remote-uuid@a.example:443", "trojan://remote-password@b.example:443"]):
            link = ConfigLink.objects.create(raw_link=raw, normalized_hash=f"native-{i}", vpn_client=vpn,
                                             source_panel=self.panel, source_inbound=self.inbound)
            CupItem.objects.create(cup=cup, config_link=link)
        client_cls.return_value.request.return_value = {"total": 1, "users": [self.pasar_user()]}
        rows, _ = read_panel(self.panel)
        maps, orphans = load_purchase_maps()
        matched, reason = resolve_maps(maps, orphans, rows)[order.pk]
        self.assertEqual(reason, "")
        self.assertEqual(len(matched), 1)
        state = record_purchase(maps[0], matched, reason, now=timezone.now())
        self.assertEqual(state.reason, "baseline")
        self.assertEqual(state.source_count, 1)

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_does_not_accept_a_different_users_credential(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.save(update_fields=["family"])
        order = self.purchase()
        vpn = self.vpn(order, xui_email="buyer", xui_raw={"username": "buyer"})
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer)
        link = ConfigLink.objects.create(raw_link="vless://foreign-id@example.com:443", normalized_hash="foreign", vpn_client=vpn)
        CupItem.objects.create(cup=cup, config_link=link)
        client_cls.return_value.request.return_value = {"total": 2, "users": [self.pasar_user(), self.pasar_user("foreign", id=2, proxy_settings={"vless": {"id": "foreign-id"}})]}
        rows, _ = read_panel(self.panel)
        maps, orphans = load_purchase_maps()
        matched, reason = resolve_maps(maps, orphans, rows)[order.pk]
        self.assertEqual(reason, "partial_coverage")
        self.assertEqual(len(matched), 1)
        self.assertIn(hash_xui_identifier("buyer"), next(iter(matched.values()))["aliases"])

    def test_partial_purchase_can_prove_activity_but_never_inactivity(self):
        order = self.purchase()
        vpn = self.vpn(order)
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer)
        link = ConfigLink.objects.create(raw_link="vless://unmapped@example.com:443", normalized_hash="unmapped")
        CupItem.objects.create(cup=cup, config_link=link)
        for minute in range(0, 49 * 60, 30):
            state = self.record(order, vpn, at=self.now + timedelta(minutes=minute))
            self.assertEqual(activity_status(state, now=state.observed_at)[0], "unknown")
            self.assertIsNone(state.continuous_since)
            self.assertEqual(state.entitlement, "unknown")
        later = state.observed_at + timedelta(minutes=15)
        state = self.record(order, vpn, used=25, at=later)
        self.assertEqual((activity_status(state, now=later)[0], state.reason), ("active", "partial_ok"))
        self.assertNotIn("at_risk", customer_rows(self.store, now=later)[0]["tags"])
        self.assertEqual(daily_counts(self.store, now=later)[-1]["active"], 1)
        link.is_active = False
        link.save(update_fields=["is_active"])
        state = self.record(order, vpn, used=25, at=later + timedelta(minutes=15))
        self.assertEqual(state.reason, "baseline")
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "collecting")

    def test_partial_attribution_excludes_shared_bytes_and_reports_only_counts(self):
        first, second = self.purchase(), self.purchase(customer=Customer.objects.create(display_name="Other"))
        dedicated, shared = self.vpn(first), self.vpn(first, uuid="shared-secret", xui_email="shared-user", username="shared-user")
        cup = SubscriptionCup.objects.create(order=second, customer=second.customer)
        link = ConfigLink.objects.create(raw_link="vless://shared-secret@example.com:443", normalized_hash="shared",
                                         source_panel=self.panel, source_inbound=self.inbound)
        CupItem.objects.create(cup=cup, config_link=link)
        maps, orphans = load_purchase_maps()
        diagnostics = {}
        resolved = resolve_maps(maps, orphans, [self.sample(dedicated, used=10), self.sample(shared, used=10000)], diagnostics=diagnostics)
        matched, reason = resolved[first.pk]
        self.assertEqual(reason, "partial_coverage")
        self.assertEqual(sum(row["used_bytes"] for row in matched.values()), 10)
        self.assertEqual(resolved[second.pk][1], "shared_identity")
        self.assertEqual(diagnostics["partial_purchases"], 1)
        self.assertEqual(diagnostics["source_outcomes"], {"matched": 3})
        self.assertNotIn("shared-secret", str(diagnostics))
        self.assertEqual(set(diagnostics), {"source_outcomes", "purchases_with_matches", "partial_purchases"})

    def test_partial_to_unknown_or_changed_sources_cannot_retain_usage(self):
        order = self.purchase()
        vpn = self.vpn(order)
        self.vpn(order, uuid="absent", username="absent", xui_email="absent")
        self.record(order, vpn)
        state = self.record(order, vpn, used=10, at=self.now + timedelta(minutes=15))
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "active")
        state = self.record(order, vpn, used=0, at=self.now + timedelta(minutes=30))
        self.assertEqual(state.reason, "partial_counter_reset")
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "unknown")
        state = self.record(order, vpn, at=self.now + timedelta(minutes=45), stats_available=False)
        self.assertEqual(activity_status(state, now=state.observed_at)[0], "unknown")
        self.assertFalse(state.counters)
        self.assertIsNone(state.last_activity_at)

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_partial_pagination_and_repeated_users_fail_closed(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        first = {"total": 2, "users": [self.pasar_user()]}
        for second in (ValueError("bad payload"), first):
            client_cls.return_value.request.side_effect = [first, second]
            rows, status = read_panel(self.panel)
            self.assertFalse(rows)
            self.assertEqual(status, "partial")

    def test_shadowsocks_native_credentials_preserve_encoded_password(self):
        def encoded(text):
            return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        password = "native:p@ss/%25+word"
        links = [
            f"ss://{encoded('chacha20-ietf-poly1305:' + password)}@example.com:443/?plugin=obfs#native",
            "ss://2022-blake3-aes-128-gcm:native%3Ap%40ss%2F%2525%2Bword@[::1]:443#native",
            f"ss://{encoded('chacha20-ietf-poly1305:' + password + '@example.com:443')}#legacy",
        ]
        for link in links:
            self.assertEqual(credential_from_link(link), password)
        for link in ("ss://not-base64!@example.com:443", "ss://YWJj@example.com:443", "ss://method:@example.com:443", "ss://method:password@example.com"):
            with self.assertRaises(ValueError):
                credential_from_link(link)

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_shadowsocks_native_link_completes_coverage_without_double_count(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.save(update_fields=["family"])
        order = self.purchase()
        vpn = self.vpn(order, uuid="synthetic-local", xui_email="buyer", xui_raw={"username": "buyer"})
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer, vpn_client=vpn)
        for index, raw in enumerate(("vless://real-remote-uuid@example.com:443", "ss://YWVzLTEyOC1nY206c3MtcGFzcw@example.com:443")):
            link = ConfigLink.objects.create(raw_link=raw, normalized_hash=f"native-ss-{index}", vpn_client=vpn,
                                             source_panel=self.panel, source_inbound=self.inbound)
            CupItem.objects.create(cup=cup, config_link=link)
        user = self.pasar_user()
        user["proxy_settings"]["shadowsocks"] = {"password": "ss-pass"}
        client_cls.return_value.request.return_value = {"total": 1, "users": [user]}
        rows, _ = read_panel(self.panel)
        maps, orphans = load_purchase_maps()
        matched, reason = resolve_maps(maps, orphans, rows)[order.pk]
        self.assertEqual(reason, "")
        self.assertEqual(len(matched), 1)
        state = record_purchase(maps[0], matched, reason, now=timezone.now())
        self.assertEqual(state.reason, "baseline")
        self.assertEqual(state.entitlement, "valid")

    @patch("customer_activity.panels.PasarGuardClient")
    def test_shadowsocks_wrong_owner_remains_incomplete(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.save(update_fields=["family"])
        order = self.purchase()
        vpn = self.vpn(order, xui_email="buyer", xui_raw={"username": "buyer"})
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer)
        link = ConfigLink.objects.create(raw_link="ss://aes-128-gcm:other-secret@example.com:443", normalized_hash="foreign-ss", vpn_client=vpn)
        CupItem.objects.create(cup=cup, config_link=link)
        other = self.pasar_user("other", id=2, proxy_settings={"shadowsocks": {"password": "other-secret"}})
        client_cls.return_value.request.return_value = {"total": 2, "users": [self.pasar_user(), other]}
        rows, _ = read_panel(self.panel)
        maps, orphans = load_purchase_maps()
        matched, reason = resolve_maps(maps, orphans, rows)[order.pk]
        self.assertEqual(reason, "partial_coverage")
        self.assertEqual(len(matched), 1)
        self.assertNotIn(hash_xui_identifier("other-secret"), next(iter(matched.values()))["aliases"])

    @patch("customer_activity.panels.PasarGuardClient")
    def test_pasarguard_missing_usage_and_ended_states_are_not_active(self, client_cls):
        self.panel.family = Panel.Family.PASARGUARD
        client_cls.return_value.request.return_value = {"total": 1, "users": [self.pasar_user(used_traffic=None, status="limited")]}
        rows, _ = read_panel(self.panel)
        self.assertFalse(rows[0]["stats_available"])
        self.assertFalse(rows[0]["enabled"])

    @patch("customer_activity.panels.XUIService")
    def test_read_errors_are_counted_without_exposing_sensitive_messages(self, service_cls):
        service_cls.return_value.get_inbound.side_effect = ValueError("HTTP 404 token=secret-token")
        details = {}
        rows, status = read_panel(self.panel, diagnostics=details)
        self.assertEqual(status, "partial")
        self.assertEqual(details["errors"], {"http_404": 1})
        self.assertNotIn("secret-token", str(details))

    @patch("customer_activity.panels.XUIService")
    def test_adapter_rejects_malformed_or_partial_traffic(self, service_cls):
        service = service_cls.return_value
        for traffic in ({"up": "invalid", "down": "bad"}, {"up": 0}):
            service.get_inbound.return_value = {"settings": {"clients": [{"id": "abc", "email": "abc", "enable": True}]},
                                               "clientStats": [{"email": "abc", **traffic}]}
            rows, _ = read_panel(self.panel)
            self.assertFalse(rows[0]["stats_available"])

    def test_dashboard_requires_capability_is_read_only_and_does_not_call_panels(self):
        url = reverse("admin_store_customer_activity")
        self.assertEqual(self.client.get(url).status_code, 302)
        staff = get_user_model().objects.create_user("staff", password="secret", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        order = self.purchase()
        vpn = self.vpn(order)
        self.record(order, vpn)
        with patch("customer_activity.services.read_panel") as reader:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "فعالیت واقعی مشتری‌ها")
            self.assertNotContains(response, vpn.uuid)
            self.assertNotContains(response, self.panel.url)
            self.assertEqual(self.client.post(url).status_code, 405)
            reader.assert_not_called()

    def test_store_filter_does_not_mix_customer_metrics(self):
        order = self.purchase()
        self.record(order, self.vpn(order))
        other = Store.objects.create(name="Other", card_number="0000000000000000", card_owner="Other")
        self.assertEqual(customer_rows(other, now=self.now), [])
        self.assertEqual(daily_counts(other, now=self.now)[-1]["active"], 0)
