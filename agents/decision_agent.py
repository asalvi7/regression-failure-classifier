"""
Decision Agent (Agent 3)
------------------------
Takes the Analyst's output and makes the final classification decision.

Its job:
  1. Review the suggested label + confidence from Agent 2
  2. Apply a confidence gate — if confidence is too low, output YET-TO-ANALYZE
  3. Cross-check the label against INTRIM_STATUS rules as a sanity check
  4. Return the final verdict: label + confidence + reasoning + flag

Confidence thresholds:
  >= 0.85  → ACCEPT  (high confidence, assign label)
  0.60–0.84 → REVIEW  (medium confidence, assign label but flag for spot-check)
  < 0.60   → REJECT  → YET-TO-ANALYZE (flag for human review)

Output:
  {
    "final_label"    : "APP-ISSUE",
    "confidence"     : 0.92,
    "decision"       : "ACCEPT",        # ACCEPT / REVIEW / REJECT
    "reasoning"      : "...",
    "flag_for_human" : false,
    "tc_id"          : 32394
  }

Usage (import):
    from agents.decision_agent import DecisionAgent

    decision = DecisionAgent()
    verdict  = decision.decide(target_row, analyst_result)

Usage (standalone):
    python agents/decision_agent.py     # runs full pipeline on 3 sample rows
"""

import sys
import time
import pandas as pd
from pathlib import Path

# Thresholds
ACCEPT_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60

# INTRIM_STATUS → expected label (hard rules from domain knowledge)
INTRIM_RULES = {
    "OOS"         : "APP-CHANGE",
    "FAILED"      : "APP-ISSUE",
    "MAINTAIN"    : "SCRIPT-ISSUE",   # could also be DATA-ISSUE
    "MAINTAINED"  : "SYNC-ISSUE",     # fix was applied and passed → Sync Issue
    "BLOCKED"     : "PERF-ISSUE",     # couldn't run → environment/perf issue
}

# Labels where INTRIM_STATUS rule is a strong override signal
HARD_RULE_STATUSES = {"OOS", "MAINTAINED", "BLOCKED"}

# Category map
CATEGORY_MAP = {
    "APP-ISSUE"     : "Product Bug",
    "APP-CHANGE"    : "Product Bug",
    "DATA-ISSUE"    : "Auto Bug",
    "SCRIPT-ISSUE"  : "Auto Bug",
    "PERF-ISSUE"    : "System Issue",
    "SYNC-ISSUE"    : "System Issue",
    "YET-TO-ANALYZE": "To Investigate",
}


class DecisionAgent:

    def decide(self, target_row: pd.Series, analyst_result: dict) -> dict:
        """
        Make the final classification decision.

        Parameters
        ----------
        target_row      : the raw target row (needs INTRIM_STATUS, TC_ID)
        analyst_result  : output dict from AnalystAgent.analyze()

        Returns
        -------
        dict with final_label, confidence, decision, reasoning, flag_for_human, tc_id
        """
        suggested  = analyst_result.get("suggested_label", "YET-TO-ANALYZE")
        confidence = float(analyst_result.get("confidence", 0.0))
        reasoning  = analyst_result.get("reasoning", "")
        evidence   = analyst_result.get("evidence", [])

        status     = str(target_row.get("INTRIM_STATUS", "") or "").strip().upper()
        tc_id      = target_row.get("TC_ID", "UNKNOWN")

        # --- Step 1: Confidence gate ---
        if confidence < REVIEW_THRESHOLD:
            return self._verdict(
                tc_id        = tc_id,
                final_label  = "YET-TO-ANALYZE",
                confidence   = confidence,
                decision     = "REJECT",
                reasoning    = (f"Confidence {confidence:.2f} is below threshold {REVIEW_THRESHOLD}. "
                                f"Analyst suggested {suggested}. Flagged for human review."),
                flag         = True,
            )

        # --- Step 2: Hard INTRIM_STATUS override ---
        # For OOS / MAINTAINED / BLOCKED, the status is near-certain signal.
        # If Analyst disagrees, override and explain why.
        if status in HARD_RULE_STATUSES:
            expected = INTRIM_RULES[status]
            if suggested != expected:
                override_confidence = max(confidence, 0.88)
                return self._verdict(
                    tc_id        = tc_id,
                    final_label  = expected,
                    confidence   = override_confidence,
                    decision     = "ACCEPT",
                    reasoning    = (f"INTRIM_STATUS={status} is a hard rule → {expected}. "
                                    f"Analyst suggested {suggested} but domain rule overrides. "
                                    f"{reasoning}"),
                    flag         = False,
                )

        # --- Step 3: Accept / Review based on confidence ---
        if confidence >= ACCEPT_THRESHOLD:
            decision     = "ACCEPT"
            flag         = False
            final_label  = suggested
        else:
            decision     = "REVIEW"
            flag         = True    # medium confidence → spot-check recommended
            final_label  = suggested

        return self._verdict(
            tc_id       = tc_id,
            final_label = final_label,
            confidence  = confidence,
            decision    = decision,
            reasoning   = reasoning,
            flag        = flag,
        )

    def _verdict(self, tc_id, final_label, confidence, decision, reasoning, flag) -> dict:
        return {
            "tc_id"          : tc_id,
            "category"       : CATEGORY_MAP.get(final_label, "To Investigate"),
            "final_label"    : final_label,
            "confidence"     : round(confidence, 3),
            "decision"       : decision,      # ACCEPT / REVIEW / REJECT
            "reasoning"      : reasoning,
            "flag_for_human" : flag,
        }


# ------------------------------------------------------------------
# Standalone run — full pipeline on 3 sample target rows
# ------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from agents.ingestion_agent import IngestionAgent
    from agents.retriever_agent import RetrieverAgent
    from agents.analyst_agent   import AnalystAgent

    print("="*60)
    print("  FULL PIPELINE TEST — 3 target rows")
    print("  Ingestion → Retriever → Analyst → Decision")
    print("="*60)

    t_total = time.time()

    # Load data
    ingestion = IngestionAgent()
    kb        = ingestion.get_knowledge_base()
    targets   = ingestion.get_target_rows()
    ingestion.close()

    # Load agents
    retriever = RetrieverAgent(kb)
    analyst   = AnalystAgent()
    decision  = DecisionAgent()

    sample = targets.sample(3, random_state=42)

    for i, (_, row) in enumerate(sample.iterrows(), 1):
        print(f"\n{'─'*60}")
        print(f"  ROW {i}  |  TC_ID: {row.get('TC_ID')}  |  "
              f"STATUS: {row.get('INTRIM_STATUS')}")
        remarks = str(row.get("FAILURE_REMARKS", "") or "")
        if remarks:
            print(f"  REMARKS : {remarks[:100]}")
        print(f"{'─'*60}")

        # Step 1 — Retrieve
        t1 = time.time()
        neighbors = retriever.query(row, top_k=5)
        top_score = neighbors["similarity_score"].max()
        print(f"  [Retriever]  top score: {top_score:.3f}  "
              f"| labels: {neighbors['AUTO_FAILURE_REASON'].value_counts().to_dict()}")

        # Step 2 — Analyse
        analyst_result = analyst.analyze(row, neighbors)
        print(f"  [Analyst]    {analyst_result['suggested_label']}  "
              f"confidence: {analyst_result['confidence']:.2f}  "
              f"({analyst_result.get('_elapsed_s', '?')}s)")

        # Step 3 — Decide
        verdict = decision.decide(row, analyst_result)

        print(f"\n  FINAL VERDICT:")
        print(f"    Label        : {verdict['final_label']}")
        print(f"    Confidence   : {verdict['confidence']:.2f}")
        print(f"    Decision     : {verdict['decision']}")
        print(f"    Flag human?  : {verdict['flag_for_human']}")
        print(f"    Reasoning    : {verdict['reasoning']}")
        print(f"    Total time   : {time.time()-t1:.1f}s")

    print(f"\n{'='*60}")
    print(f"  Pipeline complete.  Wall-clock: {(time.time()-t_total)/60:.1f} min")
    print(f"{'='*60}")
