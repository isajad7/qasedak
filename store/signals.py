from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order
from .bots import notify_new_order
from .referrals import process_referral_purchase


@receiver(post_save, sender=Order)
def create_referral_rewards_for_completed_order(sender, instance, **kwargs):
    process_referral_purchase(instance)


@receiver(post_save, sender=Order)
def notify_admin_bot_for_new_order(sender, instance, created, **kwargs):
    if created:
        transaction.on_commit(lambda: notify_new_order(instance))
