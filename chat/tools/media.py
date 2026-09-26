"""
Generated media as a tool: `generate_image`.

The Imagine page could always make images, and nothing else could reach it —
so a deck asked for "with a cover image" got a text placeholder, because the
agent building it had no hands for the picture. This is the same generation
path (`imagine/services/openrouter.py`, the user's own OpenRouter credential,
the same model catalogue and the same dial validation), saved into the user's
file tree where `render_deck` and `render_document` embed it by path.

Three decisions carry it.

**It costs money, so it is `irreversible` and its cost is recorded.** An image
cannot be un-bought: the autonomy ladder's `auto` level gates it on an
unattended run, and the result carries `cost_usd` so the run's cost rollup
(`AgentStep.cost_usd` → `ExecutionLog.cost_usd`) counts it against the spend
cap. When OpenRouter does not report a price, `IMAGE_COST_ESTIMATE_USD` stands
in — an image that counts as free would make the cap stop applying to exactly
the calls that spend most per call.

**It is `sensitive`, so the user approves each image.** It was not, on the
reasoning that the request in this turn *was* the consent — but a generation is
billed per call and a model that decides to make four variations has spent four
times what was asked for. An approval card naming the model is the cheapest
place to catch that, and "always allow for this session" is one click for
someone who really is iterating on a picture.

**Unsupported dials are dropped, not refused** — `imagine.validation.constrain`,
the conversational policy the Imagine agent already uses. An aspect ratio the
chosen model does not offer has not made the request impossible.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from decimal import Decimal
from typing import Any, Dict

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES

from .registry import tool

logger = logging.getLogger(__name__)

#: Charged when OpenRouter returns no price for a generation. Roughly the
#: middle of the recommended models' per-image prices; a blast-radius figure
#: for the spend cap, not billing.
IMAGE_COST_ESTIMATE_USD = Decimal('0.04')

ASPECT_RATIOS = ('1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3')
PROMPT_CHARS = 2000

_DATA_URL = re.compile(r'^data:(?P<mime>image/[\w.+-]+);base64,(?P<b64>.+)$', re.DOTALL)
_EXT = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/jpg': 'jpg',
        'image/webp': 'webp', 'image/gif': 'gif'}


class MediaError(ValueError):
    """Written for the model: what went wrong and what to do instead."""


def _image_bytes(url: str) -> tuple[bytes, str]:
    """The image a generation returned, as bytes and a file extension."""
    match = _DATA_URL.match(url or '')
    if match:
        try:
            data = base64.b64decode(match.group('b64'), validate=False)
        except (binascii.Error, ValueError) as exc:
            raise MediaError('The image came back unreadable. Try again.') from exc
        return data, _EXT.get(match.group('mime').lower(), 'png')
    if not (url or '').startswith('https://'):
        raise MediaError('The image service returned no usable image.')

    from core.safety.net import FetchTooLarge, UnsafeURLError, fetch_file

    # A provider-hosted result, byte-capped, with every redirect hop checked:
    # the URL is the provider's to choose, so it is not a reason to let it
    # point this server at its own network (N8).
    try:
        data, mime = fetch_file(url, timeout=60, max_bytes=AGENT_FILE_BINARY_BYTES)
    except FetchTooLarge as exc:
        raise MediaError('The generated image is too large to keep.') from exc
    except UnsafeURLError as exc:
        raise MediaError('The image service returned an unusable link.') from exc
    mime = mime or 'image/png'
    if not mime.startswith('image/'):
        raise MediaError('The image service returned something that is not an image.')
    return data, _EXT.get(mime, 'png')


def _slug(prompt: str) -> str:
    words = re.findall(r'[A-Za-z0-9]+', prompt.lower())[:6]
    return '-'.join(words) or 'image'


def _record_cost(user, context: Dict[str, Any], out: dict) -> None:
    """File this generation in the cost ledger (`logs.CostEntry`).

    The `cost_usd` in the result above stays — it is what the chat turn prices
    and what `_roll_up_cost` sums, and removing it would break both. The ledger
    row is the same charge in rupees for the spend cap, and `aggregate_rupees`
    excludes the `image` kind for exactly that reason, so one image is counted
    once. Best-effort: a failed ledger write must not fail the image.
    """
    try:
        from decimal import Decimal as _Decimal

        from agents.spend import rupees_for_usd
        from logs.costs import record

        cost = _Decimal(str(out.get('cost_usd') or '0'))
        if cost <= 0:
            return
        execution = None
        session_id = context.get('session_id')
        if session_id:
            from logs.models import ExecutionLog

            # An agent run uses its thread id as its session id, and the log
            # carries an indexed copy of it — so this joins without threading
            # an execution id through `TurnContext` and every tool context.
            execution = ExecutionLog.objects.filter(thread_id=session_id).first()
        record(
            user=user,
            kind='image',
            amount_inr=rupees_for_usd(cost),
            execution=execution,
            units=1,
            unit='image',
            estimated=(out.get('cost_source') != 'billed'),
            source=f"generate_image:{context.get('call_id') or ''}",
            dedupe_key=f"generate_image:{context.get('call_id') or ''}" if execution else '',
        )
    except Exception:  # noqa: BLE001
        logger.exception('[Media] Failed to record image cost')


def _prepare(scope, user, args: Dict[str, Any], prompt_cap: int = PROMPT_CHARS):
    """On-thread half of a generation: validate, resolve service and model.

    Everything here either is pure or touches the ORM (the credential lookup
    in `for_user`, the capabilities read), so it stays on the run's thread.
    Returns what the HTTP half needs plus the path stem — the extension is
    only known once the bytes are fetched. Raises `MediaError`, as before.
    """
    from imagine.services.capabilities import capabilities_for
    from imagine.services.catalog import default_model_id, find_model
    from imagine.services.openrouter import (
        MissingOpenRouterCredentialError, OpenRouterService,
    )
    from imagine.validation import constrain

    prompt = str(args.get('prompt') or '').strip()
    if not prompt:
        raise MediaError('Describe the image in `prompt`.')
    if len(prompt) > prompt_cap:
        raise MediaError(f'The prompt is {len(prompt)} characters; keep it under {prompt_cap}.')
    # Before any money is spent or any provider sees it: some images the
    # platform itself must not make (`core/safety/content_policy.py`).
    from core.safety.content_policy import check_image_prompt

    refused = check_image_prompt(prompt)
    if refused is not None:
        raise MediaError(f'{refused.message} Do not retry with different wording.')

    try:
        service = OpenRouterService.for_user(user)
    except MissingOpenRouterCredentialError as exc:
        raise MediaError(
            f'{exc} Image generation uses your own OpenRouter key — add one on the '
            f'Credentials page. Tell the user rather than retrying.'
        ) from exc

    caps = capabilities_for(user)
    model_id = str(args.get('model') or '').strip() or default_model_id('image', caps.get('image') or [])
    if not model_id:
        raise MediaError('No image model is available on this account right now.')
    model = find_model(caps, 'image', model_id)
    if caps.get('image') and model is None:
        raise MediaError(f'{model_id!r} is not an image model on OpenRouter. Omit `model` to use the default.')

    config = constrain('image', model, {'aspect_ratio': args.get('aspect_ratio')})
    raw_path = str(args.get('path') or '').strip()
    if raw_path and '.' in raw_path.rsplit('/', 1)[-1]:
        raw_path = raw_path.rsplit('.', 1)[0]
    return service, model_id, config, prompt, raw_path


async def _generate(scope, user, args: Dict[str, Any], prompt_cap: int = PROMPT_CHARS) -> dict:
    """Three stages, two threads: resolve on the run's thread (ORM), fetch
    bytes off it (HTTP/CPU), save back on it (ORM). A generation that sat on
    the run's only thread blocked that run's ORM calls for the whole call."""
    from inference.vfs import write_binary

    service, model_id, config, prompt, raw_path = await sync_to_async(_prepare)(
        scope, user, args, prompt_cap)
    result = await sync_to_async(service.generate_image, thread_sensitive=False)(
        prompt, model_id, config)
    if 'error' in result:
        raise MediaError(f'The image could not be generated: {result["error"]}')

    data, ext = await sync_to_async(_image_bytes, thread_sensitive=False)(
        result.get('url') or '')
    # Marked as synthetic on the picture and in the file (IT Rules 2026, EU AI
    # Act Art. 50) before it is saved anywhere a person can share it from.
    from core.safety.labels import label_image

    data = await sync_to_async(label_image, thread_sensitive=False)(data, ext, model=model_id)
    path = f'{raw_path or "images/" + _slug(prompt)}.{ext}'
    if not path.startswith('/') and scope.write_prefix:
        # A relative path lands in the scope's own write folder, the only
        # place most scopes can write; an absolute one is taken as given.
        path = '/' + '/'.join(scope.write_prefix) + '/' + path

    reported = result.get('cost')
    cost = Decimal(str(reported)) if reported is not None else IMAGE_COST_ESTIMATE_USD
    out = await sync_to_async(write_binary)(
        scope, path, data, text=f'Generated image: {prompt}',
        spec={'kind': 'image', 'prompt': prompt, 'model': model_id,
              'ai_generated': True},
    )
    out.update({
        'model': model_id,
        'applied': config,
        'cost_usd': str(cost),
        'cost_source': 'billed' if reported is not None else 'estimated',
        'rendered': (
            f'Saved the image to {out["path"]}. The user sees it as a file card. '
            f'To put it on a slide, pass this path as a slide\'s `image`.'
        ),
    })
    return out


@tool({
    'type': 'function',
    'function': {
        'name': 'generate_image',
        'description': (
            'Generate an image from a description and save it to the user\'s files. '
            'Each image is billed to the user\'s OpenRouter account, so make one '
            'when it is wanted — a cover for a deck, an illustration the user asked '
            'for — not speculatively, and do not make several variations unasked; '
            'the user approves each one. '
            'Returns the saved path, which render_deck and render_document accept '
            'as an image. Describe subject, style and composition concretely; '
            'never ask for text inside the image, which models render badly.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string', 'description': 'What the image shows, concretely.'},
                'aspect_ratio': {'type': 'string', 'enum': list(ASPECT_RATIOS),
                                 'description': '16:9 for a slide or banner, 1:1 by default.'},
                'path': {'type': 'string',
                         'description': 'Where to save it, without extension. Default: images/<words from the prompt>.'},
                'model': {'type': 'string', 'description': 'An OpenRouter image model id. Omit for the default.'},
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    },
}, requires='files', sensitive=True, effect='irreversible')
async def generate_image(args: Dict, context: Dict) -> str:
    from django.contrib.auth import get_user_model

    from inference.vfs import VfsError

    scope = context.get('file_scope')
    if scope is None:
        return json.dumps({'error': 'There is no file workspace here to save an image into.'})
    user = await get_user_model().objects.filter(id=context.get('user_id')).afirst()
    if user is None:
        return json.dumps({'error': 'No user context.'})
    try:
        from tools_config.overlay import alimit

        prompt_cap = await alimit(context, "generate_image", "promptChars")
        out = await _generate(scope, user, args, prompt_cap)
    except (MediaError, VfsError) as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Media] generate_image failed')
        return json.dumps({'error': 'The image could not be generated. Try a simpler prompt.'})
    await sync_to_async(_record_cost)(user, context, out)
    return json.dumps(out, default=str)


MEDIA_TOOLS = ('generate_image',)
