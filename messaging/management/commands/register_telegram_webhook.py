"""
Point a Telegram bot at this platform's webhook.

Telegram has no dashboard for webhooks: registration is one `setWebhook`
call, which is also where the `secret_token` is set. This command performs
it for a `MessagingAccount` through the adapter, which is also what the
account view uses — one implementation, two doors.

Usage:
    python manage.py register_telegram_webhook <account_id>
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Register this platform as a Telegram bot webhook.'

    def add_arguments(self, parser):
        parser.add_argument('account_id', type=int)

    def handle(self, *args, **options):
        from chat.tools.messaging.common import Unsupported
        from chat.tools.messaging.telegram import register_webhook
        from messaging.models import MessagingAccount

        try:
            account = MessagingAccount.objects.get(id=options['account_id'])
        except MessagingAccount.DoesNotExist:
            raise CommandError('No such messaging account.')
        if account.channel != 'telegram':
            raise CommandError(f'Account is {account.channel}, not telegram.')
        try:
            url = register_webhook(
                account.user_id, account,
                (os.environ.get('PUBLIC_URL') or '').rstrip('/'))
        except Unsupported as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f'Webhook registered: {url}'))
