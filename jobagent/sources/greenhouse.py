"""Greenhouse public job-board API (documented, no key needed). One request per board."""

import html
import os
from typing import Any

import httpx

from jobagent import __version__
from jobagent.sources.base import FetchResult, SourceError

DEFAULT_BASE = "https://boards-api.greenhouse.io"
TIMEOUT = 15.0
# location.name values that carry no information ("N/A", "na", "LOCATION" seen on live boards).
_PLACEHOLDER_LOCATIONS = {"", "n/a", "na", "location"}


class GreenhouseAdapter:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url or os.environ.get("JOBAGENT_GREENHOUSE_BASE_URL") or DEFAULT_BASE
        ).rstrip("/")

    def fetch(self, token: str) -> FetchResult:
        url = f"{self.base_url}/v1/boards/{token}/jobs"
        headers = {"User-Agent": f"jobagent/{__version__} (personal job search tool)"}
        try:
            resp = httpx.get(url, params={"content": "true"}, headers=headers, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise SourceError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceError(f"board returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError("board returned invalid JSON") from exc
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceError("unexpected response shape")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        total = meta.get("total")
        total = total if isinstance(total, int) else None
        listings = [j for j in jobs if isinstance(j, dict)]
        complete = len(listings) == len(jobs) and (total is None or total == len(jobs))
        return FetchResult(listings=listings, complete=complete, reported_total=total)

    def external_id(self, raw: dict[str, Any]) -> str | None:
        value = raw.get("id")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        text = str(value).strip()
        return text or None

    def location_text(self, raw: dict[str, Any]) -> str | None:
        """location.name, or the office names when that is a placeholder like "N/A".

        Seen on live boards: location.name "N/A" with offices [{"name": "India Locations"}].
        Offices are only a fallback, so a "Remote - US" job with a Bengaluru office stays US.
        """
        location = raw.get("location")
        name = location.get("name") if isinstance(location, dict) else None
        name = name.strip() if isinstance(name, str) else ""
        if name.lower() not in _PLACEHOLDER_LOCATIONS:
            return name
        offices = raw.get("offices")
        names = [
            o["name"].strip()
            for o in (offices if isinstance(offices, list) else [])
            if isinstance(o, dict) and isinstance(o.get("name"), str) and o["name"].strip()
        ]
        return "; ".join(names) or None

    def to_job_fields(self, raw: dict[str, Any], company: str) -> dict[str, Any]:
        departments = raw.get("departments")
        department = None
        if isinstance(departments, list) and departments and isinstance(departments[0], dict):
            department = departments[0].get("name")
        return {
            "external_id": self.external_id(raw),
            "title": raw.get("title"),
            "company": company,
            "department": department,
            "location_text": self.location_text(raw),
            # Greenhouse returns the HTML escaped (&lt;p&gt;), so unescape once before cleaning.
            "description": html.unescape(str(raw.get("content") or "")),
            "apply_url": raw.get("absolute_url"),
            "posted_at": raw.get("first_published") or raw.get("updated_at"),
            "source_updated_at": raw.get("updated_at"),
        }
