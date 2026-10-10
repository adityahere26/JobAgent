"""Read-only Gmail reply tracker.

Reads message headers and Gmail's short snippet for the last 30 days, matches them to jobs you
marked "applied", and suggests a status with keyword rules. No LLM, and no email text leaves this
machine or is stored: only the message id, date, sender domain and the suggested status are kept.

The OAuth scope is gmail.readonly and nothing else. The token lives in data/ with mode 600.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol

import httpx

from jobagent import store
from jobagent.config import ConfigError, Paths
from jobagent.filters import contains_phrase

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
DAYS = 30
MAX_MESSAGES = 500
TOKEN_FILE = "gmail_token.json"  # noqa: S105 (a file name, not a secret)
CLIENT_SECRET_FILE = "client_secret.json"  # noqa: S105 (a file name, not a secret)
# Jobs whose replies are watched: applied, and interview (an offer or rejection can still come).
WATCHED = ("applied", "interview")

# First match wins, so the more decisive outcomes are checked first.
RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "offer",
        re.compile(
            r"offer letter|pleased to offer|extend (you )?an offer|offer of employment|"
            r"job offer|formal offer",
            re.I,
        ),
    ),
    (
        "rejected",
        re.compile(
            r"unfortunately|not (to )?(be )?mov(e|ing) forward|other candidates|"
            r"decided not to|regret to inform|position has been filled|not be progressing|"
            r"not selected|will not be proceeding|no longer under consideration",
            re.I,
        ),
    ),
    (
        "interview",
        re.compile(
            r"interview|phone screen|schedule a (call|chat|time)|your availability|"
            r"next round|assessment|take-?home|assignment|calendly|shortlisted",
            re.I,
        ),
    ),
]

# Words dropped from company names before matching ("Northwind Labs Pvt Ltd" -> "northwind").
SUFFIXES = {
    "inc", "llc", "ltd", "limited", "pvt", "private", "plc", "gmbh", "corp", "corporation", "co",
    "company", "technologies", "technology", "tech", "labs", "lab", "software", "solutions",
    "systems", "india", "global", "group", "hq", "the",
}  # fmt: skip
# Generic mail and applicant-tracking domains: a message from these says nothing by its domain.
GENERIC_DOMAINS = {
    "gmail", "googlemail", "outlook", "hotmail", "yahoo", "greenhouse", "greenhouse-mail",
    "lever", "hire", "ashbyhq", "workday", "myworkday", "smartrecruiters", "icims", "linkedin",
    "naukri", "mail", "email", "noreply", "no-reply", "notifications", "careers", "jobs",
}  # fmt: skip


def classify(subject: str, snippet: str) -> str:
    """interview / rejected / offer / other, from the subject and Gmail's short snippet."""
    text = f"{subject}\n{snippet}"
    for status, pattern in RULES:
        if pattern.search(text):
            return status
    return "other"


def sender_domain(from_header: str) -> str:
    _name, addr = parseaddr(from_header)
    return addr.rpartition("@")[2].lower().strip(".") if "@" in addr else ""


def company_words(company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", company.casefold())
    core = [w for w in words if w not in SUFFIXES]
    return core or words


def domain_matches(domain: str, company: str) -> bool:
    """northwind.example or mail.northwindlabs.example match "Northwind Labs"."""
    if not domain:
        return False
    keys = set()
    for words in (company_words(company), re.findall(r"[a-z0-9]+", company.casefold())):
        keys |= {"".join(words[: i + 1]) for i in range(len(words))}  # northwind, northwindlabs
    keys = {k for k in keys if len(k) >= 4 and k not in SUFFIXES}
    labels = [lbl.replace("-", "") for lbl in domain.split(".")[:-1]]  # drop the TLD
    return any(lbl in keys for lbl in labels if lbl not in GENERIC_DOMAINS)


def name_matches(text: str, company: str) -> bool:
    phrase = " ".join(company_words(company))
    return len(phrase) >= 3 and contains_phrase(text, phrase)


@dataclass
class Message:
    """One email, held in memory only while matching. Never stored or logged."""

    id: str
    received_on: str
    from_header: str
    subject: str
    snippet: str


def match_jobs(msg: Message, applied: list[store.Job]) -> list[store.Job]:
    """Applied jobs this message is about: by sender domain, or company name in sender/subject.

    If several jobs at one company match and the subject names one title, only that one is kept.
    """
    domain = sender_domain(msg.from_header)
    name, _addr = parseaddr(msg.from_header)
    hits = [
        j
        for j in applied
        if domain_matches(domain, j.company)
        or name_matches(name, j.company)
        or name_matches(msg.subject, j.company)
    ]
    titled = [j for j in hits if contains_phrase(msg.subject, j.title)]
    return titled or hits


def suggest(conn: sqlite3.Connection, messages: list[Message]) -> list[store.Suggestion]:
    """Turn new messages into stored suggestions for watched jobs. Returns the new ones."""
    statuses = store.get_statuses(conn)
    applied = [j for jid, s in statuses.items() if s in WATCHED if (j := store.get_job(conn, jid))]
    if not applied:
        return []
    known = store.known_message_ids(conn)
    new: list[store.Suggestion] = []
    for msg in messages:
        if msg.id in known:
            continue
        status = classify(msg.subject, msg.snippet)
        for job in match_jobs(msg, applied):
            s = store.Suggestion(
                msg.id, job.id, msg.received_on, sender_domain(msg.from_header), status, job
            )
            if store.save_suggestion(conn, s):
                new.append(s)
    return new


# --- Gmail API (read-only) ---------------------------------------------------------------------


class Fetcher(Protocol):
    def recent_messages(self) -> list[Message]: ...


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []):
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _received_on(internal_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(internal_ms) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError):
        return ""


class GmailClient:
    """Lists recent messages with headers only (format=metadata). Never downloads bodies."""

    def __init__(self, access_token: str, timeout: float = 20.0) -> None:
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.timeout = timeout

    def _get(self, client: httpx.Client, url: str, params: Any) -> dict[str, Any]:
        r = client.get(url, params=params, headers=self.headers)
        if r.status_code in (401, 403):
            raise ConfigError("Gmail refused access. Run `jobagent mail setup` again.")
        r.raise_for_status()
        return r.json()

    def recent_messages(self) -> list[Message]:
        out: list[Message] = []
        with httpx.Client(timeout=self.timeout) as client:
            ids: list[str] = []
            page: str | None = None
            while len(ids) < MAX_MESSAGES:
                params = {"q": f"newer_than:{DAYS}d -in:sent -in:chats", "maxResults": 100}
                if page:
                    params["pageToken"] = page
                data = self._get(client, f"{API}/messages", params)
                ids += [m["id"] for m in data.get("messages", [])]
                page = data.get("nextPageToken")
                if not page:
                    break
            for mid in ids[:MAX_MESSAGES]:
                params = [
                    ("format", "metadata"),
                    ("metadataHeaders", "From"),
                    ("metadataHeaders", "Subject"),
                ]
                m = self._get(client, f"{API}/messages/{mid}", params)
                payload = m.get("payload", {})
                out.append(
                    Message(
                        id=str(m.get("id", mid)),
                        received_on=_received_on(m.get("internalDate")),
                        from_header=_header(payload, "From"),
                        subject=_header(payload, "Subject"),
                        snippet=str(m.get("snippet", "")),
                    )
                )
        return out


# --- OAuth token ---------------------------------------------------------------------------------


def token_path(paths: Paths) -> Path:
    return paths.data_dir / TOKEN_FILE


def client_secret_path(paths: Paths) -> Path:
    return paths.config_dir / CLIENT_SECRET_FILE


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read (mode 600), even if it already existed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def check_scopes(granted: Any) -> None:
    """Refuse a token with any scope other than gmail.readonly."""
    scopes = set(granted or [])
    if scopes != {SCOPE}:
        raise ConfigError(f"The Gmail token must have only the {SCOPE} scope. Run setup again.")


def _google():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise ConfigError("Install the Gmail helper first: uv sync --extra mail") from exc
    return InstalledAppFlow, Credentials, Request, RefreshError


def setup(paths: Paths) -> Path:
    """Run Google's desktop OAuth flow in the browser and save a read-only token."""
    secret = client_secret_path(paths)
    if not secret.is_file():
        raise ConfigError(
            "config/client_secret.json not found. See 'Gmail reply tracker' in the README."
        )
    InstalledAppFlow, _c, _r, _e = _google()
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes=[SCOPE])
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, timeout_seconds=300)
    check_scopes(creds.scopes)
    out = token_path(paths)
    write_private(out, creds.to_json())
    return out


def access_token(paths: Paths) -> str:
    """A fresh access token from the saved token, refreshed and re-saved if needed."""
    path = token_path(paths)
    if not path.is_file():
        raise ConfigError("No Gmail token yet. Run `jobagent mail setup` first.")
    os.chmod(path, 0o600)
    check_scopes(json.loads(path.read_text(encoding="utf-8")).get("scopes"))
    _f, Credentials, Request, RefreshError = _google()
    creds = Credentials.from_authorized_user_file(str(path), [SCOPE])
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise ConfigError(
                "The Gmail login expired (unverified apps must log in again every 7 days). "
                "Run `jobagent mail setup`."
            ) from exc
        write_private(path, creds.to_json())
    return creds.token
