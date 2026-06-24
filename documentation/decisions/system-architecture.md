# System Architecture — Regression Failure Classifier

> What we are building, why, and how the pieces fit together.

---

## The Goal

Automatically fill in `AUTO_FAILURE_REASON` for **226,080 unclassified test failures** in `HIS_EXEC_REPORT` — replacing what automation engineers currently do manually after every regression run.

---

## What Kind of System Is This?

**A RAG Pipeline + Multi-Agent Classifier.**

Two layers working together:

### Layer 1 — RAG (Retrieval Augmented Generation)
Use past classified failures as a knowledge base. For every new unclassified failure, find the most similar past failures and use their known labels as evidence.

```
New failure row
      ↓
Semantic search over 62,144 labeled rows
      ↓
Top-N similar past failures with known labels
```

### Layer 2 — 3 Agents Make the Decision
Three agents work in sequence. Each has one job.

```
Agent 1 — RETRIEVER
  "Find the 5 most similar past failures to this one"
  Uses: FAILURE_REMARKS text + INTRIM_STATUS
           ↓
Agent 2 — ANALYST
  "Here's the new row + 5 similar examples.
   Apply domain rules. What does the evidence suggest?"
  Uses: domain rules + retrieved examples + all row fields
           ↓
Agent 3 — DECISION MAKER
  "Make the final call."
  Output: label + confidence score + plain-English reasoning
  If unsure → "Yet to be analyzed"
```

---

## Full System Flow

```
┌─────────────────────────────────────────────────────────┐
│                  Live Oracle DB                         │
│           HIS_EXEC_REPORT (785,166 rows)                │
│           TC_MASTER        (33,896 rows)                │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Ingestion Agent                            │
│  • Connects to DB                                       │
│  • Joins TC_MASTER + HIS_EXEC_REPORT → 785,166 rows     │
│  • Filters to Knowledge Base (62,144 labeled rows)      │
│  • Filters to Target Rows (226,080 unlabeled rows)      │
└────────────┬────────────────────────┬───────────────────┘
             │                        │
             ▼                        ▼
  ┌──────────────────┐     ┌──────────────────────┐
  │  Knowledge Base  │     │    Target Rows       │
  │  62,144 rows     │     │    226,080 rows      │
  │  (labeled)       │     │    (unlabeled)       │
  └────────┬─────────┘     └──────────┬───────────┘
           │                          │
           │    For each target row:  │
           │◄─────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────┐
│              Agent 1 — RETRIEVER                        │
│  Embeds FAILURE_REMARKS (primary) / INTRIM_STATUS       │
│  Searches knowledge base using cosine similarity        │
│  Returns top-5 most similar labeled rows                │
└────────────────────────┬────────────────────────────────┘
                         │ Top-5 similar past failures
                         ▼
┌─────────────────────────────────────────────────────────┐
│              Agent 2 — ANALYST                          │
│  Reads: target row + top-5 retrieved examples           │
│  Applies domain rules:                                  │
│    - Java throwable → SCRIPT-ISSUE                      │
│    - INTRIM_STATUS=FAILED → APP-ISSUE                   │
│    - INTRIM_STATUS=OOS → APP-CHANGE                     │
│    - "passed on rerun" → Performance Issue              │
│    - Jira change ticket → APP-CHANGE                    │
│    - etc.                                               │
│  Output: evidence summary + suggested label             │
└────────────────────────┬────────────────────────────────┘
                         │ Evidence + suggested label
                         ▼
┌─────────────────────────────────────────────────────────┐
│              Agent 3 — DECISION MAKER                   │
│  Weighs all evidence                                    │
│  Makes final classification                             │
│  Output:                                                │
│    - predicted AUTO_FAILURE_REASON                      │
│    - confidence score (0.0 – 1.0)                       │
│    - plain-English reasoning                            │
│    - "Yet to be analyzed" if confidence too low         │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                      UI                                 │
│  Displays predictions per TC_ID / JIRA_ID               │
│  Shows confidence + reasoning                           │
│  Allows engineer to accept / override                   │
│  (Built separately by Claude Design)                    │
└─────────────────────────────────────────────────────────┘
```

---

## Data at a Glance (Live DB — as of 2026-06-24)

| Metric | Count |
|---|---|
| Total rows in HIS_EXEC_REPORT | 785,166 |
| Knowledge base (labeled, filtered) | 62,144 |
| Target rows to classify | 226,080 |
| Rows with FAILURE_REMARKS (live DB) | 191,912 (24.4%) |
| Labeled rows with FAILURE_REMARKS | 59,142 |

---

## Knowledge Base — Label Distribution

| Label | Count |
|---|---|
| ENV-ISSUE | 41,916 |
| SCRIPT-ISSUE | 16,040 |
| APP-ISSUE | 14,198 |
| APP-CHANGE | 11,181 |
| DATA-ISSUE | 11,136 |

---

## Target Rows — INTRIM_STATUS Breakdown

| INTRIM_STATUS | Count | Expected label |
|---|---|---|
| MAINTAIN | 72,078 | SCRIPT-ISSUE / DATA-ISSUE |
| FAILED | 48,877 | APP-ISSUE |
| OOS | 44,668 | APP-CHANGE |
| SCHEDULED | 32,433 | TBD |
| NOT-STARTED | 15,596 | TBD |
| IN-PROGRESS | 6,756 | TBD |
| MAINTAINED | 2,111 | ENV-ISSUE |
| BLOCKED | 1,771 | ENV-ISSUE |

---

## Classification Taxonomy (Output Labels)

| Group | Label | DB value |
|---|---|---|
| Product bug | App-Issue (Defect) | APP-ISSUE |
| Product bug | App-Change | APP-CHANGE |
| Auto bug | Data Issue | DATA-ISSUE |
| Auto bug | Script Issue | SCRIPT-ISSUE |
| System issue | Performance Issue | ENV-ISSUE (split from Sync) |
| System issue | Sync Issue | ENV-ISSUE (split from Perf) |
| To investigate | Yet to be analyzed | — |

---

## Key Design Decisions

- **RAG over pure ML** — past failures serve as examples, not training data for a model. No retraining needed when patterns change, just update the knowledge base.
- **3 agents, not 1** — retrieval, analysis, and decision-making are separate concerns. Each is independently testable and replaceable.
- **FAILURE_REMARKS as primary text signal** — machine-generated, objective. Falls back to INTRIM_STATUS rules when empty.
- **"Yet to be analyzed" is a valid output** — agents must not be forced to guess. Low-confidence rows are flagged for human review, not silently mislabelled.
- **Ingestion Agent owns all DB access** — single source of data for all downstream agents. Filtering rules (exclude OP components, exclude 1P) are enforced here, not scattered across agents.

---

## What Needs to Happen Before Agents Are Built

1. **Normalize dirty labels** in knowledge base — e.g. `DatA-ISSUE → DATA-ISSUE`, `APP-CHNAGE → APP-CHANGE`, `Sync Issue → ENV-ISSUE`
2. **Define rules for new INTRIM_STATUS values** — `NOT-STARTED`, `IN-PROGRESS`, `SCHEDULED`, `NOT-RUN`, `IN-INVESTIGATION` were not in the original domain rules
3. **Build embeddings index** — embed FAILURE_REMARKS for all 59,142 labeled rows that have it; this becomes the retrieval index for Agent 1
