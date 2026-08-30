from __future__ import annotations

from urllib.parse import urljoin

import requests

from ..errors import sanitize_error_value
from .errors import (
    PasarGuardAuthenticationError,
    PasarGuardDeleteUserError,
    PasarGuardIntegrationError,
    PasarGuardReadError,
    PasarGuardWriteForbiddenError,
)
from .schemas import parse_raw_subscription, raw_links_from_payload, subscription_format_url, subscription_raw_url


def _content_type(response):
    return str(getattr(response, "headers", {}).get("content-type", "")).split(";", 1)[0]


class PasarGuardClient:
    def __init__(self, panel, *, session=None, timeout=15):
        self.panel = panel
        self.base_url = str(getattr(panel, "url", "") or "").rstrip("/")
        self.api_key = str(getattr(panel, "password", "") or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()

    def _url(self, path):
        if not self.base_url:
            return path
        return urljoin(f"{self.base_url}/", str(path or "").lstrip("/"))

    def _headers(self):
        return {
            "Accept": "application/json",
            "X-Api-Key": self.api_key,
        }

    def _panel_error(self, exc, *, method, path):
        if isinstance(exc, PasarGuardIntegrationError):
            return exc
        return PasarGuardIntegrationError(
            "درخواست PasarGuard ناموفق بود.",
            error_code="pasarguard_request_failed",
            action=f"{method.upper()} {path}",
            technical_detail=str(exc or ""),
            panel=self.panel,
            panel_family="pasarguard",
        )

    def request(self, method, path, *, json=None, expected_statuses=(200,)):
        method = str(method or "GET").upper()
        path = str(path or "")
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PasarGuardIntegrationError(
                "ارتباط با PasarGuard برقرار نشد.",
                error_code="pasarguard_network_error",
                action=f"{method} {path}",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family="pasarguard",
            ) from exc

        if response.status_code in expected_statuses:
            if response.status_code == 204 or not str(response.text or "").strip():
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise PasarGuardReadError(
                    "پاسخ PasarGuard JSON معتبر نبود.",
                    error_code="pasarguard_invalid_json",
                    action=f"{method} {path}",
                    technical_detail=str(exc or ""),
                    panel=self.panel,
                    panel_family="pasarguard",
                    safe_context={"status_code": response.status_code},
                ) from exc

        error_cls = PasarGuardReadError
        error_code = f"pasarguard_http_{response.status_code}"
        if response.status_code in {401, 403}:
            error_cls = PasarGuardAuthenticationError if response.status_code == 401 else PasarGuardWriteForbiddenError
            error_code = "pasarguard_auth_failed" if response.status_code == 401 else "pasarguard_write_forbidden"
        elif method in {"POST", "PUT", "PATCH", "DELETE"}:
            error_cls = PasarGuardWriteForbiddenError if response.status_code == 403 else PasarGuardIntegrationError
        safe_text = sanitize_error_value(getattr(response, "text", ""), panel=self.panel)
        raise error_cls(
            "درخواست PasarGuard با خطای HTTP برگشت.",
            error_code=error_code,
            action=f"{method} {path}",
            technical_detail=safe_text,
            panel=self.panel,
            panel_family="pasarguard",
            safe_context={"status_code": response.status_code},
        )

    def get_system(self):
        return self.request("GET", "/api/system")

    def health(self):
        return self.get_system()

    def _groups_from_payload(self, payload, *, source="groups"):
        if isinstance(payload, list):
            groups = payload
        elif isinstance(payload, dict):
            groups = payload.get("groups") or payload.get("items") or payload.get("data") or []
        else:
            groups = []
        if not isinstance(groups, list):
            return []
        normalized = []
        for group in groups:
            if isinstance(group, dict):
                item = dict(group)
                item["_pasarguard_group_source"] = source
                normalized.append(item)
        return normalized

    def list_groups(self):
        try:
            payload = self.request("GET", "/api/groups")
        except PasarGuardWriteForbiddenError as exc:
            status_code = (getattr(exc, "safe_context", {}) or {}).get("status_code")
            if status_code == 403:
                return self.list_groups_simple()
            raise
        return self._groups_from_payload(payload, source="groups")

    def list_groups_simple(self):
        payload = self.request("GET", "/api/groups/simple")
        return self._groups_from_payload(payload, source="groups_simple")

    def get_group(self, group_id):
        return self.request("GET", f"/api/group/{int(group_id)}")

    def get_inbounds(self):
        return self.request("GET", "/api/inbounds")

    def get_user(self, username):
        return self.request("GET", f"/api/user/{username}", expected_statuses=(200,))

    def create_user(self, payload):
        return self.request("POST", "/api/user", json=payload, expected_statuses=(200, 201))

    def modify_user(self, username, payload):
        return self.request("PUT", f"/api/user/{username}", json=payload, expected_statuses=(200,))

    def set_user_disabled(self, username, *, disabled=True):
        return self.request("PUT", f"/api/user/{username}/disabled", json={"disabled": bool(disabled)}, expected_statuses=(200,))

    def disable_user(self, username):
        return self.set_user_disabled(username, disabled=True)

    def enable_user(self, username):
        return self.set_user_disabled(username, disabled=False)

    def reset_user_usage(self, username):
        return self.request("POST", f"/api/user/{username}/reset", expected_statuses=(200,))

    def revoke_subscription(self, username):
        return self.request("POST", f"/api/user/{username}/revoke_sub", expected_statuses=(200,))

    def delete_user(self, username):
        try:
            self.request("DELETE", f"/api/user/{username}", expected_statuses=(200, 204))
        except PasarGuardIntegrationError as exc:
            raise PasarGuardDeleteUserError(
                "حذف کاربر PasarGuard ناموفق بود.",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family="pasarguard",
                safe_context=getattr(exc, "safe_context", {}),
            ) from exc
        return True

    def _subscription_get(self, url, *, accept="application/json"):
        headers = {**self._headers(), "Accept": accept}
        try:
            return self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PasarGuardReadError(
                "دریافت خروجی subscription از PasarGuard ناموفق بود.",
                error_code="pasarguard_links_fetch_failed",
                action="fetch_native_links",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family="pasarguard",
            ) from exc

    def _raw_links_fallback(self, subscription_url):
        raw_url = subscription_raw_url(subscription_url)
        if not raw_url:
            return [], {"error_code": "pasarguard_raw_browser_links_disabled", "body_links_count": None}
        response = self._subscription_get(raw_url, accept="application/json, text/plain;q=0.8, */*;q=0.5")
        diagnostic = {
            "status_code": response.status_code,
            "content_type": _content_type(response),
            "body_links_count": None,
            "error_code": "",
        }
        if response.status_code not in {200, 204}:
            diagnostic["error_code"] = f"pasarguard_http_{response.status_code}"
            return [], diagnostic
        links = []
        try:
            payload = response.json()
            raw_value = None
            if isinstance(payload, dict):
                body = payload.get("body")
                raw_value = body.get("links") if isinstance(body, dict) else payload.get("links")
            diagnostic["body_links_count"] = len(raw_value) if isinstance(raw_value, list) else None
            links = raw_links_from_payload(payload)
        except ValueError:
            links = parse_raw_subscription(response.text or "")
        if not links:
            diagnostic["error_code"] = "pasarguard_raw_browser_links_disabled"
        return links, diagnostic

    def fetch_native_links(self, subscription_url):
        links_url = subscription_format_url(subscription_url, "links")
        if not links_url:
            raise PasarGuardReadError(
                "آدرس subscription برای دریافت لینک‌های native معتبر نیست.",
                error_code="pasarguard_links_fetch_failed",
                action="fetch_native_links",
                panel=self.panel,
                panel_family="pasarguard",
            )
        response = self._subscription_get(links_url, accept="text/plain, application/json;q=0.8, */*;q=0.5")
        links = parse_raw_subscription(response.text or "") if response.status_code in {200, 204} else []
        if links:
            return links

        fallback_links, raw_diagnostic = self._raw_links_fallback(subscription_url)
        if fallback_links:
            return fallback_links

        disabled_statuses = {404, 405, 406, 410, 415}
        if response.status_code in disabled_statuses:
            error_code = "pasarguard_links_format_disabled"
        elif response.status_code in {200, 204}:
            error_code = "pasarguard_links_empty"
        else:
            error_code = "pasarguard_links_fetch_failed"
        safe_text = sanitize_error_value(response.text, panel=self.panel)
        raise PasarGuardReadError(
            "دریافت لینک‌های native PasarGuard ناموفق بود.",
            error_code=error_code,
            action="fetch_native_links",
            technical_detail=safe_text,
            panel=self.panel,
            panel_family="pasarguard",
            safe_context={
                "status_code": response.status_code,
                "content_type": _content_type(response),
                "raw_fallback": raw_diagnostic,
            },
        )

    def fetch_raw_subscription(self, subscription_url):
        raw_url = subscription_raw_url(subscription_url)
        if not raw_url:
            return ""
        try:
            response = self.session.get(
                raw_url,
                headers={**self._headers(), "Accept": "application/json, text/plain;q=0.8, */*;q=0.5"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PasarGuardReadError(
                "دریافت subscription raw از PasarGuard ناموفق بود.",
                error_code="pasarguard_subscription_fetch_failed",
                action="fetch_raw_subscription",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family="pasarguard",
            ) from exc
        if response.status_code in {200, 204}:
            return response.text or ""
        raise PasarGuardReadError(
            "دریافت subscription raw از PasarGuard با خطای HTTP برگشت.",
            error_code="pasarguard_subscription_fetch_failed",
            action="fetch_raw_subscription",
            technical_detail=sanitize_error_value(response.text, panel=self.panel),
            panel=self.panel,
            panel_family="pasarguard",
            safe_context={"status_code": response.status_code},
        )

    def fetch_subscription(self, subscription_url):
        value = str(subscription_url or "").strip()
        if not value:
            return ""
        try:
            response = self.session.get(value, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise PasarGuardReadError(
                "دریافت subscription از PasarGuard ناموفق بود.",
                error_code="pasarguard_subscription_fetch_failed",
                action="fetch_subscription",
                technical_detail=str(exc or ""),
                panel=self.panel,
                panel_family="pasarguard",
            ) from exc
        if response.status_code in {200, 204}:
            return response.text or ""
        raise PasarGuardReadError(
            "دریافت subscription از PasarGuard با خطای HTTP برگشت.",
            error_code="pasarguard_subscription_fetch_failed",
            action="fetch_subscription",
            technical_detail=sanitize_error_value(response.text, panel=self.panel),
            panel=self.panel,
            panel_family="pasarguard",
            safe_context={"status_code": response.status_code},
        )
