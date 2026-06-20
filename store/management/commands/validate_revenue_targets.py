from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import RevenueOfferLog
from store.revenue_engine.actions import resolve_target
from store.telegram_bot.target_validation import validate_telegram_target


TEST_LIKE_MARKERS = ("admin", "test", "tester", "demo", "sandbox", "canary")
ERROR_REASONS = {"timeout", "api_error"}


class Command(BaseCommand):
    help = "Validate recent Revenue Engine Telegram targets with getChat only; never sends messages."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--only-dry-run-candidates",
            action="store_true",
            help="Safety flag for explicit dry-run candidate validation. This command only reads dry_run logs.",
        )
        parser.add_argument("--prefer-admin", action="store_true")
        parser.add_argument("--write-validation-metadata", action="store_true")
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, **options):
        limit = max(0, int(options["limit"] or 0))
        since = timezone.now() - timedelta(days=max(1, int(options["days"] or 1)))
        queryset = (
            RevenueOfferLog.objects.select_related("store", "customer", "bot_user", "bot_user__bot_config")
            .filter(created_at__gte=since, status=RevenueOfferLog.Status.DRY_RUN)
            .order_by("-created_at", "-pk")
        )
        total_candidates = queryset.count()
        candidates = list(queryset[:limit]) if limit else []
        if options["prefer_admin"]:
            candidates = self._prefer_admin(candidates)

        counts = Counter()
        by_reason = Counter()
        valid_candidate = None

        for log in candidates:
            if not log.customer_id or not log.bot_user_id:
                counts["missing_targets"] += 1
                by_reason["missing_target"] += 1
                self._write_validation_metadata(log, {"ok": False, "reason": "missing_target"}, options)
                continue

            counts["checked"] += 1
            target = resolve_target(
                log.bot_user,
                {
                    "bot_user": log.bot_user,
                    "customer": log.customer,
                    "store": log.store,
                    "event_type": log.event_type,
                },
            )
            if target:
                result = validate_telegram_target(
                    bot_user=log.bot_user,
                    chat_id=target.chat_id,
                    bot_config=target.bot_config,
                )
            else:
                result = {"ok": False, "reason": "missing_target", "safe_error": ""}

            reason = result.get("reason") or "api_error"
            by_reason[reason] += 1
            if result.get("ok"):
                counts["valid_targets"] += 1
                if valid_candidate is None:
                    valid_candidate = log
            elif reason == "missing_target":
                counts["missing_targets"] += 1
            else:
                counts["invalid_targets"] += 1
                if reason in ERROR_REASONS:
                    counts["timeout_api_error"] += 1

            self._write_validation_metadata(log, result, options)
            if options["verbose"]:
                self.stdout.write(
                    "candidate "
                    f"revenue_offer_log_pk={log.pk} customer_pk={log.customer_id} "
                    f"bot_user_pk={log.bot_user_id} reason={reason} ok={bool(result.get('ok'))}"
                )

        self.stdout.write("Revenue target validation summary:")
        self.stdout.write(f"total_candidates={total_candidates}")
        self.stdout.write(f"checked={counts['checked']}")
        self.stdout.write(f"valid_targets={counts['valid_targets']}")
        self.stdout.write(f"invalid_targets={counts['invalid_targets']}")
        self.stdout.write(f"missing_targets={counts['missing_targets']}")
        self.stdout.write(f"timeout_api_error={counts['timeout_api_error']}")
        breakdown = " ".join(f"{key}={value}" for key, value in sorted(by_reason.items())) or "none"
        self.stdout.write(f"by_reason {breakdown}")
        self.stdout.write(f"valid_canary_candidate_found={'yes' if valid_candidate else 'no'}")

        if valid_candidate:
            self.stdout.write("recommended_candidate:")
            self.stdout.write(f"revenue_offer_log_pk={valid_candidate.pk}")
            self.stdout.write(f"customer_pk={valid_candidate.customer_id}")
            self.stdout.write(f"bot_user_pk={valid_candidate.bot_user_id}")
            self.stdout.write(f"engine_type={valid_candidate.engine_type}")
            self.stdout.write(f"event_type={valid_candidate.event_type}")
            self.stdout.write(f"offer_type={valid_candidate.offer_type}")
            self.stdout.write(f"variant={valid_candidate.variant or 'none'}")
            self.stdout.write(f"decision_source={valid_candidate.decision_source}")
            self.stdout.write("target_validation_reason=valid")
            self.stdout.write("manual_approval_required=True")
        else:
            self.stdout.write("status=NOT_READY_NO_VALID_TARGET")

    def _prefer_admin(self, candidates):
        indexed = list(enumerate(candidates))
        indexed.sort(key=lambda item: (0 if self._is_admin_or_test_like(item[1]) else 1, item[0]))
        return [log for _, log in indexed]

    def _is_admin_or_test_like(self, log):
        bot_user = log.bot_user
        config = getattr(bot_user, "bot_config", None)
        if config and config.is_admin_user(getattr(bot_user, "chat_id", "")):
            return True
        values = [
            getattr(bot_user, "username", ""),
            getattr(bot_user, "display_name", ""),
            getattr(log.customer, "username", ""),
            getattr(log.customer, "display_name", ""),
        ]
        text = " ".join(str(value or "").casefold() for value in values)
        return any(marker in text for marker in TEST_LIKE_MARKERS)

    def _write_validation_metadata(self, log, result, options):
        if not options["write_validation_metadata"]:
            return
        metadata = dict(log.metadata or {})
        metadata["target_validation"] = {
            "checked_at": timezone.now().isoformat(),
            "ok": bool(result.get("ok")),
            "reason": str(result.get("reason") or "api_error"),
            "source": "validate_revenue_targets",
        }
        log.metadata = metadata
        log.save(update_fields=["metadata"])
