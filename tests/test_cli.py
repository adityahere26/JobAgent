from pathlib import Path

from jobagent import cli, store
from jobagent.sources import FetchResult
from jobagent.sources.greenhouse import GreenhouseAdapter
from tests.helpers import gh_job

ROOT = Path(__file__).resolve().parent.parent


def test_full_flow(tmp_path, monkeypatch, capsys):
    (tmp_path / "config").mkdir()
    for f in (ROOT / "config").glob("*.example.*"):
        (tmp_path / "config" / f.name).write_text(f.read_text())
    h = ["--home", str(tmp_path)]
    assert cli.main([*h, "init"]) == 0
    (tmp_path / "config/profile.yaml").write_text(
        "target_roles: [Backend Engineer]\nskills: [python]\nlocations: [Bengaluru, Remote]\n"
        "countries: [IN]\nmin_score: 35\n"
    )
    (tmp_path / "config/companies.yaml").write_text(
        "boards:\n  - {source: greenhouse, token: acme, company: Acme}\n"
    )
    monkeypatch.setattr(
        GreenhouseAdapter,
        "fetch",
        lambda self, t: FetchResult([gh_job(1), gh_job(2, title="Chef")]),
    )
    assert cli.main([*h, "fetch"]) == 0
    assert cli.main([*h, "rank"]) == 0
    report = (tmp_path / "reports/latest.md").read_text()
    assert "Backend Engineer" in report and "Chef" not in report
    assert cli.main([*h, "mark", "1", "applied"]) == 0
    assert cli.main([*h, "mark", "999", "applied"]) == 1
    assert cli.main([*h, "show", "1"]) == 0
    assert "Acme" in capsys.readouterr().out
    assert store.STATUSES[0] == "saved"


def test_missing_config_message(tmp_path, capsys):
    assert cli.main(["--home", str(tmp_path), "rank"]) == 2
    assert "jobagent init" in capsys.readouterr().err
