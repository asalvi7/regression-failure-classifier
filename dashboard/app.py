"""
Dashboard Backend — Flask API
------------------------------
Serves the UI and exposes classification results from SQLite.

Endpoints:
  GET /                          → dashboard HTML
  GET /api/builds                → all exec_ids with summary stats
  GET /api/builds/<exec_id>      → all test cases for one run

Run:
  python3 dashboard/app.py
  Open: http://localhost:5000
"""

import sqlite3
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, abort

DB_PATH       = Path(__file__).parent.parent / "results" / "classifications.db"
DASHBOARD_DIR = Path(__file__).parent

app = Flask(__name__)

CAT_MAP = {
    "Product Bug":    "product",
    "Auto Bug":       "auto",
    "System Issue":   "system",
    "To Investigate": "investigate",
}

RC_CAT = {
    "APP-ISSUE":      "product",
    "APP-CHANGE":     "product",
    "DATA-ISSUE":     "auto",
    "SCRIPT-ISSUE":   "auto",
    "PERF-ISSUE":     "system",
    "SYNC-ISSUE":     "system",
    "YET-TO-ANALYZE": "investigate",
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Serve static dashboard files ───────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")

@app.route("/uploads/MO_Logo.png")
def logo():
    return send_from_directory(DASHBOARD_DIR.parent / "agents" / "Logo", "MO_Logo.png")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(DASHBOARD_DIR, filename)


# ─── /api/builds ─────────────────────────────────────────────────────────────

@app.route("/api/builds")
def builds():
    if not DB_PATH.exists():
        return jsonify([])

    conn = get_db()
    rows = conn.execute("""
        SELECT
            exec_id,
            COUNT(*)                                      AS total,
            SUM(flag_for_human)                           AS review_needed,
            MIN(classified_at)                            AS first_run,
            category,
            final_label,
            decision
        FROM classifications
        GROUP BY exec_id
        ORDER BY MIN(classified_at) DESC
    """).fetchall()

    # Build per-exec_id summaries with category breakdown
    from collections import defaultdict
    exec_rows = conn.execute(
        "SELECT exec_id, category, final_label, flag_for_human FROM classifications"
    ).fetchall()
    conn.close()

    # Count categories per exec_id
    cats_by_exec = defaultdict(lambda: {"product": 0, "auto": 0, "system": 0, "investigate": 0})
    for r in exec_rows:
        cat_key = CAT_MAP.get(r["category"], RC_CAT.get(r["final_label"], "investigate"))
        cats_by_exec[r["exec_id"]][cat_key] += 1

    result = []
    seen = set()
    for r in rows:
        eid = r["exec_id"]
        if eid in seen:
            continue
        seen.add(eid)
        result.append({
            "id":           eid,
            "name":         eid,
            "date":         (r["first_run"] or "")[:16].replace("T", "  "),
            "total":        r["total"],
            "failed":       r["total"],
            "review_needed": r["review_needed"] or 0,
            "cats":         cats_by_exec[eid],
        })

    return jsonify(result)


# ─── /api/builds/<exec_id> ───────────────────────────────────────────────────

@app.route("/api/builds/<path:exec_id>")
def build_detail(exec_id):
    if not DB_PATH.exists():
        abort(404)

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM classifications WHERE exec_id = ? ORDER BY classified_at",
        (exec_id,)
    ).fetchall()
    conn.close()

    if not rows:
        abort(404)

    def parse_neighbors(labels_str, ids_str, scores_str):
        if not labels_str:
            return []
        labels = [l.strip() for l in labels_str.split(",") if l.strip()]
        ids    = [i.strip() for i in (ids_str or "").split(",") if i.strip()]
        scores = [float(s.strip()) for s in (scores_str or "").split(",") if s.strip()]
        return [
            {
                "id":    f"TC-{ids[i]}" if i < len(ids) else f"KB-{i+1}",
                "label": lbl,
                "score": int(round(scores[i] * 100)) if i < len(scores) else 0,
            }
            for i, lbl in enumerate(labels[:5])
        ]

    test_cases = []
    for r in rows:
        cat_key = CAT_MAP.get(r["category"] or "", RC_CAT.get(r["final_label"] or "", "investigate"))
        module    = r["module"] or "Unknown"
        component = r["j_component"] or ""
        test_cases.append({
            "id":         f"TC-{r['tc_id']}",
            "name":       r["automated_tc_id"] or (f"{module} — {component}" if component else module),
            "module":     module,
            "component":  component,
            "jira_id":    r["jira_id"] or "",
            "status":     r["intrim_status"] or "FAILED",
            "category":   cat_key,
            "root_cause": r["final_label"],
            "confidence": round(float(r["confidence"]) * 100),
            "decision":   r["decision"],
            "flagged":    bool(r["flag_for_human"]),
            "remarks":    "" if not r["failure_remarks"] or r["failure_remarks"].lower() == "nan" else r["failure_remarks"],
            "reasoning":  r["reasoning"] or "",
            "similar":    parse_neighbors(r["neighbor_labels"], r["neighbor_ids"], r["neighbor_scores"]),
        })

    return jsonify(test_cases)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"[app] WARNING: No DB found at {DB_PATH}")
        print(f"[app]          Run pipeline_runner.py first to generate results.")
    else:
        row_count = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
        print(f"[app] DB ready — {row_count} classified rows")

    print(f"[app] Starting at http://localhost:5000")
    app.run(debug=True, port=5000)
