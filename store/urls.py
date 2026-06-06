from django.urls import path
from . import views
from . import bot_views

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('support/', views.support, name='support'),
    path('support/messages/', views.support_messages, name='support_messages'),
    path('support/send/', views.support_send_message, name='support_send_message'),
    path('account/', views.account, name='account'),
    path('order/<uuid:order_id>/', views.order_detail, name='order_detail'),
    path('order/<uuid:order_id>/delete/', views.delete_order, name='delete_order'),
    path('r/<str:referral_code>/', views.referral_landing, name='referral_landing'),
    path('my-access/', views.my_configurations, name='my_configurations'),
    path('my-configs/', views.my_configurations, name='legacy_my_configurations'),
    path('create-order/<int:plan_id>/', views.create_order, name='create_order'),
    path('checkout/<str:tracking_code>/', views.checkout, name='checkout'),
    path('referrals/redeem/', views.redeem_referral_reward, name='redeem_referral_reward'),
    path('access/<uuid:config_id>/renew/', views.renew_config, name='renew_config'),
    path('access/<str:tracking_code>/<uuid:config_id>/', views.config_detail, name='config_detail'),
    path('access/<str:tracking_code>/<uuid:config_id>/usage/', views.config_usage, name='config_usage'),
    path('config/<str:tracking_code>/<uuid:config_id>/', views.config_detail, name='legacy_config_detail'),
    path('config/<str:tracking_code>/<uuid:config_id>/usage/', views.config_usage, name='legacy_config_usage'),
    path('api/discount/preview/', views.discount_preview, name='discount_preview'),
    path('bot/<str:provider>/<str:webhook_secret>/webhook/', bot_views.bot_webhook, name='bot_webhook'),
]
