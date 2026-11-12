# JaiHanuman: resume tailor

Rewrites `Raman.tex` so it fits a specific job, then compiles a one-page PDF.

## Use

1. Paste the job description into `description.txt`.
2. Run `.venv/bin/python main.py`.
3. Find the output in `generated_resumes/`: `<company>_<role>_<timestamp>.pdf`, the `.tex`, and `_notes.txt`. The notes give the angle the model took and the reframed claims to prepare for in interviews.

The script compiles every draft, measures how full the page is, and sends the model revision feedback (overflow, too much empty space, altered facts, LaTeX errors). It makes up to 3 revisions and saves the best draft.

## Job agent

`agent/` applies to jobs from [jobs.ypatodkar.com](https://jobs.ypatodkar.com/) on its own:

1. **Discover:** reads the site's job feed and skips stale listings and staff, principal, manager, exec, and intern roles.
2. **Fit check:** Claude reads each remaining posting against `Raman.tex` and scores the chance of an interview (0-100). It applies at 60+ and skips postings that rule out visa sponsorship or require citizenship or a clearance.
3. **Per-company cap:** at most 3 roles per company (the best-scoring ones), counting earlier applications.
4. **Tailor:** runs the resume tailor above for each selected job.
5. **Apply:** opens the Ashby, Greenhouse, or Lever form in a browser, uploads the tailored PDF, has Claude answer the questions from `profile.json`, and submits. If a person is needed (CAPTCHA, email code, a question the profile doesn't cover), it stops and records the job as `needs_manual` with a screenshot.

Every job and outcome is recorded in `data/applications.db`.

```sh
cp profile.example.json profile.json   # then fill in the null fields
.venv/bin/python -m agent.run --days 14 --stage discover   # find and score; applies to nothing
.venv/bin/python -m agent.report                           # see what it picked and why
.venv/bin/python -m agent.run --stage tailor --limit 5     # tailor resumes for the top picks
.venv/bin/python -m agent.run --stage apply --dry-run --limit 5 --headed   # fill forms, don't submit
.venv/bin/python -m agent.run --days 14                    # the real thing: all stages
```

Screenshots of every form are saved to `data/screenshots/`. Settings (score threshold, per-company cap, skipped levels) are at the top of `agent/run.py`.

To run twice a day, install the launchd job (see the comment in `launchd/com.ramanchahar.resume-tailor.plist`).

## Requirements

- `ANTHROPIC_API_KEY` set in your environment
- MacTeX (`pdflatex`)
- For the agent: `.venv/bin/python -m pip install playwright && .venv/bin/python -m playwright install chromium`

## Updating the base resume

Edit `Raman.tex`. The prompt reads it at runtime. If you add a role or a role's scope changes, also update `CAREER_CONTEXT` in `prompt_maker.py`, which keeps the model's claims plausible for each company and era.

Employers, titles, dates, locations, schools, GPAs, and contact details are pulled from `Raman.tex` automatically and checked verbatim. The preamble (fonts, margins, page size) is always taken from `Raman.tex`. The model writes only the body.
