"""
Move rows off the LLM providers this platform no longer routes.

Anthropic, DeepSeek, Gemini, Perplexity, xAI and HuggingFace were dropped in
favour of reaching the same models through OpenRouter. Rows naming them would
otherwise resolve to a node type with no handler and fail at execution time,
so they are rewritten here rather than left to break at the next run.

The rewrite is provider + model together: OpenRouter namespaces model ids by
vendor, so `gemini` / `gemini-2.5-flash` becomes `openrouter` /
`google/gemini-2.5-flash`. Ids that already carry a namespace are left alone —
some rows were written with OpenRouter-style ids while the provider column
still said `gemini`, and prefixing those again would produce `google/google/…`.

Workflow node JSON is swept too: a saved canvas holds the provider as a node
`type`, which the compiler resolves against the same registry.
"""
from django.db import migrations

from llm.providers import REPLACEMENT_PROVIDER, RETIRED_PROVIDERS, legacy_model_id

#: Set once here so a later edit to the default cannot silently rewrite history.
FALLBACK_MODEL = 'nvidia/nemotron-3-super-120b-a12b:free'


def _rewrite_rows(model, provider_field='llm_provider', model_field='llm_model'):
    """Repoint every row whose provider was retired. Returns rows touched."""
    touched = 0
    for row in model.objects.filter(**{f'{provider_field}__in': RETIRED_PROVIDERS}):
        old_provider = getattr(row, provider_field)
        old_model = getattr(row, model_field) or ''
        setattr(row, provider_field, REPLACEMENT_PROVIDER)
        setattr(row, model_field, legacy_model_id(old_provider, old_model) or FALLBACK_MODEL)
        row.save(update_fields=[provider_field, model_field])
        touched += 1
    return touched


def _rewrite_workflow_nodes(Workflow):
    """Repoint provider node types inside saved canvas JSON."""
    touched = 0
    for wf in Workflow.objects.exclude(nodes=[]).exclude(nodes=None).iterator():
        nodes = wf.nodes
        if not isinstance(nodes, list):
            continue
        changed = False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            # ReactFlow keeps the node type at the top level; some revisions of
            # the editor also mirrored it into `data.type`.
            for holder, key in ((node, 'type'), (node.get('data'), 'type')):
                if not isinstance(holder, dict):
                    continue
                if holder.get(key) in RETIRED_PROVIDERS:
                    holder[key] = REPLACEMENT_PROVIDER
                    changed = True
        if changed:
            wf.nodes = nodes
            wf.save(update_fields=['nodes'])
            touched += 1
    return touched


def forwards(apps, schema_editor):
    Workflow = apps.get_model('orchestrator', 'Workflow')
    _rewrite_rows(Workflow)
    _rewrite_workflow_nodes(Workflow)

    _rewrite_rows(apps.get_model('core', 'UserProfile'))
    _rewrite_rows(apps.get_model('chat', 'ChatSession'))


def backwards(apps, schema_editor):
    """
    Deliberately a no-op rather than an error.

    The original provider is not recoverable: `openrouter` + `google/gemini-x`
    is what both a migrated row and a row that always used OpenRouter look
    like. Reversing would have to guess, and guessing wrong points a working
    row at a provider with no handler. Unapplying leaves the data valid — the
    retired providers simply stay unused.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('orchestrator', '0011_workflow_agent_context_workflow_guardrails_and_more'),
        ('core', '0006_alter_userprofile_llm_model'),
        ('chat', '0008_alter_chatsession_llm_model'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
