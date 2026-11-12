"""Judge whether Raman has a realistic chance at a job, with Claude."""

import json
import os

from anthropic import Anthropic

from prompt_maker import CAREER_CONTEXT

MODEL = "claude-opus-5-5"
EFFORT = "low"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_RESUME = os.path.join(HERE, "Raman.tex")

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "years_required": {
            "type": "string",
            "description": "Years of experience the posting asks for, e.g. '3+', '5-8', or 'not stated'.",
        },
        "sponsorship": {
            "type": "string",
            "enum": ["available", "not_available", "not_mentioned"],
            "description": "not_available if the posting rules out visa sponsorship, or requires US citizenship, a green card, or a security clearance.",
        },
        "score": {
            "type": "integer",
            "description": "0-100: how likely Raman is to get an interview.",
        },
        "reason": {"type": "string", "description": "One sentence explaining the score."},
    },
    "required": ["years_required", "sponsorship", "score", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """
You screen job postings for Raman Chahar and estimate how likely he is to get an interview if he applies. He applies only where he has a realistic chance, so your score decides where his applications go.

Raman's resume:
<resume>
{resume}
</resume>

What each role on it covered:
<career_context>
{career_context}
</career_context>

His situation: about 1.5 years of full-time software engineering (Experian, since June 2025, promoted to SWE II in Jan 2026), plus a year as a data scientist and two internships. MS in CS (Northeastern, 2025). He's on F-1 STEM OPT and will need H-1B sponsorship.

Scoring guide:
- 80-100: strong match. Level fits his experience and the core of the job is work he's done (agents/LLM platforms, AI infrastructure, data engineering, backend/platform).
- 60-79: realistic. Level fits or is a reasonable stretch (e.g. 3+ years asked, or senior at a startup that hires on ability), and the job overlaps substantially with his skills.
- 30-59: long shot. Requires notably more experience, or the domain/stack is mostly unfamiliar (e.g. embedded firmware, iOS, hardware verification).
- 0-29: no real chance. Staff/principal/lead/manager scope, 6+ years required, or unrelated field.

Weigh the actual requirements over the title: some "Senior" startup roles ask for 3+ years, and some "Software Engineer" roles ask for 7+. Seniority labels in the feed are guesses, so read the posting.
""".strip()

client = Anthropic(max_retries=5)


def _system_prompt() -> str:
    with open(BASE_RESUME) as f:
        resume = f.read()
    return SYSTEM_PROMPT.format(resume=resume, career_context=CAREER_CONTEXT)


_SYSTEM = None


def assess(job, description: str) -> dict:
    """Returns the FIT_SCHEMA fields for one job."""
    global _SYSTEM
    if _SYSTEM is None:
        _SYSTEM = _system_prompt()
    posting = (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.city}"
        f"{' (remote OK)' if job.remote else ''}\nSalary: {job.salary or 'not stated'}\n\n"
        f"{description}"
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"<posting>\n{posting}\n</posting>"}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": FIT_SCHEMA},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Fit check hit max_tokens.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
