"""Find new jobs, keep the ones Raman has a realistic shot at, tailor his resume, and apply.

  .venv/bin/python -m agent.run --days 14            # first run: last two weeks
  .venv/bin/python -m agent.run                      # daily run: anything new
  .venv/bin/python -m agent.run --dry-run --limit 3  # fill forms but don't submit
  .venv/bin/python -m agent.run --stage discover     # only find and score jobs
  .venv/bin/python -m agent.report                   # what's been done

Each stage picks up where the last run left off, so an interrupted run can just
be started again.
"""

import argparse
import json
import os
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# Re-run under the project's .venv, like main.py.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PYTHON = os.path.join(HERE, ".venv", "bin", "python")
if os.path.exists(_VENV_PYTHON) and os.path.realpath(sys.prefix) != os.path.realpath(
    os.path.dirname(os.path.dirname(_VENV_PYTHON))
):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, "-m", "agent.run", *sys.argv[1:]])
sys.path.insert(0, HERE)

from agent import feed, fit, tracker  # noqa: E402

PROFILE_PATH = os.path.join(HERE, "profile.json")
RESUME_DIR = os.path.join(HERE, "generated_resumes", "applications")

SUPPORTED_ATS = {"ashby", "greenhouse", "lever"}
SKIP_SENIORITY = {"intern", "staff", "manager", "exec"}
SKIP_TITLE = re.compile(
    r"\b(staff|principal|distinguished|director|head of|vp|vice president|chief|manager|"
    r"lead|architect|intern(ship)?|co-?op|phd)\b",
    re.I,
)
MIN_SCORE = 60          # "realistic chance" on fit.py's scale
PER_COMPANY_CAP = 3     # most roles to apply to at one company
FIT_WORKERS = 8
TAILOR_WORKERS = 3


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def prefilter_reason(job, since: datetime) -> str | None:
    if job.stale:
        return "listing is stale"
    if not job.posted or job.posted < since:
        return "posted before the window"
    if job.seniority in SKIP_SENIORITY:
        return f"seniority: {job.seniority}"
    if SKIP_TITLE.search(job.title):
        return "title is above or outside his level"
    if not job.described:
        return "no job description"
    return None


# --------------------------------------------------------------------------- #
# Stage 1: discover and score
# --------------------------------------------------------------------------- #

def discover(days: int):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    jobs = feed.fetch_jobs()
    with tracker.connect() as conn:
        known = tracker.known_ids(conn)
        fresh = [j for j in jobs if j.job_id not in known and j.posted and j.posted >= since]
        log(f"{len(jobs)} jobs in feed, {len(fresh)} new in the last {days} days")

        candidates = []
        for job in fresh:
            reason = prefilter_reason(job, since)
            if reason:
                tracker.add_job(conn, job, "filtered", fit_reason=reason)
            else:
                candidates.append(job)
        conn.commit()
        log(f"{len(candidates)} pass the prefilter; fetching descriptions")

        descriptions = feed.fetch_descriptions([j.job_id for j in candidates])
        log(f"Scoring fit for {len(candidates)} jobs with {fit.MODEL}...")
        scored = []
        with ThreadPoolExecutor(FIT_WORKERS) as pool:
            futures = {
                pool.submit(fit.assess, job, descriptions[job.job_id]): job
                for job in candidates if job.job_id in descriptions
            }
            for i, fut in enumerate(as_completed(futures), 1):
                job = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    log(f"  fit check failed for {job.company} / {job.title}: {e}")
                    continue  # not recorded, so the next run retries it
                status = "candidate"
                if result["sponsorship"] == "not_available":
                    status = "rejected"
                elif result["score"] < MIN_SCORE:
                    status = "rejected"
                reason = f"{result['reason']} (asks {result['years_required']})"
                tracker.add_job(conn, job, status, fit_score=result["score"],
                                sponsorship=result["sponsorship"], fit_reason=reason)
                if status == "candidate":
                    scored.append((job, result["score"]))
                if i % 25 == 0:
                    conn.commit()
                    log(f"  scored {i}/{len(futures)}")
        conn.commit()

        # Per-company cap: keep each company's best-fitting roles, counting the
        # ones already chosen or applied to in earlier runs.
        by_company: dict[str, list] = {}
        for job, score in scored:
            by_company.setdefault(job.company, []).append((score, job))
        selected = 0
        for company, items in by_company.items():
            room = max(0, PER_COMPANY_CAP - tracker.committed_count(conn, company))
            items.sort(key=lambda x: x[0], reverse=True)
            for rank, (score, job) in enumerate(items):
                status = "selected" if rank < room else "over_cap"
                tracker.update(conn, job.job_id, status=status)
                selected += status == "selected"
        log(f"{len(scored)} realistic fits; {selected} selected after the per-company cap")


# --------------------------------------------------------------------------- #
# Stage 2: tailor
# --------------------------------------------------------------------------- #

def _slug(text: str, n: int = 30) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:n].lower()


def tailor_selected(limit: int | None):
    from main import tailor  # imported here so discover-only runs skip pdflatex checks

    with tracker.connect() as conn:
        rows = tracker.with_status(conn, "selected")[:limit]
    if not rows:
        log("Nothing to tailor")
        return
    descriptions = feed.fetch_descriptions([r["job_id"] for r in rows])
    log(f"Tailoring {len(rows)} resumes...")

    def work(row):
        desc = descriptions.get(row["job_id"])
        if not desc:
            raise RuntimeError("description no longer available")
        name = f"{_slug(row['company'])}_{_slug(row['title'])}_{row['job_id'][-8:]}"
        return tailor(f"{row['company']} — {row['title']}\n\n{desc}", RESUME_DIR, name)

    with ThreadPoolExecutor(TAILOR_WORKERS) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            with tracker.connect() as conn:
                try:
                    result = fut.result()
                except Exception as e:
                    tracker.update(conn, row["job_id"], status="failed", error=f"tailor: {e}")
                    log(f"  FAILED {row['company']} / {row['title']}: {e}")
                    continue
                if not result["pdf_path"]:
                    tracker.update(conn, row["job_id"], status="failed",
                                   error="tailor: resume didn't compile")
                    continue
                warn = "; ".join(p.splitlines()[0] for p in result["problems"]) or None
                tracker.update(conn, row["job_id"], status="tailored",
                               resume_pdf=result["pdf_path"], error=warn)
                log(f"  tailored {row['company']} / {row['title']}")


# --------------------------------------------------------------------------- #
# Stage 3: apply
# --------------------------------------------------------------------------- #

def apply_tailored(limit: int | None, dry_run: bool, headed: bool):
    from playwright.sync_api import sync_playwright

    from agent.apply import NeedsManual, apply

    if not os.path.exists(PROFILE_PATH):
        sys.exit(f"ERROR: {PROFILE_PATH} not found. Copy profile.example.json and fill it in.")
    with open(PROFILE_PATH) as f:
        profile = json.load(f)

    with tracker.connect() as conn:
        rows = tracker.with_status(conn, "tailored")[:limit]
    if not rows:
        log("Nothing to apply to")
        return
    descriptions = feed.fetch_descriptions([r["job_id"] for r in rows])
    log(f"{'Filling (dry run)' if dry_run else 'Applying to'} {len(rows)} jobs...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        for row in rows:
            job = dict(row)
            tex_path = row["resume_pdf"].removesuffix(".pdf") + ".tex"
            notes_path = row["resume_pdf"].removesuffix(".pdf") + "_notes.txt"
            with open(tex_path) as f:
                resume_tex = f.read()
            notes = open(notes_path).read() if os.path.exists(notes_path) else ""
            label = f"{row['company']} / {row['title']}"
            with tracker.connect() as conn:
                try:
                    detail = apply(browser, job, row["resume_pdf"], resume_tex, notes,
                                   descriptions.get(row["job_id"], ""), profile, dry_run)
                except NeedsManual as e:
                    tracker.update(conn, row["job_id"], status="needs_manual", error=str(e))
                    log(f"  NEEDS YOU  {label}: {e}")
                    continue
                except Exception as e:
                    tracker.update(conn, row["job_id"], status="failed",
                                   error=f"apply: {type(e).__name__}: {e}")
                    log(f"  FAILED     {label}: {e}")
                    traceback.print_exc()
                    continue
                if dry_run:
                    log(f"  DRY RUN    {label}: {detail}")
                else:
                    tracker.update(conn, row["job_id"], status="applied",
                                   applied_at=tracker.now(), error=None)
                    log(f"  APPLIED    {label}")
        browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3,
                    help="look at jobs posted in the last N days (default 3; 14 for the first run)")
    ap.add_argument("--stage", choices=["all", "discover", "tailor", "apply"], default="all")
    ap.add_argument("--limit", type=int, help="tailor/apply at most N jobs this run")
    ap.add_argument("--dry-run", action="store_true", help="fill forms but don't submit")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()

    if args.stage in ("all", "discover"):
        discover(args.days)
    if args.stage in ("all", "tailor"):
        tailor_selected(args.limit)
    if args.stage in ("all", "apply"):
        apply_tailored(args.limit, args.dry_run, args.headed)


if __name__ == "__main__":
    main()
