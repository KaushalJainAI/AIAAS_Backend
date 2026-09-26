"""
Install curated subagent packs for users from the server shell.

Packs are code (`agents/gallery/`, `PACKS`), not DB rows — the deployment DB
only holds each user's installs (`SubAgent.template_slug`). Copy the updated
`Backend/` + frontend build to the server, then run e.g.::

    python manage.py install_packs --pack marketing --pack hiring \\
        --pack support --pack data-science --all-users

Idempotent: a template already installed for a user (matched on
`template_slug`) is skipped, so re-running installs only what is missing —
the same rule `POST /api/orchestrator/templates/install-pack/` follows.
Templates with required requirements are skipped as `needs setup` rather
than installed half-configured, for the same reason the endpoint refuses
them. Templates holding a grant whose engine is `none` on this server are
skipped as `engine unavailable` rather than installed unable to run.
Use `--dry-run` to see what would happen without writing anything.
"""
from __future__ import annotations

import types

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agents import gallery
from agents.models import SubAgent
from logs import revisions


def _fake_request(user):
    """What `AgentSerializer` needs of a request: just `.user`."""
    return types.SimpleNamespace(user=user)


def _install_slug(user, slug: str, *, dry_run: bool = False) -> str:
    """Install one template for one user. Returns installed|already|setup|invalid."""
    from agents.config import AgentSerializer
    from agents.views.capabilities import unavailable_grants

    if SubAgent.objects.filter(user=user, template_slug=slug).exists():
        return "already installed"
    entry = gallery.get(slug)
    if entry is None:
        return "no such template"
    requirements = entry.get("requirements") or []
    if [r for r in requirements if not r.get("optional")]:
        return "needs setup"
    if unavailable_grants(entry.get("config")):
        # Same honesty rule as the HTTP install: an agent whose engine is
        # `none` on this server would arrive unable to run.
        return "engine unavailable"
    config = dict(entry["config"])
    serializer = AgentSerializer(
        data=config, context={"request": _fake_request(user)}
    )
    if not serializer.is_valid():
        return "invalid configuration"
    if dry_run:
        return "would install"
    base_name = serializer.validated_data["name"]
    name = base_name
    counter = 1
    while SubAgent.objects.filter(user=user, name=name).exists():
        name = f"{base_name} ({counter})"
        counter += 1
    data = dict(serializer.validated_data, name=name)
    with transaction.atomic():
        agent = AgentSerializer.apply(SubAgent(user=user), data)
        agent.tags = list(entry.get("tags") or []) + [f"template:{slug}"]
        agent.icon = entry.get("icon", "")
        agent.template_slug = slug
        agent.save()
        AgentSerializer.sync_schedule(agent, data)
        revisions.record(agent, user=user, source="create")
    return "installed"


class Command(BaseCommand):
    help = (
        "Install curated subagent packs for users (idempotent). "
        "Defaults to the four newest packs for every user."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pack",
            action="append",
            dest="packs",
            default=[],
            help="Pack slug to install (repeatable). Defaults to "
                 "marketing, hiring, support, data-science.",
        )
        parser.add_argument(
            "--user",
            action="append",
            dest="users",
            default=[],
            help="Username or email to install for (repeatable). "
                 "Defaults to all users; --all-users makes that explicit.",
        )
        parser.add_argument(
            "--all-users",
            action="store_true",
            help="Install for every user in the table.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be installed without writing anything.",
        )

    def handle(self, *args, **options):
        packs = options["packs"] or ["marketing", "hiring", "support", "data-science"]
        unknown = [p for p in packs if p not in (gallery.PACKS or {})]
        if unknown:
            raise CommandError(
                f"No such pack(s): {', '.join(unknown)}. "
                f"Known packs: {', '.join(sorted(gallery.PACKS))}."
            )

        User = get_user_model()
        if options["users"]:
            users = []
            for ident in options["users"]:
                user = (
                    User.objects.filter(username=ident).first()
                    or User.objects.filter(email__iexact=ident).first()
                )
                if user is None:
                    raise CommandError(f"No user {ident!r}.")
                users.append(user)
        elif options["all_users"] or not options["users"]:
            users = list(User.objects.order_by("id"))
            if not users:
                raise CommandError("No users exist.")
        else:  # unreachable, kept so the default stays "everyone"
            users = list(User.objects.order_by("id"))

        dry_run = options["dry_run"]
        totals: dict[str, int] = {}
        for user in users:
            for pack in packs:
                for slug in gallery.PACKS[pack]:
                    outcome = _install_slug(user, slug, dry_run=dry_run)
                    totals[outcome] = totals.get(outcome, 0) + 1
                    if outcome not in ("already installed",):
                        self.stdout.write(f"{user.username}: {slug} — {outcome}")
        # Keep the coding lead's delegation scope correct if the code pack
        # was among those installed.
        if "code" in packs and not dry_run:
            from agents.views.gallery import _wire_coding_lead

            for user in users:
                _wire_coding_lead(user)

        summary = ", ".join(f"{v} {k}" for k, v in sorted(totals.items()))
        self.stdout.write(self.style.SUCCESS(
            f"Done ({'dry run, ' if dry_run else ''}{len(users)} user(s), "
            f"{len(packs)} pack(s)): {summary}."
        ))
