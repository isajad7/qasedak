from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import Store


class Command(BaseCommand):
    help = "Set renewal_reminders_start_at to now for active stores."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all-stores",
            action="store_true",
            help="Update inactive stores too.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        queryset = Store.objects.all() if options["all_stores"] else Store.objects.filter(is_active=True)
        updated = queryset.update(renewal_reminders_start_at=now, updated_at=now)
        self.stdout.write(
            self.style.SUCCESS(
                f"renewal_reminders_start_at={now.isoformat()} updated_stores={updated}"
            )
        )
