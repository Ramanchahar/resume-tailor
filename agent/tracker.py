"""SQLite record of every job the agent has seen and what it did with it.

Statuses, in pipeline order:
  filtered      dropped by the rule-based prefilter (wrong level, stale, unsupported ATS)
  candidate     a realistic fit, before the per-company cap is applied
  rejected      Claude judged there's no realistic chance, or no visa sponsorship
  over_cap      a good fit, but the company already has its quota of better-fitting roles
  selected      chosen to apply; waiting to be tailored
  tailored      resume ready; waiting to be submitted
  applied       submitted and the ATS confirmed it
  needs_manual  prepared, but a person has to finish it (CAPTCHA, unknown form, ...)
  failed        something broke; `error` says what
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "data", "applications.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    ats           TEXT,
    posted        TEXT,
    seniority     TEXT,
    city          TEXT,
    salary        TEXT,
    first_seen    TEXT NOT NULL,
    status        TEXT NOT NULL,
    fit_score     INTEGER,
    sponsorship   TEXT,
    fit_reason    TEXT,
    resume_pdf    TEXT,
    applied_at    TEXT,
    error         TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
"""

# Statuses that count toward a company's cap: we chose them, or already applied.
COMMITTED = ("selected", "tailored", "applied", "needs_manual")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: str = DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def known_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT job_id FROM jobs")}


def add_job(conn, job, status: str, **fields):
    conn.execute(
        """INSERT OR IGNORE INTO jobs
           (job_id, company, title, url, ats, posted, seniority, city, salary,
            first_seen, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job.job_id, job.company, job.title, job.url, job.ats,
         job.posted.isoformat() if job.posted else None, job.seniority, job.city,
         job.salary, now(), status, now()),
    )
    if fields:
        update(conn, job.job_id, **fields)


def update(conn, job_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id))


def committed_count(conn, company: str) -> int:
    marks = ",".join("?" * len(COMMITTED))
    return conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE company = ? AND status IN ({marks})",
        (company, *COMMITTED),
    ).fetchone()[0]


def with_status(conn, *statuses: str) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY fit_score DESC", statuses
    ).fetchall()
