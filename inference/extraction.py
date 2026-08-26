"""
The LLM extraction engine (2026-08-18).

One turn per document against the schema: the model is asked for
`{field: {value, confidence}}`, the row's confidence is the *lowest* field
confidence (a row with one 0.4 field must be held even if the rest are 0.99),
and `ExtractedRow.apply_threshold()` decides accepted vs. needs_review.

Calls go through `llm.access.complete()` — the same funnel as the chat
agent — so credential resolution, the platform-key fallback and context
clamping are inherited rather than re-implemented. This is deliberately *not*
the `chat.vision` witness: that is conversational (an assistant holding one
image for follow-up questions), where extraction is a batch transform.

The shape follows `inference/tasks.py`: ORM stays in the sync function, and
only the provider call crosses into an event loop (`async_to_sync`). Running
the whole turn inside `asyncio.run` would trip Django's
`SynchronousOnlyOperation` on the first ORM access.

Replace semantics: re-running a schema over a document replaces its
accepted/needs_review rows (an LLM reread is newer and probably better); a
`reviewed`/`rejected` row is a human decision and is never overwritten.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction

from llm import access as llm
from .models import DEFAULT_EXTRACTION_MODEL, Document, ExtractedRow, ExtractionSchema

logger = logging.getLogger(__name__)

#: Soft cap on the document text handed to the model per turn. The provider
#: funnel clamps total input anyway; this keeps one document from crowding out
#: the prompt that defines the task.
MAX_DOC_TEXT_CHARS = 30_000

#: Image-ish file types are read by pixels; everything else by text.
_VISION_TYPES = ('image', 'video')

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured fields from a document. The user describes the "
    "document and gives its text content (when the document is an image, the "
    "pixels are attached). Respond with ONLY a JSON object of the form "
    '{"fields": {"<field_name>": {"value": <value>, "confidence": <0.0-1.0>}}} '
    "with one entry per requested field. Confidence must be honest: 1.0 only "
    "when the document states the value explicitly; lower when it must be "
    "inferred or is illegible; 0.0 when the document does not contain it. "
    "Numbers must be exact — never round, approximate, or infer a decimal "
    "point you cannot actually read. Never invent a field that is not "
    "requested."
)


def resolve_model(schema: ExtractionSchema) -> tuple[str, str]:
    """(provider, model) for a schema, resolved through the registry.

    A schema may pin its own model (`llm_model`); otherwise the default vision
    model applies. The provider is derived from the model's registry row where
    possible, falling back to `nvidia` for models that predate the registry.
    """
    model = schema.effective_model
    from llm.models import AIModel

    try:
        provider = AIModel.objects.get(value=model).provider.slug
    except AIModel.DoesNotExist:
        # The fallback stays — the registry can legitimately lag a provider's
        # catalogue, and a schema pinning a brand-new model should still run.
        # But an unregistered model is now *said*, because the silent version
        # sent a typo to NVIDIA and surfaced it as an opaque provider error on
        # every document in the batch. Typos are caught earlier, at the point
        # a user sets the field: `ExtractionSchemaSerializer.validate_llm_model`.
        if model != DEFAULT_EXTRACTION_MODEL:
            logger.warning(
                "[Extraction] Model '%s' is not in the registry; assuming provider "
                "'nvidia'. Register it if this is wrong.", model,
            )
        provider = 'nvidia'
    return provider, model


def _parse_reply(content: str) -> dict[str, dict[str, Any]]:
    """Parse the model's JSON reply into {field: {value, confidence}}."""
    text = content.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text.startswith('json'):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start == -1 or end <= start:
            raise ValueError('model reply was not JSON')
        payload = json.loads(text[start:end + 1])

    fields = payload.get('fields') if isinstance(payload, dict) else None
    if not isinstance(fields, dict):
        raise ValueError('model reply had no "fields" object')
    return fields


async def _extract_one(schema: ExtractionSchema, doc: Document, user_id: int,
                       *, provider: str, model: str) -> dict[str, Any]:
    """One document, one turn. Pure await — no ORM, so `async_to_sync` is safe."""
    kind = (doc.file_type or 'other').lower()
    attachments = [doc] if kind in _VISION_TYPES else None

    text = (doc.content_text or '')[:MAX_DOC_TEXT_CHARS]
    field_spec = json.dumps([
        {'name': f.get('name'), 'label': f.get('label'), 'type': f.get('type'),
         'required': bool(f.get('required'))}
        for f in schema.fields
    ], ensure_ascii=False)

    prompt = (
        f"Document name: {doc.name}\n"
        f"Requested fields (extract exactly these): {field_spec}\n"
        + (f"Document text content:\n{text}\n" if text else
           "(The document is attached as an image; read the pixels.)\n")
    )

    completion = await llm.complete(
        provider=provider,
        model=model,
        system_message=EXTRACTION_SYSTEM_PROMPT,
        prompt=prompt,
        user_id=user_id,
        temperature=0,
        max_tokens=2048,
        attachments=attachments,
    )

    allowed = {f.get('name') for f in schema.fields}
    fields = _parse_reply(completion.content)

    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"model returned fields not on the schema: {sorted(unknown)}")

    data: dict[str, Any] = {}
    field_confidence: dict[str, float] = {}
    for name in allowed:
        entry = fields.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"no entry for required field '{name}'")
        value = entry.get('value')
        conf = entry.get('confidence')
        if not isinstance(conf, (int, float)):
            conf = 0.0
        conf = max(0.0, min(1.0, float(conf)))
        data[name] = value
        field_confidence[name] = conf

    return {
        'document_name': doc.name,
        'document_id': doc.id,
        'data': data,
        'field_confidence': field_confidence,
        'confidence': min(field_confidence.values()) if field_confidence else 0.0,
    }


def run_extraction(document_ids: list[int], schema_id: int, user_id: int) -> dict[str, Any]:
    """Extract `document_ids` against the schema. Replace semantics per doc.

    Synchronous (ORM in this thread; the LLM call crosses via `async_to_sync`),
    so it runs unchanged from a sync view, a Celery task or a management
    command.
    """
    schema = ExtractionSchema.objects.get(id=schema_id, user_id=user_id)
    provider, model = resolve_model(schema)
    docs = list(Document.objects.filter(user_id=user_id, id__in=document_ids))

    stats = {'processed': 0, 'created': 0, 'needs_review': 0, 'held_decided': 0, 'errors': []}

    for doc in docs:
        try:
            payload = async_to_sync(_extract_one)(schema, doc, user_id,
                                                  provider=provider, model=model)
        except Exception as exc:
            logger.warning("[Extraction] %s failed: %s", doc.id, exc)
            stats['errors'].append({'document': doc.id, 'error': str(exc)})
            continue

        with transaction.atomic():
            decided = ExtractedRow.objects.filter(
                schema=schema, document_id=doc.id, status__in=('reviewed', 'rejected')
            )
            if decided.exists():
                stats['held_decided'] += 1
                continue

            ExtractedRow.objects.filter(
                schema=schema, document_id=doc.id, status__in=('accepted', 'needs_review')
            ).delete()

            row = ExtractedRow(
                schema=schema,
                document_id=doc.id,
                document_name=payload['document_name'],
                data=payload['data'],
                field_confidence=payload['field_confidence'],
                confidence=payload['confidence'],
            )
            row.apply_threshold()
            row.save()

            stats['processed'] += 1
            if row.status == 'needs_review':
                stats['needs_review'] += 1
            else:
                stats['created'] += 1

    return stats


def dispatch_extraction(document_ids: list[int], schema_id: int, user_id: int) -> dict[str, Any]:
    """Run the extraction, mirrored on the agent-run dispatch split.

    With `RUN_WORKFLOWS_ASYNC` the request answers 202 with a Celery task id;
    otherwise the run happens inside the request (local dev and tests have no
    Redis). Both paths share `run_extraction`, which is the point — a second
    start path is a second place for the guardrails to be forgotten.
    """
    if settings.RUN_WORKFLOWS_ASYNC:
        from .tasks import extract_documents_task
        task = extract_documents_task.delay(document_ids, schema_id, user_id)
        return {'async': True, 'task_id': task.id}
    return {'async': False, **run_extraction(document_ids, schema_id, user_id)}