# Roadmap — Current Status

> Single source of truth for what's done, what's next, and what's blocked.

---

## Status: Planning Phase — Ready to code once open questions are resolved

---

## Done

- [x] Explored TC_MASTER and HIS_EXEC_REPORT schema
- [x] Confirmed full 7-label taxonomy from manager slide (Performance + Sync split from ENV-ISSUE)
- [x] Extracted domain rules from manager conversation transcripts (both sessions)
- [x] Decided on multi-agent architecture over traditional ML
- [x] Identified the 24 unlabeled non-PASSED rows as the immediate classification target
- [x] Built `analysis.ipynb` for data exploration
- [x] Connected to live Oracle DB (`AUTOBOT_DEV` on `ny-oracle-ee11:1521`)
- [x] Confirmed FAILURE_REMARKS sparsity (59/13,624) is permanent — not a snapshot issue
- [x] Identified platform filter: use only Prisma/Ignitia EXEC_IDs; exclude 1P
- [x] Identified component filter: exclude rows where J_COMPONENT starts with "OP"
- [x] Confirmed DATA-ISSUE labels cannot be predicted from FAILURE_REMARKS (label assigned post-local-investigation, not from raw error text)

---

## Corrected Understanding (from 2026-06-24 session)

| What we assumed | What is actually true |
|---|---|
| 6,104 labeled rows are the training set | 6,104 rows need platform filtering first — usable subset is smaller |
| FAILURE_REMARKS will grow with live DB | FAILURE_REMARKS is permanently sparse — 59 rows is the real state |
| USER_REMARKS should not be used | USER_REMARKS is the only evidence for DATA-ISSUE and intermittent failure types |
| Agent 1 embeds USER_REMARKS | Agent 1 embeds FAILURE_REMARKS where available; USER_REMARKS as secondary signal |

---

## Next Steps (in order)

1. **Confirm platform filter scope** (blocking):
   - Which EXEC_IDs are Prisma vs 1P? Need to verify against DB or manager
   - How many of the 6,104 labeled rows survive the Prisma/Ignitia filter?

2. **Finalize remaining open questions with manager**:
   - Output destination: DB write-back to `AUTO_FAILURE_REASON`, CSV export, or both?
   - One-time script or continuous service triggered per regression run?
   - Confidence threshold for "Yet to be analyzed" fallback?

3. **Build Agent 1 — Retriever**
   - Filter labeled rows to Prisma/Ignitia only, exclude OP components
   - Embed FAILURE_REMARKS (where available) + USER_REMARKS as fallback
   - Input: new row → Output: top-5 similar labeled cases

4. **Build Agent 2 — Analyst**
   - Encode all domain rules as system prompt (including platform filter, DATA-ISSUE caveat)
   - Input: row fields + top-5 retrieved cases → Output: evidence summary + initial label

5. **Build Agent 3 — Decision Maker**
   - Input: evidence from Analyst → Output: final label + confidence score + reasoning
   - If confidence < threshold → output "Yet to be analyzed"

6. **Run on 24 unlabeled rows** and validate output with team

7. **ENV-ISSUE re-labeling pass** (post-validation)
   - Run classifier over 2,659 ENV-ISSUE rows to split into Performance vs Sync
   - Human spot-check sample before accepting

---

## Open Questions (blocking or important)

| # | Question | Owner | Status |
|---|---|---|---|
| 1 | Which EXEC_IDs are Prisma vs 1P? How many labeled rows survive the filter? | Manager/DB | Open — blocking |
| 2 | Output destination — DB write-back, CSV, or both? | Manager | Open |
| 3 | One-time script or continuous service per regression run? | Manager | Open |
| 4 | Confidence threshold for "Yet to be analyzed"? | Team | Open |
| 5 | Who owns the ENV-ISSUE → Perf/Sync re-labeling pass? | Team | Open |
| 6 | DATA-ISSUE for new failures with no remarks — auto-flag as "Yet to be analyzed"? | Team | Open |

---

## Key References

- Session: [2026-06-23 Requirements & Design](../sessions/2026-06-23-requirements-and-design.md)
- Session: [2026-06-24 Data Analysis & Training Filters](../sessions/2026-06-24-data-analysis-and-training-filters.md)
- Decision: [Multi-Agent over ML](../decisions/multi-agent-over-ml.md)
