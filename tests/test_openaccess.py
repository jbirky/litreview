"""Full text beyond arXiv: ADS's scans and open-access copies found through Unpaywall, read by OCR
where they are scans."""

import httpx
import pytest

from fakes import ads_doc, fake_arxiv, fake_oa, no_oa
from test_pipeline import setup  # noqa: F401  (a fixture)
from test_fulltext import close, long_body, make_pdf, scanned
from litreview.ads import paper_from_record
from litreview.config import tessdata, unpaywall_email
from litreview.library import Library
from litreview.openaccess import OpenAccessClient
from litreview.papers import RateLimited, can_fetch_open_access, read_full_text

OLD = "1975ApJ...199L.147W"
NEW = "2016ApJ...820...27S"
DOI = "10.3847/0004-637X/820/1/27"
PDF = make_pdf([[("1 Introduction", 14), (long_body(i), 10), (long_body(i + 1), 10)] for i in (1, 3, 5, 7)])


def record(bibcode, *, esources=(), doi=None, arxiv=None):
    doc = ads_doc(bibcode, int(bibcode[:4]), "A paper", ["Weisskopf, M."], arxiv=arxiv, doi=doi)
    doc["esources"] = list(esources)
    return paper_from_record(doc)


def pdf_response(data=PDF, status=200):
    return httpx.Response(status, content=data, headers={"content-type": "application/pdf"})


def unpaywall(*links, is_oa=True):
    locations = [{"url_for_pdf": u, "host_type": "repository"} for u in links]
    return httpx.Response(200, json={"is_oa": is_oa, "best_oa_location": locations[0] if locations else None,
                                     "oa_locations": locations})


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "library.sqlite")


def no_arxiv(host, path, params):
    raise AssertionError(f"unexpected arXiv request: {host}{path}")


def test_a_paper_arxiv_lacks_is_read_from_the_scan_ads_hosts(lib):
    p = record(OLD, esources=["ADS_PDF", "ADS_SCAN"])
    lib.upsert_paper(p)
    asked = []

    def route(host, path, params):
        asked.append(host + path)
        if path == f"/link_gateway/{OLD}/ADS_PDF":
            if len(asked) == 1:
                return httpx.Response(504)  # ADS's gateway times out now and then; tried once more
            return httpx.Response(302, headers={"location": f"https://articles.adsabs.harvard.edu/pdf/{OLD}"})
        if host == "articles.adsabs.harvard.edu":
            return pdf_response()
        return no_oa(host, path, params)

    text, why = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(route))
    assert why == "" and text.source == "ads_scan" and len(text.paragraphs) == 8
    assert asked == [f"ui.adsabs.harvard.edu/link_gateway/{OLD}/ADS_PDF"] * 2 + [f"articles.adsabs.harvard.edu/pdf/{OLD}"]
    log = lib.db.execute("SELECT source, ok, reason FROM fetch_log WHERE bibcode = ? ORDER BY at", (OLD,)).fetchall()
    assert [tuple(r) for r in log] == [("arxiv", 0, "no arXiv version"),
                                       ("ads_scan", 1, f"https://articles.adsabs.harvard.edu/pdf/{OLD}")]
    # Stored, so it is never fetched again.
    assert read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(no_oa))[0].checksum == text.checksum


def test_unpaywall_finds_an_open_access_copy_and_bad_copies_are_passed_over(lib):
    p = record(NEW, doi=DOI)
    lib.upsert_paper(p)
    asked = []

    def route(host, path, params):
        asked.append(host)
        if host == "api.unpaywall.org":
            assert path == f"/v2/{DOI}" and params == {"email": "test@example.org"}
            return unpaywall("https://arxiv.org/pdf/1601.00001", "https://publisher.example/login",
                             "https://repository.example/paper.pdf")
        if host == "publisher.example":
            return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})
        if host == "repository.example":
            return pdf_response()
        return no_oa(host, path, params)

    text, why = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(route))
    assert why == "" and text.source == "unpaywall"
    assert asked == ["api.unpaywall.org", "publisher.example", "repository.example"]  # arXiv's copy is skipped
    failures = lib.fetch_failures([NEW])
    assert [r["reason"] for r in failures if r["source"] == "unpaywall"] == [
        "not a PDF (a web page, perhaps a sign-in or a robot check)"]


def test_the_contact_goes_only_to_ads(monkeypatch):
    monkeypatch.setenv("LITREVIEW_CONTACT", "jo@uni.example.edu")
    agents = {}

    def handler(request):
        agents[request.url.host] = request.headers["user-agent"]
        if request.url.host == "api.unpaywall.org":
            return unpaywall("https://repository.example/paper.pdf")
        return pdf_response()

    oa = OpenAccessClient(http=httpx.Client(transport=httpx.MockTransport(handler)), interval=0)
    oa.ads_scan(OLD), oa.unpaywall(DOI), oa.pdf("https://repository.example/paper.pdf")
    assert agents["ui.adsabs.harvard.edu"].endswith("(jo@uni.example.edu)")
    assert "jo@" not in agents["api.unpaywall.org"] + agents["repository.example"]


def test_a_host_that_shows_a_robot_check_is_not_asked_again(lib):
    papers = [record(f"2024AJ....168..{n}P", doi=f"10.3847/test.{n}") for n in (253, 254)]
    asked = []

    def route(host, path, params):
        asked.append(host)
        if host == "api.unpaywall.org":
            n = path.rsplit(".", 1)[1]
            return unpaywall(f"https://publisher.example/{n}/pdf", f"https://repository.example/{n}.pdf")
        if host == "publisher.example":
            return httpx.Response(302, headers={"location": "https://validate.botcheck.example/?from=publisher"})
        if host == "validate.botcheck.example":
            return httpx.Response(200, text="<title>Bot Manager Captcha</title>", headers={"content-type": "text/html"})
        if host == "repository.example":
            return httpx.Response(403)
        return no_oa(host, path, params)

    oa = fake_oa(route)
    for p in papers:
        lib.upsert_paper(p)
        assert read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: oa)[0] is None
    assert asked == ["api.unpaywall.org", "publisher.example", "validate.botcheck.example", "repository.example",
                     "api.unpaywall.org"]  # neither host is asked about the second paper
    reasons = [r["reason"] for r in lib.fetch_failures([papers[1].bibcode]) if r["source"] == "unpaywall"]
    assert reasons == ["skipped: publisher.example showed a robot check earlier, so it isn't asked again",
                       "skipped: repository.example refused a download (http 403) earlier, so it isn't asked again"]


def test_a_copy_whose_landing_page_is_a_pdf_is_read(lib):
    p = record(NEW, doi=DOI)
    lib.upsert_paper(p)

    def route(host, path, params):
        if host == "api.unpaywall.org":
            copies = [{"url_for_pdf": None, "url_for_landing_page": "https://doi.org/" + DOI},
                      {"url_for_pdf": None, "url_for_landing_page": "https://repository.example/1/aa47162-23.PDF"}]
            return httpx.Response(200, json={"is_oa": True, "best_oa_location": copies[0], "oa_locations": copies})
        if host == "repository.example":
            return pdf_response()
        return no_oa(host, path, params)

    text, why = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(route))
    assert why == "" and text.source == "unpaywall"


def test_without_an_email_unpaywall_is_never_asked(lib):
    p = record(NEW, doi=DOI)
    lib.upsert_paper(p)
    text, why = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(no_oa, email=None))
    assert text is None and why == "no arXiv version; upload a PDF of it to read it in full"


def test_unpaywall_falls_back_to_an_address_in_the_contact(monkeypatch):
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.setenv("LITREVIEW_CONTACT", "Jo Doe <jo.doe+lit@uni.example.edu>")
    assert unpaywall_email() == OpenAccessClient().email == "jo.doe+lit@uni.example.edu"
    assert can_fetch_open_access(record(NEW, doi=DOI))
    monkeypatch.setenv("UNPAYWALL_EMAIL", "other@example.org")
    assert unpaywall_email() == "other@example.org"
    monkeypatch.delenv("UNPAYWALL_EMAIL")
    monkeypatch.setenv("LITREVIEW_CONTACT", "https://example.org/jo")
    assert unpaywall_email() is None and not can_fetch_open_access(record(NEW, doi=DOI))


def test_why_nothing_could_be_read_names_every_source(lib):
    p = record(OLD, esources=["ADS_PDF"], doi=DOI)
    lib.upsert_paper(p)

    def route(host, path, params):
        if host == "ui.adsabs.harvard.edu":
            return httpx.Response(404)
        return unpaywall(is_oa=False)

    text, why = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(route))
    assert text is None
    assert why == ("no arXiv version; ADS scan: http 404; Unpaywall: no open-access copy; "
                   "upload a PDF of it to read it in full")


def test_a_throttled_arxiv_pauses_instead_of_reading_a_poorer_copy(lib):
    p = record(OLD, esources=["ADS_PDF", "EPRINT_PDF"], arxiv="1601.00001")
    lib.upsert_paper(p)
    throttled = lambda host, path, params: httpx.Response(429)  # noqa: E731
    with pytest.raises(RateLimited):
        read_full_text(lib, p, arxiv=fake_arxiv(throttled), oa=lambda: fake_oa(no_oa))


def test_pdfs_that_are_too_big_or_missing_are_refused(lib, monkeypatch):
    from litreview import openaccess

    monkeypatch.setattr(openaccess, "MAX_PDF_BYTES", 1000)
    client = fake_oa(lambda host, path, params: pdf_response() if path == "/big.pdf" else httpx.Response(410))
    assert client.pdf("https://x.example/big.pdf") == (None, "over 0 MB")
    assert client.pdf("https://x.example/gone.pdf") == (None, "http 410")


@pytest.mark.skipif(tessdata() is None, reason="OCR needs Tesseract's English data (see the README)")
def test_a_scan_with_no_text_layer_is_read_by_ocr(lib):
    p = record(OLD, esources=["ADS_PDF"])
    lib.upsert_paper(p)

    def route(host, path, params):
        return pdf_response(scanned(PDF))

    text, _ = read_full_text(lib, p, arxiv=fake_arxiv(no_arxiv), oa=lambda: fake_oa(route))
    assert text.source == "ads_scan" and text.version.endswith(":ocr")
    assert close([x["text"] for x in text.paragraphs], [long_body(i) for i in range(1, 9)])


def test_a_review_reads_a_paper_arxiv_lacks_from_the_scan_ads_hosts(setup):
    import test_pipeline as tp
    from fakes import fake_ads
    from litreview.config import Settings
    from litreview.pipeline import Run
    from litreview.web.views import paper_view

    lib, client, kwargs, tmp_path = setup
    scan_doc = {**tp.DOCS[tp.P2], "esources": ["ADS_PDF", "ADS_SCAN"]}  # P2 has no arXiv version

    def ads(path, params, body):
        status, data, headers = tp.ads_route(path, params, body)
        if path == "/search/query":
            docs = [scan_doc if d["bibcode"] == tp.P2 else d for d in data["response"]["docs"]]
            data = {"response": {**data["response"], "docs": docs}}
        return status, data, headers

    def oa(host, path, params):
        assert path == f"/link_gateway/{tp.P2}/ADS_PDF"
        return pdf_response()

    run = Run.create(lib, Settings(question="Radius inflation", expand=False, use_batch=False),
                     runs_dir=tmp_path / "runs", **{**kwargs, "ads": fake_ads(ads), "oa": fake_oa(oa)})
    assert run.execute() == "done"
    assert run.get("texts")[tp.P2]["source"] == "ads_scan"
    card = lib.card(run.run_id, tp.P2)
    assert (card["text_source"], card["read_depth"]) == ("ads_scan", "full_text") and tp.P2 in lib.citable(run.run_id)
    assert "ads_scan" in (run.folder / "report.md").read_text()
    text = paper_view(lib, tp.P2)["texts"][0]
    assert text["label"] == "ADS scan" and text["origin"] == f"https://ui.adsabs.harvard.edu/link_gateway/{tp.P2}/ADS_PDF"
