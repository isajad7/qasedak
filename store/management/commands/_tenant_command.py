import json

from django.core.management.base import BaseCommand, CommandError

from store.deployment.owner_toolkit import OwnerToolkitError, TenantOwnerToolkit


class SafeTenantCommand(BaseCommand):
    def toolkit(self):
        return TenantOwnerToolkit()

    def write_result(self, result):
        self.stdout.write(json.dumps(result.safe_dict(), ensure_ascii=True, sort_keys=True, default=str))

    def run_safely(self, func):
        try:
            return func()
        except OwnerToolkitError as exc:
            raise CommandError(str(exc)) from exc
