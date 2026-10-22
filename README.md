# litreview

Turns a research question into a chronological literature review built only from papers and books it has fetched from NASA ADS and read. Claude chooses the searches, reads the papers and writes the prose. Code holds the set of citable papers, checks every citation and sentence against it, and builds the bibliography from ADS records.

## How it works

A run is nine steps in a fixed order. Claude chooses searches, judges relevance, reads and writes. Code does the storing, checking and rendering.

![The litreview pipeline: nine steps, five of them Claude's, around one SQLite library; only papers stored there can be cited](docs/pipeline.svg)

The draft is built only from the library's citable cards, and step 8 sends a failing draft back to step 7 once. The revision is checked again, and any sentence that still fails is cut. A sentence that came right after a cut one, and may now point back at it ("It was ..."), is reworded by Claude to stand alone and checked again; if the rewording fails a check, the sentence stays as written. report.md lists every cut and rewording.

## Setup

You need Python 3.11 or newer. The web UI also needs Node.js 22 or newer to build it, or Docker instead (see [Web UI](#web-ui)). litreview is developed on Linux and should run on macOS; on Windows, use WSL or Docker.

```bash
git clone https://github.com/jbirky/litreview.git
cd litreview
python3 -m venv .venv
source .venv/bin/activate     # in each new shell; the commands below assume it
pip install -e '.[test,web]'
cp .env.example .env          # then fill in ADS_API_TOKEN and ANTHROPIC_API_KEY
```

Install it editable (`-e`) from the clone, as above. litreview keeps its library (`data/`), its reviews (`runs/`) and `.env` in the clone's folder. To keep them in another folder, set `LITREVIEW_HOME` to that folder in your shell (not in `.env`, which is read from that folder).

- ADS token: https://ui.adsabs.harvard.edu/user/settings/token
- Claude: `ANTHROPIC_API_KEY`, or an `ant auth login` profile. On a Max or Team plan, the monthly API credits included with the plan pay for this. To claim them, link a Console organization under claude.ai **Settings → Billing → API credits**, then create the key at https://platform.claude.com. The credits expire at the end of each billing cycle. Keep the key in this `.env` rather than exporting it from your shell profile. If `ANTHROPIC_API_KEY` is set when Claude Code starts, Claude Code may use the key instead of your plan, and the credits don't cover Claude Code.
- `LITREVIEW_CONTACT` (optional) is appended to the User-Agent sent to ADS and arXiv
- `UNPAYWALL_EMAIL` (optional) lets litreview ask [Unpaywall](https://unpaywall.org/products/api) for open-access copies of papers arXiv lacks; Unpaywall asks for an email address with every request. Without it, an email address in `LITREVIEW_CONTACT` is used
- OCR, for scans with no text layer, needs Tesseract's English data, once (the Docker container reads the same folder):
  ```bash
  mkdir -p data/tessdata && curl -L -o data/tessdata/eng.traineddata https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
  ```

## Use

```bash
litreview search 'abs:"hot Jupiter" AND abs:"radius inflation"'   # one ADS query, no Claude
litreview run "What drives radius inflation in hot Jupiters?" --since 2000
litreview resume <run>         # after Ctrl-C, a low ADS quota, the cost cap, or a pending batch
litreview add <run> <bibcode>... --cost-cap 10   # read more papers for a finished review and write it again
litreview attach <bibcode> book.pdf --pages 120-180  # a PDF you have of a paper or book becomes its full text
litreview card <run> <bibcode> # one evidence card beside the passages it cites
litreview summary <bibcode>   # the paper's question-independent summary, beside its passages
litreview tasks                # run paper tasks queued from the web UI (the UI's worker does this itself)
litreview recheck <run>        # re-run the checks after hand-editing runs/<run>/draft.md
litreview report <run>
litreview share <run>          # the review as one HTML file to send people: runs/<run>/share.html, or -o FILE
litreview refs <run>           # fetch the citation links among a run's papers from ADS, where missing
litreview runs
```

Of the papers screening includes, `--max-papers` are read, chosen in turn from two rankings: the search ranking, and how many of the other included papers cite each one (from the reference lists ADS searches return, at no extra request). Citations bring in the papers a field builds on that the searches ranked low; the search ranking keeps recent papers, which have had little time to be cited. report.md says which ranking chose each paper.

`add` reads the papers it is given (any ADS bibcode, such as a gap in the citation graph or a paper screening left out) alongside the others, and writes the review again from every card. Nothing is searched or screened again, and only the new papers are read, so it costs their extraction plus drafting and checks: about $2–4 for a 20-paper review. A copy of the previous outputs is kept in `runs/<run>/v1/` (then `v2/`, ...). Extraction runs interactively unless you pass `--batch`.

Searches find refereed papers, arXiv preprints, and books and book chapters (ADS has about 75,000 books and 400,000 chapters, few of them marked refereed). Screening sees which records are books; many have only a publisher's description, or no abstract at all.

`run` options: `--until`, `--max-papers 40`, `--refereed` (refereed records only: no preprints, and only the few books ADS marks refereed), `--abstracts-only` (the baseline mode), `--no-expand` (skip citation-graph expansion), `--no-batch` (extract interactively instead of through Batches, faster for small runs), `--cost-cap 10`, `--poll 30` (seconds between batch checks; `-1` pauses instead of waiting).

Each run writes `runs/<date>-<slug>/`:

| File | Contents |
|---|---|
| review.md | The review, with author–year links to each paper's ADS page |
| refs.bib | BibTeX for every cited paper, from the ADS export API |
| cards.jsonl | Every evidence card, citable or not |
| report.md | Queries, papers found/screened/read/cited, exclusions with reasons, fetch failures, every check failure, cost |
| run.json | Settings, model IDs, token usage and cost |
| draft.md | The final draft with `[@bibcode]` keys, for `recheck` |
| draft_v1.md, draft_v2.md | The first draft and the revision, before cuts |
| v1/, v2/, ... | Earlier versions of all of the above, kept when papers are added |

The library (`data/library.sqlite`) keeps papers and their text across runs, so a paper read once is not downloaded again.

## Uploading PDFs

A paper's full text comes from the first of these that has it (see [Full text beyond arXiv](#full-text-beyond-arxiv)): arXiv, a scan ADS hosts, or an open-access copy Unpaywall finds. Books are almost never in any of them. A paper or book none of them has is read from its ADS abstract, unless you give litreview a PDF of it:

```bash
litreview attach 2008gady.book.....B binney-tremaine.pdf --pages 120-180
litreview attach 10.1086/123456 paper.pdf      # a bibcode, arXiv id, DOI or a link to one
```

or **Upload a PDF** on the paper's page in the web UI. The paper must have an ADS record, which is where its bibliography entry comes from. The PDF is read with no Claude cost, and the PDF itself isn't kept. The newest full text stored for a paper is the one reviews read, so a review that runs afterwards reads the upload. A finished review that read the paper already reads it again with `litreview add <run> <bibcode>`, or by ticking it in the review's paper table, which marks papers that have a newer text.

- `--pages` counts the file's own pages, from 1. Each paragraph's label gives its page (`3.2 Orbits, para 4, p. 133`).
- About 240,000 characters are read, roughly 80–110 pages of a textbook. Past that the text is cut, and `attach` says after which paragraph, so a book is best read a chapter or two at a time. Reading a long upload in a review costs up to about four times as much as a typical paper.
- Scanned pages are read by OCR (see [below](#full-text-beyond-arxiv) for setup and speed).
- A chapter's reference list and the index are skipped, as are running heads.
- Uploading the same file and pages again makes that text the one reviews read, without reading the file again: a stored text is never rewritten, so the cards built from it stay valid.

## Full text beyond arXiv

When arXiv has no version of a paper, or none litreview can read, it tries, in order:

1. **A scan ADS hosts.** ADS serves scans of most astronomy journals' older papers, with the publishers' permission, when the paper's ADS record lists `ADS_PDF`. They come through ADS's link gateway, which needs no API token and doesn't count against the API quota.
2. **An open-access copy that Unpaywall finds by DOI**, such as the publisher's open-access version or an author's manuscript in a repository. Up to three copies are tried, best first. Copies on arXiv and ADS are skipped, since those were tried already. This step needs an email address in `UNPAYWALL_EMAIL` or `LITREVIEW_CONTACT`. Many publishers' sites refuse programs, so some copies fail; report.md lists why. A site that refuses a download or shows a robot check isn't asked again in that run, or until the web UI's queued tasks are done. IOP, which publishes the AAS journals, shows one after a download or two, so upload AAS papers that aren't on arXiv yourself.
3. **The ADS abstract**, as before.

If arXiv is rate-limiting this machine, the run pauses instead of reading a poorer copy, as it always has.

Each host gets at most one request every 2 s. Only legal copies are fetched. The paper's page in the web UI links to where each downloaded copy came from.

Pages that are only a scanned image are read by OCR (Tesseract, built into PyMuPDF), once `data/tessdata/eng.traineddata` is in place (see [Setup](#setup)). `TESSDATA_PREFIX` points elsewhere if you prefer. OCR takes about 7 s a page, with up to 8 pages read at once, each in its own process. An 11-page two-column 1994 PASP scan took 18 s. On a two-column page the columns are found from the gutter between them and read left, then right. OCR misreads some characters, and equations come out garbled. Claude is told when a text was read by OCR, and the paper page marks it "OCR". Uploaded scans are read by OCR the same way. Without the OCR data, a scan with no text layer can't be read.

## Web UI

A browser over the same library that can also run reviews:
- **Reviews**, each with:
  - the review, where every citation opens that paper's card beside it, and every card item links to the stored paragraph it came from
  - a timeline of the cited papers
  - a citation graph of the papers read: one row per paper by date, with a bracket from each paper to every other one it cites. Hover a paper to bring its links forward; click it for its card. Below it, the papers that several of them cite but the review didn't read, each with what the review's searches made of it (not found, screened out and why, or past the number of papers to read). Tick any of them to add them to the review. The links come from ADS reference lists, which every search now stores at no extra request. Papers stored before that show **Get citation links from ADS**, about one ADS request per 40 papers and no Claude cost.
  - every screened paper, with its score and reason, and why each paper read was chosen. On a finished review, an **Add papers to this review** box at the top takes any paper: tick ones in the table that weren't read (**Included, not read** lists those screening kept) or that it read and now have a newer text, such as an uploaded PDF, or find others in ADS by bibcode, arXiv ID, DOI, a link to any of them, "Kelly et al. 2009", or an ADS query. Each result says whether the review read it, screened it (with the score and reason) or never saw it, and how many of the papers read cite it. A lookup is one ADS request and no Claude cost.
  - **Add papers** (on a finished review): reads the ticked papers and writes the review again, as `litreview add` does, with a cost estimate and a new cap. The current version stays on the page until the new one is written.
  - the run report
  - **Download shareable page**: the review as one HTML file to send people (the same file as `litreview share`). It opens in any browser with no server, and has the review, the timeline, each cited paper's card with the paragraphs the card cites, and refs.bib. It leaves out costs, the run report, the screened papers and the rest of the stored texts. **Preview** opens it in a new tab.
- **New review**: a form for the question, years, number of papers, citation-graph expansion, Batches and the cost cap, with a rough cost estimate.
- **Run controls** on any review that isn't finished:
  - live progress, with the stage and the same log lines the CLI prints
  - **Pause** (sends the worker Ctrl-C, so the review pauses cleanly)
  - **Continue**, with a new cost cap suggested when the old one stopped it
- **Paper pages**, with the paper's summary, its cards from every review, and the exact text the model read. **Upload a PDF** there stores a PDF of the paper or book as its full text, all of it or the pages you give (see [Uploading PDFs](#uploading-pdfs)).
- **The library**: every paper found, with filters, a year histogram, and search inside the papers that were read. Two buttons appear on any paper that needs them:
  - **Get text** fetches the full text from arXiv (LaTeX, then HTML, then PDF), or else from a scan ADS hosts or an open-access copy (see [Full text beyond arXiv](#full-text-beyond-arxiv)). It has no Claude cost.
  - **Summarize** writes the question-independent summary with Sonnet, about $0.07, fetching the text first if needed. Every line of the summary cites its passages and is checked like a card.

  The paper page has the same buttons. These tasks run in their own lane (`litreview tasks`), so they don't wait behind a review.

LaTeX math in stored texts, card quotes, abstracts and titles is rendered with KaTeX, in the UI and on shareable pages. A formula KaTeX can't parse, such as one using a macro a paper defines itself, is shown as its LaTeX source.

Reviews started or continued in the browser are queued. A background worker runs them one at a time, each as its own `litreview resume` process. It also waits for any review the CLI is running. Progress is kept in the library, so the page shows the same thing whether a review was started from the browser or from the CLI. Closing the page doesn't stop a review. Restarting the server pauses one, and you can continue it.

Anyone who can open the page can start reviews on your credits, so never put it on the open internet. It listens only on 127.0.0.1 unless you change that. To reach it from your other devices, use a private network, such as a Tailscale tailnet (`tailscale serve`, never `funnel`). Changes, and ADS lookups, are accepted only as JSON from the page's own origin, so another website can't start a review, or spend your ADS requests, through your browser. The one exception is an uploaded PDF, sent as `application/pdf`, which browsers likewise won't send from another site without the server's consent. Uploads are capped at 300 MB and read by a separate `litreview attach` process. `litreview web --read-only` serves browsing alone.

```bash
cd litreview/web/ui && npm install && npm run build && cd -   # once, and after UI changes (also builds the shareable page)
litreview web                                                 # http://127.0.0.1:5070, keys from .env
```

Or run it in Docker, which builds the UI itself, so it needs no Node.js. From the clone:

```bash
mkdir -p data runs            # before the first run, so the folders are yours and not root's
docker build -t litreview .
docker run -d --name litreview-web --restart unless-stopped \
  --user "$(id -u):$(id -g)" --env-file .env --stop-timeout 30 \
  -p 127.0.0.1:5070:5070 -v "$PWD/data:/app/data" -v "$PWD/runs:/app/runs" \
  litreview
```

- `--user` runs it as you, so SQLite's side files stay writable for the CLI on the host
- `--env-file .env` gives it the same keys as the CLI
- `--stop-timeout 30` gives a running review 30 seconds to pause when the container stops
- `-p 127.0.0.1:5070:5070` serves it to this machine only. To reach it from other devices on your tailnet: `sudo tailscale serve --bg --https=5070 http://127.0.0.1:5070`

After updating the code, `docker stop litreview-web && docker rm litreview-web`, then build and run again.

Port 5060 would clash with SIP, and browsers refuse it, so the UI uses 5070. Set `LITREVIEW_MONTHLY_CREDITS=100` in `.env`, or whatever your plan includes, to show this month's spend against it. Next to it, the top bar shows how many of today's ADS search requests are used, as of the last ADS request any litreview process made (saved in `data/ads-quota.json`). `npm run dev` in `litreview/web/ui` serves a live-reloading UI on :5173 against `litreview web` on :5070.

Browser tests run in Microsoft's Playwright Docker image (`tests/e2e/run` starts it), against a demo library whose reviews run on the test fakes:

```bash
ADS_API_TOKEN=demo ANTHROPIC_API_KEY=demo FAKE_WORKER_DELAY=0.25 python tests/e2e/serve_demo.py /tmp/litreview-demo 5071 &
tests/e2e/run demo http://127.0.0.1:5071/      # browsing
tests/e2e/run runs http://127.0.0.1:5071/      # start, watch, pause and continue reviews
tests/e2e/run tasks http://127.0.0.1:5071/     # Get text and Summarize in the library
tests/e2e/run passages http://127.0.0.1:5070/ <run>   # every card passage in a real run opens at the paragraph it cites
tests/e2e/run share http://127.0.0.1:5070/ <run>      # the shareable page, downloaded and opened offline: every passage opens
tests/e2e/run graph http://127.0.0.1:5071/ <run>      # the citation graph; fetches any missing links first (needs serve_demo.py)
tests/e2e/run add http://127.0.0.1:5071/              # add a paper found in ADS and a screened-out one to the demo review (needs a fresh serve_demo.py)
tests/e2e/run upload http://127.0.0.1:5071/           # upload a PDF for a paper read from its abstract, then read it again in the demo review (needs a fresh serve_demo.py)
```

## How a run spends time and money

About 25 ADS requests per review (limit 5,000/day). arXiv: one request every 3 s on export.arxiv.org (API and LaTeX source; arxiv.org's robots.txt disallows `/src`), one every 15 s on arxiv.org (HTML and PDF fallbacks). Papers arXiv can't supply: one request every 2 s to each of ADS's link gateway, Unpaywall and the hosts of the copies it finds. Claude: Opus 5.5 plans, drafts and checks. Haiku 5.5 screens abstracts for about $0.03 per 300 papers; Opus cost $0.94 for the same 300. If Haiku refuses a batch of abstracts, the same request is retried once on Sonnet 5.5, because Haiku has no server-side fallback. Sonnet 5.5 extracts cards through the Batches API (half price, usually done within an hour). The same request writes a summary of the paper that doesn't depend on the question (aim, methods, results, limitations, each citing its passages). It is written once per full text and stored apart from the run's cards, so a later review that reads the same text doesn't pay for it again. Estimated ~$3 for a 40-paper review. The run pauses before any call whose worst case (its whole `max_tokens` used) would pass `--cost-cap`, so the real spend usually stays well under the cap.

Batches can't carry refusal fallbacks, so a card request refused inside a batch is retried once interactively, where `fallbacks: "default"` applies. A paper refused outright stays uncitable and is listed in report.md.

## Tests

```bash
python -m pytest -q        # offline: fake ADS, arXiv and Claude, plus the real SDK against a mock transport
```

Evaluation against 5 published reviews (costs real money, run by hand):

```bash
python tests/eval/score.py gold            # gold reference lists from ADS
python tests/eval/score.py run-all --yes   # ~$15
python tests/eval/score.py score
python tests/eval/score.py sheets <run>    # CSVs for the human-graded metrics
python tests/eval/score.py human <run>
```
