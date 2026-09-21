"""
Text-to-speech behind `TTS_ENGINE`.

`none` (default) — no synthesis; `text_to_speech` is not offered. Synthesis
spends money per call, so the tool is `irreversible` + `sensitive` and capped
at 5,000 characters per call (refused, never truncated — a cut sentence read
aloud is worse than no audio).
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Characters per call. A cap on what one approval spends, not a budget.
TTS_MAX_CHARS = 5_000


class TTSError(RuntimeError):
    """Written for the model: what failed and what to do instead."""


def engine() -> str:
    return (getattr(settings, 'TTS_ENGINE', 'none') or 'none').strip().lower()


def tts_available() -> bool:
    return engine() not in ('', 'none')


async def synthesize(text: str, voice: str = '') -> tuple[bytes, str]:
    """Speak `text`. Returns (audio bytes, extension).

    Raises `TTSError` when no engine is configured or the provider fails.
    Tests patch this function.
    """
    if not tts_available():
        raise TTSError('No speech engine is configured on this platform.')
    if len(text) > TTS_MAX_CHARS:
        raise TTSError(
            f'That is {len(text):,} characters; one call speaks at most '
            f'{TTS_MAX_CHARS:,}. Split it into parts.'
        )
    from workflow_backend.httpclient import shared_client

    endpoint = getattr(settings, 'TTS_REMOTE_URL', '')
    token = getattr(settings, 'TTS_API_TOKEN', '')
    if not endpoint:
        raise TTSError('The speech engine has no endpoint configured.')
    try:
        resp = await shared_client().post(
            endpoint.rstrip('/'),
            headers={'Authorization': f'Bearer {token}'} if token else None,
            json={'text': text, 'voice': voice or 'default'},
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[TTS] remote call failed: %s', exc)
        raise TTSError('The speech service could not be reached.') from exc
    if resp.status_code >= 400:
        raise TTSError(f'The speech service refused the request ({resp.status_code}).')
    data = resp.content
    if not data:
        raise TTSError('The speech service returned no audio.')
    return bytes(data), 'mp3'
