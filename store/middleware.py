import hashlib
import uuid

from django.conf import settings
from django.core.signing import BadSignature
from django.utils import timezone

from .models import Customer


CUSTOMER_COOKIE_NAME = "customer_key"
CUSTOMER_COOKIE_SALT = "customer-key"
LEGACY_CUSTOMER_COOKIE_NAME = "vpn_customer_id"
LEGACY_CUSTOMER_COOKIE_SALT = "vpn-customer"
CUSTOMER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
CUSTOMER_SEEN_UPDATE_SECONDS = 15 * 60


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def hash_user_agent(request):
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


class CustomerTrackingMiddleware:
    """
    Creates a lightweight persistent customer account for public storefront pages.
    The browser stores only a signed UUID; all order ownership lives server-side.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.customer = None
        should_track = self.should_track_request(request)
        customer = None
        needs_cookie = False
        clear_legacy_cookie = False

        if should_track:
            customer, needs_cookie, clear_legacy_cookie = self.get_or_create_customer(request)
            request.customer = customer

        response = self.get_response(request)

        if should_track and customer and (needs_cookie or clear_legacy_cookie):
            response.set_signed_cookie(
                CUSTOMER_COOKIE_NAME,
                str(customer.public_id),
                salt=CUSTOMER_COOKIE_SALT,
                max_age=CUSTOMER_COOKIE_MAX_AGE,
                httponly=True,
                secure=not settings.DEBUG,
                samesite="Lax",
            )
        if should_track and clear_legacy_cookie:
            response.delete_cookie(
                LEGACY_CUSTOMER_COOKIE_NAME,
                samesite="Lax",
            )

        return response

    def should_track_request(self, request):
        path = request.path_info or ""
        if path.startswith(("/admin/", "/bot/", settings.STATIC_URL, settings.MEDIA_URL)):
            return False
        return request.method in {"GET", "POST"}

    def get_or_create_customer(self, request):
        public_id, cookie_source = self.get_customer_id_from_cookie(request)
        ip_address = get_client_ip(request)
        user_agent_hash = hash_user_agent(request)
        needs_cookie = False
        clear_legacy_cookie = cookie_source == "legacy"

        if public_id:
            customer = Customer.objects.filter(public_id=public_id, is_active=True).first()
            if customer:
                self.touch_customer(customer, ip_address=ip_address, user_agent_hash=user_agent_hash)
                return customer, cookie_source != "current", clear_legacy_cookie

        customer = Customer.objects.create(
            first_ip=ip_address,
            last_ip=ip_address,
            user_agent_hash=user_agent_hash,
            last_seen_at=timezone.now(),
        )
        needs_cookie = True
        return customer, needs_cookie, clear_legacy_cookie

    def get_customer_id_from_cookie(self, request):
        cookie_candidates = (
            (CUSTOMER_COOKIE_NAME, CUSTOMER_COOKIE_SALT, "current"),
            (LEGACY_CUSTOMER_COOKIE_NAME, LEGACY_CUSTOMER_COOKIE_SALT, "legacy"),
        )
        for cookie_name, cookie_salt, source in cookie_candidates:
            try:
                raw_value = request.get_signed_cookie(cookie_name, salt=cookie_salt)
                return uuid.UUID(str(raw_value)), source
            except (KeyError, BadSignature, ValueError, TypeError):
                continue
        return None, ""

    def touch_customer(self, customer, *, ip_address, user_agent_hash):
        now = timezone.now()
        if (now - customer.last_seen_at).total_seconds() < CUSTOMER_SEEN_UPDATE_SECONDS:
            return
        customer.last_seen_at = now
        customer.last_ip = ip_address
        if user_agent_hash:
            customer.user_agent_hash = user_agent_hash
        customer.save(update_fields=["last_seen_at", "last_ip", "user_agent_hash", "updated_at"])
