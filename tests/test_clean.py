import pytest

from jobagent.clean import (
    CONTACT_PLACEHOLDER as P,
)
from jobagent.clean import (
    clean_description,
    html_to_text,
    strip_contacts,
    truncate,
    validate_https_url,
)


def test_html_to_text_drops_script_and_contact_links():
    html = "<p>Hi</p><script>alert(1)</script><a href='mailto:a@b.com'>mail us</a><li>x</li>"
    text = html_to_text(html)
    assert "alert" not in text and "mail us" not in text and "- x" in text


@pytest.mark.parametrize(
    "raw",
    [
        "write to jane.doe@corp.com now",
        "jane [at] corp [dot] com",
        "call +91 98765 43210 today",
        "WhatsApp 9876543210",
        "reach 98765 43210",
    ],
)
def test_contacts_removed(raw):
    out = strip_contacts(raw)
    assert P in out and "9876" not in out and "@" not in out


def test_numbers_that_are_not_phones_survive():
    text = "Salary 1500000 per year, job code 20240101, 5 years experience"
    assert strip_contacts(text) == text


def test_truncate_on_word_boundary():
    out = truncate("word " * 100, 50)
    assert len(out) <= 50 and out.endswith("…")


def test_clean_description_strips_invisible_and_caps():
    out = clean_description("<p>a​b</p>" + "x " * 50_000)
    assert "​" not in out and len(out) <= 20_000


@pytest.mark.parametrize(
    "url", ["http://a.com/x", "https://u:p@a.com/", "javascript:alert(1)", "https://a.com/ x", ""]
)
def test_bad_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_https_url(url)


def test_good_url():
    assert validate_https_url(" https://a.com/x ") == "https://a.com/x"
