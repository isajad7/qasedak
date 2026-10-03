"""Purchase-bound journeys. Reserve before transport; never retry ambiguous sends."""
from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from store.models import BotConfiguration, BotUser, Customer, Order, RevenueOfferLog, Store, SupportConversation, VPNClient
from store.db_locking import select_for_update_self
from store.telegram_bot.client import BotClient, BotDeliveryError
from store.telegram_bot.formatting import bot_datetime, bot_gb_from_bytes
from .mapping import FULFILLED
from .models import OutreachEvent, OutreachPreference, OutreachSettings, PurchaseActivity
from .services import MAX_GAP, activity_status

PRIORITY = {"inactive_48h": 100, "ended": 90, "expiry_24h": 80, "volume_5": 70, "expiry_72h": 40, "volume_20": 30}
LABELS = {"inactive_48h": "پیگیری عدم مصرف", "ended": "پایان سرویس", "expiry_24h": "انقضا تا ۲۴ ساعت",
          "volume_5": "حجم زیر ۵٪", "expiry_72h": "انقضا تا ۷۲ ساعت", "volume_20": "حجم زیر ۲۰٪"}
PENDING = (OutreachEvent.Status.PREVIEW, OutreachEvent.Status.READY, OutreachEvent.Status.CANCELLED)
RESERVED = (OutreachEvent.Status.SENDING, OutreachEvent.Status.SENT, OutreachEvent.Status.UNCERTAIN)
REASONS = {"disabled": "ارسال خاموش است", "quiet_hours": "خارج از ساعت ارسال", "muted": "مشتری یادآوری را متوقف کرده",
           "open_support": "تیکت پشتیبانی باز دارد", "customer_cap": "سقف روزانه/هفتگی مشتری", "store_cap": "سقف روزانه فروشگاه",
           "no_target": "حساب تلگرام خصوصی و مرتبط پیدا نشد", "renewal_pending": "تمدید در انتظار بررسی است",
           "renewed": "این خرید تمدید شده", "not_eligible": "شرایط یا دادهٔ معتبر دیگر برقرار نیست",
           "cycle_changed": "دورهٔ سرویس تغییر کرده", "transport_unknown": "ممکن است پیام رسیده باشد؛ خودکار تکرار نمی‌شود",
           "worker_interrupted": "ارسال قطع شده؛ خودکار تکرار نمی‌شود", "telegram_rejected": "تلگرام ارسال را رد کرد"}


def purchase_clients(order):
    ids = list(VPNClient.objects.filter(order=order, store=order.store).values_list("pk", flat=True))
    target = (order.metadata or {}).get("renewal_client_pk")
    if str(target or "").isdigit():
        ids.extend(VPNClient.objects.filter(pk=int(target), store=order.store, order__customer_id=order.customer_id).values_list("pk", flat=True))
    return VPNClient.objects.filter(pk__in=ids).order_by("pk")


def renewal_state(order, now):
    for client in purchase_clients(order):
        renewals = Order.objects.filter(Q(metadata__renewal_client_pk=client.pk) | Q(metadata__renewal_client_pk=str(client.pk)),
                                        store=order.store, customer_id=order.customer_id).exclude(pk=order.pk)
        renewed = renewals.filter(status__in=FULFILLED, verification_status="verified",
                                 created_at__gt=order.created_at).order_by("-created_at").first()
        if renewed:
            return "renewed", renewed
        if renewals.filter(status="pending_verification", payment_submitted_at__gte=now - timedelta(days=7)).exists():
            return "renewal_pending", None
    return "", None


def candidate_kind(activity, config, now):
    order = activity.order
    if (not order.customer_id or not order.store.is_active or order.status not in FULFILLED
            or order.verification_status != "verified" or activity.reason != "ok"
            or not activity.observed_at or now - activity.observed_at > MAX_GAP or activity.observed_at > now):
        return ""
    # Current local cancellation/Cup changes must suppress a queued message before the next panel poll.
    if order.updated_at > activity.observed_at:
        return ""
    if order.subscription_cups.filter(Q(status="disabled") | Q(expires_at__lte=now) | Q(updated_at__gt=activity.observed_at)
                                      | Q(items__updated_at__gt=activity.observed_at)
                                      | Q(items__config_link__updated_at__gt=activity.observed_at)).exists():
        return ""
    if order.vpn_clients.filter(updated_at__gt=activity.observed_at).exists():
        return ""
    if renewal_state(order, now)[0]:
        return ""
    if (config.inactivity_enabled and activity.entitlement == "valid"
            and activity_status(activity, now=now)[0] == "inactive"):
        return "inactive_48h"
    if not config.renewal_enabled:
        return ""
    if activity.entitlement in {"expired", "exhausted", "ended"}:
        return "ended" if activity.ended_at and now - timedelta(hours=48) <= activity.ended_at <= now else ""
    if activity.entitlement != "valid" or not activity.counters:
        return ""
    expiry, remaining = [], []
    for values in activity.counters.values():
        at = parse_datetime(values["at"])
        if not at or not timedelta(0) <= now - at <= MAX_GAP:
            return ""
        if values.get("expiry"):
            date = parse_datetime(values["expiry"])
            if date and date > now:
                expiry.append((date - now).total_seconds())
        quota = values.get("quota")
        if isinstance(quota, int) and quota > 0:
            remaining.append(max(0, quota - values["used"]) / quota)
    if expiry and min(expiry) <= 86400:
        return "expiry_24h"
    if remaining and min(remaining) <= .05:
        return "volume_5"
    if expiry and min(expiry) <= 3 * 86400:
        return "expiry_72h"
    if remaining and min(remaining) <= .20:
        return "volume_20"
    return ""


def message_body(activity, kind, now):
    order = activity.order
    title = str(order.plan.name if order.plan_id else "سرویس")[:160]
    quotas = sorted({v["quota"] for v in activity.counters.values() if v.get("quota") and v["quota"] > 0})
    volume = " / ".join(bot_gb_from_bytes(value) for value in quotas)
    header = f"خرید «{title}» · سفارش #{order.pk}\nتاریخ خرید: {bot_datetime(order.created_at)}"
    if volume:
        header += f"\nسقف فعلی حجم منبع‌های این خرید: {volume} گیگابایت"
    if kind == "inactive_48h":
        since = activity.last_activity_at or activity.continuous_since
        days = max(2, (now - since).days)
        text = f"دست‌کم {days} روز است مصرفی برای این خرید ثبت نشده. برای اتصال یا استفاده مشکلی پیش آمده؟ از دکمهٔ زیر به پشتیبانی پیام بده؛ مشخصات همین خرید همراه پیامت ثبت می‌شود."
    elif kind == "ended":
        text = "زمان یا حجم این سرویس به پایان رسیده است. اگر هنوز به آن نیاز داری، می‌توانی از دکمهٔ زیر تمدید کنی."
    elif kind.startswith("expiry_"):
        hours = 24 if kind == "expiry_24h" else 72
        text = f"زمان دست‌کم یکی از منبع‌های این خرید تا {hours} ساعت دیگر تمام می‌شود. برای ادامهٔ استفاده، وضعیت خرید و گزینهٔ تمدید را بررسی کن."
    else:
        percent = 5 if kind == "volume_5" else 20
        text = f"حجم باقی‌ماندهٔ دست‌کم یکی از منبع‌های این خرید به {percent}٪ یا کمتر رسیده است. برای ادامهٔ استفاده می‌توانی وضعیت خرید و گزینهٔ تمدید را بررسی کنی."
    return header + "\n\n" + text


def keyboard(event):
    rows = [[{"text": "ارسال پیام به پشتیبانی", "callback_data": f"journey:support:{event.pk}"}]]
    if event.kind != "inactive_48h":
        rows.insert(0, [{"text": "بررسی و تمدید همین خرید", "callback_data": f"journey:renew:{event.pk}"}])
    rows.append([{"text": "توقف یادآوری‌های خودکار", "callback_data": f"journey:mute:{event.pk}"}])
    return {"inline_keyboard": rows}


def personal_target(order):
    # An existing private bot membership is required; never fall back to an admin/group chat.
    users = BotUser.objects.filter(customer_id=order.customer_id, is_active=True,
                                  bot_config__store_id=order.store_id, bot_config__is_active=True,
                                  bot_config__provider=BotConfiguration.Provider.TELEGRAM).exclude(bot_config__bot_token="").select_related("bot_config")
    for user in users.order_by("-last_seen_at", "pk"):
        chat, provider_id = str(user.chat_id or ""), str(user.provider_user_id or "")
        if chat.isdigit() and int(chat) > 0 and chat == provider_id:
            return user
    return None


def suppression(order, config, now, *, event=None):
    if config.mode == OutreachSettings.Mode.OFF:
        return "disabled"
    hour = now.astimezone(ZoneInfo("Asia/Tehran")).hour
    if not config.start_hour <= hour < config.end_hour:
        return "quiet_hours"
    if OutreachPreference.objects.filter(store_id=order.store_id, customer_id=order.customer_id, muted=True).exists():
        return "muted"
    if SupportConversation.objects.filter(store_id=order.store_id, customer_id=order.customer_id,
                                           status__in=("open", "waiting_admin")).exists():
        return "open_support"
    previous = OutreachEvent.objects.filter(status__in=RESERVED)
    if event:
        previous = previous.exclude(pk=event.pk)
    personal = previous.filter(order__customer_id=order.customer_id)
    legacy = RevenueOfferLog.objects.filter(customer_id=order.customer_id, status__in=("sent", "converted"))
    if (personal.filter(claimed_at__gte=now - timedelta(hours=24)).exists()
            or legacy.filter(created_at__gte=now - timedelta(hours=24)).exists()
            or personal.filter(claimed_at__gte=now - timedelta(days=7)).count()
               + legacy.exclude(event_type="purchase_journey").filter(created_at__gte=now - timedelta(days=7)).count() >= 3):
        return "customer_cap"
    other_sent = RevenueOfferLog.objects.filter(store_id=order.store_id, status__in=("sent", "converted"),
                                                created_at__gte=now - timedelta(days=1)).exclude(event_type="purchase_journey").count()
    if previous.filter(order__store_id=order.store_id, claimed_at__gte=now - timedelta(days=1)).count() + other_sent >= config.daily_limit:
        return "store_cap"
    if not personal_target(order):
        return "no_target"
    return ""


def prepare_store(config, now):
    activities = PurchaseActivity.objects.filter(order__store=config.store).select_related("order__store", "order__plan")
    active = set()
    for activity in activities:
        reason, renewal = renewal_state(activity.order, now)
        if renewal:
            OutreachEvent.objects.filter(order=activity.order, converted_order__isnull=True).update(converted_order=renewal)
        kind = candidate_kind(activity, config, now)
        if not kind:
            continue
        event, _ = OutreachEvent.objects.get_or_create(order=activity.order, cycle=activity.cycle, kind=kind)
        active.add(event.pk)
        if event.status in PENDING:
            reason = suppression(activity.order, config, now, event=event)
            OutreachEvent.objects.filter(pk=event.pk, status__in=PENDING).update(
                status=OutreachEvent.Status.PREVIEW if config.mode != OutreachSettings.Mode.LIVE else OutreachEvent.Status.READY,
                body=message_body(activity, kind, now), reason=reason, updated_at=now,
            )
    OutreachEvent.objects.filter(order__store=config.store, status__in=PENDING).exclude(pk__in=active).update(
        status=OutreachEvent.Status.CANCELLED, reason="not_eligible", updated_at=now,
    )
    return active


@transaction.atomic
def reserve(event_id, now):
    snapshot = OutreachEvent.objects.select_related("order").get(pk=event_id)
    if not snapshot.order.customer_id:
        OutreachEvent.objects.filter(pk=event_id, status="ready").update(status="cancelled", reason="not_eligible")
        return None
    config = OutreachSettings.objects.select_for_update().get(store_id=snapshot.order.store_id)
    # Customer lock makes the personal cap apply across stores and simultaneous runners.
    Customer.objects.select_for_update().get(pk=snapshot.order.customer_id)
    event = select_for_update_self(OutreachEvent.objects.select_related("order__store", "order__plan")).get(pk=event_id)
    if config.mode != OutreachSettings.Mode.LIVE or event.status != OutreachEvent.Status.READY:
        return None
    activity = PurchaseActivity.objects.select_related("order__store", "order__plan").get(order=event.order)
    if activity.cycle != event.cycle or candidate_kind(activity, config, now) != event.kind:
        event.status, event.reason = OutreachEvent.Status.CANCELLED, "not_eligible"
        event.save(update_fields=["status", "reason", "updated_at"])
        return None
    reason = suppression(event.order, config, now, event=event)
    if reason:
        event.reason = reason
        event.save(update_fields=["reason", "updated_at"])
        return None
    event.bot_user = personal_target(event.order)
    event.body = message_body(activity, event.kind, now)
    event.claimed_at, event.status, event.reason = now, OutreachEvent.Status.SENDING, ""
    event.save()
    return event


def dispatch(event_id, *, now=None, client_class=BotClient):
    now = now or timezone.now()
    event = reserve(event_id, now)
    if not event:
        return False
    # Revalidate after the reservation commits, immediately before contacting Telegram.
    config = OutreachSettings.objects.get(store_id=event.order.store_id)
    activity = PurchaseActivity.objects.select_related("order__store", "order__plan").get(order_id=event.order_id)
    target = personal_target(activity.order)
    if (config.mode != OutreachSettings.Mode.LIVE or activity.cycle != event.cycle
            or candidate_kind(activity, config, timezone.now()) != event.kind
            or suppression(activity.order, config, timezone.now(), event=event)
            or not target or target.pk != event.bot_user_id):
        OutreachEvent.objects.filter(pk=event.pk, status="sending").update(status="cancelled", reason="not_eligible")
        return False
    try:
        result = client_class(target.bot_config).send_message(event.body, chat_id=target.chat_id,
                                                             reply_markup=keyboard(event), parse_mode="")
        message_id = (result.get("result") or {}).get("message_id") if isinstance(result, dict) else None
        if not message_id:
            raise BotDeliveryError("Missing delivery acknowledgement")
    except Exception as exc:
        rejected = getattr(exc, "error_code", None) in {400, 401, 403, 404, 429}
        OutreachEvent.objects.filter(pk=event.pk, status="sending").update(
            status="failed" if rejected else "uncertain", reason="telegram_rejected" if rejected else "transport_unknown", updated_at=timezone.now())
        return False
    with transaction.atomic():
        OutreachEvent.objects.filter(pk=event.pk, status="sending").update(status="sent", sent_at=timezone.now(), message_id=str(message_id), updated_at=timezone.now())
        # Legacy offer guards see this message too, preventing overlapping campaigns.
        RevenueOfferLog.objects.create(store_id=event.order.store_id, customer_id=event.order.customer_id,
            bot_user=target, engine_type="silent_active" if event.kind == "inactive_48h" else "renewal",
            event_type="purchase_journey", offer_type=event.kind, decision_source="rule", status="sent",
            sent_at=timezone.now(), metadata={"purchase_id": event.order_id, "journey_event_id": event.pk})
    return True


def run_outreach(*, store_id=None, now=None):
    now = now or timezone.now()
    OutreachEvent.objects.filter(status="sending", claimed_at__lt=now - timedelta(minutes=10)).update(
        status="uncertain", reason="worker_interrupted", updated_at=now,
    )
    stores = Store.objects.filter(is_active=True)
    if store_id:
        stores = stores.filter(pk=store_id)
    results = []
    for store in stores:
        config, _ = OutreachSettings.objects.get_or_create(store=store)
        ids = prepare_store(config, now)
        candidates = list(OutreachEvent.objects.filter(pk__in=ids, status="ready"))
        candidates.sort(key=lambda event: (-PRIORITY[event.kind], event.created_at, event.pk))
        sent = 0
        for event in candidates:
            sent += bool(dispatch(event.pk, now=timezone.now()))
        counts = dict(Counter(OutreachEvent.objects.filter(order__store=store).values_list("status", flat=True)))
        blocked = dict(Counter(OutreachEvent.objects.filter(pk__in=ids, status__in=PENDING).exclude(reason="").values_list("reason", flat=True)))
        kinds = dict(Counter(OutreachEvent.objects.filter(pk__in=ids).values_list("kind", flat=True)))
        summary = {"mode": config.mode, "eligible": len(ids), "sent_this_run": sent, "statuses": counts,
                   "blocked": blocked, "kinds": kinds}
        OutreachSettings.objects.filter(pk=config.pk).update(last_run_at=timezone.now(), summary=summary)
        results.append({"store_id": store.pk, **summary})
    return results
