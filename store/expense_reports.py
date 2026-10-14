"""Expense and order revenue totals for the admin financial overview."""

from django.db.models import Sum

from .admin_reports_center import _successful_orders
from .models import Expense
from .order_revenue import revenue_amount_expression


def financial_summary(store, period):
    """Keep currencies separate; revenue uses the same order policy as sales reports."""
    expenses = Expense.objects.filter(
        store=store, expense_date__gte=period.start_date, expense_date__lte=period.end_date,
    )
    revenue = {
        row["currency"]: row["total"] or 0
        for row in _successful_orders(period.start, period.end, store).values("currency")
        .annotate(total=Sum(revenue_amount_expression()))
    }
    expense_totals = {
        row["currency"]: row["total"] or 0
        for row in expenses.values("currency").annotate(total=Sum("amount"))
    }
    rows = [
        {"currency": currency, "revenue": revenue.get(currency, 0),
         "expense": expense_totals.get(currency, 0),
         "net": revenue.get(currency, 0) - expense_totals.get(currency, 0)}
        for currency in sorted(set(revenue) | set(expense_totals) | {"TOMAN"})
    ]
    categories = list(expenses.values("category", "currency").annotate(total=Sum("amount"))
                      .order_by("category", "currency"))
    return expenses, rows, categories
