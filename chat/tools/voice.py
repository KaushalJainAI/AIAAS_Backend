"""
Speech in both directions, behind one-door engines.

`transcribe_audio` reads a recording the user already has — a Meet audio file
in Drive or the VFS — and writes a `.md` transcript with speakers and
timestamps beside it. A meeting bot that joins live calls is out of v1.
`text_to_speech` speaks at most 5,000 characters per call into an audio file;
it spends money, so it is `irreversible` and `sensitive`, and its cost lands
in the ledger like every other non-token spend.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

from tools_config.overlay import alimit

logger = logging.getLogger(__name__)

#: Rough minutes per megabyte at telephone quality, for the ledger estimate.
#: An estimate, never a bill — the provider meters, we do not.
BYTES_PER_MINUTE = 1_000_000
#: Rupees per transcribed minute, estimated. The cap counts *something* rather
#: than nothing for a call that spent real money.
RUPEES_PER_TRANSCRIBE_MINUTE = 2
#: Rupees per thousand synthesised characters, estimated.
RUPEES_PER_TTS_KCHARS = 3


def _audio_bytes_sync(scope, path: str) -> tuple[str, bytes]:
    from inference import vfs
    from inference.vfs import VfsError

    parent_parts, leaf = vfs._split_leaf(scope, path)
    folder = vfs._folder_at(scope, parent_parts)
    doc = vfs._document_in(scope, folder, leaf)
    if doc is None:
        raise ValueError(f'No such file: {vfs.render(scope, parent_parts + [leaf])}.')
    try:
        return doc.name, bytes(vfs.read_binary(scope, path))
    except VfsError as exc:
        raise ValueError(str(exc)) from exc


def _markdown(segments: list[dict], fallback: str) -> str:
    lines = []
    for seg in segments:
        start = seg.get('start', seg.get('start_s', ''))
        speaker = seg.get('speaker', seg.get('who', ''))
        text = str(seg.get('text') or '').strip()
        if not text:
            continue
        head = f'[{start}]' if start else ''
        who = f'**{speaker}:**' if speaker else ''
        lines.append(f'{head} {who} {text}'.strip())
    return '\n'.join(lines) if lines else fallback


@tool({
    'type': 'function',
    'function': {
        'name': 'transcribe_audio',
        'description': (
            'Transcribe a recording the user already has — a Meet or Zoom audio '
            'file in their workspace. Writes a markdown transcript with '
            'speakers and timestamps next to the audio. Never joins a live '
            'call; there is no meeting bot.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'The audio file in your workspace.'},
                'language': {'type': 'string',
                             'description': 'BCP-47 hint, e.g. en or hi. Omit to auto-detect.'},
                'diarize': {'type': 'boolean',
                            'description': 'Separate speakers. Default false.'},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
}, requires='stt', effect='reversible')
async def transcribe_audio(args: Dict, context: Dict) -> str:
    from voice.stt import STTError, transcribe

    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'This agent has no file access, so it cannot read audio.'})
    path = str(args.get('path') or '').strip()
    if not path:
        return json.dumps({'error': 'Give the path of the audio file.'})
    try:
        name, data = await sync_to_async(_audio_bytes_sync)(scope, path)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Voice] Could not read audio')
        return json.dumps({'error': 'The audio file could not be read.'})
    try:
        result = await transcribe(
            data, name, language=str(args.get('language') or ''),
            diarize=bool(args.get('diarize')),
        )
    except STTError as exc:
        return json.dumps({'error': str(exc)})
    minutes = max(1, len(data) // BYTES_PER_MINUTE)
    transcript = _markdown(result.get('segments') or [], result.get('text') or '')
    if not transcript.strip():
        return json.dumps({'error': 'The transcription came back empty.'})
    from inference import vfs as _vfs

    md_path = path.rsplit('.', 1)[0] + '.md'
    try:
        saved = await sync_to_async(_vfs.write_file)(
            scope, md_path, f'# Transcript: {name}\n\n{transcript}\n')
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Voice] Could not save transcript')
        return json.dumps({'error': 'The transcript could not be saved.'})
    await _record_voice_cost(context, 'transcription', minutes, 'minute',
                             minutes * RUPEES_PER_TRANSCRIBE_MINUTE)
    return json.dumps({
        'transcript_path': saved['path'],
        'duration_s': result.get('duration_s') or 0,
        'language': result.get('language') or '',
        'rendered': f'Saved the transcript to {saved["path"]}.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'text_to_speech',
        'description': (
            'Speak text into an audio file saved in your workspace. At most '
            '5,000 characters per call, split longer passages yourself. Each '
            'call spends money, so make audio that was asked for.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': 'What to speak (max 5,000 characters).'},
                'voice': {'type': 'string', 'description': 'Voice id. Omit for the default.'},
                'path': {'type': 'string',
                         'description': 'Where to save it, without extension.'},
            },
            'required': ['text'],
            'additionalProperties': False,
        },
    },
}, requires='tts', sensitive=True, effect='irreversible')
async def text_to_speech(args: Dict, context: Dict) -> str:
    from voice.tts import TTSError, synthesize

    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'There is no file workspace here to save audio into.'})
    text = str(args.get('text') or '').strip()
    if not text:
        return json.dumps({'error': 'Give the text to speak.'})
    tts_cap = await alimit(context, 'text_to_speech', 'maxChars')
    if len(text) > tts_cap:
        return json.dumps({
            'error': f'That is {len(text):,} characters; one call speaks at most '
                     f'{tts_cap:,}. Split it into parts.',
        })
    try:
        data, ext = await synthesize(text, str(args.get('voice') or ''))
    except TTSError as exc:
        return json.dumps({'error': str(exc)})
    raw_path = str(args.get('path') or '').strip() or 'audio/speech'
    if '.' in raw_path.rsplit('/', 1)[-1]:
        raw_path = raw_path.rsplit('.', 1)[0]
    if not raw_path.startswith('/') and scope.write_prefix:
        # A relative path lands in the scope's own write folder, the only
        # place most scopes can write; an absolute one is taken as given.
        raw_path = '/' + '/'.join(scope.write_prefix) + '/' + raw_path
    from inference import vfs as _vfs

    try:
        out = await sync_to_async(_vfs.write_binary)(
            scope, f'{raw_path}.{ext}', bytes(data),
            text=f'Spoken audio: {text[:200]}',
        )
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Voice] Could not save audio')
        return json.dumps({'error': 'The audio could not be saved.'})
    kchars = max(1, len(text) // 1000)
    await _record_voice_cost(context, 'transcription', kchars, 'kchars',
                             kchars * RUPEES_PER_TTS_KCHARS, kind_source='tts')
    return json.dumps({
        'path': out['path'], 'chars': len(text),
        'rendered': f'Saved spoken audio to {out["path"]}.',
    })


async def _record_voice_cost(context: Dict, kind: str, units: int, unit: str,
                             amount_inr: int, kind_source: str = '') -> None:
    """One voice charge in the ledger, estimated. Best-effort."""
    from django.contrib.auth import get_user_model

    from logs.costs import record

    user_id = context.get('user_id')
    if not user_id or amount_inr <= 0:
        return
    try:
        user = await get_user_model().objects.filter(id=user_id).afirst()
        if user is None:
            return
        from asgiref.sync import sync_to_async as _sta

        await _sta(record)(
            user=user, kind='transcription', amount_inr=int(amount_inr),
            units=units, unit=unit, estimated=True,
            source=f"voice:{kind_source or kind}:{context.get('call_id') or ''}",
        )
    except Exception:  # noqa: BLE001
        logger.exception('[Voice] Failed to record voice cost')


VOICE_TOOLS = ('transcribe_audio', 'text_to_speech')
