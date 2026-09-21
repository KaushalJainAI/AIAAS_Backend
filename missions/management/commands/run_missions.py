from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Start mission runs whose next_wake_at is due.'

    def handle(self, *args, **options):
        from missions.sweep import run_mission_sweep

        counts = run_mission_sweep()
        self.stdout.write(
            f"Fired {counts.get('fired', 0)}, "
            f"failed {counts.get('failed', 0)}.")
