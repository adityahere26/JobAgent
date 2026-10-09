# jobagent

A small, free, personal job-search agent. It downloads jobs from company career boards, filters and
ranks them against **your** profile, and writes a markdown report. It never applies for you.

Everything specific to a profession (roles, skills, cities, companies, resume) lives in `config/`.
To use it for a different profession, change those files only. No code changes.

## Quick start

```bash
git clone <this repo> && cd jobagent
uv sync
uv run jobagent init            # creates config/profile.yaml, companies.yaml, resume.md
# edit the three files in config/
uv run jobagent fetch           # download jobs
uv run jobagent rank            # writes reports/latest.md
uv run jobagent show 12         # read one job
uv run jobagent mark 12 applied --note "sent on Monday"
```

## Finding boards
Add companies to `config/companies.yaml`. For Greenhouse, the token is the last part of the
company's board URL: `boards.greenhouse.io/<token>`.

## Jobs from LinkedIn/Naukri/company sites
jobagent never scrapes LinkedIn, Naukri or other sites whose terms forbid automation. If you find a
job there (or on a company's own careers page), copy the job text yourself and add it by hand:

```bash
uv run jobagent add --title "Associate Product Manager" --company "Acme" \
  --url "https://www.linkedin.com/jobs/view/123/" --location "Bengaluru" \
  --description-file job.txt      # or leave this out and paste the text, then Ctrl-D
```

The job goes through the same cleaning as fetched jobs (contact details removed, https links only),
so `rank`, `show`, `tailor`, `apply` and `mark` work on it. Hand-added jobs are kept until you
`mark` them; `fetch` never expires them. Adding the same URL again updates the job.

## Optional AI ranking
Without it, ranking is a free offline keyword score. To add an AI opinion on the top N jobs:
- **Private:** install [Ollama](https://ollama.com), pull a model, set `llm.provider: ollama`.
- **Cloud free tier:** `llm.provider: openai_compatible`, set `base_url`, `model`, and put the key in `.env`.
  Your profile and resume (emails and phones removed) are sent to that provider, and some free tiers
  use submitted data to improve their products. Use Ollama if that matters to you.

**Honest advice:** small local models (3B) rank worse than the free keyword score. On an 8 GB laptop
`qwen2.5:3b` gave nearly every job 20-45 and wrote reasons that contradicted the resume. Use
`jobagent rank --no-llm` unless you have 16 GB+ RAM (for a 7B+ model) or a strong cloud model.
`llm.weight` (default 0.3) sets how much the AI can move a score, and a job capped by a rule (for
example "asks for 4+ years, you have 1") stays capped whatever the AI says.

## Privacy and safety
- Your config, resume, database and reports are gitignored. Check with `git status` before pushing.
- Job text is treated as untrusted: contact details are stripped, it is never rendered as HTML, and it
  is fenced off from instructions in AI prompts.
- Only public job-board APIs are used. No LinkedIn/Naukri scraping.
- Jobs are marked expired only after a complete, trusted fetch.

## Limits (v0.1)
- Sources: Greenhouse and Lever, plus jobs you add by hand. Adding another is one file in `jobagent/sources/`.
- The Greenhouse and Lever adapters have been run against the live services; tests use offline fixtures shaped like their real responses.
- `jobagent tailor <id>` drafts a summary, picks your most relevant bullets (verbatim) and a cover note; numbers or names not found in your resume are flagged. Needs an LLM.
- `jobagent apply <id>` opens the application page in a visible browser and pre-fills name, email, phone, links, resume PDF and (if you ran `tailor`) the cover note, from `config/applicant.yaml`. It never clicks submit, never ticks checkboxes (consents, legal, diversity), and leaves unsure fields empty. You review and submit. Needs `uv sync --extra apply`.
- Many career sites (Workday, login walls, CAPTCHAs) will only be partly filled; that is expected.

## Development
`uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

MIT licensed.
