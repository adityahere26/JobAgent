import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from jobagent.models import JobIn
from jobagent.sources import ADAPTERS, SourceError
from jobagent.sources.lever import LeverAdapter

# Field names and types copied from live api.lever.co responses; text shortened and made up.
POSTINGS = json.loads((Path(__file__).parent / "fixtures/lever/postings.json").read_text())


def fake_get(status=200, payload=None, raises=None, seen=None):
    def get(url, **kw):
        if seen is not None:
            seen.append((url, kw.get("params")))
        if raises:
            raise raises
        return httpx.Response(
            status, content=json.dumps(payload), request=httpx.Request("GET", url)
        )

    return get


def job(index: int) -> JobIn:
    adapter = LeverAdapter()
    return JobIn.model_validate(adapter.to_job_fields(POSTINGS[index], "Acme"))


def test_registered():
    assert ADAPTERS["lever"] is LeverAdapter


def test_fetch_full_list_is_complete(monkeypatch):
    seen = []
    monkeypatch.setattr(httpx, "get", fake_get(payload=POSTINGS, seen=seen))
    r = LeverAdapter().fetch("acme")
    assert r.complete and len(r.listings) == 4 and r.reported_total is None
    assert seen == [("https://api.lever.co/v0/postings/acme", {"mode": "json"})]


def test_non_dict_entry_is_incomplete(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(payload=[POSTINGS[0], "junk"]))
    r = LeverAdapter().fetch("acme")
    assert not r.complete and len(r.listings) == 1


@pytest.mark.parametrize("status", [404, 500])
def test_http_errors(monkeypatch, status):
    # Lever answers an unknown company with 404 {"ok": false, "error": "Document not found"}.
    monkeypatch.setattr(httpx, "get", fake_get(status=status, payload={"ok": False}))
    with pytest.raises(SourceError):
        LeverAdapter().fetch("acme")


def test_network_error_and_bad_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(raises=httpx.ConnectError("boom")))
    with pytest.raises(SourceError):
        LeverAdapter().fetch("acme")
    monkeypatch.setattr(httpx, "get", fake_get(payload={"ok": False, "error": "x"}))
    with pytest.raises(SourceError):
        LeverAdapter().fetch("acme")


def test_mapping_onsite_india():
    j = job(0)
    assert j.external_id == "7d9af9b5-c1c7-48ec-bbb5-9b25e49f6596"
    assert j.title == "Product Manager I" and j.department == "Product"
    assert j.location_text == "Bangalore, Karnataka; IN"
    assert j.cities == ["Bengaluru"] and j.countries == ["IN"] and not j.is_remote
    assert j.apply_url.endswith("/apply")
    assert j.posted_at == "2025-09-15T06:02:29+00:00"
    assert j.source_updated_at is None


def test_description_includes_lists_and_is_cleaned():
    d = job(0).description
    assert "Write PRDs & run A/B tests" in d and "1-3 years of product experience" in d
    assert "What you will need" in d and "About us" in d
    assert "<" not in d and "&nbsp;" not in d
    assert "jobs@example.com" not in d  # contact details stripped by JobIn


def test_remote_india_multi_city():
    j = job(1)
    assert j.is_remote and j.countries == ["IN"]
    assert {"Noida", "Bengaluru", "Delhi"} <= set(j.cities)


def test_remote_us_keeps_country():
    j = job(2)
    assert j.location_text == "East Coast; Remote; US"
    assert j.is_remote and j.countries == ["US"]


def test_missing_all_locations_falls_back_to_location():
    j = job(3)
    assert j.location_text == "Hyderabad/Bangalore; IN"
    assert j.cities == ["Hyderabad", "Bengaluru"]


def test_https_only_apply_url():
    raw = dict(POSTINGS[0], applyUrl="http://jobs.lever.co/acme/x/apply")
    with pytest.raises(ValidationError):
        JobIn.model_validate(LeverAdapter().to_job_fields(raw, "Acme"))
    raw = dict(POSTINGS[0], applyUrl=None)
    assert LeverAdapter().to_job_fields(raw, "Acme")["apply_url"] == POSTINGS[0]["hostedUrl"]


def test_bad_fields_do_not_crash():
    a = LeverAdapter()
    assert a.external_id({"id": None}) is None and a.external_id({"id": 5}) is None
    raw = {"id": "x", "text": "PM", "categories": "junk", "lists": "junk", "createdAt": True}
    fields = a.to_job_fields(raw, "Acme")
    assert fields["location_text"] is None and fields["posted_at"] is None
    assert fields["description"] == ""


def test_double_escaped_entities_are_fixed():
    # Live Paytm postings send "Scale &amp;amp; Transformation" in their HTML.
    raw = dict(POSTINGS[1], description="<div>Scale &amp;amp; Transformation &amp;lt;3</div>")
    d = JobIn.model_validate(LeverAdapter().to_job_fields(raw, "Acme")).description
    assert "Scale & Transformation" in d and "&amp;" not in d
