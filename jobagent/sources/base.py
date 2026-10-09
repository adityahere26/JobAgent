"""What every source adapter must provide."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class SourceError(Exception):
    """A fetch failed. The message never contains response bodies."""


@dataclass
class FetchResult:
    """Raw listings plus whether the fetch is trustworthy enough to expire missing jobs."""

    listings: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    reported_total: int | None = None


class SourceAdapter(Protocol):
    def fetch(self, token: str) -> FetchResult: ...

    def to_job_fields(self, raw: dict[str, Any], company: str) -> dict[str, Any]:
        """Map one raw listing to JobIn field names. May raise on bad data."""
        ...

    def external_id(self, raw: dict[str, Any]) -> str | None:
        """The listing's id if readable, else None."""
        ...
