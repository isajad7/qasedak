from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from store.panels.xui.adapter import XUIProvisioningRequest

from ..capabilities import CapabilityFlag, CapabilityProfile, InboundHealthResult, PanelCapabilityReport
from ..errors import PanelIntegrationError, PanelOperationUnsupportedError, sanitize_error_value
from .client import PasarGuardClient
from .errors import (
    PasarGuardCreateUserError,
    PasarGuardIntegrationError,
    PasarGuardReadError,
    PasarGuardUserConflictError,
)
from .schemas import (
    bytes_from_gb,
    expires_at_from_days,
    merge_qasedak_note,
    normalize_pasarguard_username,
    pasarguard_datetime,
    pasarguard_note_marker,
    safe_pasarguard_group,
    user_note_matches_context,
)


PASARGUARD_PROFILE = "pasarguard_groups"
PASARGUARD_READ_ENDPOINTS = (
    "/api/system",
    "/api/groups",
    "/api/groups/simple",
    "/api/group/{id}",
    "/api/inbounds",
    "/api/inbounds/details",
    "/<subscription-path>/<token>/links",
    "/<subscription-path>/<token>/raw",
)
PASARGUARD_WRITE_ENDPOINTS = (
    "POST /api/user",
    "GET /api/user/{username}",
    "PUT /api/user/{username}",
    "DELETE /api/user/{username}",
    "PUT /api/user/{username}/disabled",
    "POST /api/user/{username}/reset",
    "POST /api/user/{username}/revoke_sub",
)


def _pasarguard_flags():
    return frozenset(
        {
            CapabilityFlag.LOGIN,
            CapabilityFlag.API_KEY_AUTH,
            CapabilityFlag.READ_INBOUNDS,
            CapabilityFlag.WRITE_CLIENTS,
            CapabilityFlag.UPDATE_CLIENTS,
            CapabilityFlag.DISABLE_CLIENTS,
            CapabilityFlag.RESET_USAGE,
            CapabilityFlag.MULTI_INBOUND_CLIENTS,
            CapabilityFlag.MULTI_GROUP_USERS,
            CapabilityFlag.SUBSCRIPTION_LINKS,
            CapabilityFlag.NATIVE_RAW_CONFIGS,
            CapabilityFlag.USAGE_INFO,
            CapabilityFlag.REALITY_NATIVE_DELIVERY,
            CapabilityFlag.SELLABILITY_PROBE,
        }
    )


def _report(panel, *, metadata=None, warnings=(), errors=()):
    return PanelCapabilityReport(
        family="pasarguard",
        supported=True,
        profile=CapabilityProfile(
            family="pasarguard",
            profile=PASARGUARD_PROFILE,
            version=str((metadata or {}).get("version") or ""),
            flags=_pasarguard_flags(),
            metadata=metadata or {"native_raw_delivery": True},
        ),
        login_method="x-api-key",
        read_endpoints=PASARGUARD_READ_ENDPOINTS,
        write_endpoints=PASARGUARD_WRITE_ENDPOINTS,
        supported_protocols=("native_raw",),
        warnings=tuple(warnings),
        errors=tuple(errors),
        metadata={
            "native_raw_delivery": True,
            "local_link_reconstruction": False,
            **(metadata or {}),
        },
    )


def _group_id_from_inbound(inbound):
    value = getattr(inbound, "inbound_id", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise PanelOperationUnsupportedError(
            "گروه PasarGuard شناسه معتبر ندارد.",
            error_code="pasarguard_group_id_invalid",
            layer="inbound_validation",
            action="validate_group",
            panel=getattr(inbound, "panel", None),
            panel_family="pasarguard",
            inbound=inbound,
        )


def _safe_remote_user_id(user):
    if not isinstance(user, dict):
        return None
    return user.get("id")


class PasarGuardPanelAdapter:
    family = "pasarguard"
    supports_sellability_probe = True

    def __init__(self, panel, *, client=None):
        self.panel = panel
        self.client = client or PasarGuardClient(panel)

    def get_capability_report(self) -> PanelCapabilityReport:
        return self.detect_capabilities(live=False)

    def test_connection(self) -> bool:
        self.client.get_system()
        return True

    def detect_capabilities(self, *, live: bool = False, write: bool = False) -> PanelCapabilityReport:
        metadata = {"native_raw_delivery": True, "write_probe_performed": False}
        warnings = []
        errors = []
        if live:
            try:
                system = self.client.get_system()
                groups = self.client.list_groups()
            except Exception as exc:
                if isinstance(exc, PasarGuardIntegrationError):
                    errors.append(exc.message)
                else:
                    errors.append(str(exc or "pasarguard_detection_failed"))
                return _report(self.panel, metadata=metadata, warnings=warnings, errors=errors)
            metadata.update(
                {
                    "version": str((system or {}).get("version") or (system or {}).get("app_version") or ""),
                    "group_count": len(groups),
                }
            )
            if write:
                warnings.append("Write probing for PasarGuard intentionally does not mutate production users.")
        return _report(self.panel, metadata=metadata, warnings=warnings, errors=errors)

    def list_inbounds(self) -> list[dict]:
        try:
            return [safe_pasarguard_group(item) for item in self.client.list_groups()]
        except Exception as exc:
            if isinstance(exc, PasarGuardReadError):
                raise
            raise PasarGuardReadError(
                "خواندن گروه‌های PasarGuard ناموفق بود.",
                action="list_groups",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family=self.family,
            ) from exc

    def check_inbound(self, inbound) -> InboundHealthResult:
        group_id = _group_id_from_inbound(inbound)
        try:
            group = self._get_group_for_validation(group_id)
        except Exception as exc:
            return InboundHealthResult(
                ok=False,
                inbound_id=str(group_id),
                remote_key=f"pasarguard_group:{group_id}",
                status="error",
                message=str(exc or "pasarguard_group_check_failed"),
            )
        if group["is_disabled"] is None:
            return InboundHealthResult(
                ok=False,
                inbound_id=str(group_id),
                remote_key=f"pasarguard_group:{group_id}",
                status="unknown",
                message="PasarGuard group detail is not readable with this API key.",
                metadata={
                    "group_name": group["name"],
                    "inbound_tag_count": len(group["inbound_tags"]),
                    "disabled_known": False,
                },
            )
        ok = not group["is_disabled"]
        return InboundHealthResult(
            ok=ok,
            inbound_id=str(group_id),
            remote_key=f"pasarguard_group:{group_id}",
            status="ok" if ok else "disabled",
            message="" if ok else "PasarGuard group is disabled.",
            metadata={
                "group_name": group["name"],
                "inbound_tag_count": len(group["inbound_tags"]),
            },
        )

    def _get_group_for_validation(self, group_id):
        try:
            return safe_pasarguard_group(self.client.get_group(group_id))
        except PanelIntegrationError as exc:
            status_code = (getattr(exc, "safe_context", {}) or {}).get("status_code")
            if status_code != 403:
                raise
            for group in self.client.list_groups_simple():
                safe_group = safe_pasarguard_group(group)
                if safe_group["id"] == int(group_id):
                    return safe_group
            raise

    def _normalize_inbounds(self, request: XUIProvisioningRequest):
        inbounds = list(request.inbounds or [])
        if request.inbound and request.inbound not in inbounds:
            inbounds.append(request.inbound)
        if not inbounds:
            raise PanelOperationUnsupportedError(
                "ساخت کاربر PasarGuard به حداقل یک گروه نیاز دارد.",
                error_code="pasarguard_group_required",
                layer="inbound_validation",
                action="create_user",
                panel=self.panel,
                panel_family=self.family,
            )
        panel_ids = {getattr(inbound, "panel_id", None) for inbound in inbounds}
        if len(panel_ids) != 1 or self.panel.pk not in panel_ids:
            raise PanelOperationUnsupportedError(
                "همه گروه‌های PasarGuard باید به همین پنل تعلق داشته باشند.",
                error_code="pasarguard_cross_panel_group_selection",
                layer="inbound_validation",
                action="create_user",
                panel=self.panel,
                panel_family=self.family,
                inbound=inbounds[0],
            )
        normalized = []
        seen = set()
        for inbound in inbounds:
            group_id = _group_id_from_inbound(inbound)
            if group_id in seen:
                continue
            seen.add(group_id)
            normalized.append(inbound)
        return normalized

    def _verify_groups(self, inbounds):
        groups = []
        for inbound in inbounds:
            try:
                group = self._get_group_for_validation(_group_id_from_inbound(inbound))
            except PanelIntegrationError as exc:
                code = str(getattr(exc, "error_code", "") or "")
                status_code = (getattr(exc, "safe_context", {}) or {}).get("status_code")
                if code.endswith("_404") or status_code == 404:
                    raise PanelOperationUnsupportedError(
                        "گروه PasarGuard در پنل پیدا نشد.",
                        error_code="pasarguard_group_missing",
                        layer="inbound_validation",
                        action="create_user",
                        panel=self.panel,
                        panel_family=self.family,
                        inbound=inbound,
                    ) from exc
                raise
            if group["is_disabled"] is True:
                raise PanelOperationUnsupportedError(
                    "گروه PasarGuard در پنل غیرفعال است.",
                    error_code="pasarguard_group_disabled",
                    layer="inbound_validation",
                    action="create_user",
                    panel=self.panel,
                    panel_family=self.family,
                    inbound=inbound,
                    safe_context={"group_id": group["id"], "group_name": group["name"]},
                )
            groups.append(group)
        return groups

    def _request_context(self, request, group_ids):
        return "|".join(
            [
                str(getattr(self.panel, "pk", "") or ""),
                str(request.client_uuid or ""),
                str(request.sub_id or ""),
                str(request.email or request.email_prefix or ""),
                ",".join(str(item) for item in sorted(group_ids)),
            ]
        )

    def _payload(self, request, username, marker, group_ids, existing_user=None):
        expires_at = expires_at_from_days(request.duration_days)
        payload = {
            "username": username,
            "status": "active",
            "expire": pasarguard_datetime(expires_at),
            "data_limit": bytes_from_gb(request.total_gb),
            "data_limit_reset_strategy": "no_reset",
            "group_ids": group_ids,
            "proxy_settings": {},
            "note": merge_qasedak_note((existing_user or {}).get("note"), marker),
        }
        try:
            limit_ip = int(request.limit_ip or 0)
        except (TypeError, ValueError):
            limit_ip = 0
        if limit_ip > 0:
            payload["hwid_limit"] = limit_ip
        return payload, expires_at

    def _get_user_or_none(self, username):
        try:
            return self.client.get_user(username)
        except PanelIntegrationError as exc:
            code = str(getattr(exc, "error_code", "") or "")
            context_status = (getattr(exc, "safe_context", {}) or {}).get("status_code")
            if code.endswith("_404") or context_status == 404:
                return None
            raise

    def _probe_error_code(self, exc, *, phase):
        code = str(getattr(exc, "error_code", "") or "").strip()
        if phase in {"create", "update"}:
            return "remote_create_failed"
        if phase == "subscription":
            if code == "pasarguard_subscription_missing":
                return "subscription_missing"
            if "links_empty" in code:
                return "native_links_empty"
            if "links" in code or "subscription" in code:
                return "native_links_fetch_failed"
        if code == "pasarguard_group_disabled":
            return "source_not_allowed"
        return sanitize_error_value(code or "source_verification_failed")[:80]

    def _probe_cleanup(self, source, username):
        if not str(username or "").strip():
            return False, "cleanup_identity_missing"
        try:
            self.delete_client(source, username)
            return self._get_user_or_none(username) is None, ""
        except Exception as exc:
            return False, sanitize_error_value(str(exc or ""), panel=self.panel)[:160]

    def probe_source_sellability(self, source):
        from store.external_subscription_sources import filter_native_configs

        group_id = _group_id_from_inbound(source)
        context = f"sellability-probe:{getattr(self.panel, 'pk', '')}:{getattr(source, 'pk', '')}:{group_id}"
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"qasedak:{context}:uuid"))
        sub_id = hashlib.sha256(f"qasedak:{context}:sub".encode("utf-8")).hexdigest()[:16]
        email_prefix = f"qasedak_verify_{getattr(source, 'pk', group_id) or group_id}"
        request = XUIProvisioningRequest(
            email_prefix=email_prefix,
            total_gb=Decimal("0.001"),
            duration_days=1,
            inbound=source,
            inbounds=[source],
            limit_ip=1,
            client_uuid=client_uuid,
            sub_id=sub_id,
            email=email_prefix,
        )
        group_ids = [group_id]
        marker = pasarguard_note_marker(self._request_context(request, group_ids))
        username = normalize_pasarguard_username(request.email or request.email_prefix, context=self._request_context(request, group_ids))
        phase = "validate_group"
        created_or_reused = False
        created_new = False
        cleanup_succeeded = False
        cleanup_error = ""
        filter_result = None
        error_code = ""
        safe_details = {
            "source_id": getattr(source, "pk", None),
            "group_id": group_id,
            "cleanup_attempted": False,
            "cleanup_confirmed": False,
        }
        try:
            self._verify_groups([source])
            phase = "lookup"
            existing = self._get_user_or_none(username)
            if existing and not user_note_matches_context(existing, marker):
                raise PasarGuardUserConflictError(
                    "کاربر PasarGuard با همین نام وجود دارد اما marker تأیید فروش با آن هم‌خوان نیست.",
                    error_code="source_not_allowed",
                    panel=self.panel,
                    panel_family=self.family,
                    safe_context={"username_saved": True, "group_ids": group_ids},
                )
            payload, _expires_at = self._payload(request, username, marker, group_ids, existing_user=existing)
            phase = "update" if existing else "create"
            user = self.client.modify_user(username, payload) if existing else self.client.create_user(payload)
            created_or_reused = True
            created_new = not bool(existing)
            user = user if isinstance(user, dict) else {}
            subscription_url = str(user.get("subscription_url") or (existing or {}).get("subscription_url") or "").strip()
            if not subscription_url:
                fetched = self._get_user_or_none(username) or {}
                subscription_url = str(fetched.get("subscription_url") or "").strip()
                user = {**fetched, **user}
            if not subscription_url:
                error_code = "subscription_missing"
            else:
                phase = "subscription"
                raw_links = list(self.client.fetch_native_links(subscription_url) or [])
                filter_result = filter_native_configs(raw_links, {"require_reality_pbk": True})
                if not raw_links:
                    error_code = "native_links_empty"
                elif (filter_result.invalid_reasons or {}).get("reality_missing_pbk"):
                    error_code = "reality_pbk_missing"
                elif filter_result.selected_count <= 0:
                    error_code = "native_links_empty"
            safe_details.update(
                {
                    "created_new": created_new,
                    "reused_existing": created_or_reused and not created_new,
                    "observed_config_count": filter_result.selected_count if filter_result else 0,
                    "upstream_count": filter_result.upstream_count if filter_result else 0,
                    "parsed_count": filter_result.parsed_count if filter_result else 0,
                    "invalid_count": filter_result.invalid_count if filter_result else 0,
                    "protocol_counts": dict(filter_result.protocol_counts) if filter_result else {},
                    "security_counts": dict(filter_result.security_counts) if filter_result else {},
                    "invalid_reasons": dict(filter_result.invalid_reasons) if filter_result else {},
                }
            )
        except PanelIntegrationError as exc:
            error_code = self._probe_error_code(exc, phase=phase)
            safe_details["provider_error_code"] = sanitize_error_value(getattr(exc, "error_code", "") or "")
        except Exception as exc:
            error_code = "source_verification_failed"
            safe_details["error"] = sanitize_error_value(str(exc or ""), panel=self.panel)[:160]
        finally:
            if created_or_reused:
                cleanup_succeeded, cleanup_error = self._probe_cleanup(source, username)
                safe_details["cleanup_attempted"] = True
                safe_details["cleanup_confirmed"] = cleanup_succeeded
                if cleanup_error:
                    safe_details["cleanup_error"] = cleanup_error

        if created_or_reused and not cleanup_succeeded:
            error_code = "cleanup_failed"
        if error_code:
            return {
                "ok": False,
                "error_code": error_code,
                "observed_config_count": 0,
                "protocol_counts": dict(filter_result.protocol_counts) if filter_result else {},
                "reality_count": int((filter_result.security_counts or {}).get("reality", 0)) if filter_result else 0,
                "pbk_validation_ok": False,
                "cleanup_succeeded": cleanup_succeeded,
                "safe_details": safe_details,
            }
        return {
            "ok": True,
            "observed_config_count": filter_result.selected_count if filter_result else 0,
            "protocol_counts": dict(filter_result.protocol_counts) if filter_result else {},
            "reality_count": int((filter_result.security_counts or {}).get("reality", 0)) if filter_result else 0,
            "pbk_validation_ok": True,
            "cleanup_succeeded": cleanup_succeeded,
            "safe_details": safe_details,
        }

    def _user_result(self, *, request, username, user, group_ids, raw_links, expires_at, marker):
        local_uuid = request.client_uuid or str(uuid.uuid5(uuid.NAMESPACE_URL, f"pasarguard:{self.panel.pk}:{username}"))
        sub_id = request.sub_id or hashlib.sha256(f"pasarguard-sub:{self.panel.pk}:{username}".encode("utf-8")).hexdigest()[:16]
        subscription_url = str((user or {}).get("subscription_url") or "").strip()
        return {
            "uuid": local_uuid,
            "email": username,
            "sub_id": sub_id,
            "sub_link": subscription_url,
            "direct_link": raw_links[0] if raw_links else "",
            "raw_links": raw_links,
            "expires_at": expires_at,
            "xui_node_id": "",
            "remote_client_key": f"pasarguard:{self.panel.pk}:user:{username}",
            "remote_scope": {
                "panel_id": self.panel.pk,
                "pasarguard_group_ids": group_ids,
            },
            "raw": {
                "family": self.family,
                "username": username,
                "remote_user_id": _safe_remote_user_id(user),
                "group_ids": group_ids,
                "subscription_url_saved": bool(subscription_url),
                "raw_link_count": len(raw_links),
                "idempotency_marker": marker,
                "native_raw_delivery": True,
            },
        }

    def create_enabled_client(self, request: XUIProvisioningRequest) -> dict:
        return self.create_enabled_multi_inbound_client(request)

    def create_enabled_multi_inbound_client(self, request: XUIProvisioningRequest) -> dict:
        inbounds = self._normalize_inbounds(request)
        group_ids = [_group_id_from_inbound(inbound) for inbound in inbounds]
        context = self._request_context(request, group_ids)
        marker = pasarguard_note_marker(context)
        username = normalize_pasarguard_username(request.email or request.email_prefix, context=context)
        created_new = False
        try:
            self._verify_groups(inbounds)
            existing = self._get_user_or_none(username)
            if existing and not user_note_matches_context(existing, marker):
                raise PasarGuardUserConflictError(
                    "کاربر PasarGuard با همین نام وجود دارد اما marker سفارش با آن هم‌خوان نیست.",
                    panel=self.panel,
                    panel_family=self.family,
                    safe_context={"username_saved": True, "group_ids": group_ids},
                )
            payload, expires_at = self._payload(request, username, marker, group_ids, existing_user=existing)
            if existing:
                user = self.client.modify_user(username, payload)
            else:
                user = self.client.create_user(payload)
                created_new = True
            user = user if isinstance(user, dict) else {}
            subscription_url = str(user.get("subscription_url") or (existing or {}).get("subscription_url") or "").strip()
            if not subscription_url:
                fetched = self._get_user_or_none(username) or {}
                subscription_url = str(fetched.get("subscription_url") or "").strip()
                user = {**fetched, **user}
            if not subscription_url:
                raise PasarGuardCreateUserError(
                    "PasarGuard کاربر را ساخت اما subscription_url برنگرداند.",
                    error_code="pasarguard_subscription_missing",
                    panel=self.panel,
                    panel_family=self.family,
                    safe_context={"username_saved": True, "group_ids": group_ids},
                )
            raw_links = self.client.fetch_native_links(subscription_url)
            if not raw_links:
                raise PasarGuardCreateUserError(
                    "خروجی native links پنل PasarGuard خالی بود یا کانفیگ قابل استفاده نداشت.",
                    error_code="pasarguard_links_empty",
                    panel=self.panel,
                    panel_family=self.family,
                    safe_context={"username_saved": True, "group_ids": group_ids},
                )
            return self._user_result(
                request=request,
                username=username,
                user={**user, "subscription_url": subscription_url},
                group_ids=group_ids,
                raw_links=raw_links,
                expires_at=expires_at,
                marker=marker,
            )
        except PanelIntegrationError:
            if created_new:
                try:
                    self.client.delete_user(username)
                except Exception:
                    pass
            raise
        except Exception as exc:
            if created_new:
                try:
                    self.client.delete_user(username)
                except Exception:
                    pass
            raise PasarGuardCreateUserError(
                "ساخت کاربر PasarGuard ناموفق بود.",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family=self.family,
                safe_context={"group_count": len(group_ids), "created_new": created_new},
            ) from exc

    def delete_client(self, inbound: object, identifier: str, *, allow_multi_scope: bool = False) -> bool:
        if not str(identifier or "").strip():
            raise PasarGuardCreateUserError(
                "برای حذف کاربر PasarGuard شناسه کاربر لازم است.",
                error_code="pasarguard_username_required",
                panel=self.panel,
                panel_family=self.family,
                inbound=inbound,
            )
        return self.client.delete_user(str(identifier).strip())

    def get_user(self, username):
        return self.client.get_user(username)

    def modify_user(self, username, payload):
        return self.client.modify_user(username, payload)

    def update_user(self, username, payload):
        return self.modify_user(username, payload)

    def disable_user(self, username):
        return self.client.set_user_disabled(username, disabled=True)

    def enable_user(self, username):
        return self.client.set_user_disabled(username, disabled=False)

    def reset_user_usage(self, username):
        return self.client.reset_user_usage(username)

    def revoke_subscription(self, username):
        return self.client.revoke_subscription(username)
