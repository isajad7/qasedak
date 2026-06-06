
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from store.admin_views import card_receipts_report

urlpatterns = [
    path(
        'admin/card-receipts-report/',
        admin.site.admin_view(card_receipts_report),
        name='admin_card_receipts_report',
    ),
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    path('', include('payments.urls')),
    path('', include('store.urls')),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
