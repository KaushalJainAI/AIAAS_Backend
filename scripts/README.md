# `scripts/`: one-off developer scripts

Run these from the `Backend/` folder. None of them run automatically.

| Script | What it does | How to run |
|---|---|---|
| `seed_demo.py` | Fills a dev database with a demo user, agents, knowledge bases and chats | `python manage.py shell < scripts/seed_demo.py` |
| `seed_runs.py` | Adds example agent runs (run after `seed_demo.py`) | `python manage.py shell < scripts/seed_runs.py` |
| `seed_improve.py` | Adds more demo agents and extraction data | `python manage.py shell < scripts/seed_improve.py` |
| `seed_notion_dev.py` | Creates test pages in a Notion workspace for the Notion tools | `python scripts/seed_notion_dev.py --token ntn_xxx --parent <page>` |
| `check_imports.py` | Imports every module one by one to find import errors | `python scripts/check_imports.py [prefix]` |

Two seed files stay in `Backend/` on purpose, because other code loads them by
name:

- `populate_models.py`: the model catalogue. The production container runs it
  on every start (see `Dockerfile`), and `llm/catalog_refresh.py` imports it.
- `populate_credentials.py`: credential types, used by `instance/scripts/setup.*`.
