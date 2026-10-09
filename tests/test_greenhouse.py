import json

import httpx
import pytest

from jobagent.sources import SourceError
from jobagent.sources.greenhouse import GreenhouseAdapter
from tests.helpers import gh_job


def fake_get(status=200, payload=None, raises=None):
    def get(url, **kw):
        if raises:
            raise raises
        return httpx.Response(
            status, content=json.dumps(payload), request=httpx.Request("GET", url)
        )

    return get


def test_fetch_ok(monkeypatch):
    payload = {"jobs": [gh_job(1)], "meta": {"total": 1}}
    monkeypatch.setattr(httpx, "get", fake_get(payload=payload))
    r = GreenhouseAdapter().fetch("acme")
    assert r.complete and len(r.listings) == 1


def test_total_mismatch_is_incomplete(monkeypatch):
    payload = {"jobs": [gh_job(1)], "meta": {"total": 5}}
    monkeypatch.setattr(httpx, "get", fake_get(payload=payload))
    assert not GreenhouseAdapter().fetch("acme").complete


def test_non_dict_entry_is_incomplete(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(payload={"jobs": [gh_job(1), "junk"]}))
    assert not GreenhouseAdapter().fetch("acme").complete


@pytest.mark.parametrize("status", [404, 500])
def test_http_errors(monkeypatch, status):
    monkeypatch.setattr(httpx, "get", fake_get(status=status, payload={}))
    with pytest.raises(SourceError):
        GreenhouseAdapter().fetch("acme")


def test_network_error_and_bad_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get(raises=httpx.ConnectError("boom")))
    with pytest.raises(SourceError):
        GreenhouseAdapter().fetch("acme")
    monkeypatch.setattr(httpx, "get", fake_get(payload={"nope": 1}))
    with pytest.raises(SourceError):
        GreenhouseAdapter().fetch("acme")


def test_mapping_unescapes_content():
    fields = GreenhouseAdapter().to_job_fields(
        gh_job(3, content="&lt;p&gt;Hello&lt;/p&gt;"), "Acme"
    )
    assert fields["description"] == "<p>Hello</p>" and fields["external_id"] == "3"
    assert fields["department"] == "Eng"


# Shapes below were seen on live boards (Stripe, Databricks), not only in the documented format.
@pytest.mark.parametrize(
    ("name", "offices", "expected"),
    [
        ("N/A", [{"id": 1, "name": "India Locations", "location": None}], "India Locations"),
        ("LOCATION", [{"name": "US"}, {"name": "Canada Locations"}], "US; Canada Locations"),
        ("na", [], None),
        ("Belgrade, Serbia", [{"name": "Amsterdam, Netherlands"}], "Belgrade, Serbia"),
        ("Remote - US", [{"name": "Bengaluru"}], "Remote - US"),
    ],
)
def test_placeholder_location_falls_back_to_offices(name, offices, expected):
    raw = gh_job(4, loc=name, offices=offices)
    assert GreenhouseAdapter().to_job_fields(raw, "Acme")["location_text"] == expected


def test_missing_location_and_bad_offices():
    raw = gh_job(5, location=None, offices=[None, {"name": 7}, {"name": "Pune"}])
    assert GreenhouseAdapter().to_job_fields(raw, "Acme")["location_text"] == "Pune"
