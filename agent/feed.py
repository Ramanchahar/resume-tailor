"""Read jobs and their descriptions from the public feed behind jobs.ypatodkar.com.

The site is a static app over two kinds of JSON file:
- api/all-jobs.json: every job (title, company index, URL, ATS, seniority, ...)
  plus a company table the jobs point into by index.
- api/desc/<shard>.json: job descriptions as HTML, keyed by job_id. The shard is
  an FNV-1a hash of the job_id mod 4096, matching the site's JobDescription.shardOf.
"""

import html
import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

BASE_URL = "https://jobs.ypatodkar.com/api"
# The feed rejects Python's default user agent with a 403.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) resume-tailor"}
SHARDS = 4096


@dataclass
class Job:
    job_id: str
    company: str
    title: str
    url: str
    ats: str | None
    posted: datetime | None
    seniority: str
    career: str | None
    city: str
    remote: bool
    roles: list[str] = field(default_factory=list)
    salary: str | None = None
    stale: bool = False
    described: bool = False


def _get_json(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_jobs() -> list[Job]:
    data = _get_json(f"{BASE_URL}/all-jobs.json")
    companies = data["companies"]
    jobs = []
    for raw in data["jobs"]:
        jobs.append(Job(
            job_id=raw["job_id"],
            company=companies[raw["c"]]["n"],
            title=raw["title"],
            url=raw["url"],
            ats=raw.get("ats"),
            posted=_parse_time(raw.get("posted")),
            seniority=raw.get("seniority", ""),
            career=raw.get("career"),
            city=raw.get("city", ""),
            remote=bool(raw.get("remote")),
            roles=raw.get("roles", []),
            salary=raw.get("salary"),
            stale=bool(raw.get("stale")),
            described=bool(raw.get("described")),
        ))
    return jobs


def shard_of(job_id: str) -> str:
    """FNV-1a over UTF-16 code units, as JavaScript's charCodeAt sees the string."""
    h = 2166136261
    units = job_id.encode("utf-16-le")
    for i in range(0, len(units), 2):
        h ^= units[i] | (units[i + 1] << 8)
        h = (h * 16777619) & 0xFFFFFFFF
    return format(h % SHARDS, "03x")


def html_to_text(markup: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>", "", markup, flags=re.I)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</(p|div|h\d|ul|ol|li|tr)>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_descriptions(job_ids: list[str]) -> dict[str, str]:
    """Plain-text descriptions for the given jobs. Jobs without one are omitted."""
    by_shard: dict[str, list[str]] = {}
    for job_id in job_ids:
        by_shard.setdefault(shard_of(job_id), []).append(job_id)
    out = {}
    for shard, ids in by_shard.items():
        try:
            descs = _get_json(f"{BASE_URL}/desc/{shard}.json")
        except Exception:
            continue
        for job_id in ids:
            if descs.get(job_id):
                out[job_id] = html_to_text(descs[job_id])
    return out
