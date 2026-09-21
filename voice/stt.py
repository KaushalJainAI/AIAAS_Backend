"""
Speech-to-text behind `STT_ENGINE`.

`none` (default) — no transcription; `transcribe_audio` is not offered.
A provider value (decision D5: Sarvam for Hindi/Indian-language accuracy,
otherwise Deepgram) posts audio and returns segments. Nothing here joins a
live call: meeting recordings are read from Drive or the VFS, never captured.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    """Written for the model: what failed and what to do instead."""


def engine() -> str:
    return (getattr(settings, 'STT_ENGINE', 'none') or 'none').strip().lower()


def stt_available() -> bool:
    return engine() not in ('', 'none')


async def transcribe(data: bytes, filename: str, *, language: str = '',
                     diarize: bool = False) -> dict:
    """Transcribe audio bytes. Returns `{text, segments, duration_s, language}`.

    Raises `STTError` when no engine is configured or the provider fails.
    Tests patch this function — the network is not what they prove.
    """
    if not stt_available():
        raise STTError('No transcription engine is configured on this platform.')
    from workflow_backend.httpclient import shared_client

    endpoint = getattr(settings, 'STT_REMOTE_URL', '')
    token = getattr(settings, 'STT_API_TOKEN', '')
    if not endpoint:
        raise STTError('The transcription engine has no endpoint configured.')
    try:
        resp = await shared_client().post(
            endpoint.rstrip('/'),
            headers={'Authorization': f'Bearer {token}'} if token else None,
            files={'audio': (filename, data)},
            data={'language': language, 'diarize': 'true' if diarize else 'false'},
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[STT] remote call failed: %s', exc)
        raise STTError('The transcription service could not be reached.') from exc
    if resp.status_code >= 400:
        raise STTError(f'The transcription service refused the request ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise STTError('The transcription service returned something unreadable.') from exc
    segments = [s for s in (payload.get('segments') or []) if isinstance(s, dict)][:500]
    return {
        'text': str(payload.get('text') or ''),
        'segments': segments,
        'duration_s': float(payload.get('duration_s') or 0),
        'language': str(payload.get('language') or language or ''),
    }
