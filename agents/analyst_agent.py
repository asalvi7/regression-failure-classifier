"""
Analyst Agent (Agent 2)
-----------------------
Takes one unlabeled target row + top-5 retrieved neighbors from the
Retriever Agent and produces a structured evidence summary + suggested label.

Uses a local Llama 3.1 model via Ollama — no API key, no cost, fully local.

Two reasoning paths (based on retrieval confidence):
  HIGH-SIGNAL  (similarity >= 0.80, FAILURE_REMARKS present)
    → Weight neighbors heavily. Confirm with text + INTRIM_STATUS.
  LOW-SIGNAL   (similarity < 0.80 or no FAILURE_REMARKS)
    → Rely on INTRIM_STATUS domain rules. Treat neighbors as weak context.

Output (JSON):
  {
    "suggested_label" : "APP-ISSUE",     # one of 5 valid labels or YET-TO-ANALYZE
    "confidence"      : 0.87,            # 0.0 – 1.0
    "reasoning"       : "...",           # plain English, 2-3 sentences
    "evidence"        : ["...", "..."]   # key signals used
  }

Usage (import):
    from agents.analyst_agent import AnalystAgent

    analyst  = AnalystAgent()
    result   = analyst.analyze(target_row, neighbors_df)

Usage (standalone):
    python agents/analyst_agent.py          # runs on 3 sample target rows
"""

import json
import sys
import time
import re
import ollama
import pandas as pd
from pathlib import Path

MODEL        = "llama3.1"
HIGH_SIGNAL_THRESHOLD = 0.80   # similarity score above which neighbors are trusted

VALID_LABELS = {
    "APP-ISSUE",
    "APP-CHANGE",
    "SCRIPT-ISSUE",
    "DATA-ISSUE",
    "PERF-ISSUE",
    "SYNC-ISSUE",
    "YET-TO-ANALYZE",
}

CATEGORY_MAP = {
    "APP-ISSUE"     : "Product Bug",
    "APP-CHANGE"    : "Product Bug",
    "DATA-ISSUE"    : "Auto Bug",
    "SCRIPT-ISSUE"  : "Auto Bug",
    "PERF-ISSUE"    : "System Issue",
    "SYNC-ISSUE"    : "System Issue",
    "YET-TO-ANALYZE": "To Investigate",
}

SYSTEM_PROMPT = """You are an expert test failure classifier for a software regression testing system.

Your job: given a failed test case and similar past failures, assign one of these labels:

TAXONOMY:
  Product Bug:
    APP-ISSUE   — The product/application code has a bug. Test fails due to a defect in the app.
    APP-CHANGE  — The application behavior changed intentionally (new feature, UI change, config change).
                  Test is now out of date. INTRIM_STATUS is often OOS (Out of Scope).

  Auto Bug:
    DATA-ISSUE    — The test data used by the test is wrong or stale. Test passes when data is fixed.
                    Often identified only after local investigation; NOT obvious from error text.
    SCRIPT-ISSUE  — The automation test script itself has a bug or is unmaintained.
                    Java/Python exceptions in the test code, wrong selectors, missing waits.

  System Issue:
    PERF-ISSUE  — Intermittent failure. Test passes on rerun or locally. Slowness/timing issue.
                  Signals: "passed on rerun", "passed locally", "slowness", "intermittent".
    SYNC-ISSUE  — Definitive failure due to a sync/environment mismatch. Fix was applied.
                  Signals: INTRIM_STATUS=MAINTAINED, "sync issue" in remarks.

  To Investigate:
    YET-TO-ANALYZE — Not enough signal to classify confidently. Flag for human review.

DOMAIN RULES (apply these first before looking at neighbors):
  1. INTRIM_STATUS = OOS        → strong signal for APP-CHANGE
  2. INTRIM_STATUS = FAILED     → strong signal for APP-ISSUE (but check FAILURE_REMARKS)
  3. INTRIM_STATUS = MAINTAIN   → strong signal for SCRIPT-ISSUE or DATA-ISSUE
  4. INTRIM_STATUS = MAINTAINED → strong signal for SYNC-ISSUE (fix was applied and passed)
  5. INTRIM_STATUS = BLOCKED    → strong signal for PERF-ISSUE
  6. Java exception (NullPointerException, ClassCastException, etc.) in FAILURE_REMARKS
     AND INTRIM_STATUS != MAINTAIN → usually SCRIPT-ISSUE
  7. "Row mismatch", "expected vs actual" in FAILURE_REMARKS → DATA-ISSUE
  8. "passed on rerun", "passed locally", "passed manually" in USER_REMARKS → PERF-ISSUE
  9. "sync" in USER_REMARKS or INTRIM_STATUS=MAINTAINED → SYNC-ISSUE
  10. Jira ticket referenced that describes a change → APP-CHANGE
  11. Assertion failure comparing UI/output data → DATA-ISSUE or APP-CHANGE (check neighbors)

IMPORTANT CAVEATS:
  - DATA-ISSUE is often NOT visible in FAILURE_REMARKS (the error looks like SCRIPT-ISSUE).
    If USER_REMARKS says the data was fixed, trust that over the error text.
  - PERF-ISSUE vs SYNC-ISSUE: if it passes on rerun → PERF-ISSUE. If fix was applied → SYNC-ISSUE.
  - If similarity scores for neighbors are below 0.80, treat them as weak context only.
    Rely on domain rules and INTRIM_STATUS instead.
  - Never force a label. If genuinely unsure, output YET-TO-ANALYZE.

OUTPUT FORMAT (strict JSON, no extra text):
{
  "suggested_label": "<one of the 7 labels>",
  "category": "<Product Bug | Auto Bug | System Issue | To Investigate>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentences explaining the classification>",
  "evidence": ["<signal 1>", "<signal 2>", "<signal 3>"]
}"""


def _format_row(row: pd.Series, label: str = None) -> str:
    """Format a single row as readable text for the prompt."""
    lines = []
    if label:
        lines.append(f"Label: {label}")
    for field in ["TC_ID", "INTRIM_STATUS", "FAILURE_REMARKS", "USER_REMARKS",
                  "MODULE", "J_COMPONENT", "FUNC_AREA", "JIRA_ID"]:
        val = str(row.get(field, "") or "").strip()
        if val and val.lower() != "none":
            lines.append(f"{field}: {val[:200]}")
    return "\n".join(lines) if lines else "No data available"


def _build_user_prompt(target_row: pd.Series, neighbors: pd.DataFrame) -> str:
    """Build the user-turn prompt from target row + retrieved neighbors."""
    top_score    = float(neighbors["similarity_score"].max()) if len(neighbors) > 0 else 0.0
    has_remarks  = bool(str(target_row.get("FAILURE_REMARKS", "") or "").strip())
    high_signal  = top_score >= HIGH_SIGNAL_THRESHOLD and has_remarks

    signal_note = (
        f"HIGH-SIGNAL retrieval (top score: {top_score:.3f}) — neighbors are strong evidence."
        if high_signal else
        f"LOW-SIGNAL retrieval (top score: {top_score:.3f}) — rely on domain rules and INTRIM_STATUS."
    )

    neighbor_text = ""
    for i, (_, n) in enumerate(neighbors.iterrows(), 1):
        score = n.get("similarity_score", 0)
        neighbor_text += f"\n  Neighbor {i} (similarity: {score:.3f}):\n"
        neighbor_text += "    " + _format_row(n, label=n.get("AUTO_FAILURE_REASON", "UNKNOWN")).replace("\n", "\n    ")

    prompt = f"""TARGET ROW TO CLASSIFY:
{_format_row(target_row)}

RETRIEVAL NOTE: {signal_note}

TOP-5 SIMILAR PAST FAILURES FROM KNOWLEDGE BASE:
{neighbor_text}

Classify the target row. Output ONLY valid JSON."""

    return prompt


class AnalystAgent:

    def __init__(self, model: str = MODEL):
        self.model = model
        self._verify_model()

    def _verify_model(self):
        try:
            models = [m.model for m in ollama.list().models]
            if not any(self.model in m for m in models):
                raise RuntimeError(
                    f"Model '{self.model}' not found in Ollama. "
                    f"Run: ollama pull {self.model}"
                )
            print(f"[AnalystAgent] Model ready: {self.model}")
        except Exception as e:
            raise RuntimeError(f"[AnalystAgent] Cannot connect to Ollama: {e}")

    def analyze(self, target_row: pd.Series, neighbors: pd.DataFrame) -> dict:
        """
        Classify one target row using the retrieved neighbors as context.

        Returns dict with: suggested_label, confidence, reasoning, evidence
        """
        user_prompt = _build_user_prompt(target_row, neighbors)

        t = time.time()
        response = ollama.chat(
            model=self.model,
            messages=[
                {"role": "system",  "content": SYSTEM_PROMPT},
                {"role": "user",    "content": user_prompt},
            ],
            options={"temperature": 0.1},   # low temp for consistent classification
        )
        elapsed = time.time() - t

        raw = response.message.content.strip()
        result = self._parse_response(raw)
        result["_elapsed_s"]  = round(elapsed, 2)
        result["_model"]      = self.model
        result["_raw"]        = raw
        return result

    def _parse_response(self, raw: str) -> dict:
        """Extract JSON from model output — handles markdown code fences."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        # Find the JSON object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {
                "suggested_label" : "YET-TO-ANALYZE",
                "confidence"      : 0.0,
                "reasoning"       : "Model did not return valid JSON.",
                "evidence"        : [],
                "_parse_error"    : raw[:200],
            }

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as e:
            return {
                "suggested_label" : "YET-TO-ANALYZE",
                "category"        : "To Investigate",
                "confidence"      : 0.0,
                "reasoning"       : f"JSON parse error: {e}",
                "evidence"        : [],
                "_parse_error"    : raw[:200],
            }

        # Validate label
        label = str(data.get("suggested_label", "YET-TO-ANALYZE")).upper().strip()
        if label not in VALID_LABELS:
            label = "YET-TO-ANALYZE"

        return {
            "suggested_label" : label,
            "category"        : CATEGORY_MAP.get(label, "To Investigate"),
            "confidence"      : float(data.get("confidence", 0.0)),
            "reasoning"       : str(data.get("reasoning", "")),
            "evidence"        : data.get("evidence", []),
        }


# ------------------------------------------------------------------
# Standalone run — test on 3 sample target rows
# ------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from agents.ingestion_agent import IngestionAgent
    from agents.retriever_agent import RetrieverAgent

    print("="*60)
    print("  ANALYST AGENT — TEST RUN")
    print("="*60)

    ingestion = IngestionAgent()
    kb        = ingestion.get_knowledge_base()
    targets   = ingestion.get_target_rows()
    ingestion.close()

    retriever = RetrieverAgent(kb)
    analyst   = AnalystAgent()

    sample = targets.sample(3, random_state=42)

    for i, (_, row) in enumerate(sample.iterrows(), 1):
        print(f"\n{'='*60}")
        print(f"  TARGET {i}")
        print(f"{'='*60}")
        print(f"  TC_ID         : {row.get('TC_ID')}")
        print(f"  INTRIM_STATUS : {row.get('INTRIM_STATUS')}")
        remarks = str(row.get("FAILURE_REMARKS", "") or "")
        print(f"  FAILURE_RMKS  : {remarks[:100]}")

        neighbors = retriever.query(row, top_k=5)
        result    = analyst.analyze(row, neighbors)

        print(f"\n  ANALYST OUTPUT:")
        print(f"    Label      : {result['suggested_label']}")
        print(f"    Confidence : {result['confidence']:.2f}")
        print(f"    Time       : {result.get('_elapsed_s', '?')}s")
        print(f"    Reasoning  : {result['reasoning']}")
        print(f"    Evidence   :")
        for e in result.get("evidence", []):
            print(f"      - {e}")

    print(f"\n{'='*60}")
    print("  Done.")
    print(f"{'='*60}")
