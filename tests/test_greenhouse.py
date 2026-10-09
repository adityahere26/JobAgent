import httpx
import pytest

from jobagent.sources import SourceError
from jobagent.sources.greenhouse import GreenhouseAdapter
from tests.helpers import gh_job, json_handler, mock_http


def test_fetch_ok(monkeypatch):
    payload = {"jobs": [gh_job(1)], "meta": {"total": 1}}
    mock_http(monkeypatch, json_handler(payload=payload))
    r = GreenhouseAdapter().fetch("acme")
    assert r.complete and len(r.listings) == 1


def test_total_mismatch_is_incomplete(monkeypatch):
    payload = {"jobs": [gh_job(1)], "meta": {"total": 5}}
    mock_http(monkeypatch, json_handler(payload=payload))
    assert not GreenhouseAdapter().fetch("acme").complete


def test_non_dict_entry_is_incomplete(monkeypatch):
    mock_http(monkeypatch, json_handler(payload={"jobs": [gh_job(1), "junk"]}))
    assert not GreenhouseAdapter().fetch("acme").complete


@pytest.mark.parametrize("status", [404, 500])
def test_http_errors(monkeypatch, status):
    mock_http(monkeypatch, json_handler(status=status, payload={}))
    with pytest.raises(SourceError):
        GreenhouseAdapter().fetch("acme")


def test_network_error_and_bad_shape(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("boom", request=request)

    mock_http(monkeypatch, refuse)
    with pytest.raises(SourceError):
        GreenhouseAdapter().fetch("acme")
    mock_http(monkeypatch, json_handler(payload={"nope": 1}))
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
