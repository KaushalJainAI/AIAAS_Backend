"""
What the selected model will actually accept.

OpenRouter normalises media generation across providers, but *not* the dial set:
each model advertises its own, per parameter, at `/images/models` and
`/videos/models`. Two failure modes follow, and they look nothing alike:

- A value outside an advertised enum is a **hard 400**, measured against the
  live API::

      resolution "512": not supported. Accepted: 2K, 4K

  which is what the studio produced for every model whose tiers were not the
  invented `["1K", "2K"]` the catalog used to fall back to.
- A parameter the model does not advertise at all is **silently ignored**. No
  error, no effect — the worst outcome of the two, because the control looks
  like it worked and the user pays for a result that ignored it.

So a dial is offered only where the model claims it, and a request carrying one
it does not claim is refused *here*, before a billed call. The catalogue is the
single source for both halves: `capabilities_for` describes what the panel may
render, and this validates against the same descriptor. A rule written twice is
a rule that disagrees with itself the first time OpenRouter ships a model.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Fields carrying a url the user supplied. Only these two shapes are accepted:
#: a public http(s) url, or an inline data URI. Nothing here fetches either —
#: they are passed to OpenRouter as given — but a `file://` or `gopher://`
#: string has no business being forwarded anywhere.
_URL_SCHEMES = ('http://', 'https://', 'data:')

#: An `output_compression` only exists for lossy formats. Sending it with png
#: is accepted and ignored, which is the silent kind of wrong.
_COMPRESSIBLE_FORMATS = {'jpeg', 'jpg', 'webp'}

MAX_REFERENCE_URL_CHARS = 2_000_000


class DialError(ValueError):
    """A dial this model does not take, or a value it does not accept."""


def _reject(field: str, message: str) -> None:
    raise DialError({field: message})


def _check_enum(field: str, value: Any, allowed: List[Any], *, label: str) -> None:
    """A dial is offered only where advertised; refuse both ways it can be wrong."""
    if value in (None, ''):
        return
    if not allowed:
        _reject(field, f"{label} does not accept a {field}.")
    if value not in allowed:
        _reject(
            field,
            f"{label} does not accept {field} '{value}'. "
            f"Accepted: {', '.join(str(a) for a in allowed)}.",
        )


def _check_urls(field: str, urls: Any, limit: int, *, label: str) -> None:
    if not urls:
        return
    if limit <= 0:
        _reject(field, f"{label} does not accept reference images.")
    if len(urls) > limit:
        _reject(field, f"{label} accepts at most {limit} reference image(s).")
    for url in urls:
        if not isinstance(url, str) or not url.startswith(_URL_SCHEMES):
            _reject(field, 'Each reference must be an http(s) url or a data: URI.')
        if len(url) > MAX_REFERENCE_URL_CHARS:
            _reject(field, 'A reference image is too large to send inline.')


def validate_dials(kind: str, model: Optional[Dict[str, Any]], data: Dict[str, Any]) -> None:
    """Raise `DialError` if `data` asks this model for something it cannot do.

    `model` is a descriptor from `services.catalog`; None means the catalogue
    is unavailable (no credential, OpenRouter unreachable), and then nothing is
    checked — an outage must not turn into a validation error about the user's
    choices.
    """
    if not model:
        return
    label = model.get('name') or model.get('id') or 'This model'

    if kind == 'image':
        _check_enum('resolution', data.get('resolution'), model.get('resolutions') or [], label=label)
        _check_enum('aspect_ratio', data.get('aspect_ratio'), model.get('aspect_ratios') or [], label=label)
        _check_enum('quality', data.get('quality'), model.get('qualities') or [], label=label)
        _check_enum('output_format', data.get('output_format'), model.get('output_formats') or [], label=label)
        _check_enum('background', data.get('background'), model.get('backgrounds') or [], label=label)
        _check_urls('reference_urls', data.get('reference_urls'),
                    int(model.get('max_references') or 0), label=label)

        compression = data.get('output_compression')
        if compression is not None:
            window = model.get('output_compression')
            if not window:
                _reject('output_compression', f'{label} does not accept output compression.')
            if not window['min'] <= compression <= window['max']:
                _reject('output_compression',
                        f"{label} accepts output_compression between "
                        f"{window['min']} and {window['max']}.")
            fmt = (data.get('output_format') or '').lower()
            if fmt and fmt not in _COMPRESSIBLE_FORMATS:
                _reject('output_compression',
                        f"Compression applies to {'/'.join(sorted(_COMPRESSIBLE_FORMATS))} "
                        f"only, not {fmt}.")

        batch = data.get('batch_size')
        if batch is not None:
            window = model.get('batch')
            if not window:
                _reject('batch_size', f'{label} returns one image per request.')
            if not window['min'] <= batch <= window['max']:
                _reject('batch_size',
                        f"{label} accepts between {window['min']} and "
                        f"{window['max']} images per request.")

    elif kind == 'video':
        _check_enum('resolution', data.get('resolution'), model.get('resolutions') or [], label=label)
        _check_enum('aspect_ratio', data.get('aspect_ratio'), model.get('aspect_ratios') or [], label=label)
        _check_enum('size', data.get('size'), model.get('sizes') or [], label=label)
        _check_urls('reference_urls', data.get('reference_urls'), 4, label=label)

        duration = data.get('duration')
        allowed = model.get('durations') or []
        if duration not in (None, ''):
            # Stored as a string ('8'), advertised as numbers ([4, 6, 8]).
            try:
                seconds = int(float(str(duration)))
            except (TypeError, ValueError):
                _reject('duration', 'Duration must be a number of seconds.')
            _check_enum('duration', seconds, allowed, label=label)

        frames = data.get('frame_images') or []
        slots = model.get('frame_slots') or []
        if frames:
            if not slots:
                _reject('frame_images', f'{label} cannot be given a start or end frame.')
            seen = set()
            for frame in frames:
                if not isinstance(frame, dict):
                    _reject('frame_images', 'Each frame needs a url and a frame_type.')
                slot = frame.get('frame_type')
                if slot not in slots:
                    _reject('frame_images',
                            f"{label} accepts frame_type: {', '.join(slots)}.")
                if slot in seen:
                    _reject('frame_images', f'Only one {slot} may be given.')
                seen.add(slot)
                _check_urls('frame_images', [frame.get('url')], 1, label=label)

    elif kind == 'audio':
        voices = model.get('voices') or []
        voice = data.get('voice')
        # An empty voice list means the model takes a free-form provider id —
        # unlike every other dial here, where empty means "not accepted".
        # Guessing wrong in this one place would either block every MiniMax
        # voice or let an OpenAI name through to a provider that rejects it.
        if voice and voices and voice not in voices:
            _reject('voice', f"{label} does not have a voice '{voice}'. "
                             f"Accepted: {', '.join(voices)}.")
        _check_enum('response_format', data.get('response_format'),
                    model.get('response_formats') or [], label=label)

        speed = data.get('speed')
        if speed is not None:
            window = model.get('speed_range')
            if not window:
                _reject('speed', f'{label} does not accept a speed.')
            if not window['min'] <= speed <= window['max']:
                _reject('speed',
                        f"{label} accepts a speed between {window['min']} and "
                        f"{window['max']}.")

        if data.get('instructions') and not model.get('supports_instructions'):
            _reject('instructions',
                    f'{label} does not take tone instructions — put the '
                    f'direction in the text instead.')


# ---------------------------------------------------------------------------
# The same rule, for a caller that must not refuse
# ---------------------------------------------------------------------------

#: Every dial, and where its permitted values live on a model descriptor. The
#: `coerce` is what bridges storage to catalogue: durations arrive as `'8'` and
#: are advertised as `8`, and a comparison that does not bridge that refuses
#: every valid length.
_ENUM_DIALS = {
    'image': (
        ('resolution', 'resolutions', str),
        ('aspect_ratio', 'aspect_ratios', str),
        ('quality', 'qualities', str),
        ('output_format', 'output_formats', str),
        ('background', 'backgrounds', str),
    ),
    'video': (
        ('resolution', 'resolutions', str),
        ('aspect_ratio', 'aspect_ratios', str),
        ('size', 'sizes', str),
        ('duration', 'durations', int),
    ),
    'audio': (
        ('voice', 'voices', str),
        ('response_format', 'response_formats', str),
    ),
}

#: Dials that belong to exactly one modality. Anything else is dropped for the
#: others, because a router that has just been told "make it a video" will
#: cheerfully carry the voice it picked a turn earlier.
_MODALITY_DIALS = {
    'image': {'resolution', 'aspect_ratio', 'quality', 'output_format', 'background',
              'output_compression', 'batch_size', 'reference_urls', 'negative_prompt', 'seed'},
    'video': {'resolution', 'aspect_ratio', 'size', 'duration', 'generate_audio',
              'frame_images', 'reference_urls', 'negative_prompt', 'seed'},
    'audio': {'voice', 'speed', 'instructions', 'response_format'},
}


def constrain(kind: str, model: Optional[Dict[str, Any]], params: Dict[str, Any]) -> Dict[str, Any]:
    """`validate_dials`, for the caller that must not refuse the turn.

    The form path raises: the user set a dial explicitly and deserves to be told
    it will not apply. The conversational path cannot — a router that guessed
    `quality: high` for a model without a quality switch has not made the user's
    request impossible, it has added something to drop. Same table, same
    descriptors, two policies; the alternative is a second copy of the rule that
    goes stale on the first model OpenRouter ships.

    Returns a new dict containing only dials this model will honour.
    """
    out = {k: v for k, v in params.items()
           if k in _MODALITY_DIALS.get(kind, set()) and v not in (None, '')}
    if not model:
        return out

    for field, source, coerce in _ENUM_DIALS.get(kind, ()):
        if field not in out:
            continue
        allowed = model.get(source) or []
        # An empty voice list means free-form provider ids, not "no voices" —
        # the one dial where absent does not mean forbidden.
        if not allowed:
            if not (kind == 'audio' and field == 'voice'):
                out.pop(field)
            continue
        try:
            value = coerce(out[field])
            permitted = [coerce(a) for a in allowed]
        except (TypeError, ValueError):
            out.pop(field)
            continue
        if value in permitted:
            out[field] = value
        else:
            out.pop(field)

    for field, window_key in (('output_compression', 'output_compression'),
                              ('batch_size', 'batch'),
                              ('speed', 'speed_range')):
        if field not in out:
            continue
        window = model.get(window_key)
        if not window:
            out.pop(field)
            continue
        try:
            number = float(out[field])
        except (TypeError, ValueError):
            out.pop(field)
            continue
        # Clamped rather than dropped: a router asking for eight images from a
        # model that returns four wants as many as it can have, and the
        # user approves the number on the HITL card either way.
        out[field] = type(out[field])(min(max(number, window['min']), window['max']))

    if kind == 'image' and 'output_compression' in out:
        fmt = str(out.get('output_format') or '').lower()
        if fmt and fmt not in _COMPRESSIBLE_FORMATS:
            out.pop('output_compression')

    if 'reference_urls' in out:
        limit = int(model.get('max_references') or 0) if kind == 'image' else 4
        urls = [u for u in out['reference_urls']
                if isinstance(u, str) and u.startswith(_URL_SCHEMES)][:limit]
        if urls:
            out['reference_urls'] = urls
        else:
            out.pop('reference_urls')

    if 'frame_images' in out:
        slots = model.get('frame_slots') or []
        seen = set()
        frames = []
        for frame in out['frame_images']:
            if not isinstance(frame, dict):
                continue
            slot = frame.get('frame_type')
            url = frame.get('url')
            if (slot in slots and slot not in seen and isinstance(url, str)
                    and url.startswith(_URL_SCHEMES)):
                frames.append({'url': url, 'frame_type': slot})
                seen.add(slot)
        if frames:
            out['frame_images'] = frames
        else:
            out.pop('frame_images')

    if 'instructions' in out and not model.get('supports_instructions'):
        out.pop('instructions')

    return out
