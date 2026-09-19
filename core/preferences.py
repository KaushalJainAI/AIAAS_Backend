"""
The user's own settings, read once and rendered for the model.

Settings has offered timezone, language, a display name and a bio since the
profile existed, and the model path read none of them: chat's clock was the
server's zone, an agent told "use the environment" got a UTC timestamp, and the
language picker changed nothing anyone could see. This module is the one place
those columns are turned into something a prompt can carry, so chat and agent
runs cannot describe the same person two ways.

Three rules.

- **Session-stable, so it rides in the system prompt.** These change only when
  the user saves Settings, which clears the same bar `core.memory.for_prompt`
  does: the cached prefix survives, unlike the clock, which is why the *time*
  itself is rendered separately by `local_now` and goes wherever the caller's
  moving facts go.
- **Bounded.** A bio is free text the user typed, paid for on every turn of
  every session, so it is cut on a word boundary at `MAX_BIO_CHARS`.
- **Degrades to nothing.** A profile that cannot be read costs the answer its
  personalisation, never the answer — the rule every context gatherer here
  follows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: What the bio may cost in the system prompt, in characters.
MAX_BIO_CHARS = 600
MAX_NAME_CHARS = 80

#: Code -> the name the model is told. Codes are what is stored; names are
#: what the Settings page offered before this, so both are accepted on write.
LANGUAGES: dict[str, str] = {
    'en': 'English',
    'es': 'Spanish',
    'de': 'German',
    'fr': 'French',
    'hi': 'Hindi',
    'pt': 'Portuguese',
    'it': 'Italian',
    'ja': 'Japanese',
}
DEFAULT_LANGUAGE = 'en'


def zone_is_valid(zone: str) -> bool:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def language_code(value) -> str | None:
    """`'English'`, `'english'` or `'en'` -> `'en'`; None if unsupported."""
    text = str(value or '').strip()
    if not text:
        return DEFAULT_LANGUAGE
    lowered = text.lower()
    if lowered in LANGUAGES:
        return lowered
    for code, name in LANGUAGES.items():
        if name.lower() == lowered:
            return code
    return None


@dataclass(frozen=True)
class Preferences:
    timezone: str = 'UTC'
    language: str = DEFAULT_LANGUAGE
    display_name: str = ''
    bio: str = ''
    default_temperature: float = 0.7

    @property
    def language_name(self) -> str:
        return LANGUAGES.get(self.language, LANGUAGES[DEFAULT_LANGUAGE])


DEFAULTS = Preferences()


def _clip(text: str, limit: int) -> str:
    """Cut on a word boundary; half a sentence about someone reads as a fact."""
    text = ' '.join((text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0]
    return f'{cut}…'


def for_user(user_id: int | None) -> Preferences:
    """The profile's settings, validated, or the defaults."""
    if not user_id:
        return DEFAULTS
    try:
        from core.models import UserProfile

        row = (
            UserProfile.objects.filter(user_id=user_id)
            .values('timezone', 'language', 'display_name', 'bio',
                    'default_temperature')
            .first()
        )
    except Exception:  # noqa: BLE001
        logger.warning('[Preferences] Could not read profile for %s', user_id,
                       exc_info=True)
        return DEFAULTS
    if not row:
        return DEFAULTS

    zone = (row['timezone'] or '').strip()
    return Preferences(
        # A zone saved before validation existed may be junk; UTC is what the
        # clock always showed, so falling back to it changes nothing.
        timezone=zone if zone and zone_is_valid(zone) else 'UTC',
        language=language_code(row['language']) or DEFAULT_LANGUAGE,
        display_name=_clip(row['display_name'] or '', MAX_NAME_CHARS),
        bio=_clip(row['bio'] or '', MAX_BIO_CHARS),
        default_temperature=float(row['default_temperature'] or 0.7),
    )


def local_now(prefs: Preferences, now: datetime | None = None) -> str:
    """The time where the user is, naming the zone so the model can reason."""
    from django.utils import timezone

    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(prefs.timezone))
    return f"{local.strftime('%A, %B %d, %Y %I:%M %p')} ({prefs.timezone})"


def about_user(prefs: Preferences) -> str:
    """The block that goes in the system prompt, or '' when there is nothing.

    Written as information, not instruction, except the language line: the
    user picked a language in Settings, and a reply in another one is the thing
    that setting exists to prevent. It yields to the conversation, because a
    user who writes in Hindi has told us more than a dropdown did.
    """
    lines: list[str] = []
    if prefs.display_name:
        lines.append(f'- Name: {prefs.display_name}')
    lines.append(f'- Timezone: {prefs.timezone}')
    if prefs.bio:
        lines.append(f'- In their own words: {prefs.bio}')
    if prefs.language != DEFAULT_LANGUAGE:
        lines.append(
            f'- Preferred language: {prefs.language_name}. Reply in '
            f'{prefs.language_name} unless they write to you in another language.'
        )
    if not (prefs.display_name or prefs.bio
            or prefs.language != DEFAULT_LANGUAGE or prefs.timezone != 'UTC'):
        # Nothing but the defaults, which tell the model nothing it can use.
        # No block beats a block of boilerplate.
        return ''
    return '### ABOUT THE USER (from their settings) ###\n' + '\n'.join(lines)
