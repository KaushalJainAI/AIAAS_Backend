# `browsing/`: a real browser the AI can drive

Behind the `browse_page` (read) and `browser_act` (click, type...) tools in
`chat/tools/browser.py`. **Off by default** (`BROWSER_ENGINE=none`). With no
engine the tools are simply not offered.

## Files

| File | What it does |
|---|---|
| `engine.py` | The one door to a remote browser. The AI describes steps as data; it never sends raw JavaScript |
| `models.py` | `BrowserSession`: a saved browser profile per user per website |
| `sessions.py` | Opening, reusing and closing sessions |
| `tasks.py` | Background jobs |

Which sites an agent may *act* on is set per agent (`browserDomains`). Empty
means read-only.

Management command: `sweep_browser_sessions`.
