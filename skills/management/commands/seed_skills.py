"""
Seed a set of realistic sample Skills for local development.

A Skill is not decoration: `agents/agent/runtime.py::build_system_prompt`
pastes `title` + `content` straight into the agent's system prompt under
"SKILLS - instructions you have been given for this work". So the sample rows
here are written the way a real skill has to be written - procedural
instructions addressed to the agent, not prose about the topic - otherwise the
Skills page looks populated while every agent that attaches one gets noise.

Categories match the picker in `better-n8n-frontend/src/pages/Skills.tsx`;
anything else renders as a chip the user cannot reproduce from the UI.

Idempotent: rows are matched on (user, title), so re-running updates in place
rather than duplicating. `--embed` additionally indexes each skill into the
shared skills KB (inference app) so the vector half of `SkillService.hybrid_search`
has something to score (without it search still works, on the fuzzy half alone).
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from skills.models import Skill

SKILLS: list[dict] = [
    # -- Development ------------------------------------------------------
    {
        "title": "Python Code Review Checklist",
        "description": "Review Python diffs for correctness, safety, and style before approving.",
        "category": "Development",
        "is_shared": True,
        "content": """# Python Code Review Checklist

Work through the diff in this order. Report findings most severe first, and
quote the file and line for each one.

## 1. Correctness
- Trace every new branch with at least one concrete input. State the input.
- Check boundary values: empty list, single element, None, zero, negative.
- Any bare `except:` that swallows the error without logging is a finding.
- Mutable default arguments (`def f(x=[])`) are always a finding.

## 2. Async safety
- No blocking call inside an `async def`: requests, time.sleep, plain ORM access.
- Detached work must go through `spawn()`, never bare `asyncio.create_task`.
- Anything holding a lock across an `await` gets flagged.

## 3. Data handling
- User input reaching SQL, a shell, or `eval` must be parameterised or escaped.
- Secrets never land in a log line, an exception message, or a default value.

## 4. Tests
- Every fixed bug needs a test that fails without the fix.
- New branches without coverage: say which branch, not just "needs tests".

## Output format
- One bullet per finding: path:line, what breaks, and the input that breaks it.
- End with a one-line verdict: approve, approve-with-nits, or request-changes.
- If nothing is wrong, say so plainly. Do not invent findings to look thorough.
""",
    },
    {
        "title": "SQL Query Optimization",
        "description": "Diagnose a slow query from its plan and propose the smallest effective fix.",
        "category": "Development",
        "is_shared": True,
        "content": """# SQL Query Optimization

## Before changing anything
- Ask for the query, the EXPLAIN ANALYZE output, and the row counts of each table.
- Without the plan you are guessing. Say that and ask, rather than speculating.

## Read the plan in this order
- Find the node with the largest actual time. That is the target, nothing else.
- Compare estimated vs actual rows. A 100x gap means stale statistics; suggest ANALYZE first.
- A sequential scan on a large table under a selective filter is an index candidate.
- A nested loop with a high loop count usually means the join key is unindexed.

## Fixes, cheapest first
- ANALYZE the table. Free, and it fixes bad estimates outright.
- Add the index the filter or join actually needs. Name the exact columns and their order.
- Rewrite: pull correlated subqueries into a join, drop SELECT *, add LIMIT.
- Denormalise or add a materialized view. Last resort, and say what it costs to maintain.

## Always state
- Expected improvement, and how you arrived at that number.
- The write cost of any new index.
- How to verify: the exact command to re-run and which number should drop.
""",
    },
    {
        "title": "API Error Triage",
        "description": "Turn a 5xx report into a root cause with a reproduction and a fix.",
        "category": "Development",
        "is_shared": False,
        "content": """# API Error Triage

## Collect first, theorise second
- Endpoint, HTTP method, status code, and the exact timestamp.
- Request id or trace id if one exists. It is what links the logs together.
- Whether it reproduces, and whether it hits one user or all of them.

## Classify by status
- 400 or 422: the contract is wrong. Compare the payload against the serializer.
- 401 or 403: auth or permission. Check token expiry before the permission class.
- 404: routing, or a scoped queryset filtering the row out. Check the queryset first.
- 429: throttling. Report the limit and window rather than raising it reflexively.
- 500: read the traceback bottom-up and stop at the first line in project code.
- 502 or 504: upstream or timeout, not the view. Check the dependency's health.

## Then
- Reproduce with the smallest possible request. Paste it as a curl command.
- Name the single line where the failure originates.
- Propose a fix and the test that would have caught it.

## Never
- Never close a report as "cannot reproduce" without saying what you tried.
- Never fix by widening a try/except. That hides the next occurrence.
""",
    },

    # -- Data Science -----------------------------------------------------
    {
        "title": "CSV Data Cleaning Playbook",
        "description": "Profile, clean, and validate a raw CSV before any analysis runs on it.",
        "category": "Data Science",
        "is_shared": True,
        "content": """# CSV Data Cleaning Playbook

Run every step in the Python sandbox and show the output. Never describe a
result you have not actually computed.

## 1. Profile before touching anything
- Shape, dtypes, and the first ten rows.
- Null count and null percentage per column.
- Duplicate row count, and cardinality of every object column.
- Report this profile to the user before proposing a single change.

## 2. Structure
- Normalise column names: lowercase, strip whitespace, spaces to underscores.
- Coerce types explicitly. Dates with an explicit format, numerics with errors='coerce'.
- Count what coercion turned into NaT or NaN and report it. Silent coercion loses data.

## 3. Missing values
- Never impute without saying which strategy you used and why.
- Under 5% missing and not systematic: drop those rows.
- Numeric and skewed: median. Numeric and symmetric: mean. Categorical: an explicit "unknown".
- A column above 40% missing is a candidate for dropping. Ask before dropping it.

## 4. Outliers
- Flag with the 1.5x IQR rule and report them. Do not remove them on your own
  initiative. In finance and sensor data the outliers are frequently the signal.

## 5. Validate and hand back
- Re-run the profile and show before and after side by side.
- List every transformation applied, in order, as a reproducible list.
- Save to a new file. Never overwrite the user's raw input.
""",
    },
    {
        "title": "Exploratory Data Analysis Report",
        "description": "Produce a structured EDA with charts and findings a stakeholder can act on.",
        "category": "Data Science",
        "is_shared": True,
        "content": """# Exploratory Data Analysis Report

## Structure, in this order
- Dataset overview: rows, columns, date range, one line on provenance.
- Data quality: nulls, duplicates, type problems, anything that limits the conclusions.
- Univariate: distribution of each key variable; note skew and multimodality.
- Bivariate: relationships against the target, plus a correlation matrix for numerics.
- Findings: three to five statements, each with the number that supports it.
- Limitations: what this data cannot answer. Never omit this section.

## Charts
- One idea per chart. The title states the finding, not the variable names.
- Label both axes with units. Never leave a bare index axis.
- Histogram for distribution, box plot for comparison across groups, scatter for
  relationships, line only when the x axis is genuinely ordered time.
- No pie charts above five categories. No dual y-axes.

## Rules for the findings section
- Every claim carries its number: "churn is 3.2x higher on monthly plans, 18.4% vs 5.7%".
- Correlation is reported as correlation. Do not phrase it as cause.
- If a segment has fewer than 30 rows, say so next to the number.
""",
    },

    # -- Automation -------------------------------------------------------
    {
        "title": "Workflow Retry and Backoff Policy",
        "description": "Decide what a failing workflow node should retry, and for how long.",
        "category": "Automation",
        "is_shared": True,
        "content": """# Workflow Retry and Backoff Policy

## Retry only what can succeed on a second attempt
- Retry: 429, 502, 503, 504, connection reset, DNS failure, socket timeout.
- Never retry: 400, 401, 403, 404, 422. The request is wrong; repeating it wastes
  quota and, on auth failures, can lock the account.
- Ambiguous 500: retry at most twice, then stop and surface the response body.

## Backoff
- Exponential with jitter: delay = min(base * 2 ** attempt, cap) * (0.5 + random/2).
- base 1s, cap 30s, 5 attempts for interactive runs; cap 300s for batch runs.
- Honour Retry-After when the response carries it. It overrides the formula.
- Jitter is not optional. Without it every retrying run collides on the same tick.

## Idempotency
- Before enabling retry on a node that writes, confirm the write is idempotent.
- If it is not, attach an idempotency key or move the retry to a read-only step.
- Payments, emails, and message sends are never retried blind.

## Give up loudly
- On final failure emit the attempt count, the last status, and the response body.
- Never let a workflow report success on a step that exhausted its retries.
""",
    },
    {
        "title": "Webhook Payload Validation",
        "description": "Verify, validate, and safely normalise an inbound webhook before acting on it.",
        "category": "Automation",
        "is_shared": False,
        "content": """# Webhook Payload Validation

## Order matters, do not reorder these
- Verify the signature first, on the raw body, before any parsing. Use a
  constant-time compare. Reject with 401 and log nothing but the delivery id.
- Check the timestamp. Reject anything older than five minutes to stop replays.
- Only then parse the JSON. Parsing before verifying hands untrusted input to the parser.

## Validate the shape
- Reject unknown event types rather than falling through to a default branch.
- Required fields present and correctly typed. No coercing a string id to an int.
- Enforce a body size cap. Unbounded payloads are an availability problem.

## Deduplicate
- Providers retry. Store the delivery id and drop repeats; at-least-once is the norm.

## Respond
- Return 2xx as soon as the payload is accepted and queued, not after processing.
  Slow handlers cause the provider to retry, which multiplies the work.
- Do the real work in a background task.
- Never echo the payload back in the response body.
""",
    },

    # -- Security ---------------------------------------------------------
    {
        "title": "Secret Scanning and Redaction",
        "description": "Find credentials in code, logs, or output and redact them safely.",
        "category": "Security",
        "is_shared": True,
        "content": """# Secret Scanning and Redaction

## What counts as a secret
- API keys, bearer and JWT tokens, private keys, database connection strings
  containing a password, cloud access keys, session cookies, webhook signing secrets.
- Not secrets: public keys, key ids, account ids, non-sensitive config.

## Scanning
- Check the full history, not just the working tree. A rotated key is still exposed
  in the commit that removed it.
- Include .env files, CI config, fixtures, notebooks, and log samples.
- Rank hits by blast radius: production write credentials before a local test token.

## Redaction
- Replace with a fixed placeholder, never a partial mask that still shows entropy.
  Keep at most the last four characters, and only when the reader must tell two keys apart.
- Redact at the point of logging, not after. A redacting log filter beats a cleanup script.

## After a confirmed leak, in this order
- Rotate the credential. Rotate first; scrubbing history does not un-leak it.
- Revoke the old value at the provider.
- Check access logs for use of the leaked key.
- Then clean the history, and record the incident.

## Never
- Never paste a discovered secret into a report, ticket, or chat message.
- Never commit a fix whose own diff contains the secret.
""",
    },
    {
        "title": "Dependency CVE Triage",
        "description": "Turn a vulnerability scan into a ranked, evidence-backed action list.",
        "category": "Security",
        "is_shared": False,
        "content": """# Dependency CVE Triage

## Do not act on severity alone
A critical CVE in a code path you never call outranks nothing. For each finding
establish, in order:
- Reachability: is the vulnerable function actually called? Grep for it and say where.
- Exposure: does untrusted input reach that path?
- Direct or transitive: transitive means the fix may be a parent bump.
- Fix availability: is there a patched version, and does it break the API?

## Rank
- P0: reachable, reachable from user input, and a patch exists. Fix today.
- P1: reachable but no untrusted input, or no patch yet. Schedule it and mitigate.
- P2: not reachable. Record the reasoning and bump on the next routine update.

## Report per finding
- Package, installed version, fixed version, CVE id.
- The one-line reachability verdict with the file and line that proves it.
- The upgrade command, and whether it is a breaking change.

## Never
- Never suppress a finding without writing down why, and where that reason lives.
- Never bump a major version silently as part of a security fix. Call it out.
""",
    },

    # -- Marketing --------------------------------------------------------
    {
        "title": "SEO Blog Post Outline",
        "description": "Build a search-intent-driven outline before any drafting starts.",
        "category": "Marketing",
        "is_shared": True,
        "content": """# SEO Blog Post Outline

## Start with intent, not keywords
- Classify the primary query: informational, comparison, or transactional.
- Read what currently ranks and note what all of them cover. That is table stakes.
- Name the one thing none of them cover. That is the reason this post exists.
  If there is no such angle, say so rather than producing another identical post.

## Outline shape
- Title: under 60 characters, primary keyword near the front, no clickbait.
- Meta description: 150 to 160 characters, and it states the payoff.
- Intro: three sentences. The problem, who has it, what this post gives them.
- H2 sections: one question per H2, phrased the way a person would search it.
- H3s: the steps or sub-points. Anything needing more than four H3s is its own post.
- Conclusion: the summary plus exactly one call to action.

## Rules
- Keyword density is not a target. Write for the reader and the terms follow.
- Every claim about a number needs a source and that source's date.
- One idea per paragraph, four sentences at most.
- Suggest internal links by topic, and flag where a diagram or screenshot earns its place.
""",
    },

    # -- Finance ----------------------------------------------------------
    {
        "title": "Invoice Data Extraction",
        "description": "Pull structured fields from an invoice with explicit confidence and no guessing.",
        "category": "Finance",
        "is_shared": False,
        "content": """# Invoice Data Extraction

## Fields to extract
- invoice_number, issue_date, due_date
- vendor_name, vendor_tax_id, bill_to
- line_items: description, quantity, unit_price, amount
- subtotal, tax_amount, tax_rate, total, currency
- payment_terms, and bank_details marked as sensitive

## Rules
- Return null for any field not present on the document. Never infer a missing value
  from context. An invented invoice number reconciles against nothing.
- Dates go out as ISO 8601. Keep the raw string alongside when the format is ambiguous:
  03/04/2025 is two different dates depending on locale.
- Amounts are decimal strings, never floats. Currency as an ISO 4217 code.
- Strip thousands separators. Keep the decimal separator the document actually used.

## Validate before returning
- The line item amounts must sum to the subtotal. Report the delta if they do not.
- Subtotal plus tax must equal the total. Report the delta if it does not.
- A failed check is reported, never silently corrected.

## Output
- JSON only, plus a per-field confidence between 0 and 1.
- Anything below 0.8 goes in a needs_review list for a human to confirm.
""",
    },

    # -- Communication ----------------------------------------------------
    {
        "title": "Meeting Notes to Action Items",
        "description": "Convert a transcript into owned, dated actions and nothing else.",
        "category": "Communication",
        "is_shared": True,
        "content": """# Meeting Notes to Action Items

## Output, in this order
- Decisions: what was actually settled. One line each, no rationale.
- Action items: owner, action, due date. Every one of the three parts.
- Open questions: unresolved, with who needs to resolve them.
- Context: only what a person who missed the meeting needs. Cut everything else.

## Rules for action items
- No owner means it is not an action item. Put it under open questions instead.
- "Soon", "next sprint", and "ASAP" are not dates. Ask, or write "date TBC".
- Use the speaker's own verb. Do not upgrade "look into" to "implement".
- Never invent an owner from whoever talked most about the topic.

## Rules for the whole summary
- No filler, no pleasantries, no attendance list unless it was asked for.
- Disagreement that did not resolve is recorded as an open question, not smoothed over.
- If someone spoke conditionally, keep the condition. Dropped conditions are how
  summaries become wrong.
- Length target: a page of transcript becomes five lines. Ruthless is correct here.
""",
    },
    {
        "title": "Customer Support Reply",
        "description": "Answer a support ticket accurately, briefly, and without over-promising.",
        "category": "Communication",
        "is_shared": False,
        "content": """# Customer Support Reply

## Structure
- One sentence confirming the specific problem, in their words rather than a template.
- The answer or the fix, as steps they can follow.
- What happens next if anything is still open, with a real timeframe.

## Tone
- Plain language. No "we sincerely apologise for any inconvenience caused".
- One apology at most, and only when something actually went wrong on our side.
- Never blame the customer, and never say "as I mentioned".
- Match their register. A terse ticket gets a terse reply.

## Accuracy
- Never promise a ship date for a feature. "On the roadmap, no date yet" is honest.
- Never guess at root cause. "I don't know yet, here is how I'm finding out" is stronger.
- If it is a bug, call it a bug and give the tracking id.
- If the answer is no, say no in the first two sentences. Burying it wastes their time.

## Before sending
- Reread the ticket. Did you answer the question they asked, or the one you assumed?
- Strip every internal detail: stack traces, ticket links, teammate names.
""",
    },

    # -- General ----------------------------------------------------------
    {
        "title": "Web Research with Citations",
        "description": "Research a question online and report only what the sources actually support.",
        "category": "General",
        "is_shared": True,
        "content": """# Web Research with Citations

## Search
- Break the question into its separate factual claims and search each one.
- Run at least two differently worded searches per claim. The first phrasing biases results.
- Prefer primary sources: the docs, the paper, the filing, the changelog, over coverage of them.

## Source quality
- Record the publication date of every source. Anything undated is treated as unreliable.
- For version-dependent or fast-moving topics, discard sources older than 18 months
  unless nothing newer exists, and say so when that happens.
- Two outlets repeating one press release is one source, not two. Trace it back.

## Reporting
- Every factual claim carries its source link inline.
- Attribute contested claims: "X reports", not a bare assertion.
- Where sources disagree, present both and say which is better supported and why.
- State plainly what you could not find. A gap reported is useful; a gap filled by
  inference is a fabrication.

## Never
- Never cite a page you did not open.
- Never present a search snippet as if you had read the full page.
- Never restate a number more precisely than the source gave it.
""",
    },
]


class Command(BaseCommand):
    help = "Seed realistic sample Skills for local development (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            help="Username to own the skills. Defaults to the first superuser, "
                 "else the first user in the table.",
        )
        parser.add_argument(
            "--embed",
            action="store_true",
            help="Also index skills into the shared skills KB so vector search "
                 "scores these rows. Loads the platform embedding model, so it "
                 "is slow on first run.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete the seeded titles for this user before inserting.",
        )

    def handle(self, *args, **options):
        User = get_user_model()

        if options["user"]:
            user = User.objects.filter(username=options["user"]).first()
            if not user:
                raise CommandError(f"No user with username {options['user']!r}")
        else:
            user = (User.objects.filter(is_superuser=True).order_by("id").first()
                    or User.objects.order_by("id").first())
            if not user:
                raise CommandError(
                    "No users exist. Run `python manage.py createsuperuser` first."
                )

        titles = [s["title"] for s in SKILLS]

        with transaction.atomic():
            if options["clear"]:
                deleted, _ = Skill.objects.filter(user=user, title__in=titles).delete()
                self.stdout.write(f"Cleared {deleted} existing seeded skill(s).")

            created = updated = 0
            for spec in SKILLS:
                _, was_created = Skill.objects.update_or_create(
                    user=user,
                    title=spec["title"],
                    defaults={
                        "description": spec["description"],
                        "content": spec["content"].strip(),
                        "category": spec["category"],
                        "is_shared": spec["is_shared"],
                    },
                )
                created += was_created
                updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"Seeded skills for {user.username}: {created} created, {updated} updated "
            f"({sum(s['is_shared'] for s in SKILLS)} shared publicly)."
        ))

        if options["embed"]:
            self._embed(user, titles)

    def _embed(self, user, titles):
        from inference.engine import run_kb_async
        from skills.services import SkillService

        service = SkillService()
        done = failed = 0
        for skill in Skill.objects.filter(user=user, title__in=titles):
            try:
                run_kb_async(service.update_embedding(skill))
                done += 1
            except Exception as exc:  # embeddings are optional; search falls back to fuzzy
                failed += 1
                self.stderr.write(f"  embedding failed for {skill.title!r}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Embedded {done} skill(s), {failed} failed."))
