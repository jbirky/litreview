"""The evaluation script scores a hand-made run (build plan step 2's check)."""

import csv
import importlib.util
from pathlib import Path

from test_pipeline import P1, P4, P4PRE, P7, setup  # noqa: F401  (setup is a fixture)
from litreview.config import Settings
from litreview.pipeline import Run

spec = importlib.util.spec_from_file_location("score", Path(__file__).parent / "eval" / "score.py")
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)


def gold(*refs):
    return {"references": [{"bibcode": b, "alternates": [], "arxiv": a, "doi": None, "title": "", "citation_count": 0}
                           for b, a in refs]}


def test_score_run_and_human_sheets(setup):
    lib, _, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation"), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"

    # P4 is matched through its preprint bibcode, P7 was found but screened out, the last was never found.
    g = gold((P1, None), (P4PRE, None), (P7, None), ("1999ApJ...500....1Z", "9901.00001"))
    result = score.score_run(lib, run.run_id, g)
    assert result["citation_validity"] == 1.0 and result["rendered_links_valid"] and result["chronology_ok"]
    assert result["coverage"] == {"found": 0.75, "included": 0.5, "read": 0.5, "cited": 0.5}
    assert 0 < result["claim_support"] < 1 and result["cost_usd"] > 0

    paths = score.write_sheets(lib, run.run_id)
    for path in paths:
        rows = list(csv.reader(open(path)))
        header, body = rows[0], rows[1:]
        assert body, path.name
        for r in body:
            if path.name == "eval_card_grounding.csv":
                r[5] = "1"
            elif path.name == "eval_claims.csv":
                r[2] = r[1]
            else:
                r[3], r[4] = "y", "3"
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows([header, *body])
    human = score.score_sheets(run.folder)
    assert human["card_grounding"] == 1.0 and human["claim_agreement"] == 1.0
    assert human["limitations_inferred_mean"] == 3.0 and human["limitations_stated_ok"] == 1.0
    assert P4 in lib.citable(run.run_id)


def test_cli_card_report_runs(setup, capsys):
    from litreview.cli import main

    lib, _, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    db = str(tmp_path / "library.sqlite")
    lib.close()
    assert main(["--db", db, "card", run.run_id, P1]) == 0
    out = capsys.readouterr().out
    assert "status: verified" in out and "FINDING: Radius grows with flux" in out and "[0] Abstract" in out
    assert main(["--db", db, "report", run.run_id]) == 0
    assert "# Run report" in capsys.readouterr().out
    assert main(["--db", db, "runs"]) == 0
    assert run.run_id in capsys.readouterr().out
    assert main(["--db", db, "summary", P1]) == 0
    out = capsys.readouterr().out
    assert "AIM: Study Inflation first seen" in out and "RESULT: A main result" in out and "[0] Abstract" in out
    assert main(["--db", db, "summary", "2099Fake....1....1Z"]) == 1
