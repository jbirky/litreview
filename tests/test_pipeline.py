"""End to end with fake ADS, arXiv and Claude: the citation guarantee holds through a whole run."""

import json
import re
from urllib.parse import unquote

import httpx
import pytest

from fakes import FakeClient, ads_doc, atom, fake_ads, fake_arxiv, fake_oa, latex_tarball, message, no_oa, text_block
from litreview.config import Settings
from litreview.extract import EXTRACT_SYSTEM, SUMMARY_OFF, SUMMARY_ON, SUMMARY_SYSTEM
from litreview.library import Library
from litreview.pipeline import Run, request_additions, take_turns
from litreview.plan import PLAN_SYSTEM, SCREEN_SYSTEM
from litreview.synthesize import BASE_SYSTEM
from litreview.verify import check_chronology, parse_draft

P1, P2, P3 = "2003ApJ...590....1A", "2005ApJ...620....2B", "2007ApJ...660....3C"
P4, P4PRE = "2011ApJ...730....4D", "2010arXiv1012.0004D"
P5, P6, P7, P8, P9 = "2013MNRAS.430....5E", "2016A&A...590A...6F", "2018ApJ...850....7G", "2019ApJ...870....8H", "2009ApJ...700....9I"
EXT, EXT1 = "2001ApJ...550...10Z", "1999ApJ...500...11Y"  # cited, never found by any search

DOCS = {
    P1: ads_doc(P1, 2003, "Inflation first seen", ["Adams, A.", "Bell, B.", "Cole, C."], arxiv="astro-ph/0301001",
                refs=[EXT1]),
    P2: ads_doc(P2, 2005, "Inflation and flux", ["Baker, B."], abstract="We find inflation scales with flux.", refs=[P1]),
    P3: ads_doc(P3, 2007, "A third look", ["Chen, C.", "Diaz, D."], arxiv="0701.0003", refs=[P1]),
    P4: ads_doc(P4, 2011, "A population study", ["Dunn, D.", "Eve, E.", "Fox, F."], arxiv="1012.0004",
                refs=[P1, P2, P9, EXT]),
    P4PRE: ads_doc(P4PRE, 2010, "A population study", ["Dunn, D.", "Eve, E.", "Fox, F."], arxiv="1012.0004", refereed=False,
                   month="12", refs=[P1, P2, P9, EXT]),
    P5: ads_doc(P5, 2013, "Ohmic heating", ["Egan, E.", "Gale, G.", "Hu, H."], arxiv="1301.0005", refs=[P4PRE, P1, EXT, P7]),
    P6: ads_doc(P6, 2016, "Re-inflation", ["Ford, F.", "Iris, I.", "Jay, J."], arxiv="1601.00006", refs=[P5, P4, EXT, P7]),
    P7: ads_doc(P7, 2018, "Irrelevant galaxy paper", ["Gray, G."], arxiv="1801.00007"),
    P8: ads_doc(P8, 2019, "Off-topic instrument paper", ["Hale, H."], arxiv="1901.00008"),
    P9: ads_doc(P9, 2009, "Tidal heating", ["Ito, I.", "King, K.", "Lam, L."], arxiv="0901.00009", refs=[P1]),
}
ARXIV = {d["identifier"][-1].split(":", 1)[1]: (b, d) for b, d in DOCS.items() if b != P4PRE and d["identifier"][-1].startswith("arXiv:")}
POSTED = {"astro-ph/0301001": "2003-01-10", "0701.0003": "2007-01-05", "1012.0004": "2010-12-20",
          "1301.0005": "2013-01-02", "1601.00006": "2016-01-03", "1801.00007": "2018-01-04",
          "1901.00008": "2019-01-05", "0901.00009": "2009-01-06"}


def ads_route(path, params, body):
    if path == "/search/query":
        q = params["q"]
        if params["rows"] == "0":
            return 200, {"response": {"numFound": 12, "docs": []}}, None
        if q.startswith("identifier:("):
            asked = set(re.findall(r'"([^"]+)"', q))
            docs = [d for d in DOCS.values() if {d["bibcode"], *d["alternate_bibcode"]} & asked]
        elif "citations(" in q:
            docs = [DOCS[P9], DOCS[P1]]
        elif "q-alpha" in q:
            docs = [DOCS[b] for b in (P1, P2, P3, P4, P7)]
        elif "q-beta" in q:
            docs = [DOCS[b] for b in (P4PRE, P5, P6, P8)]
        else:
            docs = []
        return 200, {"response": {"numFound": len(docs), "docs": docs}}, None
    if path == "/export/bibtex":
        return 200, {"export": "\n".join(f"@ARTICLE{{{b}}}" for b in body["bibcode"])}, None
    return 404, {"error": "not found"}, None


def paper_tex(title):
    para = (f"This paragraph of {title} describes measurements of radius inflation in hot Jupiters "
            "using transit photometry and radial velocities, with careful treatment of systematics. ") * 4
    body = "\n\n".join(para for _ in range(3))
    return (r"\documentclass{aastex631}\begin{document}\title{" + title + r"}\begin{abstract}"
            + para + r"\end{abstract}\section{Introduction}" + body + r"\section{Results}" + body
            + r"% a private comment that must not be read" + "\n" + r"\end{document}")


def arxiv_route(host, path, params):
    if path == "/api/query":
        ids = params["id_list"].split(",")
        return httpx.Response(200, text=atom([(f"{i}v2", POSTED[i], ARXIV[i][1]["title"][0], "arXiv abstract of " + i)
                                              for i in ids if i in ARXIV]))
    if path.startswith("/src/"):
        aid = path[5:].rsplit("v", 1)[0]
        if aid == "0701.0003":
            return httpx.Response(404)
        tar = latex_tarball({"ms.tex": paper_tex(ARXIV[aid][1]["title"][0])})
        return httpx.Response(200, content=tar, headers={"content-disposition": f'attachment; filename="arXiv-{aid}v2.tar.gz"'})
    return httpx.Response(404)


DRAFT_V1 = f"""## Overview

Inflation tracks flux [@{P2}; @{P4}]. Adams et al. (2003) first showed this [@{P1}]. This is an unsupportedclaim. Later work agrees [@2099Fake....1....1Z].

## Chronology

### 2000–2008: Early detections

**[@{P1}]** Found inflated radii. The authors note small samples.

**[@{P2}]** Linked inflation to flux.

**[@{P9}]** Proposed tidal heating.

**[@{P3}]** Revisited the trend; limitations were not assessed.

### 2009–2020: Populations

**[@{P4}]** Measured the population trend.

**[@{P5}]** Tested Ohmic heating.

**[@{P6}]** Found re-inflation.

## Open problems

- Heating mechanisms remain debated [@{P5}; @{P6}].
"""

DRAFT_V2 = f"""## Overview

Inflation tracks flux [@{P2}; @{P4}]. Early detections came first [@{P1}]. The trend is overstated everywhere [@{P4}]. An unsupportedclaim persists [@{P5}].

## Chronology

### 2000–2008: Early detections

**[@{P1}]** Found inflated radii. The authors note small samples.

**[@{P2}]** Linked inflation to flux.

**[@{P3}]** Revisited the trend; limitations were not assessed.

**[@{P9}]** Proposed tidal heating.

### 2009–2020: Populations

**[@{P4}]** Measured the population trend.

**[@{P5}]** Tested Ohmic heating.

**[@{P6}]** Found re-inflation.

## Open problems

- Heating mechanisms remain debated [@{P5}; @{P6}].
"""


MEND_ITEM = re.compile(r"^M(\d+)\n(?:  .*\n)*?  sentence: (.*)$", re.M)


def responder(params):
    system = params.get("system")
    content = params["messages"][0]["content"]
    if system == PLAN_SYSTEM:
        return message(json.dumps({"interpretation": "Radius inflation of hot Jupiters.", "since": 0, "until": 0,
                                   "queries": ['abs:"q-alpha"', 'abs:"q-beta"']}))
    if system == SCREEN_SYSTEM:
        decisions = [{"key": k, "score": 0 if "Irrelevant" in t else 3, "reason": "fake"}
                     for k, t in re.findall(r"\[(P\d+)\] \S+ \| ([^\n]*)", content)]
        return message(json.dumps({"decisions": decisions}))
    if system == SUMMARY_SYSTEM:
        doc = content[0]
        last = len(doc["source"]["content"]) - 1
        if doc["title"].startswith("Unsummarisable"):
            return message("SUMMARY (aim): An aim with no citation", model="claude-sonnet-5-5")
        return message([
            text_block("SUMMARY (aim): "),
            text_block("Study " + doc["title"], [{"start_block_index": 0, "end_block_index": 1, "cited_text": "t"}]),
            text_block("\nSUMMARY (result): "),
            text_block("A standalone result", [{"start_block_index": last, "end_block_index": last + 1, "cited_text": "u"}]),
        ], model="claude-sonnet-5-5")
    if system == EXTRACT_SYSTEM:
        doc = content[0]
        n = len(doc["source"]["content"])
        last = n - 1
        summary = []
        if SUMMARY_ON in content[1]["text"]:
            summary = [
                text_block("\nSUMMARY (aim): "),
                text_block("Study " + doc["title"], [{"start_block_index": 0, "end_block_index": 1, "cited_text": "t"}]),
                text_block("\nSUMMARY (result): "),
                text_block("A main result", [{"start_block_index": last, "end_block_index": last + 1, "cited_text": "u"}]),
            ]
        if doc["title"].startswith("Off-topic"):
            return message([text_block("RELEVANCE: none - about an instrument"), *summary], model="claude-sonnet-5-5")
        return message([
            text_block("RELEVANCE: high - on topic\nMETHOD: "),
            text_block("Transit fits", [{"start_block_index": 0, "end_block_index": 1, "cited_text": "t"}]),
            text_block("\nFINDING: "),
            text_block("Radius grows with flux", [{"start_block_index": last, "end_block_index": last + 1, "cited_text": "u"}]),
            text_block("\nLIMITATION (stated): "),
            text_block("Small sample", [{"start_block_index": 0, "end_block_index": 1, "cited_text": "t"}]),
            *summary,
        ], model="claude-sonnet-5-5")
    if system == BASE_SYSTEM:
        task = content[1]["text"]
        if task.startswith("Task: write the review draft"):
            return message(DRAFT_V1, cache_write=50_000)
        if task.startswith("Task: revise"):
            return message(DRAFT_V2, cache_read=50_000)
        if task.startswith("Task: some sentences"):
            return message("\n".join(f"M{n}: {t}" for n, t in MEND_ITEM.findall(task)), cache_read=50_000)
        if task.startswith("Task: check"):
            lines = []
            for sid, text in re.findall(r"^S(\d+) \[[^\]]*\]: (.*)$", task, re.M):
                label = "unsupported" if "unsupportedclaim" in text else "partial" if "overstated" in text else "supported"
                lines.append(f"S{sid}: {label} - fake")
            return message("\n".join(lines), cache_read=50_000)
    raise AssertionError(f"unexpected request: {str(params)[:300]}")


@pytest.fixture
def setup(tmp_path):
    lib = Library(tmp_path / "library.sqlite")
    client = FakeClient(responder)
    kwargs = dict(ads=fake_ads(ads_route), arxiv=fake_arxiv(arxiv_route), oa=fake_oa(no_oa), llm_client=client,
                  poll_interval=0, log=lambda *a: None)
    return lib, client, kwargs, tmp_path


def test_full_run_cites_only_read_papers(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="What drives radius inflation in hot Jupiters?"), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"

    citable = lib.citable(run.run_id)
    assert citable == {P1, P2, P3, P4, P5, P6, P9}
    cards = {r["bibcode"]: r for r in lib.cards(run.run_id)}
    assert cards[P8]["status"] == "excluded"
    assert P7 not in cards
    assert cards[P3]["read_depth"] == "abstract_only" and cards[P3]["text_source"] == "abstract"
    assert cards[P1]["text_source"] == "arxiv_src" and cards[P1]["text_version"] == "astro-ph/0301001v2"
    assert P4PRE in lib.paper(P4).alternate_bibcodes
    assert lib.paper(P4).sort_date == "2010-12-20"
    assert lib.references([P5, P8, P4PRE]) == {P5: [P4PRE, P1, EXT, P7], P8: [], P4PRE: [P1, P2, P9, EXT]}
    screen_prompts = " ".join(c["messages"][0]["content"] for c in client.calls if c.get("system") == SCREEN_SYSTEM)
    assert "arXiv abstract of astro-ph/0301001" in screen_prompts  # arXiv's abstract, not ADS's, where one exists
    assert "Screened on their ADS abstract (no arXiv abstract available): 1" in (run.folder / "report.md").read_text()

    review = (run.folder / "review.md").read_text()
    assert "[@" not in review
    linked = {unquote(b) for b in re.findall(r"ui\.adsabs\.harvard\.edu/abs/([^/]+)/abstract", review)}
    assert linked and linked <= citable
    assert "Adams et al. (2003)" not in review and "unsupportedclaim" not in review
    assert "overstated everywhere" in review and "†" in review
    assert "a private comment" not in json.dumps([dict(r) for r in lib.db.execute("SELECT paragraphs FROM texts")])

    final = parse_draft((run.folder / "draft.md").read_text())
    papers = lib.papers(sorted(citable))
    assert check_chronology(final, papers, citable) == []

    bib = (run.folder / "refs.bib").read_text()
    assert all(b in bib for b in linked)
    report = (run.folder / "report.md").read_text()
    assert "[rule5]" in report and "[rule4]" in report and "[rule6]" in report
    record = json.loads((run.folder / "run.json").read_text())
    assert record["total_cost_usd"] > 0
    assert len((run.folder / "cards.jsonl").read_text().splitlines()) == len(cards)

    # Extraction went through Batches, which must not carry the fallbacks parameter.
    assert len(client.batch_params) == 8 and all("fallbacks" not in p for p in client.batch_params)
    # Drafting, revision and checks share one cached prefix: same system and same cards block.
    shared = [c for c in client.calls if c.get("system") == BASE_SYSTEM]
    assert len(shared) >= 3
    assert len({json.dumps([c["system"], c["messages"][0]["content"][0]], sort_keys=True) for c in shared}) == 1
    assert shared[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert len({json.dumps([c["model"], c["output_config"], c["thinking"]]) for c in shared}) == 1


def test_interrupted_run_resumes(setup):
    lib, client, kwargs, tmp_path = setup
    calls = {"n": 0}

    def flaky(params):
        if params.get("system") == BASE_SYSTEM and calls["n"] == 0:
            calls["n"] += 1
            raise KeyboardInterrupt
        return responder(params)

    client.responder = flaky
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    assert lib.run(run.run_id)["stage"] == "synthesize"
    searches = sum(1 for c in client.calls if c.get("system") == PLAN_SYSTEM)
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    assert sum(1 for c in client.calls if c.get("system") == PLAN_SYSTEM) == searches  # planning not redone
    assert (run.folder / "review.md").exists()


def test_cost_cap_pauses_the_run(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=0.0001), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    assert "cap" in lib.run(run.run_id)["error"]
    assert client.calls == []


# --- regressions from the code review ---------------------------------------------------------


def test_batch_refusal_retry_interrupted_then_resumed_counts_batch_cost_once(setup):
    lib, client, kwargs, tmp_path = setup
    state = {"interrupted": False}

    def flaky(params):
        system, content = params.get("system"), params["messages"][0]["content"]
        if system == EXTRACT_SYSTEM and content[0]["title"] == "Ohmic heating":
            if "betas" not in params:  # inside the batch
                return message("", stop_reason="refusal", model="claude-sonnet-5-5")
            if not state["interrupted"]:
                state["interrupted"] = True
                raise KeyboardInterrupt
        return responder(params)

    client.responder = flaky
    run = Run.create(lib, Settings(question="Radius inflation", expand=False), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    batch_rows = lib.db.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE run_id = ? AND batch = 1", (run.run_id,)).fetchone()["n"]
    assert batch_rows == len(client.batch_params) == 7
    assert lib.card(run.run_id, P5)["status"] == "verified"


def test_expansion_resumes_with_the_same_seeds(setup):
    lib, client, kwargs, tmp_path = setup
    operator_queries = []
    ads = kwargs["ads"]
    original = ads.search

    def counting(q, **kw):
        if "bibcode:(" in q:
            operator_queries.append(q)
        return original(q, **kw)

    ads.search = counting
    state = {"n": 0}

    def flaky(params):
        if params.get("system") == SCREEN_SYSTEM and "Tidal heating" in params["messages"][0]["content"] and not state["n"]:
            state["n"] += 1
            raise KeyboardInterrupt
        return responder(params)

    client.responder = flaky
    run = Run.create(lib, Settings(question="Radius inflation"), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    seeds = lib.get(run.run_id, "expansion")["seeds"]
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    assert len(operator_queries) == 5
    expansion = lib.get(run.run_id, "expansion")
    assert expansion["seeds"] == seeds and expansion["new"] == [P9]


def test_check_stage_resumes_without_repeating_paid_steps(setup):
    lib, client, kwargs, tmp_path = setup
    state = {"revised": False, "interrupted": False}

    def flaky(params):
        if params.get("system") == BASE_SYSTEM:
            task = params["messages"][0]["content"][1]["text"]
            if task.startswith("Task: revise"):
                state["revised"] = True
            elif task.startswith("Task: check") and state["revised"] and not state["interrupted"]:
                state["interrupted"] = True
                raise KeyboardInterrupt
        return responder(params)

    client.responder = flaky
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    tasks = [c["messages"][0]["content"][1]["text"][:20] for c in client.calls if c.get("system") == BASE_SYSTEM]
    assert tasks.count("Task: write the revi") == 1 and tasks.count("Task: revise the rev") == 1
    assert tasks.count("Task: check each num") == 3  # draft, interrupted revision check, revision check


def test_sentences_left_dangling_by_cuts_are_reworded_and_rechecked(setup):
    lib, client, kwargs, tmp_path = setup
    v2 = DRAFT_V2.replace(f"An unsupportedclaim persists [@{P5}].",
                          f"An unsupportedclaim persists [@{P5}]. It was tested twice [@{P5}].\n\n"
                          f"Another unsupportedclaim [@{P6}]. They agree [@{P6}]. A third unsupportedclaim [@{P2}]. "
                          f"This held up [@{P2}].")
    rewrites = {f"It was tested twice [@{P5}].": f"Ohmic heating was tested twice [@{P5}].",
                f"They agree [@{P6}].": f"They agree [@{P6}; @{P1}].",  # new citation: not used
                f"This held up [@{P2}].": f"This unsupportedclaim held up [@{P2}]."}  # fails the claim check
    state = {"interrupted": False}

    def dangling(params):
        if params.get("system") == BASE_SYSTEM:
            task = params["messages"][0]["content"][1]["text"]
            if task.startswith("Task: revise"):
                return message(v2)
            if task.startswith("Task: some sentences"):
                return message("\n".join(f"M{n}: {rewrites.get(t, t)}" for n, t in MEND_ITEM.findall(task)))
            if "Ohmic heating was tested" in task and not state["interrupted"]:
                state["interrupted"] = True
                raise KeyboardInterrupt
        return responder(params)

    client.responder = dangling
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    assert Run(lib, run.run_id, **kwargs).execute() == "done"

    review = (run.folder / "review.md").read_text()
    assert "Ohmic heating was tested twice" in review and "It was tested twice" not in review
    assert "They agree" in review and "This held up" in review and "unsupportedclaim" not in review
    report = (run.folder / "report.md").read_text()
    assert "which followed removed text: “It was tested twice" in report
    assert "its rewrite changed the citations" in report and "its rewrite was rated unsupported" in report
    tasks = [c["messages"][0]["content"][1]["text"] for c in client.calls if c.get("system") == BASE_SYSTEM]
    assert sum(t.startswith("Task: some sentences") for t in tasks) == 1  # not asked again on resume
    recheck = [t for t in tasks if t.startswith("Task: check") and "Ohmic heating was tested" in t]
    assert len(recheck) == 2 and "This unsupportedclaim held up" in recheck[-1] and "They agree" not in recheck[-1]


def test_names_in_headings_and_scope_never_reach_the_review(setup):
    lib, client, kwargs, tmp_path = setup

    def named(params):
        msg = responder(params)
        system = params.get("system")
        block = msg.content[0]
        if system == PLAN_SYSTEM:
            data = json.loads(block.text)
            data["interpretation"] = "Inflation since the discovery by Mayor & Queloz (1995)."
            block.text = json.dumps(data)
        elif system == BASE_SYSTEM and params["messages"][0]["content"][1]["text"].startswith("Task: revise"):
            block.text = block.text.replace("### 2000–2008: Early detections",
                                            f"### 2000–2008: After Charbonneau et al. [@{P1}] and [see @2099Fake....1....1Z]")
        return msg

    client.responder = named
    run = Run.create(lib, Settings(question="Radius inflation", expand=False), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    review = (run.folder / "review.md").read_text()
    assert "Queloz" not in review and "Charbonneau" not in review and "2099Fake" not in review
    # The theme is gone; the bounds may move because P9 (found only by expansion) isn't citable here.
    assert re.search(r"^### 2000–\d{4}$", review, re.M) and "As interpreted" not in review


def test_arxiv_api_throttling_falls_back_to_id_dates_and_html(setup):
    lib, client, kwargs, tmp_path = setup

    def throttled_export(host, path, params):
        if host.startswith("export."):
            return httpx.Response(429, text="Rate exceeded.")
        if path.startswith("/html/"):
            aid = path[6:].rsplit("v", 1)[0] if "v" in path[6:] else path[6:]
            title = ARXIV[aid][1]["title"][0]
            paras = "".join(f'<div class="ltx_para"><p class="ltx_p">{title} paragraph {i}: radius inflation in hot '
                            "Jupiters measured from transits and radial velocities, with careful treatment of the "
                            "systematics involved and a comparison with every earlier sample.</p></div>" for i in range(20))
            return httpx.Response(200, text=f'<html><body><article class="ltx_document"><section class="ltx_section">'
                                            f'<h2 class="ltx_title">Results</h2>{paras}</section></article></body></html>',
                                  headers={"content-type": "text/html"})
        return httpx.Response(404)

    logs = []
    kwargs = {**kwargs, "arxiv": fake_arxiv(throttled_export), "log": logs.append}
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    assert lib.paper(P4).sort_date == "2010-12-01"  # the month from 1012.0004, as no exact date could be fetched
    assert any("arXiv metadata unavailable" in line for line in logs)
    sources = {r["bibcode"]: r["text_source"] for r in lib.cards(run.run_id)}
    assert sources[P1] == "arxiv_html"
    assert "Screened on their ADS abstract (no arXiv abstract available): 8" in (run.folder / "report.md").read_text()


def test_full_text_blocked_by_throttling_pauses_instead_of_using_the_abstract(setup):
    lib, client, kwargs, tmp_path = setup
    state = {"throttled": True}

    def route(host, path, params):
        if state["throttled"] and (path.startswith("/src/") or path.startswith("/pdf/")):
            return httpx.Response(503)
        return arxiv_route(host, path, params)

    kwargs = {**kwargs, "arxiv": fake_arxiv(route)}
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    row = lib.run(run.run_id)
    assert row["stage"] == "fetch" and "rate limiting" in row["error"]
    assert not lib.cards(run.run_id)
    state["throttled"] = False
    assert Run(lib, run.run_id, **{**kwargs, "arxiv": fake_arxiv(route)}).execute() == "done"
    assert lib.card(run.run_id, P1)["text_source"] == "arxiv_src"


def test_summaries_are_written_once_per_full_text(setup):
    lib, client, kwargs, tmp_path = setup
    first = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                       runs_dir=tmp_path / "runs", **kwargs)
    assert first.execute() == "done"
    cards = {r["bibcode"]: r for r in lib.cards(first.run_id)}
    full = {b for b, r in cards.items() if r["read_depth"] == "full_text"}
    summarised = {r["bibcode"] for r in lib.db.execute("SELECT bibcode FROM summaries")}
    assert summarised == full and P1 in full and P3 not in summarised  # P3 was read from its abstract only
    assert cards[P8]["status"] == "excluded" and P8 in summarised  # off-topic here, still summarised
    assert all("summary" not in json.loads(r["card"]) for r in cards.values())
    row = lib.summaries([P1])[0]
    assert row["run_id"] == first.run_id and row["text_checksum"] == cards[P1]["text_checksum"]
    assert json.loads(row["summary"])["aim"][0]["text"] == "Study Inflation first seen"

    asked = len(client.calls)
    second = Run.create(lib, Settings(question="Ohmic heating in hot Jupiters", expand=False, use_batch=False),
                        runs_dir=tmp_path / "runs", **kwargs)
    assert second.execute() == "done"
    extract_asks = [c["messages"][0]["content"][1]["text"] for c in client.calls[asked:] if c.get("system") == EXTRACT_SYSTEM]
    assert extract_asks and all(SUMMARY_OFF in t for t in extract_asks)  # the same texts were summarised already
    assert len(lib.db.execute("SELECT * FROM summaries").fetchall()) == len(full)


def test_progress_lines_are_kept_in_the_library(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    lines = [e["line"] for e in lib.events(run.run_id)]
    assert lines[0] == "[plan]" and "[render]" in lines and lines[-1].startswith("done:")
    seqs = [e["seq"] for e in lib.events(run.run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert [e["seq"] for e in lib.events(run.run_id, after=seqs[-3])] == seqs[-2:]


def test_a_run_cannot_execute_twice_at_once(setup):
    from litreview.pipeline import RunBusy, run_is_active, run_lock

    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False), runs_dir=tmp_path / "runs", **kwargs)
    assert not run_is_active(lib, run.run_id)
    with run_lock(lib, run.run_id):  # as if another process were running it
        assert run_is_active(lib, run.run_id)
        with pytest.raises(RunBusy):
            Run(lib, run.run_id, **kwargs).execute()
    assert not run_is_active(lib, run.run_id)
    assert Run(lib, run.run_id, **kwargs).execute() == "done"


def test_cost_cap_pause_records_what_the_next_call_needs(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", cost_cap=0.0001), runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "paused"
    pause = lib.get(run.run_id, "cap_pause")
    assert pause["cap"] == 0.0001 and pause["estimate"] > 0 and pause["spent"] >= 0
    settings = json.loads(lib.run(run.run_id)["settings"])
    settings["cost_cap"] = 10.0
    lib.set_settings(run.run_id, settings)
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    assert lib.get(run.run_id, "cap_pause") is None


def test_take_turns_alternates_and_skips_what_is_taken():
    assert take_turns([["a", "b", "c", "d"], ["c", "a", "e"]], 4) == [("a", 0), ("c", 1), ("b", 0), ("e", 1)]
    assert take_turns([["a", "b"], []], 5) == [("a", 0), ("b", 0)]
    assert take_turns([["a"], ["a"]], 3) == [("a", 0)]


def test_half_the_papers_read_are_the_ones_the_others_cite_most(setup):
    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False, max_papers=2),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    # Among the included papers, P1 is cited by P2, P3, P4 and P5, and P4 by P5 (as its preprint) and P6.
    top = run.get("merged")[0]["bibcode"]
    picked = P4 if top == P1 else P1
    assert run.get("selected") == [top, picked]
    assert run.get("chosen_for_citations") == {picked: 2 if picked == P4 else 4}
    assert [b for b, *_ in run.get("not_read")] == [b for b, *_ in run._ranked_inclusions() if b not in (top, picked)]
    report = (run.folder / "report.md").read_text()
    assert f"cited by {2 if picked == P4 else 4} included papers" in report and "search rank" in report


EXT_DOC = ads_doc(EXT, 2001, "An early radius measurement", ["Zane, Z."], abstract="Radii were measured early.")


def ads_with_ext(path, params, body):
    """The fake ADS, plus a record for EXT, which no search finds."""
    status, data, headers = ads_route(path, params, body)
    if path == "/search/query" and params["q"].startswith("identifier:(") and EXT in params["q"]:
        docs = [*data["response"]["docs"], EXT_DOC]
        return 200, {"response": {"numFound": len(docs), "docs": docs}}, None
    return status, data, headers


def test_papers_added_to_a_finished_review_are_read_and_the_review_written_again(setup):
    lib, client, kwargs, tmp_path = setup
    kwargs = {**kwargs, "ads": fake_ads(ads_with_ext)}
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False, max_papers=3),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    first = (run.folder / "review.md").read_text()
    read = run.get("selected")
    skipped_paper = next(b for b, *_ in run.get("not_read") if b != P8)  # P8's card would be excluded
    calls = len(client.calls)

    with pytest.raises(KeyError):
        request_additions(lib, "nope", [EXT])
    wanted, skipped = request_additions(lib, run.run_id, [skipped_paper, EXT, "2099Fake....1....1Z", read[0], "nonsense"])
    assert wanted == [skipped_paper, EXT, "2099Fake....1....1Z"]
    assert [s["reason"] for s in skipped] == ["already read for this review", "not an ADS bibcode"]
    with pytest.raises(ValueError):
        request_additions(lib, run.run_id, [EXT])  # this one isn't finished yet

    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    assert run.get("selected") == [*read, skipped_paper, EXT]
    assert skipped_paper not in [b for b, *_ in run.get("not_read")]
    assert lib.citable(run.run_id) >= {skipped_paper, EXT}
    record = run.get("additions")[-1]
    assert record["added"] == [skipped_paper, EXT] and record["not_found"] == ["2099Fake....1....1Z"]
    assert record["previous"] == "v1" and record["done"]
    assert (run.folder / "v1" / "review.md").read_text() == first
    report = (run.folder / "report.md").read_text()
    assert "### Added after the review was first written" in report and "added later" in report
    assert "Not in ADS: 2099Fake....1....1Z" in report

    # Only the new papers were read; nothing was planned or screened again; the review was written again.
    new = client.calls[calls:]
    assert not [c for c in new if c.get("system") in (PLAN_SYSTEM, SCREEN_SYSTEM)]
    assert sorted(c["messages"][0]["content"][0]["title"] for c in new if c.get("system") == EXTRACT_SYSTEM) == sorted(
        [lib.paper(skipped_paper).title, "An early radius measurement"])
    tasks = [c["messages"][0]["content"][1]["text"] for c in new if c.get("system") == BASE_SYSTEM]
    assert sum(t.startswith("Task: write the review draft") for t in tasks) == 1

    # A paper ADS doesn't know changes nothing, and costs nothing.
    calls = len(client.calls)
    request_additions(lib, run.run_id, ["2098Fake....1....1Y"])
    assert Run(lib, run.run_id, **kwargs).execute() == "done"
    assert len(client.calls) == calls and not (run.folder / "v2").exists()
    assert lib.run(run.run_id)["stage"] == "done" and run.get("additions")[-1]["not_found"] == ["2098Fake....1....1Y"]


def test_run_folder_falls_back_to_runs_dir_by_name(tmp_path):
    from litreview.pipeline import resolve_folder

    there = tmp_path / "runs" / "2026-10-15-x"
    there.mkdir(parents=True)
    assert resolve_folder(str(there), tmp_path / "elsewhere") == there
    assert resolve_folder("/app/runs/2026-10-15-x", tmp_path / "runs") == there


def test_books_are_marked_for_screening(setup):
    from litreview import plan as planning
    from litreview.library import Paper
    from litreview.llm import LLM

    lib, client, kwargs, tmp_path = setup
    s = Settings(question="Bars")
    papers = [Paper(bibcode="2008gady.book.....B", pubdate="2008-00-00", title="Galactic Dynamics", doctype="book"),
              Paper(bibcode="2011ASSL..123...45X", pubdate="2011-00-00", title="Bars", doctype="inbook",
                    ads_abstract="A chapter on bars."),
              Paper(bibcode=P1, pubdate="2003-00-00", title="Inflation first seen", doctype="article")]
    scores = planning.screen(LLM(lib, "screen-test", 5.0, client=client), s.models["screen"], s.efforts["screen"],
                             "Bars", "Bars in disks", papers)
    sent = client.calls[-1]["messages"][0]["content"]
    assert "[P1] 2008 | Galactic Dynamics [book]\n(no abstract)" in sent
    assert "[P2] 2011 | Bars [book chapter]\nA chapter on bars." in sent and "[P3] 2003 | Inflation first seen\n" in sent
    assert set(scores) == {p.bibcode for p in papers}


def test_a_pdf_uploaded_for_a_paper_a_review_read_from_its_abstract_is_read_again(setup):
    from test_fulltext import BOOK, make_pdf

    from litreview.papers import attach_pdf

    lib, client, kwargs, tmp_path = setup
    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **kwargs)
    assert run.execute() == "done"
    assert run.get("texts")[P2]["source"] == "abstract" and lib.card(run.run_id, P2)["read_depth"] == "abstract_only"
    assert request_additions(lib, run.run_id, [P2])[1] == [{"bibcode": P2, "reason": "already read for this review"}]

    text = attach_pdf(lib, lib.paper(P2), make_pdf(BOOK), (2, 4))
    assert lib.best_text([P2]).checksum == text.checksum
    wanted, skipped = request_additions(lib, run.run_id, [P2])
    assert wanted == [P2] and not skipped
    calls = len(client.calls)
    assert Run(lib, run.run_id, **kwargs).execute() == "done"

    card = lib.card(run.run_id, P2)
    assert (card["text_source"], card["text_version"], card["read_depth"]) == ("upload", text.version, "full_text")
    assert P2 in lib.citable(run.run_id) and run.get("selected").count(P2) == 1
    assert run.get("additions")[-1]["reread"] == [P2]
    reads = [c["messages"][0]["content"][0] for c in client.calls[calls:] if c.get("system") == EXTRACT_SYSTEM]
    assert [d["title"] for d in reads] == ["Inflation and flux"]
    assert reads[0]["context"].endswith("Read depth: pages 2-4 of an uploaded PDF.")
    report = (run.folder / "report.md").read_text()
    assert "search rank, read again from a newer text" in report and f"({P2} again, from a newer text)" in report
    assert request_additions(lib, run.run_id, [P2])[1][0]["reason"] == "already read for this review"
