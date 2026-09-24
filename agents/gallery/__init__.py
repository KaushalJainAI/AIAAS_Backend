"""
The template gallery: ready-made agents you can install from Explore and then edit.

Where things are:

- One file per **pack** (`office.py`, `research.py`, `coding.py`, ...). Each
  holds its templates' prompts, a `TEMPLATES` dict, and `PACK`, the list the
  pack installs in order.
- `standalone.py`: templates in no pack. Each installs on its own.
- This file: joins them into the public `TEMPLATES` and `PACKS`, plus the
  helpers other code calls (`get`, `listing`, `pack_of`, `check_catalogue`).

To add a template, add an entry to the right file (and to its `PACK` list if
it belongs to a pack). `agents/tests/test_gallery.py` runs `check_catalogue`,
so a broken template fails the tests rather than the first person installing it.

The rules every template follows:

1. **A template is code, not a database row.** It becomes a normal `SubAgent`
   when installed, so a table would only duplicate columns the agent already
   has, and could drift from the serializer that checks it.
2. **`config` is the same flat `AgentConfig` the agent builder sends.**
   Installing passes it straight to `AgentSerializer`, so the install screen
   shows exactly the tools and guardrails the runtime will enforce.
3. **A template asks for things; it never names ids.** "Knowledge base 2"
   would mean someone else's row in another account. So `requirements` lists
   *what kind* of connection or corpus the agent needs, and the installer
   picks one of their own. Credentials never travel.
   Therefore `config` must never contain `connectors`, `knowledgeBases`,
   `skills`, `apiConnections` or `dataConnections`. `check_catalogue` enforces it.
"""
from __future__ import annotations

from typing import Any

from . import (
    coding,
    data,
    data_science,
    hiring,
    marketing,
    money,
    office,
    paperwork,
    research,
    standalone,
    support,
    team,
    web,
)

#: The requirement kinds an installer can satisfy, mapped to the `AgentConfig`
#: list each resolves into. Closed on purpose: a requirement of an unknown kind
#: would render on the install screen as a dropdown with nothing behind it.
REQUIREMENT_FIELDS: dict[str, str] = {
    'connector': 'connectors',
    'knowledge_base': 'knowledgeBases',
    'skill': 'skills',
    # A custom tool (the installer's own API/database connection, or a fresh
    # install of the author's frozen snapshot — see `datasources/sharing.py`).
    'api_tool': 'apiConnections',
    'data_tool': 'dataConnections',
}

#: Pack slug -> the module that defines it. The order here is the order packs
#: are listed in `PACKS`.
_PACK_MODULES = {
    'office': office,
    'research': research,
    'data': data,
    'web': web,
    'team': team,
    'paperwork': paperwork,
    'code': coding,
    'money': money,
    'marketing': marketing,
    'hiring': hiring,
    'support': support,
    'data-science': data_science,
}

#: Packs install together: `pack slug -> template slugs`. Every pack member
#: needs no connection or corpus, so a pack installs with one click. Anything
#: that needs one stays a single template with its own install screen.
PACKS: dict[str, list[str]] = {
    slug: list(module.PACK) for slug, module in _PACK_MODULES.items()
}

#: slug -> the gallery entry. `config` is a flat `AgentConfig`; anything it
#: omits takes the serializer's default, which is the cautious end of every
#: dial. Within each file, entries keep their order: Explore lists a pack's
#: members in this order.
TEMPLATES: dict[str, dict[str, Any]] = {}
for _module in (*_PACK_MODULES.values(), standalone):
    for _slug, _entry in _module.TEMPLATES.items():
        if _slug in TEMPLATES:
            raise ValueError(f'template {_slug!r} is defined in two files')
        TEMPLATES[_slug] = _entry
del _module, _slug, _entry


#: The keys a template's `config` may never carry — they point at rows in the
#: author's account, and `requirements` is how a template asks for them
#: portably instead.
_ID_BEARING_KEYS = frozenset(REQUIREMENT_FIELDS.values())


def pack_of(slug: str) -> str | None:
    """The one-click pack `slug` installs with, if any.

    Computed from `PACKS` rather than stored on the entry, so the catalogue
    cannot say one thing and the pack another. A template in no pack is not
    an error — it installs on its own — but one in two packs would render in
    two groups on Explore, which `check_catalogue` refuses.
    """
    for pack, slugs in PACKS.items():
        if slug in slugs:
            return pack
    return None


def get(slug: str) -> dict[str, Any] | None:
    """The catalogue entry for `slug`, or None."""
    entry = TEMPLATES.get(slug)
    if entry is None:
        return None
    return {'slug': slug, **entry}

def listing() -> list[dict[str, Any]]:
    """Every template, in catalogue order."""
    return [{'slug': slug, **entry} for slug, entry in TEMPLATES.items()]


def check_catalogue() -> list[str]:
    """Every way the catalogue is malformed, as messages. Empty means sound.

    Called by the tests rather than at import: a broken template should fail a
    test run, not stop the server booting. The rules it enforces are the ones
    that make a template portable at all — see the module docstring.
    """
    problems: list[str] = []
    for slug, entry in TEMPLATES.items():
        for field in ('name', 'tagline', 'description', 'config'):
            if not entry.get(field):
                problems.append(f'{slug}: missing {field}')
        config = entry.get('config') or {}
        leaked = _ID_BEARING_KEYS & set(config)
        if leaked:
            problems.append(
                f'{slug}: config carries {sorted(leaked)}, which are row ids '
                f'from whoever wrote it. Ask for them in `requirements`.'
            )
        if config.get('name') != entry.get('name'):
            problems.append(f'{slug}: config name does not match the card name')

        keys: set = set()
        for req in entry.get('requirements') or []:
            for field in ('key', 'type', 'label', 'why'):
                if not req.get(field):
                    problems.append(f'{slug}: requirement missing {field}')
            if req.get('type') not in REQUIREMENT_FIELDS:
                problems.append(f'{slug}: unknown requirement type {req.get("type")!r}')
            if req.get('key') in keys:
                problems.append(f'{slug}: duplicate requirement key {req.get("key")!r}')
            keys.add(req.get('key'))

        # A requirement nothing can use is a dropdown that installs a capability
        # the agent was not granted. Both directions matter: asking for a
        # mailbox without the `mcp` grant, and asking for a corpus without
        # `rag`.
        tools = config.get('tools') or {}
        kinds = {req.get('type') for req in entry.get('requirements') or []}
        if 'connector' in kinds and not tools.get('mcp'):
            problems.append(f'{slug}: asks for a connector but has no `mcp` grant')
        if 'knowledge_base' in kinds and not tools.get('rag'):
            problems.append(f'{slug}: asks for a knowledge base but has no `rag` grant')
        if config.get('schedule') and not config.get('allowUnattended'):
            problems.append(f'{slug}: has a schedule but is not cleared to run unattended')

    # A pack naming a slug that is not a template installs nothing for that
    # entry and silently shortens the pack; a template in two packs renders
    # in two groups on Explore. Both are caught here rather than by whoever
    # clicks Install. A template in *no* pack is fine — it installs on its own.
    claimed: dict[str, str] = {}
    for pack, slugs in PACKS.items():
        for slug in slugs:
            if slug not in TEMPLATES:
                problems.append(f'{pack}: no such template {slug!r}')
            elif slug in claimed:
                problems.append(
                    f'{slug}: in two packs ({claimed[slug]} and {pack})'
                )
            else:
                claimed[slug] = pack
    return problems
