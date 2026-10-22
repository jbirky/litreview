import time

import pytest

from fakes import ads_doc, atom, fake_ads
import re

from litreview.ads import MAX_Q, ADSQuotaLow, arxiv_id_from_record, build_query, identifier_queries, paper_from_record
import httpx

from fakes import fake_arxiv
from litreview.arxiv import ArxivThrottled, RateLimiter, normalize_id, parse_atom, posting_month, split_version
from litreview.library import Library, Paper, StoredText
from litreview.plan import merge


def test_arxiv_ids_from_ads_records():
    assert arxiv_id_from_record({"identifier": ["arXiv:1801.06117"]}) == "1801.06117"
    assert arxiv_id_from_record({"identifier": ["arXiv:astro-ph/0501001v2"]}) == "astro-ph/0501001"
    assert arxiv_id_from_record({"doi": ["10.48550/arXiv.2101.01234"]}) == "2101.01234"
    assert arxiv_id_from_record({"alternate_bibcode": ["2018arXiv180106117D"]}) == "1801.06117"
    assert arxiv_id_from_record({"alternate_bibcode": ["2014arXiv1410.4199W"]}) == "1410.4199"
    assert arxiv_id_from_record({"identifier": ["10.1146/annurev-astro-081817-051853"]}) is None


def test_ids_and_versions():
    assert normalize_id("https://arxiv.org/abs/2101.01234v3") == "2101.01234v3"
    assert split_version("2101.01234v3") == ("2101.01234", "v3")
    assert split_version("astro-ph/0501001") == ("astro-ph/0501001", None)


def test_parse_atom_takes_first_posting_and_latest_version():
    meta = parse_atom(atom([("2101.01234v3", "2021-01-04", "A  title", "An\n abstract.")]))
    m = meta["2101.01234"]
    assert (m.latest_version, m.published, m.title, m.abstract) == ("2101.01234v3", "2021-01-04", "A title", "An abstract.")


def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept = []
    lim = RateLimiter(3.0, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    lim.wait()
    t[0] += 1.0
    lim.wait()
    lim.wait()
    assert slept == [2.0, 3.0]


def test_build_query_adds_filters():
    q = build_query('abs:"hot Jupiter"', 2000, None, False)
    assert q == ('(abs:"hot Jupiter") AND year:2000-3000 AND '
                 '((property:refereed OR doctype:(eprint OR book OR inbook)) NOT doctype:(abstract OR erratum))')
    assert "eprint" not in build_query("x", None, None, True) and "book" not in build_query("x", None, None, True)


def test_ads_client_search_export_and_quota():
    seen = []

    def route(path, params, body):
        seen.append((path, params, body))
        if path == "/search/query":
            return 200, {"response": {"numFound": 1, "docs": [ads_doc("2019ApJ...1....1A", 2019, "T", ["A, B."])]}}, {"x-ratelimit-remaining": "40"}
        if path == "/export/bibtex":
            return 200, {"export": "@ARTICLE{x}"}, None
        return 404, {}, None

    ads = fake_ads(route)
    n, docs = ads.search("q", rows=5)
    assert n == 1 and docs[0]["bibcode"] == "2019ApJ...1....1A"
    assert seen[0][1]["rows"] == "5" and "identifier" in seen[0][1]["fl"]
    with pytest.raises(ADSQuotaLow):
        ads.export_bibtex(["x"])  # 40 left is under the floor of 50


def test_quota_is_shared_through_a_file(tmp_path):
    from litreview.ads import quota_used, record_quota

    f = tmp_path / "ads-quota.json"
    assert quota_used(f) is None
    soon = str(time.time() + 3600)
    record_quota(f, "/search/query", {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4880", "x-ratelimit-reset": soon})
    record_quota(f, "/search/query", {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4890", "x-ratelimit-reset": soon})  # arrived late
    record_quota(f, "/export/bibtex", {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "99", "x-ratelimit-reset": soon})
    assert quota_used(f) == {"used": 120, "limit": 5000, "reset": float(soon)}
    assert quota_used(f, "export")["used"] == 1
    record_quota(f, "/search/query", {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "10", "x-ratelimit-reset": "1700000000"})
    assert quota_used(f) == {"used": 0, "limit": 5000, "reset": None}  # that day's quota has since reset


def test_paper_from_record():
    p = paper_from_record(ads_doc("2019ApJ...880....1S", 2019, "T", ["Smith, J."], arxiv="1901.00001",
                                  alternates=("2019arXiv190100001S",)))
    assert p.arxiv_id == "1901.00001" and p.refereed and p.alternate_bibcodes == ["2019arXiv190100001S"]
    assert p.sort_date == "2019-03-01"
    assert p.references == []  # ADS leaves `reference` out when a paper cites nothing it knows
    assert paper_from_record(ads_doc("2019ApJ...880....1S", 2019, "T", ["S, J."], refs=["2001A&A...1....1B"])).references == ["2001A&A...1....1B"]


def test_identifier_queries_stay_within_the_ads_limit():
    bibcodes = [f"20{i % 100:02d}A&A...{i:03d}A..{i % 10}X" for i in range(120)]
    queries = identifier_queries(bibcodes)
    assert len(queries) > 2 and all(len(q) <= MAX_Q and q.startswith("identifier:(") for q in queries)
    assert [b for q in queries for b in re.findall(r'"([^"]+)"', q)] == bibcodes


def test_references_are_keyed_by_the_bibcode_asked_for(tmp_path):
    def route(path, params, body):
        docs = [{"bibcode": "2011ApJ...730....4D", "alternate_bibcode": ["2010arXiv1012.0004D"], "reference": ["A", "B"]},
                {"bibcode": "2012MNRAS.1....1E"}]
        return 200, {"response": {"numFound": 2, "docs": docs}}, None

    found = fake_ads(route).references(["2010arXiv1012.0004D", "2012MNRAS.1....1E", "2099Nope.....1Z"])
    assert found == {"2010arXiv1012.0004D": ["A", "B"], "2012MNRAS.1....1E": []}

    lib = Library(tmp_path / "library.sqlite")
    lib.upsert_paper(Paper(bibcode="X", references=["A"]))
    lib.upsert_paper(Paper(bibcode="X", title="Refreshed from the library, with no references loaded"))
    assert lib.references(["X", "Y"]) == {"X": ["A"]}


def test_merge_dedupes_preprint_and_journal_versions():
    journal = paper_from_record(ads_doc("2019ApJ...880....1S", 2019, "T", ["Smith, J."], arxiv="1901.00001", citations=30))
    preprint = paper_from_record(ads_doc("2019arXiv190100001S", 2019, "T", ["Smith, J."], arxiv="1901.00001", refereed=False))
    other = paper_from_record(ads_doc("2020MNRAS.1....2J", 2020, "U", ["Jones, K."], doi="10.1/X"))
    other_dup = paper_from_record(ads_doc("2020MNRAS.1....2Z", 2020, "U", ["Jones, K."], doi="10.1/x"))
    merged = merge([("q1", 1, preprint), ("q2", 3, journal), ("q2", 1, other), ("q1", 2, other_dup)])
    assert len(merged) == 2
    top = {c.paper.bibcode: c for c in merged}
    smith = top["2019ApJ...880....1S"]
    assert smith.queries == {"q1", "q2"} and smith.best_rank == 1
    assert "2019arXiv190100001S" in smith.paper.alternate_bibcodes


def test_library_citable_needs_record_text_and_verified_card(tmp_path):
    lib = Library(tmp_path / "lib.sqlite")
    lib.create_run("r", "q", {}, str(tmp_path))
    for b in ("A", "B", "C", "D"):
        lib.upsert_paper(Paper(bibcode=b, pubdate="2010-01-00"))
    ta = lib.add_text("A", "arxiv_src", "v1", [{"label": "x", "text": "y"}])
    tb = lib.add_text("B", "abstract", "ads:1", [{"label": "Abstract", "text": "y"}])
    tc = lib.add_text("C", "arxiv_src", "v1", [{"label": "x", "text": "y"}])
    lib.put_card("r", "A", {}, verified=True, status="verified", read_depth="full_text", text=ta)
    lib.put_card("r", "B", {}, verified=True, status="verified", read_depth="abstract_only", text=tb)
    lib.put_card("r", "C", {}, verified=False, status="excluded", read_depth="full_text", text=tc)
    missing = StoredText("D", "arxiv_src", "v9", [], False, "0" * 64)
    lib.put_card("r", "D", {}, verified=True, status="verified", read_depth="full_text", text=missing)
    assert lib.citable("r") == {"A", "B"}


def test_stored_texts_are_never_overwritten(tmp_path):
    lib = Library(tmp_path / "lib.sqlite")
    lib.create_run("r", "q", {}, str(tmp_path))
    lib.upsert_paper(Paper(bibcode="A", pubdate="2010-01-00"))
    first = lib.add_text("A", "arxiv_src", "1001.0001v1", [{"label": "x", "text": "original"}])
    lib.put_card("r", "A", {}, verified=True, status="verified", read_depth="full_text", text=first)
    again = lib.add_text("A", "arxiv_src", "1001.0001v1", [{"label": "x", "text": "re-parsed differently"}])
    assert again.paragraphs[0]["text"] == "original" and lib.citable("r") == {"A"}
    # If the row is changed behind the library's back, the card no longer counts.
    lib.db.execute("UPDATE texts SET paragraphs = '[]', checksum = 'x' WHERE bibcode = 'A'")
    assert lib.citable("r") == set()


def test_upsert_keeps_arxiv_details_and_recomputes_sort_date(tmp_path):
    lib = Library(tmp_path / "lib.sqlite")
    lib.upsert_paper(Paper(bibcode="A", pubdate="2012-05-00"))
    lib.set_arxiv("A", arxiv_id="1111.1111", first_date="2011-11-02", abstract="arXiv abstract")
    lib.upsert_paper(Paper(bibcode="A", pubdate="2012-05-00", ads_abstract="ADS abstract"))
    p = lib.paper("A")
    assert p.first_arxiv_date == "2011-11-02" and p.abstract == "arXiv abstract"
    row = lib.db.execute("SELECT sort_date FROM papers WHERE bibcode='A'").fetchone()
    assert row["sort_date"] == "2011-11-02"


def test_redirects_are_rate_limited_per_hop():
    import httpx
    from fakes import fake_arxiv

    def route(host, path, params):
        if path == "/html/2101.01234":
            return httpx.Response(301, headers={"location": "https://arxiv.org/html/2101.01234v2"})
        return httpx.Response(200, text="<html>ok</html>")

    client = fake_arxiv(route)
    waits = []
    client.main_limiter.wait = lambda: waits.append("main")
    resp = client.html("2101.01234")
    assert resp.status_code == 200 and str(resp.url).endswith("2101.01234v2")
    assert waits == ["main", "main"]


def test_shared_rate_limiter_spaces_separate_processes(tmp_path):
    from litreview.arxiv import SharedRateLimiter

    slept = []
    a = SharedRateLimiter(3.0, tmp_path / "lock", sleep=slept.append)
    b = SharedRateLimiter(3.0, tmp_path / "lock", sleep=slept.append)
    a.wait()
    b.wait()  # a different limiter object, as in another process, still has to wait
    assert len(slept) == 1 and 2.0 < slept[0] <= 3.0


def test_posting_month_comes_from_the_id():
    assert posting_month("1801.06117") == "2018-01-00"
    assert posting_month("2101.01234v3") == "2021-01-00"
    assert posting_month("0704.0001") == "2007-04-00"
    assert posting_month("astro-ph/9911436") == "1999-11-00"
    assert posting_month("hep-th/0002110v2") == "2000-02-00"
    assert posting_month("math.GT/0309136") == "2003-09-00"
    assert posting_month("not-an-id") is None


def test_a_throttled_host_is_skipped_for_the_rest_of_the_run():
    hits = []

    def route(host, path, params):
        hits.append((host, path))
        return httpx.Response(429, text="Rate exceeded.") if host.startswith("export.") else httpx.Response(200, text="ok")

    client = fake_arxiv(route)
    with pytest.raises(ArxivThrottled):
        client.metadata(["1801.06117"])
    assert len(hits) == client.max_retries + 1
    with pytest.raises(ArxivThrottled):
        client.source("1801.06117")  # same host: refused without a request
    assert len(hits) == client.max_retries + 1
    assert client.html("1801.06117").status_code == 200  # arxiv.org is still asked
    assert set(client.throttled) == {"export.arxiv.org"}


def test_a_throttled_host_is_tried_again_after_the_cooldown():
    now = [0.0]
    calls = []

    def route(host, path, params):
        calls.append(path)
        return httpx.Response(429) if len(calls) <= 4 else httpx.Response(200, text="ok")

    client = fake_arxiv(route)
    client.clock = lambda: now[0]
    with pytest.raises(ArxivThrottled):
        client.source("1801.06117")
    now[0] += 60
    with pytest.raises(ArxivThrottled):
        client.source("1801.06117")
    assert len(calls) == 4
    now[0] += 900
    assert client.source("1801.06117").status_code == 200 and not client.throttled
