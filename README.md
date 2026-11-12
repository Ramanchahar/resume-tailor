# JaiHanuman: resume tailor

Rewrites `Raman.tex` so it fits a specific job, then compiles a one-page PDF.

## Use

1. Paste the job description into `description.txt`.
2. Run `.venv/bin/python main.py`.
3. Find the output in `generated_resumes/`: `<company>_<role>_<timestamp>.pdf`, the `.tex`, and `_notes.txt`. The notes give the angle the model took and the reframed claims to prepare for in interviews.

The script compiles every draft, measures how full the page is, and sends the model revision feedback (overflow, too much empty space, altered facts, LaTeX errors). It makes up to 3 revisions and saves the best draft.

## Requirements

- `ANTHROPIC_API_KEY` set in your environment
- MacTeX (`pdflatex`)

## Updating the base resume

Edit `Raman.tex`. The prompt reads it at runtime. If you add a role or a role's scope changes, also update `CAREER_CONTEXT` in `prompt_maker.py`, which keeps the model's claims plausible for each company and era.

Employers, titles, dates, locations, schools, GPAs, and contact details are pulled from `Raman.tex` automatically and checked verbatim. The preamble (fonts, margins, page size) is always taken from `Raman.tex`. The model writes only the body.
