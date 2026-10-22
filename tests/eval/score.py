"""Evaluation on 5 questions, each matched to a published review whose reference list is the gold set.

    python tests/eval/score.py gold              # fetch the gold sets from ADS (needs ADS_API_TOKEN)
    python tests/eval/score.py run-all --yes     # run all 5 questions; about $20 of API use
    python tests/eval/score.py score [RUN ...]   # automatic metrics (default: latest run per question)
    python tests/eval/score.py sheets RUN        # CSVs for the human-graded metrics, in the run folder
    python tests/eval/score.py human RUN         # score the filled-in CSVs

It costs real money, so it is run by hand, not in CI.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from litreview.ads import FIELDS, ADSClient, arxiv_id_from_record
from litreview.config import DB_PATH, Settings, load_dotenv
from litreview.library import Library
from litreview.pipeline import Run
from litreview.verify import check_chronology, parse_draft

HERE = Path(__file__).parent
QUESTIONS = json.loads((HERE / "questions.json").read_text())
GOLD_DIR = HERE / "gold"
GOLD_SIZE = 20


def identities(bibcodes: list[str], arxiv: str | None, doi: str | None) -> set[str]:
    keys = {f"bib:{b}" for b in bibcodes}
    if arxiv:
        keys.add(f"arxiv:{arxiv}")
    if doi:
        keys.add(f"doi:{doi.lower()}")
    return keys


def fetch_gold(ads: ADSClient, q: dict) -> dict:
    """The review's 20 most-cited references published by the question's cutoff year."""
    _, docs = ads.search(f'identifier:"arXiv:{q["gold_arxiv"]}"', rows=1, fl=["bibcode", "title"])
    if not docs:
        raise SystemExit(f"ADS has no record for arXiv:{q['gold_arxiv']}")
    review = docs[0]["bibcode"]
    _, refs = ads.search(f'references(bibcode:"{review}")', rows=2000, sort="citation_count desc",
                         fl=[*FIELDS, "year"])
    refs = [r for r in refs if int(r.get("year") or 0) <= q["until"]][:GOLD_SIZE]
    return {
        "question_id": q["id"], "review_bibcode": review, "until": q["until"],
        "references": [{
            "bibcode": r["bibcode"], "alternates": r.get("alternate_bibcode") or [],
            "arxiv": arxiv_id_from_record(r), "doi": (r.get("doi") or [None])[0],
            "title": (r.get("title") or [""])[0], "citation_count": r.get("citation_count", 0),
        } for r in refs],
    }


def paper_ids(lib: Library, bibcode: str) -> set[str]:
    p = lib.paper(bibcode)
    if p is None:
        return {f"bib:{bibcode}"}
    return identities(p.all_bibcodes, p.arxiv_id, p.doi)


def recall(gold: dict, lib: Library, bibcodes) -> float:
    found = set()
    for b in bibcodes:
        found |= paper_ids(lib, b)
    refs = gold["references"]
    hit = sum(1 for r in refs if identities([r["bibcode"], *r["alternates"]], r["arxiv"], r["doi"]) & found)
    return hit / len(refs) if refs else 0.0


def score_run(lib: Library, run_id: str, gold: dict | None) -> dict:
    run = Run(lib, run_id, log=lambda *a: None)
    citable = lib.citable(run_id)
    draft = parse_draft((run.folder / "draft.md").read_text())
    keys = draft.keys()
    review = (run.folder / "review.md").read_text()
    linked = {unquote(b) for b in re.findall(r"ui\.adsabs\.harvard\.edu/abs/([^/]+)/abstract", review)}
    papers = lib.papers(sorted(citable))
    placement = [i for i in check_chronology(draft, papers, citable) if "has no entry" not in i.detail]
    labels = run.get("claim_labels", {})
    rated = sum(n for k, n in labels.items() if k not in ("no_claim", "unrated"))
    record = json.loads((run.folder / "run.json").read_text())
    out = {
        "run": run_id,
        "citation_validity": (len(keys & citable) / len(keys)) if keys else 1.0,
        "rendered_links_valid": linked <= citable,
        "chronology_ok": not placement,
        "claim_support": (labels.get("supported", 0) / rated) if rated else None,
        "cost_usd": record["total_cost_usd"],
        "minutes": round(record["elapsed_seconds"] / 60, 1),
    }
    if gold:
        alias = run.get("alias", {})
        included = [b for b, sc in ((r["bibcode"], r["score"]) for r in lib.screening(run_id)) if sc >= 2]
        out["coverage"] = {
            "found": recall(gold, lib, alias.keys()),
            "included": recall(gold, lib, included),
            "read": recall(gold, lib, run.get("selected", [])),
            "cited": recall(gold, lib, keys & citable),
        }
    return out


def latest_run(lib: Library, question: str) -> str | None:
    for r in lib.runs():
        if r["question"] == question and r["status"] == "done":
            return r["run_id"]
    return None


def write_sheets(lib: Library, run_id: str, seed: int = 0) -> list[Path]:
    run = Run(lib, run_id, log=lambda *a: None)
    rng = random.Random(f"{run_id}:{seed}")
    cards = [r for r in lib.cards(run_id) if r["status"] == "verified"]

    items = []
    for r in cards:
        card = json.loads(r["card"])
        text = lib.text(r["bibcode"], r["text_source"], r["text_version"])
        for kind in ("methods", "findings", "limitations"):
            for it in card[kind]:
                passages = " || ".join(f"[{text.paragraphs[i]['label']}] {text.paragraphs[i]['text']}"
                                       for i in it["passages"] if text and i < len(text.paragraphs))
                items.append([r["bibcode"], kind, it.get("source", ""), it["text"], passages[:4000], ""])
    rng.shuffle(items)
    labels = run.get("sentence_labels", {})
    sentences = [[t, lab, ""] for t, lab in labels.items() if lab != "no_claim"]
    rng.shuffle(sentences)
    lims = []
    for r in rng.sample(cards, min(10, len(cards))):
        card = json.loads(r["card"])
        lims.append([r["bibcode"], r["read_depth"],
                     " | ".join(f"({l['source']}) {l['text']}" for l in card["limitations"]) or card.get("limitations_note", ""),
                     "", ""])

    paths = []
    for name, header, rows in (
        ("eval_card_grounding.csv", ["bibcode", "kind", "source", "item", "cited passages", "supported (1/0)"], items[:30]),
        ("eval_claims.csv", ["sentence", "checker label", "your label (supported/partial/unsupported)"], sentences[:20]),
        ("eval_limitations.csv", ["bibcode", "read depth", "limitations",
                                  "stated ones really stated (y/n)", "inferred ones fair (1-3)"], lims),
    ):
        path = run.folder / name
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        paths.append(path)
    return paths


def score_sheets(folder: Path) -> dict:
    def rows(name):
        with open(folder / name, newline="") as f:
            return list(csv.reader(f))[1:]

    grounding = [int(r[5]) for r in rows("eval_card_grounding.csv") if r[5].strip() in ("0", "1")]
    claims = [(r[1].strip().lower(), r[2].strip().lower()) for r in rows("eval_claims.csv") if r[2].strip()]
    lims = [int(r[4]) for r in rows("eval_limitations.csv") if r[4].strip() in ("1", "2", "3")]
    stated = [r[3].strip().lower() == "y" for r in rows("eval_limitations.csv") if r[3].strip()]
    return {
        "card_grounding": sum(grounding) / len(grounding) if grounding else None,
        "claim_agreement": sum(a == b for a, b in claims) / len(claims) if claims else None,
        "claim_support_human": sum(b == "supported" for _, b in claims) / len(claims) if claims else None,
        "limitations_inferred_mean": sum(lims) / len(lims) if lims else None,
        "limitations_stated_ok": sum(stated) / len(stated) if stated else None,
    }


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gold")
    p = sub.add_parser("run-all")
    p.add_argument("--yes", action="store_true", help="confirm spending about $20 of API use")
    p = sub.add_parser("score")
    p.add_argument("runs", nargs="*")
    p = sub.add_parser("sheets")
    p.add_argument("run")
    p = sub.add_parser("human")
    p.add_argument("run")
    args = ap.parse_args(argv)
    lib = Library(args.db)

    if args.cmd == "gold":
        ads = ADSClient()
        GOLD_DIR.mkdir(exist_ok=True)
        for q in QUESTIONS:
            gold = fetch_gold(ads, q)
            (GOLD_DIR / f"{q['id']}.json").write_text(json.dumps(gold, indent=2))
            print(f"{q['id']}: {gold['review_bibcode']}, {len(gold['references'])} gold references")
    elif args.cmd == "run-all":
        if not args.yes:
            print("This runs 5 full reviews (about $20 of API use). Re-run with --yes.")
            return 1
        for q in QUESTIONS:
            run = Run.create(lib, Settings(question=q["question"], until=q["until"]))
            print(f"{q['id']}: {run.run_id}")
            run.execute()
    elif args.cmd == "score":
        pairs = []
        if args.runs:
            for r in args.runs:
                row = lib.run(r)
                q = next((q for q in QUESTIONS if row and q["question"] == row["question"]), None)
                pairs.append((r, q))
        else:
            pairs = [(latest_run(lib, q["question"]), q) for q in QUESTIONS]
        for run_id, q in pairs:
            if run_id is None:
                print(f"{q['id']}: no finished run")
                continue
            gold_path = GOLD_DIR / f"{q['id']}.json" if q else None
            gold = json.loads(gold_path.read_text()) if gold_path and gold_path.exists() else None
            print(json.dumps({"question": q["id"] if q else None, **score_run(lib, run_id, gold)}, indent=2))
    elif args.cmd == "sheets":
        for path in write_sheets(lib, args.run):
            print(f"wrote {path}")
    elif args.cmd == "human":
        print(json.dumps(score_sheets(Path(lib.run(args.run)["folder"])), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
