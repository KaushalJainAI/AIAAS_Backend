# `skills/`: reusable instructions

A **skill** is a named block of instructions (for example "how to write a
release note") that you can give an agent or use in chat, instead of pasting
it every time. Skills can be shared and forked.

## Files

| File | What it does |
|---|---|
| `models.py` | `Skill` |
| `services.py` | Finding and loading skills |
| `views.py`, `serializers.py`, `urls.py` | `/api/skills/`: list, create, search, share, fork |

Management command: `seed_skills` (the built-in skills).
