from datetime import datetime, time, timedelta
from decimal import Decimal

import jdatetime
from django.db import models
from django.db.models import Count, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Customer, Order, ReferralRewardLedger, Store, VPNClient


SEGMENT_ALL = "all"
SEGMENT_ACTIVE_CUSTOMERS = "active_customers"
SEGMENT_ACTIVE_CONFIG = "customers_with_active_config"
SEGMENT_CUSTOMERS_WITHOUT_ORDER = "customers_without_order"
SEGMENT_LOYAL = "loyal"
SEGMENT_GOOD = "good"
SEGMENT_NEW_CUSTOMER = "new_customer"
SEGMENT_TOP_BUYER = "top_buyer"
SEGMENT_TOP_REFERRER = "top_referrer"
SEGMENT_INACTIVE = "inactive"
SEGMENT_NO_ORDER = "no_order"

SUPPORTED_SEGMENTS = {
    SEGMENT_ALL,
    SEGMENT_ACTIVE_CUSTOMERS,
    SEGMENT_ACTIVE_CONFIG,
    SEGMENT_CUSTOMERS_WITHOUT_ORDER,
    SEGMENT_LOYAL,
    SEGMENT_GOOD,
    SEGMENT_NEW_CUSTOMER,
    SEGMENT_TOP_BUYER,
    SEGMENT_TOP_REFERRER,
    SEGMENT_INACTIVE,
    SEGMENT_NO_ORDER,
}

PERIOD_TODAY = "today"
PERIOD_LAST_7_DAYS = "last_7_days"
PERIOD_LAST_30_DAYS = "last_30_days"
PERIOD_CURRENT_MONTH = "current_month"
PERIOD_ALL_TIME = "all_time"

SUPPORTED_PERIODS = {
    PERIOD_TODAY,
    PERIOD_LAST_7_DAYS,
    PERIOD_LAST_30_DAYS,
    PERIOD_CURRENT_MONTH,
    PERIOD_ALL_TIME,
}

CUSTOMER_STAT_DEFAULTS = {
    "total_paid_amount": 0,
    "successful_orders_count": 0,
    "renewal_orders_count": 0,
    "first_purchase_at": None,
    "last_purchase_at": None,
    "active_configs_count": 0,
    "total_purchased_gb": Decimal("0"),
    "total_referrals_count": 0,
    "successful_referrals_count": 0,
    "available_referral_gb": Decimal("0"),
    "redeemed_referral_gb": Decimal("0"),
}

METRIC_FIELDS = {
    "amount": "analytics_total_paid_amount",
    "orders": "analytics_successful_orders_count",
    "count": "analytics_successful_orders_count",
    "renewals": "analytics_renewal_orders_count",
    "renewal": "analytics_renewal_orders_count",
    "volume": "analytics_total_purchased_gb",
}

INTEGER_FIELD = models.IntegerField()
MONEY_FIELD = models.BigIntegerField()
DECIMAL_FIELD = models.DecimalField(max_digits=18, decimal_places=3)
DATETIME_FIELD = models.DateTimeField()


def get_success_order_statuses():
    return (Order.Status.COMPLETED,)


def get_analytics_store(store=None):
    if store:
        return store
    return Store.objects.filter(is_active=True).order_by("id").first() or Store.objects.order_by("id").first()


def analytics_enabled(store=None):
    store = get_analytics_store(store)
    return bool(getattr(store, "analytics_enabled", True)) if store else True


def _setting_value(name, default, store=None):
    store = get_analytics_store(store)
    value = getattr(store, name, default) if store else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def good_customer_min_total_amount(store=None):
    return _setting_value("good_customer_min_total_amount", 500000, store=store)


def loyal_customer_min_orders_30d(store=None):
    return _setting_value("loyal_customer_min_orders_30d", 2, store=store)


def top_customers_limit(store=None):
    return max(_setting_value("top_customers_limit", 10, store=store), 1)


def inactive_customer_days(store=None):
    return max(_setting_value("inactive_customer_days", 30, store=store), 1)


def _apply_limit(queryset, limit):
    if limit is None:
        return queryset
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return queryset
    if limit <= 0:
        return queryset.none()
    return queryset[:limit]


def get_period_range(period):
    period = period or PERIOD_ALL_TIME
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"Unsupported analytics period: {period}")

    now = timezone.now()
    local_now = timezone.localtime(now)

    if period == PERIOD_ALL_TIME:
        return None, None
    if period == PERIOD_TODAY:
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start, now
    if period == PERIOD_LAST_7_DAYS:
        return now - timedelta(days=7), now
    if period == PERIOD_LAST_30_DAYS:
        return now - timedelta(days=30), now

    jalali_today = jdatetime.date.fromgregorian(date=local_now.date())
    jalali_month_start = jdatetime.date(jalali_today.year, jalali_today.month, 1).togregorian()
    local_month_start = datetime.combine(jalali_month_start, time.min, tzinfo=local_now.tzinfo)
    return local_month_start, now


def _purchase_at_range_q(prefix="", date_from=None, date_to=None):
    query = Q()
    if date_from:
        query &= (
            Q(**{f"{prefix}verified_at__gte": date_from})
            | (Q(**{f"{prefix}verified_at__isnull": True}) & Q(**{f"{prefix}created_at__gte": date_from}))
        )
    if date_to:
        query &= (
            Q(**{f"{prefix}verified_at__lt": date_to})
            | (Q(**{f"{prefix}verified_at__isnull": True}) & Q(**{f"{prefix}created_at__lt": date_to}))
        )
    return query


def successful_order_q(prefix="", date_from=None, date_to=None):
    query = Q(**{f"{prefix}status__in": get_success_order_statuses()})
    query &= Q(**{f"{prefix}verification_status": Order.VerificationStatus.VERIFIED})
    query &= _purchase_at_range_q(prefix=prefix, date_from=date_from, date_to=date_to)
    return query


def _filter_successful_orders(queryset, date_from=None, date_to=None):
    return queryset.filter(successful_order_q(date_from=date_from, date_to=date_to))


def renewal_order_q(prefix=""):
    return Q(**{f"{prefix}metadata__renewal": True}) | Q(**{f"{prefix}metadata__renewal_client_pk__isnull": False})


def _order_success_at_expression():
    return Coalesce("verified_at", "created_at", output_field=DATETIME_FIELD)


def _decimal_zero():
    return Value(Decimal("0"), output_field=DECIMAL_FIELD)


def annotate_customer_queryset(queryset=None, date_from=None, date_to=None):
    queryset = queryset if queryset is not None else Customer.objects.all()
    now = timezone.now()

    successful_orders = _filter_successful_orders(
        Order.objects.filter(customer=OuterRef("pk")).order_by(),
        date_from=date_from,
        date_to=date_to,
    )
    renewal_orders = successful_orders.filter(renewal_order_q())
    volume_expression = ExpressionWrapper(
        F("plan__volume_gb") * F("quantity"),
        output_field=DECIMAL_FIELD,
    )

    total_paid = (
        successful_orders.values("customer")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    successful_count = (
        successful_orders.values("customer")
        .annotate(total=Count("pk"))
        .values("total")
    )
    renewal_count = (
        renewal_orders.values("customer")
        .annotate(total=Count("pk"))
        .values("total")
    )
    first_purchase = (
        successful_orders.annotate(success_at=_order_success_at_expression())
        .order_by("success_at", "pk")
        .values("success_at")[:1]
    )
    last_purchase = (
        successful_orders.annotate(success_at=_order_success_at_expression())
        .order_by("-success_at", "-pk")
        .values("success_at")[:1]
    )
    total_volume = (
        successful_orders.values("customer")
        .annotate(total=Sum(volume_expression))
        .values("total")
    )

    active_configs = (
        VPNClient.objects.filter(
            order__customer=OuterRef("pk"),
            status=VPNClient.Status.ACTIVE,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .order_by()
        .values("order__customer")
        .annotate(total=Count("pk"))
        .values("total")
    )

    referred_customers = Customer.objects.filter(referred_by=OuterRef("pk")).order_by()
    if date_from:
        referred_customers = referred_customers.filter(created_at__gte=date_from)
    if date_to:
        referred_customers = referred_customers.filter(created_at__lt=date_to)
    total_referrals = (
        referred_customers.values("referred_by")
        .annotate(total=Count("pk"))
        .values("total")
    )

    successful_referral_orders = _filter_successful_orders(
        Order.objects.filter(customer__referred_by=OuterRef("pk")).order_by(),
        date_from=date_from,
        date_to=date_to,
    )
    successful_referrals = (
        successful_referral_orders.values("customer__referred_by")
        .annotate(total=Count("customer", distinct=True))
        .values("total")
    )
    successful_referral_amount = (
        successful_referral_orders.values("customer__referred_by")
        .annotate(total=Sum("amount"))
        .values("total")
    )

    available_referral_gb = (
        ReferralRewardLedger.objects.filter(
            inviter=OuterRef("pk"),
            status=ReferralRewardLedger.Status.AVAILABLE,
        )
        .order_by()
        .values("inviter")
        .annotate(total=Sum("reward_gb"))
        .values("total")
    )
    redeemed_referral_gb = (
        ReferralRewardLedger.objects.filter(
            inviter=OuterRef("pk"),
            status=ReferralRewardLedger.Status.REDEEMED,
        )
        .order_by()
        .values("inviter")
        .annotate(total=Sum("reward_gb"))
        .values("total")
    )

    return (
        queryset.select_related("referred_by")
        .annotate(
            analytics_total_paid_amount=Coalesce(
                Subquery(total_paid, output_field=MONEY_FIELD),
                Value(0),
                output_field=MONEY_FIELD,
            ),
            analytics_successful_orders_count=Coalesce(
                Subquery(successful_count, output_field=INTEGER_FIELD),
                Value(0),
                output_field=INTEGER_FIELD,
            ),
            analytics_renewal_orders_count=Coalesce(
                Subquery(renewal_count, output_field=INTEGER_FIELD),
                Value(0),
                output_field=INTEGER_FIELD,
            ),
            analytics_first_purchase_at=Subquery(first_purchase, output_field=DATETIME_FIELD),
            analytics_last_purchase_at=Subquery(last_purchase, output_field=DATETIME_FIELD),
            analytics_active_configs_count=Coalesce(
                Subquery(active_configs, output_field=INTEGER_FIELD),
                Value(0),
                output_field=INTEGER_FIELD,
            ),
            analytics_total_purchased_gb=Coalesce(
                Subquery(total_volume, output_field=DECIMAL_FIELD),
                _decimal_zero(),
                output_field=DECIMAL_FIELD,
            ),
            analytics_total_referrals_count=Coalesce(
                Subquery(total_referrals, output_field=INTEGER_FIELD),
                Value(0),
                output_field=INTEGER_FIELD,
            ),
            analytics_successful_referrals_count=Coalesce(
                Subquery(successful_referrals, output_field=INTEGER_FIELD),
                Value(0),
                output_field=INTEGER_FIELD,
            ),
            analytics_successful_referral_amount=Coalesce(
                Subquery(successful_referral_amount, output_field=MONEY_FIELD),
                Value(0),
                output_field=MONEY_FIELD,
            ),
            analytics_available_referral_gb=Coalesce(
                Subquery(available_referral_gb, output_field=DECIMAL_FIELD),
                _decimal_zero(),
                output_field=DECIMAL_FIELD,
            ),
            analytics_redeemed_referral_gb=Coalesce(
                Subquery(redeemed_referral_gb, output_field=DECIMAL_FIELD),
                _decimal_zero(),
                output_field=DECIMAL_FIELD,
            ),
        )
    )


def stats_from_annotated_customer(customer):
    if not customer:
        return dict(CUSTOMER_STAT_DEFAULTS)
    return {
        "total_paid_amount": getattr(customer, "analytics_total_paid_amount", 0) or 0,
        "successful_orders_count": getattr(customer, "analytics_successful_orders_count", 0) or 0,
        "renewal_orders_count": getattr(customer, "analytics_renewal_orders_count", 0) or 0,
        "first_purchase_at": getattr(customer, "analytics_first_purchase_at", None),
        "last_purchase_at": getattr(customer, "analytics_last_purchase_at", None),
        "active_configs_count": getattr(customer, "analytics_active_configs_count", 0) or 0,
        "total_purchased_gb": getattr(customer, "analytics_total_purchased_gb", Decimal("0")) or Decimal("0"),
        "total_referrals_count": getattr(customer, "analytics_total_referrals_count", 0) or 0,
        "successful_referrals_count": getattr(customer, "analytics_successful_referrals_count", 0) or 0,
        "available_referral_gb": getattr(customer, "analytics_available_referral_gb", Decimal("0")) or Decimal("0"),
        "redeemed_referral_gb": getattr(customer, "analytics_redeemed_referral_gb", Decimal("0")) or Decimal("0"),
    }


def get_customer_stats(customer, date_from=None, date_to=None):
    if not customer:
        return dict(CUSTOMER_STAT_DEFAULTS)
    annotated = annotate_customer_queryset(
        Customer.objects.filter(pk=customer.pk),
        date_from=date_from,
        date_to=date_to,
    ).first()
    return stats_from_annotated_customer(annotated)


def _metric_field(metric):
    return METRIC_FIELDS.get(metric or "amount", METRIC_FIELDS["amount"])


def _order_by_metric(queryset, metric="amount"):
    field = _metric_field(metric)
    return queryset.order_by(f"-{field}", "-analytics_total_paid_amount", "display_name", "pk")


def get_top_customers(metric="amount", date_from=None, date_to=None, limit=None):
    limit = top_customers_limit() if limit is None else limit
    queryset = annotate_customer_queryset(
        Customer.objects.filter(is_active=True),
        date_from=date_from,
        date_to=date_to,
    ).filter(analytics_successful_orders_count__gt=0)
    return _apply_limit(_order_by_metric(queryset, metric=metric), limit)


def get_loyal_customers(date_from=None, date_to=None, limit=None):
    if date_from is None and date_to is None:
        date_from, date_to = get_period_range(PERIOD_LAST_30_DAYS)
    queryset = annotate_customer_queryset(
        Customer.objects.filter(is_active=True),
        date_from=date_from,
        date_to=date_to,
    ).filter(
        Q(analytics_successful_orders_count__gte=loyal_customer_min_orders_30d())
        | Q(analytics_renewal_orders_count__gte=1)
    )
    queryset = queryset.order_by("-analytics_successful_orders_count", "-analytics_renewal_orders_count", "display_name", "pk")
    return _apply_limit(queryset, limit)


def get_good_customers(date_from=None, date_to=None, limit=None):
    queryset = annotate_customer_queryset(
        Customer.objects.filter(is_active=True),
        date_from=date_from,
        date_to=date_to,
    ).filter(analytics_total_paid_amount__gte=good_customer_min_total_amount())
    queryset = queryset.order_by("-analytics_total_paid_amount", "display_name", "pk")
    return _apply_limit(queryset, limit)


def get_top_referrers(date_from=None, date_to=None, limit=None):
    limit = top_customers_limit() if limit is None else limit
    queryset = annotate_customer_queryset(
        Customer.objects.filter(is_active=True),
        date_from=date_from,
        date_to=date_to,
    ).filter(analytics_successful_referrals_count__gt=0)
    queryset = queryset.order_by(
        "-analytics_successful_referral_amount",
        "-analytics_successful_referrals_count",
        "display_name",
        "pk",
    )
    return _apply_limit(queryset, limit)


def _recent_30_day_range():
    return get_period_range(PERIOD_LAST_30_DAYS)


def get_customer_segment(customer):
    if not customer:
        return SEGMENT_NO_ORDER

    customer_pk = getattr(customer, "pk", None)
    if not customer_pk:
        return SEGMENT_NO_ORDER

    if customer_pk in set(get_top_customers(limit=top_customers_limit()).values_list("pk", flat=True)):
        return SEGMENT_TOP_BUYER
    if customer_pk in set(get_top_referrers(limit=top_customers_limit()).values_list("pk", flat=True)):
        return SEGMENT_TOP_REFERRER

    stats = get_customer_stats(customer)
    recent_from, recent_to = _recent_30_day_range()
    recent_stats = get_customer_stats(customer, recent_from, recent_to)

    if (
        recent_stats["successful_orders_count"] >= loyal_customer_min_orders_30d()
        or stats["renewal_orders_count"] >= 1
    ):
        return SEGMENT_LOYAL
    if stats["total_paid_amount"] >= good_customer_min_total_amount():
        return SEGMENT_GOOD
    if stats["first_purchase_at"] and stats["first_purchase_at"] >= recent_from:
        return SEGMENT_NEW_CUSTOMER
    if stats["successful_orders_count"] <= 0:
        return SEGMENT_NO_ORDER

    inactive_cutoff = timezone.now() - timedelta(days=inactive_customer_days())
    if stats["last_purchase_at"] and stats["last_purchase_at"] < inactive_cutoff:
        return SEGMENT_INACTIVE
    return SEGMENT_ACTIVE_CUSTOMERS


def get_customers_by_segment(segment, date_from=None, date_to=None, limit=None):
    segment = segment or SEGMENT_ALL
    if segment not in SUPPORTED_SEGMENTS:
        raise ValueError(f"Unsupported customer segment: {segment}")

    if segment == SEGMENT_TOP_BUYER:
        return get_top_customers(date_from=date_from, date_to=date_to, limit=limit)
    if segment == SEGMENT_TOP_REFERRER:
        return get_top_referrers(date_from=date_from, date_to=date_to, limit=limit)
    if segment == SEGMENT_LOYAL:
        return get_loyal_customers(date_from=date_from, date_to=date_to, limit=limit)
    if segment == SEGMENT_GOOD:
        return get_good_customers(date_from=date_from, date_to=date_to, limit=limit)

    if segment in {SEGMENT_NO_ORDER, SEGMENT_CUSTOMERS_WITHOUT_ORDER}:
        queryset = annotate_customer_queryset(Customer.objects.filter(is_active=True)).filter(
            analytics_successful_orders_count=0,
        ).order_by("-created_at", "pk")
        return _apply_limit(queryset, limit)

    if segment == SEGMENT_NEW_CUSTOMER:
        cutoff = date_from or get_period_range(PERIOD_LAST_30_DAYS)[0]
        queryset = annotate_customer_queryset(Customer.objects.filter(is_active=True)).filter(
            analytics_first_purchase_at__gte=cutoff,
        ).order_by("-analytics_first_purchase_at", "display_name", "pk")
        return _apply_limit(queryset, limit)

    if segment == SEGMENT_INACTIVE:
        inactive_cutoff = timezone.now() - timedelta(days=inactive_customer_days())
        queryset = annotate_customer_queryset(Customer.objects.filter(is_active=True)).filter(
            analytics_successful_orders_count__gt=0,
            analytics_last_purchase_at__lt=inactive_cutoff,
        ).order_by("analytics_last_purchase_at", "display_name", "pk")
        return _apply_limit(queryset, limit)

    queryset = annotate_customer_queryset(
        Customer.objects.filter(is_active=True),
        date_from=date_from,
        date_to=date_to,
    )
    if segment == SEGMENT_ACTIVE_CUSTOMERS:
        queryset = queryset.filter(analytics_successful_orders_count__gt=0)
    elif segment == SEGMENT_ACTIVE_CONFIG:
        queryset = queryset.filter(analytics_active_configs_count__gt=0)

    queryset = queryset.order_by("-analytics_total_paid_amount", "display_name", "pk")
    return _apply_limit(queryset, limit)


def period_range_from_request(period):
    return get_period_range(period or PERIOD_LAST_30_DAYS)
