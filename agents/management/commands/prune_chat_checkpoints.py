"""
Delete old chat checkpoints, keeping the latest few per thread.

    python manage.py prune_chat_checkpoints
    python manage.py prune_chat_checkpoints --dry-run
    python manage.py prune_chat_checkpoints --keep 5

Runs on anything but the Postgres saver as a no-op that says so — see
`chat/turn/prune.py` for what a chat thread is and why the latest few are
all a resume can ever need.
"""

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Prune old chat checkpoints on the Postgres saver.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count what would be pruned without deleting anything.',
        )
        parser.add_argument(
            '--keep',
            type=int,
            default=None,
            help='Checkpoints kept per thread (default: CHAT_CHECKPOINT_KEEP or 3).',
        )

    def handle(self, *args, **options):
        from chat.turn import checkpoints
        from chat.turn.prune import prune_chat_checkpoints

        self.stdout.write(f'Checkpointer: {checkpoints._configured()}')
        tally = async_to_sync(prune_chat_checkpoints)(
            keep=options['keep'], dry_run=options['dry_run'])
        if tally.get('status') == 'skipped':
            self.stdout.write(f"Skipped: {tally['reason']}.")
            return
        self.stdout.write(self.style.SUCCESS(
            ' '.join(f'{k}={v}' for k, v in sorted(tally.items()))))
