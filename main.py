"""Tailor Raman.tex to the job in description.txt and compile a one-page PDF.

Usage: paste a job description into description.txt, then run `python main.py`.
"""

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

# Re-run under the project's .venv when launched with another interpreter
# (e.g. Anaconda's `python`), since dependencies are installed there.
_VENV_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
if os.path.exists(_VENV_PYTHON) and os.path.realpath(sys.prefix) != os.path.realpath(
    os.path.dirname(os.path.dirname(_VENV_PYTHON))
):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

from anthropic import Anthropic

from prompt_maker import build_job_message, build_revision_message, build_system_prompt

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_OUTPUT_TOKENS = 64000
MAX_REVISIONS = 3

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_RESUME = os.path.join(HERE, "Raman.tex")
JOB_DESCRIPTION = os.path.join(HERE, "description.txt")
OUTPUT_DIR = os.path.join(HERE, "generated_resumes")
PDFLATEX_SEARCH_PATHS = [
    "/Library/TeX/texbin",
    "/usr/local/texlive/2026/bin/universal-darwin",
    "/usr/local/texlive/2025/bin/universal-darwin",
    "/opt/homebrew/bin",
]

# Page-fit thresholds. A bullet line plus item spacing is roughly 13pt tall.
LINE_PT = 13.0
UNDERFILL_PT = 40.0  # more than ~3 empty lines at the bottom counts as underfilled

FILL_PROBE = (
    "\\par\\typeout{JHFILL total=\\the\\pagetotal\\space goal=\\the\\pagegoal"
    "\\space shrink=\\the\\pageshrink}\n"
)

client = Anthropic(max_retries=5)


# --------------------------------------------------------------------------- #
# Resume parsing
# --------------------------------------------------------------------------- #

def split_preamble(tex: str) -> tuple[str, str]:
    idx = tex.index("\\begin{document}")
    return tex[:idx], tex[idx:]


def brace_args(text: str, start: int, count: int) -> list[str]:
    """Read `count` consecutive {...} groups starting at `start`."""
    args, i = [], start
    while len(args) < count:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "{":
            break
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{" and text[j - 1] != "\\":
                depth += 1
            elif text[j] == "}" and text[j - 1] != "\\":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        args.append(text[i + 1:j].strip())
        i = j + 1
    return args


def protected_facts(base_body: str) -> list[str]:
    """Strings from the base resume that must survive verbatim: employers,
    titles, dates, locations, schools, GPAs, and contact details."""
    facts = []
    for m in re.finditer(r"\\resumeSubheading", base_body):
        args = brace_args(base_body, m.end(), 4)
        if len(args) != 4:
            continue
        # Publications use \resumeProjectHeading, so these are jobs and schools.
        title, dates, org, place = args
        facts += [title, dates]
        facts += [a for a in (org, place) if "\\" not in a and len(a) <= 40]
    facts += re.findall(r"\\scshape\s+([^}]+)\}", base_body)[:1]  # name
    facts += re.findall(r"\d{3}-\d{3}-\d{4}", base_body)  # phone
    facts += re.findall(r"mailto:\s*([^}\s]+)", base_body)  # email
    return list(dict.fromkeys(f.strip() for f in facts if f.strip()))


def href_urls(tex: str) -> set[str]:
    return set(re.findall(r"\\href\{([^}]+)\}", tex))


def extract_reply(text: str) -> tuple[str, str | None]:
    notes_m = re.search(r"<notes>([\s\S]*?)</notes>", text)
    notes = notes_m.group(1).strip() if notes_m else ""
    fence = re.search(r"```(?:latex|tex)?\s*([\s\S]*?)```", text)
    candidate = fence.group(1) if fence else text
    body_m = re.search(r"\\begin\{document\}[\s\S]*\\end\{document\}", candidate)
    return notes, (body_m.group(0) if body_m else None)


def parse_target(notes: str) -> tuple[str, str]:
    m = re.search(r"Target:\s*(.+?)\s*\|\s*(.+)", notes)
    if not m:
        return "", ""
    clean = lambda s: "" if s.strip().lower() == "unknown" else s.strip()
    return clean(m.group(1)), clean(m.group(2))


# --------------------------------------------------------------------------- #
# Compilation and measurement
# --------------------------------------------------------------------------- #

def find_pdflatex() -> str | None:
    found = shutil.which("pdflatex")
    if found:
        return found
    for path in PDFLATEX_SEARCH_PATHS:
        candidate = os.path.join(path, "pdflatex")
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def compile_and_measure(tex: str, pdflatex: str) -> dict:
    """Compile in a temp dir with a fill probe. Returns pages, slack/overflow
    (in pt), the PDF bytes, and any LaTeX errors."""
    probed = tex.replace("\\end{document}", FILL_PROBE + "\\end{document}")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "resume.tex"), "w") as f:
            f.write(probed)
        env = {**os.environ, "PATH": f"{os.path.dirname(pdflatex)}:{os.environ.get('PATH', '')}"}
        for _ in range(2):  # second pass settles hyperref
            proc = subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=tmp, env=env, capture_output=True, text=True, errors="ignore",
            )
            if proc.returncode != 0:
                lines = proc.stdout.splitlines()
                errs = [
                    " ".join(lines[i:i + 3])
                    for i, ln in enumerate(lines) if ln.startswith("!")
                ]
                return {"ok": False, "errors": errs or lines[-15:]}
        with open(os.path.join(tmp, "resume.log"), errors="ignore") as f:
            log = f.read()
        with open(os.path.join(tmp, "resume.pdf"), "rb") as f:
            pdf = f.read()

    pages_m = re.search(r"Output written on .*?\((\d+) page", log)
    fill_m = re.search(r"JHFILL total=([\d.]+)pt goal=([\d.]+)pt shrink=([\d.]+)pt", log)
    pages = int(pages_m.group(1)) if pages_m else 0
    result = {"ok": True, "pdf": pdf, "pages": pages, "slack": None, "overflow": None}
    if fill_m:
        total, goal, shrink = map(float, fill_m.groups())
        if pages == 1:
            result["slack"] = goal - (total - shrink)
        else:
            result["overflow"] = total  # height spilled onto the last page
    return result


def review(body: str, build: dict, facts: list[str], allowed_urls: set[str]) -> list[str]:
    problems = []
    missing = [f for f in facts if f not in body]
    if missing:
        problems.append(
            "These facts must appear exactly as in the original resume but are "
            "missing or altered: " + "; ".join(f'"{m}"' for m in missing)
        )
    bad_urls = href_urls(body) - allowed_urls
    if bad_urls:
        problems.append(
            "These URLs aren't in the original resume, so remove them or restore the "
            "originals: " + ", ".join(sorted(bad_urls))
        )
    if not build["ok"]:
        problems.append("pdflatex failed:\n  " + "\n  ".join(build["errors"][:5]))
        return problems
    if build["pages"] > 1:
        spill = build["overflow"] or LINE_PT
        lines = math.ceil(spill / LINE_PT)
        problems.append(
            f"The resume runs to {build['pages']} pages. Cut roughly {lines} lines "
            f"(~{spill:.0f}pt spilled onto the next page) so it fits on one page."
        )
    elif build["slack"] is not None and build["slack"] > UNDERFILL_PT:
        lines = int(build["slack"] // LINE_PT)
        problems.append(
            f"The page has about {lines} empty lines at the bottom. Fill most of that "
            "space with job-relevant substance (don't exceed it, or the page overflows)."
        )
    return problems


def score(build: dict, problems: list[str]) -> tuple:
    """Higher is better: compiles, one page, facts intact, least empty space."""
    if not build["ok"]:
        return (0,)
    facts_ok = not any(p.startswith(("These facts", "These URLs")) for p in problems)
    one_page = build["pages"] == 1
    return (1, one_page, facts_ok, -(build["slack"] or 0) if one_page else 0)


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

def call_claude(system_prompt: str, messages: list) -> tuple[str, list]:
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens before finishing.")
    u = response.usage
    print(
        f"  tokens: in={u.input_tokens} cached={u.cache_read_input_tokens or 0} "
        f"out={u.output_tokens}"
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    return text, response.content


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def slugify(text: str, max_len: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:max_len].lower()


def main() -> int:
    if not os.path.exists(JOB_DESCRIPTION):
        print(f"ERROR: {JOB_DESCRIPTION} not found. Paste a job description into it.")
        return 1
    with open(JOB_DESCRIPTION) as f:
        job_description = f.read().strip()
    if not job_description:
        print("ERROR: description.txt is empty. Paste a job description into it.")
        return 1

    pdflatex = find_pdflatex()
    if not pdflatex:
        print("ERROR: pdflatex not found. Install MacTeX: https://www.tug.org/mactex/")
        return 1

    with open(BASE_RESUME) as f:
        base_tex = f.read()
    preamble, base_body = split_preamble(base_tex)
    facts = protected_facts(base_body)
    allowed_urls = href_urls(base_body)

    system_prompt = build_system_prompt(base_tex)
    messages = [{"role": "user", "content": build_job_message(job_description)}]
    best = None  # (score, tex, notes, build, problems)

    for attempt in range(MAX_REVISIONS + 1):
        label = "Drafting" if attempt == 0 else f"Revision {attempt}/{MAX_REVISIONS}"
        print(f"{label} with {MODEL} (effort={EFFORT})...")
        text, content = call_claude(system_prompt, messages)
        notes, body = extract_reply(text)

        if body is None:
            problems = ["No \\begin{document}...\\end{document} body found in a ```latex fence."]
            build = {"ok": False, "errors": []}
            tex = ""
        else:
            tex = preamble + body + "\n"
            build = compile_and_measure(tex, pdflatex)
            problems = review(body, build, facts, allowed_urls)

        if build["ok"]:
            fit = (
                f"{build['pages']} page(s)"
                + (f", {build['slack']:.0f}pt free" if build.get("slack") is not None else "")
            )
            print(f"  compiled: {fit}")
        candidate = (score(build, problems), tex, notes, build, problems)
        if body is not None and (best is None or candidate[0] > best[0]):
            best = candidate

        if not problems:
            break
        for p in problems:
            print(f"  issue: {p.splitlines()[0]}")
        if attempt < MAX_REVISIONS:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": build_revision_message(problems)})

    if best is None:
        print("ERROR: no usable resume was produced.")
        return 1

    _, tex, notes, build, problems = best
    company, role = parse_target(notes)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base_name = "_".join(s for s in (slugify(company), slugify(role), stamp) if s)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, base_name)

    with open(out + ".tex", "w") as f:
        f.write(tex)
    with open(out + "_notes.txt", "w") as f:
        f.write(notes + "\n")
    if build["ok"]:
        with open(out + ".pdf", "wb") as f:
            f.write(build["pdf"])

    print()
    if notes:
        print(notes)
        print()
    print(f"TeX:   {out}.tex")
    print(f"Notes: {out}_notes.txt")
    if build["ok"]:
        print(f"PDF:   {out}.pdf")
    if problems:
        print("WARNING: best draft still has issues:")
        for p in problems:
            print(f"  - {p.splitlines()[0]}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
