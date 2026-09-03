"""
Seed a Notion *dev* workspace with fixtures worth testing a connector against.

Unlike the other seed_*.py scripts in this directory, this one does not touch
the Django ORM at all -- it talks to the Notion REST API. Run it standalone:

    python seed_notion_dev.py --token ntn_xxx --parent <page-id-or-url>
    python seed_notion_dev.py --token ntn_xxx --reset

The token is an internal-integration secret from notion.so/my-integrations
(or an OAuth access token). Either way the integration must be *shared with*
the parent page first -- Notion grants access per page, and a token with no
shared pages can create nothing. Use the sidebar switcher to make a separate
"AIAAS Dev" workspace and point --parent at a page in it; never seed a
workspace holding real content, because --reset archives what it created.

What it builds, and why each piece is here rather than three plain pages:

  Teams (database)      relation + rollup target
  Tasks (database)      every property type the API will let us create, plus
                        --rows deliberately > 100, so a caller that forgets
                        start_cursor truncates silently and we notice
  Kitchen Sink (page)   one of every block type, plus >100 blocks so the block
                        list paginates too -- the same bug, a different endpoint
  Nested pages          a three-deep tree, because a child page is a *block*
                        and a flat block read never descends into it
  Edge-case rows        empty title, all-null optionals, a >2000-char title,
                        emoji/RTL/newlines, a date *range*, a timezone-bearing
                        datetime -- each breaks a different naive reader

Created ids are recorded in .notion_dev_seed.json beside this file so --reset
can archive them. Archiving the root archives its descendants; the manifest
stays complete anyway, so a database someone moved out from under the root is
not left as an orphan nothing remembers creating.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import requests

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MANIFEST = Path(__file__).with_name(".notion_dev_seed.json")

# Notion's documented ceiling is ~3 requests/second averaged over time, and it
# answers 429 rather than degrading. Pace up front instead of retrying into it.
REQUEST_INTERVAL = 0.34
MAX_CHILDREN_PER_APPEND = 100  # hard API cap; batching is not optional
MAX_TEXT_RUN = 1900            # API rejects a run over 2000 chars


class NotionError(RuntimeError):
    """A 4xx/5xx from Notion, carrying the body's own code and message."""


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

class Notion:
    def __init__(self, token: str, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.calls = 0
        self._last = 0.0
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _pace(self) -> None:
        gap = time.monotonic() - self._last
        if gap < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - gap)
        self._last = time.monotonic()

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.dry_run:
            print(f"  [dry-run] {method} {path}")
            return {"id": "00000000-0000-0000-0000-000000000000"}

        for attempt in range(5):
            self._pace()
            self.calls += 1
            r = self._s.request(method, f"{API}{path}", json=body, timeout=60)

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2)) + 0.5
                print(f"  rate limited, sleeping {wait:.1f}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
                continue

            if r.status_code >= 400:
                # Notion's error body is the useful part; a bare status code
                # sends you looking in the wrong place.
                try:
                    err = r.json()
                    detail = f"{err.get('code')}: {err.get('message')}"
                except ValueError:
                    detail = r.text[:500]
                raise NotionError(f"{r.status_code} on {method} {path} -- {detail}")
            return r.json()

        raise NotionError(f"gave up on {method} {path} after repeated 429/5xx")

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)

    def patch(self, path: str, body: dict) -> dict:
        return self.request("PATCH", path, body)


# --------------------------------------------------------------------------
# small builders
# --------------------------------------------------------------------------

def rt(text: str, **annotations: Any) -> list[dict]:
    """A rich_text array, split into runs Notion will accept."""
    chunks = [text[i:i + MAX_TEXT_RUN] for i in range(0, len(text), MAX_TEXT_RUN)] or [""]
    runs = []
    for chunk in chunks:
        run: dict[str, Any] = {"type": "text", "text": {"content": chunk}}
        if annotations:
            run["annotations"] = annotations
        runs.append(run)
    return runs


def link(text: str, url: str) -> dict:
    return {"type": "text", "text": {"content": text, "link": {"url": url}}}


def title_prop(text: str) -> dict:
    return {"title": rt(text)}


def para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rt(text)}}


def heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": rt(text)}}


def extract_id(raw: str) -> str:
    """Accept a bare id, a dashed id, or a pasted notion.so URL."""
    found = re.findall(r"[0-9a-fA-F]{32}", raw.strip().replace("-", ""))
    if not found:
        raise SystemExit(
            f"Could not find a Notion id in {raw!r}.\n"
            "Pass the page id, or paste the page URL from your browser."
        )
    h = found[-1].lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# fixture data
# --------------------------------------------------------------------------

TEAMS = [
    ("Platform", "Asha Menon", 6),
    ("Agents", "Ravi Kulkarni", 4),
    ("Inference", "Lena Ortiz", 5),
    ("Growth", "Tomas Ferreira", 3),
]

STATUSES = ["Backlog", "In progress", "Blocked", "Shipped"]
STATUS_COLORS = ["default", "blue", "red", "green"]
PRIORITIES = ["P0", "P1", "P2", "P3"]
PRIORITY_COLORS = ["red", "orange", "yellow", "gray"]
TAGS = ["api", "infra", "ux", "docs", "flaky", "security", "perf"]

VERBS = ["Fix", "Refactor", "Investigate", "Document", "Benchmark", "Harden", "Retire"]
NOUNS = ["the tool registry", "MCP session pooling", "the curation watermark",
         "credential injection", "the trigger sweep", "vector reindexing",
         "the approval ladder", "spend-cap accounting", "the recycle sweep"]


# --------------------------------------------------------------------------
# databases
# --------------------------------------------------------------------------

def create_teams_db(api: Notion, parent_page: str) -> str:
    print("Creating Teams database...")
    db = api.post("/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": rt("Teams"),
        "description": rt("Relation + rollup target for Tasks."),
        "properties": {
            "Name": {"title": {}},
            "Lead": {"rich_text": {}},
            "Headcount": {"number": {"format": "number"}},
        },
    })
    return db["id"]


def create_tasks_db(api: Notion, parent_page: str, teams_db: str) -> str:
    print("Creating Tasks database...")
    db = api.post("/databases", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "title": rt("Tasks"),
        "description": rt("Every property type the API will create, plus edge-case rows."),
        "properties": {
            "Name": {"title": {}},
            # A `status` property cannot be *created* through the API -- Notion
            # will read one but not write the schema -- so the honest stand-in
            # is a select. Add a real status column in the UI if you need one;
            # the seeded rows will simply show it empty.
            "Status": {"select": {"options": [
                {"name": n, "color": c} for n, c in zip(STATUSES, STATUS_COLORS)
            ]}},
            "Priority": {"select": {"options": [
                {"name": n, "color": c} for n, c in zip(PRIORITIES, PRIORITY_COLORS)
            ]}},
            "Tags": {"multi_select": {"options": [{"name": t} for t in TAGS]}},
            "Due": {"date": {}},
            "Estimate": {"number": {"format": "number"}},
            "Done": {"checkbox": {}},
            # Left unpopulated on purpose: values need workspace user ids, and
            # an always-empty people column is itself a case worth reading.
            "Owner": {"people": {}},
            "Attachments": {"files": {}},
            "Link": {"url": {}},
            "Contact": {"email": {}},
            "Phone": {"phone_number": {}},
            "Notes": {"rich_text": {}},
            "Team": {"relation": {
                "database_id": teams_db,
                "type": "single_property",
                "single_property": {},
            }},
            "Added": {"created_time": {}},
            "Touched": {"last_edited_time": {}},
        },
    })
    db_id = db["id"]

    # Formula and rollup reference other properties, so they go in a second
    # pass: a rollup naming a relation defined in the same request is rejected
    # often enough that it is not worth the coin flip.
    print("Adding formula + rollup...")
    try:
        api.patch(f"/databases/{db_id}", {"properties": {
            "Estimate x2": {"formula": {"expression": 'prop("Estimate") * 2'}},
            "Team size": {"rollup": {
                "relation_property_name": "Team",
                "rollup_property_name": "Headcount",
                "function": "sum",
            }},
        }})
    except NotionError as exc:
        # Non-fatal: the fixture is still worth having without derived columns.
        print(f"  skipped derived properties ({exc})")

    return db_id


def seed_teams(api: Notion, teams_db: str) -> list[str]:
    print(f"Seeding {len(TEAMS)} teams...")
    ids = []
    for name, lead, headcount in TEAMS:
        page = api.post("/pages", {
            "parent": {"database_id": teams_db},
            "properties": {
                "Name": title_prop(name),
                "Lead": {"rich_text": rt(lead)},
                "Headcount": {"number": headcount},
            },
        })
        ids.append(page["id"])
    return ids


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

def edge_case_rows(team_ids: list[str]) -> list[dict]:
    """Each row is here because it breaks a different naive property reader."""
    return [
        # Empty title: properties["Name"]["title"][0] raises IndexError.
        {"Name": {"title": []},
         "Status": {"select": {"name": "Backlog"}}},

        # Every optional null: the key is present, the value is None. Code that
        # checks `if "Due" in props` rather than `if props["Due"]["date"]` dies.
        {"Name": title_prop("All optionals null"),
         "Status": {"select": None}, "Priority": {"select": None},
         "Tags": {"multi_select": []}, "Due": {"date": None},
         "Estimate": {"number": None}, "Link": {"url": None},
         "Contact": {"email": None}, "Phone": {"phone_number": None},
         "Notes": {"rich_text": []}, "Team": {"relation": []}},

        # Past the 2000-char run boundary, so the title comes back as several
        # runs and title[0]["plain_text"] quietly returns a fragment.
        {"Name": title_prop("Long title " + ("lorem ipsum dolor sit amet " * 90))},

        # Unicode, emoji, RTL, and a newline inside a title.
        {"Name": title_prop("emoji \U0001F680 rtl مرحبا "
                            "cjk 漢字 newline\nsecond line"),
         "Notes": {"rich_text": rt('quotes "double" \'single\' backslash \\ tab\there')}},

        # A date range rather than a point: date["end"] is usually None.
        {"Name": title_prop("Date range, not a point"),
         "Due": {"date": {"start": "2026-09-07", "end": "2026-09-21"}}},

        # A datetime with an offset: not 10 characters, so
        # datetime.strptime(v, "%Y-%m-%d") fails here and only here.
        {"Name": title_prop("Datetime with timezone"),
         "Due": {"date": {"start": "2026-09-09T14:30:00.000+05:30"}}},

        # Saturated multi-select, every relation at once, a negative float.
        {"Name": title_prop("Every tag, every team"),
         "Tags": {"multi_select": [{"name": t} for t in TAGS]},
         "Team": {"relation": [{"id": i} for i in team_ids]},
         "Estimate": {"number": -3.5},
         "Done": {"checkbox": True}},
    ]


def seed_tasks(api: Notion, tasks_db: str, team_ids: list[str], rows: int) -> int:
    edges = edge_case_rows(team_ids)
    filler = max(0, rows - len(edges))
    total = len(edges) + filler
    note = "past 100, so listing paginates" if total > 100 else "under 100 -- NOT enough to paginate"
    print(f"Seeding {len(edges)} edge-case rows + {filler} ordinary rows "
          f"({total} total; {note})...")

    rng = random.Random(20260902)  # deterministic, so two runs are comparable
    created = 0

    for props in edges:
        api.post("/pages", {"parent": {"database_id": tasks_db}, "properties": props})
        created += 1

    for n in range(filler):
        props: dict[str, Any] = {
            "Name": title_prop(f"{rng.choice(VERBS)} {rng.choice(NOUNS)} #{n + 1:03d}"),
            "Status": {"select": {"name": rng.choice(STATUSES)}},
            "Priority": {"select": {"name": rng.choice(PRIORITIES)}},
            "Tags": {"multi_select": [
                {"name": t} for t in rng.sample(TAGS, rng.randint(0, 3))]},
            "Estimate": {"number": rng.choice([None, 1, 2, 3, 5, 8, 13])},
            "Done": {"checkbox": rng.random() < 0.3},
            "Team": {"relation": [{"id": rng.choice(team_ids)}]},
        }
        # Optional fields are left off most rows on purpose, so the null branch
        # is exercised in bulk rather than by one hand-written case.
        if rng.random() < 0.7:
            props["Due"] = {"date": {
                "start": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"}}
        if rng.random() < 0.4:
            props["Link"] = {"url": f"https://example.com/issue/{n + 1}"}
        if rng.random() < 0.3:
            props["Contact"] = {"email": f"dev{n + 1}@example.com"}
        if rng.random() < 0.5:
            props["Notes"] = {"rich_text": rt(
                f"Row {n + 1}. " + "Context. " * rng.randint(1, 40))}

        api.post("/pages", {"parent": {"database_id": tasks_db}, "properties": props})
        created += 1

        if created % 25 == 0:
            print(f"  {created} rows...")

    return created


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

def kitchen_sink_blocks() -> list[dict]:
    """One of every block type a connector is likely to meet."""
    return [
        heading(1, "Kitchen sink"),
        para("Every block below is here because something reads blocks "
             "generically and then meets one of these."),
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "Bold "},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": "italic "},
             "annotations": {"italic": True}},
            {"type": "text", "text": {"content": "struck "},
             "annotations": {"strikethrough": True}},
            {"type": "text", "text": {"content": "code"},
             "annotations": {"code": True}},
            {"type": "text", "text": {"content": ", and "}},
            link("an inline link", "https://developers.notion.com/reference/rich-text"),
        ]}},

        heading(2, "Lists"),
        {"object": "block", "type": "bulleted_list_item",
         "bulleted_list_item": {"rich_text": rt("First bullet")}},
        {"object": "block", "type": "bulleted_list_item",
         "bulleted_list_item": {
             "rich_text": rt("Bullet with a nested child"),
             "children": [{"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rt("Nested one level")}}],
         }},
        {"object": "block", "type": "numbered_list_item",
         "numbered_list_item": {"rich_text": rt("Numbered item")}},
        {"object": "block", "type": "to_do",
         "to_do": {"rich_text": rt("Unchecked task"), "checked": False}},
        {"object": "block", "type": "to_do",
         "to_do": {"rich_text": rt("Checked task"), "checked": True}},

        heading(2, "Containers"),
        {"object": "block", "type": "toggle", "toggle": {
            "rich_text": rt("Toggle -- children are NOT in the parent's block list"),
            "children": [para("You only see me if you recursed on has_children.")],
        }},
        {"object": "block", "type": "callout", "callout": {
            "rich_text": rt("Callout with an emoji icon"),
            "icon": {"type": "emoji", "emoji": "\U0001F4A1"},
            "color": "blue_background",
        }},
        {"object": "block", "type": "quote",
         "quote": {"rich_text": rt("A quote block.")}},
        {"object": "block", "type": "divider", "divider": {}},

        heading(2, "Code and media"),
        {"object": "block", "type": "code", "code": {
            "language": "python",
            "rich_text": rt('def hello():\n    return "world"  # unicode: cafe ✅\n'),
        }},
        {"object": "block", "type": "code", "code": {
            "language": "json",
            "rich_text": rt('{"nested": {"array": [1, 2, 3], "null": null}}'),
        }},
        {"object": "block", "type": "equation",
         "equation": {"expression": "e^{i\\pi} + 1 = 0"}},
        {"object": "block", "type": "image", "image": {
            "type": "external",
            "external": {"url": "https://www.notion.so/images/page-cover/"
                                "met_william_morris_1875.jpg"},
            "caption": rt("External image. Uploaded files come back with a signed "
                          "URL that expires in an hour -- this one does not, which "
                          "is exactly why you should also test with an uploaded file."),
        }},
        {"object": "block", "type": "bookmark", "bookmark": {
            "url": "https://developers.notion.com/reference/block",
            "caption": rt("Bookmark block"),
        }},

        heading(2, "Table"),
        {"object": "block", "type": "table", "table": {
            "table_width": 3,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {"object": "block", "type": "table_row", "table_row": {
                    "cells": [rt("Property"), rt("Type"), rt("Nullable")]}},
                {"object": "block", "type": "table_row", "table_row": {
                    "cells": [rt("Due"), rt("date"), rt("yes")]}},
                {"object": "block", "type": "table_row", "table_row": {
                    "cells": [rt("Team"), rt("relation"), rt("yes")]}},
            ],
        }},
    ]


def create_kitchen_sink(api: Notion, parent_page: str, filler_blocks: int) -> str:
    print("Creating Kitchen Sink page...")
    blocks = kitchen_sink_blocks()

    page = api.post("/pages", {
        "parent": {"type": "page_id", "page_id": parent_page},
        "icon": {"type": "emoji", "emoji": "\U0001F9EA"},
        "properties": {"title": title_prop("Kitchen Sink")},
        "children": blocks[:MAX_CHILDREN_PER_APPEND],
    })
    page_id = page["id"]

    rest = list(blocks[MAX_CHILDREN_PER_APPEND:])
    # Padding so the block list itself runs past one page of 100: the same
    # cursor bug as the database, on a different endpoint.
    rest.append(heading(2, "Padding (forces block pagination)"))
    rest += [para(f"Filler paragraph {i + 1} of {filler_blocks}.")
             for i in range(filler_blocks)]

    for i in range(0, len(rest), MAX_CHILDREN_PER_APPEND):
        batch = rest[i:i + MAX_CHILDREN_PER_APPEND]
        print(f"  appending blocks {i + 1}-{i + len(batch)}...")
        api.patch(f"/blocks/{page_id}/children", {"children": batch})

    return page_id


def create_nested_pages(api: Notion, parent_page: str, depth: int = 3) -> list[str]:
    """A page tree, so 'walk the children' has something to recurse into."""
    print(f"Creating a {depth}-deep page tree...")
    ids, current = [], parent_page
    for level in range(1, depth + 1):
        page = api.post("/pages", {
            "parent": {"type": "page_id", "page_id": current},
            "properties": {"title": title_prop(f"Nested level {level}")},
            "children": [
                para(f"This page sits {level} level(s) below the fixture root."),
                para("A subpage is a block of type `child_page`, which is why a "
                     "flat block read never finds the page below this one."),
            ],
        })
        ids.append(page["id"])
        current = page["id"]
    return ids


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def do_seed(api: Notion, parent: str, rows: int, filler_blocks: int) -> None:
    existing = load_manifest()
    if existing.get("root_page"):
        raise SystemExit(
            f"A previous seed is recorded in {MANIFEST.name} "
            f"(root {existing['root_page']}).\n"
            "Run --reset first, or delete that file if you archived it by hand."
        )

    print(f"\nSeeding under parent page {parent}\n")

    root = api.post("/pages", {
        "parent": {"type": "page_id", "page_id": parent},
        "icon": {"type": "emoji", "emoji": "\U0001F9F0"},
        "properties": {"title": title_prop("AIAAS Dev Fixtures")},
        "children": [para("Generated by Backend/seed_notion_dev.py. Safe to "
                          "archive; re-run the script to rebuild.")],
    })
    root_id = root["id"]

    # Written before anything else is created: a crash mid-run must still leave
    # a handle on the root, or --reset has nothing to archive.
    manifest: dict[str, Any] = {
        "root_page": root_id,
        "parent": parent,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_manifest(manifest)

    manifest["teams_db"] = teams_db = create_teams_db(api, root_id)
    save_manifest(manifest)

    manifest["tasks_db"] = tasks_db = create_tasks_db(api, root_id, teams_db)
    save_manifest(manifest)

    team_ids = seed_teams(api, teams_db)
    manifest["task_rows"] = count = seed_tasks(api, tasks_db, team_ids, rows)
    save_manifest(manifest)

    manifest["kitchen_sink"] = create_kitchen_sink(api, root_id, filler_blocks)
    manifest["nested_pages"] = create_nested_pages(api, root_id)
    save_manifest(manifest)

    print(f"\nDone. {api.calls} API calls.")
    print(f"  Root page:  {root_id}")
    print(f"  Teams DB:   {teams_db}")
    print(f"  Tasks DB:   {tasks_db}  ({count} rows)")
    print(f"  Manifest:   {MANIFEST}")
    print("\nReset with:  python seed_notion_dev.py --token ... --reset\n")


def do_reset(api: Notion) -> None:
    manifest = load_manifest()
    if not manifest:
        raise SystemExit(f"No {MANIFEST.name} found -- nothing recorded to reset.")

    # Archiving the root archives everything beneath it. The rest are archived
    # explicitly anyway, so a database moved out from under the root does not
    # survive as an orphan nothing remembers creating.
    targets = [manifest.get("root_page")]
    targets += manifest.get("nested_pages", [])
    targets += [manifest.get("kitchen_sink"), manifest.get("tasks_db"),
                manifest.get("teams_db")]

    for obj in [t for t in targets if t]:
        try:
            api.patch(f"/pages/{obj}", {"archived": True})
            print(f"  archived {obj}")
        except NotionError as exc:
            # Already archived, or archived as a descendant of the root.
            print(f"  skipped {obj} ({exc})")

    MANIFEST.unlink(missing_ok=True)
    print(f"\nReset complete. Removed {MANIFEST.name}.")
    print("Notion 'archived' means in the trash -- empty it in the UI to purge.\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Seed (or reset) a Notion dev workspace with connector-test fixtures.")
    p.add_argument("--token", default=os.environ.get("NOTION_TOKEN"),
                   help="Notion integration token (or set NOTION_TOKEN).")
    p.add_argument("--parent",
                   help="Page id or URL to build under. Required unless --reset.")
    p.add_argument("--rows", type=int, default=137,
                   help="Task rows to create. Default 137: comfortably past "
                        "Notion's 100-per-page cap, so a missing cursor shows.")
    p.add_argument("--filler-blocks", type=int, default=110,
                   help="Padding paragraphs on the Kitchen Sink page, to push "
                        "its block list past one page too.")
    p.add_argument("--reset", action="store_true",
                   help="Archive everything recorded in the manifest.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the calls that would be made; touch nothing.")
    args = p.parse_args()

    if not args.token and not args.dry_run:
        raise SystemExit("Pass --token or set NOTION_TOKEN.")
    if not args.reset and not args.parent:
        raise SystemExit("Pass --parent <page id or URL> (or --reset).")

    api = Notion(args.token or "dry-run", dry_run=args.dry_run)

    try:
        if args.reset:
            do_reset(api)
        else:
            do_seed(api, extract_id(args.parent), args.rows, args.filler_blocks)
    except NotionError as exc:
        raise SystemExit(f"\nNotion API error -- {exc}\n")


if __name__ == "__main__":
    main()
