"""
Content the platform refuses to produce, whoever asks and whatever the model says.

Every model provider runs its own safety filter, and we rely on them for the
broad middle (hate, harassment, self-harm). This module is for the narrow band
where relying on someone else is not good enough because **the platform itself
carries the legal duty**:

* **Sexual content involving minors.** IT Act s.67B and POCSO make no
  distinction between a photograph and an AI-generated image, and since
  20 Feb 2026 the amended IT Rules require intermediaries to deploy technical
  measures that *prevent* synthetic CSAM — not merely take it down.
* **Sexual deepfakes of real people** (non-consensual intimate imagery),
  named in the same amendment.
* **Instructions for mass-casualty weapons** (chemical, biological,
  radiological, nuclear) — NIST AI 600-1's CBRN category.

It is deliberately deterministic and small. A pattern can be phrased around,
so this is a floor under the providers' classifiers, not a replacement for
them; what it guarantees is that the obvious request never reaches a model we
pay for, and that the refusal is logged by category. Checks read the same
de-obfuscated views as the input sanitizer (look-alike letters, invisible
characters, leetspeak), so "n4ked" and "nаked" with a Cyrillic а are the same
word here.

Refusals never quote the matched text back and never explain how to get
around them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.safety.security import _LEET, _normalise

logger = logging.getLogger(__name__)

_I = re.IGNORECASE

_MINOR = (
    r'(child(ren)?|kids?|minors?|under\s*-?\s*age(d)?|preteens?|pre-teens?|teen(ager)?s?|'
    r'school\s*(girl|boy)s?|little\s+(girl|boy)s?|toddlers?|infants?|bab(y|ies)|'
    r'lolis?|shotas?|juvenile|young(er)?\s+(girl|boy)s?|'
    r'(?<!\d)(1[0-7]|[1-9])\s*(-|\s)?\s*(yo|y/o|yrs?\s*old|years?\s*old|year-old)\b)'
)
#: Sexual terms. Kept to words that mean sexual content, never words that can
#: also mean a colour or a clinical topic on their own ("nude lipstick", "sex
#: education" do not match, because "nude" alone is not in the chat list and
#: "sex" alone is not in either).
_SEXUAL_STRONG = (
    r'(porn\w*|nsfw|naked|nudes\b|nude\s+(photo|pic|image|selfie|video|shot)s?|nudity|'
    r'erotic\w*|lewd|hentai|explicit\s+(sex\w*|image|photo|content)|'
    r'sexual(ly)?\s+(explicit|image|photo|pose|act|content|scene)|sex\s+(scene|act|video|image|photo)|'
    r'topless|undress\w*|strip(ped|ping)?\s+(naked|nude)|genitals?|onlyfans|fetish)'
)
#: For image prompts the bar is lower: an image is the artefact the law names,
#: and "a teen in lingerie" is not a phrasing with an innocent reading.
_SEXUAL_IMAGE = _SEXUAL_STRONG[:-1] + r'|lingerie|bikini|sexy|seductive|sensual|provocative|suggestive|nude)'

_REAL_PERSON = (
    r'(celebrit(y|ies)|actress|actor|politician|influencer|youtuber|real\s+person|'
    r'my\s+(ex|girlfriend|boyfriend|wife|husband|classmate|colleague|coworker|neighbou?r|boss|teacher|friend)|'
    # A capitalised full name after "photo of" is a real person; "a photo of
    # a woman" is not. Case-sensitive inside an otherwise case-blind pattern.
    r'(photo|picture|image|pic)\s+of\s+(?-i:[A-Z][a-z]+\s+[A-Z][a-z]+)|'
    r'deep\s*-?\s*fake|face\s*-?\s*swap|'
    r'@\w{2,})'
)

_CBRN_ACT = (
    r'(synthesi[sz]e|make|produce|manufactur\w*|weaponi[sz]\w*|cultivat\w*|culture|'
    r'grow|extract|aeroli[sz]e|refine|enrich|build|assemble|recipe\s+for|steps?\s+to\s+(make|build))'
)
_CBRN_AGENT = (
    r'(sarin|soman|tabun|vx(\s+nerve)?|novichok|nerve\s+agents?|mustard\s+gas|'
    r'ricin|abrin|anthrax|botulinum(\s+toxin)?|smallpox|variola|ebola|plague\s+bacteri\w*|'
    r'bioweapons?|biological\s+weapons?|chemical\s+weapons?|'
    r'weapons?-?\s*grade\s+(uranium|plutonium)|highly\s+enriched\s+uranium|'
    r'(nuclear|dirty|radiological)\s+(bomb|device|weapon))'
)


@dataclass(frozen=True)
class Violation:
    category: str   # 'csam' | 'ncii' | 'cbrn'
    message: str    # what the person is told


_MESSAGES = {
    'csam': ("This can't be created here: sexual content involving minors is "
             "illegal, including AI-generated content."),
    'ncii': ("This can't be created here: sexual or intimate images of real "
             "people without their consent are not allowed."),
    'cbrn': ("This can't be helped with here: instructions for making "
             "chemical, biological, radiological or nuclear weapons are not "
             "provided."),
}


def _near(a: str, b: str, text: str, window: int = 160) -> bool:
    """Both patterns within one window of each other, in either order."""
    return bool(
        re.search(rf'{a}.{{0,{window}}}{b}', text, _I | re.DOTALL)
        or re.search(rf'{b}.{{0,{window}}}{a}', text, _I | re.DOTALL)
    )


def _views(text: str) -> list[str]:
    normal = _normalise(text or '')
    views = [text or '', normal, normal.translate(_LEET)]
    return list(dict.fromkeys(views))


def _hit(category: str, where: str) -> Violation:
    logger.warning('[ContentPolicy] refused %s in %s', category, where)
    return Violation(category, _MESSAGES[category])


def check_text(text: str, *, where: str = 'text') -> Violation | None:
    """Refuse a message, answer, outbound text or page that asks for or carries
    content in the three categories above. None means it passes."""
    if not text or not text.strip():
        return None
    for view in _views(text[:50_000]):
        if _near(_MINOR, _SEXUAL_STRONG, view, window=120):
            return _hit('csam', where)
        if _near(_REAL_PERSON, _SEXUAL_STRONG, view, window=120):
            return _hit('ncii', where)
        if _near(_CBRN_ACT, _CBRN_AGENT, view, window=80):
            return _hit('cbrn', where)
    return None


def check_image_prompt(prompt: str) -> Violation | None:
    """The stricter check for a prompt that will become an image."""
    if not prompt or not prompt.strip():
        return None
    for view in _views(prompt[:5_000]):
        if _near(_MINOR, _SEXUAL_IMAGE, view, window=200):
            return _hit('csam', 'image prompt')
        if _near(_REAL_PERSON, _SEXUAL_IMAGE, view, window=200):
            return _hit('ncii', 'image prompt')
    return check_text(prompt, where='image prompt')
