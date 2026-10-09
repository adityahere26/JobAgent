import pytest
from pydantic import ValidationError

from jobagent.models import JobIn, normalize_timestamp


def make(**over):
    base = {
        "external_id": 7, "title": " Backend\nEngineer ", "company": "Acme",
        "location_text": "Bangalore", "description": "<p>mail a@b.com</p>",
        "apply_url": "https://x.example/7", "posted_at": "2026-05-01T10:00:00-04:00",
    }  # fmt: skip
    base.update(over)
    return JobIn.model_validate(base)


def test_normalises_everything():
    j = make()
    assert j.external_id == "7" and j.title == "Backend Engineer"
    assert "a@b.com" not in j.description and j.cities == ["Bengaluru"]
    assert j.posted_at == "2026-05-01T14:00:00+00:00"


def test_rejects_http_and_missing_title():
    with pytest.raises(ValidationError):
        make(apply_url="http://x.example")
    with pytest.raises(ValidationError):
        make(title="  ")


def test_hash_changes_with_content_only():
    assert make().content_hash == make(posted_at=None).content_hash
    assert make().content_hash != make(description="other").content_hash


def test_bad_timestamp_is_none():
    assert normalize_timestamp("not a date") is None
