from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from django.utils import timezone

from store.admin_panel_center.routing_forms import ROUTE_MODE_MULTI, ROUTE_MODE_SINGLE
from store.admin_panel_center.routing_services import validate_routing_selection
from store.models import Order, Panel, VPNClient
from store.naming import build_xui_client_email
from store.panels import get_safe_panel_adapter
from store.panels.errors import PanelOperationUnsupportedError
from store.panels.xui.adapter import XUIProvisioningRequest
from store.xui_api import XUIError, find_xui_client_and_stats, sanitize_xui_operational_text


STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

ERROR_ROUTE_INVALID = "route_invalid"
ERROR_UNSUPPORTED_PANEL_FAMILY = "unsupported_panel_family"
ERROR_UNSUPPORTED_CAPABILITY = "unsupported_capability"
ERROR_LOGIN_FAILED = "login_failed"
ERROR_WRITE_API_FORBIDDEN = "write_api_forbidden"
ERROR_CREATE_FAILED = "create_failed"
ERROR_VERIFY_FAILED = "verify_failed"
ERROR_LINK_GENERATION_FAILED = "link_generation_failed"
ERROR_CLEANUP_FAILED = "cleanup_failed"
ERROR_PARTIAL_CLEANUP = "partial_cleanup"

CONFIG_LINK_PATTERN = re.compile(r"\b(?:vless|vmess|trojan|ss|ssr)://[^\s<>()]+", re.IGNORECASE)
SUB_LINK_PATTERN = re.compile(r"https?://[^\s<>()]+/sub/[A-Za-z0-9_-]+", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
SUB_ID_PATTERN = re.compile(r"(?i)\b(subId|sub_id|sub)[\"':=\s/]+[A-Za-z0-9_-]{8,}\b")


@dataclass
class SafeProvisioningTestStep:
    code: str
    title: str
    status: str = STATUS_SKIPPED
    message: str = ""
    safe_details: dict = field(default_factory=dict)
    error_code: str = ""


@dataclass
class SafeProvisioningTestCleanupResult:
    attempted: bool = False
    success: bool = False
    remaining_remote_test_clients_count: int = 0
    errors: list[str] = field(default_factory=list)
    remediation: str = ""


@dataclass
class MaskedDeliveryPreview:
    subscription_generated: bool = False
    subscription_link: str = ""
    direct_link_count: int = 0
    direct_links: list[str] = field(default_factory=list)


@dataclass
class SafeProvisioningTestRequest:
    plan: object
    mode: str
    panel: object | None = None
    inbound: object | None = None
    inbounds: list[object] = field(default_factory=list)


@dataclass
class SafeProvisioningTestResult:
    status: str
    panel_id: int | None = None
    panel_name: str = ""
    panel_family: str = ""
    capability_profile: str = ""
    plan_id: int | None = None
    plan_title: str = ""
    selected_local_inbound_pks: list[int] = field(default_factory=list)
    selected_remote_inbound_ids: list[int] = field(default_factory=list)
    expected_mode: str = ""
    create_attempted: bool = False
    create_success: bool = False
    verified_inbound_count: int = 0
    expected_inbound_count: int = 0
    subscription_generated: bool = False
    direct_link_count: int = 0
    cleanup_attempted: bool = False
    cleanup_success: bool = False
    remaining_remote_test_clients_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps: list[SafeProvisioningTestStep] = field(default_factory=list)
    cleanup: SafeProvisioningTestCleanupResult = field(default_factory=SafeProvisioningTestCleanupResult)
    masked_delivery_preview: MaskedDeliveryPreview = field(default_factory=MaskedDeliveryPreview)
    test_marker: str = ""
    masked_client_name: str = ""

    @property
    def ok(self):
        return self.status == STATUS_SUCCESS

    @property
    def message(self):
        if self.status == STATUS_SUCCESS:
            return "Live provisioning test completed and cleanup verified."
        if self.cleanup_attempted and not self.cleanup_success:
            return "Live provisioning test finished with cleanup risk."
        return "Live provisioning test failed safely."


def mask_sensitive_output(value):
    if value is None:
        return ""
    text = str(value)
    text = CONFIG_LINK_PATTERN.sub("<direct-link-redacted>", text)
    text = SUB_LINK_PATTERN.sub("<subscription-link-redacted>", text)
    text = UUID_PATTERN.sub("<uuid-redacted>", text)
    text = SUB_ID_PATTERN.sub(r"\1=<sub-id-redacted>", text)
    text = re.sub(r"(?i)(password|token|csrf|cookie|session)[\"':=\s]+[^\"'\s,}]+", r"\1=<redacted>", text)
    return text


def build_test_marker(plan_id):
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"qasedak-route-test-{plan_id}-{timestamp}-{suffix}"


def _mask_client_name(email, marker):
    marker = str(marker or "").strip()
    if marker:
        return f"{marker}-<client-suffix-redacted>"
    if not email:
        return "<client-name-redacted>"
    return f"{str(email)[:24]}<client-suffix-redacted>"


def _safe_error(exc, panel=None):
    return mask_sensitive_output(sanitize_xui_operational_text(exc, panel=panel, max_length=700))


def _classify_exception(exc):
    message = str(exc or "").lower()
    category = str(getattr(exc, "category", "") or "").lower()
    if "login" in message or "auth" in category or "auth" in message:
        return ERROR_LOGIN_FAILED
    if "403" in message or "forbidden" in message or "csrf" in category:
        return ERROR_WRITE_API_FORBIDDEN
    if isinstance(exc, PanelOperationUnsupportedError):
        return ERROR_UNSUPPORTED_CAPABILITY
    return ERROR_CREATE_FAILED


def _selected_inbounds(request):
    if request.mode == ROUTE_MODE_SINGLE:
        return [request.inbound] if request.inbound else []
    if request.mode == ROUTE_MODE_MULTI:
        return list(request.inbounds or [])
    return []


def _iter_remote_clients(inbound_data):
    if not isinstance(inbound_data, dict):
        return
    for key in ("settings",):
        try:
            parsed = json.loads(inbound_data.get(key) or "{}")
        except (TypeError, ValueError):
            parsed = {}
        for client in parsed.get("clients") or []:
            if isinstance(client, dict):
                yield client
    for stats in inbound_data.get("clientStats") or inbound_data.get("client_stats") or []:
        if isinstance(stats, dict):
            yield stats


class SafeRouteProvisioningTestService:
    def __init__(self, *, adapter_factory=get_safe_panel_adapter):
        self.adapter_factory = adapter_factory

    def run(self, request: SafeProvisioningTestRequest) -> SafeProvisioningTestResult:
        inbounds = _selected_inbounds(request)
        panel = request.panel or (inbounds[0].panel if inbounds else None)
        report = None
        adapter = None
        marker = build_test_marker(request.plan.pk)
        client_uuid = str(uuid.uuid4())
        sub_id = uuid.uuid4().hex[:16]
        email = build_xui_client_email(marker, client_uuid)
        expected_mode = "multi" if request.mode == ROUTE_MODE_MULTI else "single" if request.mode == ROUTE_MODE_SINGLE else request.mode
        result = SafeProvisioningTestResult(
            status=STATUS_ERROR,
            panel_id=getattr(panel, "pk", None),
            panel_name=str(getattr(panel, "name", "") or ""),
            plan_id=getattr(request.plan, "pk", None),
            plan_title=str(getattr(request.plan, "name", "") or ""),
            selected_local_inbound_pks=[inbound.pk for inbound in inbounds if inbound],
            selected_remote_inbound_ids=[inbound.inbound_id for inbound in inbounds if inbound],
            expected_mode=expected_mode,
            expected_inbound_count=len(inbounds),
            test_marker=marker,
            masked_client_name=_mask_client_name(email, marker),
        )
        steps = {
            "validation": SafeProvisioningTestStep("validation", "اعتبارسنجی مسیر"),
            "create": SafeProvisioningTestStep("create", "ساخت روی پنل"),
            "verify": SafeProvisioningTestStep("verify", "بررسی روی اینباندها"),
            "links": SafeProvisioningTestStep("links", "ساخت لینک‌ها"),
            "cleanup": SafeProvisioningTestStep("cleanup", "پاکسازی"),
        }
        result.steps = list(steps.values())

        try:
            adapter = self.adapter_factory(panel) if panel else None
            report = adapter.get_capability_report() if adapter else None
            result.panel_family = getattr(report, "family", "") or str(getattr(adapter, "family", "") or "")
            result.capability_profile = getattr(report, "capability_profile", "") or ""
            validation_errors, validation_warnings = validate_routing_selection(
                plan=request.plan,
                mode=request.mode,
                panel=panel,
                inbound=request.inbound,
                inbounds=request.inbounds,
            )
            result.warnings.extend(mask_sensitive_output(item) for item in validation_warnings)
            if not panel or not adapter or not report:
                validation_errors.append("Panel is required.")
            elif result.panel_family != Panel.Family.XUI:
                validation_errors.append("Only X-UI live route tests are implemented.")
                steps["validation"].error_code = ERROR_UNSUPPORTED_PANEL_FAMILY
            elif not report.supports_create_client or not report.supports_delete_client:
                validation_errors.append("Selected panel does not support create/delete client operations.")
                steps["validation"].error_code = ERROR_UNSUPPORTED_CAPABILITY
            elif request.mode == ROUTE_MODE_MULTI and not report.supports_multi_inbound_create:
                validation_errors.append("Selected panel does not support multi-inbound create.")
                steps["validation"].error_code = ERROR_UNSUPPORTED_CAPABILITY

            if validation_errors:
                result.errors.extend(mask_sensitive_output(item) for item in validation_errors)
                steps["validation"].status = STATUS_ERROR
                steps["validation"].message = "Route validation failed."
                steps["validation"].error_code = steps["validation"].error_code or ERROR_ROUTE_INVALID
                result.status = STATUS_ERROR
                return result

            steps["validation"].status = STATUS_SUCCESS
            steps["validation"].message = "Route is valid for live testing."
            steps["validation"].safe_details = {
                "selected_local_inbound_pks": result.selected_local_inbound_pks,
                "selected_remote_inbound_ids": result.selected_remote_inbound_ids,
                "expected_mode": result.expected_mode,
            }

            try:
                result.create_attempted = True
                provisioning_request = XUIProvisioningRequest(
                    email_prefix=marker,
                    total_gb=request.plan.volume_gb,
                    duration_days=request.plan.duration_days,
                    inbound=inbounds[0] if request.mode == ROUTE_MODE_SINGLE else None,
                    inbounds=inbounds if request.mode == ROUTE_MODE_MULTI else None,
                    limit_ip=request.plan.device_limit,
                    client_uuid=client_uuid,
                    sub_id=sub_id,
                    email=email,
                )
                created = (
                    adapter.create_enabled_multi_inbound_client(provisioning_request)
                    if request.mode == ROUTE_MODE_MULTI
                    else adapter.create_enabled_client(provisioning_request)
                )
                result.create_success = bool(created)
                steps["create"].status = STATUS_SUCCESS
                steps["create"].message = "Remote test client was created."
                steps["create"].safe_details = {
                    "totalGB": str(request.plan.volume_gb),
                    "duration_days": request.plan.duration_days,
                    "limitIp": request.plan.device_limit,
                    "client_marker": marker,
                }
            except Exception as exc:
                error_code = _classify_exception(exc)
                safe_error = _safe_error(exc, panel=panel)
                result.errors.append(safe_error)
                steps["create"].status = STATUS_ERROR
                steps["create"].message = "Remote test client creation failed."
                steps["create"].error_code = error_code
                result.status = STATUS_ERROR
                return result

            try:
                verified_count = self._verify_remote(adapter, inbounds, client_uuid, email)
                result.verified_inbound_count = verified_count
                if verified_count != len(inbounds):
                    raise XUIError("Test client was not verified on every selected inbound.")
                steps["verify"].status = STATUS_SUCCESS
                steps["verify"].message = "Remote test client exists on expected inbound(s)."
                steps["verify"].safe_details = {
                    "verified_inbound_count": verified_count,
                    "expected_inbound_count": len(inbounds),
                }
            except Exception as exc:
                safe_error = _safe_error(exc, panel=panel)
                result.errors.append(safe_error)
                steps["verify"].status = STATUS_ERROR
                steps["verify"].message = "Remote verification failed."
                steps["verify"].error_code = ERROR_VERIFY_FAILED
                result.status = STATUS_ERROR
                return result

            try:
                preview = self._masked_delivery_preview(created)
                result.masked_delivery_preview = preview
                result.subscription_generated = preview.subscription_generated
                result.direct_link_count = preview.direct_link_count
                steps["links"].status = STATUS_SUCCESS if preview.subscription_generated or preview.direct_link_count else STATUS_WARNING
                steps["links"].message = "Delivery links were generated and masked." if steps["links"].status == STATUS_SUCCESS else "No delivery links were returned."
                steps["links"].safe_details = {
                    "subscription_generated": preview.subscription_generated,
                    "direct_link_count": preview.direct_link_count,
                    "subscription_link": preview.subscription_link,
                    "direct_links": preview.direct_links,
                }
            except Exception as exc:
                safe_error = _safe_error(exc, panel=panel)
                result.errors.append(safe_error)
                steps["links"].status = STATUS_ERROR
                steps["links"].message = "Delivery link generation failed."
                steps["links"].error_code = ERROR_LINK_GENERATION_FAILED
                result.status = STATUS_ERROR
                return result
        finally:
            if result.create_attempted and adapter and inbounds:
                cleanup = self._cleanup(adapter, inbounds, identifiers=[client_uuid, email], marker=marker, mode=request.mode)
                result.cleanup = cleanup
                result.cleanup_attempted = cleanup.attempted
                result.cleanup_success = cleanup.success
                result.remaining_remote_test_clients_count = cleanup.remaining_remote_test_clients_count
                steps["cleanup"].status = STATUS_SUCCESS if cleanup.success else STATUS_ERROR
                steps["cleanup"].message = "Remote test client cleanup verified." if cleanup.success else "Remote cleanup needs attention."
                steps["cleanup"].error_code = "" if cleanup.success else (ERROR_PARTIAL_CLEANUP if cleanup.remaining_remote_test_clients_count else ERROR_CLEANUP_FAILED)
                steps["cleanup"].safe_details = {
                    "remaining_remote_test_clients_count": cleanup.remaining_remote_test_clients_count,
                    "remediation": cleanup.remediation,
                }
                if cleanup.errors:
                    result.errors.extend(cleanup.errors)
            elif not result.create_attempted:
                steps["cleanup"].status = STATUS_SKIPPED
                steps["cleanup"].message = "No remote client was created."

        if result.cleanup_attempted and not result.cleanup_success:
            result.status = STATUS_ERROR
        elif any(step.status == STATUS_ERROR for step in result.steps):
            result.status = STATUS_ERROR
        elif any(step.status == STATUS_WARNING for step in result.steps):
            result.status = STATUS_WARNING
        else:
            result.status = STATUS_SUCCESS
        return result

    def _verify_remote(self, adapter, inbounds, client_uuid, email):
        verified = 0
        for inbound in inbounds:
            inbound_data = adapter.service.get_inbound(inbound, use_cache=False)
            target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, client_uuid)
            if not target_client and not target_stats:
                target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, email)
            if target_client or target_stats:
                verified += 1
        return verified

    def _masked_delivery_preview(self, created):
        bundle_results = list((created or {}).get("bundle_inbound_results") or [])
        direct_values = [item.get("direct_link") for item in bundle_results if item.get("direct_link")]
        if not direct_values and (created or {}).get("direct_link"):
            direct_values = [created.get("direct_link")]
        sub_link = str((created or {}).get("sub_link") or "")
        return MaskedDeliveryPreview(
            subscription_generated=bool(sub_link),
            subscription_link="<subscription-link-redacted>" if sub_link else "",
            direct_link_count=len(direct_values),
            direct_links=[self._mask_direct_link(value) for value in direct_values],
        )

    def _mask_direct_link(self, value):
        scheme = str(value or "").split("://", 1)[0].lower()
        if scheme not in {"vless", "vmess", "trojan", "ss", "ssr"}:
            scheme = "direct"
        return f"{scheme}://<direct-link-redacted>"

    def _cleanup(self, adapter, inbounds, *, identifiers, marker, mode):
        cleanup = SafeProvisioningTestCleanupResult(attempted=True)
        allow_multi_scope = mode == ROUTE_MODE_MULTI
        errors = []
        for inbound in inbounds:
            deleted = False
            for identifier in identifiers:
                try:
                    adapter.delete_client(inbound, identifier, allow_multi_scope=allow_multi_scope)
                    deleted = True
                    break
                except Exception as exc:
                    errors.append(
                        f"inbound={getattr(inbound, 'inbound_id', '')}: {_safe_error(exc, panel=getattr(adapter, 'panel', None))}"
                    )
            if not deleted:
                continue
        remaining = self._remaining_test_clients(adapter, inbounds, identifiers=identifiers, marker=marker)
        cleanup.remaining_remote_test_clients_count = remaining
        cleanup.success = remaining == 0
        if not cleanup.success:
            cleanup.errors = errors[:5] or ["Remote test client may still exist."]
            cleanup.remediation = (
                f"Search panel_id={getattr(adapter.panel, 'pk', '')} inbounds="
                f"{[getattr(inbound, 'inbound_id', '') for inbound in inbounds]} for marker {marker} and delete test clients."
            )
        return cleanup

    def _remaining_test_clients(self, adapter, inbounds, *, identifiers, marker):
        remaining = 0
        for inbound in inbounds:
            try:
                inbound_data = adapter.service.get_inbound(inbound, use_cache=False)
            except Exception:
                remaining += 1
                continue
            seen = set()
            for identifier in identifiers:
                target_client, target_stats, _matched, _clients, _stats = find_xui_client_and_stats(inbound_data, identifier)
                for data in (target_client, target_stats):
                    if data:
                        key = str(data.get("id") or data.get("email") or data.get("subId") or identifier)
                        if key not in seen:
                            seen.add(key)
                            remaining += 1
            marker = str(marker or "")
            if marker:
                for client in _iter_remote_clients(inbound_data):
                    email = str(client.get("email") or "")
                    if marker in email and email not in seen:
                        seen.add(email)
                        remaining += 1
        return remaining


def run_safe_route_provisioning_test(*, plan, mode, panel=None, inbound=None, inbounds=None):
    before_orders = Order.objects.count()
    before_clients = VPNClient.objects.count()
    result = SafeRouteProvisioningTestService().run(
        SafeProvisioningTestRequest(
            plan=plan,
            mode=mode,
            panel=panel,
            inbound=inbound,
            inbounds=list(inbounds or []),
        )
    )
    if Order.objects.count() != before_orders:
        result.errors.append("Safety violation: Order count changed during live route test.")
        result.status = STATUS_ERROR
    if VPNClient.objects.count() != before_clients:
        result.errors.append("Safety violation: VPNClient count changed during live route test.")
        result.status = STATUS_ERROR
    return result
