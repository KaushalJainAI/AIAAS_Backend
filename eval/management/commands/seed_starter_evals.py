"""
Seed starter eval suites into a user's account. See `eval/starter_kits.py`.

    python manage.py seed_starter_evals --user you@example.com
    python manage.py seed_starter_evals --user you@example.com --template analyst --template research
    python manage.py seed_starter_evals --user you@example.com --agent "Analyst"

Idempotent: suites are matched by `template_slug` (falling back to the kit
name for suites cloned before that column existed) and cases by name within
their suite, so re-running converges on what the file says. Cases the user
added themselves are kept — only kit cases are synced, never retired — which
is the deliberate difference from `benchmark install` (that one owns its
suites and retires removed cases; this one must not delete a user's own work).

Needs migration `eval.0005` (`template_slug`) applied first — `migrate` does it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Install/update starter eval suites for a user (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True,
                            help='Email or username owning the suites.')
        parser.add_argument('--template', action='append', default=[],
                            help='Starter kit slug (repeatable). Default: all kits.')
        parser.add_argument('--agent', default='',
                            help='Point the suites at this agent (id or name). '
                                 'Default: leave suites as they are.')

    def handle(self, *args, **options):
        from eval import starter_kits as kits

        User = get_user_model()
        ident = options['user']
        user = (User.objects.filter(email__iexact=ident).first()
                or User.objects.filter(username=ident).first())
        if user is None:
            raise CommandError(f'No user {ident!r}.')

        wanted = options['template'] or list(kits.STARTER_KITS)
        if isinstance(wanted, str):
            wanted = [wanted]
        unknown = [t for t in wanted if kits.get_kit(t) is None]
        if unknown:
            raise CommandError(
                f'Unknown kit(s): {", ".join(unknown)}. '
                f'Known: {", ".join(sorted(kits.STARTER_KITS))}.')

        agent = None
        if options['agent']:
            from agents.models import SubAgent
            ref = options['agent']
            agent = (SubAgent.objects.filter(user=user, id=ref).first()
                     if str(ref).isdigit() else None)
            if agent is None:
                agent = SubAgent.objects.filter(user=user, name=ref).first()
            if agent is None:
                raise CommandError(f'No agent {ref!r} for {ident!r}.')

        for slug in wanted:
            suite, n_created, n_updated = self._sync_kit(user, slug, agent)
            self.stdout.write(
                f'{slug}: suite "{suite.name}" '
                f'({n_created} cases added, {n_updated} updated)')

    def _sync_kit(self, user, slug: str, agent):
        from eval import graders as _graders
        from eval import starter_kits as kits
        from eval.models import EvalCase, EvalSuite

        kit = kits.get_kit(slug)

        suite = (EvalSuite.objects.filter(user=user, template_slug=slug).first()
                 or EvalSuite.objects.filter(user=user, name=kit['name']).first())
        if suite is None:
            suite = EvalSuite.objects.create(
                user=user, name=kit['name'][:200],
                description=kit.get('description', ''),
                subagent=agent, supervision='disagreement',
                template_slug=slug,
            )
        else:
            suite.description = kit.get('description', '')
            suite.template_slug = slug
            if agent is not None:
                suite.subagent = agent
            suite.save(update_fields=['description', 'template_slug', 'subagent', 'updated_at'])

        n_created = n_updated = 0
        for order, case_def in enumerate(kit['cases']):
            # Keep the judge-never-alone rule honest for seeds too: a kit
            # case that drifted must fail loudly here, not install quietly.
            validated = _graders.validate_case_graders(case_def.get('graders', []))
            _, created = EvalCase.objects.update_or_create(
                suite=suite, name=str(case_def.get('name', f'Case {order + 1}'))[:200],
                defaults={
                    'order': order,
                    'goal': str(case_def.get('goal', '')),
                    'input_data': dict(case_def.get('input_data', {}) or {}),
                    'reference': str(case_def.get('reference', '')),
                    'graders': validated,
                    'tags': ['starter', slug],
                    'is_active': True,
                },
            )
            if created:
                n_created += 1
            else:
                n_updated += 1
        return suite, n_created, n_updated
