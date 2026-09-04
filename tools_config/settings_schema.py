"""
What a user is actually allowed to change about a tool.

Two rules keep this honest.

**A knob exists here only if a tool reads it.** The schema is not documentation
of what we might one day support — every entry below is fetched at the one line
in the tool that used to hold a constant, so a knob that appears in the UI is a
knob that moves something. `test_config.py` fails if a declared key is never
read.

**Nothing here changes behaviour, only budget.** These are ceilings: how much
text comes back, how many results, how much stdout. A setting that changed what
a tool *does* would make the tool's own description a lie, and the description
is what the model plans against.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from workflow_backend.thresholds import (
    DEEP_RESEARCH_CHAR_LIMIT,
    IMAGE_SEARCH_MAX_RESULTS,
    READ_URL_CHAR_LIMIT,
    SEARCH_RESULT_LIMIT,
    VIDEO_SEARCH_MAX_RESULTS,
)


@dataclass(frozen=True, slots=True)
class Setting:
    """One integer knob. Integers only, deliberately.

    Every knob so far is a budget, and a budget has a floor, a ceiling and a
    default — which is exactly enough to render a control and to validate a
    write without a second schema language. The day a tool needs a string or an
    enum, that is a new field here and a new input in the UI, not a free-form
    JSON editor.
    """

    key: str
    label: str
    help: str
    default: int
    minimum: int
    maximum: int
    unit: str = ''

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(int(value), self.maximum))

    def as_dict(self) -> dict:
        return asdict(self)


#: tool name -> its knobs. Absent from this map means "on/off only", which is
#: true of most of the library and is not a gap.
TOOL_SETTINGS: dict[str, tuple[Setting, ...]] = {
    'web_search': (
        Setting('maxResults', 'Results per search',
                'How many results one search brings back.',
                SEARCH_RESULT_LIMIT, 3, 25),
    ),
    'image_search': (
        Setting('maxResults', 'Images per search',
                'How many images one search brings back.',
                IMAGE_SEARCH_MAX_RESULTS, 1, 12),
    ),
    'video_search': (
        Setting('maxResults', 'Videos per search',
                'How many videos one search brings back.',
                VIDEO_SEARCH_MAX_RESULTS, 1, 10),
    ),
    'read_url': (
        Setting('charLimit', 'Characters per page',
                'How much text is kept from one page. Longer pages are cut.',
                READ_URL_CHAR_LIMIT, 2_000, 60_000, 'characters'),
    ),
    'deep_research': (
        Setting('charLimit', 'Research text budget',
                'Total text kept across every page one research run reads.',
                DEEP_RESEARCH_CHAR_LIMIT, 10_000, 120_000, 'characters'),
        Setting('maxPages', 'Pages to read',
                'Default number of pages a run reads when it does not ask for one.',
                15, 5, 50, 'pages'),
    ),
    # 20k mirrors `chat.tools.sandbox.MAX_CODE_OUTPUT_CHARS`, which stays as
    # the floor under a failed overlay read.
    'execute_python': (
        Setting('outputLimit', 'Output kept',
                'How much printed output comes back from one run.',
                20_000, 1_000, 60_000, 'characters'),
    ),
}

#: Tools whose switch is not the user's to flip.
#:
#: Not a paternalism list — each of these is named by text we put in front of
#: the model. `tool_output.bound` tells it to "call read_tool_output with that
#: id" and the curator's notices name `recall_context`; turning either off
#: makes those instructions dishonest, and an escape hatch nobody can open is
#: worse than none. `get_current_time` reads a clock, has no egress and is what
#: `ALWAYS_AVAILABLE` means.
LOCKED_TOOLS = frozenset({
    'get_current_time',
    'read_tool_output',
    'recall_context',
})


def settings_for(tool_name: str) -> tuple[Setting, ...]:
    return TOOL_SETTINGS.get(tool_name, ())


def defaults_for(tool_name: str) -> dict[str, int]:
    return {s.key: s.default for s in settings_for(tool_name)}


def clean_config(tool_name: str, raw: dict) -> dict[str, int]:
    """Keep only declared keys, coerced to int and clamped to their range.

    Unknown keys are dropped rather than rejected: the caller is a UI that may
    be a deploy behind, and a stale field should not fail a save the user made
    for a different reason. A *value* out of range is clamped for the same
    reason — the control could not have offered it, so the number is noise, not
    an instruction. What is rejected loudly (in the serializer) is an unknown
    *tool*, because that is the mistake that writes a row nothing will read.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for setting in settings_for(tool_name):
        if setting.key not in raw:
            continue
        value = raw[setting.key]
        if isinstance(value, bool) or value is None:
            continue
        try:
            cleaned[setting.key] = setting.clamp(int(value))
        except (TypeError, ValueError):
            continue
    return cleaned
