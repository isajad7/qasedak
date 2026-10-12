import copy
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BotConfiguration, BotUser, ConfigAllocation, ConfigInventoryAsset,
    ConfigInventoryPool, CupFillerRule, CupFulfillmentRecipe, CupItem, Customer, Inbound, Order, Panel, Plan,
    PlanDeliveryConfig, PlanDeliverySource, Store, VPNClient,
)
from .order_recovery import can_retry_order, resend_order_result
from .provisioning_services import approve_and_provision_order, order_identity
from .telegram_bot.client import BotDeliveryError
from .xui_api import XUIError, XUIService


class OrderRecoveryTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Recovery", slug="recovery")
        self.plan = Plan.objects.create(
            store=self.store, name="1GB", slug="1gb", volume_gb=Decimal("1"),
            duration_days=30, price=100000, device_limit=2,
        )
        self.customer = Customer.objects.create(display_name="Buyer")
        self.panel = Panel.objects.create(
            store=self.store, name="Panel", url="https://panel.example.com", username="admin",
            password="test", capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel, inbound_id=1, server_ip="vpn.example.com", port="443",
        )
        self.config = BotConfiguration.objects.create(
            store=self.store, provider=BotConfiguration.Provider.TELEGRAM,
            name="Bot", bot_token="test-token", admin_user_id="42",
        )
        self.bot_user = BotUser.objects.create(
            bot_config=self.config, customer=self.customer, provider_user_id="123", chat_id="123",
        )
        self.order = Order.objects.create(
            store=self.store, plan=self.plan, customer=self.customer, inbound=self.inbound,
            amount=100000, is_paid=True, status=Order.Status.CONFIRMED,
            verification_status=Order.VerificationStatus.VERIFIED,
            metadata={"bot": {"bot_user_id": self.bot_user.pk}, "source": "recovery-test"},
        )

    def completed_order(self):
        self.order.status = Order.Status.COMPLETED
        self.order.provisioning_status = Order.ProvisioningStatus.PROVISIONED
        self.order.sub_link = "https://vpn.example.com/sub/test"
        self.order.save()
        return self.order

    def remote_result(self, identity=None):
        identity = identity or order_identity(self.order, self.inbound)
        return {
            "uuid": identity["uuid"], "email": identity["email"], "sub_id": identity["sub_id"],
            "sub_link": "https://vpn.example.com/sub/test", "direct_link": "",
            "raw": {"id": identity["uuid"], "email": identity["email"], "enable": True},
        }

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
    @patch("store.provisioning_services.approve_and_provision_order")
    def test_resend_only_sends_to_owner_and_preserves_completed_order(self, provision, send, sync):
        order = self.completed_order()
        other = Customer.objects.create(display_name="Other")
        BotUser.objects.create(bot_config=self.config, customer=other, provider_user_id="456", chat_id="456")
        result = resend_order_result(order)
        self.assertTrue(result.success)
        self.assertEqual(send.call_args.kwargs["chat_id"], "123")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.metadata["source"], "recovery-test")
        self.assertEqual(order.metadata["customer_notification"]["status"], "sent")
        self.assertTrue(order.metadata["customer_notification"]["attempted_at"])
        provision.assert_not_called()

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message")
    def test_failed_notification_can_be_resent_without_rolling_back_service(self, send, sync):
        order = self.completed_order()
        send.side_effect = BotDeliveryError("Telegram unavailable")
        self.assertFalse(resend_order_result(order).success)
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.metadata["customer_notification"]["status"], "failed")
        self.assertIn("unavailable", order.metadata["customer_notification"]["error"])
        send.side_effect = None
        send.return_value = 1
        self.assertTrue(resend_order_result(order).success)
        self.assertEqual(order.metadata["customer_notification"]["attempts"], 2)

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
    def test_explicit_resend_overrides_suppression_and_reports_no_target(self, send, sync):
        order = self.completed_order()
        order.metadata["suppress_customer_notification"] = True
        order.save()
        self.assertTrue(resend_order_result(order).success)
        self.bot_user.is_active = False
        self.bot_user.save()
        self.assertFalse(resend_order_result(order).success)
        self.assertEqual(order.metadata["customer_notification"]["status"], "no_target")

    def test_unfinished_order_cannot_use_resend(self):
        with patch("store.telegram_bot.notifications.notify_order_event") as send:
            self.assertFalse(resend_order_result(self.order).success)
            send.assert_not_called()
        self.assertTrue(can_retry_order(self.order))

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_remote_create_timeout_reuses_same_identity_on_retry(self, lookup, create):
        remote = {}

        def find(*args, **kwargs):
            return remote.get("client")

        def timed_out(**kwargs):
            remote["client"] = self.remote_result()
            return None

        lookup.side_effect = find
        create.side_effect = timed_out
        self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
        self.assertTrue(approve_and_provision_order(self.order, notify=False).ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.vpn_clients.count(), 1)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(self.order.status, Order.Status.COMPLETED)
        self.assertEqual(self.order.metadata["source"], "recovery-test")

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client", side_effect=XUIError("unreachable"))
    def test_unknown_panel_result_does_not_create_blindly(self, lookup, create):
        self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
        create.assert_not_called()

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", side_effect=BotDeliveryError("offline"))
    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_successful_provisioning_reports_message_failure_separately(self, lookup, create, send, sync):
        remote = self.remote_result()
        lookup.side_effect = [None, remote]
        create.return_value = remote
        result = approve_and_provision_order(self.order)
        self.order.refresh_from_db()
        self.assertTrue(result.ok)
        self.assertIn("پیام مشتری ارسال نشد", result.message)
        self.assertEqual(self.order.status, Order.Status.COMPLETED)
        self.assertEqual(self.order.metadata["customer_notification"]["status"], "failed")

    def test_partial_inventory_delivery_reuses_successful_allocations(self):
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan, delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
            failure_policy=PlanDeliveryConfig.FailurePolicy.STRICT,
        )
        pools = [ConfigInventoryPool.objects.create(title=f"Pool {i}") for i in range(2)]
        for i, pool in enumerate(pools):
            PlanDeliverySource.objects.create(
                delivery_config=config, source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
                inventory_pool=pool, quantity=1, priority=i,
            )
        def asset(pool, suffix):
            return ConfigInventoryAsset.objects.create(
                pool=pool, raw_link=f"vless://aaaaaaaa-aaaa-4aaa-8aaa-{suffix:012d}@vpn.example.com:443#Test",
            )
        first = asset(pools[0], 1)
        self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
        self.assertEqual(ConfigAllocation.objects.filter(order=self.order).count(), 1)
        asset(pools[1], 2)
        self.assertTrue(approve_and_provision_order(self.order, notify=False).ok)
        self.assertEqual(ConfigAllocation.objects.filter(order=self.order).count(), 2)
        self.assertEqual(ConfigAllocation.objects.filter(order=self.order, asset=first).count(), 1)

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
    def test_completed_partial_recipe_can_finish_missing_source_without_reallocating(self, send, sync):
        from .order_recovery import retry_order_completion

        recipe = CupFulfillmentRecipe.objects.create(
            plan=self.plan, title="Two pools", failure_policy=CupFulfillmentRecipe.FailurePolicy.PARTIAL_ALLOWED,
        )
        pools = [ConfigInventoryPool.objects.create(title=f"Pool {i}") for i in range(2)]
        for i, pool in enumerate(pools):
            CupFillerRule.objects.create(
                recipe=recipe, source_type=CupFillerRule.SourceType.INVENTORY_POOL,
                inventory_pool=pool, quantity=1, position=i+1,
            )
        for i in range(2):
            if i:
                self.assertTrue(can_retry_order(self.order))
            ConfigInventoryAsset.objects.create(
                pool=pools[i], raw_link=f"vless://aaaaaaaa-aaaa-4aaa-8aaa-{i+1:012d}@vpn.example.com:443#Test",
            )
            if not i:
                self.assertTrue(approve_and_provision_order(self.order, notify=False).ok)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, Order.Status.COMPLETED)
                self.assertEqual(self.order.metadata["cup_fulfillment_recipe"]["status"], "partial")
                original_token = self.order.subscription_cups.get().token
            else:
                self.assertTrue(retry_order_completion(self.order).ok)
                self.order.refresh_from_db()
                self.assertFalse(can_retry_order(self.order))
                self.assertEqual(self.order.subscription_cups.get().token, original_token)
        self.assertEqual(ConfigAllocation.objects.filter(order=self.order).count(), 2)
        self.assertEqual(CupItem.objects.filter(cup__order=self.order).count(), 2)
        send.assert_called_once()

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_partial_multi_inbound_builds_only_missing_remote_client(self, lookup, create):
        from .plan_delivery_execution import _execute_multi_panel_group
        from .provisioning_services import multi_inbound_order_identity

        self.panel.capability_profile = Panel.CapabilityProfile.MODERN_MULTI_NODE
        self.panel.save()
        second = Inbound.objects.create(panel=self.panel, inbound_id=2, server_ip="vpn2.example.com", port="443")
        config = PlanDeliveryConfig.objects.create(plan=self.plan, delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS)
        sources = [PlanDeliverySource.objects.create(
            delivery_config=config, source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=self.panel, inbound=inbound,
        ) for inbound in [self.inbound, second]]
        identity = multi_inbound_order_identity(self.order, [self.inbound, second])
        first_result = self.remote_result(identity)
        first_result["direct_link"] = f"vless://{identity['uuid']}@vpn.example.com:443#First"
        remote = {self.inbound.pk: first_result}
        lookup.side_effect = lambda panel, inbound, identity: remote.get(inbound.pk)

        def create_missing(**kwargs):
            result = {**self.remote_result(identity), "direct_link": f"vless://{identity['uuid']}@vpn2.example.com:443#Second"}
            remote[kwargs["inbound"].pk] = result
            return result

        create.side_effect = create_missing
        result = _execute_multi_panel_group(self.order, sources)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs["inbound"], second)
        self.assertEqual(len(result.raw_links), 2)

    def test_legacy_activation_response_timeout_does_not_reset_expiry(self):
        service, remote = self.renewal_setup()
        vpn_client = VPNClient.objects.get(pk=self.order.metadata["renewal_client_pk"])
        service._post_client_update = Mock()
        original_expiry = remote["expiryTime"]
        self.assertTrue(service.update_client_enabled(vpn_client))
        service._post_client_update.assert_not_called()
        self.assertEqual(remote["expiryTime"], original_expiry)

    def renewal_setup(self):
        original = Order.objects.create(store=self.store, plan=self.plan, customer=self.customer, amount=100000)
        vpn_client = VPNClient.objects.create(
            order=original, store=self.store, plan=self.plan, inbound=self.inbound,
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-000000000001", xui_email="buyer", username="buyer",
            status=VPNClient.Status.ACTIVE,
        )
        self.order.metadata["renewal_client_pk"] = vpn_client.pk
        self.order.save()
        remote = {
            "id": str(vpn_client.uuid), "email": "buyer", "enable": True,
            "totalGB": 1024, "limitIp": 1,
            "expiryTime": int((timezone.now() + timedelta(days=3)).timestamp() * 1000),
        }
        service = XUIService(self.panel)
        service._assert_precise_scope = Mock()
        service.get_inbound = Mock(side_effect=lambda *a, **k: {"settings": json.dumps({"clients": [remote]})})
        service._post_client_reset_traffic = Mock()
        return service, remote

    def test_renewal_update_timeout_does_not_extend_twice(self):
        service, remote = self.renewal_setup()
        previous_expiry = remote["expiryTime"]

        def timeout_after_write(inbound, uuid, payload, **kwargs):
            remote.update(copy.deepcopy(payload))
            raise XUIError("timeout after update")

        service._post_client_update = Mock(side_effect=timeout_after_write)
        with patch("store.xui_api.XUIService", return_value=service):
            self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
            self.assertTrue(approve_and_provision_order(self.order, notify=False).ok)
            self.assertTrue(approve_and_provision_order(self.order, notify=False).already_provisioned)
        self.assertEqual(remote["expiryTime"], previous_expiry + 30 * 86400000)
        self.assertEqual(service._post_client_update.call_count, 1)
        self.assertEqual(service._post_client_reset_traffic.call_count, 1)

    def test_ambiguous_renewal_reset_is_blocked_without_repeating_it(self):
        service, remote = self.renewal_setup()
        service._post_client_update = Mock(side_effect=lambda inbound, uuid, payload, **k: remote.update(payload))
        service._post_client_reset_traffic.side_effect = XUIError("reset timeout")
        with patch("store.xui_api.XUIService", return_value=service):
            self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
            self.assertFalse(approve_and_provision_order(self.order, notify=False).ok)
        self.assertEqual(service._post_client_update.call_count, 1)
        self.assertEqual(service._post_client_reset_traffic.call_count, 1)

    def test_interrupted_renewal_cannot_be_repeated_blindly(self):
        self.renewal_setup()
        with patch("store.provisioning_services._approve_and_provision_order", side_effect=RuntimeError("worker stopped")):
            with self.assertRaises(RuntimeError):
                approve_and_provision_order(self.order, notify=False)
        self.order.refresh_from_db()
        self.assertEqual(self.order.metadata["renewal_recovery_attempt"]["status"], "running")
        with patch("store.order_actions.renew_client") as renew:
            result = approve_and_provision_order(self.order, notify=False)
            self.assertFalse(result.ok)
            self.assertIn("نامشخص", result.message)
            renew.assert_not_called()

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
    def test_admin_panel_resend_button_and_action(self, send, sync):
        order = self.completed_order()
        admin = get_user_model().objects.create_superuser(username="admin", password="test")
        self.client.force_login(admin)
        url = reverse("admin_store_order_review", args=[order.pk])
        self.assertContains(self.client.get(url), "ارسال مجدد پیام و لینک سرویس")
        response = self.client.post(url, {"action": "resend_result", "confirm_external": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send.call_args.kwargs["chat_id"], "123")

    @patch("store.order_recovery.retry_order_completion")
    def test_panel_retry_available_without_local_client(self, activate):
        activate.return_value = Mock(success=True, message="done")
        admin = get_user_model().objects.create_superuser(username="admin", password="test")
        self.client.force_login(admin)
        url = reverse("admin_store_order_review", args=[self.order.pk])
        self.assertContains(self.client.get(url), "تلاش مجدد تکمیل سفارش")
        self.client.post(url, {"action": "retry_delivery", "confirm_external": "1"})
        activate.assert_called_once()

    @patch("store.telegram_bot.notifications.sync_admin_order_messages")
    @patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
    @patch("store.bots.BotClient")
    def test_bot_resend_callback_and_state_specific_buttons(self, client, send, sync):
        from .bots import handle_callback_update, order_admin_keyboard, parse_callback_data

        retry_keyboard = str(order_admin_keyboard(self.order))
        self.assertIn("order:retry:", retry_keyboard)
        order = self.completed_order()
        self.assertIn("order:resend:", str(order_admin_keyboard(order)))
        self.assertNotIn("order:retry:", str(order_admin_keyboard(order)))
        data = f"order:resend:{order.order_tracking_code}"
        self.assertEqual(parse_callback_data(data), ("resend", order.order_tracking_code))
        result = handle_callback_update(self.config, {"id": "cb", "data": data, "from": {"id": 42}}, chat_id="42")
        self.assertTrue(result["success"])
        self.assertEqual(send.call_args.kwargs["chat_id"], "123")
