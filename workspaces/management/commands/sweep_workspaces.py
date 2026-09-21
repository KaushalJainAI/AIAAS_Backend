from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Hibernate idle workspaces (same sweep as the beat task).'

    def handle(self, *args, **options):
        from workspaces.sweep import run_workspace_sweep

        counts = run_workspace_sweep()
        self.stdout.write(f"{counts.get('hibernated', 0)} workspace(s) hibernated.")
