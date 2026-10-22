"""Paper tasks from the web UI's library: fetch a full text, write a question-independent summary."""

import httpx
import pytest

from fakes import fake_arxiv
from test_pipeline import P2, P7, setup  # noqa: F401  (setup is a fixture)
from litreview.config import Settings
from litreview.papers import drain_tasks, run_task
from litreview.pipeline import Run


@pytest.fixture
def library(setup):
    """A library after one review: P7 was screened out, so it has no text and no summary."""
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    assert lib.best_text([P7], full_text=True) is None and not lib.summaries([P7])
    return lib, client, kwargs


def do(lib, kind, bibcode, arxiv, client):
    """Queue, claim, run and finish one task, as drain_tasks does; returns (task, status, error)."""
    lib.add_task(kind, bibcode)
    t = lib.claim_task()
    status, error = run_task(lib, t, arxiv=arxiv, llm_client=client)
    lib.finish_task(t["id"], status, error)
    return t, status, error


def test_fetch_reads_the_full_text(library):
    lib, client, kwargs = library
    _, status, error = do(lib, "fetch", P7, kwargs["arxiv"], client)
    assert (status, error) == ("done", "")
    assert lib.best_text([P7], full_text=True).source == "arxiv_src"
    assert not lib.summaries([P7])  # fetching never calls Claude


def test_summarize_fetches_first_then_stores_a_checked_summary(library):
    lib, client, kwargs = library
    asked = len(client.calls)
    t, status, error = do(lib, "summarize", P7, kwargs["arxiv"], client)
    assert (status, error) == ("done", "")
    row = lib.summaries([P7])[0]
    assert row["run_id"] == f"task-{t['id']}" and row["text_source"] == "arxiv_src"
    assert '"Study Irrelevant galaxy paper"' in row["summary"]
    assert len(client.calls) == asked + 1  # one summary-only request
    assert lib.run_cost(f"task-{t['id']}") > 0  # its cost is recorded, so it counts toward the month
    # Asking again does nothing: the text is already summarised.
    assert do(lib, "summarize", P7, kwargs["arxiv"], client)[1:] == ("done", "")
    assert len(client.calls) == asked + 1


def test_tasks_fail_with_a_reason(library):
    lib, client, kwargs = library
    _, status, error = do(lib, "fetch", P2, kwargs["arxiv"], client)
    assert status == "failed" and "no arXiv version" in error

    throttled = fake_arxiv(lambda host, path, params: httpx.Response(429))
    _, status, error = do(lib, "fetch", P7, throttled, client)
    assert status == "failed" and "rate limiting" in error

    lib.db.execute("UPDATE papers SET title = 'Unsummarisable paper' WHERE bibcode = ?", (P7,))
    lib.db.commit()
    _, status, error = do(lib, "summarize", P7, kwargs["arxiv"], client)
    assert status == "failed" and "no summary result with a cited passage" in error
    assert not lib.summaries([P7])


def test_queue_dedupes_and_drains_in_order(library):
    lib, client, kwargs = library
    first = lib.add_task("fetch", P7)
    assert lib.add_task("fetch", P7) == first  # already queued
    lib.add_task("summarize", P7)
    lib.add_task("fetch", P2)
    log = []
    assert drain_tasks(lib, arxiv=kwargs["arxiv"], llm_client=client, log=log.append) == 3
    statuses = {(r["kind"], r["bibcode"]): r["status"] for r in lib.db.execute("SELECT * FROM tasks")}
    assert statuses == {("fetch", P7): "done", ("summarize", P7): "done", ("fetch", P2): "failed"}
    assert log[0].startswith("task ") and lib.claim_task() is None


def test_ctrl_c_puts_the_task_back_on_the_queue(library):
    lib, client, kwargs = library
    lib.add_task("summarize", P7)

    def interrupt(params):
        raise KeyboardInterrupt

    client.responder = interrupt
    with pytest.raises(KeyboardInterrupt):
        drain_tasks(lib, arxiv=kwargs["arxiv"], llm_client=client, log=lambda *a: None)
    assert lib.db.execute("SELECT status FROM tasks").fetchone()["status"] == "queued"
