from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .customer_assignment import (assign_customer_service, assignment_assets, ensure_target_customer,
                                  search_bot_users, target_orders)
from .customer_delivery import resolve_customer_order_delivery
from .models import (BotConfiguration, BotUser, ConfigAllocation, ConfigInventoryAsset, ConfigInventoryPool,
                     Customer, Order, Plan, PlanDeliveryConfig, PlanDeliverySource, Store, SubscriptionCup)
from .order_recovery import resend_order_result, retry_order_completion
from .order_revenue import order_revenue_amount
from .telegram_bot import admin_users


class CustomerAssignmentTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Assignment", slug="assignment")
        self.plan = Plan.objects.create(store=self.store, name="Basic", slug="basic", volume_gb=5,
                                       duration_days=30, price=100000)
        self.config = BotConfiguration.objects.create(store=self.store, name="Bot", bot_token="test-token", admin_user_id="999")
        self.other_config = BotConfiguration.objects.create(store=self.store, name="Other bot", bot_token="other-token")
        self.customer = Customer.objects.create(display_name="Same name")
        self.target = BotUser.objects.create(bot_config=self.config, customer=self.customer, display_name="Same name",
                                             first_name="Ali", username="buyer", provider_user_id="42", chat_id="42")
        self.other_target = BotUser.objects.create(bot_config=self.other_config, customer=self.customer,
                                                   display_name="Same name", username="buyer", provider_user_id="42", chat_id="42")
        self.admin = BotUser.objects.create(bot_config=self.config, provider_user_id="999", chat_id="999")
        self.web_admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "test")
        self.pool = ConfigInventoryPool.objects.create(title="Inventory", connected_plan=self.plan)
        self.asset = ConfigInventoryAsset.objects.create(pool=self.pool,
            raw_link="vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000001@vpn.example.com:443#Test")
        delivery = PlanDeliveryConfig.objects.create(plan=self.plan, delivery_mode="SUBSCRIPTION")
        PlanDeliverySource.objects.create(delivery_config=delivery, source_type="INVENTORY_POOL", inventory_pool=self.pool)
        self.send_patch = patch("store.telegram_bot.notifications._send_customer_order_event_message", return_value=1)
        self.send = self.send_patch.start()
        self.addCleanup(self.send_patch.stop)

    def assign(self, **overrides):
        data = dict(customer=self.customer, target=self.target, plan=self.plan, mode="existing", asset=self.asset,
                    included=True, amount=65000, request_id=uuid4().hex, actor_key="test:admin")
        data.update(overrides)
        return assign_customer_service(**data)

    def test_search_is_scoped_includes_users_without_purchase_and_all_identifiers(self):
        for term in ["42", "@buyer", "Same", "Ali"]:
            self.assertEqual(list(search_bot_users(self.config, term)), [self.target])
        self.assertFalse(Order.objects.exists())

    def test_missing_customer_does_not_adopt_another_customer_by_username(self):
        unlinked = BotUser.objects.create(bot_config=self.config, provider_user_id="99", chat_id="99", username="buyer")
        ensure_target_customer(unlinked)
        self.assertNotEqual(unlinked.customer_id, self.customer.pk)
        original = unlinked.customer_id
        ensure_target_customer(unlinked)
        self.assertEqual(unlinked.customer_id, original)

    def test_inventory_assignment_records_owner_order_cup_allocation_and_revenue_without_panel_calls(self):
        with patch("store.order_services.grant_free_subscription") as grant, patch("store.order_actions.activate_order") as activate:
            result = self.assign()
        self.assertTrue(result.success, result.message)
        grant.assert_not_called()
        activate.assert_not_called()
        order = result.order
        self.assertEqual(order.customer, self.customer)
        cup = SubscriptionCup.objects.get(order=order)
        self.assertEqual(cup.customer, self.customer)
        allocation = ConfigAllocation.objects.get(order=order)
        self.assertEqual(allocation.cup, cup)
        self.assertEqual(cup.items.get().config_link.raw_link, self.asset.raw_link)
        self.assertTrue(resolve_customer_order_delivery(order).is_ready)
        self.assertEqual(order_revenue_amount(order), 65000)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0].config.pk, self.config.pk)
        self.assertEqual(self.send.call_args.kwargs["chat_id"], "42")

    def test_new_service_uses_delivery_engine_and_selected_customer(self):
        result = self.assign(mode="new", asset=None, included=False, amount=None)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.order.customer, self.customer)
        self.assertEqual(SubscriptionCup.objects.get(order=result.order).customer, self.customer)
        self.assertEqual(order_revenue_amount(result.order), 0)

    def test_double_confirm_never_reallocates_or_sends_again(self):
        for mode in ["existing", "new"]:
            self.asset.status = ConfigInventoryAsset.Status.AVAILABLE
            self.asset.current_allocations = 0
            self.asset.save()
            token = uuid4().hex
            first = self.assign(mode=mode, request_id=token)
            count = ConfigAllocation.objects.count()
            sent = self.send.call_count
            second = self.assign(mode=mode, request_id=token)
            self.assertEqual(first.order.pk, second.order.pk)
            self.assertTrue(second.duplicate_detected)
            self.assertEqual(ConfigAllocation.objects.count(), count)
            self.assertEqual(self.send.call_count, sent)

    def test_exclusive_owned_asset_cannot_transfer_and_failure_is_retryable(self):
        first = self.assign()
        other = Customer.objects.create(display_name="Other")
        target = BotUser.objects.create(bot_config=self.config, customer=other, provider_user_id="43", chat_id="43")
        second = self.assign(customer=other, target=target)
        self.assertFalse(second.success)
        self.assertEqual(second.order.provisioning_status, Order.ProvisioningStatus.FAILED)
        self.assertEqual(ConfigAllocation.objects.count(), 1)
        self.assertEqual(ConfigAllocation.objects.get().order_id, first.order.pk)
        self.assertFalse(retry_order_completion(second.order).success)
        self.assertEqual(ConfigAllocation.objects.count(), 1)

    def test_shared_limited_capacity_and_unlimited_reuse(self):
        self.pool.allocation_mode = "shared_limited"
        self.pool.max_allocations_per_asset = 2
        self.pool.save()
        self.assertTrue(self.assign().success)
        self.assertTrue(self.assign().success)
        self.assertFalse(self.assign().success)
        self.pool.allocation_mode = "shared_unlimited"
        self.pool.save()
        self.assertTrue(self.assign().success)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_allocations, 3)

    def test_delivery_failure_preserves_service_and_resend_only_sends(self):
        self.send.side_effect = RuntimeError("Telegram unavailable")
        result = self.assign()
        self.assertTrue(result.success)
        self.assertEqual(result.order.status, Order.Status.COMPLETED)
        self.assertEqual(result.order.metadata["customer_notification"]["status"], "failed")
        self.send.side_effect = None
        with patch("store.order_actions.activate_order") as activate:
            self.assertTrue(resend_order_result(result.order, target_bot_user=self.target).success)
            activate.assert_not_called()
        self.assertEqual(ConfigAllocation.objects.count(), 1)

    def test_new_service_failure_keeps_same_order_and_retry_completes(self):
        self.asset.status = "disabled"
        self.asset.save()
        result = self.assign(mode="new")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.order)
        self.asset.status = "available"
        self.asset.save()
        self.assertTrue(retry_order_completion(result.order).success)
        result.order.refresh_from_db()
        self.assertEqual(result.order.status, Order.Status.COMPLETED)
        self.assertEqual(Order.objects.count(), 1)

    def test_legacy_panel_failure_reserves_order_before_remote_create(self):
        from .models import Inbound, Panel

        PlanDeliveryConfig.objects.filter(plan=self.plan).delete()
        panel = Panel.objects.create(store=self.store, name="Legacy", url="https://panel.example.com",
                                     username="test", password="test", capability_profile="legacy_single_node")
        inbound = Inbound.objects.create(panel=panel, inbound_id=1, port=443, server_ip="vpn.example.com")
        token = uuid4().hex
        with patch("store.order_services.select_inbound_for_plan", return_value=inbound), \
                patch("store.order_services.validate_inbound_for_order", return_value=inbound), \
                patch("store.provisioning_services.lookup_existing_remote_client", return_value=None), \
                patch("store.xui_api.create_inactive_client_details", return_value=None) as create:
            result = self.assign(mode="new", request_id=token)
            self.assertFalse(result.success)
            self.assertIsNotNone(result.order)
            self.assertEqual(result.order.provisioning_status, Order.ProvisioningStatus.FAILED)
            self.assertEqual(create.call_count, 1)
            repeated = self.assign(mode="new", request_id=token)
            self.assertEqual(repeated.order.pk, result.order.pk)
            self.assertEqual(create.call_count, 1)

    def test_inactive_target_never_falls_back_to_another_bot_user(self):
        BotUser.objects.create(bot_config=self.config, customer=self.customer, provider_user_id="43", chat_id="43")
        self.target.is_active = False
        self.target.save()
        result = self.assign()
        self.assertTrue(result.success)
        self.send.assert_not_called()
        self.assertEqual(result.order.metadata["customer_notification"]["status"], "no_target")

    def test_invalid_store_owner_actor_and_asset_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.assign(customer=Customer.objects.create(display_name="Wrong"))
        result = self.assign()
        with self.assertRaises(ValidationError):
            self.assign(request_id=result.order.public_id, actor_key="other")
        other_plan = Plan.objects.create(store=self.store, name="Other", slug="other", volume_gb=1, duration_days=1, price=1)
        self.assertFalse(assignment_assets(other_plan).exists())
        with self.assertRaises(ValidationError):
            self.assign(plan=other_plan)

    def test_services_menu_includes_order_only_services_and_checks_owner(self):
        from .telegram_bot.services_flow import active_subscription_lines, subscription_management_keyboard, send_order_service
        order = self.assign().order
        self.assertIn(order.order_tracking_code, active_subscription_lines(self.target))
        self.assertIn(f"user:order_service:{order.public_id}", str(subscription_management_keyboard(self.target)))
        client = Mock()
        self.assertTrue(send_order_service(client, self.target, str(order.public_id), chat_id="42")["handled"])
        other = BotUser.objects.create(bot_config=self.config, provider_user_id="444", chat_id="444")
        self.assertFalse(send_order_service(client, other, str(order.public_id), chat_id="444")["success"])

    def test_panel_preview_confirm_and_duplicate_post(self):
        self.client.force_login(self.web_admin)
        url = reverse("admin_store_customer_review", args=[self.customer.pk]) + f"?bot_user={self.target.pk}"
        data = {"action": "grant_service", "mode": "existing", "bot_user": self.target.pk, "plan": self.plan.pk,
                "asset": self.asset.pk, "revenue_included": "yes", "revenue_amount": "۶۵۰۰۰"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())
        self.assertContains(response, "پیش‌نمایش اختصاص")
        confirmation = response.context["assignment_confirmation"]
        self.assertTrue(confirmation, response.context["grant_form"].errors)
        response = self.client.post(url, {"action": "confirm_grant", "confirmation": confirmation})
        self.assertEqual(response.status_code, 302)
        self.client.post(url, {"action": "confirm_grant", "confirmation": confirmation})
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(ConfigAllocation.objects.count(), 1)
        self.assertEqual(Order.objects.get().revenue_amount, 65000)
        response = self.client.get(url)
        self.assertContains(response, "ارسال مجدد سرویس")
        self.assertContains(response, self.target.provider_user_id)
        self.client.post(url, {"action": "resend_service", "order_id": Order.objects.get().pk})
        self.assertEqual(self.send.call_count, 2)

    def test_panel_unsigned_confirmation_or_permission_does_not_create(self):
        self.client.force_login(self.web_admin)
        url = reverse("admin_store_customer_review", args=[self.customer.pk])
        response = self.client.post(url, {"action": "confirm_grant", "confirmation": "tampered"})
        self.assertContains(response, "تأیید معتبر نیست")
        self.assertFalse(Order.objects.exists())
        staff = get_user_model().objects.create_user("staff", password="test", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post(url, {"action": "grant_service"}).status_code, 403)

    def test_botuser_panel_links_and_missing_customer_post(self):
        self.client.force_login(self.web_admin)
        response = self.client.get(reverse("admin:store_botuser_changelist"), {"q": "@buyer"})
        self.assertContains(response, "پرونده · اختصاص سرویس")
        url = reverse("admin:store_botuser_customer", args=[self.admin.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.admin.refresh_from_db()
        self.assertIsNone(self.admin.customer_id)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.admin.refresh_from_db()
        self.assertIsNotNone(self.admin.customer_id)

    def callback(self, data):
        self.admin.refresh_from_db()
        return admin_users.handle_callback(self.bot_client, self.config, self.admin, "admin:users:" + data, chat_id="999")

    def test_bot_wizard_search_finance_confirm_and_old_button(self):
        self.bot_client = Mock()
        self.callback("search")
        admin_users.handle_text(self.bot_client, self.config, self.admin, "@buyer", chat_id="999")
        self.assertIn("Same name", self.bot_client.send_message.call_args.args[0])
        self.callback(f"view:{self.target.pk}")
        self.callback(f"plans:existing:{self.target.pk}:1")
        self.callback(f"plan:existing:{self.target.pk}:{self.plan.pk}")
        self.callback(f"asset:{self.target.pk}:{self.plan.pk}:{self.asset.pk}")
        token = self.admin.state_data["request_id"]
        self.callback(f"yes:{token}")
        admin_users.handle_text(self.bot_client, self.config, self.admin, "۶۵۰۰۰", chat_id="999")
        self.assertFalse(Order.objects.exists())
        self.assertIn("65,000", self.bot_client.send_message.call_args.args[0])
        self.callback(f"confirm:{token}")
        self.callback(f"confirm:{token}")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(ConfigAllocation.objects.count(), 1)
        self.assertEqual(Order.objects.get().customer_id, self.customer.pk)
        self.assertIn("پیام مشتری", self.bot_client.send_message.call_args.args[0])

    def test_bot_new_service_no_revenue_and_resend(self):
        self.bot_client = Mock()
        self.callback(f"plan:new:{self.target.pk}:{self.plan.pk}")
        token = self.admin.state_data["request_id"]
        self.callback(f"no:{token}")
        self.callback(f"confirm:{token}")
        order = Order.objects.get()
        self.assertEqual(order_revenue_amount(order), 0)
        self.callback(f"services:{self.target.pk}:1")
        self.callback(f"resend:{self.target.pk}:{order.pk}")
        self.assertEqual(self.send.call_count, 2)

    def test_bot_unauthorized_foreign_target_and_pagination(self):
        from . import bots
        from .telegram_bot.user_menu import main_menu_keyboard
        self.assertIn("admin:users:list:1", str(main_menu_keyboard(is_admin=True)))
        self.assertNotIn("admin:users:", str(main_menu_keyboard()))
        self.assertTrue(bots.is_admin_menu_callback_data("admin:users:list:1"))
        result = admin_users.handle_callback(Mock(), self.config, self.target, "admin:users:list:1", chat_id="42")
        self.assertTrue(result["permission_denied"])
        self.bot_client = Mock()
        self.callback(f"view:{self.other_target.pk}")
        self.assertIn("معتبر نیست", self.bot_client.send_message.call_args.args[0])
        for number in range(10):
            BotUser.objects.create(bot_config=self.config, provider_user_id=str(1000+number), chat_id=str(1000+number))
        self.callback("list:1")
        self.assertIn("بعدی", str(self.bot_client.send_message.call_args.kwargs))
        self.callback("page:2")
        self.assertIn("صفحه 2", self.bot_client.send_message.call_args.args[0])

    def test_telegram_facade_routes_directory_and_search_messages(self):
        from . import bots
        update = {"callback_query": {"id": "test", "from": {"id": 999}, "message": {"chat": {"id": 999}},
                                     "data": "admin:users:search"}}
        with patch("store.bots.BotClient", return_value=Mock()):
            result = bots.handle_bot_update(self.config.provider, self.config.webhook_secret, update)
            self.assertTrue(result["handled"])
            result = bots.handle_bot_update(self.config.provider, self.config.webhook_secret,
                {"message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "buyer"}})
            self.assertTrue(result["handled"])
