import re

from django.db import IntegrityError, transaction

from store.orchestrator_v2.models import TenantInstance


TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
BASE_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
QASEDAK_SUBDOMAIN_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
LEGACY_QASEDAK_SUBDOMAIN_RE = re.compile(
    r"^bot_[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
RESERVED_TENANT_SLUGS = {"www", "admin", "api", "control", "mail", "root", "qasedak", "bots"}


class SubdomainError(Exception):
    pass


class SubdomainManager:
    default_base_domain = "panelwpvideo.ir"
    prefix = ""

    def generate(self, tenant_id, base_domain=None):
        tenant = self.validate_tenant_id(tenant_id)
        domain = self.validate_base_domain(base_domain or self.default_base_domain)
        subdomain = f"{self.prefix}{tenant}.{domain}"
        return self.validate_subdomain(subdomain)

    def assign(self, instance, base_domain):
        subdomain = self.generate(instance.tenant_id, base_domain)
        with transaction.atomic():
            conflict = (
                TenantInstance.objects.select_for_update()
                .filter(subdomain=subdomain)
                .exclude(pk=instance.pk)
                .exists()
            )
            if conflict:
                raise SubdomainError("Subdomain is already allocated.")
            instance.subdomain = subdomain
            instance.domain = subdomain
            instance.save(update_fields=["subdomain", "domain", "updated_at"])
        return subdomain

    def mapping(self, instance):
        return {
            "tenant_id": instance.tenant_id,
            "subdomain": instance.subdomain,
            "port": instance.port,
            "container_id": instance.container_id,
        }

    def validate_tenant_id(self, tenant_id):
        value = self.normalize_tenant_id(tenant_id)
        if not TENANT_RE.match(value):
            raise SubdomainError("Invalid tenant_id for subdomain generation.")
        if value in RESERVED_TENANT_SLUGS:
            raise SubdomainError("Reserved tenant_id for subdomain generation.")
        return value

    def normalize_tenant_id(self, tenant_id):
        value = str(tenant_id or "").strip().lower()
        value = re.sub(r"[^a-z0-9-]+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        if not value:
            raise SubdomainError("Empty tenant_id for subdomain generation.")
        if len(value) > 50:
            raise SubdomainError("Tenant_id is too long for DNS and database identifiers.")
        return value

    def validate_base_domain(self, base_domain):
        value = str(base_domain or "").strip().lower().rstrip(".")
        value = value.removeprefix("https://").removeprefix("http://").strip("/")
        if not BASE_DOMAIN_RE.match(value):
            raise SubdomainError("Invalid base domain.")
        return value

    def validate_subdomain(self, subdomain):
        value = str(subdomain or "").strip().lower().rstrip(".")
        if not (QASEDAK_SUBDOMAIN_RE.match(value) or LEGACY_QASEDAK_SUBDOMAIN_RE.match(value)):
            raise SubdomainError("Invalid Qasedak subdomain.")
        return value
