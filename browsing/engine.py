"""One door to a real browser, whichever engine is configured.

`read_url` fetches HTML and parses it; a page that builds itself in JavaScript
(most dashboards, many storefronts, every single-page app) reads as an empty
shell. This is the other half: a real Chromium that runs the page's scripts,
and — for `browser_act` — clicks and types.

**No browser runs on this box.** Chromium does not fit beside the app in 913 MB,
so the engine is *remote*: a hosted browser API (Browserless-compatible, its v2
`/function` endpoint) behind `BROWSER_ENGINE`, the same one-door shape as
`sandbox/engine.py`:

- ``none`` (default) — no browser. The tools are not offered at all, rather
  than offered and refusing (`browser_available`).
- ``remote`` — POSTs to `BROWSER_REMOTE_URL` with `BROWSER_API_TOKEN`.

There is deliberately no automatic fallback to `read_url`: an agent told it
browsed a page it only fetched would be reasoning from the empty shell.

**Steps are data, never code.** `act` sends one fixed script and a list of
`{action, selector, text}` steps as its *context*; the script interprets them.
Nothing a model writes is ever executed as JavaScript, so a step cannot do
anything the five verbs below cannot.

The remote browser runs on someone else's network, so it cannot reach this
box's internal services — but the URL guard still applies, because an agent
steered by a page into `http://169.254.169.254/` is a mistake worth refusing
wherever the browser happens to live.
"""
from __future__ import annotations

import base64
import binascii
import logging
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

#: The verbs a step may use. Closed, because the script has a branch per verb.
ACTIONS = ('click', 'type', 'select', 'press', 'wait')
MAX_STEPS = 15
TEXT_CHARS = 15_000
STEP_TEXT_CHARS = 500
SELECTOR_CHARS = 300


class BrowserError(RuntimeError):
    """Written for the model: what failed and what to do instead."""


def engine() -> str:
    return (getattr(settings, 'BROWSER_ENGINE', 'none') or 'none').strip().lower()


def browser_available() -> bool:
    return engine() == 'remote' and bool(getattr(settings, 'BROWSER_REMOTE_URL', ''))


def host_of(url: str) -> str:
    return (urlparse(url).hostname or '').lower().rstrip('.')


def check_steps(steps) -> list[dict]:
    """Validate and normalise `act` steps, or raise `BrowserError`."""
    if not isinstance(steps, list) or not steps:
        raise BrowserError('Give at least one step.')
    if len(steps) > MAX_STEPS:
        raise BrowserError(f'{len(steps)} steps is more than one call may take ({MAX_STEPS}). Split the task.')
    out = []
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise BrowserError(f'Step {i} must be an object with an action.')
        action = str(raw.get('action') or '').strip().lower()
        if action not in ACTIONS:
            raise BrowserError(f'Step {i}: action must be one of {", ".join(ACTIONS)}.')
        selector = str(raw.get('selector') or '').strip()
        text = str(raw.get('text') or '')
        if action in ('click', 'type', 'select', 'wait') and not selector:
            raise BrowserError(f'Step {i} ({action}) needs a CSS selector.')
        if action in ('type', 'select', 'press') and not text:
            raise BrowserError(f'Step {i} ({action}) needs `text`.')
        if len(selector) > SELECTOR_CHARS or len(text) > STEP_TEXT_CHARS:
            raise BrowserError(f'Step {i} is too long.')
        out.append({'action': action, 'selector': selector, 'text': text})
    return out


# The script the remote browser runs. Fixed: its inputs arrive as `context`.
_SCRIPT = r"""
export default async function ({ page, context }) {
  const out = { steps: [] };
  await page.setViewport({ width: 1280, height: 900 });
  await page.goto(context.url, { waitUntil: 'networkidle2', timeout: 30000 });
  for (const step of (context.steps || [])) {
    try {
      if (step.action === 'click') {
        await page.waitForSelector(step.selector, { timeout: 10000 });
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 }).catch(() => null),
          page.click(step.selector),
        ]);
      } else if (step.action === 'type') {
        await page.waitForSelector(step.selector, { timeout: 10000 });
        await page.type(step.selector, step.text);
      } else if (step.action === 'select') {
        await page.select(step.selector, step.text);
      } else if (step.action === 'press') {
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 }).catch(() => null),
          page.keyboard.press(step.text),
        ]);
      } else if (step.action === 'wait') {
        await page.waitForSelector(step.selector, { timeout: 15000 });
      }
      out.steps.push({ action: step.action, ok: true });
    } catch (e) {
      out.steps.push({ action: step.action, ok: false, error: String(e && e.message || e) });
      break;
    }
  }
  out.url = page.url();
  out.title = await page.title();
  out.text = await page.evaluate(() => document.body ? document.body.innerText : '');
  out.links = await page.evaluate(() => Array.from(document.querySelectorAll('a[href]'))
    .slice(0, 60).map(a => ({ text: (a.innerText || '').trim().slice(0, 80), href: a.href })));
  if (context.screenshot) {
    out.screenshot = await page.screenshot({ type: 'jpeg', quality: 60, encoding: 'base64' });
  }
  return { data: out, type: 'application/json' };
}
"""


async def run(url: str, *, steps: list[dict] | None = None, screenshot: bool = False) -> dict:
    """Open `url`, apply `steps` in order, and read the page back.

    Returns `{url, title, text, links, steps, screenshot: bytes | None}`.
    """
    from core.safety.net import validate_url_async

    if not browser_available():
        raise BrowserError('No browser is configured on this platform.')
    ok, reason = await validate_url_async(url)
    if not ok:
        raise BrowserError(reason)

    from workflow_backend.httpclient import shared_client

    base = settings.BROWSER_REMOTE_URL.rstrip('/')
    token = getattr(settings, 'BROWSER_API_TOKEN', '')
    try:
        resp = await shared_client().post(
            f'{base}/function',
            params={'token': token} if token else None,
            json={'code': _SCRIPT,
                  'context': {'url': url, 'steps': steps or [], 'screenshot': screenshot}},
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001 — network failures become a readable refusal
        logger.warning('[Browser] remote call failed: %s', exc)
        raise BrowserError('The browser service could not be reached. Try again shortly.') from exc
    if resp.status_code == 429:
        raise BrowserError('The browser service is busy (rate limited). Try again later.')
    if resp.status_code >= 400:
        raise BrowserError(f'The browser service refused the request ({resp.status_code}).')
    try:
        data = resp.json()
    except ValueError as exc:
        raise BrowserError('The browser service returned something unreadable.') from exc
    data = data.get('data', data) if isinstance(data, dict) else {}

    shot = None
    if screenshot and data.get('screenshot'):
        try:
            shot = base64.b64decode(data['screenshot'])
        except (binascii.Error, ValueError):
            shot = None
    text = str(data.get('text') or '')
    return {
        'url': str(data.get('url') or url),
        'title': str(data.get('title') or ''),
        'text': text[:TEXT_CHARS],
        'truncated': len(text) > TEXT_CHARS,
        'links': [link for link in (data.get('links') or []) if isinstance(link, dict)][:60],
        'steps': data.get('steps') or [],
        'screenshot': shot,
    }
