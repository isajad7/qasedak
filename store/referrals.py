import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Customer, CustomerReward, DiscountCode, Plan, Referral, Store, generate_short_code

REFERRAL_REWARD_PERCENTAGE = 20
FREE_PLAN_REFERRAL_MILESTONE = 5


def normalize_referral_code(code):
    return (code or "").strip().upper()


def build_referral_link(request, customer):
    return request.build_absolute_uri(f"/?ref={customer.referral_code}")


def get_referral_code_from_request(request):
    return normalize_referral_code(
        request.POST.get("referral_code")
        or request.GET.get("ref")
        or request.GET.get("referral")
        or ""
    )


def assign_referrer(customer, referral_code):
    referral_code = normalize_referral_code(referral_code)
    if not customer or not referral_code or customer.referred_by_id:
        return None

    referrer = Customer.objects.filter(referral_code=referral_code, is_active=True).first()
    if not referrer or referrer.pk == customer.pk:
        return None

    with transaction.atomic():
        locked_customer = Customer.objects.select_for_update().get(pk=customer.pk)
        if locked_customer.referred_by_id:
            return getattr(locked_customer, "referral_received", None)

        locked_customer.referred_by = referrer
        locked_customer.referral_code_used = referral_code
        locked_customer.referred_at = timezone.now()
        locked_customer.save(
            update_fields=[
                "referred_by",
                "referral_code_used",
                "referred_at",
                "updated_at",
            ]
        )
        referral, _ = Referral.objects.get_or_create(
            referrer=referrer,
            referred_customer=locked_customer,
            defaults={
                "referral_code": referral_code,
                "status": Referral.Status.REGISTERED,
            },
        )
    return referral


def generate_unique_discount_code(prefix):
    prefix = normalize_referral_code(prefix)
    for _ in range(20):
        code = f"{prefix}{generate_short_code('', 8)}"
        if not DiscountCode.objects.filter(code=code).exists():
            return code
    return f"{prefix}{uuid.uuid4().hex[:10].upper()}"


def create_referral_discount_reward(referral):
    referrer = referral.referrer
    reward, created = CustomerReward.objects.get_or_create(
        customer=referrer,
        referral=referral,
        reward_type=CustomerReward.RewardType.DISCOUNT_20,
        defaults={
            "title": "20% referral discount",
            "description": "Earned when your invited user completed their first purchase.",
        },
    )
    if not created and reward.discount_code_id:
        return reward

    discount = DiscountCode.objects.create(
        code=generate_unique_discount_code("REF20"),
        discount_type=DiscountCode.DiscountType.PERCENTAGE,
        value=REFERRAL_REWARD_PERCENTAGE,
        max_usage=1,
        valid_from=timezone.now(),
        is_active=True,
    )
    reward.discount_code = discount
    reward.status = CustomerReward.Status.AVAILABLE
    reward.earned_at = reward.earned_at or timezone.now()
    reward.save(update_fields=["discount_code", "status", "earned_at", "updated_at"])
    return reward


def get_or_create_free_referral_plan(store):
    store = store or Store.objects.filter(is_active=True).first() or Store.objects.first()
    plan, _ = Plan.objects.get_or_create(
        store=store,
        slug="referral-free-1gb",
        defaults={
            "name": "Referral Free 1GB",
            "description": "Internal reward plan for referral milestones.",
            "volume_gb": Decimal("1.000"),
            "duration_days": 30,
            "price": 0,
            "currency": Plan.Currency.TOMAN,
            "device_limit": 1,
            "is_active": True,
            "is_public": False,
            "sort_order": 9999,
        },
    )
    changed_fields = []
    if plan.volume_gb != Decimal("1.000"):
        plan.volume_gb = Decimal("1.000")
        changed_fields.append("volume_gb")
    if plan.price != 0:
        plan.price = 0
        changed_fields.append("price")
    if plan.is_public:
        plan.is_public = False
        changed_fields.append("is_public")
    if not plan.is_active:
        plan.is_active = True
        changed_fields.append("is_active")
    if changed_fields:
        plan.save(update_fields=[*changed_fields, "updated_at"])
    return plan


def create_free_plan_reward(referrer, *, milestone, store=None):
    reward, created = CustomerReward.objects.get_or_create(
        customer=referrer,
        reward_type=CustomerReward.RewardType.FREE_1GB_PLAN,
        milestone=milestone,
        defaults={
            "title": "Free 1GB referral plan",
            "description": f"Earned after {milestone} successful referral purchases.",
        },
    )
    if not created and reward.plan_id:
        return reward

    plan = get_or_create_free_referral_plan(store)
    discount = reward.discount_code
    if not discount:
        discount = DiscountCode.objects.create(
            code=generate_unique_discount_code("FREE1GB"),
            discount_type=DiscountCode.DiscountType.PERCENTAGE,
            value=100,
            max_usage=1,
            valid_from=timezone.now(),
            is_active=True,
        )
        discount.applicable_plans.add(plan)

    reward.plan = plan
    reward.discount_code = discount
    reward.status = CustomerReward.Status.AVAILABLE
    reward.earned_at = reward.earned_at or timezone.now()
    reward.save(update_fields=["plan", "discount_code", "status", "earned_at", "updated_at"])
    return reward


def process_referral_purchase(order):
    if (
        not order.customer_id
        or order.status != order.Status.COMPLETED
        or order.verification_status != order.VerificationStatus.VERIFIED
    ):
        return []

    customer = order.customer
    if not customer.referred_by_id:
        return []

    with transaction.atomic():
        referral = (
            Referral.objects.select_for_update()
            .select_related("referrer", "referred_customer")
            .filter(referred_customer=customer)
            .first()
        )
        if not referral:
            referral = Referral.objects.create(
                referrer=customer.referred_by,
                referred_customer=customer,
                referral_code=customer.referral_code_used,
            )

        rewards = []
        if referral.status != Referral.Status.PURCHASED:
            referral.status = Referral.Status.PURCHASED
            referral.first_order = order
            referral.purchased_at = timezone.now()
            referral.save(update_fields=["status", "first_order", "purchased_at", "updated_at"])
            rewards.append(create_referral_discount_reward(referral))

        successful_count = Referral.objects.filter(
            referrer=referral.referrer,
            status=Referral.Status.PURCHASED,
        ).count()

        if successful_count and successful_count % FREE_PLAN_REFERRAL_MILESTONE == 0:
            rewards.append(
                create_free_plan_reward(
                    referral.referrer,
                    milestone=successful_count,
                    store=order.store,
                )
            )

    return rewards
