# Decision: Multi-Agent System over Traditional ML

> Why we chose an LLM-based multi-agent pipeline instead of a trained ML classifier.

---

## Decision

Build a 3-agent pipeline (Retriever → Analyst → Decision Maker) using Claude via the Anthropic SDK, rather than training a traditional ML model (e.g. TF-IDF + Logistic Regression).

## Rejected Alternative

Train a supervised classifier on the 6,104 labeled rows using TF-IDF on USER_REMARKS + one-hot INTRIM_STATUS as features.

---

## Why

**The decisive case:** A row with `INTRIM_STATUS=BLOCKED` and USER_REMARKS saying *"retired due to ADINFRA-181030 change"* should be APP-CHANGE. A classifier trained on INTRIM_STATUS patterns would predict ENV-ISSUE (99.3% of BLOCKED rows are ENV-ISSUE). Only reading and reasoning about the remarks text resolves this correctly.

Beyond that single case:

| Concern | ML | Multi-Agent |
|---|---|---|
| Handles novel text patterns | Poor — unseen vocab breaks it | Good — LLM generalizes |
| Explains its decision | No | Yes — plain-English reasoning |
| Can retrieve similar past cases | No | Yes — via tool use |
| Needs retraining when patterns shift | Yes | No — update the prompt |
| Performance vs Sync distinction | Unlikely — both were labelled ENV-ISSUE | Yes — LLM can distinguish from remarks text |

The Performance/Sync split is particularly important: both sub-types lived under a single `ENV-ISSUE` label in historical data. A trained classifier would learn the combined category and never split it. An LLM reading remarks like *"passed on rerun"* vs *"sync issue while navigating"* can make the distinction on first use.

---

## Architecture

```
Input Row (INTRIM_STATUS + USER_REMARKS + metadata)
         │
         ▼
┌─────────────────────────────────┐
│  Agent 1: RETRIEVER             │
│  Semantic search over 6,104     │
│  labeled rows — find top-5      │
│  most similar past failures     │
└────────────┬────────────────────┘
             │ Similar cases + their known labels
             ▼
┌─────────────────────────────────┐
│  Agent 2: ANALYST               │
│  Applies domain rules           │
│  Interprets USER_REMARKS text   │
│  Weighs rules vs retrieved cases│
│  Produces: evidence + initial   │
│  label suggestion               │
└────────────┬────────────────────┘
             │ Evidence summary
             ▼
┌─────────────────────────────────┐
│  Agent 3: DECISION MAKER        │
│  Final arbitration              │
│  Assigns label + confidence     │
│  Writes plain-English reasoning │
└────────────┬────────────────────┘
             ▼
  { TC_ID, predicted_category, confidence, reasoning }
```

**Why 3 agents, not 1?**
- Retrieval is a tool call, not reasoning — separating it from the Analyst keeps each agent's role clear and debuggable
- The Analyst applying rules and the Decision Maker resolving conflicts mirrors how a human engineer investigates: gather evidence first, decide second
- Scales to parallel processing: Agent 1 can run across all rows concurrently in future batch runs

**Stack:** Claude Sonnet 4.6, Python, Anthropic SDK, sentence-transformers + cosine similarity for retrieval.

---

## Tradeoff Accepted

LLM inference costs more per row than a trained model. Acceptable at current scale (24 immediate rows; future runs likely in the hundreds). Revisit if volume reaches thousands of failures per run.
