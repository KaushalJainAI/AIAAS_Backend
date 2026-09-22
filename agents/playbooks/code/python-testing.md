## Python testing

Conventions for Python work in this repo.

- `pytest -q` is the test command; `pytest <path>` runs a subset.
- `ruff check .` is the lint command where configured.
- Prefer small pure functions that are easy to test over clever integration.
- When adding tests, follow the per-app `tests/test_<topic>.py` layout and
  import the app's own modules absolutely (`from chat.models import ...`).
