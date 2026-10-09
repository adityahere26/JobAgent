"""Pre-fill an application form in a visible browser. The human reviews and submits.

Hard rules: this module never clicks a submit/apply button, never ticks checkboxes or radio
buttons (consents, legal and diversity questions are yours to answer), and leaves any field it is
not sure about empty.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from jobagent.config import ConfigError, Paths

# Ordered: the first rule whose pattern matches a field's label text wins.
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("first_name", re.compile(r"first\s*name|given\s*name|\bfname\b", re.I)),
    ("last_name", re.compile(r"last\s*name|sur\s*name|family\s*name|\blname\b", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|contact\s*number", re.I)),
    ("linkedin", re.compile(r"linked\s*in", re.I)),
    ("github", re.compile(r"git\s*hub", re.I)),
    ("portfolio", re.compile(r"portfolio|personal\s*(web)?site|website", re.I)),
    ("location", re.compile(r"current\s*(location|city)|\blocation\b|\bcity\b", re.I)),
    ("cover_note", re.compile(r"cover\s*letter|why .*(join|want|interested)", re.I)),
    ("full_name", re.compile(r"^\W*(full\s*)?name\W*$", re.I)),
]
SKIP_TYPES = {"password", "checkbox", "radio", "submit", "button", "hidden", "image", "reset"}


class Applicant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    resume_pdf: str = ""

    @field_validator("linkedin", "github", "portfolio")
    @classmethod
    def _https(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("must be a full https:// URL")
        return v

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def load_applicant(paths: Paths) -> Applicant:
    path = paths.config_dir / "applicant.yaml"
    if not path.is_file():
        raise ConfigError("applicant.yaml not found. Run `jobagent init` and fill it in.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Applicant.model_validate(data)
    except (yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"applicant.yaml is invalid: {type(exc).__name__}") from exc


def match_field(label_text: str) -> str | None:
    for key, pattern in RULES:
        if pattern.search(label_text):
            return key
    return None


def values_for(applicant: Applicant, cover_note: str = "") -> dict[str, str]:
    values = applicant.model_dump()
    values["full_name"] = applicant.full_name
    values["cover_note"] = cover_note
    return {k: v for k, v in values.items() if v}


def read_cover_note(paths: Paths, job_id: int) -> str:
    """The cover note section from `jobagent tailor`, if it exists."""
    path = paths.reports_dir / "tailored" / f"job-{job_id}.md"
    if not path.is_file():
        return ""
    m = re.search(r"## Cover note\n(.*?)(?:\n_Review|\Z)", path.read_text(encoding="utf-8"), re.S)
    return m.group(1).strip() if m else ""


_FIELD_JS = """
() => [...document.querySelectorAll('input, textarea')].map((el, i) => {
  const label = el.labels && el.labels.length ? [...el.labels].map(l => l.innerText).join(' ') : '';
  el.setAttribute('data-ja-idx', String(i));
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  return {idx: i, tag: el.tagName.toLowerCase(), type: type,
    text: [label, el.getAttribute('aria-label'), el.getAttribute('placeholder'),
           el.getAttribute('name'), el.id].filter(Boolean).join(' '),
    visible: !!(el.offsetWidth || el.offsetHeight), filled: !!el.value};
})
"""


def prefill(page: Any, values: dict[str, str], resume_pdf: str = "") -> tuple[list[str], list[str]]:
    """Fill what we are sure about. Returns (filled labels, fields left for the human)."""
    filled: list[str] = []
    left: list[str] = []
    for f in page.evaluate(_FIELD_JS):
        if not f["visible"] and f["type"] != "file":
            continue
        if f["type"] in SKIP_TYPES:
            continue
        el = page.locator(f'[data-ja-idx="{f["idx"]}"]')
        if f["type"] == "file":
            if resume_pdf and re.search(r"resume|cv", f["text"], re.I):
                el.set_input_files(resume_pdf)
                filled.append("resume file")
            continue
        key = match_field(f["text"])
        if key and key in values and not f["filled"]:
            el.fill(values[key])
            filled.append(key)
        elif not f["filled"]:
            left.append((f["text"] or "unlabelled field")[:40].strip())
    return filled, left


def run(url: str, values: dict[str, str], resume_pdf: str) -> tuple[list[str], list[str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError("Install the browser helper first: uv sync --extra apply") from exc
    if resume_pdf and not Path(resume_pdf).is_file():
        raise ConfigError("resume_pdf in applicant.yaml does not point to a file")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_load_state("domcontentloaded")
        result = prefill(page, values, resume_pdf)
        print("Review the form in the browser, answer the rest, and submit it yourself.")
        print("Close the browser window when you are done.")
        page.wait_for_event("close", timeout=0)
        browser.close()
    return result
