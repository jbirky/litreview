"""Browser checks for the read-only web UI (UI-1), run in the Playwright image by tests/e2e/run.

    tests/e2e/run demo <url>        # a demo library with a finished review (see make_demo.py)
    tests/e2e/run runs <url>        # start, watch, pause and continue reviews (needs serve_demo.py)
    tests/e2e/run tasks <url>       # Get text and Summarize in the library (needs serve_demo.py)
    tests/e2e/run passages <url> <run_id>
        # every card passage in a run opens the stored text at the paragraph it cites
    tests/e2e/run share <url> <run_id>
        # the review's shareable page, downloaded from the UI and opened from the file offline
    tests/e2e/run graph <url> <run_id>
        # the citation graph; fetches missing citation links first if the run has any (needs a worker)
    tests/e2e/run add <url>         # add a paper to the demo's finished review (needs serve_demo.py)
    tests/e2e/run upload <url>      # upload a PDF for a paper read from its abstract, and read it again
                                    # in the demo's finished review (needs a fresh serve_demo.py)

Screenshots go to tests/e2e/shots/.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SHOTS = Path(__file__).parent / "shots"


def demo(page, url):
    page.goto(url)
    expect(page.locator(".run")).to_have_count(2)
    page.locator(".run", has_text="What drives radius inflation").click()

    # The review renders with author-year citations and no raw keys.
    expect(page.locator(".prose h2").first).to_be_visible()
    assert "[@" not in page.locator(".prose").inner_text()
    cites = page.locator(".prose .cite-btn")
    assert cites.count() > 3

    # A citation opens its card; a passage opens the stored text at that paragraph.
    cites.first.click()
    panel = page.locator(".panel")
    expect(panel.locator(".items").first).to_be_visible()
    chip = panel.locator(".passage").first
    i = chip.get_attribute("data-i")
    chip.click()
    focused = panel.locator(".para.focus")
    expect(focused).to_have_count(1)
    assert focused.get_attribute("data-i") == i
    expect(focused).to_be_in_viewport()
    page.screenshot(path=SHOTS / "review-text.png")
    panel.locator("text=Back to the card").click()
    page.screenshot(path=SHOTS / "review-card.png")

    # Partly supported sentences carry a dagger.
    expect(page.locator(".prose .dagger").first).to_be_visible()

    page.locator(".tabs a", has_text="Timeline").click()
    expect(page.locator(".timeline .paper").first).to_be_visible()
    assert page.locator(".timeline .paper").count() > 3
    page.screenshot(path=SHOTS / "timeline.png")
    page.locator(".tabs a", has_text="Papers").click()
    expect(page.locator("table.grid tbody tr").first).to_be_visible()
    page.locator(".tabs a", has_text="Report").click()
    expect(page.locator(".md h1")).to_contain_text("Run report")

    # The paused run explains itself and offers no review.
    page.goto(url)
    page.locator(".run", has_text="Ohmic dissipation").click()
    expect(page.locator(".controls")).to_contain_text("Paused")
    expect(page.locator(".controls")).to_contain_text("litreview resume")
    expect(page.locator(".controls").get_by_role("button", name="Continue")).to_be_visible()

    # Library: filters, title search and search inside papers.
    page.goto(url + "#/library")
    expect(page.locator("table.grid tbody tr").first).to_be_visible()
    read_rows = page.locator("table.grid tbody tr").count()
    page.locator(".seg button", has_text="All").last.click()
    expect(page.locator("table.grid tbody tr")).not_to_have_count(read_rows)
    page.screenshot(path=SHOTS / "library.png")
    page.fill("input[type=search]", "ohmic")
    expect(page.locator("table.grid tbody tr")).to_have_count(1)
    page.locator(".seg button", has_text="Inside papers").click()
    expect(page).to_have_url(re.compile(r"inside=1"))
    page.fill("input[type=search]", "radial velocities")
    expect(page).to_have_url(re.compile(r"inside=1.*q=radial|q=radial.*inside=1"))
    expect(page.locator(".hits li").first).to_be_visible()
    expect(page.locator(".hits mark").first).to_be_visible()
    page.screenshot(path=SHOTS / "inside-papers.png")
    page.locator(".hits li a").first.click()
    expect(page.locator(".reader .para.focus")).to_have_count(1)
    expect(page.locator(".reader .para.focus")).to_be_in_viewport()
    page.screenshot(path=SHOTS / "paper.png")

    # Phone width: the evidence panel becomes a sheet, and nothing scrolls sideways.
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator(".run", has_text="What drives radius inflation").click()
    page.locator(".prose .cite-btn").first.click()
    expect(page.locator(".panel")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.screenshot(path=SHOTS / "phone-review.png")
    print("demo: ok")


def runs(page, url):
    """Start a review from the form, watch it, pause it, continue it; continue one paused at its cap.
    Needs serve_demo.py, whose worker runs reviews against the test fakes."""
    page.goto(url + "#/new")
    page.fill("textarea", "Does Ohmic heating explain the inflated radii of hot Jupiters?")
    page.fill("input[type=number] >> nth=2", "5")  # papers to read
    page.get_by_label("Follow the citation graph").uncheck()
    page.get_by_label("Use the Batches API").uncheck()
    page.screenshot(path=SHOTS / "new-review.png", full_page=True)
    page.get_by_role("button", name="Start review").click()

    expect(page).to_have_url(re.compile(r"#/review/"))
    controls = page.locator(".controls")
    expect(controls).to_contain_text("Running", timeout=20_000)
    expect(controls.locator(".log")).to_contain_text("[plan]", timeout=20_000)
    page.screenshot(path=SHOTS / "running.png")
    controls.get_by_role("button", name="Pause").click()
    expect(controls).to_contain_text("Paused", timeout=20_000)
    expect(controls).to_contain_text("interrupted")
    page.screenshot(path=SHOTS / "paused.png")
    controls.get_by_role("button", name="Continue").click()
    expect(page.locator(".controls")).to_have_count(0, timeout=90_000)  # done: the controls go away
    expect(page.locator(".prose h2").first).to_be_visible()
    assert "[@" not in page.locator(".prose").inner_text()

    # The demo's second review paused at its cost cap; continue it with the suggested cap.
    page.goto(url)
    page.locator(".run", has_text="Ohmic dissipation").click()
    controls = page.locator(".controls")
    expect(controls).to_contain_text("could cost up to")
    cap = float(controls.locator("input[type=number]").input_value())
    assert cap > 0.0001
    page.screenshot(path=SHOTS / "cap-paused.png")
    controls.get_by_role("button", name="Continue").click()
    expect(page.locator(".controls")).to_have_count(0, timeout=90_000)
    expect(page.locator(".prose h2").first).to_be_visible()
    print("runs: ok")


def tasks(page, url):
    """Get text and Summarize from the library table (needs serve_demo.py)."""
    page.goto(url + "#/library?status=all")
    row = page.locator("table.grid tbody tr", has_text="Irrelevant galaxy paper")
    expect(row.get_by_role("button", name="Get text")).to_be_enabled()
    expect(row.get_by_role("button", name="Summarize")).to_be_enabled()
    no_arxiv = page.locator("table.grid tbody tr", has_text="Inflation and flux")
    expect(no_arxiv.get_by_role("button", name="Get text")).to_be_disabled()

    row.get_by_role("button", name="Get text").click()
    expect(row).to_contain_text("1801.00007v2", timeout=30_000)  # the stored text's version
    expect(row.get_by_role("button", name="Get text")).to_have_count(0)
    page.screenshot(path=SHOTS / "library-tasks.png")
    row.get_by_role("button", name="Summarize").click()
    expect(row.get_by_text("✓ summary")).to_be_visible(timeout=30_000)
    row.get_by_text("✓ summary").click()
    expect(page.locator(".block", has_text="Summary")).to_contain_text("A standalone result")
    page.screenshot(path=SHOTS / "paper-summary.png")
    print("tasks: ok")


def passages(page, url, run_id):
    page.goto(f"{url}#/review/{run_id}?tab=papers")
    expect(page.locator("table.grid tbody tr").first).to_be_visible()
    cards = page.locator("button.linkish-pill")
    n_cards, n_chips = cards.count(), 0
    for c in range(n_cards):
        cards.nth(c).click()
        panel = page.locator(".panel")
        expect(panel.locator(".panel-title")).not_to_have_text("Evidence")
        chips = panel.locator(".passage")
        for k in range(chips.count()):
            chip = chips.nth(k)
            i = chip.get_attribute("data-i")
            label = chip.inner_text().replace("↗", "").strip()
            chip.click()
            focused = panel.locator(".para.focus")
            expect(focused).to_have_count(1)
            assert focused.get_attribute("data-i") == i, (c, k, i)
            assert focused.locator(".para-label").inner_text().strip() == f"[{i}] {label}", (c, k, label)
            expect(focused).to_be_in_viewport()
            panel.locator("text=Back to the card").click()
            n_chips += 1
        if c == 0:
            page.screenshot(path=SHOTS / "real-card.png")
    print(f"passages: {n_chips} passages across {n_cards} cards each opened at the paragraph they cite")


def share(page, url, run_id):
    page.goto(f"{url}#/review/{run_id}")
    page.locator(".head").screenshot(path=SHOTS / "share-button.png")
    with page.expect_download() as info:
        page.get_by_role("link", name="Download shareable page").click()
    download = info.value
    assert download.suggested_filename == f"{run_id}.html", download.suggested_filename
    path = SHOTS / download.suggested_filename
    download.save_as(path)

    # The file works with nothing but itself: no request leaves the page.
    requests = []
    page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:")) else None)
    page.route(re.compile(r"^https?://"), lambda route: route.abort())
    page.goto(path.resolve().as_uri())
    expect(page.locator(".prose h2").first).to_be_visible()
    assert "[@" not in page.locator(".prose").inner_text()
    assert page.locator(".prose .cite-btn").count() > 3
    assert page.locator("a[download='refs.bib']").get_attribute("href").startswith("data:application/x-bibtex")
    page.screenshot(path=SHOTS / "share-review.png")

    # Every cited paper's card opens, and every passage opens the paragraph it cites.
    refs = page.locator(".refs li")
    n_cards, n_chips = refs.count(), 0
    for c in range(n_cards):
        title = refs.nth(c).locator("em").inner_text()
        refs.nth(c).locator(".cite-btn").click()
        panel = page.locator(".panel")
        expect(panel.locator(".paper-title")).to_have_text(title)
        chips = panel.locator(".passage")
        for k in range(chips.count()):
            chip = chips.nth(k)
            i = chip.get_attribute("data-i")
            label = chip.inner_text().replace("↗", "").strip()
            chip.click()
            focused = panel.locator(".para.focus")
            expect(focused).to_have_count(1)
            assert focused.get_attribute("data-i") == i, (c, k, i)
            assert focused.locator(".para-label").inner_text().strip() == f"[{i}] {label}", (c, k, label)
            assert focused.locator("p").inner_text().strip()
            if c == 0 and k == 0:
                page.screenshot(path=SHOTS / "share-passage.png")
            panel.locator("text=Back to the card").click()
            n_chips += 1
        if c == 0:
            page.screenshot(path=SHOTS / "share-card.png")

    page.locator(".tabs a", has_text="Timeline").click()
    expect(page.locator(".timeline .paper").first).to_be_visible()
    page.locator(".timeline .paper").first.click()
    expect(page.locator(".panel .items").first).to_be_visible()
    page.screenshot(path=SHOTS / "share-timeline.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=SHOTS / "share-phone.png")
    assert not requests, requests
    print(f"share: {path.name} offline, {n_chips} passages across {n_cards} cards each opened at its paragraph")


def graph(page, url, run_id):
    page.goto(f"{url}#/review/{run_id}?tab=graph")
    expect(page.locator(".graph .node").first).to_be_visible()
    edges = page.locator(".graph .edges path")
    n_edges = edges.count()
    missing = page.locator(".missing")
    if missing.count():
        page.screenshot(path=SHOTS / "graph-missing.png")
        page.get_by_role("button", name="Get citation links from ADS").click()
        expect(missing).to_have_count(0, timeout=30_000)
        assert edges.count() > n_edges, (edges.count(), n_edges)
        n_edges = edges.count()
    assert n_edges > 0

    # Hovering a paper brings its links forward and fades the rest; clicking opens its card.
    nodes = page.locator(".graph .node")
    for i in range(nodes.count()):
        nodes.nth(i).hover()
        if page.locator(".graph .edges path.on").count():
            break
    assert page.locator(".graph .edges path.on").count() > 0 and page.locator(".graph .node.dim").count() > 0
    page.screenshot(path=SHOTS / "graph-hover.png")
    nodes.nth(i).click()
    expect(page.locator(".panel .items").first).to_be_visible()
    page.mouse.move(0, 0)
    page.screenshot(path=SHOTS / "graph.png")

    gaps = page.locator(".gaps tbody tr")
    if gaps.count():
        gaps.first.hover()
        lit = page.locator(".graph .node:not(.dim)").count()
        assert 0 < lit < nodes.count(), lit
        page.screenshot(path=SHOTS / "graph-gaps.png", full_page=True)
    print(f"graph: {nodes.count()} papers, {n_edges} links, {gaps.count()} gaps")


def add(page, url):
    page.goto(url)
    page.locator(".run", has_text="What drives radius inflation").click()
    expect(page.locator(".prose h2").first).to_be_visible()
    run_url = page.url.split("?")[0]
    page.goto(run_url + "?tab=papers")
    # The Papers tab opens on the papers read, none of which can be ticked; the add box is there anyway.
    adder = page.locator(".adder")
    expect(adder).to_contain_text("Add papers to this review")
    unread = adder.get_by_role("button", name=re.compile("that screening included"))
    if unread.count():
        unread.click()
        expect(page.locator(".seg button.on")).to_contain_text("Included, not read")
    # A paper no search found, looked up in ADS by its bibcode.
    adder.get_by_role("searchbox").fill("https://ui.adsabs.harvard.edu/abs/2001ApJ...550...10Z/abstract")
    adder.get_by_role("button", name="Find in ADS").click()
    found = adder.locator(".results li", has_text="An early radius measurement")
    expect(found).to_contain_text("not in the library yet")
    expect(found).to_contain_text("cited by")
    found.get_by_role("checkbox").check()
    # And one screening left out, ticked in the table.
    page.get_by_role("button", name=re.compile(r"^All screened")).click()
    row = page.locator("tbody tr", has_text="Irrelevant galaxy paper")
    row.get_by_role("checkbox").check()
    bar = page.locator("form.add")
    expect(bar).to_contain_text("Read 2 more papers and write the review again")
    expect(bar).to_contain_text("Zane 2001")
    page.screenshot(path=SHOTS / "add-papers.png", full_page=True)
    bar.get_by_role("button", name="Add and rewrite").click()
    expect(page.locator(".controls")).to_be_visible()
    expect(page.locator(".controls .log")).to_contain_text("reading 2 more", timeout=30_000)
    expect(page.locator(".controls")).to_have_count(0, timeout=90_000)  # done again
    expect(page.locator(".rewritten").last).to_contain_text("after reading 2 more papers")
    page.get_by_role("button", name=re.compile(r"^Read")).click()
    for title in ("Irrelevant galaxy paper", "An early radius measurement"):
        expect(page.locator("tbody tr", has_text=title)).to_contain_text("added later")
    page.screenshot(path=SHOTS / "added.png")

    # The graph's gaps can be ticked too.
    page.goto(run_url + "?tab=graph")
    expect(page.locator(".graph .node").first).to_be_visible()
    if page.locator(".gaps tbody tr").count():
        page.locator(".gaps tbody tr").first.get_by_role("checkbox").check()
        expect(page.locator("form.add")).to_be_visible()
        page.get_by_role("button", name="Clear").click()
        expect(page.locator("form.add")).to_have_count(0)
    print("add: a paper found in ADS and a screened-out one were read and the review written again")


def upload(page, url):
    page.goto(url)
    page.locator(".run", has_text="What drives radius inflation").click()
    expect(page.locator(".prose h2").first).to_be_visible()
    run_url = page.url.split("?")[0]
    page.goto(run_url + "?tab=papers")
    row = page.locator("tbody tr", has_text="Inflation and flux")
    expect(row).to_contain_text("abstract")
    expect(row.get_by_role("checkbox")).to_have_count(0)  # read already, and nothing newer to read
    row.get_by_role("link").click()

    text = page.locator("section", has=page.get_by_role("heading", name="Text"))
    expect(text).to_contain_text("Only the abstract is stored.")
    form = text.locator("form.upload")
    form.locator("input[type=file]").set_input_files(SHOTS / "book.pdf")
    form.get_by_label("Pages").fill("4-9")
    form.get_by_role("button", name="Upload").click()
    expect(form.locator(".err")).to_contain_text("aren't in the PDF, which has 5 pages")
    form.get_by_label("Pages").fill("2-4")
    form.get_by_role("button", name="Upload").click()
    expect(form).to_contain_text("Stored 9 paragraphs from pages 2–4.", timeout=30_000)
    item = text.locator("li", has_text="uploaded PDF")
    expect(item).to_contain_text("uploaded PDF, pages 2–4 · 9 paragraphs")
    item.get_by_role("button", name="read the text").click()
    expect(page.locator(".reader")).to_contain_text("uploaded PDF, pages 2–4 · 9 paragraphs")
    expect(page.locator(".reader")).to_contain_text("3 Orbits, para 1, p. 2")
    page.screenshot(path=SHOTS / "upload.png", full_page=True)

    page.goto(run_url + "?tab=papers")
    expect(page.locator(".adder")).to_contain_text("One paper it read has a newer text")
    row = page.locator("tbody tr", has_text="Inflation and flux")
    expect(row).to_contain_text("newer text to read")
    row.get_by_role("checkbox").check()
    bar = page.locator("form.add")
    expect(bar).to_contain_text("Read a paper again, from its newer text, and write the review again")
    bar.get_by_role("button", name="Add and rewrite").click()
    expect(page.locator(".controls")).to_be_visible()
    expect(page.locator(".controls .log")).to_contain_text("1 of them again, from a newer text", timeout=30_000)
    expect(page.locator(".controls")).to_have_count(0, timeout=90_000)  # done again
    page.get_by_role("button", name=re.compile(r"^Read")).click()
    row = page.locator("tbody tr", has_text="Inflation and flux")
    expect(row).to_contain_text("read again from a newer text")
    expect(row).to_contain_text("uploaded PDF")
    expect(row).not_to_contain_text("newer text to read")
    expect(page.locator(".rewritten").last).to_contain_text("after reading 1 paper again, from its newer text")
    page.screenshot(path=SHOTS / "reread.png")
    print("upload: a PDF uploaded for a paper read from its abstract was read again and the review written again")


if __name__ == "__main__":
    mode, url = sys.argv[1], sys.argv[2].rstrip("/") + "/"
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            if mode == "demo":
                demo(page, url)
            elif mode == "runs":
                runs(page, url)
            elif mode == "tasks":
                tasks(page, url)
            elif mode == "share":
                share(page, url, sys.argv[3])
            elif mode == "graph":
                graph(page, url, sys.argv[3])
            elif mode == "add":
                add(page, url)
            elif mode == "upload":
                upload(page, url)
            else:
                passages(page, url, sys.argv[3])
        finally:
            if errors:
                print("page errors:", *errors, sep="\n  ")
            browser.close()
    assert not errors, errors
