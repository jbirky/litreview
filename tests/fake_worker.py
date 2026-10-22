"""A stand-in for `litreview resume <run>` and `litreview tasks` that works against the test fakes
(no real ADS, arXiv or Claude), so the web UI's job runner can be tested with real processes and signals.

    python tests/fake_worker.py <library.sqlite> <run_id>
    python tests/fake_worker.py <library.sqlite> --tasks
FAKE_WORKER_DELAY=<seconds> slows every Claude call, to leave time to pause a run.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_pipeline as tp  # noqa: E402
from fakes import FakeClient, fake_ads, fake_arxiv  # noqa: E402
from litreview.library import Library  # noqa: E402
from litreview.pipeline import Run, RunBusy  # noqa: E402

db, run_id = sys.argv[1], sys.argv[2]
delay = float(os.environ.get("FAKE_WORKER_DELAY", "0"))


def responder(params):
    time.sleep(delay)
    return tp.responder(params)


lib = Library(db)
if run_id == "--tasks":
    from litreview.papers import drain_tasks

    drain_tasks(lib, arxiv=fake_arxiv(tp.arxiv_route), ads=lambda: fake_ads(tp.ads_with_ext),
                llm_client=FakeClient(responder))
    sys.exit(0)
run = Run(lib, run_id, ads=fake_ads(tp.ads_with_ext), arxiv=fake_arxiv(tp.arxiv_route),
          llm_client=FakeClient(responder), poll_interval=0)
try:
    sys.exit(0 if run.execute() in ("done", "paused") else 1)
except RunBusy as e:
    print(e, file=sys.stderr)
    sys.exit(1)
