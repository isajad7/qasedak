"""Small shared admin assignment flow. Remote provisioning stays in order_services."""
from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .config_inventory_services import _validate_selected_asset, ConfigInventoryError
from .db_locking import select_for_update_self
from .models import BotUser, ConfigInventoryAsset, Customer, Order, OrderAutomation, Plan, SubscriptionCup, normalize_payment_digits
from .order_revenue import AdminServiceGrantForm
from .order_services import ProvisionedOrderResult, get_current_store


def bot_store(config):
    return config.store or get_current_store()


def search_bot_users(config, query=""):
    users = BotUser.objects.filter(bot_config=config).select_related("customer", "bot_config")
    query = normalize_payment_digits(str(query or "")).strip().lstrip("@")[:160]
    if query:
        users = users.filter(
            Q(provider_user_id=query) | Q(username__icontains=query) | Q(display_name__icontains=query)
            | Q(first_name__icontains=query) | Q(last_name__icontains=query)
        )
    return users.order_by("-last_seen_at", "-pk")


def target_label(target):
    name = target.display_name or " ".join(filter(None, [target.first_name, target.last_name])) or "کاربر"
    return f"{name} · @{target.username.lstrip('@') if target.username else '-'} · {target.provider_user_id}"


def ensure_target_customer(target):
    # A username is mutable; never adopt another customer's account by username here.
    with transaction.atomic():
        current = select_for_update_self(BotUser.objects.all()).get(pk=target.pk)
        if not current.customer_id:
            current.customer = Customer.objects.create(display_name=(target.display_name or "کاربر ربات")[:120])
            current.save(update_fields=["customer", "updated_at"])
    target.customer = current.customer
    return current.customer


def target_orders(target):
    if not target.customer_id:
        return Order.objects.none()
    return Order.objects.filter(customer_id=target.customer_id, store=bot_store(target.bot_config)).filter(
        Q(metadata__bot__bot_config_id=target.bot_config_id) | Q(metadata__bot__bot_config_id__isnull=True)
    ).select_related("plan", "customer", "store").order_by("-created_at", "-pk")


def assignment_plans(config=None):
    qs = Plan.objects.filter(is_active=True, store__is_active=True).select_related("store")
    return qs.filter(store=bot_store(config)) if config else qs


def assignment_assets(plan):
    """Plan inventory and unbound general stock; never another plan's private pool."""
    pools = (
        Q(pool__connected_plan=plan)
        | Q(pool__plan_delivery_sources__delivery_config__plan=plan, pool__plan_delivery_sources__active=True)
        | Q(pool__cup_filler_rules__recipe__plan=plan, pool__cup_filler_rules__is_active=True)
        | Q(pool__connected_plan__isnull=True, pool__plan_delivery_sources__isnull=True,
            pool__cup_filler_rules__isnull=True)
    )
    return ConfigInventoryAsset.objects.filter(pools, pool__is_active=True).filter(
        status__in=[ConfigInventoryAsset.Status.AVAILABLE, ConfigInventoryAsset.Status.ASSIGNED],
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())).select_related("pool").distinct().order_by("pk")


def asset_is_available(asset):
    try:
        _validate_selected_asset(asset, pool=asset.pool, mode=asset.pool.allocation_mode, requested_count=1)
        return True
    except ConfigInventoryError:
        return False


def asset_label(asset):
    return f"#{asset.pk} · {asset.pool.title} · {asset.protocol} · {asset.pool.get_allocation_mode_display()}"


class AssignmentForm(AdminServiceGrantForm):
    mode = forms.ChoiceField(label="نوع اختصاص", choices=(("new", "اختصاص سرویس جدید"), ("existing", "اختصاص کانفیگ موجود")))
    bot_user = forms.ModelChoiceField(label="گیرنده در ربات", queryset=BotUser.objects.none(), required=False)
    asset = forms.ModelChoiceField(label="کانفیگ موجود", queryset=ConfigInventoryAsset.objects.none(), required=False)

    def __init__(self, *args, customer, target=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target
        self.fields["bot_user"].queryset = customer.bot_users.select_related("bot_config")
        self.fields["bot_user"].label_from_instance = lambda user: f"{user.bot_config.name} · {target_label(user)}"
        self.fields["bot_user"].required = self.fields["bot_user"].queryset.exists()
        if target:
            self.fields["bot_user"].queryset = self.fields["bot_user"].queryset.filter(pk=target.pk)
            self.fields["bot_user"].initial = target.pk
        self.fields["plan"].queryset = assignment_plans(target.bot_config if target else None)
        plan_id = self.data.get("plan") if self.is_bound else None
        if str(plan_id or "").isdigit():
            plan = self.fields["plan"].queryset.filter(pk=plan_id).first()
            if plan:
                self.fields["asset"].queryset = assignment_assets(plan)
        self.fields["asset"].label_from_instance = asset_label

    def clean(self):
        cleaned = super().clean()
        target, plan = cleaned.get("bot_user"), cleaned.get("plan")
        if target and plan and plan.store_id != getattr(bot_store(target.bot_config), "pk", None):
            self.add_error("plan", "پلن باید متعلق به فروشگاه همین ربات باشد.")
        if cleaned.get("mode") == "existing":
            asset = cleaned.get("asset")
            if not asset or not asset_is_available(asset):
                self.add_error("asset", "یک کانفیگ قابل‌واگذاری انتخاب کن.")
        return cleaned


def assignment_metadata(target, actor_key):
    metadata = {"assignment_actor": actor_key}
    if target:
        metadata["bot"] = {
            "bot_user_id": target.pk, "bot_config_id": target.bot_config_id,
            "provider_user_id": target.provider_user_id, "chat_id": target.chat_id,
        }
    return metadata


def assign_customer_service(*, customer, plan, target, mode, asset, included, amount, request_id,
                            actor_key, actor=None, reason=""):
    from .order_services import grant_free_subscription

    if target and (target.customer_id != customer.pk or plan.store_id != getattr(bot_store(target.bot_config), "pk", None)):
        raise ValidationError("مشتری یا فروشگاه مقصد معتبر نیست.")
    previous = Order.objects.filter(public_id=request_id).first()
    if previous:
        if previous.customer_id != customer.pk or (previous.metadata or {}).get("assignment_actor") != actor_key:
            raise ValidationError("تأیید عملیات معتبر نیست.")
        return ProvisionedOrderResult(previous.status == Order.Status.COMPLETED,
                                      "این عملیات قبلاً ثبت شده؛ از همان سفارش پیگیری کن.", previous,
                                      duplicate_detected=True)
    if not plan.is_active or not plan.store.is_active or (included and (amount is None or not 0 < amount <= 2147483647)):
        raise ValidationError("پلن یا مبلغ معتبر نیست.")
    metadata = assignment_metadata(target, actor_key)
    if mode == "new":
        return grant_free_subscription(
            store=plan.store, customer=customer, plan=plan, granted_by=actor, reason=reason,
            metadata=metadata, revenue_included=included, revenue_amount=amount, request_id=request_id,
        )
    if mode != "existing" or not asset or not assignment_assets(plan).filter(pk=asset.pk).exists():
        raise ValidationError("کانفیگ انتخاب‌شده به موجودی این پلن تعلق ندارد.")
    metadata.update({
        "source": "admin_inventory", "free_grant": True, "free_grant_reason": reason,
        "suppress_new_order_notification": True, "suppress_admin_order_updates": True,
        "admin_inventory_asset_id": asset.pk,
        "plan_delivery_v2": {"delivery_mode": "SUBSCRIPTION", "status": "pending"},
    })
    order, created = Order.objects.get_or_create(public_id=request_id, defaults={
        "store": plan.store, "customer": customer, "plan": plan, "amount": 0,
        "original_amount": plan.price, "discount_amount": plan.price, "currency": plan.currency,
        "payment_method": Order.PaymentMethod.ADMIN_FREE, "is_paid": True,
        "payment_submitted_at": timezone.now(), "provisioning_status": Order.ProvisioningStatus.PENDING,
        "metadata": metadata, "revenue_included": included, "revenue_amount": amount if included else None,
    })
    if not created:
        if order.customer_id != customer.pk or (order.metadata or {}).get("assignment_actor") != actor_key:
            raise ValidationError("تأیید عملیات معتبر نیست.")
        return ProvisionedOrderResult(False, "این عملیات قبلاً ثبت شده؛ از همان سفارش پیگیری کن.", order,
                                      duplicate_detected=True)
    result = complete_inventory_assignment(order, actor=actor)
    order.refresh_from_db()
    return ProvisionedOrderResult(result.success, result.message, order)


def complete_inventory_assignment(order, *, actor=None, notify=True):
    from .admin_cup_center.services import add_inventory_assets_to_cup
    from .provisioning_services import ProvisioningResult
    from .subscription_cups import build_subscription_cup_url
    from .telegram_bot.notifications import notify_order_event

    with transaction.atomic():
        order = select_for_update_self(Order.objects.all()).get(pk=order.pk)
        if order.status == Order.Status.COMPLETED:
            return ProvisioningResult(True, "این سفارش قبلاً تکمیل شده است.", already_provisioned=True)
        if OrderAutomation.objects.filter(order=order, review_status__in=("cancelling", "cancel_failed", "cancelled")).exists():
            return ProvisioningResult(False, "لغو سفارش ثبت شده؛ اختصاص دوباره مجاز نیست.")
        if order.status in {Order.Status.CANCELLED, Order.Status.REJECTED} or order.verification_status == Order.VerificationStatus.REJECTED:
            return ProvisioningResult(False, "سفارش لغوشده یا ردشده قابل اختصاص نیست.")
        asset = assignment_assets(order.plan).filter(pk=order.metadata["admin_inventory_asset_id"]).first()
        order.provisioning_attempts += 1
        error = "کانفیگ موجودی این پلن پیدا نشد."
        if asset:
            cup, _ = SubscriptionCup.objects.get_or_create(order=order, vpn_client=None, defaults={
                "customer": order.customer, "plan": order.plan, "title": order.plan.name,
                "expires_at": min(filter(None, [asset.expires_at, timezone.now() + timedelta(days=asset.pool.duration_days or order.plan.duration_days)])),
                "traffic_limit_bytes": order.plan.traffic_limit_bytes, "device_limit": order.plan.device_limit,
            })
            result = add_inventory_assets_to_cup(cup, asset.pool, [asset.pk])
            error = ("؛ ".join(result.errors) or "کانفیگ به کاپ اضافه نشد.") if not result.added_count else ""
            if not error:
                cup.config_allocations.update(order=order)
                order.sub_link = build_subscription_cup_url(cup, store=order.store)
                order.mark_payment_verified(user=actor)
                order.provisioned_at = timezone.now()
                order.metadata["plan_delivery_v2"]["status"] = "provisioned"
        order.last_provisioning_error = error
        order.provisioning_status = Order.ProvisioningStatus.FAILED if error else Order.ProvisioningStatus.PROVISIONED
        order.save()
    if error:
        return ProvisioningResult(False, error)
    message = "کانفیگ موجود به مشتری اختصاص یافت."
    if notify and not notify_order_event(order, event_type="approved"):
        message += " پیام مشتری ارسال نشد؛ ارسال مجدد در دسترس است."
    return ProvisioningResult(True, message)
