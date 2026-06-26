"""
Pipeline Runner
---------------
Runs the full classification pipeline on a sample of target rows
and saves results to a local SQLite database.

Flow:
    Ingestion Agent → Retriever Agent → Analyst Agent → Decision Agent → SQLite

Usage:
    python pipeline_runner.py           # runs on 20 rows (default)
    python pipeline_runner.py --n 50    # runs on 50 rows
    python pipeline_runner.py --fresh   # re-fetches from DB before running
"""

import sys
import time
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from agents.ingestion_agent import IngestionAgent
from agents.retriever_agent  import RetrieverAgent
from agents.analyst_agent    import AnalystAgent
from agents.decision_agent   import DecisionAgent

DB_PATH = Path(__file__).parent / "results" / "classifications.db"


# ------------------------------------------------------------------
# SQLite setup
# ------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS classifications")
    conn.execute("""
        CREATE TABLE classifications (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            tc_id               TEXT,
            exec_id             TEXT,
            intrim_status       TEXT,
            failure_remarks     TEXT,
            module              TEXT,
            j_component         TEXT,
            category            TEXT,
            final_label         TEXT,
            confidence          REAL,
            decision            TEXT,
            flag_for_human      INTEGER,
            reasoning           TEXT,
            top_similarity      REAL,
            neighbor_labels     TEXT,
            classified_at       TEXT
        )
    """)
    conn.commit()
    print(f"[Runner] Results DB ready → {db_path}")
    return conn


def save_result(conn: sqlite3.Connection, row, verdict: dict, neighbors):
    top_sim       = float(neighbors["similarity_score"].max())
    neighbor_lbls = ", ".join(neighbors["AUTO_FAILURE_REASON"].tolist())

    conn.execute("""
        INSERT INTO classifications (
            tc_id, exec_id, intrim_status, failure_remarks, module, j_component,
            category, final_label, confidence, decision, flag_for_human,
            reasoning, top_similarity, neighbor_labels, classified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(row.get("TC_ID",           "") or ""),
        str(row.get("EXEC_ID",         "") or ""),
        str(row.get("INTRIM_STATUS",   "") or ""),
        str(row.get("FAILURE_REMARKS", "") or "")[:500],
        str(row.get("MODULE",          "") or ""),
        str(row.get("J_COMPONENT",     "") or ""),
        verdict["category"],
        verdict["final_label"],
        verdict["confidence"],
        verdict["decision"],
        1 if verdict["flag_for_human"] else 0,
        verdict["reasoning"],
        top_sim,
        neighbor_lbls,
        datetime.now().isoformat(),
    ))
    conn.commit()


# ------------------------------------------------------------------
# Main runner
# ------------------------------------------------------------------

def run(n: int = 20, fresh: bool = False):
    print("\n" + "="*60)
    print(f"  PIPELINE RUNNER — {n} rows")
    print("="*60)

    t_start = time.time()

    # --- Load agents ---
    ingestion = IngestionAgent(fresh=fresh)
    kb        = ingestion.get_knowledge_base()
    targets   = ingestion.get_target_rows()
    ingestion.close()

    retriever = RetrieverAgent(kb)
    analyst   = AnalystAgent()
    decision  = DecisionAgent()

    # --- Pick sample rows ---
    sample = targets.sample(n, random_state=42).reset_index(drop=True)
    print(f"\n[Runner] Processing {n} rows ...\n")

    # --- Init DB ---
    conn = init_db(DB_PATH)

    # --- Process ---
    results_summary = {"ACCEPT": 0, "REVIEW": 0, "REJECT": 0}
    label_summary   = {}

    for i, (_, row) in enumerate(sample.iterrows(), 1):
        t_row = time.time()

        tc_id  = row.get("TC_ID", "?")
        status = row.get("INTRIM_STATUS", "?")

        print(f"[{i:>2}/{n}]  TC_ID: {tc_id:<8}  STATUS: {status:<15}", end="  ", flush=True)

        # Step 1 — Retrieve
        neighbors = retriever.query(row, top_k=5)
        top_score = neighbors["similarity_score"].max()

        # Step 2 — Analyse
        analyst_result = analyst.analyze(row, neighbors)

        # Step 3 — Decide
        verdict = decision.decide(row, analyst_result)

        # Step 4 — Save
        save_result(conn, row, verdict, neighbors)

        # Track stats
        results_summary[verdict["decision"]] = results_summary.get(verdict["decision"], 0) + 1
        label = verdict["final_label"]
        label_summary[label] = label_summary.get(label, 0) + 1

        flag_icon = "⚑" if verdict["flag_for_human"] else " "
        print(f"→ {verdict['final_label']:<15}  conf: {verdict['confidence']:.2f}  "
              f"[{verdict['decision']}] {flag_icon}  ({time.time()-t_row:.0f}s)")

    conn.close()

    # --- Summary ---
    total_time = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"  DONE — {n} rows classified in {total_time/60:.1f} min")
    print(f"{'='*60}")
    print(f"\n  Decision breakdown:")
    for d, cnt in results_summary.items():
        print(f"    {d:<8} : {cnt}")
    print(f"\n  Label breakdown:")
    for lbl, cnt in sorted(label_summary.items(), key=lambda x: -x[1]):
        print(f"    {lbl:<18} : {cnt}")
    print(f"\n  Results saved to: {DB_PATH}")
    print(f"  Open in DBeaver → File → New Connection → SQLite → select the file above")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n",     type=int,  default=20,    help="Number of rows to classify")
    parser.add_argument("--fresh", action="store_true",      help="Force re-fetch from DB")
    args = parser.parse_args()
    run(n=args.n, fresh=args.fresh)
