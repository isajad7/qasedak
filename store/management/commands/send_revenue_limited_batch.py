import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
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


CONFIRM_TEXT = "SEND_LIMITED_REVENUE_BATCH"
BATCH_COMMAND_VERSION = "2026-06-20-limited-batch-v2"
MAX_BATCH_LIMIT = 3
BATCH_DELAY_SECONDS = 7
DEFAULT_ALL_LIMIT_CAP = 100
RECENT_REAL_SEND_DAYS = 7
FINAL_ATTEMPT_STATUSES = (
    RevenueOfferLog.Status.SENT,
    RevenueOfferLog.Status.FAILED,
    RevenueOfferLog.Status.SKIPPED,
)
SOURCE_ATTEMPT_KEYS = (
    "canary_sent_log_id",
    "canary_failed_log_id",
    "canary_skipped_log_id",
    "limited_batch_sent_log_id",
    "limited_batch_failed_log_id",
    "limited_batch_skipped_log_id",
)
SENT_OR_SKIPPED_SOURCE_ATTEMPT_KEYS = (
    "canary_sent_log_id",
    "canary_skipped_log_id",
    "limited_batch_sent_log_id",
    "limited_batch_skipped_log_id",
)
FAILED_SOURCE_ATTEMPT_KEYS = (
    "canary_failed_log_id",
    "limited_batch_failed_log_id",
)
TRANSIENT_FAILURE_TERMS = (
    "timeout",
    "timed out",
    "read timed out",
    "proxy",
    "connection aborted",
    "connection reset",
    "connection refused",
    "remote disconnected",
    "temporarily unavailable",
    "502",
    "503",
    "504",
)
RATE_LIMIT_FAILURE_TERMS = (
    "429",
    "too many requests",
    "retry after",
)
TARGET_INVALID_FAILURE_TERMS = (
    "telegram_target_invalid",
    "target_invalid",
    "chat_not_found",
    "chat not found",
    "bot_blocked",
    "bot was blocked",
    "blocked by the user",
    "invalid_chat_id",
)
SENSITIVE_DELIVERY_LOGGERS = (
    "store.telegram_bot.notifications",
)


@dataclass
class Candidate:
    log: RevenueOfferLog
    decision: dict


class Command(BaseCommand):
    help = "Send a guarded, limited real Revenue Engine batch while Store dry-run stays enabled."

    def add_arguments(self, parser):
        parser.add_argument("--engine", choices=("retention",), default="retention")
        parser.add_argument("--event", default="user_inactive_72h")
        parser.add_argument("--limit", default=str(MAX_BATCH_LIMIT))
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--preview", action="store_true")
        parser.add_argument("--confirm", default="")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--retry-transient-failed", action="store_true")

    def handle(self, *args, **options):
        if options["engine"] != RevenueOfferLog.EngineType.RETENTION:
            raise CommandError("Limited batch currently supports retention only.")
        if options["event"] != "user_inactive_72h":
            raise CommandError("Limited batch currently supports user_inactive_72h only.")

        days = max(int(options["days"] or 1), 1)
        preview = bool(options["preview"])
        verbose = bool(options["verbose"])
        if not preview and options.get("confirm") != CONFIRM_TEXT:
            raise CommandError(f"Refusing limited batch without --confirm {CONFIRM_TEXT}")

        store = Store.objects.filter(is_active=True).order_by("pk").first()
        self._validate_store(store)
        limit, limit_mode, daily_cap_configured = self._resolve_limit(options["limit"], store)
        batch_id = f"limited-{int(timezone.now().timestamp()):x}"
        started_at = timezone.now()
        summary, selected = self._select_candidates(
            store=store,
            engine=options["engine"],
            event=options["event"],
            limit=limit,
            limit_mode=limit_mode,
            daily_cap_configured=daily_cap_configured,
            days=days,
            verbose=verbose,
            retry_transient_failed=bool(options["retry_transient_failed"]),
        )

        self._print_selection_summary(summary, selected, batch_id=batch_id, preview=preview)
        if preview:
            if not selected:
                self.stdout.write("status=NOT_READY_NO_BATCH_CANDIDATES")
            else:
                self.stdout.write("status=PREVIEW_OK")
            return
        if not selected:
            self.stdout.write("status=NOT_READY_NO_BATCH_CANDIDATES")
            return

        self._suppress_sensitive_delivery_logs()
        sent_log_ids = []
        failed_log_ids = []
        skipped_log_ids = []
        send_attempts = 0
        consecutive_transient_failures = 0
        stopped_reason = ""
        for candidate in selected:
            if send_attempts:
                time.sleep(BATCH_DELAY_SECONDS)
            store.refresh_from_db()
            self._validate_store(store)
            recheck_reason = self._recheck_candidate(candidate.log, store, candidate.decision)
            if recheck_reason:
                summary[f"skipped_{recheck_reason}"] += 1
                if recheck_reason == "invalid_target":
                    skipped_log_id = self._record_limited_batch_skipped(
                        candidate.log,
                        candidate.decision,
                        store,
                        batch_id,
                        recheck_reason,
                    )
                    skipped_log_ids.append(skipped_log_id)
                    self._mark_source_skipped(candidate.log, skipped_log_id, batch_id)
                if recheck_reason == "daily_total_cap":
                    stopped_reason = recheck_reason
                    break
                if verbose:
                    self.stdout.write(f"skip offer_log_id={candidate.log.pk} reason={recheck_reason}")
                continue

            send_attempts += 1
            result = execute_retention(
                candidate.log.bot_user,
                candidate.decision,
                self._build_context(candidate.log, store, batch_id),
            )
            log_id = result.get("revenue_offer_log_id")
            if result.get("sent"):
                sent_log_ids.append(log_id)
                self._mark_source_sent(candidate.log, log_id, batch_id)
                consecutive_transient_failures = 0
                if verbose:
                    self.stdout.write(
                        "sent "
                        f"source_log_id={candidate.log.pk} sent_log_id={log_id}"
                    )
                continue

            reason_text = self._result_failure_text(result, candidate.log)
            if result.get("failed"):
                failed_log_ids.append(log_id)
                self._mark_source_failed(candidate.log, log_id, batch_id)
            else:
                skipped_log_ids.append(log_id)
                self._mark_source_skipped(candidate.log, log_id, batch_id)
            if verbose:
                self.stdout.write(
                    "not_sent "
                    f"source_log_id={candidate.log.pk} log_id={log_id or 'none'} "
                    f"reason={self._safe_output_text(reason_text)}"
                )
            failure_kind = self._classify_failure_text(reason_text)
            if failure_kind == "rate_limit":
                stopped_reason = "rate_limited"
                break
            if failure_kind == "transient":
                consecutive_transient_failures += 1
                if consecutive_transient_failures >= 3:
                    stopped_reason = "transient_failures"
                    break
                continue
            consecutive_transient_failures = 0
            if failure_kind == "target_invalid":
                continue
            stopped_reason = result.get("reason") or "send_failed"
            break

        if len(sent_log_ids) > limit:
            self._kill_switch()
            raise CommandError("Limited batch exceeded selected send cap; kill switch executed.")

        after_sent_count = RevenueOfferLog.objects.filter(
            created_at__gte=started_at,
            status=RevenueOfferLog.Status.SENT,
            metadata__limited_batch=True,
            metadata__batch_id=batch_id,
        ).count()
        self.stdout.write("Limited revenue batch result:")
        self.stdout.write(f"batch_id={batch_id}")
        self.stdout.write(f"sent_count={len(sent_log_ids)}")
        self.stdout.write(f"sent_log_ids={sent_log_ids}")
        self.stdout.write(f"failed_count={len([item for item in failed_log_ids if item])}")
        self.stdout.write(f"failed_log_ids={failed_log_ids}")
        self.stdout.write(f"skipped_count={len([item for item in skipped_log_ids if item])}")
        self.stdout.write(f"skipped_log_ids={skipped_log_ids}")
        self.stdout.write(f"stopped_reason={stopped_reason or 'none'}")
        self.stdout.write(f"db_limited_batch_sent_count={after_sent_count}")
        self.stdout.write(f"store_dry_run={Store.objects.filter(revenue_engine_dry_run=True).exists()}")
        if failed_log_ids or stopped_reason:
            self.stdout.write("status=LIMITED_BATCH_FAILED")
        elif sent_log_ids:
            self.stdout.write("status=LIMITED_BATCH_OK")
        else:
            self.stdout.write("status=NOT_READY_NO_BATCH_CANDIDATES")

    def _resolve_limit(self, raw_limit, store):
        daily_cap_configured = self._configured_daily_cap(store)
        daily_remaining = self._daily_remaining_cap(store, daily_cap_configured)
        raw = str(raw_limit or "").strip().lower()
        if raw == "all":
            return daily_remaining, "all", daily_cap_configured
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            raise CommandError("--limit must be a positive integer or 'all'.") from None
        requested = min(max(requested, 1), MAX_BATCH_LIMIT)
        return min(requested, daily_remaining), "numeric", daily_cap_configured

    def _configured_daily_cap(self, store):
        configured = getattr(store, "revenue_max_total_offers_per_day", None)
        try:
            configured = int(configured or 0)
        except (TypeError, ValueError):
            configured = 0
        return configured if configured > 0 else DEFAULT_ALL_LIMIT_CAP

    def _daily_remaining_cap(self, store, daily_cap):
        since = timezone.now() - timedelta(days=1)
        sent_today = RevenueOfferLog.objects.filter(
            created_at__gte=since,
            status__in=[RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED],
        )
        if store:
            sent_today = sent_today.filter(Q(store=store) | Q(store__isnull=True))
        return max(int(daily_cap or 0) - sent_today.count(), 0)

    def _select_candidates(
        self,
        *,
        store,
        engine,
        event,
        limit,
        limit_mode,
        daily_cap_configured,
        days,
        verbose,
        retry_transient_failed,
    ):
        since = timezone.now() - timedelta(days=days)
        queryset = (
            RevenueOfferLog.objects.select_related("store", "customer", "bot_user", "bot_user__bot_config")
            .filter(
                created_at__gte=since,
                status=RevenueOfferLog.Status.DRY_RUN,
                engine_type=engine,
                event_type=event,
            )
            .order_by("-created_at", "-pk")
        )
        summary = Counter()
        summary["cap_used"] = limit
        summary["daily_cap_configured"] = daily_cap_configured
        summary["limit_mode_all"] = 1 if limit_mode == "all" else 0
        summary["candidates_found"] = queryset.count()
        selected = []
        for log in queryset.iterator(chunk_size=100):
            summary["checked"] += 1
            reason, decision = self._candidate_skip_reason(
                log,
                store,
                retry_transient_failed=retry_transient_failed,
            )
            if reason:
                summary[f"skipped_{reason}"] += 1
                if verbose:
                    self.stdout.write(f"skip offer_log_id={log.pk} reason={reason}")
                continue
            summary["valid_targets"] += 1
            if len(selected) < limit:
                selected.append(Candidate(log=log, decision=decision))
        summary["not_selected_due_to_cap"] = max(summary["valid_targets"] - len(selected), 0)
        summary["selected_count"] = len(selected)
        summary["estimated_real_sends"] = len(selected)
        return summary, selected

    def _candidate_skip_reason(self, log, store, *, retry_transient_failed=False):
        if not log.customer_id or not log.bot_user_id:
            return "missing_target", None
        attempt_reason = self._source_attempt_skip_reason(
            log,
            retry_transient_failed=retry_transient_failed,
        )
        if attempt_reason:
            return attempt_reason, None
        if self._customer_has_recent_real_sent(log.customer_id):
            return "recent_sent", None

        target = resolve_target(log.bot_user, self._build_context(log, store, "preview"))
        if not self._target_is_sendable(target):
            return "invalid_target", None
        validation = validate_telegram_target(
            bot_user=log.bot_user,
            chat_id=target.chat_id,
            bot_config=target.bot_config,
        )
        if not validation.get("ok"):
            return "invalid_target", None

        try:
            decision = self._build_decision(log, store)
        except CommandError:
            return "decision_mismatch", None
        guard = can_send_revenue_offer(
            log.customer,
            log.engine_type,
            log.event_type,
            store=store,
            bot_user=log.bot_user,
            target=target,
            decision=decision,
        )
        if guard.reason != "dry_run":
            return guard.reason or "guard_blocked", None
        return "", decision

    def _recheck_candidate(self, log, store, decision):
        if self._source_attempt_skip_reason(log):
            return "existing_attempt"
        if self._customer_has_recent_real_sent(log.customer_id):
            return "recent_sent"
        target = resolve_target(log.bot_user, self._build_context(log, store, "recheck"))
        if not self._target_is_sendable(target):
            return "invalid_target"
        validation = validate_telegram_target(
            bot_user=log.bot_user,
            chat_id=target.chat_id,
            bot_config=target.bot_config,
        )
        if not validation.get("ok"):
            return "invalid_target"
        guard = can_send_revenue_offer(
            log.customer,
            log.engine_type,
            log.event_type,
            store=store,
            bot_user=log.bot_user,
            target=target,
            decision=decision,
        )
        if guard.reason != "dry_run":
            return guard.reason or "guard_blocked"
        return ""

    def _validate_store(self, store):
        if not store:
            raise CommandError("No active Store settings found for limited batch.")
        if not store.revenue_engine_enabled:
            raise CommandError("Revenue Engine is disabled.")
        if not store.revenue_engine_dry_run:
            raise CommandError("Refusing limited batch because Store dry_run is not enabled.")
        if not store.retention_engine_enabled:
            raise CommandError("Retention engine is disabled.")

    def _source_attempt_skip_reason(self, log, *, retry_transient_failed=False):
        metadata = log.metadata or {}
        if any(metadata.get(key) for key in SENT_OR_SKIPPED_SOURCE_ATTEMPT_KEYS):
            return "existing_attempt"
        if RevenueOfferLog.objects.filter(
            status__in=[RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED],
            metadata__source_dry_run_log_id=log.pk,
        ).exists():
            return "existing_attempt"

        attempts = list(
            RevenueOfferLog.objects.filter(
                status__in=FINAL_ATTEMPT_STATUSES,
                metadata__source_dry_run_log_id=log.pk,
            )
            .filter(Q(metadata__canary=True) | Q(metadata__limited_batch=True))
            .order_by("-created_at", "-pk")
        )
        metadata_failed_ids = [metadata.get(key) for key in FAILED_SOURCE_ATTEMPT_KEYS if metadata.get(key)]
        if metadata_failed_ids:
            attempts.extend(
                RevenueOfferLog.objects.filter(
                    pk__in=metadata_failed_ids,
                    status=RevenueOfferLog.Status.FAILED,
                )
            )
        attempts_by_id = {attempt.pk: attempt for attempt in attempts}
        attempts = list(attempts_by_id.values())
        if any(attempt.status in {RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.SKIPPED} for attempt in attempts):
            return "existing_attempt"
        failed_attempts = [attempt for attempt in attempts if attempt.status == RevenueOfferLog.Status.FAILED]
        if failed_attempts:
            if retry_transient_failed and all(self._failed_attempt_is_transient(attempt) for attempt in failed_attempts):
                return ""
            return "existing_attempt"
        if any(metadata.get(key) for key in SOURCE_ATTEMPT_KEYS):
            return "existing_attempt"
        return ""

    def _failed_attempt_is_transient(self, attempt):
        text = " ".join(
            str(part or "")
            for part in (
                getattr(attempt, "skip_reason", ""),
                getattr(attempt, "error_message", ""),
                (attempt.metadata or {}).get("reason", ""),
                (attempt.metadata or {}).get("safe_error", ""),
            )
        )
        return self._classify_failure_text(text) == "transient"

    def _customer_has_recent_real_sent(self, customer_id):
        since = timezone.now() - timedelta(days=RECENT_REAL_SEND_DAYS)
        return RevenueOfferLog.objects.filter(
            customer_id=customer_id,
            created_at__gte=since,
            status__in=[RevenueOfferLog.Status.SENT, RevenueOfferLog.Status.CONVERTED],
        ).exists()

    def _target_is_sendable(self, target):
        if not target or not target.chat_id or not target.bot_config:
            return False
        return bool(
            target.bot_config.is_active
            and target.bot_config.provider == BotConfiguration.Provider.TELEGRAM
            and target.bot_config.bot_token
        )

    def _build_context(self, source_log, store, batch_id):
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
            "revenue_limited_batch_send": True,
            "source_dry_run_log_id": source_log.pk,
            "limited_batch_id": batch_id,
            "batch_command_version": BATCH_COMMAND_VERSION,
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

    def _build_decision(self, source_log, store):
        user = source_log.bot_user
        context = self._build_context(source_log, store, "decision")
        base = RetentionRuleEngine().evaluate(source_log.event_type, user, context)
        if not base:
            raise CommandError("Retention rule engine no longer produces a decision for this candidate.")
        decision = RetentionEngine()._optimize_offer(base, user, context)
        if not decision or decision.get("skip_offer"):
            raise CommandError("Optimized limited batch decision is not sendable.")
        if offer_type_from_decision(decision) != source_log.offer_type:
            raise CommandError("Decision offer_type mismatch.")
        if variant_from_decision(decision) != source_log.variant:
            raise CommandError("Decision variant mismatch.")
        if decision_source(decision) != source_log.decision_source:
            raise CommandError("Decision source mismatch.")
        return decision

    def _mark_source_sent(self, source_log, sent_log_id, batch_id):
        self._update_source_metadata(
            source_log,
            limited_batch_sent_log_id=sent_log_id,
            limited_batch_sent_at=timezone.now().isoformat(),
            limited_batch_id=batch_id,
            batch_command_version=BATCH_COMMAND_VERSION,
        )

    def _mark_source_failed(self, source_log, failed_log_id, batch_id):
        values = {
            "limited_batch_failed_at": timezone.now().isoformat(),
            "limited_batch_id": batch_id,
            "batch_command_version": BATCH_COMMAND_VERSION,
        }
        if failed_log_id:
            values["limited_batch_failed_log_id"] = failed_log_id
        self._update_source_metadata(source_log, **values)

    def _mark_source_skipped(self, source_log, skipped_log_id, batch_id):
        values = {
            "limited_batch_skipped_at": timezone.now().isoformat(),
            "limited_batch_id": batch_id,
            "batch_command_version": BATCH_COMMAND_VERSION,
        }
        if skipped_log_id:
            values["limited_batch_skipped_log_id"] = skipped_log_id
        self._update_source_metadata(source_log, **values)

    def _record_limited_batch_skipped(self, source_log, decision, store, batch_id, reason):
        log = record_revenue_offer_attempt(
            user=source_log.bot_user,
            context=self._build_context(source_log, store, batch_id),
            engine_type=source_log.engine_type,
            event_type=source_log.event_type,
            decision=decision,
            status=RevenueOfferLog.Status.SKIPPED,
            skip_reason=reason,
            store=store,
            metadata={
                "limited_batch": True,
                "batch_id": batch_id,
                "source_dry_run_log_id": source_log.pk,
                "batch_command_version": BATCH_COMMAND_VERSION,
            },
        )
        return log.pk

    def _update_source_metadata(self, source_log, **values):
        metadata = dict(source_log.metadata or {})
        metadata.update(values)
        source_log.metadata = sanitize_revenue_metadata(metadata)
        source_log.save(update_fields=["metadata"])

    def _print_selection_summary(self, summary, selected, *, batch_id, preview):
        self.stdout.write("Limited revenue batch preview:" if preview else "Limited revenue batch selection:")
        self.stdout.write(f"batch_id={batch_id}")
        for key in (
            "limit_mode_all",
            "daily_cap_configured",
            "cap_used",
            "candidates_found",
            "checked",
            "valid_targets",
            "skipped_recent_sent",
            "skipped_invalid_target",
            "skipped_existing_attempt",
            "skipped_missing_target",
            "skipped_decision_mismatch",
            "skipped_cooldown_active",
            "skipped_daily_user_cap",
            "skipped_weekly_user_cap",
            "skipped_daily_total_cap",
            "not_selected_due_to_cap",
            "selected_count",
            "estimated_real_sends",
        ):
            self.stdout.write(f"{key}={summary[key]}")
        self.stdout.write(f"selected_offer_log_ids={[item.log.pk for item in selected]}")

    def _result_failure_text(self, result, source_log=None):
        reason = str((result or {}).get("reason") or "")
        log_id = (result or {}).get("revenue_offer_log_id")
        if log_id:
            log = RevenueOfferLog.objects.filter(pk=log_id).only("skip_reason", "error_message").first()
            if log:
                reason = " ".join(part for part in (reason, log.skip_reason, log.error_message) if part)
        bot_config_id = getattr(getattr(source_log, "bot_user", None), "bot_config_id", None)
        if bot_config_id:
            config = BotConfiguration.objects.filter(pk=bot_config_id).only("last_error").first()
            if config and config.last_error:
                reason = " ".join(part for part in (reason, config.last_error) if part)
        return reason or "not_sent"

    def _classify_failure_text(self, text):
        lowered = str(text or "").casefold()
        if any(term in lowered for term in RATE_LIMIT_FAILURE_TERMS):
            return "rate_limit"
        if any(term in lowered for term in TARGET_INVALID_FAILURE_TERMS):
            return "target_invalid"
        if any(term in lowered for term in TRANSIENT_FAILURE_TERMS):
            return "transient"
        return "other"

    def _safe_output_text(self, text):
        return sanitize_revenue_metadata({"message": text}).get("message", "")

    def _suppress_sensitive_delivery_logs(self):
        for logger_name in SENSITIVE_DELIVERY_LOGGERS:
            logging.getLogger(logger_name).setLevel(logging.ERROR)

    def _kill_switch(self):
        Store.objects.update(revenue_engine_enabled=False, revenue_engine_dry_run=True)
