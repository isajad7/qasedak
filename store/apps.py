from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StoreConfig(AppConfig):
    name = 'store'
    verbose_name = _('Shop')

    def ready(self):
        import store.signals  # noqa: F401
