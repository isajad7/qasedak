from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import BotEventLog, Inbound, Panel
from .panels import get_safe_panel_adapter
from .panels.errors import PanelIntegrationError, sanitize_error_value


VERIFICATION_METHOD_PROVISIONING_PROBE = "provisioning_probe"
SOURCE_VERIFICATION_STALE_DAYS = 7


@dataclass(frozen=True)
class SourceSellabilityVerificationResult:
    source_id: int | None
    panel_id: int | None
    ok: bool
    verification_status: str
    verification_method: str = VERIFICATION_METHOD_PROVISIONING_PROBE
    observed_config_count: int = 0
    protocol_counts: dict = field(default_factory=dict)
    reality_count: int = 0
    pbk_validation_ok: bool = False
    cleanup_succeeded: bool = False
    error_code: str = ""
    warnings: list[str] = field(default_factory=list)
    safe_details: dict = field(default_factory=dict)

    def to_safe_dict(self):
        return {
            "source_id": self.source_id,
            "panel_id": self.panel_id,
            "ok": self.ok,
            "verification_status": self.verification_status,
            "verification_method": self.verification_method,
            "observed_config_count": self.observed_config_count,
            "protocol_counts": sanitize_error_value(self.protocol_counts),
            "reality_count": self.reality_count,
            "pbk_validation_ok": self.pbk_validation_ok,
            "cleanup_succeeded": self.cleanup_succeeded,
            "error_code": sanitize_error_value(self.error_code),
            "warnings": sanitize_error_value(self.warnings),
            "safe_details": sanitize_error_value(self.safe_details),
        }


def is_pasarguard_group_source(source):
    panel = getattr(source, "panel", None)
    return bool(
        source
        and str(getattr(panel, "family", "") or "").lower() == Panel.Family.PASARGUARD
        and getattr(source, "xui_source", "") == Inbound.XUISource.PASARGUARD_GROUP
    )


def source_requires_sellability_verification(source):
    return bool(getattr(source, "requires_sellability_verification", False))


def source_sellability_is_stale(source, *, now=None):
    if not source or getattr(source, "verification_status", "") != Inbound.VerificationStatus.VERIFIED_SELLABLE:
        return False
    verified_at = getattr(source, "verified_at", None)
    if not verified_at:
        return False
    now = now or timezone.now()
    return verified_at <= now - timedelta(days=SOURCE_VERIFICATION_STALE_DAYS)


def source_verification_ui_state(source, *, now=None):
    status = getattr(source, "verification_status", Inbound.VerificationStatus.UNVERIFIED)
    if status == Inbound.VerificationStatus.VERIFIED_SELLABLE:
        if source_sellability_is_stale(source, now=now):
            return {
                "code": "stale",
                "label": "VERIFIED / STALE",
                "tone": "amber",
                "action_label": "Verify Again",
                "blocking": False,
            }
        return {
            "code": status,
            "label": "VERIFIED FOR SALE",
            "tone": "emerald",
            "action_label": "Verify Again",
            "blocking": False,
        }
    if status == Inbound.VerificationStatus.VERIFICATION_FAILED:
        return {
            "code": status,
            "label": "VERIFICATION FAILED",
            "tone": "rose",
            "action_label": "Retry Verification",
            "blocking": source_requires_sellability_verification(source),
        }
    return {
        "code": Inbound.VerificationStatus.UNVERIFIED,
        "label": "UNVERIFIED",
        "tone": "slate",
        "action_label": "Verify for Sale",
        "blocking": source_requires_sellability_verification(source),
    }


def source_sellability_issues(source):
    errors = []
    warnings = []
    if not source_requires_sellability_verification(source):
        return errors, warnings
    status = getattr(source, "verification_status", Inbound.VerificationStatus.UNVERIFIED)
    if status == Inbound.VerificationStatus.VERIFIED_SELLABLE:
        if source_sellability_is_stale(source):
            warnings.append("Sellability verification is older than 7 days.")
        return errors, warnings
    if status == Inbound.VerificationStatus.VERIFICATION_FAILED:
        code = getattr(source, "last_verification_error_code", "") or "verification_failed"
        errors.append(f"PasarGuard source verification failed: {code}.")
    else:
        errors.append("PasarGuard source is unverified; run Verify for Sale before enabling new sales.")
    return errors, warnings


def _safe_error_code(exc, default="source_verification_failed"):
    code = str(getattr(exc, "error_code", "") or "").strip()
    if code:
        return sanitize_error_value(code)[:80]
    return default


def _normalize_probe_payload(payload):
    payload = dict(payload or {})
    ok = bool(payload.get("ok"))
    error_code = str(payload.get("error_code") or "")
    status = Inbound.VerificationStatus.VERIFIED_SELLABLE if ok else Inbound.VerificationStatus.VERIFICATION_FAILED
    return {
        "ok": ok,
        "verification_status": status,
        "verification_method": str(payload.get("verification_method") or VERIFICATION_METHOD_PROVISIONING_PROBE)[:80],
        "observed_config_count": int(payload.get("observed_config_count") or 0),
        "protocol_counts": sanitize_error_value(dict(payload.get("protocol_counts") or {})),
        "reality_count": int(payload.get("reality_count") or 0),
        "pbk_validation_ok": bool(payload.get("pbk_validation_ok")),
        "cleanup_succeeded": bool(payload.get("cleanup_succeeded")),
        "error_code": sanitize_error_value(error_code)[:80],
        "warnings": list(sanitize_error_value(list(payload.get("warnings") or []))),
        "safe_details": sanitize_error_value(dict(payload.get("safe_details") or {})),
    }


def _persist_source_verification(source, payload, *, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        locked = Inbound.objects.select_for_update().select_related("panel").get(pk=source.pk)
        metadata = dict(locked.metadata or {})
        verification_record = {
            "status": payload["verification_status"],
            "method": payload["verification_method"],
            "attempted_at": now.isoformat(),
            "observed_config_count": payload["observed_config_count"],
            "protocol_counts": payload["protocol_counts"],
            "reality_count": payload["reality_count"],
            "pbk_validation_ok": payload["pbk_validation_ok"],
            "cleanup_succeeded": payload["cleanup_succeeded"],
            "error_code": payload["error_code"],
        }
        if payload["ok"]:
            verification_record["verified_at"] = now.isoformat()
        metadata["sellability_verification"] = sanitize_error_value(verification_record)
        locked.verification_status = payload["verification_status"]
        locked.verification_attempted_at = now
        locked.verification_method = payload["verification_method"] if payload["ok"] else ""
        locked.last_verification_error_code = "" if payload["ok"] else payload["error_code"]
        if payload["ok"]:
            locked.verified_at = now
            locked.last_verified_config_count = payload["observed_config_count"]
            if locked.panel_id and locked.panel.is_active and not (locked.legacy_note or "").strip():
                locked.is_active = True
                locked.available_for_new_orders = True
        else:
            locked.verified_at = None
            locked.last_verified_config_count = 0
            if source_requires_sellability_verification(locked):
                locked.available_for_new_orders = False
                if (locked.metadata or {}).get("disabled_known") is False:
                    locked.is_active = False
        locked.metadata = metadata
        locked.save(
            update_fields=[
                "verification_status",
                "verified_at",
                "verification_method",
                "last_verified_config_count",
                "last_verification_error_code",
                "verification_attempted_at",
                "is_active",
                "available_for_new_orders",
                "metadata",
                "updated_at",
            ]
        )
    return locked


def _audit_source_verification(source, result, *, actor=None):
    payload = result.to_safe_dict()
    payload["actor_id"] = getattr(actor, "pk", None) if getattr(actor, "is_authenticated", False) else None
    BotEventLog.objects.create(
        event_type=BotEventLog.EventType.WEBHOOK,
        status=BotEventLog.Status.SUCCESS if result.ok else BotEventLog.Status.FAILED,
        message="panel_source_sellability_verification",
        raw_payload=sanitize_error_value(payload),
    )


def _result_from_payload(source, payload):
    return SourceSellabilityVerificationResult(
        source_id=getattr(source, "pk", None),
        panel_id=getattr(source, "panel_id", None),
        ok=payload["ok"],
        verification_status=payload["verification_status"],
        verification_method=payload["verification_method"],
        observed_config_count=payload["observed_config_count"],
        protocol_counts=payload["protocol_counts"],
        reality_count=payload["reality_count"],
        pbk_validation_ok=payload["pbk_validation_ok"],
        cleanup_succeeded=payload["cleanup_succeeded"],
        error_code=payload["error_code"],
        warnings=payload["warnings"],
        safe_details=payload["safe_details"],
    )


def _failure_payload(error_code, *, cleanup_succeeded=False, safe_details=None, warnings=None):
    return {
        "ok": False,
        "verification_status": Inbound.VerificationStatus.VERIFICATION_FAILED,
        "verification_method": VERIFICATION_METHOD_PROVISIONING_PROBE,
        "observed_config_count": 0,
        "protocol_counts": {},
        "reality_count": 0,
        "pbk_validation_ok": False,
        "cleanup_succeeded": bool(cleanup_succeeded),
        "error_code": str(error_code or "source_verification_failed")[:80],
        "warnings": list(warnings or []),
        "safe_details": sanitize_error_value(safe_details or {}),
    }


def verify_panel_source_sellability(source_id, *, actor=None, adapter_factory=None, now=None):
    now = now or timezone.now()
    source = Inbound.objects.select_related("panel").get(pk=source_id)
    adapter_factory = adapter_factory or get_safe_panel_adapter
    panel = getattr(source, "panel", None)
    if not panel:
        payload = _failure_payload("source_panel_missing")
    else:
        adapter = adapter_factory(panel)
        probe = getattr(adapter, "probe_source_sellability", None)
        if not getattr(adapter, "supports_sellability_probe", False) or not callable(probe):
            payload = _failure_payload("source_probe_unsupported", safe_details={"family": getattr(adapter, "family", "")})
        else:
            try:
                payload = _normalize_probe_payload(probe(source))
                if payload["ok"] and not payload["cleanup_succeeded"]:
                    payload["ok"] = False
                    payload["verification_status"] = Inbound.VerificationStatus.VERIFICATION_FAILED
                    payload["error_code"] = "cleanup_failed"
            except PanelIntegrationError as exc:
                payload = _failure_payload(
                    _safe_error_code(exc),
                    safe_details=getattr(exc, "to_safe_dict", lambda: {})(),
                    warnings=getattr(exc, "warnings", []),
                )
            except Exception as exc:
                payload = _failure_payload(
                    "source_verification_failed",
                    safe_details={"message": sanitize_error_value(str(exc or ""), panel=panel)},
                )
    source = _persist_source_verification(source, payload, now=now)
    result = _result_from_payload(source, payload)
    _audit_source_verification(source, result, actor=actor)
    return result
