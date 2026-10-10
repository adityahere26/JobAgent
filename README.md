# jobagent

A small, free, personal job-search agent. It downloads jobs from company career boards, filters and
ranks them against **your** profile, and writes a markdown report. It never applies for you.

Everything specific to a profession (roles, skills, cities, companies, resume) lives in `config/`.
To use it for a different profession, change those files only. No code changes.

## Quick start for a friend

```bash
git clone https://github.com/adityahere26/JobAgent.git jobagent && cd jobagent && uv sync
uv run jobagent init                # makes your private config files (gitignored)
# edit config/profile.yaml, config/companies.yaml and config/resume.md for YOUR job hunt
uv run jobagent fetch               # download jobs from the boards you listed
uv run jobagent rank --no-llm       # free offline scoring -> reports/latest.md
uv run jobagent show 12             # read one job; then: uv run jobagent mark 12 applied
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

## Dashboard

```bash
uv sync --extra ui          # add --extra apply for the "pre-fill the form" button
uv run jobagent ui          # opens http://127.0.0.1:8000  (--port 8001, --no-browser)
```

Browse and filter your matches, mark statuses in one click, read a job, prepare a tailoring
prompt and check the model's reply, add a job by hand, see your tracker, and refresh boards with
live progress. It listens on 127.0.0.1 only, loads nothing from the internet, and never submits
an application: "pre-fill" opens the form for you to finish and send.

### Apply queue (you submit)

Tick up to 10 jobs on the dashboard and press **Apply queue**. One browser window opens each form
in turn and pre-fills what it is sure of from `config/applicant.yaml` and `config/answers.yaml`
(notice period, expected salary, relocation, work authorisation and so on: copy
`config/answers.example.yaml`; empty answers are left for you). Check every field, submit it
yourself, then press **I submitted it** (or **Skip**) and the next one opens. Closing the tab
skips it, and a site's own "application received" page counts as submitted.

It never presses Submit, never ticks consent or other checkboxes, never touches dropdowns, and
stops with a message if a CAPTCHA or a sign-in page appears.

## Gmail reply tracker (optional, read-only)

`jobagent mail check` reads the last 30 days of your Gmail, finds replies to jobs you marked
`applied` or `interview` (by company name or the sender's domain), and suggests a status:
interview, rejected, offer or other. Simple keyword rules, no AI, and no email text is sent
anywhere or saved: only the message id, date, sender domain and the suggestion are stored. You
confirm each one in the dashboard Tracker.

One-time setup (about 10 minutes):

1. Open <https://console.cloud.google.com/>, create a project (any name).
2. **APIs & Services → Library**: enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen**: choose **External**, fill in an app name and your
   email, add the scope `https://www.googleapis.com/auth/gmail.readonly` only, and add your own
   Gmail address under **Test users**. Leave the app in **Testing**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Desktop app**.
   Download the JSON and save it as `config/client_secret.json` (gitignored; never share it).
5. Run:

   ```bash
   uv sync --extra mail
   uv run jobagent mail setup   # browser opens; Google warns the app is unverified: that is you
   uv run jobagent mail check
   ```

The token is saved in `data/gmail_token.json` (gitignored, readable only by you). Because the app
is unverified and in Testing, **Google ends the login after 7 days**; when `mail check` says so,
run `jobagent mail setup` again. To revoke access at any time:
<https://myaccount.google.com/permissions>.

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
- `jobagent tailor <id>` drafts a summary, picks your most relevant bullets (verbatim) and a cover note; numbers or names not found in your resume are flagged. Needs an LLM, or use `jobagent tailor <id> --prompt` to get a prompt you paste into any chat model, then `jobagent tailor <id> --import reply.txt` to check its reply the same way (no API, no network).
- `jobagent apply <id>` opens the application page in a visible browser and pre-fills name, email, phone, links, resume PDF and (if you ran `tailor`) the cover note, from `config/applicant.yaml`. It never clicks submit, never ticks checkboxes (consents, legal, diversity), and leaves unsure fields empty. You review and submit. Needs `uv sync --extra apply`.
- Many career sites (Workday, login walls, CAPTCHAs) will only be partly filled; that is expected.

## Development
`uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

MIT licensed.
