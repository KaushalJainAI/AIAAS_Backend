# `tools_config/`: the Tools page

Tools themselves are code (`chat/tools/`). This app stores what **you** changed
about them: switched one off, or changed a limit (e.g. how many search results).

**No row means the default.** A fresh install has zero rows and every tool
behaves normally. "Reset to default" deletes the row.

This is separate from an agent's grants. A grant decides what *one agent* may
use. This decides what *exists* for your account at all.

## Files

| File | What it does |
|---|---|
| `models.py` | `ToolConfig`: one row per (user, tool) you changed |
| `settings_schema.py` | Which settings each tool has, with min, max and default |
| `overlay.py` | Fast read of your changes, cached 60 s. If the read fails, nothing is treated as off |
| `views.py`, `serializers.py`, `urls.py` | `/api/tools/`: the catalogue plus your changes |
| `signals.py` | Clears the cache when you change something |

The switches are enforced in `chat/tools/` (`disabled_tools_for`), not here.

Tests: `tools_config/tests/test_config.py`.
