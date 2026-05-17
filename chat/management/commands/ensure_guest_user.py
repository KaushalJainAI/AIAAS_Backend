"""Idempotently create the shared guest chat user."""
from django.core.management.base import BaseCommand

from chat.guest import get_guest_user_sync


class Command(BaseCommand):
    help = "Ensure the shared guest chat user exists."

    def handle(self, *args, **options):
        user = get_guest_user_sync()
        self.stdout.write(self.style.SUCCESS(f"Guest user ready: id={user.pk}, email={getattr(user, 'email', '?')}"))
