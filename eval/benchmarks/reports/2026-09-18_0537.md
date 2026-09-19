# AIAAS benchmark scorecard

Generated 2026-09-18 05:37 UTC.

## Headline

- **Cases passed:** 5 / 6 (83%)
- **Capability:** 5/6 cases (83%), 2/3 suites at their pass bar
- **Guardrails:** not run
- **Agent model:** `openrouter/deepseek/deepseek-v4.1-flash`
- **Judge model:** `openrouter/meta/muse-spark-1.3-contributor`
- **Real cost (agent runs):** $0.0766 (≈ ₹6.74) for 712,841 tokens. Judge calls are billed to the same key but not recorded, so they are not included.
- **Wall time:** 1403.6s

## Suites

| Suite | Group | Verdict | Passed | Score / bar | Errors | Review | Time | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|
| Work: Analyst workday | capability | REVIEW | 1/2 | 0.50 / 0.67 | 1 | 1 | 817.5s | 100,838 | $0.0134 |
| Work: Analyst workday | capability | PASS | 2/2 | 1.00 / 0.67 | 0 | 0 | 489.1s | 488,553 | $0.0491 |
| Work: Analyst workday | capability | PASS | 2/2 | 1.00 / 0.67 | 0 | 0 | 97.0s | 123,450 | $0.0141 |

## Reliability

Each suite was attempted more than once from a fresh workspace. **pass@1** is the share of attempts that passed; **pass^k** is the share of cases that passed *every* time.

| Suite | Group | Attempts | pass@1 | pass^k |
|---|---|---|---|---|
| Work: Analyst workday | capability | 3 | 83% | 50% |

| Suite › Case | Passed |
|---|---|
| Work: Analyst workday › Month-end revenue close | ⚠️ 2/3 |
| Work: Analyst workday › Bank reconciliation | ✅ 3/3 |

## Needs attention

### Errored: the agent never answered (1)

- **Work: Analyst workday › Month-end revenue close**: run errored: The model took too long to respond.

## Results by suite

### Work: Analyst workday — REVIEW

*What a pass proves:* Given a folder of messy real-world exports and a written spec, the agent produces output files whose every number is right, the same way every time.

Agent `[Bench] Operator` · model `openrouter/deepseek/deepseek-v4.1-flash` · run `6d567c56-dd4d-44e1-be82-e79183865a1a` · status `awaiting_review`

| Case | Result | Why it failed | Time | Tokens |
|---|---|---|---|---|
| Month-end revenue close | ERROR | run errored: The model took too long to respond. | 817.5s | 0 |
| Bank reconciliation | PASS |  | 195.1s | 100,838 |

### Work: Analyst workday — PASS

*What a pass proves:* Given a folder of messy real-world exports and a written spec, the agent produces output files whose every number is right, the same way every time.

Agent `[Bench] Operator` · model `openrouter/deepseek/deepseek-v4.1-flash` · run `b6d19679-8b1d-4a62-b6e1-651a8734c33a` · status `completed`

| Case | Result | Why it failed | Time | Tokens |
|---|---|---|---|---|
| Month-end revenue close | PASS |  | 489.0s | 439,179 |
| Bank reconciliation | PASS |  | 66.9s | 49,374 |

### Work: Analyst workday — PASS

*What a pass proves:* Given a folder of messy real-world exports and a written spec, the agent produces output files whose every number is right, the same way every time.

Agent `[Bench] Operator` · model `openrouter/deepseek/deepseek-v4.1-flash` · run `4f328159-f99d-4e89-9d0a-a5092bcb936e` · status `completed`

| Case | Result | Why it failed | Time | Tokens |
|---|---|---|---|---|
| Month-end revenue close | PASS |  | 96.9s | 65,546 |
| Bank reconciliation | PASS |  | 82.2s | 57,904 |

## Case details

### Work: Analyst workday

#### Month-end revenue close — ERROR

- **Goal:** The month-end close for August 2026 is due. Everything you need is in {workspace}. Follow README.md exactly and write the two output files there.
- **Tools really called:** none
- **Time / tokens / cost:** 817.5s · 0 · $0.0000 (≈ ₹0.00)

| Grader | Passed | Detail |
|---|---|---|
| (run) | ❌ | The model took too long to respond. |

#### Bank reconciliation — PASS

- **Goal:** Reconcile the August bank statement. The files and instructions are in {workspace} (see README.md).
- **Tools really called:** `list_files`, `list_files`, `read_file`, `read_file`, `read_file`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `write_file`, `read_file`
- **Time / tokens / cost:** 195.1s · 100,838 · $0.0134 (≈ ₹1.18)
- **Execution:** `366bbac1-a6ab-40a7-aa34-ec306fb84d83` (open on /runs)

| Grader | Passed | Detail |
|---|---|---|
| `file_exists` | ✅ |  |
| `csv_rows` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `tool_used` | ✅ |  |

<details><summary>Answer excerpt</summary>

```text
**August 2026 bank reconciliation — done.** `unmatched.csv` written to `/work/bank-recon/`.

**Matching rule applied:** equal amount to the cent AND dates ≤ 3 days apart, one-to-one.

**10 of 13 bank lines and 10 of 12 ledger entries matched cleanly:**

| Bank line | Ledger | Amount | Day gap |
|---|---|---|---|
| 1 | L-01 | -1,500.00 | 0 |
| 2 | L-02 | 4,200.00 | 2 |
| 3 | L-03 | -312.45 | 1 |
| 4 | L-04 | -89.90 | 0 |
| 5 | L-05 | 2,750.00 | 1 |
| 6 | L-06 | -6,100.00 | 0 |
| 7 | L-07 | -49.00 | 1 |
| 8 | L-08 | -49.00 | 2 |
| 9 | L-09 | -230.00 | 2 |
| 10 | L-10 | 1,800.00 | 1 |

Lines 7 and 8 (both -49.00) each had two candidate ledger entries (L-07, L-08, same date); the pairing is stil…
```

</details>

### Work: Analyst workday

#### Month-end revenue close — PASS

- **Goal:** The month-end close for August 2026 is due. Everything you need is in {workspace}. Follow README.md exactly and write the two output files there.
- **Tools really called:** `list_files`, `read_file`, `read_file`, `read_file`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `write_file`, `write_file`, `read_file`, `read_file`
- **Time / tokens / cost:** 489.0s · 439,179 · $0.0443 (≈ ₹3.90)
- **Execution:** `ef0eb331-0f41-4bd2-98e0-4656f971fc06` (open on /runs)

| Grader | Passed | Detail |
|---|---|---|
| `file_exists` | ✅ |  |
| `csv_rows` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `file_number` | ✅ |  |
| `file_number` | ✅ |  |
| `file_number` | ✅ |  |
| `file_regex` | ✅ |  |
| `tool_used` | ✅ |  |

<details><summary>Answer excerpt</summary>

```text
Both output files are written and verified.

**What I did** (applying the README rules in order to `orders.csv` + `fx.json`):

1. **Deduplicated** on `order_id`, keeping first occurrence → 3 rows removed (A-1021, A-1042, A-1045).
2. **Kept August 2026** orders only, parsing both `YYYY-MM-DD` and `DD/MM/YYYY` → 51 rows.
3. **Skipped blank amounts** → 2 rows (A-1013 west, A-1034 North).
4. **Normalised region**: trimmed + title-cased, so `" East "`, `EAST`, `east` → `East`.
5. **Converted to USD** with the fx rates (USD 1.0, EUR 1.08, INR 0.012).
6. **Net = paid − refunded** per region, ignoring `cancelled`.

**`/work/month-end-close/summary.csv`**

| region | paid_orders | net_usd |
|---|---|…
```

</details>

#### Bank reconciliation — PASS

- **Goal:** Reconcile the August bank statement. The files and instructions are in {workspace} (see README.md).
- **Tools really called:** `list_files`, `read_file`, `read_file`, `read_file`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `write_file`, `read_file`
- **Time / tokens / cost:** 66.9s · 49,374 · $0.0048 (≈ ₹0.42)
- **Execution:** `96544245-715b-47e4-b7d3-388075a192f5` (open on /runs)

| Grader | Passed | Detail |
|---|---|---|
| `file_exists` | ✅ |  |
| `csv_rows` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `tool_used` | ✅ |  |

<details><summary>Answer excerpt</summary>

```text
Reconciliation of the August 2026 statement is done. Output written to `/work/bank-recon/unmatched.csv` and read back to verify.

**Matching rule applied:** equal amount to the cent AND dates ≤ 3 days apart, one-to-one (each bank line to at most one ledger entry).

**Matched: 10 pairs**

| bank line | ledger id | amount | day gap |
|---|---|---|---|
| 1 | L-01 | -1500.00 | 0 |
| 2 | L-02 | 4200.00 | 2 |
| 3 | L-03 | -312.45 | 1 |
| 4 | L-04 | -89.90 | 0 |
| 5 | L-05 | 2750.00 | 1 |
| 6 | L-06 | -6100.00 | 0 |
| 7 | L-07 | -49.00 | 1 |
| 8 | L-08 | -49.00 | 2 |
| 9 | L-09 | -230.00 | 2 |
| 10 | L-10 | 1800.00 | 1 |

The two identical -49.00 subscriptions are paired off in order (7→L-07, 8→L-0…
```

</details>

### Work: Analyst workday

#### Month-end revenue close — PASS

- **Goal:** The month-end close for August 2026 is due. Everything you need is in {workspace}. Follow README.md exactly and write the two output files there.
- **Tools really called:** `list_files`, `read_file`, `read_file`, `read_file`, `execute_python`, `execute_python`, `execute_python`, `write_file`, `write_file`, `read_file`, `read_file`
- **Time / tokens / cost:** 96.9s · 65,546 · $0.0086 (≈ ₹0.76)
- **Execution:** `2b4d01b8-b85b-40d8-969c-9f2c8647a83f` (open on /runs)

| Grader | Passed | Detail |
|---|---|---|
| `file_exists` | ✅ |  |
| `csv_rows` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `file_number` | ✅ |  |
| `file_number` | ✅ |  |
| `file_number` | ✅ |  |
| `file_regex` | ✅ |  |
| `tool_used` | ✅ |  |

<details><summary>Answer excerpt</summary>

```text
Month-end close for August 2026 is done. Both files are written in `/work/month-end-close` and read back to confirm.

**`summary.csv`**

| region | paid_orders | net_usd |
|---|---|---|
| East | 13 | 1902.39 |
| North | 6 | 948.05 |
| South | 7 | 1156.94 |
| West | 10 | 1745.83 |

**`report.md`**
'''
Total net USD: 5753.21
Duplicates removed: 3
Rows skipped (blank amount): 2
Top region: East
'''

How the numbers came out, following README rules in order:
- **Duplicates:** 63 raw rows → 3 removed (A‑1021, A‑1042, A‑1045 repeated at the end), keeping first occurrences.
- **August filter:** 51 rows fell in August 2026 (both `YYYY-MM-DD` and `DD/MM/YYYY` parsed day-first; July and September orde…
```

</details>

#### Bank reconciliation — PASS

- **Goal:** Reconcile the August bank statement. The files and instructions are in {workspace} (see README.md).
- **Tools really called:** `list_files`, `read_file`, `read_file`, `read_file`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `execute_python`, `write_file`, `read_file`
- **Time / tokens / cost:** 82.2s · 57,904 · $0.0054 (≈ ₹0.48)
- **Execution:** `041e2c6d-517d-4a8b-bff1-ebce503d7678` (open on /runs)

| Grader | Passed | Detail |
|---|---|---|
| `file_exists` | ✅ |  |
| `csv_rows` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `csv_value` | ✅ |  |
| `tool_used` | ✅ |  |

<details><summary>Answer excerpt</summary>

```text
Reconciliation done. `unmatched.csv` is written at `/work/bank-recon/unmatched.csv`:

'''
source,ref,amount
bank,11,-410.00
bank,12,950.01
bank,13,-12.50
ledger,L-11,-410.00
ledger,L-12,950.00
'''

**How the matching went**

Rule applied: equal amounts to the cent, dates at most 3 days apart, one-to-one (each bank line matches at most one ledger entry and vice versa), descriptions ignored.

10 pairs matched:

| Bank line | Ledger id | Date gap |
|---|---|---|
| 1 (RENT AUG) | L-01 | 0 |
| 4 (STAPLES) | L-04 | 0 |
| 6 (PAYROLL RUN) | L-06 | 0 |
| 3 (AWS) | L-03 | 1 |
| 5 (GLOBEX LTD) | L-05 | 1 |
| 7 (SAAS SUBSCRIPTION) | L-07 | 1 |
| 10 (INITECH INC) | L-10 | 1 |
| 2 (ACME CORP PAYMENT) | L-…
```

</details>

## How to read this

- **PASS / FAIL** on a case is every grader agreeing / at least one disagreeing.
- **REVIEW** means the automatic graders could not decide; answer it on the Evals page and regenerate with `manage.py benchmark report`.
- **Skipped** suites need something this account does not have (usually Google connected). They are not failures.
- **ERROR** means the agent never answered (provider down, missing key, spend cap). It is an outage, not a wrong answer.
- **Tools really called** is what dispatched, from the run trace. A `MOCK` line in an answer is a proposal and never appears there.
- Rupee figures use an approximate rate of ₹88 per US dollar.
