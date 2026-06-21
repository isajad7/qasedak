
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from store.admin_views import (
    card_receipts_report,
    customer_message,
    customer_review,
    order_review,
    order_workbench,
    owner_dashboard,
    revenue_control_center,
    service_review,
    service_workbench,
    setup_center,
    setup_wizard_index,
    setup_wizard_step,
    support_review,
    support_workbench,
)

urlpatterns = [
    path(
        'admin/card-receipts-report/',
        admin.site.admin_view(card_receipts_report),
        name='admin_card_receipts_report',
    ),
    path(
        'admin/store/setup/',
        admin.site.admin_view(setup_center),
        name='admin_store_setup_center',
    ),
    path(
        'admin/store/dashboard/',
        admin.site.admin_view(owner_dashboard),
        name='admin_store_owner_dashboard',
    ),
    path(
        'admin/store/revenue/control/',
        admin.site.admin_view(revenue_control_center),
        name='admin_store_revenue_control',
    ),
    path(
        'admin/store/orders/workbench/',
        admin.site.admin_view(order_workbench),
        name='admin_store_order_workbench',
    ),
    path(
        'admin/store/orders/<int:order_id>/review/',
        admin.site.admin_view(order_review),
        name='admin_store_order_review',
    ),
    path(
        'admin/store/support/workbench/',
        admin.site.admin_view(support_workbench),
        name='admin_store_support_workbench',
    ),
    path(
        'admin/store/support/<int:support_id>/review/',
        admin.site.admin_view(support_review),
        name='admin_store_support_review',
    ),
    path(
        'admin/store/services/workbench/',
        admin.site.admin_view(service_workbench),
        name='admin_store_service_workbench',
    ),
    path(
        'admin/store/services/<int:vpn_client_id>/review/',
        admin.site.admin_view(service_review),
        name='admin_store_service_review',
    ),
    path(
        'admin/store/customers/<int:customer_id>/review/',
        admin.site.admin_view(customer_review),
        name='admin_store_customer_review',
    ),
    path(
        'admin/store/customers/<int:customer_id>/message/',
        admin.site.admin_view(customer_message),
        name='admin_store_customer_message',
    ),
    path(
        'admin/store/setup/wizard/',
        admin.site.admin_view(setup_wizard_index),
        name='admin_store_setup_wizard',
    ),
    path(
        'admin/store/setup/wizard/store/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'store')),
        name='admin_store_setup_wizard_store',
    ),
    path(
        'admin/store/setup/wizard/payment/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'payment')),
        name='admin_store_setup_wizard_payment',
    ),
    path(
        'admin/store/setup/wizard/telegram/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'telegram')),
        name='admin_store_setup_wizard_telegram',
    ),
    path(
        'admin/store/setup/wizard/telegram-proxy/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'telegram-proxy')),
        name='admin_store_setup_wizard_telegram_proxy',
    ),
    path(
        'admin/store/setup/wizard/panel/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'panel')),
        name='admin_store_setup_wizard_panel',
    ),
    path(
        'admin/store/setup/wizard/inbounds/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'inbounds')),
        name='admin_store_setup_wizard_inbounds',
    ),
    path(
        'admin/store/setup/wizard/plans/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'plans')),
        name='admin_store_setup_wizard_plans',
    ),
    path(
        'admin/store/setup/wizard/routes/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'routes')),
        name='admin_store_setup_wizard_routes',
    ),
    path(
        'admin/store/setup/wizard/review/',
        admin.site.admin_view(lambda request: setup_wizard_step(request, 'review')),
        name='admin_store_setup_wizard_review',
    ),
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    path('', include('payments.urls')),
    path('', include('store.urls')),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
