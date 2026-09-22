"""Refresh the model catalogue from OpenRouter's live /models endpoint.

Same service the staff button calls (`llm/catalog_refresh.py`), for host cron
— production has no beat service, so periodic work runs as `docker exec`
against the live container (zero downtime, zero rebuilds):

    docker compose -f docker-compose.prod.yml exec -T backend \\
        python manage.py refresh_models
"""
from django.core.management.base import BaseCommand

from llm.catalog_refresh import RefreshError, RefreshInProgress, refresh_catalog


class Command(BaseCommand):
    help = 'Diff the live OpenRouter catalogue against held model rows.'

    def handle(self, *args, **options):
        try:
            summary = refresh_catalog()
        except RefreshInProgress:
            self.stdout.write('Another refresh is running; exiting.')
            return
        except RefreshError as exc:
            self.stderr.write(f'Refresh refused: {exc}')
            return
        retired = ', '.join(
            r['value'] for r in summary['retired']) or 'none'
        self.stdout.write(
            f"added={summary['added']} updated={summary['updated']} "
            f"retired=[{retired}] "
            f"new_upstream={len(summary['new_upstream'])} "
            f"affected_agents={len(summary['affected_agents'])}"
        )
