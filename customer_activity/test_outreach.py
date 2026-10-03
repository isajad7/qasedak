from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from store.models import BotConfiguration, BotUser, Order, RevenueOfferLog, SupportConversation, SupportMessage
from store.telegram_bot.client import BotDeliveryError
from store.telegram_bot.support_flow import create_support_ticket_from_bot
from store import bots
from . import tests as activity_tests
from .bot_flow import handle_callback
from .models import ActivityCollector, OutreachEvent, OutreachPreference, OutreachSettings, PurchaseActivity
from .outreach import candidate_kind, dispatch, keyboard, prepare_store, reserve, run_outreach


class OutreachTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        activity_tests.ActivityTests.setUpTestData.__func__(cls)

    purchase = activity_tests.ActivityTests.purchase
    vpn = activity_tests.ActivityTests.vpn
    sample = activity_tests.ActivityTests.sample
    record = activity_tests.ActivityTests.record

    def setUp(self):
        self.now = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        self.clock = patch("customer_activity.outreach.timezone.now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.config = OutreachSettings.objects.create(store=self.store, mode="preview")
        self.bot = BotConfiguration.objects.create(store=self.store, name="Journeys", provider="telegram", bot_token="test-token", is_active=True)
        self.bot_user = BotUser.objects.create(bot_config=self.bot, customer=self.customer, provider_user_id="112233", chat_id="112233", is_active=True)
        self.transport = Mock()
        self.transport.return_value.send_message.return_value = {"ok": True, "result": {"message_id": 42}}

    def eligible(self, *, customer=None, used=8100, expiry=None):
        order = self.purchase(customer=customer or self.customer)
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=5), updated_at=self.now - timedelta(days=5))
        order.refresh_from_db()
        vpn = self.vpn(order)
        activity = PurchaseActivity.objects.create(order=order, observed_at=self.now, entitlement="valid", reason="ok",
            continuous_since=self.now - timedelta(hours=50), counters={"dedicated": {
                "used": used, "up": 0, "down": used, "quota": 10000, "at": self.now.isoformat(),
                "expiry": (expiry or self.now + timedelta(days=10)).isoformat(),
            }})
        # Consumption exists recently, unless a test explicitly selects the inactivity journey.
        activity.last_activity_start = self.now - timedelta(hours=1)
        activity.last_activity_at = self.now - timedelta(minutes=45)
        activity.save()
        return order, vpn, activity

    def queue(self, *, live=True, **kwargs):
        order, vpn, activity = self.eligible(**kwargs)
        self.config.mode = "live" if live else "preview"
        self.config.save()
        prepare_store(self.config, self.now)
        return order, vpn, activity, OutreachEvent.objects.get(order=order)

    def test_preview_has_purchase_context_but_never_calls_transport(self):
        order, _, _ = self.eligible()
        with patch("customer_activity.outreach.BotClient") as transport:
            report = run_outreach(now=self.now)
            transport.assert_not_called()
        event = OutreachEvent.objects.get(order=order)
        self.assertEqual(event.status, "preview")
        self.assertIn(f"#{order.pk}", event.body)
        self.assertIn("تاریخ خرید", event.body)
        self.assertEqual(report[0]["eligible"], 1)
        self.assertFalse(RevenueOfferLog.objects.exists())

    def test_full_48_hour_inactivity_has_support_and_no_sales_button(self):
        _, _, activity = self.eligible()
        activity.last_activity_start = activity.last_activity_at = None
        activity.save()
        self.assertEqual(candidate_kind(activity, self.config, self.now), "inactive_48h")
        prepare_store(self.config, self.now)
        event = OutreachEvent.objects.get()
        self.assertIn("دست‌کم 2 روز", event.body)
        self.assertIn("journey:support:", str(keyboard(event)))
        self.assertNotIn("journey:renew:", str(keyboard(event)))
        activity.continuous_since = self.now - timedelta(hours=47)
        activity.save()
        self.assertEqual(candidate_kind(activity, self.config, self.now), "volume_20")

    def test_partial_stale_unverified_cancelled_or_unmapped_never_sends(self):
        order, _, activity = self.eligible()
        for reason in ("partial_ok", "baseline", "counter_reset", "unmapped_source", "shared_identity"):
            activity.reason = reason
            self.assertEqual(candidate_kind(activity, self.config, self.now), "")
        activity.reason = "ok"
        activity.observed_at = self.now - timedelta(hours=1)
        self.assertEqual(candidate_kind(activity, self.config, self.now), "")
        activity.observed_at = self.now
        order.status = "cancelled"
        self.assertEqual(candidate_kind(activity, self.config, self.now), "")

    def test_expiry_volume_and_ended_stages_use_fresh_remote_counters(self):
        _, _, activity = self.eligible(used=9700)
        self.assertEqual(candidate_kind(activity, self.config, self.now), "volume_5")
        activity.counters["dedicated"]["expiry"] = (self.now + timedelta(hours=20)).isoformat()
        self.assertEqual(candidate_kind(activity, self.config, self.now), "expiry_24h")
        activity.counters["dedicated"]["expiry"] = (self.now + timedelta(hours=60)).isoformat()
        activity.counters["dedicated"]["used"] = 100
        self.assertEqual(candidate_kind(activity, self.config, self.now), "expiry_72h")
        activity.entitlement = "expired"
        activity.ended_at = self.now - timedelta(hours=1)
        self.assertEqual(candidate_kind(activity, self.config, self.now), "ended")
        activity.ended_at = self.now - timedelta(days=5)
        self.assertEqual(candidate_kind(activity, self.config, self.now), "")

    def test_send_is_once_per_purchase_cycle_and_updates_legacy_caps(self):
        _, _, _, event = self.queue()
        self.assertTrue(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.transport.return_value.send_message.assert_called_once()
        self.assertEqual(self.transport.return_value.send_message.call_args.kwargs["chat_id"], "112233")
        event.refresh_from_db()
        self.assertEqual((event.status, event.message_id), ("sent", "42"))
        self.assertEqual(RevenueOfferLog.objects.get().event_type, "purchase_journey")
        prepare_store(self.config, self.now)
        self.assertEqual(OutreachEvent.objects.count(), 1)

    def test_multiple_purchases_do_not_send_multiple_daily_messages(self):
        _, _, _, first = self.queue()
        _, _, _, second = self.queue()
        self.assertTrue(dispatch(first.pk, now=self.now, client_class=self.transport))
        self.assertFalse(dispatch(second.pk, now=self.now, client_class=self.transport))
        second.refresh_from_db()
        self.assertEqual(second.reason, "customer_cap")
        self.assertEqual(OutreachEvent.objects.count(), 2)

    def test_reservation_counts_towards_cap_before_http_and_stops_other_runner(self):
        _, _, _, first = self.queue()
        _, _, _, second = self.queue()
        self.assertIsNotNone(reserve(first.pk, self.now))
        self.assertIsNone(reserve(first.pk, self.now))
        self.assertIsNone(reserve(second.pk, self.now))

    def test_muted_open_support_quiet_hours_and_disabled_block_send(self):
        order, _, _, event = self.queue()
        pref = OutreachPreference.objects.create(store=self.store, customer=self.customer, muted=True)
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        pref.delete()
        ticket = SupportConversation.objects.create(store=self.store, customer=self.customer, status="waiting_admin")
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        ticket.delete()
        self.assertFalse(dispatch(event.pk, now=self.now.replace(hour=22), client_class=self.transport))
        self.config.mode = "off"
        self.config.save()
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.transport.assert_not_called()

    def test_renewal_after_queue_is_revalidated_and_converted_order_recorded(self):
        order, vpn, _, event = self.queue()
        renewal = self.purchase(metadata={"renewal_client_pk": str(vpn.pk)})
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        prepare_store(self.config, self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, "cancelled")
        self.assertEqual(event.converted_order_id, renewal.pk)

    def test_unrelated_new_purchase_does_not_suppress_this_purchase(self):
        _, _, _, event = self.queue()
        self.purchase()
        self.assertTrue(dispatch(event.pk, now=self.now, client_class=self.transport))

    def test_pending_receipt_renewal_suppresses_and_rejection_restores_eligibility(self):
        order, vpn, _, event = self.queue()
        renewal = self.purchase(status="pending_verification", verification_status="pending", payment_submitted_at=self.now,
                                metadata={"renewal_client_pk": vpn.pk})
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        renewal.status = "cancelled"
        renewal.save()
        prepare_store(self.config, self.now)
        self.assertTrue(dispatch(event.pk, now=self.now, client_class=self.transport))

    def test_changed_cycle_or_cancelled_order_after_queue_cancels_send(self):
        order, _, activity, event = self.queue()
        activity.cycle += 1
        activity.save()
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        prepare_store(self.config, self.now)
        new_event = OutreachEvent.objects.get(order=order, cycle=2)
        Order.objects.filter(pk=order.pk).update(status="cancelled")
        self.assertFalse(dispatch(new_event.pk, now=self.now, client_class=self.transport))

    def test_timeout_or_missing_ack_is_uncertain_and_never_retried(self):
        _, _, _, event = self.queue()
        self.transport.return_value.send_message.side_effect = BotDeliveryError("timeout with secret-token")
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        event.refresh_from_db()
        self.assertEqual((event.status, event.reason), ("uncertain", "transport_unknown"))
        self.assertNotIn("secret-token", event.reason)
        prepare_store(self.config, self.now)
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.transport.return_value.send_message.assert_called_once()

    def test_explicit_telegram_rejection_is_distinct_from_timeout(self):
        _, _, _, event = self.queue()
        self.transport.return_value.send_message.side_effect = BotDeliveryError("blocked", error_code=403)
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")

    def test_crashed_sender_is_marked_uncertain_without_resend(self):
        _, _, _, event = self.queue()
        OutreachEvent.objects.filter(pk=event.pk).update(status="sending", claimed_at=self.now - timedelta(minutes=11))
        with patch("customer_activity.outreach.dispatch") as sender:
            run_outreach(now=self.now)
            sender.assert_not_called()
        event.refresh_from_db()
        self.assertEqual((event.status, event.reason), ("uncertain", "worker_interrupted"))

    def test_wrong_store_or_group_chat_cannot_be_selected(self):
        _, _, _, event = self.queue()
        self.bot_user.chat_id = "-100112233"
        self.bot_user.save()
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.transport.assert_not_called()

    def sent_event(self):
        order, vpn, activity, event = self.queue()
        dispatch(event.pk, now=self.now, client_class=self.transport)
        event.refresh_from_db()
        return event, vpn

    def test_support_callback_binds_ticket_to_purchase_and_preserves_message(self):
        event, _ = self.sent_event()
        client = Mock()
        handle_callback(client, self.bot, self.bot_user, f"journey:support:{event.pk}", chat_id="112233", start_renewal=Mock())
        self.bot_user.refresh_from_db()
        self.assertEqual(self.bot_user.state_data["journey_event_id"], event.pk)
        notify = Mock()
        result = create_support_ticket_from_bot(self.bot, self.bot_user, "وصل نمی‌شود", {}, chat_id="112233",
            client_cls=Mock(return_value=client), notify_support_message_func=notify, get_message_id_func=lambda value: "73")
        self.assertTrue(result["success"])
        message = SupportMessage.objects.get()
        self.assertEqual(message.metadata["purchase_id"], event.order_id)
        self.assertEqual(message.body, "وصل نمی‌شود")
        event.refresh_from_db()
        self.assertEqual(event.support_conversation_id, message.conversation_id)
        notify.assert_called_once()

    def test_foreign_callback_and_forged_support_state_do_not_leak_or_create_ticket(self):
        event, _ = self.sent_event()
        stranger = BotUser.objects.create(bot_config=self.bot, customer=None, provider_user_id="999", chat_id="999")
        client = Mock()
        result = handle_callback(client, self.bot, stranger, f"journey:support:{event.pk}", chat_id="999", start_renewal=Mock())
        self.assertFalse(result["success"])
        stranger.state_data = {"journey_event_id": event.pk}
        result = create_support_ticket_from_bot(self.bot, stranger, "text", {}, chat_id="999",
            client_cls=Mock(return_value=client), notify_support_message_func=Mock(), get_message_id_func=lambda value: "1")
        self.assertFalse(result["success"])
        self.assertFalse(SupportConversation.objects.exists())

    def test_renew_button_uses_exact_purchase_and_mute_is_reversible(self):
        event, vpn = self.sent_event()
        client, renew = Mock(), Mock(return_value={"ok": True})
        handle_callback(client, self.bot, self.bot_user, f"journey:renew:{event.pk}", chat_id="112233", start_renewal=renew)
        self.assertEqual(renew.call_args.args[3], str(vpn.public_id))
        for action, expected in (("mute", True), ("resume", False)):
            handle_callback(client, self.bot, self.bot_user, f"journey:{action}:{event.pk}", chat_id="112233", start_renewal=renew)
            self.assertEqual(OutreachPreference.objects.get().muted, expected)

    def test_dashboard_permissions_validation_and_no_sending_in_request(self):
        url = reverse("admin_store_customer_outreach")
        self.assertEqual(self.client.get(url).status_code, 302)
        staff = get_user_model().objects.create_user("outreach-staff", password="test", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        with patch("customer_activity.outreach.dispatch") as sender:
            self.assertEqual(self.client.get(url).status_code, 200)
            invalid = {"store": self.store.pk, "mode": "live", "start_hour": 22, "end_hour": 9, "daily_limit": 50}
            response = self.client.post(url, invalid)
            self.assertEqual(response.status_code, 200)
            self.config.refresh_from_db()
            self.assertEqual(self.config.mode, "preview")
            valid = {**invalid, "start_hour": 9, "end_hour": 21, "inactivity_enabled": "on", "renewal_enabled": "on"}
            self.assertEqual(self.client.post(url, valid).status_code, 302)
            self.config.refresh_from_db()
            self.assertEqual(self.config.mode, "live")
            self.assertEqual(self.config.changed_by_id, self.admin.pk)
            sender.assert_not_called()

    def test_read_only_status_command_and_mode_changes_are_explicit(self):
        out = StringIO()
        with patch("customer_activity.management.commands.run_customer_outreach.run_outreach") as runner:
            call_command("run_customer_outreach", status=True, stdout=out)
            runner.assert_not_called()
        self.assertNotIn("test-token", out.getvalue())
        call_command("run_customer_outreach", store=self.store.pk, set_mode="off", stdout=StringIO())
        self.config.refresh_from_db()
        self.assertEqual(self.config.mode, "off")

    def test_real_callback_router_preserves_message_and_support_context(self):
        event, _ = self.sent_event()
        callback = {"id": "callback", "data": f"journey:support:{event.pk}",
                    "message": {"message_id": 42, "chat": {"id": 112233}}}
        with patch("store.bots.BotClient") as client, patch("store.bots.delete_callback_message") as delete:
            result = bots.handle_user_callback(self.bot, self.bot_user, callback, chat_id="112233")
            self.assertTrue(result["handled"])
            delete.assert_not_called()
            client.return_value.answer_callback.assert_called_once()
        self.bot_user.refresh_from_db()
        self.assertEqual(self.bot_user.state_data["journey_event_id"], event.pk)

    def test_missing_ack_and_late_mute_cannot_trigger_repeated_delivery(self):
        order, _, _, event = self.queue()
        original = reserve
        def reserve_then_mute(*args, **kwargs):
            value = original(*args, **kwargs)
            OutreachPreference.objects.create(store=self.store, customer=self.customer, muted=True)
            return value
        with patch("customer_activity.outreach.reserve", side_effect=reserve_then_mute):
            self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        self.transport.assert_not_called()
        OutreachPreference.objects.all().delete()
        prepare_store(self.config, self.now)
        self.transport.return_value.send_message.return_value = {"ok": True, "result": {}}
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        event.refresh_from_db()
        self.assertEqual(event.status, "uncertain")

    def test_weekly_personal_and_store_caps_are_enforced(self):
        order, _, _, event = self.queue()
        for days in (2, 3, 4):
            OutreachEvent.objects.create(order=order, cycle=1, kind=f"past-{days}", status="sent", claimed_at=self.now - timedelta(days=days))
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        event.refresh_from_db()
        self.assertEqual(event.reason, "customer_cap")
        OutreachEvent.objects.exclude(pk=event.pk).delete()
        other = activity_tests.Customer.objects.create(display_name="Other")
        other_order = self.purchase(customer=other)
        OutreachEvent.objects.create(order=other_order, cycle=1, kind="volume_20", status="uncertain", claimed_at=self.now)
        self.config.daily_limit = 1
        self.config.save()
        self.assertFalse(dispatch(event.pk, now=self.now, client_class=self.transport))
        event.refresh_from_db()
        self.assertEqual(event.reason, "store_cap")
        self.transport.assert_not_called()

    def test_heartbeat_requires_recent_outreach_pass(self):
        with self.assertRaises(CommandError):
            call_command("run_customer_outreach", check_running=True, stdout=StringIO())
        run_outreach(now=self.now)
        call_command("run_customer_outreach", check_running=True, stdout=StringIO())
        OutreachSettings.objects.update(last_run_at=self.now - timedelta(hours=1))
        with self.assertRaises(CommandError):
            call_command("run_customer_outreach", check_running=True, stdout=StringIO())

    def test_initial_activation_requires_completed_preview_and_scoped_recipient(self):
        def activate():
            call_command("run_customer_outreach", store=self.store.pk, activate_initial=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            activate()
        ActivityCollector.objects.create(heartbeat_at=self.now, completed_at=self.now)
        run_outreach(now=self.now)
        with self.assertRaises(CommandError):
            activate()
        self.eligible()
        with patch("customer_activity.outreach.dispatch") as sender:
            activate()
            sender.assert_not_called()
        self.config.refresh_from_db()
        self.assertEqual((self.config.mode, self.config.activated_at), ("live", self.now))

    def test_initial_activation_preserves_operator_settings_and_previous_activation(self):
        for values in ({"mode": "off"}, {"mode": "preview", "changed_by": self.admin},
                       {"mode": "preview", "changed_by": None, "activated_at": self.now}):
            OutreachSettings.objects.filter(pk=self.config.pk).update(**values)
            call_command("run_customer_outreach", store=self.store.pk, activate_initial=True, stdout=StringIO())
            self.config.refresh_from_db()
            self.assertEqual(self.config.mode, values["mode"])

    def test_single_store_activation_rejects_ambiguous_tenant(self):
        call_command("run_customer_outreach", store=self.store.pk, set_mode="off", stdout=StringIO())
        call_command("run_customer_outreach", only_active_store=True, activate_initial=True, stdout=StringIO())
        activity_tests.Store.objects.create(name="Another active store", is_active=True)
        with self.assertRaises(CommandError):
            call_command("run_customer_outreach", only_active_store=True, activate_initial=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("run_customer_outreach", only_active_store=True, store=self.store.pk, activate_initial=True, stdout=StringIO())
