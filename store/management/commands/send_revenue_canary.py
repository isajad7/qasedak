from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from store.models import BotConfiguration, Order, RevenueOfferLog, Store, VPNClient
from store.revenue_engine.actions import resolve_target
from store.revenue_engine.guards import (
    can_send_revenue_offer,
    decision_source,
    offer_type_from_decision,
    record_revenue_offer_attempt,
    sanitize_revenue_metadata,
    variant_from_decision,
)
from store.revenue_engine.retention.actions import execute as execute_retention
from store.revenue_engine.retention.engine import RetentionEngine
from store.revenue_engine.retention.rules import RetentionRuleEngine
from store.telegram_bot.target_validation import validate_telegram_target


CONFIRM_TEXT = "SEND_ONE_REVENUE_CANARY"
CANARY_COMMAND_VERSION = "2026-06-20-idempotent-v1"
CANARY_COOLDOWN_HOURS = 24
CANARY_FINAL_STATUSES = (
    RevenueOfferLog.Status.SENT,
    RevenueOfferLog.Status.FAILED,
    RevenueOfferLog.Status.SKIPPED,
)


class Command(BaseCommand):
    help = "Send exactly one guarded Revenue Engine canary offer from a dry-run RevenueOfferLog."

    def add_arguments(self, parser):
        parser.add_argument("--offer-log-id", type=int, required=True)
        parser.add_argument("--customer-id", type=int, required=True)
        parser.add_argument("--bot-user-id", type=int, required=True)
        parser.add_argument("--confirm", required=True)
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, **options):
        if options["confirm"] != CONFIRM_TEXT:
            raise CommandError(f"Refusing canary send without --confirm {CONFIRM_TEXT}")

        source_log = RevenueOfferLog.objects.select_related("store", "customer", "bot_user", "bot_user__bot_config").get(
            pk=options["offer_log_id"]
        )
        self._validate_source_log(source_log, options)
        store = source_log.store or Store.objects.filter(is_active=True).order_by("pk").first()
        self._validate_store(store)
        self._validate_source_not_previously_canaried(source_log)
        self._validate_global_canary_cooldown()
        self._validate_no_recent_real_send(source_log)

        user = source_log.bot_user
        context = self._build_context(source_log, store)
        decision = self._build_decision(source_log, user, context)
        self._validate_decision(source_log, decision)

        target = resolve_target(user, context)
        if not self._target_is_sendable(target):
            log = record_revenue_offer_attempt(
                user=user,
                context=context,
                engine_type=source_log.engine_type,
                event_type=source_log.event_type,
                decision=decision,
                status=RevenueOfferLog.Status.SKIPPED,
                skip_reason="no_personal_telegram_target",
                store=store,
                metadata=self._metadata(source_log),
            )
            self._mark_source_canary_failed(source_log, log)
            raise CommandError("Canary target is not a valid active Telegram personal target.")

        target_validation = validate_telegram_target(
            bot_user=user,
            chat_id=target.chat_id,
            bot_config=target.bot_config,
        )
        if not target_validation.get("ok"):
            reason = target_validation.get("reason") or "api_error"
            log = record_revenue_offer_attempt(
                user=user,
                context=context,
                engine_type=source_log.engine_type,
                event_type=source_log.event_type,
                decision=decision,
                status=RevenueOfferLog.Status.FAILED,
                error_message=f"telegram_target_invalid: {reason}",
                store=store,
                metadata=self._metadata(
                    source_log,
                    target_validation={
                        "ok": False,
                        "reason": reason,
                        "source": "send_revenue_canary",
                    },
                ),
            )
            self._mark_source_canary_failed(source_log, log)
            raise CommandError(f"Canary Telegram target validation failed: {reason}")

        guard = can_send_revenue_offer(
            source_log.customer,
            source_log.engine_type,
            source_log.event_type,
            store=store,
            bot_user=source_log.bot_user,
            target=target,
            decision=decision,
            allow_canary_send=True,
        )
        if not guard.allowed:
            log = record_revenue_offer_attempt(
                user=user,
                context=context,
                engine_type=source_log.engine_type,
                event_type=source_log.event_type,
                decision=decision,
                status=guard.status,
                skip_reason=guard.reason,
                store=store,
                metadata=self._metadata(source_log),
            )
            self._mark_source_canary_failed(source_log, log)
            raise CommandError(f"Canary suppressed by guard: {guard.reason}")

        result = execute_retention(user, decision, context)

        if result.get("sent"):
            sent_log_id = result.get("revenue_offer_log_id")
            self._mark_source_canary_sent(source_log, sent_log_id)
            self.stdout.write(
                "Revenue canary sent: "
                f"source_log={source_log.pk} sent_log={sent_log_id} "
                f"customer_id={source_log.customer_id} bot_user_id={source_log.bot_user_id} "
                f"engine={source_log.engine_type} event={source_log.event_type} variant={source_log.variant or 'none'}"
            )
            return

        reason = result.get("reason") or "not_sent"
        log_id = result.get("revenue_offer_log_id") or "none"
        self._mark_source_canary_failed(source_log, log_id if log_id != "none" else None)
        raise CommandError(f"Revenue canary was not sent: reason={reason} log_id={log_id}")

    def _validate_source_log(self, source_log, options):
        expected = {
            "customer_id": options["customer_id"],
            "bot_user_id": options["bot_user_id"],
            "engine_type": RevenueOfferLog.EngineType.RETENTION,
            "event_type": "user_inactive_72h",
            "offer_type": "retention",
            "variant": "AI",
            "status": RevenueOfferLog.Status.DRY_RUN,
        }
        actual = {
            "customer_id": source_log.customer_id,
            "bot_user_id": source_log.bot_user_id,
            "engine_type": source_log.engine_type,
            "event_type": source_log.event_type,
            "offer_type": source_log.offer_type,
            "variant": source_log.variant,
            "status": source_log.status,
        }
        mismatches = [key for key, value in expected.items() if actual.get(key) != value]
        if mismatches:
            details = ", ".join(f"{key}={actual.get(key)!r}" for key in mismatches)
            raise CommandError(f"Canary source log does not match the approved candidate: {details}")
        if source_log.decision_source != RevenueOfferLog.DecisionSource.AI:
            raise CommandError("Canary source log is not an AI decision.")
        if not source_log.customer_id or not source_log.bot_user_id:
            raise CommandError("Canary source log must have customer and bot_user.")

        metadata = source_log.metadata or {}
        if metadata.get("canary_sent_log_id") or metadata.get("canary_failed_log_id") or metadata.get(
            "canary_skipped_log_id"
        ):
            raise CommandError("Canary source log was already attempted and requires manual reset before retry.")

    def _validate_store(self, store):
        if not store:
            raise CommandError("No Store settings found for canary.")
        if not store.revenue_engine_enabled:
            raise CommandError("Revenue Engine is disabled.")
        if not store.revenue_engine_dry_run:
            raise CommandError("Refusing canary because Store dry_run is not enabled.")
        if not store.retention_engine_enabled:
            raise CommandError("Retention engine is disabled.")

    def _validate_source_not_previously_canaried(self, source_log):
        existing = (
            RevenueOfferLog.objects.filter(
                status__in=CANARY_FINAL_STATUSES,
                metadata__canary=True,
                metadata__source_dry_run_log_id=source_log.pk,
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if existing:
            raise CommandError(
                "Refusing canary because this dry-run source already has a canary attempt: "
                f"log_id={existing.pk} status={existing.status}"
            )

    def _validate_global_canary_cooldown(self):
        since = timezone.now() - timedelta(hours=CANARY_COOLDOWN_HOURS)
        existing = (
            RevenueOfferLog.objects.filter(
                status=RevenueOfferLog.Status.SENT,
                created_at__gte=since,
                metadata__canary=True,
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if existing:
            raise CommandError(
                "Refusing canary because another canary was sent within the last "
                f"{CANARY_COOLDOWN_HOURS} hours: log_id={existing.pk}"
            )

    def _validate_no_recent_real_send(self, source_log):
        since = timezone.now() - timedelta(hours=24)
        exists = RevenueOfferLog.objects.filter(
            customer_id=source_log.customer_id,
            created_at__gte=since,
            status__in=[RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED],
        ).exists()
        if exists:
            raise CommandError("Refusing canary because this customer has a recent real sent offer.")

    def _build_context(self, source_log, store):
        customer = source_log.customer
        bot_user = source_log.bot_user
        return {
            "bot_user": bot_user,
            "customer": customer,
            "store": store,
            "event_type": source_log.event_type,
            "last_active_at": getattr(bot_user, "last_seen_at", None),
            "last_purchase": self._last_purchase(customer),
            "subscription_active": self._active_subscription_exists(customer),
            "revenue_canary_send": True,
            "source_dry_run_log_id": source_log.pk,
            "canary_command_version": CANARY_COMMAND_VERSION,
        }

    def _last_purchase(self, customer):
        if not customer:
            return None
        return (
            Order.objects.filter(customer=customer, status=Order.Status.COMPLETED)
            .order_by("-created_at", "-pk")
            .first()
        )

    def _active_subscription_exists(self, customer):
        if not customer:
            return False
        return VPNClient.objects.filter(
            order__customer=customer,
            expires_at__gt=timezone.now(),
            status__in=[VPNClient.Status.CREATED, VPNClient.Status.INACTIVE, VPNClient.Status.ACTIVE],
        ).exists()

    def _build_decision(self, source_log, user, context):
        base = RetentionRuleEngine().evaluate(source_log.event_type, user, context)
        if not base:
            raise CommandError("Retention rule engine no longer produces a decision for this canary event.")
        decision = RetentionEngine()._optimize_offer(base, user, context)
        if not decision or decision.get("skip_offer"):
            raise CommandError(f"Optimized canary decision is not sendable: {decision.get('skip_reason', 'skip_offer')}")
        return decision

    def _validate_decision(self, source_log, decision):
        if offer_type_from_decision(decision) != source_log.offer_type:
            raise CommandError("Current decision offer_type no longer matches the dry-run source log.")
        if variant_from_decision(decision) != source_log.variant:
            raise CommandError("Current decision variant no longer matches the dry-run source log.")
        if decision_source(decision) != source_log.decision_source:
            raise CommandError("Current decision source no longer matches the dry-run source log.")

    def _target_is_sendable(self, target):
        if not target or not target.chat_id or not target.bot_config:
            return False
        return bool(
            target.bot_config.is_active
            and target.bot_config.provider == BotConfiguration.Provider.TELEGRAM
            and target.bot_config.bot_token
        )

    def _metadata(self, source_log, *, target_validation=None):
        metadata = {
            "canary": True,
            "source_dry_run_log_id": source_log.pk,
            "canary_command": "send_revenue_canary",
            "canary_command_version": CANARY_COMMAND_VERSION,
        }
        if target_validation:
            metadata["target_validation"] = target_validation
        return metadata

    def _update_source_canary_metadata(self, source_log, **values):
        metadata = dict(source_log.metadata or {})
        metadata.update(values)
        source_log.metadata = sanitize_revenue_metadata(metadata)
        source_log.save(update_fields=["metadata"])

    def _mark_source_canary_sent(self, source_log, sent_log_id):
        self._update_source_canary_metadata(
            source_log,
            canary_sent_log_id=sent_log_id,
            canary_sent_at=timezone.now().isoformat(),
            canary_command_version=CANARY_COMMAND_VERSION,
        )

    def _mark_source_canary_failed(self, source_log, log_or_id):
        log_id = getattr(log_or_id, "pk", log_or_id)
        values = {
            "canary_failed_at": timezone.now().isoformat(),
            "canary_command_version": CANARY_COMMAND_VERSION,
        }
        if log_id:
            values["canary_failed_log_id"] = log_id
        self._update_source_canary_metadata(source_log, **values)
