"""Lever public postings API (documented, no key needed). One request per board.

The response is a bare JSON list of every published posting; there is no total to check against,
so a fetch is complete only when the body is a list made entirely of posting objects.
"""

import html
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from jobagent import __version__
from jobagent.sources.base import FetchResult, SourceError

DEFAULT_BASE = "https://api.lever.co"
TIMEOUT = 15.0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class LeverAdapter:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url or os.environ.get("JOBAGENT_LEVER_BASE_URL") or DEFAULT_BASE
        ).rstrip("/")

    def fetch(self, token: str) -> FetchResult:
        url = f"{self.base_url}/v0/postings/{token}"
        headers = {"User-Agent": f"jobagent/{__version__} (personal job search tool)"}
        try:
            resp = httpx.get(url, params={"mode": "json"}, headers=headers, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise SourceError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceError(f"board returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError("board returned invalid JSON") from exc
        if not isinstance(data, list):
            raise SourceError("unexpected response shape")
        listings = [p for p in data if isinstance(p, dict)]
        return FetchResult(listings=listings, complete=len(listings) == len(data))

    def external_id(self, raw: dict[str, Any]) -> str | None:
        return _text(raw.get("id")) or None

    def location_text(self, raw: dict[str, Any]) -> str | None:
        """All listed locations, plus "Remote" and the ISO country when Lever gives them.

        Seen live: Zeta's "East Coast" with workplaceType "remote" and country "US" becomes
        "East Coast; Remote; US", so it is not mistaken for a remote job open to India.
        """
        categories = raw.get("categories")
        categories = categories if isinstance(categories, dict) else {}
        all_locations = categories.get("allLocations")
        parts = [_text(loc) for loc in all_locations] if isinstance(all_locations, list) else []
        parts = [p for p in parts if p] or [p for p in [_text(categories.get("location"))] if p]
        if _text(raw.get("workplaceType")).lower() == "remote":
            parts.append("Remote")
        country = _text(raw.get("country"))
        if len(country) == 2 and country.isalpha():
            parts.append(country.upper())
        return "; ".join(dict.fromkeys(parts)) or None

    def description_html(self, raw: dict[str, Any]) -> str:
        """description + each requirements list + closing text. Lever sends raw (unescaped) HTML."""
        chunks = [_text(raw.get("description"))]
        lists = raw.get("lists")
        for item in lists if isinstance(lists, list) else []:
            if isinstance(item, dict):
                heading = html.escape(_text(item.get("text")))
                chunks.append(f"<h3>{heading}</h3><ul>{_text(item.get('content'))}</ul>")
        chunks.append(_text(raw.get("additional")))
        return "\n".join(c for c in chunks if c)

    def to_job_fields(self, raw: dict[str, Any], company: str) -> dict[str, Any]:
        categories = raw.get("categories")
        categories = categories if isinstance(categories, dict) else {}
        created = raw.get("createdAt")
        posted_at = None
        if isinstance(created, int) and not isinstance(created, bool) and created > 0:
            posted_at = datetime.fromtimestamp(created / 1000, UTC).isoformat()
        return {
            "external_id": self.external_id(raw),
            "title": raw.get("text"),
            "company": company,
            "department": _text(categories.get("department")) or _text(categories.get("team")),
            "location_text": self.location_text(raw),
            "description": self.description_html(raw),
            # applyUrl opens the form itself, which is what `jobagent apply` pre-fills.
            "apply_url": raw.get("applyUrl") or raw.get("hostedUrl"),
            "posted_at": posted_at,
            "source_updated_at": None,  # Lever's public API has no updated timestamp
        }
