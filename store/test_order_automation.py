from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BotConfiguration, BotUser, Customer, Inbound, Order, OrderAutomation, Panel, Plan, Store, SubscriptionCup, VPNClient
from .order_automation import auto_approve_order, cancel_auto_approved_order, confirm_auto_payment, process_order_automation, send_cancellation_notice, send_receipt_reminders


class OrderAutomationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("automation-owner", "owner@example.com", "secret")
        cls.store = Store.objects.create(name="Automation", card_number="0000000000000000", card_owner="Owner")
        cls.plan = Plan.objects.create(store=cls.store, name="10 GB", volume_gb=Decimal("10"), price=100000, duration_days=30)
        cls.customer = Customer.objects.create(display_name="Buyer")
        cls.panel = Panel.objects.create(store=cls.store, name="Panel", url="https://panel.example.com", username="test", password="test")
        cls.inbound = Inbound.objects.create(panel=cls.panel, inbound_id=1, xui_node_id="node-7", server_ip="example.com", port="443")
        cls.bot = BotConfiguration.objects.create(store=cls.store, provider=BotConfiguration.Provider.TELEGRAM, name="Bot", bot_token="test-token", admin_user_id="123", is_active=True)

    def setUp(self):
        self.now = timezone.now()

    def receipt(self, **overrides):
        data = dict(store=self.store, customer=self.customer, plan=self.plan, inbound=self.inbound,
                    status=Order.Status.PENDING_VERIFICATION, payment_submitted_at=self.now - timedelta(minutes=6),
                    payment_receipt_image="payment_receipts/test.png", amount=100000)
        data.update(overrides)
        return Order.objects.create(**data)

    def enable(self):
        Store.objects.filter(pk=self.store.pk).update(order_auto_approve_enabled=True, order_auto_approve_enabled_at=self.now - timedelta(hours=1))

    def service(self, order, **overrides):
        data = dict(store=self.store, order=order, plan=self.plan, inbound=self.inbound,
                    username=f"client-{order.pk}", uuid=f"uuid-{order.pk}", xui_email=f"client-{order.pk}",
                    xui_node_id="node-7", status=VPNClient.Status.ACTIVE)
        data.update(overrides)
        return VPNClient.objects.create(**data)

    def approved(self, **overrides):
        overrides.setdefault("status", Order.Status.COMPLETED)
        overrides.setdefault("verification_status", Order.VerificationStatus.VERIFIED)
        overrides.setdefault("verified_at", self.now)
        order = self.receipt(**overrides)
        OrderAutomation.objects.create(order=order, auto_approved_at=self.now, review_status="pending")
        return order

    def telegram_customer(self):
        return BotUser.objects.create(bot_config=self.bot, customer=self.customer, provider_user_id="42", chat_id="42")

    def test_defaults_and_enable_time_are_preserved_on_other_changes(self):
        self.assertFalse(self.store.order_auto_approve_enabled)
        self.store.order_auto_approve_enabled = True
        self.store.save(update_fields=["order_auto_approve_enabled"])
        started = self.store.order_auto_approve_enabled_at
        self.assertIsNotNone(started)
        self.store.name = "New name"
        self.store.save()
        self.assertEqual(self.store.order_auto_approve_enabled_at, started)

    @patch("store.telegram_bot.notifications.notify_order_event")
    @patch("store.provisioning_services.approve_and_provision_order", return_value=SimpleNamespace(ok=True))
    def test_exact_five_minute_boundary_and_no_repeat(self, provision, notify):
        self.enable()
        order = self.receipt(payment_submitted_at=self.now)
        self.assertFalse(auto_approve_order(order.pk, now=self.now + timedelta(minutes=5) - timedelta(seconds=1)))
        self.assertTrue(auto_approve_order(order.pk, now=self.now + timedelta(minutes=5)))
        self.assertFalse(auto_approve_order(order.pk, now=self.now + timedelta(hours=1)))
        provision.assert_called_once()
        self.assertEqual(OrderAutomation.objects.get(order=order).review_status, "pending")

    @patch("store.provisioning_services.approve_and_provision_order")
    def test_disabled_missing_receipt_other_payment_and_closed_orders_are_excluded(self, provision):
        order = self.receipt()
        self.assertFalse(auto_approve_order(order.pk, now=self.now))
        self.enable()
        for kwargs in [dict(payment_receipt_image=""), dict(status=Order.Status.CANCELLED),
                       dict(status=Order.Status.REJECTED), dict(payment_method=Order.PaymentMethod.ADMIN_FREE),
                       dict(verification_status=Order.VerificationStatus.VERIFIED)]:
            candidate = self.receipt(**kwargs)
            self.assertFalse(auto_approve_order(candidate.pk, now=self.now))
        provision.assert_not_called()

    @patch("store.telegram_bot.notifications.notify_order_event")
    @patch("store.provisioning_services.approve_and_provision_order", return_value=SimpleNamespace(ok=True))
    def test_empty_receipt_metadata_is_excluded_but_bot_file_is_eligible(self, provision, notify):
        self.enable()
        for metadata in ({"receipt": {}}, {"receipt": {"file_id": None}}, {"receipt": {"file_id": ""}}, {"receipt_text": None}):
            order = self.receipt(payment_receipt_image="", metadata=metadata)
            self.assertFalse(auto_approve_order(order.pk, now=self.now))
        order = self.receipt(payment_receipt_image="", metadata={"receipt": {"file_id": "uploaded-file"}})
        self.assertTrue(auto_approve_order(order.pk, now=self.now))
        provision.assert_called_once()

    @patch("store.provisioning_services.approve_and_provision_order")
    def test_existing_receipts_get_grace_period_after_enabling(self, provision):
        self.store.order_auto_approve_enabled = True
        self.store.save()
        order = self.receipt(payment_submitted_at=self.now - timedelta(days=2))
        self.assertFalse(auto_approve_order(order.pk, now=self.now + timedelta(minutes=4)))
        provision.assert_not_called()

    @patch("store.provisioning_services.approve_and_provision_order", side_effect=TimeoutError("uncertain remote result"))
    def test_uncertain_provisioning_is_not_automatically_repeated(self, provision):
        self.enable()
        order = self.receipt()
        self.assertTrue(auto_approve_order(order.pk, now=self.now))
        self.assertFalse(auto_approve_order(order.pk, now=self.now + timedelta(hours=1)))
        provision.assert_called_once()
        self.assertTrue(OrderAutomation.objects.get(order=order).last_error)

    @patch("store.telegram_bot.notifications.notify_order_event", return_value=1)
    @patch("store.subscription_cups.rebuild_subscription_cups_for_order", return_value=[])
    @patch("store.order_actions.enable_client", return_value=True)
    def test_real_purchase_pipeline_activates_once(self, enable_client, rebuild, notify):
        self.enable()
        order = self.receipt()
        vpn = self.service(order, status=VPNClient.Status.INACTIVE)
        self.assertTrue(auto_approve_order(order.pk, now=self.now))
        order.refresh_from_db()
        vpn.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(vpn.status, VPNClient.Status.ACTIVE)
        self.assertFalse(auto_approve_order(order.pk, now=self.now + timedelta(minutes=1)))
        enable_client.assert_called_once()

    @patch("store.telegram_bot.notifications.notify_order_event", return_value=1)
    @patch("store.subscription_cups.rebuild_subscription_cups_for_order", return_value=[])
    @patch("store.order_actions.renew_client")
    def test_real_renewal_pipeline_renews_once(self, renew, rebuild, notify):
        self.enable()
        original = self.receipt(status=Order.Status.COMPLETED, verification_status=Order.VerificationStatus.VERIFIED)
        vpn = self.service(original)
        renewal = self.receipt(metadata={"renewal_client_pk": vpn.pk})
        renew.return_value = {"expiry_at": self.now + timedelta(days=30), "raw": {}}
        self.assertTrue(auto_approve_order(renewal.pk, now=self.now))
        renewal.refresh_from_db()
        self.assertEqual(renewal.status, Order.Status.COMPLETED)
        self.assertFalse(auto_approve_order(renewal.pk, now=self.now + timedelta(minutes=1)))
        renew.assert_called_once()

    @patch("store.telegram_bot.notifications.send_to_config", return_value=True)
    def test_reminders_are_batched_and_follow_5_15_30_then_hourly(self, send):
        order = self.receipt(payment_submitted_at=self.now)
        self.receipt(payment_submitted_at=self.now)
        for minutes, expected in [(4, 0), (5, 2), (6, 0), (15, 2), (30, 2), (31, 0), (90, 2)]:
            self.assertEqual(send_receipt_reminders(self.store.pk, now=self.now + timedelta(minutes=minutes)), expected)
        self.assertEqual(send.call_count, 4)
        buttons = send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["callback_data"], f"order:detail:{order.order_tracking_code}")

    @patch("store.telegram_bot.notifications.send_to_config", side_effect=[False, True])
    def test_failed_reminder_can_retry_and_closed_order_stops(self, send):
        order = self.receipt()
        self.assertEqual(send_receipt_reminders(self.store.pk, now=self.now), 0)
        self.assertEqual(send_receipt_reminders(self.store.pk, now=self.now), 1)
        order.status = Order.Status.REJECTED
        order.save()
        self.assertEqual(send_receipt_reminders(self.store.pk, now=self.now + timedelta(hours=2)), 0)

    @patch("store.bots.send_new_order_to_config", side_effect=[False, True])
    def test_initial_notification_failure_releases_claim(self, send):
        from .admin_notifications import notify_admins_new_order

        order = self.receipt()
        self.assertEqual(notify_admins_new_order(order), 0)
        order.refresh_from_db()
        self.assertIsNone(order.admin_notified_at)
        self.assertIsNone(order.admin_receipt_notified_at)
        self.assertEqual(notify_admins_new_order(order), 1)

    @patch("store.referral_services.create_referral_reward_for_order")
    @patch("store.provisioning_services.approve_and_provision_order")
    def test_review_does_not_repeat_provisioning(self, provision, reward):
        order = self.approved()
        confirm_auto_payment(order.pk, user=self.admin, note="Bank checked")
        confirm_auto_payment(order.pk, user=self.admin)
        provision.assert_not_called()
        reward.assert_called_once()
        audit = OrderAutomation.objects.get(order=order)
        self.assertEqual(audit.review_status, "verified")
        self.assertEqual(audit.reviewed_by, self.admin)

    @patch("store.vpn_client_management_services.set_vpn_client_enabled_by_admin")
    def test_cancel_suspends_exact_client_and_disables_cup_once(self, disable):
        order = self.approved()
        vpn = self.service(order)
        cup = SubscriptionCup.objects.create(order=order, customer=self.customer, vpn_client=vpn)
        self.assertTrue(cancel_auto_approved_order(order.pk, user=self.admin, reason="پرداخت یافت نشد"))
        self.assertTrue(cancel_auto_approved_order(order.pk, user=self.admin, reason="پرداخت یافت نشد"))
        order.refresh_from_db()
        cup.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertFalse(order.is_paid)
        self.assertFalse(cup.is_accessible)
        disable.assert_called_once()
        self.assertEqual(disable.call_args.args[1]["node_id"], "node-7")
        self.assertEqual(OrderAutomation.objects.get(order=order).cancellation_notification_status, "no_target")

    @patch("store.vpn_client_management_services.set_vpn_client_enabled_by_admin")
    def test_partial_cancel_retains_progress_and_can_retry(self, disable):
        order = self.approved()
        first = self.service(order)
        self.service(order, username="second", uuid="second", xui_email="second")
        cup = SubscriptionCup.objects.create(order=order)
        disable.side_effect = [None, RuntimeError("panel unreachable")]
        self.assertFalse(cancel_auto_approved_order(order.pk, user=self.admin, reason="not paid"))
        audit = OrderAutomation.objects.get(order=order)
        self.assertEqual(audit.review_status, "cancel_failed")
        self.assertEqual(len(audit.revoked_client_ids), 1)
        order.refresh_from_db()
        self.assertNotEqual(order.status, Order.Status.CANCELLED)
        cup.refresh_from_db()
        self.assertFalse(cup.is_accessible)
        disable.reset_mock()
        disable.side_effect = None
        self.assertTrue(cancel_auto_approved_order(order.pk, user=self.admin, reason="not paid"))
        disable.assert_called_once()

    @patch("store.vpn_client_management_services.set_vpn_client_enabled_by_admin")
    def test_renewal_cancellation_targets_existing_service(self, disable):
        original = self.receipt(status=Order.Status.COMPLETED, verification_status=Order.VerificationStatus.VERIFIED)
        vpn = self.service(original)
        original_cup = SubscriptionCup.objects.create(order=original, vpn_client=vpn)
        renewal = self.approved(metadata={"renewal_client_pk": vpn.pk})
        self.assertTrue(cancel_auto_approved_order(renewal.pk, user=self.admin, reason="not paid"))
        self.assertEqual(disable.call_args.args[1]["vpn_client_id"], vpn.pk)
        original.refresh_from_db()
        original_cup.refresh_from_db()
        self.assertEqual(original.status, Order.Status.COMPLETED)
        self.assertFalse(original_cup.is_accessible)

    @patch("store.vpn_client_management_services.set_vpn_client_enabled_by_admin")
    def test_later_paid_renewal_blocks_cancellation_of_its_service(self, disable):
        order = self.approved()
        vpn = self.service(order)
        self.receipt(status=Order.Status.COMPLETED, verified_at=self.now + timedelta(minutes=1), metadata={"renewal_client_pk": vpn.pk})
        with self.assertRaises(ValidationError):
            cancel_auto_approved_order(order.pk, user=self.admin, reason="not paid")
        disable.assert_not_called()

    @patch("store.vpn_client_management_services.set_vpn_client_enabled_by_admin")
    def test_reconciliation_preserves_activation_time_and_later_renewal_protection(self, disable):
        order = self.approved()
        vpn = self.service(order)
        self.receipt(status=Order.Status.COMPLETED, verified_at=self.now + timedelta(minutes=1), metadata={"renewal_client_pk": vpn.pk})
        with patch("store.order_automation.timezone.now", return_value=self.now + timedelta(minutes=2)):
            confirm_auto_payment(order.pk, user=self.admin)
        order.refresh_from_db()
        self.assertEqual(order.verified_at, self.now)
        with self.assertRaises(ValidationError):
            cancel_auto_approved_order(order.pk, user=self.admin, reason="not paid")
        disable.assert_not_called()

    @patch("store.telegram_bot.notifications.notify_order_event", side_effect=[0, 1])
    def test_customer_notice_retries_without_repeating_success(self, notify):
        self.telegram_customer()
        order = self.approved()
        self.assertTrue(cancel_auto_approved_order(order.pk, user=self.admin, reason="not paid"))
        self.assertFalse(send_cancellation_notice(order.pk, now=self.now + timedelta(minutes=1)))
        self.assertTrue(send_cancellation_notice(order.pk, now=self.now + timedelta(minutes=6)))
        self.assertFalse(send_cancellation_notice(order.pk, now=self.now + timedelta(hours=1)))
        self.assertEqual(notify.call_count, 2)

    def test_cancellation_message_uses_only_customers_telegram_in_the_same_store(self):
        from .telegram_bot.order_delivery import format_customer_order_event, notify_customer_order_event

        target = self.telegram_customer()
        other_store = Store.objects.create(name="Other store")
        other_bot = BotConfiguration.objects.create(store=other_store, provider=BotConfiguration.Provider.TELEGRAM, name="Other bot", bot_token="other")
        other_target = BotUser.objects.create(bot_config=other_bot, customer=self.customer, provider_user_id="43", chat_id="43")
        bale_bot = BotConfiguration.objects.create(store=self.store, provider=BotConfiguration.Provider.BALE, name="Bale", bot_token="bale")
        BotUser.objects.create(bot_config=bale_bot, customer=self.customer, provider_user_id="44", chat_id="44")
        order = self.approved(rejection_reason="رسید <اشتباه>", metadata={"suppress_customer_notification": True, "bot": {"bot_user_id": other_target.pk}})
        deliver = Mock()
        sent = notify_customer_order_event(order, event_type="cancelled", client_cls=Mock(), delivery_error_cls=RuntimeError,
                                          log_event_func=Mock(), send_customer_order_event_message_func=deliver)
        self.assertEqual(sent, 1)
        self.assertEqual(deliver.call_args.kwargs["chat_id"], target.chat_id)
        self.assertIn("&lt;اشتباه&gt;", format_customer_order_event(order, event_type="cancelled", format_order_message_func=Mock()))

    @patch("store.telegram_bot.notifications.notify_order_event")
    def test_panel_review_settings_and_reconciliation(self, notify):
        self.client.force_login(self.admin)
        order = self.approved()
        url = reverse("admin_store_order_workbench") + f"?store={self.store.pk}"
        response = self.client.get(url)
        self.assertContains(response, "بازبینی تأییدهای خودکار")
        self.assertContains(response, order.order_tracking_code)
        response = self.client.post(url, {"action": "save_order_automation", "order_auto_approve_enabled": "on", "order_review_reminders_enabled": "on"})
        self.assertEqual(response.status_code, 302)
        self.store.refresh_from_db()
        self.assertTrue(self.store.order_auto_approve_enabled)
        detail = reverse("admin_store_order_review", args=[order.pk])
        self.assertContains(self.client.get(detail), "لغو سفارش و اطلاع به مشتری")
        result = self.client.post(detail, {"action": "confirm_auto_payment", "confirm_external": "1"})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(OrderAutomation.objects.get(order=order).review_status, "verified")
        notify.assert_not_called()

    def test_read_only_staff_and_csrf_cannot_mutate(self):
        viewer = get_user_model().objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_order", content_type__app_label="store"))
        self.client.force_login(viewer)
        order = self.approved()
        detail = reverse("admin_store_order_review", args=[order.pk])
        for action in ("cancel_auto_approval", "confirm_auto_payment"):
            self.assertEqual(self.client.post(detail, {"action": action, "confirm_external": "1", "reason": "test"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin_store_order_workbench") + f"?store={self.store.pk}", {"action": "save_order_automation"}).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(detail, {"action": "cancel_auto_approval", "confirm_external": "1", "reason": "test"}).status_code, 403)
