"""
Data migration: empty `agent_context['connectors']` wherever it holds slugs.

The field was decorative. `AgentSerializer` validated it against a hardcoded
`CONNECTOR_IDS = {'gdrive', 'gmail', 'sheets', 'photos', 'calendar', 'slack'}`,
stored it, and handed it back — and nothing on the run path ever read it. The
`mcp` grant resolved *every* connection the user had, so an agent that named
Gmail alone was also given Calendar, Drive, Notion and Slack.

Now that it is enforced it holds `MCPServer` ids, and the old values cannot be
carried across:

  * The set had drifted from the catalogue in both directions. `photos` names
    no server that has ever existed, and Notion and Fetch could not be named at
    all — so a mapping would be partial by construction.
  * Mapping the four that do resolve would *newly restrict* agents that have
    been running unrestricted. That is the trap `kb_scope_for` documents for
    knowledge bases: a selection that was only ever decorative must not become
    enforcing the moment someone reads it, or an agent silently loses tools its
    brief assumes it has.

So they are cleared, which is what "no choice was ever really made" looks like
in the new field — and empty means unrestricted, exactly the behaviour every
one of these agents has today. The user picks connections deliberately, once,
from a list that is now the real catalogue.

Non-integer entries are also skipped at read time (`mcp_scope_for`), so a row
this migration does not reach — a fixture loaded later, a hand-edited JSON
column — degrades the same way rather than to an agent with no connectors and
no explanation.
"""
from django.db import migrations


def _clear_slugs(apps, schema_editor):
    SubAgent = apps.get_model("orchestrator", "SubAgent")

    for agent in SubAgent.objects.exclude(agent_context={}).iterator():
        ctx = agent.agent_context or {}
        connectors = ctx.get("connectors")
        if not connectors:
            continue
        # Keep genuine ids: this migration can run after a row was written by a
        # newer client (a re-run, a restored dump), and dropping those would
        # widen an agent someone deliberately narrowed.
        kept = [c for c in connectors
                if isinstance(c, int) and not isinstance(c, bool)]
        if kept == list(connectors):
            continue
        ctx["connectors"] = kept
        agent.agent_context = ctx
        agent.save(update_fields=["agent_context"])


def _reverse(apps, schema_editor):
    """Nothing to restore — the slugs named no connection that could be reached.

    Not `RunPython.noop`, because a reverse that silently does nothing reads as
    "the data came back". It did not; there is no mapping from an id to the slug
    it was never derived from.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("orchestrator", "0020_trigger_schedule_config"),
    ]

    operations = [
        migrations.RunPython(_clear_slugs, reverse_code=_reverse),
    ]
