from django.urls import path

from . import views


urlpatterns = [
    path("webhooks/smsforwarder/", views.smsforwarder_webhook, name="smsforwarder_webhook"),
]
