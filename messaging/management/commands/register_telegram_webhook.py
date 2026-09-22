"""
Point a Telegram bot at this platform's webhook.

Telegram has no dashboard for webhooks: registration is one `setWebhook`
call, which is also where the `secret_token` is set. This command performs
it for a `MessagingAccount`, using the account's own path secret as that
token — the same value `message_hook` verifies in
`X-Telegram-Bot-Api-Secret-Token` — and marks the account verified when
Telegram answers ok.

Usage:
    python manage.py register_telegram_webhook <account_id>
"""
from __future__ import annotations

import os

import httpx
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Register this platform as a Telegram bot webhook.'

    def add_arguments(self, parser):
        parser.add_argument('account_id', type=int)

    def handle(self, *args, **options):
        from django.urls import reverse

        from messaging.models import MessagingAccount

        try:
            account = MessagingAccount.objects.get(id=options['account_id'])
        except MessagingAccount.DoesNotExist:
            raise CommandError('No such messaging account.')
        if account.channel != 'telegram':
            raise CommandError(f'Account is {account.channel}, not telegram.')

        token = self._bot_token(account)
        public = (os.environ.get('PUBLIC_URL') or '').rstrip('/')
        if not public:
            raise CommandError('PUBLIC_URL is not set; cannot build the webhook URL.')
        url = f'{public}{reverse("messaging:message_hook", args=["telegram", account.secret])}'
        try:
            resp = httpx.post(
                f'https://api.telegram.org/bot{token}/setWebhook',
                json={'url': url, 'secret_token': account.secret,
                      'allowed_updates': ['message', 'edited_message']},
                timeout=20,
            )
            payload = resp.json()
        except Exception as exc:
            raise CommandError(f'Telegram could not be reached: {exc}')
        if not payload.get('ok'):
            raise CommandError(
                f"Telegram refused: {payload.get('description', 'unknown error')}.")
        account.verified = True
        account.save(update_fields=['verified', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'Webhook registered: {url}'))

    def _bot_token(self, account) -> str:
        from credentials.manager import CredentialManager

        slug = account.credential_slug or 'telegram'
        credential = CredentialManager.lookup_by_slug_sync(slug, account.user_id)
        if credential is None:
            raise CommandError(
                'No telegram credential for this user. Store the bot token first.')
        data = credential.get_credential_data() or {}
        token = data.get('token') or data.get('bot_token')
        if not token:
            raise CommandError('The telegram credential holds no token.')
        return str(token)
