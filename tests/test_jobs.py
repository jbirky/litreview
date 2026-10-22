"""The web UI's job runner, with real worker processes running reviews against the test fakes."""

import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the web UI needs pip install -e '.[web]'")

from fastapi.testclient import TestClient  # noqa: E402

from litreview.config import Settings  # noqa: E402
from litreview.library import Library  # noqa: E402
from litreview.pipeline import Run, run_lock  # noqa: E402
from litreview.web.app import create_app  # noqa: E402
from litreview.web.jobs import INTERRUPTED, JobRunner  # noqa: E402

WORKER = str(Path(__file__).resolve().parent / "fake_worker.py")
JSON = {"content-type": "application/json"}


def fake_command(db):
    return lambda run_id: [sys.executable, WORKER, str(db), run_id]


def wait_for(check, timeout=60.0, every=0.1):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(every)
    raise AssertionError("timed out")


def status(db, run_id):
    lib = Library(db, schema=False)
    try:
        return lib.run(run_id)["status"]
    finally:
        lib.close()


@pytest.fixture
def paths(tmp_path):
    db = tmp_path / "library.sqlite"
    Library(db).close()
    return db, tmp_path / "runs"


def queue(db, runs, question="Radius inflation", **settings):
    lib = Library(db)
    run = Run.create(lib, Settings(question=question, expand=False, use_batch=False, **settings),
                     runs_dir=runs, status="queued", log=lambda *a: None)
    lib.close()
    return run.run_id


def test_queued_reviews_run_one_at_a_time(paths):
    db, runs = paths
    first, second = queue(db, runs), queue(db, runs, "Ohmic heating")
    runner = JobRunner(db, runs, command=fake_command(db), poll=0.1)
    runner.start()
    try:
        wait_for(lambda: status(db, first) == "running" or status(db, first) == "done")
        assert status(db, second) == "queued"  # waits for the first
        wait_for(lambda: status(db, second) == "done")
        assert status(db, first) == "done"
    finally:
        runner.stop()
    lib = Library(db)
    assert lib.citable(first) and (runs / first / "review.md").exists() and (runs / first / "worker.log").exists()


def test_pause_sends_ctrl_c_and_the_run_can_continue(paths, monkeypatch):
    db, runs = paths
    monkeypatch.setenv("FAKE_WORKER_DELAY", "0.3")
    run_id = queue(db, runs)
    runner = JobRunner(db, runs, command=fake_command(db), poll=0.1)
    runner.start()
    try:
        wait_for(lambda: status(db, run_id) == "running")
        time.sleep(0.5)
        assert runner.interrupt(run_id)
        wait_for(lambda: status(db, run_id) == "paused")
        lib = Library(db)
        assert lib.run(run_id)["error"] == "interrupted"
        lib.set_run(run_id, status="queued", error="")
        lib.close()
        monkeypatch.setenv("FAKE_WORKER_DELAY", "0")
        wait_for(lambda: status(db, run_id) == "done")
    finally:
        runner.stop()


def test_a_worker_that_dies_early_fails_the_run_once(paths):
    db, runs = paths
    run_id = queue(db, runs)
    runner = JobRunner(db, runs, command=lambda r: [sys.executable, "-c", "import sys; sys.exit(3)"], poll=0.1)
    runner.start()
    try:
        wait_for(lambda: status(db, run_id) == "failed")
    finally:
        runner.stop()
    lib = Library(db)
    assert "code 3" in lib.run(run_id)["error"]


def test_a_run_left_running_by_a_dead_process_is_paused_on_start(paths):
    db, runs = paths
    run_id = queue(db, runs)
    lib = Library(db)
    lib.set_run(run_id, status="running")
    other = queue(db, runs, "Held by the CLI")
    lib.set_run(other, status="running")
    with run_lock(lib, other):  # the CLI is genuinely running this one
        JobRunner(db, runs, command=fake_command(db)).recover()
        assert lib.run(run_id)["status"] == "paused" and lib.run(run_id)["error"] == INTERRUPTED
        assert lib.run(other)["status"] == "running"


def test_web_ui_starts_watches_pauses_and_continues_a_review(paths, monkeypatch):
    db, runs = paths
    monkeypatch.setenv("FAKE_WORKER_DELAY", "0.2")
    app = create_app(db, runs, static_dir=None, worker_command=fake_command(db))
    with TestClient(app) as api:
        body = {"question": "What drives radius inflation in hot Jupiters?", "max_papers": 5,
                "expand": False, "use_batch": False, "cost_cap": 3}
        made = api.post("/api/runs", json=body).json()
        run_id = made["run_id"]
        assert made["status"] == "queued"
        wait_for(lambda: api.get(f"/api/runs/{run_id}").json()["status"] == "running")
        assert api.get(f"/api/runs/{run_id}").json()["here"]
        lines = wait_for(lambda: [e["line"] for e in api.get(f"/api/runs/{run_id}/events").json() if e["line"] == "[plan]"])
        assert lines
        assert api.post(f"/api/runs/{run_id}/pause", json={}).json() == {"pausing": True}
        wait_for(lambda: api.get(f"/api/runs/{run_id}").json()["status"] == "paused")
        monkeypatch.setenv("FAKE_WORKER_DELAY", "0")
        assert api.post(f"/api/runs/{run_id}/resume", json={"cost_cap": 4}).json()["status"] == "queued"
        wait_for(lambda: api.get(f"/api/runs/{run_id}").json()["status"] == "done")
        detail = api.get(f"/api/runs/{run_id}").json()
        assert detail["settings"]["cost_cap"] == 4 and detail["settings"]["max_papers"] == 5
        assert api.get(f"/api/runs/{run_id}/review").json()["review"]["blocks"]
        seqs = [e["seq"] for e in api.get(f"/api/runs/{run_id}/events").json()]
        assert seqs == sorted(seqs) and len(seqs) > 10


def test_paper_tasks_run_in_their_own_lane(paths):
    """A summary queued from the library runs even while a review is running."""
    from test_pipeline import P7

    db, runs = paths
    review = queue(db, runs)
    app = create_app(db, runs, static_dir=None, worker_command=fake_command(db),
                     task_command=lambda: [sys.executable, WORKER, str(db), "--tasks"])
    with TestClient(app) as api:
        wait_for(lambda: status(db, review) == "done")  # the review puts P7 in the library, unread
        row = next(r for r in api.get("/api/library", params={"status": "all"}).json()["rows"] if r["bibcode"] == P7)
        assert row["text"] is None and not row["has_summary"] and row["can_fetch"]
        made = api.post("/api/tasks", json={"kind": "summarize", "bibcodes": [P7, "2099Fake....1....1Z"]}).json()
        assert [q["bibcode"] for q in made["queued"]] == [P7] and made["skipped"][0]["reason"] == "not in the library"

        def finished():  # not has_summary: the summary is stored a moment before the task is marked done
            r = next(r for r in api.get("/api/library", params={"status": "all"}).json()["rows"] if r["bibcode"] == P7)
            return r if (r["tasks"]["summarize"] or {}).get("status") in ("done", "failed") else None

        row = wait_for(finished)
        assert row["has_summary"] and row["text"] == "arxiv_src" and row["tasks"]["summarize"]["status"] == "done"
        assert api.get("/api/tasks").json() == []
        again = api.post("/api/tasks", json={"kind": "summarize", "bibcodes": [P7]}).json()
        assert again == {"queued": [], "skipped": [{"bibcode": P7, "reason": "already summarised"}]}
        page = api.get("/api/papers", params={"bibcode": P7}).json()
        assert page["summary"]["results"][0]["text"] == "A standalone result" and page["tasks"]["summarize"]["status"] == "done"
