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
#: `upload` validates but always refuses: the provider cannot see our files,
#: so a step naming one would pretend at something unrunnable — uploads stay
#: refused with a reason until a provider path exists.
ACTIONS = ('click', 'type', 'select', 'press', 'wait', 'scroll',
           'fill_secret', 'download', 'extract', 'upload')
#: Now workspace knobs (`browse_page.textChars` / `browser_act.maxSteps`);
#: the constants stay as the floor under a failed overlay read.
MAX_STEPS = 15
TEXT_CHARS = 15_000
STEP_TEXT_CHARS = 500
SELECTOR_CHARS = 300
#: Biggest download kept from one call. Bigger files come back as "it is there,
#: fetch it piece by piece" rather than as a turn-killing payload.
DOWNLOAD_BYTES = 10 * 1024 * 1024
TRACE_SHOTS = 10


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
        secret_ref = str(raw.get('secret_ref') or '').strip()
        if action in ('click', 'type', 'select', 'wait', 'scroll', 'download') and not selector:
            raise BrowserError(f'Step {i} ({action}) needs a CSS selector.')
        if action == 'fill_secret' and not selector:
            raise BrowserError(f'Step {i} (fill_secret) needs a CSS selector.')
        if action == 'fill_secret' and not (secret_ref or text):
            raise BrowserError(
                f'Step {i} (fill_secret) needs `secret_ref` (a vault login) '
                'or `text` already resolved from one.'
            )
        if action in ('type', 'select', 'press') and not text:
            raise BrowserError(f'Step {i} ({action}) needs `text`.')
        if action == 'extract' and text and text not in ('table', 'text', 'links'):
            raise BrowserError(f"Step {i} (extract): `text` must be 'table', 'text' or 'links'.")
        if action == 'upload':
            raise BrowserError(
                f'Step {i} (upload) is not available: the browser cannot see '
                'files on this machine. Point the page at a URL instead.'
            )
        if len(selector) > SELECTOR_CHARS or len(text) > STEP_TEXT_CHARS:
            raise BrowserError(f'Step {i} is too long.')
        step = {'action': action, 'selector': selector, 'text': text}
        if secret_ref:
            step['secret_ref'] = secret_ref
        out.append(step)
    return out


#: Steps that move toward committing something: paying, filing, confirming,
#: deleting. A call containing one makes the whole call `sensitive`, even
#: under `auto` — see `chat/turn/reviewer.py`.
_SUBMIT_WORDS = ('pay', 'submit', 'file', 'confirm', 'delete', 'buy',
                 'checkout', 'send', 'transfer', 'sign')


def looks_submitting(steps) -> bool:
    """Whether any step aims at a commit action, by selector or by wording.

    The word match applies to `click` / `press` steps only: for a `type` step
    the text is field *content*, and flagging on the words someone types would
    pause every message containing "send".
    """
    import re as _re

    for raw in steps or []:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get('action') or '').lower()
        selector = str(raw.get('selector') or '').lower()
        text = str(raw.get('text') or '').lower()
        if 'type=submit' in selector.replace(' ', '').replace('"', '').replace("'", ''):
            return True
        if action in ('click', 'press'):
            words = set(_re.findall(r'[a-z]+', selector + ' ' + text))
            if words & set(_SUBMIT_WORDS):
                return True
    return False


# The script the remote browser runs. Fixed: its inputs arrive as `context`.
_SCRIPT = r"""
export default async function ({ page, context }) {
  const out = { steps: [] };
  await page.setViewport({ width: 1280, height: 900 });
  // File responses (downloads) are captured, not navigated: anything that is
  // not text or HTML is buffered up to the cap and handed back as base64.
  page.on('response', async (resp) => {
    try {
      const ct = String((resp.headers() || {})['content-type'] || '');
      if (ct && !/text|html|json|javascript/.test(ct)) {
        const buf = await resp.buffer();
        if (buf && buf.length && buf.length <= (context.download_cap || 10485760)) {
          out.download = { url: resp.url(), mime: ct.split(';')[0].trim(),
                           bytes: buf.length, data: buf.toString('base64') };
        } else if (buf && buf.length) {
          out.download = { url: resp.url(), mime: ct.split(';')[0].trim(),
                           bytes: buf.length, too_large: true };
        }
      }
    } catch (e) { /* a missed download is a note, not a failure */ }
  });
  await page.goto(context.url, { waitUntil: 'networkidle2', timeout: 30000 });
  for (const step of (context.steps || [])) {
    try {
      if (step.action === 'click') {
        await page.waitForSelector(step.selector, { timeout: 10000 });
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 }).catch(() => null),
          page.click(step.selector),
        ]);
      } else if (step.action === 'type' || step.action === 'fill_secret') {
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
      } else if (step.action === 'scroll') {
        if (step.selector) {
          await page.waitForSelector(step.selector, { timeout: 10000 });
          await page.evaluate((sel) => {
            const el = document.querySelector(sel);
            if (el) el.scrollIntoView();
          }, step.selector);
        } else {
          await page.evaluate(() => window.scrollBy(0, 800));
        }
      } else if (step.action === 'download') {
        await page.waitForSelector(step.selector, { timeout: 10000 });
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 }).catch(() => null),
          page.click(step.selector),
        ]);
        await new Promise((r) => setTimeout(r, 3000));
      } else if (step.action === 'extract') {
        const mode = step.text || 'text';
        const grabbed = await page.evaluate((args) => {
          const root = args.selector ? document.querySelector(args.selector) : document.body;
          if (!root) return null;
          if (args.mode === 'links') {
            return Array.from(root.querySelectorAll('a[href]')).slice(0, 60)
              .map((a) => ({ text: (a.innerText || '').trim().slice(0, 80), href: a.href }));
          }
          if (args.mode === 'table') {
            const table = root.tagName === 'TABLE' ? root : root.querySelector('table');
            if (!table) return null;
            return Array.from(table.rows).slice(0, 100).map((row) =>
              Array.from(row.cells).map((c) => (c.innerText || '').trim().slice(0, 200)));
          }
          return (root.innerText || '').slice(0, 15000);
        }, { selector: step.selector, mode: mode });
        out.steps.push({ action: step.action, ok: true, extracted: grabbed });
        if (context.trace) {
          try {
            const shot = await page.screenshot({ type: 'jpeg', quality: 50, encoding: 'base64' });
            (out.trace || (out.trace = [])).push(shot);
          } catch (e) { /* tracing is best-effort */ }
        }
        continue;
      }
      out.steps.push({ action: step.action, ok: true });
      if (context.trace) {
        try {
          const shot = await page.screenshot({ type: 'jpeg', quality: 50, encoding: 'base64' });
          (out.trace || (out.trace = [])).push(shot);
          if (out.trace.length >= 10) { out.trace_truncated = true; }
        } catch (e) { /* tracing is best-effort */ }
      }
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


async def run(url: str, *, steps: list[dict] | None = None, screenshot: bool = False,
            trace: bool = False, session_id: str = '',
            text_chars: int = TEXT_CHARS) -> dict:
    """Open `url`, apply `steps` in order, and read the page back.

    Returns `{url, title, text, links, steps, screenshot: bytes | None,
    download: {url, mime, bytes, data_b64?} | None, trace: [bytes], live_url}`.
    `session_id` names a provider-side session to resume, best-effort: a
    provider without reconnect simply starts fresh, and the run still works.
    `text_chars` is the caller's workspace knob (`browse_page.textChars`)
    resolved by the tool layer; the module constant stays the default.
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
                  'context': {'url': url, 'steps': steps or [],
                              'screenshot': screenshot, 'trace': trace,
                              'download_cap': DOWNLOAD_BYTES,
                              'sessionId': session_id or None}},
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
    download = data.get('download') if isinstance(data.get('download'), dict) else None
    payload = None
    if download and download.get('data') and not download.get('too_large'):
        try:
            payload = base64.b64decode(download['data'])
        except (binascii.Error, ValueError):
            payload = None
        if payload is not None and len(payload) > DOWNLOAD_BYTES:
            payload = None
    trace_shots: list[bytes] = []
    if trace:
        for raw in (data.get('trace') or [])[:TRACE_SHOTS]:
            try:
                trace_shots.append(base64.b64decode(raw))
            except (binascii.Error, ValueError, TypeError):
                continue
    text = str(data.get('text') or '')
    extracted = [
        s.get('extracted') for s in (data.get('steps') or [])
        if isinstance(s, dict) and 'extracted' in s
    ]
    return {
        'url': str(data.get('url') or url),
        'title': str(data.get('title') or ''),
        'text': text[:text_chars],
        'truncated': len(text) > text_chars,
        'links': [link for link in (data.get('links') or []) if isinstance(link, dict)][:60],
        'steps': data.get('steps') or [],
        'screenshot': shot,
        'download': (
            {'url': str(download.get('url') or ''), 'mime': str(download.get('mime') or ''),
             'bytes': int(download.get('bytes') or 0),
             'too_large': bool(download.get('too_large')), 'data': payload}
            if download else None
        ),
        'extracted': extracted,
        'trace': trace_shots,
        'trace_truncated': bool(data.get('trace_truncated')),
        'live_url': str(data.get('liveUrl') or data.get('live_url') or ''),
    }
