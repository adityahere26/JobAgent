"""The one validated shape every job takes before it is stored."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from jobagent.clean import clean_description, clean_line, validate_https_url
from jobagent.locations import normalize_location


def normalize_timestamp(value: Any) -> str | None:
    """Any ISO-ish timestamp to UTC ISO string; None if missing or unreadable."""
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    title: str
    company: str
    department: str | None = None
    location_text: str | None = None
    description: str = ""
    apply_url: str
    posted_at: str | None = None
    source_updated_at: str | None = None
    # derived
    cities: list[str] = []
    countries: list[str] = []
    is_remote: bool = False

    @field_validator("external_id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str:
        text = str(value).strip() if value is not None else ""
        if not text or len(text) > 200:
            raise ValueError("external_id is missing or too long")
        return text

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: Any) -> str:
        text = clean_line(str(value or ""), 300)
        if not text:
            raise ValueError("title is empty")
        return text

    @field_validator("company", mode="before")
    @classmethod
    def _company(cls, value: Any) -> str:
        text = clean_line(str(value or ""), 200)
        if not text:
            raise ValueError("company is empty")
        return text

    @field_validator("department", "location_text", mode="before")
    @classmethod
    def _optional_line(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return clean_line(str(value), 300) or None

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: Any) -> str:
        return clean_description(str(value or ""))

    @field_validator("apply_url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> str:
        return validate_https_url(str(value or ""))

    @field_validator("posted_at", "source_updated_at", mode="before")
    @classmethod
    def _ts(cls, value: Any) -> str | None:
        return normalize_timestamp(value)

    @model_validator(mode="after")
    def _derive_location(self) -> "JobIn":
        info = normalize_location(self.location_text)
        self.cities, self.countries, self.is_remote = info.cities, info.countries, info.is_remote
        return self

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [
                self.title,
                self.company,
                self.department,
                self.location_text,
                self.description,
                self.apply_url,
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
