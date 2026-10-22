"""A demo library from the test fakes (fake ADS, arXiv and Claude): one finished review, one paused.

    .venv/bin/python tests/e2e/make_demo.py /tmp/litreview-demo
    .venv/bin/python -c "import uvicorn; from litreview.web.app import create_app; \\
        uvicorn.run(create_app('/tmp/litreview-demo/library.sqlite', '/tmp/litreview-demo/runs'), port=5071)"
"""
import shutil, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
MARKER = root / ".litreview-demo"
if root.exists() and any(root.iterdir()) and not MARKER.exists():
    sys.exit(f"{root} exists and isn't a demo library made by this script; pick an empty or new directory")
shutil.rmtree(root, ignore_errors=True)
root.mkdir(parents=True)
MARKER.touch()

import test_pipeline as tp
from fakes import FakeClient, fake_ads, fake_arxiv
from litreview.config import Settings
from litreview.library import Library
from litreview.pipeline import Run

lib = Library(root / "library.sqlite")
kw = dict(ads=fake_ads(tp.ads_route), arxiv=fake_arxiv(tp.arxiv_route), llm_client=FakeClient(tp.responder),
          poll_interval=0, log=lambda *a: None)
done = Run.create(lib, Settings(question="What drives radius inflation in hot Jupiters?"), runs_dir=root / "runs", **kw)
print(done.run_id, done.execute())
paused = Run.create(lib, Settings(question="Is Ohmic dissipation enough to inflate hot Jupiters?", cost_cap=0.0001),
                    runs_dir=root / "runs", **kw)
print(paused.run_id, paused.execute())
