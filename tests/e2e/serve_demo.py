"""Serve a demo library whose reviews, and ADS lookups, run against the test fakes (no real ADS,
arXiv or Claude), for the browser tests:

    ADS_API_TOKEN=demo ANTHROPIC_API_KEY=demo FAKE_WORKER_DELAY=0.25 \\
        .venv/bin/python tests/e2e/serve_demo.py /tmp/litreview-demo 5071
"""

import subprocess
import sys
from pathlib import Path

import uvicorn

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))

import test_pipeline as tp  # noqa: E402
from fakes import fake_ads  # noqa: E402
from test_fulltext import BOOK, make_pdf  # noqa: E402
from litreview.web.app import create_app  # noqa: E402

root, port = Path(sys.argv[1]), int(sys.argv[2])
subprocess.run([sys.executable, str(here / "make_demo.py"), str(root)], check=True)
(here / "shots").mkdir(exist_ok=True)
(here / "shots" / "book.pdf").write_bytes(make_pdf(BOOK))  # for `tests/e2e/run upload`
db, worker = root / "library.sqlite", here.parent / "fake_worker.py"
app = create_app(db, root / "runs", worker_command=lambda run_id: [sys.executable, str(worker), str(db), run_id],
                 task_command=lambda: [sys.executable, str(worker), str(db), "--tasks"],
                 ads_client=lambda: fake_ads(tp.ads_with_ext))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
