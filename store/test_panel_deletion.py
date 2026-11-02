from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from store.admin import PanelAdmin
from store.free_trial_services import get_next_free_trial_available_at
from store.models import (
    ConfigLink, Customer, FreeTrialRequest, Inbound, Order, Panel,
    PanelClientUsageSnapshot, Plan, PlanInboundRoute, Store, SubscriptionCup, VPNClient,
)


class PanelDeletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("panel-operator", password="test-only")
        self.store = Store.objects.create(name="Store", english_name="Store", card_number="0", card_owner="Store")
        self.panel = Panel.objects.create(store=self.store, name="Old panel", url="https://panel.example")
        self.inbound = Inbound.objects.create(panel=self.panel, inbound_id=1, server_ip="127.0.0.1", port="443")
        self.plan = Plan.objects.create(
            store=self.store, name="Plan", slug="deletion-plan", volume_gb=1, duration_days=30, price=1000,
        )
        self.model_admin = PanelAdmin(Panel, admin.site)
        self.request = RequestFactory().get("/")
        self.request.user = self.user
        PanelClientUsageSnapshot.objects.bulk_create([
            PanelClientUsageSnapshot(
                panel=self.panel, inbound=self.inbound, captured_at=timezone.now(), client_identifier_hash=str(i),
            ) for i in range(250)
        ])

    def test_preview_counts_large_history_without_loading_monitoring_instances(self):
        with patch.object(PanelClientUsageSnapshot, "from_db", side_effect=AssertionError("Loaded history")):
            deleted, counts, permissions, protected = self.model_admin.get_deleted_objects([self.panel], self.request)
        self.assertEqual(counts[PanelClientUsageSnapshot._meta.verbose_name_plural], 250)
        self.assertLess(len(str(deleted)), 4000)
        self.assertFalse(permissions)
        self.assertFalse(protected)

    def test_actual_admin_confirmation_is_bounded(self):
        self.client.force_login(self.user)
        with patch.object(PanelClientUsageSnapshot, "from_db", side_effect=AssertionError("Loaded history")):
            response = self.client.get(reverse("admin:store_panel_delete", args=[self.panel.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 120000)
        self.assertTrue(Panel.objects.filter(pk=self.panel.pk).exists())

    def test_plan_route_protection_is_not_bypassed(self):
        route = PlanInboundRoute.objects.create(plan=self.plan, inbound=self.inbound)
        with patch.object(PanelClientUsageSnapshot, "from_db", side_effect=AssertionError("Loaded history")):
            _deleted, _counts, _permissions, protected = self.model_admin.get_deleted_objects([self.panel], self.request)
        self.assertTrue(protected)
        self.assertTrue(PlanInboundRoute.objects.filter(pk=route.pk).exists())

    def test_history_delete_permission_is_still_required(self):
        staff = get_user_model().objects.create_user("restricted-operator", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="store", codename__in=["delete_panel", "delete_inbound"],
        ))
        self.request.user = staff
        _deleted, _counts, permissions, _protected = self.model_admin.get_deleted_objects([self.panel], self.request)
        self.assertIn(PanelClientUsageSnapshot._meta.verbose_name, permissions)

    def test_bulk_preview_combines_monitoring_counts(self):
        other = Panel.objects.create(name="Other panel", url="https://other.example")
        PanelClientUsageSnapshot.objects.create(panel=other, captured_at=timezone.now(), client_identifier_hash="other")
        with patch.object(PanelClientUsageSnapshot, "from_db", side_effect=AssertionError("Loaded history")):
            _deleted, counts, _permissions, _protected = self.model_admin.get_deleted_objects(Panel.objects.all(), self.request)
        self.assertEqual(counts[PanelClientUsageSnapshot._meta.verbose_name_plural], 251)

    def test_delete_preserves_customer_services_links_and_trial_cooldown(self):
        customer = Customer.objects.create(display_name="Customer")
        order = Order.objects.create(store=self.store, customer=customer, plan=self.plan, inbound=self.inbound)
        vpn_client = VPNClient.objects.create(order=order, inbound=self.inbound, username="retained-service")
        link = ConfigLink.objects.create(
            raw_link="vless://test-only@example.invalid:443", source_panel=self.panel, source_inbound=self.inbound,
        )
        cup = SubscriptionCup.objects.create(customer=customer, order=order, vpn_client=vpn_client)
        trial = FreeTrialRequest.objects.create(
            customer=customer, panel=self.panel, inbound=self.inbound, vpn_client=vpn_client,
            status=FreeTrialRequest.Status.DELIVERED, traffic_gb=Decimal("1"), duration_hours=24,
        )
        original_token = cup.token
        available_at = get_next_free_trial_available_at(customer, store=self.store)
        self.client.force_login(self.user)
        with patch.object(PanelClientUsageSnapshot, "from_db", side_effect=AssertionError("Loaded history")):
            response = self.client.post(reverse("admin:store_panel_delete", args=[self.panel.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Panel.objects.filter(pk=self.panel.pk).exists())
        trial.refresh_from_db()
        order.refresh_from_db()
        vpn_client.refresh_from_db()
        link.refresh_from_db()
        cup.refresh_from_db()
        self.assertIsNone(trial.panel_id)
        self.assertIsNone(trial.inbound_id)
        self.assertIsNone(order.inbound_id)
        self.assertIsNone(vpn_client.inbound_id)
        self.assertIsNone(link.source_panel_id)
        self.assertEqual(link.raw_link, "vless://test-only@example.invalid:443")
        self.assertEqual(cup.token, original_token)
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())
        self.assertEqual(get_next_free_trial_available_at(customer, store=self.store), available_at)
        self.assertEqual(PanelClientUsageSnapshot.objects.count(), 0)
