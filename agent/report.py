"""Summarize the application tracker.

  .venv/bin/python -m agent.report             # counts, recent applications, what needs you
  .venv/bin/python -m agent.report --csv out.csv
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import tracker  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="export every tracked job to this CSV file")
    ap.add_argument("--all", action="store_true", help="list every application, not just recent")
    args = ap.parse_args()

    with tracker.connect() as conn:
        if args.csv:
            rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
            with open(args.csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(rows[0].keys() if rows else [])
                w.writerows([tuple(r) for r in rows])
            print(f"Wrote {len(rows)} rows to {args.csv}")
            return

        print("Status counts:")
        for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY COUNT(*) DESC"
        ):
            print(f"  {status:<13} {n}")

        limit = "" if args.all else "LIMIT 25"
        applied = conn.execute(
            f"SELECT * FROM jobs WHERE status = 'applied' ORDER BY applied_at DESC {limit}"
        ).fetchall()
        print(f"\nApplied ({'all' if args.all else 'latest 25'}):")
        for r in applied:
            print(f"  {r['applied_at'][:10]}  {r['fit_score']:>3}  {r['company']} / {r['title']}")

        manual = tracker.with_status(conn, "needs_manual")
        if manual:
            print("\nNeeds you (resume is ready, finish the form yourself):")
            for r in manual:
                print(f"  {r['company']} / {r['title']}\n    {r['url']}\n"
                      f"    resume: {r['resume_pdf']}\n    why: {r['error']}")

        failed = tracker.with_status(conn, "failed")
        if failed:
            print("\nFailed:")
            for r in failed:
                print(f"  {r['company']} / {r['title']}: {r['error']}")


if __name__ == "__main__":
    main()
