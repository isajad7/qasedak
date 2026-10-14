from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .admin_reports_center import get_sales_metrics, resolve_report_period
from .expense_reports import financial_summary
from .models import Customer, Expense, Order, Panel, Plan, Store


class ExpenseAdminTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Shop A", english_name="Shop A", slug="shop-a")
        self.other = Store.objects.create(name="Shop B", english_name="Shop B", slug="shop-b")
        self.admin = get_user_model().objects.create_superuser("expense-admin", "admin@example.com", "secret")
        self.client.force_login(self.admin)
        self.today = timezone.localdate()
        self.url = reverse("admin:store_expense_summary")
        self.add_url = reverse("admin:store_expense_add")

    def expense(self, **kwargs):
        data = dict(store=self.store, amount=100, expense_date=self.today,
                    category=Expense.Category.DOMAIN, description="Domain renewal")
        data.update(kwargs)
        return Expense.objects.create(**data)

    def post_data(self, **kwargs):
        data = dict(store=str(self.store.pk), amount="100", currency="TOMAN",
                    expense_date=self.today.isoformat(), category=Expense.Category.DOMAIN,
                    description="Domain renewal", panel="", _save="ذخیره")
        data.update(kwargs)
        return data

    def test_admin_create_edit_delete_and_summary_refresh(self):
        response = self.client.post(self.add_url, self.post_data())
        self.assertEqual(response.status_code, 302, response.context["adminform"].form.errors if response.context else "")
        expense = Expense.objects.get()
        summary = self.client.get(self.url, {"store": self.store.pk, "range": "today"})
        self.assertEqual(summary.context["financial_rows"][0]["expense"], 100)
        edit_url = reverse("admin:store_expense_change", args=[expense.pk])
        response = self.client.post(edit_url, self.post_data(amount="250"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(self.url, {"store": self.store.pk, "range": "today"}).context["financial_rows"][0]["expense"], 250)
        delete_url = reverse("admin:store_expense_delete", args=[expense.pk])
        response = self.client.post(delete_url, {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Expense.objects.exists())
        self.assertEqual(self.client.get(self.url, {"store": self.store.pk, "range": "today"}).context["financial_rows"][0]["expense"], 0)

    def test_date_category_search_store_and_list_total(self):
        self.expense()
        self.expense(amount=200, category=Expense.Category.SOFTWARE, description="Tool")
        self.expense(amount=500, expense_date=self.today - timedelta(days=40))
        self.expense(store=self.other, amount=900)
        response = self.client.get(self.url, {"store": self.store.pk, "range": "today",
                                               "category": Expense.Category.DOMAIN, "q": "Domain"})
        self.assertEqual(response.context["financial_rows"][0]["expense"], 300)
        self.assertEqual(response.context["matching_totals"], ["۱۰۰ تومان"])
        self.assertEqual(response.context["page"].paginator.count, 1)
        period = resolve_report_period({"range": "custom", "start": (self.today - timedelta(days=41)).isoformat(),
                                        "end": (self.today - timedelta(days=39)).isoformat()}, self.store)
        expenses, rows, _ = financial_summary(self.store, period)
        self.assertEqual(expenses.count(), 1)
        self.assertEqual(rows[0]["expense"], 500)

    def test_panel_validation_and_currency_separation(self):
        panel = Panel.objects.create(store=self.other, name="Other panel", url="https://example.com", username="user", password="pass")
        response = self.client.post(self.add_url, self.post_data(panel=str(panel.pk)))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.exists())
        invalid = self.expense(panel=panel)
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        self.expense(amount=700, currency="IRR", panel=None)
        rows = self.client.get(self.url, {"store": self.store.pk, "range": "today"}).context["financial_rows"]
        self.assertEqual({row["currency"]: row["expense"] for row in rows}, {"IRR": 700, "TOMAN": 100})

    def test_revenue_matches_existing_sales_and_excludes_admin_direct(self):
        plan = Plan.objects.create(store=self.store, name="Plan", slug="expense-plan", price=1000,
                                   volume_gb=Decimal("5"), duration_days=30)
        customer = Customer.objects.create(display_name="Customer", username="expense-customer")
        for metadata in ({}, {"admin_direct_purchase": True}):
            Order.objects.create(store=self.store, plan=plan, customer=customer, amount=1000,
                                 status=Order.Status.COMPLETED, is_paid=True, verified_at=timezone.now(),
                                 verification_status=Order.VerificationStatus.VERIFIED, metadata=metadata)
        self.expense(amount=1500)
        period = resolve_report_period({"range": "today"}, self.store)
        _, rows, _ = financial_summary(self.store, period)
        sales = get_sales_metrics(period, self.store)
        self.assertEqual(rows[0]["revenue"], sales["revenue"])
        self.assertEqual(rows[0]["revenue"], 1000)
        self.assertEqual(rows[0]["net"], -500)
