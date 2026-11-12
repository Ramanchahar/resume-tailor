"""Fill and submit an application on Ashby, Greenhouse, or Lever with Playwright.

Rather than hard-coding each company's form, this reads every field on the page
(label, type, options, required), asks Claude to answer them from profile.json,
the resume, and the job, then fills them in. Anything it can't do honestly or
reliably (a CAPTCHA, a required question the profile doesn't cover, an unknown
form) comes back as needs_manual with a screenshot, rather than a guess.
"""

import json
import os
import re
from urllib.parse import urlparse

from anthropic import Anthropic

MODEL = "claude-opus-5-5"
EFFORT = "medium"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCREENSHOT_DIR = os.path.join(HERE, "data", "screenshots")

CONFIRMATION = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (was|has been) "
    r"(successfully )?(submitted|received)|we('|’)ve received your application|successfully submitted",
    re.I,
)
VERIFICATION = re.compile(r"security code|verification code|verify your email", re.I)

client = Anthropic(max_retries=5)


class NeedsManual(Exception):
    """A person has to finish this application."""


# --------------------------------------------------------------------------- #
# Reading the form
# --------------------------------------------------------------------------- #

# Tags each control with data-rt-key and returns a description of every field.
# Radios and checkboxes sharing a name become one field with options; so do
# Yes/No button pairs (Ashby renders those as buttons, not inputs).
EXTRACT_FIELDS_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(' ').map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    // Fall back to the nearest container that has some text besides this control.
    let node = el.parentElement;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const t = clean(node.innerText);
      if (t) return t.slice(0, 300);
    }
    return clean(el.getAttribute('placeholder') || el.name || '');
  };
  // The question a radio group, Yes/No pair, or file input belongs to: the nearest
  // heading-like text above it that isn't one of its own option labels.
  const groupLabel = el => {
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    const isOptionLabel = l => l.htmlFor && document.getElementById(l.htmlFor)?.matches(
      'input[type="radio"], input[type="checkbox"]');
    let node = el.parentElement;
    for (let i = 0; node && i < 6; i++, node = node.parentElement) {
      const cands = node.querySelectorAll('label, [class*="title"], [class*="label"], legend, h3, h4');
      for (const lbl of cands) {
        if (lbl.contains(el) || lbl.querySelector('input') || isOptionLabel(lbl)) continue;
        if (clean(lbl.innerText)) return clean(lbl.innerText);
      }
    }
    return '';
  };
  const required = el =>
    el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$/.test(labelOf(el));
  // react-select shows the chosen value in a sibling element, not the input.
  const comboValue = el => {
    if (el.value) return el.value;
    let node = el.parentElement;
    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
      const v = node.querySelector('[class*="single-value"], [class*="singleValue"], ' +
                                   '[class*="multi-value"], [class*="multiValue"]');
      if (v) return clean(v.innerText);
    }
    return '';
  };

  let n = 0;
  const key = el => {
    if (!el.dataset.rtKey) el.dataset.rtKey = 'f' + (n++);
    return el.dataset.rtKey;
  };
  document.querySelectorAll('[data-rt-key]').forEach(el => {
    n = Math.max(n, parseInt(el.dataset.rtKey.slice(1)) + 1);
  });

  const fields = [], groups = {};
  const controls = document.querySelectorAll('input, textarea, select, [role="combobox"]');
  for (const el of controls) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (type === 'file') {
      const label = [groupLabel(el), labelOf(el), el.id, el.name].filter(Boolean).join(' | ');
      fields.push({key: key(el), kind: 'file', label, required: required(el)});
      continue;
    }
    if (!visible(el) && !(type === 'radio' || type === 'checkbox')) continue;
    // Validation shadows (e.g. Greenhouse's hidden requiredInput behind each dropdown).
    if (el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0 && type !== 'radio' && type !== 'checkbox') continue;
    if (type === 'radio' || type === 'checkbox') {
      const name = el.name || ('_' + key(el));
      const optLabel = labelOf(el);
      if (!groups[name]) {
        groups[name] = {key: key(el), kind: type, label: groupLabel(el) || optLabel,
                        required: required(el), options: [], optionKeys: [], checked: []};
        fields.push(groups[name]);
      }
      groups[name].options.push(optLabel);
      groups[name].optionKeys.push(key(el));
      if (el.checked) groups[name].checked.push(optLabel);
      if (required(el)) groups[name].required = true;
      continue;
    }
    if (el.tagName === 'SELECT') {
      fields.push({key: key(el), kind: 'select', label: labelOf(el), required: required(el),
                   options: [...el.options].map(o => clean(o.text)).filter(Boolean),
                   value: el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : ''});
      continue;
    }
    const isCombo = el.getAttribute('role') === 'combobox';
    fields.push({key: key(el), kind: isCombo ? 'combobox' : (el.tagName === 'TEXTAREA' ? 'textarea' : type),
                 label: labelOf(el), required: required(el),
                 value: isCombo ? comboValue(el) : (el.value || '')});
  }

  // Yes/No button pairs.
  const seen = new Set();
  for (const btn of document.querySelectorAll('button')) {
    if (!visible(btn) || !/^(yes|no)$/i.test(clean(btn.innerText))) continue;
    const box = btn.parentElement;
    if (seen.has(box)) continue;
    seen.add(box);
    const btns = [...box.querySelectorAll('button')].filter(b => /^(yes|no)$/i.test(clean(b.innerText)));
    if (btns.length !== 2) continue;
    fields.push({key: key(box), kind: 'buttons', label: groupLabel(btns[0]), required: true,
                 options: btns.map(b => clean(b.innerText)), optionKeys: btns.map(key),
                 checked: btns.filter(b => /selected|active|checked/i.test(b.className)
                                          || b.getAttribute('aria-pressed') === 'true')
                              .map(b => clean(b.innerText))});
  }
  return fields;
}
"""


def _find_form_url(page, job) -> str:
    """Navigate to the job's application form and return its URL."""
    url = job["url"].split("#")[0]
    host = urlparse(url).netloc
    if job["ats"] == "lever":
        return url.rstrip("/").removesuffix("/apply") + "/apply"
    if job["ats"] == "ashby":
        return url.rstrip("/").removesuffix("/application") + "/application"
    if job["ats"] == "greenhouse":
        if "greenhouse.io" in host:
            return url
        # Greenhouse on a company's own site embeds the form in an iframe.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        frame = page.locator('iframe[src*="greenhouse.io"]').first
        try:
            frame.wait_for(state="attached", timeout=15000)
        except Exception:
            raise NeedsManual("Greenhouse form not found on the company's site.")
        return frame.get_attribute("src")
    raise NeedsManual(f"Unsupported ATS: {job['ats']}")


def _open_form(page):
    """Some boards show the posting first and the form behind an Apply button."""
    if page.locator('input[type="file"]').count():
        return
    for name in (r"^apply( for this job| now)?$", r"apply"):
        btn = page.get_by_role("button", name=re.compile(name, re.I)).or_(
            page.get_by_role("link", name=re.compile(name, re.I))
        ).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(2000)
            if page.locator('input[type="file"]').count():
                return


def _captcha_challenge(page) -> bool:
    """A visible challenge (not invisible reCAPTCHA, which usually passes silently)."""
    for sel in ('iframe[src*="recaptcha"][src*="bframe"]', 'iframe[src*="hcaptcha"]',
                'iframe[src*="challenges.cloudflare"]', 'iframe[title*="challenge" i]'):
        for frame in page.locator(sel).all():
            try:
                if frame.is_visible() and (frame.bounding_box() or {}).get("height", 0) > 100:
                    return True
            except Exception:
                pass
    return False


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One value, or several for multi-select checkboxes. For fields with options, copy option text exactly.",
                    },
                },
                "required": ["key", "values"],
                "additionalProperties": False,
            },
        },
        "unanswerable": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["key", "why"],
                "additionalProperties": False,
            },
            "description": "Required fields that can't be answered truthfully from the information given.",
        },
    },
    "required": ["answers", "unanswerable"],
    "additionalProperties": False,
}

ANSWER_SYSTEM = """
You fill in job application forms for Raman Chahar. You get his profile, his tailored resume for this job, the job description, and the form's fields. Return a value for every field that should be filled.

Rules:
- Facts come only from the profile and resume. Never invent anything: no made-up degrees, employers, dates, numbers, links, or legal status. If a required field asks for something they don't cover, list it under unanswerable instead of guessing.
- Profile values set to null are unknown. Treat them the same as missing.
- For fields with options (select, radio, checkbox, buttons), copy the chosen option's text exactly as given.
- Work authorization: answer from the profile. He is authorized to work in the US now (F-1 STEM OPT) and will require sponsorship in the future, so "will you now or in the future require sponsorship" is Yes.
- Demographic and EEO questions: use the profile's eeo answers; pick the option that matches, or the closest "decline to answer" option.
- Short open-ended questions ("Why are you interested in X?", "Tell us about a project"): write 2-4 specific sentences in first person, tying real work from the resume to this job. No filler, no superlatives.
- Fill optional fields the profile covers (location, links, preferred name). Skip optional fields that add nothing (e.g. an optional cover letter text box, "anything else?"). Don't skip the resume upload; it's handled separately, so leave file fields out of answers.
- Leave out fields that already show the correct value.
- Phone numbers: digits as in the profile. Location fields: "San Jose, California, United States" unless the options require another form.
""".strip()


def _answer_fields(fields, profile, resume_tex, notes, job, description) -> dict:
    visible_fields = [
        {k: v for k, v in f.items() if k != "optionKeys"} for f in fields if f["kind"] != "file"
    ]
    prompt = (
        f"<profile>\n{json.dumps(profile, indent=1)}\n</profile>\n\n"
        f"<tailored_resume>\n{resume_tex}\n</tailored_resume>\n\n"
        f"<tailoring_notes>\n{notes}\n</tailoring_notes>\n\n"
        f"<job>\nCompany: {job['company']}\nTitle: {job['title']}\n\n{description}\n</job>\n\n"
        f"<form_fields>\n{json.dumps(visible_fields, indent=1)}\n</form_fields>"
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": ANSWER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Form answers hit max_tokens.")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


# --------------------------------------------------------------------------- #
# Filling
# --------------------------------------------------------------------------- #

def _match_option(options: list[str], wanted: str) -> int | None:
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    w = norm(wanted)
    for i, o in enumerate(options):
        if norm(o) == w:
            return i
    for i, o in enumerate(options):
        if w and (w in norm(o) or norm(o) in w):
            return i
    return None


def _fill_combobox(page, el, value: str):
    el.click()
    el.fill("")
    el.type(value, delay=30)
    page.wait_for_timeout(1200)
    options = page.locator('[role="option"]:visible')
    texts = options.all_inner_texts()
    idx = _match_option(texts, value) if texts else None
    if idx is not None:
        options.nth(idx).click()
    elif texts:
        options.first.click()  # autocomplete (e.g. location): take the top suggestion
    else:
        el.press("Enter")


def _fill(page, field: dict, values: list[str]):
    loc = lambda k: page.locator(f'[data-rt-key="{k}"]')
    kind = field["kind"]
    if not values:
        return
    if kind in ("radio", "checkbox", "buttons"):
        for v in values:
            idx = _match_option(field["options"], v)
            if idx is None:
                raise NeedsManual(f'No option "{v}" for "{field["label"][:80]}"')
            target = loc(field["optionKeys"][idx])
            if kind == "buttons":
                target.click()
            else:
                target.check(force=True)
    elif kind == "select":
        idx = _match_option(field["options"], values[0])
        if idx is None:
            raise NeedsManual(f'No option "{values[0]}" for "{field["label"][:80]}"')
        loc(field["key"]).select_option(label=field["options"][idx])
    elif kind == "combobox":
        _fill_combobox(page, loc(field["key"]), values[0])
    else:
        loc(field["key"]).fill(values[0])


def _upload_resume(page, fields, resume_pdf: str):
    files = [f for f in fields if f["kind"] == "file"]
    if not files:
        raise NeedsManual("No file upload field for the resume.")
    # Skip Ashby's "Autofill from resume" helper and cover-letter inputs.
    real = [f for f in files if not re.search(r"autofill|cover", f["label"], re.I)] or files
    target = next((f for f in real if re.search(r"resume|cv", f["label"], re.I)), real[0])
    page.locator(f'[data-rt-key="{target["key"]}"]').set_input_files(resume_pdf)
    page.wait_for_timeout(3000)  # Ashby parses the resume and autofills fields


def _missing_required(fields) -> list[str]:
    missing = []
    for f in fields:
        if not f["required"] or f["kind"] == "file":
            continue
        filled = f.get("checked") if f["kind"] in ("radio", "checkbox", "buttons") else f.get("value")
        if not filled:
            missing.append(f["label"][:80])
    return missing


def _submit(page):
    btn = page.locator('button[type="submit"]:visible, input[type="submit"]:visible').or_(
        page.get_by_role("button", name=re.compile(r"submit( application)?", re.I))
    ).first
    if not btn.count():
        raise NeedsManual("Couldn't find the submit button.")
    btn.click()
    for _ in range(20):
        page.wait_for_timeout(1500)
        body = page.locator("body").inner_text()
        if CONFIRMATION.search(body) or re.search(r"confirmation|thank", page.url, re.I):
            return
        if VERIFICATION.search(body):
            raise NeedsManual("The ATS sent an email verification code.")
        if _captcha_challenge(page):
            raise NeedsManual("CAPTCHA challenge after submitting.")
    raise NeedsManual("Submitted, but no confirmation appeared. Check the screenshot.")


def apply(browser, job, resume_pdf, resume_tex, notes, description, profile, dry_run=False) -> str:
    """Fill (and unless dry_run, submit) one application.

    Returns a short detail string on success. Raises NeedsManual when a person
    has to finish it; any other exception is a failure.
    """
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    shot = os.path.join(SCREENSHOT_DIR, f"{job['job_id']}.png")
    context = browser.new_context(viewport={"width": 1280, "height": 1600}, locale="en-US")
    page = context.new_page()
    try:
        form_url = _find_form_url(page, job)
        page.goto(form_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        _open_form(page)
        if _captcha_challenge(page):
            raise NeedsManual("CAPTCHA challenge on page load.")

        fields = page.evaluate(EXTRACT_FIELDS_JS)
        _upload_resume(page, fields, resume_pdf)
        fields = page.evaluate(EXTRACT_FIELDS_JS)  # re-read after resume autofill

        plan = _answer_fields(fields, profile, resume_tex, notes, job, description)
        by_key = {f["key"]: f for f in fields}
        for ans in plan["answers"]:
            if ans["key"] in by_key:
                _fill(page, by_key[ans["key"]], ans["values"])

        missing = _missing_required(page.evaluate(EXTRACT_FIELDS_JS))
        page.screenshot(path=shot, full_page=True)
        if plan["unanswerable"]:
            raise NeedsManual("Can't answer: " + "; ".join(
                f'{by_key.get(u["key"], {}).get("label", u["key"])[:60]} ({u["why"]})'
                for u in plan["unanswerable"]
            ))
        if missing:
            raise NeedsManual("Required fields still empty: " + "; ".join(missing))
        if dry_run:
            return f"dry run: filled {len(plan['answers'])} fields, not submitted"

        _submit(page)
        page.screenshot(path=shot, full_page=True)
        return "submitted"
    except Exception:
        try:
            page.screenshot(path=shot, full_page=True)
        except Exception:
            pass
        raise
    finally:
        context.close()
