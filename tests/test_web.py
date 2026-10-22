import json
import re
import time

import pytest

pytest.importorskip("fastapi", reason="the web UI needs pip install -e '.[web]'")

from fastapi.testclient import TestClient  # noqa: E402

from fakes import fake_ads
from test_pipeline import (DOCS, EXT, EXT_DOC, P1, P2, P3, P4, P4PRE, P5, P6, P7, P8, ads_route, ads_with_ext,  # noqa: F401
                           setup)  # (setup is a fixture)
from litreview.ads import lookup_queries
from litreview.config import Settings
from litreview.library import Library, Paper
from litreview.papers import drain_tasks
from litreview.pipeline import Run
from litreview.render import render_review
from litreview.verify import parse_draft
from litreview.web.app import create_app
from litreview.web.search import MARK_CLOSE, MARK_OPEN, fts_query
from litreview.web.views import read_papers


@pytest.fixture
def web(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="What drives radius inflation in hot Jupiters?", expand=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    app = create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None)
    return TestClient(app), lib, run


def segments_of(review):
    for b in review["blocks"]:
        yield from b.get("heading", [])
        for s in b.get("sentences", []):
            yield from s["segments"]


def test_runs_and_detail(web):
    api, lib, run = web
    runs = api.get("/api/runs").json()
    assert [r["run_id"] for r in runs] == [run.run_id]
    assert runs[0]["status"] == "done" and runs[0]["has_review"] and runs[0]["cited"] == len(lib.citable(run.run_id) & set(
        r["bibcode"] for r in api.get(f"/api/runs/{run.run_id}/papers").json() if r["cited"]))
    detail = api.get(f"/api/runs/{run.run_id}").json()
    assert detail["queries"] and detail["resume"] is None
    assert api.get("/api/runs/nope").status_code == 404


def test_review_only_cites_citable_papers_and_never_shows_keys(web):
    api, lib, run = web
    review = api.get(f"/api/runs/{run.run_id}/review").json()["review"]
    citable = lib.citable(run.run_id)
    cites = [i for s in segments_of(review) if s["t"] == "cite" for i in s["items"]]
    assert cites and all(i.get("invalid") or i["bibcode"] in citable for i in cites)
    text = " ".join(s["v"] for s in segments_of(review) if s["t"] == "text")
    assert "@" not in text and "unsupportedclaim" not in text
    entries = [b["entry"]["bibcode"] for b in review["blocks"] if b["kind"] == "entry"]
    assert entries and set(entries) <= citable
    assert any(s["partial"] for b in review["blocks"] for s in b.get("sentences", []))
    assert {r["bibcode"] for r in review["references"]} <= citable
    assert review["periods"] and all(p["start"] <= p["end"] for p in review["periods"])


def test_review_keeps_bullets_under_their_entry_and_their_nesting(web):
    api, lib, run = web
    md = (f"## Overview\n\nInflation tracks flux [@{P2}].\n\n## Chronology\n\n### 2000–2020: All\n\n**[@{P1}]**\n\n"
          "- **Limitations.** The authors note two issues:\n\n  - small samples;\n\n  - short baselines.\n\n"
          f"- **Context.** Later work agrees [@{P2}].\n")
    lib.put(run.run_id, "final_draft", md)
    blocks = api.get(f"/api/runs/{run.run_id}/review").json()["review"]["blocks"]
    i = next(i for i, b in enumerate(blocks) if b["kind"] == "entry")
    assert [(b["kind"], b.get("depth")) for b in blocks[i:]] == [
        ("entry", None), ("bullet", 0), ("bullet", 1), ("bullet", 1), ("bullet", 0)]

    citable = lib.citable(run.run_id)
    review = render_review(parse_draft(md), lib.papers(sorted(citable)), citable, question="Q", interpretation=None,
                           since=None, until=None, stats={"read": 1, "cited": 2, "full_text": 1, "abstract_only": 0,
                                                          "sources": {}}, title="T")
    assert "issues:\n\n  - small samples;\n\n  - short baselines.\n\n- **Context.**" in review


def test_adding_papers_queues_a_finished_review_to_be_written_again(web):
    api, lib, run = web
    detail = api.get(f"/api/runs/{run.run_id}").json()
    assert detail["can_add"] and detail["additions"] == []
    url = f"/api/runs/{run.run_id}/add"
    spent = lib.run_cost(run.run_id)
    assert api.post(url, json={"bibcodes": [EXT], "cost_cap": spent / 2}).status_code == 422
    read = run.get("selected")[0]
    refused = api.post(url, json={"bibcodes": [read], "cost_cap": spent + 3})
    assert refused.status_code == 422 and "already read" in refused.json()["detail"]

    made = api.post(url, json={"bibcodes": [EXT, read], "cost_cap": spent + 3}).json()
    assert made["status"] == "queued" and made["stage"] == "fetch" and not made["additions"][-1]["done"]
    assert made["skipped"] == [{"bibcode": read, "reason": "already read for this review"}]
    settings = json.loads(lib.run(run.run_id)["settings"])
    assert settings["cost_cap"] == spent + 3 and settings["use_batch"] is False
    assert not api.get(f"/api/runs/{run.run_id}").json()["can_add"]
    assert api.post(url, json={"bibcodes": [EXT], "cost_cap": spent + 3}).status_code == 409
    assert api.get(f"/api/runs/{run.run_id}/review").json()["review"]  # the current version shows until then


def test_what_is_typed_to_find_a_paper_becomes_an_ads_query():
    assert lookup_queries("2009ApJ...698..895K, arXiv:0903.5315v2 https://doi.org/10.1088/0004-637X/698/1/895") == (
        ['identifier:("2009ApJ...698..895K" OR "arXiv:0903.5315" OR "10.1088/0004-637X/698/1/895")'],
        ["2009ApJ...698..895K", "arXiv:0903.5315", "10.1088/0004-637X/698/1/895"])
    assert lookup_queries("https://ui.adsabs.harvard.edu/abs/2011A%26A...527A..12X/abstract https://arxiv.org/pdf/astro-ph/0301158")[1] == [
        "2011A&A...527A..12X", "arXiv:astro-ph/0301158"]
    assert lookup_queries("Kelly et al. 2009 quasars") == (
        ['(author:"^Kelly" year:2009 quasars) NOT doctype:(abstract OR erratum)'], [])
    assert lookup_queries("Shakura & Sunyaev (1973)")[0] == [
        '(author:"^Shakura" author:"Sunyaev" year:1973) NOT doctype:(abstract OR erratum)']
    assert lookup_queries("damped random walk")[0] == ["(damped random walk) NOT doctype:(abstract OR erratum)"]


def test_papers_to_add_are_looked_up_in_ads_with_what_the_review_made_of_each(web, tmp_path):
    _, lib, run = web
    asked = []

    def route(path, params, body):
        asked.append(params.get("q"))
        if params["q"].startswith('(author:"^Zane"'):
            return 200, {"response": {"numFound": 40, "docs": [DOCS[P7], EXT_DOC]}}, None
        if "broken" in params["q"]:
            return 500, {"error": "ADS is down"}, None
        return ads_with_ext(path, params, body)

    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None,
                                ads_client=lambda: fake_ads(route)))
    url = f"/api/runs/{run.run_id}/lookup"
    read = set(run.get("selected"))
    citing_ext = {b for b in read_papers(lib, run.run_id) if EXT in (DOCS.get(b) or {}).get("reference", [])}
    assert citing_ext

    got = api.post(url, json={"q": f"{EXT}, {P4PRE} arXiv:9999.99999"}).json()
    by = {r["bibcode"]: r for r in got["results"]}
    assert got["not_found"] == ["arXiv:9999.99999"] and set(by) == {EXT, P4}  # the preprint is the paper read
    assert by[EXT]["status"] == "new" and by[EXT]["cited_by"] == len(citing_ext) and by[P4]["status"] == "read"
    assert got["results"][0]["bibcode"] == EXT  # cited most first
    assert by[EXT]["ads"] == f"https://ui.adsabs.harvard.edu/abs/{EXT}/abstract"

    got = api.post(url, json={"q": "Zane et al. 2001"}).json()
    assert asked[-1] == '(author:"^Zane" year:2001) NOT doctype:(abstract OR erratum)'
    assert got["total"] == 40 and [r["bibcode"] for r in got["results"]] == [EXT, P7]
    excluded = got["results"][1]
    assert excluded["status"] == "excluded" and excluded["reason"] and P7 not in read

    assert api.post(url, json={"q": "broken"}).status_code == 502
    assert api.post(url, json={"q": ""}).status_code == 422
    assert api.post("/api/runs/nope/lookup", json={"q": EXT}).status_code == 404
    calls = len(asked)
    assert api.post(url, json={"q": EXT}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert len(asked) == calls  # another site can't spend ADS requests through the page


def test_card_passages_point_at_stored_paragraphs(web):
    api, lib, run = web
    card = api.get("/api/cards", params={"run": run.run_id, "bibcode": P1}).json()
    assert card["status"] == "verified" and card["text"]["matches"] and card["text"]["source"] == "arxiv_src"
    text = api.get("/api/text", params={"bibcode": P1, "source": card["text"]["source"],
                                        "version": card["text"]["version"]}).json()
    labels = [p["label"] for p in text["paragraphs"]]
    for kind in ("methods", "findings", "limitations"):
        for item in card[kind]:
            assert item["passages"]
            for ps in item["passages"]:
                assert labels[ps["i"]] == ps["label"]
    assert api.get("/api/cards", params={"run": run.run_id, "bibcode": P7}).status_code == 404


def test_paper_page(web):
    api, lib, run = web
    page = api.get("/api/papers", params={"bibcode": P1}).json()
    assert page["title"] == "Inflation first seen" and page["links"]["arxiv"].endswith("astro-ph/0301001")
    assert page["texts"][0]["source"] == "arxiv_src" and page["texts"][0]["paragraphs"] > 0
    assert page["summary"]["aim"][0]["text"] == "Study Inflation first seen" and page["summary"]["text"]["matches"]
    assert page["cards"][0]["question"] == run.settings.question and page["cards"][0]["cited"] in (True, False)
    assert page["screening"][0]["score"] >= 2
    # A preprint's bibcode finds the journal record it was merged into.
    assert api.get("/api/papers", params={"bibcode": P4PRE}).json()["bibcode"] == P4
    assert api.get("/api/papers", params={"bibcode": "2099Fake....1....1Z"}).status_code == 404


def test_library_filters_histogram_and_search(web):
    api, lib, run = web
    everything = api.get("/api/library", params={"status": "all", "limit": 500}).json()
    bibcodes = [r["bibcode"] for r in everything["rows"]]
    assert P4PRE not in bibcodes and P4 in bibcodes  # the preprint record is folded into its journal paper
    read = api.get("/api/library").json()  # default: papers read in full
    assert read["total"] == everything["totals"]["read"] and all(r["text"] for r in read["rows"])
    assert sum(read["years"].values()) == read["total"]
    cited = api.get("/api/library", params={"status": "cited"}).json()
    assert cited["total"] == len({b for b in lib.citable(run.run_id)} & set(bibcodes)) or cited["total"] > 0
    years = api.get("/api/library", params={"status": "all", "year_from": 2010, "year_to": 2016}).json()
    assert years["rows"] and all(2010 <= r["year"] <= 2016 for r in years["rows"])
    assert sum(years["years"].values()) == everything["total"]  # the histogram ignores the year filter
    found = api.get("/api/library", params={"status": "all", "q": "ohmic", "sort": "relevance"}).json()
    assert [r["title"] for r in found["rows"]] == ["Ohmic heating"]
    assert api.get("/api/library", params={"status": "bogus"}).status_code == 422


def test_search_inside_papers(web):
    api, lib, run = web
    hits = api.get("/api/search", params={"q": "radial velocities"}).json()
    assert hits and all(MARK_OPEN in h["snippet"] and MARK_CLOSE in h["snippet"] for h in hits)
    h = hits[0]
    text = api.get("/api/text", params={"bibcode": h["bibcode"], "source": h["source"], "version": h["version"]}).json()
    assert "radial velocit" in text["paragraphs"][h["idx"]]["text"].lower()
    assert api.get("/api/search", params={"q": '" OR NEAR('}).json() == []


def test_fts_query_is_always_safe():
    assert fts_query('"hot Jupiter" inflat') == '"hot Jupiter" "inflat"*'
    assert fts_query("NOT AND OR") == '"NOT"* "AND"* "OR"*'
    assert fts_query('" * ( ) :') is None


def test_report_and_overview(web):
    api, lib, run = web
    assert api.get(f"/api/runs/{run.run_id}/report").json()["markdown"].startswith("# Run report")
    o = api.get("/api/overview").json()
    assert o["month_spend"] > 0 and o["papers"] >= 8 and o["runs"] == 1


def test_overview_shows_ads_quota(tmp_path):
    from litreview.ads import record_quota

    f = tmp_path / "ads-quota.json"
    app = lambda: TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None,  # noqa: E731
                                        run_jobs=False, ads_quota_file=f))
    assert app().get("/api/overview").json()["ads_quota"] is None
    record_quota(f, "/search/query", {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4700",
                                      "x-ratelimit-reset": str(time.time() + 600)})
    assert app().get("/api/overview").json()["ads_quota"]["used"] == 300


def test_paused_run_has_no_review_yet(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=0.0001), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))
    assert api.get(f"/api/runs/{run.run_id}/review").json() == {"review": None}
    detail = api.get(f"/api/runs/{run.run_id}").json()
    assert detail["resume"] == f"litreview resume {run.run_id}" and "cap" in detail["error"]


def test_server_never_writes_the_library(web, tmp_path):
    api, lib, run = web
    db = tmp_path / "library.sqlite"
    before = lib.db.execute("SELECT COUNT(*) FROM run_data").fetchone()[0], db.stat().st_size
    for path in ("/api/overview", "/api/runs", f"/api/runs/{run.run_id}/review", "/api/library",
                 "/api/search?q=inflation", f"/api/papers?bibcode={P1}", f"/api/runs/{run.run_id}/graph"):
        assert api.get(path).status_code == 200
    after = lib.db.execute("SELECT COUNT(*) FROM run_data").fetchone()[0], db.stat().st_size
    assert before == after


def test_starting_continuing_and_pausing_from_the_api(setup):
    lib, client, kwargs, tmp_path = setup
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))  # no worker loop
    made = api.post("/api/runs", json={"question": "Radius inflation", "since": 2000, "until": 2020, "cost_cap": 2})
    assert made.status_code == 201
    run_id = made.json()["run_id"]
    row = lib.run(run_id)
    assert row["status"] == "queued" and json.loads(row["settings"])["since"] == 2000
    assert api.get(f"/api/runs/{run_id}").json()["queue_position"] == 1
    assert [e["line"] for e in api.get(f"/api/runs/{run_id}/events").json()] == ["queued from the web UI"]
    # Taking it off the queue pauses it; continuing puts it back.
    assert api.post(f"/api/runs/{run_id}/pause", json={}).json() == {"paused": True}
    assert lib.run(run_id)["status"] == "paused"
    assert api.post(f"/api/runs/{run_id}/resume", json={}).json()["status"] == "queued"
    assert api.post(f"/api/runs/{run_id}/resume", json={}).status_code == 409  # already queued


def test_new_run_validation(setup):
    lib, client, kwargs, tmp_path = setup
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))
    for bad in ({"question": "x"}, {"question": "Radius inflation", "max_papers": 0},
                {"question": "Radius inflation", "cost_cap": 0}, {"question": "Radius inflation", "since": 2020, "until": 2000}):
        assert api.post("/api/runs", json=bad).status_code == 422, bad
    assert not lib.runs()


def test_a_raised_cap_must_exceed_what_was_spent(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=0.5), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    spent = lib.run_cost(run.run_id)
    assert spent > 0
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))
    detail = api.get(f"/api/runs/{run.run_id}").json()
    assert detail["cap_pause"]["estimate"] > 0
    assert api.post(f"/api/runs/{run.run_id}/resume", json={"cost_cap": spent / 2}).status_code == 422
    assert api.post(f"/api/runs/{run.run_id}/resume", json={"cost_cap": 9}).json()["status"] == "queued"
    assert json.loads(lib.run(run.run_id)["settings"])["cost_cap"] == 9


def test_cap_pause_is_read_from_older_pause_messages(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=3), runs_dir=tmp_path / "runs", **kwargs)
    lib.set_run(run.run_id, status="paused", error="next call (~$1.65) would take the run past its $3.00 cap "
                "(spent $1.65); raise --cost-cap and run `litreview resume`")
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))
    assert api.get(f"/api/runs/{run.run_id}").json()["cap_pause"] == {"estimate": 1.65, "cap": 3.0, "spent": 1.65}


def test_other_sites_cannot_start_or_change_reviews(setup):
    lib, client, kwargs, tmp_path = setup
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None))
    body = {"question": "Radius inflation"}
    assert api.post("/api/runs", json=body, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert api.post("/api/runs", json=body, headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert api.post("/api/runs", content="question=x", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415
    assert api.post("/api/runs", content='{"question": "Radius inflation"}', headers={"content-type": "text/plain"}).status_code == 415
    assert not lib.runs()
    assert api.post("/api/runs", json=body, headers={"sec-fetch-site": "same-origin"}).status_code == 201


@pytest.fixture
def share_assets(tmp_path):
    """A stand-in for the built share bundle, holding markup that must not end its element early."""
    assets = tmp_path / "share-assets"
    assets.mkdir()
    (assets / "share.js").write_text('render("<!---->", "</script>")')
    (assets / "share.css").write_text('a::after{content:"</style>"}')
    return assets


def shared_data(page: str) -> dict:
    return json.loads(re.search(r'<script type="application/json" id="review-data">(.*?)</script>', page, re.S).group(1))


def test_shared_page_is_one_file_with_the_cards_and_only_the_cited_paragraphs(web, tmp_path, share_assets):
    _, lib, run = web
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None, share_assets=share_assets))
    db = tmp_path / "library.sqlite"
    before = lib.db.execute("SELECT COUNT(*) FROM run_data").fetchone()[0], db.stat().st_size
    res = api.get(f"/api/runs/{run.run_id}/share", params={"download": True})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert res.headers["content-disposition"] == f'attachment; filename="{run.run_id}.html"'
    assert api.get(f"/api/runs/{run.run_id}/share").headers["content-disposition"].startswith("inline;")
    assert (lib.db.execute("SELECT COUNT(*) FROM run_data").fetchone()[0], db.stat().st_size) == before

    page = res.text
    assert page.count("</script>") == 2 and page.count("</style>") == 1
    assert 'render("<\\!---->", "<\\/script>")' in page and '"<\\/style>"' in page
    data = shared_data(page)
    assert set(data) == {"run", "review", "cards", "bib", "exported_at", "version"}
    assert "cost" not in data["run"] and "notes" not in data["review"] and "Run report" not in page
    assert data["run"]["question"] == run.settings.question and data["run"]["screened"] > 0
    assert data["bib"] == (run.folder / "refs.bib").read_text()
    assert {r["bibcode"] for r in data["review"]["references"]} == set(data["cards"])
    for b, card in data["cards"].items():
        items = [it for kind in ("methods", "findings", "limitations") for it in card[kind]]
        assert all(len(it["quotes"]) <= 1 for it in items)
        if not card["text"]["matches"]:
            assert card["paragraphs"] == {}
            continue
        text = lib.text(b, card["text"]["source"], card["text"]["version"])
        assert {int(i) for i in card["paragraphs"]} == {p["i"] for it in items for p in it["passages"]}
        for i, para in card["paragraphs"].items():
            assert para == {"label": text.paragraphs[int(i)]["label"], "text": text.paragraphs[int(i)]["text"]}
    assert data["cards"][P1]["paragraphs"]


def test_quotes_are_cut_where_the_card_cuts_them():
    from litreview.web.share import shown_quote

    long = "[2 Methods, para 1]  " + "word " * 100
    (q,) = shown_quote([long, "[3 Results] another"])
    assert q.startswith("[2 Methods, para 1]  word word") and q.endswith("…") and len(q) == len("[2 Methods, para 1]  ") + 278
    assert shown_quote(["[x]  short\n text"]) == ["[x]  short text"] and shown_quote([]) == []
    body = "a" * 250 + " $M_{\\odot}$ " + "c" * 10 + " $\\sim$10 days " + "b" * 50  # 277 falls inside $\sim$
    assert shown_quote(["[1] " + body]) == ["[1] " + "a" * 250 + " $M_{\\odot}$ " + "c" * 10 + "…"]


def test_shared_page_needs_a_written_review_and_the_built_bundle(setup, tmp_path, share_assets):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=0.0001), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    api = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None, share_assets=share_assets))
    assert api.get(f"/api/runs/{run.run_id}/share").status_code == 409
    assert api.get("/api/runs/nope/share").status_code == 404
    unbuilt = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None,
                                    share_assets=tmp_path / "nothing"))
    res = unbuilt.get(f"/api/runs/{run.run_id}/share")
    assert res.status_code == 503 and "npm run build" in res.json()["detail"]


def test_cli_share_writes_the_page_into_the_run_folder(setup, share_assets, monkeypatch, capsys):
    from litreview.cli import main
    from litreview.web import share

    lib, _, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    lib.close()
    monkeypatch.setattr(share, "ASSETS", share_assets)
    db = str(tmp_path / "library.sqlite")
    assert main(["--db", db, "share", run.run_id]) == 0
    assert shared_data((run.folder / "share.html").read_text())["run"]["run_id"] == run.run_id
    assert main(["--db", db, "share", run.run_id, "-o", str(tmp_path / "x.html")]) == 0
    assert "x.html" in capsys.readouterr().out and (tmp_path / "x.html").exists()
    assert main(["--db", db, "share", "nope"]) == 1


def test_citation_graph_links_the_papers_read_and_lists_what_they_cite_that_the_review_lacks(web):
    api, lib, run = web
    graph = api.get(f"/api/runs/{run.run_id}/graph").json()
    nodes = {n["bibcode"]: n for n in graph["nodes"]}
    assert set(nodes) == set(read_papers(lib, run.run_id)) and not graph["missing"]
    assert nodes[P1]["cited"] and nodes[P1]["cited_by"] == 4 and not nodes[P8]["cited"] and nodes[P8]["has_refs"]
    # P5 cites P4 under its preprint bibcode; references to papers nobody read aren't edges.
    assert {tuple(e) for e in graph["edges"]} == {(P2, P1), (P3, P1), (P4, P1), (P4, P2), (P5, P1), (P5, P4), (P6, P4), (P6, P5)}
    ext, p7 = graph["gaps"]
    assert ext == {"bibcode": EXT, "by": [P4, P5, P6], "in_library": False, "label": None, "title": None,
                   "year": 2001, "status": "not_found"}
    assert p7["bibcode"] == P7 and p7["by"] == [P5, P6] and p7["label"] == "Gray 2018" and p7["status"] == "excluded"
    assert p7["score"] == 0 and p7["reason"]
    assert api.get("/api/runs/nope/graph").status_code == 404


def test_missing_citation_links_are_fetched_in_one_batch(web):
    api, lib, run = web
    lib.db.execute("DELETE FROM reference_lists WHERE bibcode IN (?, ?, ?)", (P1, P5, P6))
    lib.upsert_paper(Paper(bibcode="2099Fake.....1Z"))  # in the library, unknown to ADS
    graph = api.get(f"/api/runs/{run.run_id}/graph").json()
    assert set(graph["missing"]) == {P1, P5, P6} and [P6, P5] not in graph["edges"]

    res = api.post("/api/tasks", json={"kind": "refs", "bibcodes": [*graph["missing"], P2, "2099Fake.....1Z"]}).json()
    assert {q["bibcode"] for q in res["queued"]} == {P1, P5, P6, "2099Fake.....1Z"}
    assert res["skipped"] == [{"bibcode": P2, "reason": "its citation links are already stored"}]
    queries = []

    def route(path, params, body):
        queries.append(params.get("q"))
        return ads_route(path, params, body)

    assert drain_tasks(lib, arxiv=None, ads=lambda: fake_ads(route), log=lambda *a: None) == 4
    assert len(queries) == 1
    status = {r["bibcode"]: (r["status"], r["error"]) for r in lib.db.execute("SELECT * FROM tasks WHERE kind = 'refs'")}
    assert status[P5] == ("done", "") and status["2099Fake.....1Z"][0] == "failed" and "no record" in status["2099Fake.....1Z"][1]
    graph = api.get(f"/api/runs/{run.run_id}/graph").json()
    assert not graph["missing"] and [P6, P5] in graph["edges"]


def test_cli_refs_fills_in_a_runs_missing_citation_links(setup, monkeypatch, capsys):
    from litreview import cli

    lib, _, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    lib.db.execute("DELETE FROM reference_lists WHERE bibcode = ?", (P6,))
    lib.db.commit()
    lib.close()
    monkeypatch.setattr(cli, "ADSClient", lambda: fake_ads(ads_route))
    db = str(tmp_path / "library.sqlite")
    assert cli.main(["--db", db, "refs", run.run_id]) == 0
    assert "stored 1 of 1 reference lists; 8 citation links" in capsys.readouterr().out
    assert cli.main(["--db", db, "refs", run.run_id]) == 0
    assert "already have their citation links" in capsys.readouterr().out
    assert cli.main(["--db", db, "refs", "nope"]) == 1


def test_a_pdf_uploaded_for_a_paper_becomes_the_text_reviews_read(web, tmp_path):
    from test_fulltext import BOOK, make_pdf

    client, lib, run = web
    pdf = make_pdf(BOOK)
    as_pdf = {"content-type": "application/pdf"}
    r = client.post("/api/papers/upload", params={"bibcode": P2, "pages": "2-4"}, content=pdf, headers=as_pdf)
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in ("bibcode", "source", "pages", "paragraphs", "truncated")} == {
        "bibcode": P2, "source": "upload", "pages": [2, 4], "paragraphs": 9, "truncated": False}
    page = client.get("/api/papers", params={"bibcode": P2}).json()
    assert page["texts"][0]["source"] == "upload" and page["texts"][0]["label"] == "uploaded PDF, pages 2–4"
    papers = {p["bibcode"]: p for p in client.get(f"/api/runs/{run.run_id}/papers").json()}
    assert papers[P2]["newer_text"] and not papers[P1]["newer_text"]

    def upload(params, content=pdf, headers=as_pdf, api=client):
        return api.post("/api/papers/upload", params=params, content=content, headers=headers)

    r = upload({"bibcode": P2, "pages": "4-9"})
    assert r.status_code == 422 and "aren't in the PDF, which has 5 pages" in r.json()["detail"]
    assert upload({"bibcode": P2, "pages": "x"}).status_code == 422
    assert upload({"bibcode": "2099Fake....1....1Z"}).status_code == 404
    assert upload({"bibcode": P2}, content=b"").status_code == 422
    assert upload({"bibcode": P2}, headers={"content-type": "text/plain"}).status_code == 415
    assert upload({"bibcode": P2}, headers={**as_pdf, "sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/api/runs", content=pdf, headers=as_pdf).status_code == 415  # only uploads take a PDF
    small = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None, max_upload_bytes=1000))
    assert upload({"bibcode": P2}, api=small).status_code == 413
    read_only = TestClient(create_app(tmp_path / "library.sqlite", tmp_path / "runs", static_dir=None, run_jobs=False))
    assert upload({"bibcode": P2}, api=read_only).status_code == 503
    assert not list((tmp_path / "uploads").iterdir())  # nothing kept but the text


def test_cli_attach_finds_the_paper_by_any_identifier_and_stores_the_pdf(setup, monkeypatch, capsys):
    from test_fulltext import BOOK, make_pdf

    from litreview import cli
    from litreview.ads import paper_from_record

    lib, _, kwargs, tmp_path = setup
    lib.upsert_paper(paper_from_record(DOCS[P3]))
    lib.close()
    asked = []
    monkeypatch.setattr(cli, "ADSClient", lambda: asked.append(1) or fake_ads(ads_with_ext))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(make_pdf(BOOK))
    db = str(tmp_path / "library.sqlite")

    assert cli.main(["--db", db, "attach", "https://arxiv.org/abs/0701.0003", str(pdf), "--pages", "2–4"]) == 0
    out = capsys.readouterr().out
    assert P3 in out and f"stored 9 paragraphs from pages 2-4 of {pdf} as its full text" in out and not asked
    assert cli.main(["--db", db, "attach", EXT, str(pdf), "--json"]) == 0  # not in the library: found in ADS
    assert json.loads(capsys.readouterr().out)["bibcode"] == EXT and asked
    lib = Library(tmp_path / "library.sqlite")
    assert lib.best_text([P3]).source == "upload" and lib.paper(EXT).title == "An early radius measurement"

    assert cli.main(["--db", db, "attach", P3, str(tmp_path / "missing.pdf")]) == 1
    assert "can't read" in capsys.readouterr().err
    assert cli.main(["--db", db, "attach", P3, str(pdf), "--pages", "two", "--json"]) == 1
    assert "pages should look like 120-180" in json.loads(capsys.readouterr().out)["error"]
