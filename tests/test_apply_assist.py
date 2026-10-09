import pytest

from jobagent.apply_assist import (
    Applicant,
    match_field,
    prefill,
    read_cover_note,
    values_for,
)
from jobagent.config import Paths


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("First Name *", "first_name"),
        ("Email address", "email"),
        ("Mobile number", "phone"),
        ("LinkedIn Profile", "linkedin"),
        ("Why do you want to join us?", "cover_note"),
        ("Full name", "full_name"),
        ("Are you authorized to work in India?", None),
        ("Expected salary", None),
    ],
)
def test_match_field(label, key):
    assert match_field(label) == key


def test_applicant_validation_and_values():
    with pytest.raises(ValueError):
        Applicant(linkedin="http://x")
    a = Applicant(first_name="Asha", last_name="Rao", email="a@x.com")
    v = values_for(a, "Hello")
    assert v["full_name"] == "Asha Rao" and v["cover_note"] == "Hello" and "phone" not in v


def test_cover_note_read(tmp_path):
    p = Paths(tmp_path)
    (p.reports_dir / "tailored").mkdir(parents=True)
    (p.reports_dir / "tailored/job-3.md").write_text("## Cover note\nHi team.\n\n_Review all._\n")
    assert read_cover_note(p, 3) == "Hi team."
    assert read_cover_note(p, 4) == ""


class FakeLoc:
    def __init__(self, log, idx):
        self.log, self.idx = log, idx

    def fill(self, v):
        self.log.append(("fill", self.idx, v))

    def set_input_files(self, v):
        self.log.append(("file", self.idx, v))

    def click(self):  # pragma: no cover
        raise AssertionError("must never click")


class FakePage:
    def __init__(self, fields):
        self.fields, self.log = fields, []

    def evaluate(self, _js):
        return self.fields

    def locator(self, sel):
        return FakeLoc(self.log, int(sel.split('"')[1]))


def field(idx, text, type_="text", filled=False, visible=True):
    return {"idx": idx, "tag": "input", "type": type_, "text": text, "visible": visible,
            "filled": filled}  # fmt: skip


def test_prefill_fills_sure_fields_only():
    page = FakePage(
        [
            field(0, "First Name"),
            field(1, "Email"),
            field(2, "Expected salary"),
            field(3, "I agree to the terms", "checkbox"),
            field(4, "Resume/CV", "file", visible=False),
            field(5, "Phone", filled=True),
            field(6, "Hidden email", visible=False),
        ]
    )
    filled, left = prefill(page, {"first_name": "Asha", "email": "a@x.com", "phone": "1"}, "/r.pdf")
    assert filled == ["first_name", "email", "resume file"]
    assert left == ["Expected salary"]
    assert [e[0] for e in page.log] == ["fill", "fill", "file"]
