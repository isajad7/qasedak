from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from .models import BotConfiguration, Customer, Inbound, Order, Panel, Plan, Store
from .order_revenue import RevenueSettingsForm, order_revenue_amount, revenue_amount_expression, update_order_revenue


class AdminOrderRevenueTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Revenue Store", english_name="Revenue Store", slug="revenue-store")
        self.plan = Plan.objects.create(store=self.store, name="Revenue Plan", slug="revenue-plan",
                                       price=100000, volume_gb=Decimal("5"), duration_days=30)
        self.customer = Customer.objects.create(display_name="Revenue Customer", username="revenue-customer")
        self.admin = get_user_model().objects.create_superuser("revenue-admin", "admin@example.com", "secret")
        self.config = BotConfiguration.objects.create(store=self.store, name="Revenue Bot", bot_token="test-token")

    def order(self, **kwargs):
        data = dict(store=self.store, plan=self.plan, customer=self.customer, amount=100000,
                    status=Order.Status.COMPLETED, is_paid=True,
                    verification_status=Order.VerificationStatus.VERIFIED)
        data.update(kwargs)
        return Order.objects.create(**data)

    def test_historical_admin_purchase_and_renewal_excluded_without_rewriting_orders(self):
        own = self.order(metadata={"admin_direct_purchase": True})
        renewal = self.order(metadata={"admin_direct_purchase": True, "renewal": True})
        source_only = self.order(metadata={"source": "admin_direct_purchase"})
        normal = self.order(verified_by=self.admin)
        total = Order.objects.aggregate(total=Sum(revenue_amount_expression()))["total"]
        self.assertEqual(total, normal.amount)
        for order in (own, renewal, source_only):
            self.assertEqual(order_revenue_amount(order), 0)
            self.assertEqual(order.amount, 100000)
            self.assertIsNone(order.revenue_included)
        self.assertEqual(Order.objects.count(), 4)

    def test_yes_override_no_and_old_gift_have_correct_amounts(self):
        grant = self.order(amount=0, discount_amount=100000, payment_method=Order.PaymentMethod.ADMIN_FREE,
                           revenue_included=True, revenue_amount=65000)
        excluded = self.order(revenue_included=False)
        old_gift = self.order(amount=0, discount_amount=100000, payment_method=Order.PaymentMethod.ADMIN_FREE)
        self.assertEqual(Order.objects.aggregate(total=Sum(revenue_amount_expression()))["total"], 65000)
        for order in (grant, excluded, old_gift):
            amount = Order.objects.filter(pk=order.pk).aggregate(total=Sum(revenue_amount_expression()))["total"]
            self.assertEqual(amount, order_revenue_amount(order))
        self.assertEqual(grant.amount, 0)

    def test_financial_choice_cannot_complete_pending_rejected_or_unpaid_orders(self):
        for kwargs in ({"status": Order.Status.PENDING_VERIFICATION}, {"status": Order.Status.CONFIRMED},
                       {"status": Order.Status.REJECTED}, {"is_paid": False}):
            order = self.order(revenue_included=True, revenue_amount=80000, **kwargs)
            self.assertEqual(order_revenue_amount(order), 0)
        self.assertEqual(Order.objects.aggregate(total=Sum(revenue_amount_expression()))["total"], 0)

    def test_finance_edit_bypasses_save_signals_and_all_delivery_actions(self):
        order = self.order(amount=0, discount_amount=100000)
        with patch.object(Order, "save") as save, patch("store.order_actions.activate_order") as activate, \
                patch("store.order_recovery.resend_order_result") as resend:
            update_order_revenue(order, included=True, amount=45000)
            update_order_revenue(order, included=False)
            save.assert_not_called()
            activate.assert_not_called()
            resend.assert_not_called()
        order.refresh_from_db()
        self.assertFalse(order.revenue_included)
        self.assertEqual(order.amount, 0)
        self.assertEqual(order.status, Order.Status.COMPLETED)
        with self.assertRaises(ValidationError):
            update_order_revenue(self.order(metadata={"admin_direct_purchase": True}), included=True, amount=1)

    def test_form_requires_explicit_choice_and_positive_amount_accepts_persian_digits(self):
        self.assertFalse(RevenueSettingsForm({"revenue_amount": "1000"}).is_valid())
        for amount in ("", "0", "-1", "10.5", "bad"):
            self.assertFalse(RevenueSettingsForm({"revenue_included": "yes", "revenue_amount": amount}).is_valid())
        yes = RevenueSettingsForm({"revenue_included": "yes", "revenue_amount": "۶۵٬۰۰۰"})
        self.assertTrue(yes.is_valid(), yes.errors)
        self.assertEqual(yes.cleaned_data["revenue_amount"], 65000)
        no = RevenueSettingsForm({"revenue_included": "no"})
        self.assertTrue(no.is_valid())
        self.assertIsNone(no.cleaned_data["revenue_amount"])

    def test_reports_customer_total_daily_series_and_csv_agree(self):
        from .admin_dashboard import get_revenue_metrics
        from .admin_reports_center import build_reports_csv, get_daily_series, get_sales_metrics, resolve_report_period
        from .customer_analytics import get_customer_stats
        from .daily_report_services import collect_daily_sales_stats
        from .telegram_bot.admin_reports import admin_sales_report_text
        from .telegram_bot.notifications import build_sales_report
        self.order(amount=100000, verified_by=self.admin)
        self.order(metadata={"admin_direct_purchase": True})
        self.order(metadata={"admin_direct_purchase": True, "renewal": True})
        self.order(amount=0, discount_amount=100000, revenue_included=True, revenue_amount=65000)
        self.order(revenue_included=False)
        self.order(status=Order.Status.PENDING_VERIFICATION, revenue_included=True, revenue_amount=900000)
        period = resolve_report_period({"range": "today"}, self.store)
        self.assertEqual(get_revenue_metrics(self.store)["today"]["by_currency"][Plan.Currency.TOMAN], 165000)
        self.assertEqual(get_sales_metrics(period, self.store)["revenue"], 165000)
        self.assertEqual(sum(get_daily_series(period, self.store)["revenue"]), 165000)
        self.assertEqual(get_customer_stats(self.customer)["total_paid_amount"], 165000)
        daily = collect_daily_sales_stats(period.start, period.end, store=self.store)
        self.assertEqual(daily["successful_amount"], 165000)
        self.assertEqual(daily["successful_count"], 5)
        text, _ = build_sales_report(self.config)
        self.assertIn("165,000", text)
        self.assertIn("۱۶۵,۰۰۰", admin_sales_report_text(self.config))
        self.assertIn("۱۶۵,۰۰۰", build_reports_csv(period, "sales", self.store)[1])
        self.assertEqual(self.customer.orders.count(), 6)

    def test_export_contains_effective_revenue(self):
        from .admin import OrderResource
        order = self.order(metadata={"admin_direct_purchase": True})
        data = OrderResource().export(Order.objects.filter(pk=order.pk))
        self.assertIn("reported_revenue", data.headers)
        self.assertEqual(data.dict[0]["reported_revenue"], 0)

    def test_order_review_edit_financials_only_and_shows_amount_currency(self):
        order = self.order(amount=0, discount_amount=100000)
        self.client.force_login(self.admin)
        url = reverse("admin_store_order_review", args=[order.pk])
        response = self.client.get(url)
        self.assertContains(response, "درآمد سفارش")
        self.assertContains(response, "ذخیره وضعیت درآمد")
        with patch("store.order_actions.activate_order") as activate, patch("store.order_recovery.resend_order_result") as resend:
            response = self.client.post(url, {"action": "update_revenue", "revenue_included": "yes", "revenue_amount": "۶۵۰۰۰"})
            self.assertEqual(response.status_code, 302)
            activate.assert_not_called()
            resend.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.revenue_amount, 65000)
        self.assertEqual(order.amount, 0)
        self.assertEqual(order.status, Order.Status.COMPLETED)
        response = self.client.get(url)
        self.assertContains(response, "65,000")
        self.assertEqual(response.context["revenue_currency"], order.get_currency_display())

    def test_customer_form_prompts_before_grant_and_passes_selected_amount(self):
        from .order_services import ProvisionedOrderResult
        self.client.force_login(self.admin)
        url = reverse("admin_store_customer_review", args=[self.customer.pk])
        response = self.client.get(url)
        self.assertContains(response, "این سرویس در درآمد حساب شود؟")
        self.assertContains(response, 'data-price="100000"')
        with patch("store.order_services.grant_free_subscription") as grant:
            self.client.post(url, {"action": "grant_service", "plan": self.plan.pk})
            grant.assert_not_called()
            grant.return_value = ProvisionedOrderResult(False, "test failure")
            response = self.client.post(url, {"action": "grant_service", "plan": self.plan.pk,
                                             "revenue_included": "yes", "revenue_amount": "65000"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(grant.call_args.kwargs["revenue_amount"], 65000)
            self.assertTrue(grant.call_args.kwargs["revenue_included"])
            self.assertEqual(grant.call_args.kwargs["customer"], self.customer)
            self.client.post(url, {"action": "grant_service", "plan": self.plan.pk, "revenue_included": "no"})
            self.assertFalse(grant.call_args.kwargs["revenue_included"])
            self.assertIsNone(grant.call_args.kwargs["revenue_amount"])

    def test_existing_grant_function_stores_financial_choice_before_activation(self):
        from .order_services import grant_free_subscription
        panel = Panel.objects.create(store=self.store, name="Test Panel", url="https://panel.example.com",
                                     username="test", password="test")
        inbound = Inbound.objects.create(panel=panel, inbound_id=1, server_ip="localhost", port="443")

        def activation(order, **kwargs):
            self.assertEqual(order.revenue_amount, 65000)
            self.assertTrue(order.revenue_included)
            self.assertEqual(order.amount, 0)
            self.assertEqual(order_revenue_amount(order), 0)
            Order.objects.filter(pk=order.pk).update(status=Order.Status.COMPLETED,
                verification_status=Order.VerificationStatus.VERIFIED)
            return SimpleNamespace(success=True)

        with patch("store.order_services.select_inbound_for_plan", return_value=inbound), \
                patch("store.order_services.validate_inbound_for_order", return_value=inbound), \
                patch("store.order_services.resolve_provisioning_strategy", return_value="test_deferred"), \
                patch("store.order_actions.activate_order", side_effect=activation):
            result = grant_free_subscription(store=self.store, customer=self.customer, plan=self.plan,
                                             revenue_included=True, revenue_amount=65000)
        self.assertTrue(result.success)
        self.assertEqual(order_revenue_amount(result.order), 65000)
        self.assertEqual(result.order.amount, 0)
        self.assertEqual(result.order.customer, self.customer)

    def test_v2_grant_uses_current_delivery_engine_and_keeps_customer_ownership(self):
        from .models import ConfigAllocation, ConfigInventoryAsset, ConfigInventoryPool, PlanDeliveryConfig, PlanDeliverySource
        from .order_services import grant_free_subscription
        pool = ConfigInventoryPool.objects.create(title="Grant Inventory")
        ConfigInventoryAsset.objects.create(pool=pool, raw_link="vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000001@vpn.example.com:443#Test")
        config = PlanDeliveryConfig.objects.create(plan=self.plan, delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS)
        PlanDeliverySource.objects.create(delivery_config=config, source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
                                          inventory_pool=pool, quantity=1)
        with patch("store.order_services.create_inactive_client_details") as legacy_create:
            result = grant_free_subscription(store=self.store, customer=self.customer, plan=self.plan,
                                             revenue_included=True, revenue_amount=65000, notify_customer=False)
            legacy_create.assert_not_called()
        self.assertTrue(result.success, result.message)
        self.assertEqual(order_revenue_amount(result.order), 65000)
        self.assertEqual(result.order.amount, 0)
        self.assertEqual(result.order.customer, self.customer)
        self.assertEqual(ConfigAllocation.objects.filter(order=result.order, order__customer=self.customer).count(), 1)
