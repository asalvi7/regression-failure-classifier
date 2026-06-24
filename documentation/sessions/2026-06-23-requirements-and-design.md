# 2026-06-23 — Requirements Gathering & System Design

> What we learned about the data, the domain, and how the system should work.

---

### Context

The team runs large regression suites via Jenkins/Docker. When tests fail, automation engineers manually investigate each failure and assign a root-cause category (`AUTO_FAILURE_REASON`) in the DB. This is slow, repetitive, and creates a backlog. Goal: automate that investigation step using an AI agent system.

This session covered: exploring the two DB tables, extracting domain rules from manager conversation transcripts, clarifying the full classification taxonomy from a slide shared by the manager, and settling on a multi-agent architecture.

---

### What We Learned About the Data

**TC_MASTER** (~33,890 rows) — one row per Jira test case (`TC_ID`). A single automation script (`AUTOMATED_TC_ID`) can map to multiple `TC_ID`s — they are not interchangeable.

**HIS_EXEC_REPORT** (~13,624 rows) — one row per test execution. The columns that matter:

| Column | Role |
|---|---|
| `INTRIM_STATUS` | Authoritative status. Use this, NOT `FINAL_STATUS` |
| `AUTO_FAILURE_REASON` | The label — filled by engineer post-investigation; blank = unlabeled |
| `USER_REMARKS` | Free-text by engineer — 100% coverage on all labeled rows; richest text feature |
| `FAILURE_REMARKS` | Framework-generated — nearly empty (59/13,624 rows); low utility |

**Data split:**
- Labeled: 6,104 rows (training/few-shot reference)
- Unlabeled non-PASSED: 24 rows (immediate classification target)
- Unlabeled PASSED: 7,496 rows — passed during execution, no failure to classify

**Label distribution in labeled set:** ENV-ISSUE 2,659 · APP-ISSUE 1,765 · APP-CHANGE 1,012 · SCRIPT-ISSUE 441 · DATA-ISSUE 227

---

### Classification Taxonomy (full — from manager slide)

The DB currently stores 5 values but the intended taxonomy has 7 labels across 4 groups. ENV-ISSUE conflates two distinct failure types:

| Group | Label | Current DB value |
|---|---|---|
| Product bug | App-Issue (Defect) | APP-ISSUE |
| Product bug | App-Change | APP-CHANGE |
| Auto bug | Data Issue | DATA-ISSUE |
| Auto bug | Script Issue | SCRIPT-ISSUE |
| System issue | **Performance Issue** | ENV-ISSUE ← combined |
| System issue | **Sync Issue** | ENV-ISSUE ← combined |
| To investigate | Yet to be analyzed | (unlabeled) |

The 2,659 ENV-ISSUE rows need a secondary pass to split Performance from Sync. Until that's done, ENV-ISSUE labeled rows are ambiguous training data for that dimension.

---

### Domain Rules Extracted from Manager Transcript

- `FAILURE_REMARKS` contains Java exception/throwable → **SCRIPT-ISSUE**
- `INTRIM_STATUS = FAILED` (no other signals) → **APP-ISSUE** (99.5% historical accuracy)
- `INTRIM_STATUS = BLOCKED` (no change-related remarks) → **Performance Issue** (99.3% historical accuracy)
- `INTRIM_STATUS = OOS` → **APP-CHANGE** (~81%)
- Remarks: "passed on rerun" / "passed locally" / "pass from QB" / "slowness" → **Performance Issue**
- Remarks: "sync issue" + `MAINTAIN` status → **Sync Issue**
- Remarks reference a Jira change ticket (ADINFRA-XXXXXX) → **APP-CHANGE**
- **Never use `FINAL_STATUS`** — manager explicitly flagged it as unreliable

**INTRIM_STATUS semantics:**
- `MAINTAINED` — fix applied, re-executed, passed. Done.
- `MAINTAIN` — direction given, script still failing. In-progress.
- `BLOCKED` — could not execute
- `OOS` — out of scope (usually retired due to an app change)

**DATA-ISSUE caveat:** Some rows were manually entered and don't reflect automation-detectable patterns. Use only report-portal execution IDs as training data for this category.

---

### The Decisive Insight: Status Alone Is Not Enough

A row with `INTRIM_STATUS=BLOCKED` and remarks saying *"retired due to ADINFRA-181030 change"* should be APP-CHANGE, not ENV-ISSUE. The status alone gets it wrong; the remarks carry the real signal. This is why the classifier needs to reason over text, not just match on status — and why we chose agents over a rule engine or ML.

---

### Tradeoffs

- Splitting ENV-ISSUE is correct but requires re-labeling ~2,659 existing rows; until that pass is done, historical data is partially dirty for Performance vs Sync
- No FAILURE_REMARKS signal: newly arrived failures with no USER_REMARKS yet must default to rule-based classification only (no retrieval, no text reasoning)
- LLM costs grow with volume; acceptable now, worth revisiting if runs produce hundreds of failures regularly
