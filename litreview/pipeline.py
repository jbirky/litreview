"""A review run: each stage saves its results to the library, so `litreview resume` continues
from the first unfinished stage after an interruption, a low ADS quota, the cost cap, or a batch
that is still processing.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import time
import traceback
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx

from . import plan as planning
from .ads import ADSClient, ADSQuotaLow, build_query, identifier_queries, paper_from_record
from .arxiv import ArxivClient, posting_month
from .config import RUNS_DIR, VERSION, Settings
from .extract import build_card, parse_tagged, request_params, summary_of, verify_card
from .library import Library, Paper, StoredText, checksum
from .llm import LLM, CostCapReached, Refused, Reply
from .openaccess import OpenAccessClient
from .papers import RateLimited, read_full_text
from .render import render_report, render_review, rerun_text, slug
from .synthesize import ask, cards_block, draft, mend, parse_mends, revise
from .verify import (Draft, Issue, check_chronology, check_keys, check_names_and_quotes, claim_check_prompts,
                     fix_chronology, keys_in, mend_targets, parse_draft, parse_labels, remove_sentences,
                     split_sentences, strip_heading_problems, text_problems)

STAGES = ["plan", "search", "screen", "expand", "select", "fetch", "extract", "synthesize", "check", "render", "done"]
OPERATORS = ["references", "citations", "useful", "reviews", "similar"]
MAX_HITS = 5000
EXTRACT_MAX_TOKENS = 16000  # thinking at medium effort plus ~20 tagged lines
CITED_MIN = 2  # how many included papers must cite a paper before it can be read for its citations
MAX_ADDED = 30  # papers added to a finished review at once
BIBCODE = re.compile(r"^\d{4}\S{15}$")
# The first stage whose results each setting a rerun can change. A rerun starts at the earliest stage
# among the settings it changes, or at fetch when it changes none of them, so it reads each paper's
# newest text; the cost cap and Batches change no results.
RERUN_FROM = {"since": "search", "until": "search", "refereed_only": "search", "expand": "expand",
              "max_papers": "select", "abstracts_only": "fetch"}
RERUN_SETTINGS = (*RERUN_FROM, "use_batch", "cost_cap")


try:
    import fcntl
except ImportError:  # not on Linux/macOS: no cross-process run locks
    fcntl = None


class Paused(Exception):
    pass


class RunBusy(Exception):
    """Another process (the CLI, or the web UI's worker) is executing this run."""


def _lock_path(lib: Library, run_id: str) -> Path | None:
    return lib.path.parent / "locks" / f"{run_id}.lock" if lib.path is not None and fcntl else None


@contextmanager
def run_lock(lib: Library, run_id: str):
    """Held while a run executes, so two processes never run the same review at once."""
    path = _lock_path(lib, run_id)
    if path is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunBusy(f"{run_id} is already running in another process") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_is_active(lib: Library, run_id: str) -> bool:
    """Whether some process holds the run's lock right now."""
    path = _lock_path(lib, run_id)
    if path is None or not path.exists():
        return False
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def take_turns(rankings: list[list[str]], n: int) -> list[tuple[str, int]]:
    """Up to n items, taking from each ranking in turn its best item not already taken:
    (item, the index of the ranking it was taken from)."""
    out, taken, pos = [], set(), [0] * len(rankings)
    while len(out) < n:
        moved = False
        for k, ranking in enumerate(rankings):
            while pos[k] < len(ranking) and ranking[pos[k]] in taken:
                pos[k] += 1
            if pos[k] < len(ranking) and len(out) < n:
                taken.add(ranking[pos[k]])
                out.append((ranking[pos[k]], k))
                moved = True
        if not moved:
            break
    return out


def has_newer_text(lib: Library, texts: dict, bibcode: str) -> bool:
    """Whether the library holds a full text of this paper other than the one a run read (texts is the
    run's "texts"), such as a PDF uploaded after the run read only the paper's abstract."""
    p = lib.paper(bibcode)
    best = lib.best_text(p.all_bibcodes, full_text=True) if p else None
    used = texts.get(bibcode)
    return best is not None and (used is None or (best.source, best.version) != (used["source"], used["version"]))


def request_additions(lib: Library, run_id: str, bibcodes: list[str]) -> tuple[list[str], list[dict]]:
    """Ask a finished review to read more papers. The next time the run executes, it reads them
    alongside the others and writes the review again. Papers not in the library are looked up in
    ADS then. A paper the review read already is read again if a newer full text of it is stored.
    Returns (the bibcodes asked for, [{bibcode, reason}] for those left out)."""
    row = lib.run(run_id)
    if row is None:
        raise KeyError(f"no run named {run_id}")
    if row["stage"] != "done":
        raise ValueError("only a finished review can take more papers; continue this one first")
    alias = lib.aliases()
    read = set(lib.get(run_id, "selected", []) or [])
    texts = lib.get(run_id, "texts", {}) or {}
    wanted, skipped = [], []
    for b in dict.fromkeys(x.strip() for x in bibcodes):
        if not BIBCODE.match(b):
            skipped.append({"bibcode": b, "reason": "not an ADS bibcode"})
        elif alias.get(b, b) in read and not has_newer_text(lib, texts, alias.get(b, b)):
            skipped.append({"bibcode": b, "reason": "already read for this review"})
        elif alias.get(b, b) not in wanted:
            wanted.append(alias.get(b, b))
    if len(wanted) > MAX_ADDED:
        raise ValueError(f"add at most {MAX_ADDED} papers at once")
    if wanted:
        additions = lib.get(run_id, "additions", []) or []
        additions.append({"requested": wanted, "requested_at": time.time(), "added": None, "not_found": None,
                          "previous": None, "done": False})
        lib.put(run_id, "additions", additions)
        lib.set_run(run_id, stage="fetch")
    return wanted, skipped


def rerun_start(old: Settings, new: Settings) -> str:
    """The stage a rerun with these settings starts at (see RERUN_FROM)."""
    changed = [STAGES.index(stage) for key, stage in RERUN_FROM.items() if getattr(old, key) != getattr(new, key)]
    return STAGES[min(changed, default=STAGES.index("fetch"))]


def rerun_plan(plan: dict, old: Settings, new: Settings) -> dict:
    """The original's plan with a rerun's years: those given, else the planner's own, as stage_plan
    chooses them. Its hit counts were for the old filters, so the searches count them again."""
    planned = plan.get("planned") or {}
    out = {k: v for k, v in plan.items() if k != "first_counts"} | {"counts": {}}
    for k in ("since", "until"):
        # Plans from before "planned" was kept hold the planner's year only where none was given.
        mine = planned[k] if k in planned else plan[k] if getattr(old, k) is None else None
        out[k] = getattr(new, k) or mine
    return out


def resolve_folder(stored: str, runs_dir: Path = RUNS_DIR) -> Path:
    """A run's folder. The stored path is absolute on the machine that created the run; the web
    container mounts runs/ elsewhere, so fall back to the same folder name under runs_dir."""
    path = Path(stored)
    return path if path.exists() else runs_dir / path.name


class Run:
    def __init__(self, lib: Library, run_id: str, *, ads: ADSClient | None = None,
                 arxiv: ArxivClient | None = None, oa: OpenAccessClient | None = None, llm_client=None,
                 poll_interval: float = 30.0, log=print):
        row = lib.run(run_id)
        if row is None:
            raise KeyError(f"no run named {run_id}")
        self.lib = lib
        self.run_id = run_id
        self.settings = Settings.from_dict(json.loads(row["settings"]))
        self.folder = resolve_folder(row["folder"])
        self.llm = LLM(lib, run_id, self.settings.cost_cap, client=llm_client)
        self._ads = ads
        self._arxiv = arxiv
        self._oa = oa
        self.poll_interval = poll_interval
        self._print = log

    def log(self, message: str) -> None:
        """Print, and keep each line in the library so the web UI can show progress."""
        for line in str(message).splitlines() or [""]:
            self.lib.add_event(self.run_id, line)
        self._print(message)

    @classmethod
    def create(cls, lib: Library, settings: Settings, runs_dir: Path = RUNS_DIR, status: str = "running",
               **kwargs) -> "Run":
        base = f"{date.today().isoformat()}-{slug(settings.question)}"
        run_id, n = base, 2
        while lib.run(run_id) or (runs_dir / run_id).exists():
            run_id, n = f"{base}-{n}", n + 1
        folder = runs_dir / run_id
        folder.mkdir(parents=True)
        lib.create_run(run_id, settings.question, settings.to_dict(), str(folder), status=status)
        return cls(lib, run_id, **kwargs)

    @classmethod
    def rerun(cls, lib: Library, source: str, changes: dict, runs_dir: Path = RUNS_DIR, status: str = "running",
              **kwargs) -> "Run":
        """A new review of a finished one's question, with `changes` to its settings (any of
        RERUN_SETTINGS). It copies what the original found in the stages before the first one the
        changes affect, and starts there. Later, a screening decision or a card is copied wherever
        the new run would ask Claude the same thing about the same text."""
        row = lib.run(source)
        if row is None:
            raise KeyError(f"no run named {source}")
        if row["stage"] != "done" or lib.get(source, "final_draft") is None:
            raise ValueError("only a finished review can be run again")
        if unknown := sorted(set(changes) - set(RERUN_SETTINGS)):
            raise ValueError(f"a rerun can't change {', '.join(unknown)}")
        old = Settings.from_dict(json.loads(row["settings"]))
        new = Settings.from_dict({**old.to_dict(), **changes})
        if new.since and new.until and new.since > new.until:
            raise ValueError("the first year is after the last")
        run = cls.create(lib, new, runs_dir=runs_dir, status=status, **kwargs)
        run._copy_from(source, old)
        return run

    def _copy_from(self, source: str, old: Settings) -> None:
        """Copy the original's results from the stages a rerun skips, and start it at the next one."""
        lib, new = self.lib, self.settings
        start = rerun_start(old, new)
        at = STAGES.index(start)
        plan = lib.get(source, "plan")
        self.put("plan", rerun_plan(plan, old, new) if start == "search" else plan)
        copied = ["plan"]
        if at > STAGES.index("search"):
            # Starting at expand, only what the planned queries found: expansion is done again.
            again = start == "expand"
            for (q, via), hits in itertools.groupby(lib.hits(source), key=lambda r: (r["query"], r["via"])):
                if not (again and via in OPERATORS):
                    lib.add_hits(self.run_id, q, via, [r["bibcode"] for r in hits])
            searched = lib.get(source, "searched", [])
            self.put("searched", [q for q in searched if q in plan["queries"]] if again else searched)
            if again:
                self._merge()
            else:
                self.put("merged", lib.get(source, "merged"))
                self.put("alias", lib.get(source, "alias"))
            rounds = ("search",) if again else ("search", "expand")
            for round_ in rounds:
                lib.add_screening(self.run_id, [(r["bibcode"], r["score"], r["reason"]) for r in lib.screening(source)
                                                if r["round"] == round_], round_)
            screened = {r["bibcode"] for r in lib.screening(self.run_id)}
            self.put("screened_on_ads_abstract",
                     [b for b in lib.get(source, "screened_on_ads_abstract", []) if b in screened])
            copied += ["searches", "screening"]
            if not again:
                self.put("expansion", lib.get(source, "expansion"))
                copied.append("expansion")
        added = []
        if at > STAGES.index("select"):
            for key in ("selected", "chosen_for_citations", "not_read"):
                self.put(key, lib.get(source, key))
            copied.append("selection")
            added = [b for a in lib.get(source, "additions", []) or [] for b in a.get("added") or []
                     if b not in (a.get("reread") or [])]
        changes = {k: [getattr(old, k), getattr(new, k)] for k in RERUN_FROM if getattr(old, k) != getattr(new, k)}
        rerun = {"run_id": source, "start": start, "changes": changes, "copied": copied, "added": added,
                 "screening": 0, "cards": []}
        self.put("rerun_of", rerun)
        lib.set_run(self.run_id, stage=start)
        self.log(f"run again from {source} {rerun_text(rerun)} Starting at {start}.")

    def _source(self) -> tuple[str, Settings] | None:
        """The review this one reruns, and its settings; None if it isn't a rerun."""
        rerun = self.get("rerun_of")
        row = self.lib.run(rerun["run_id"]) if rerun else None
        return (row["run_id"], Settings.from_dict(json.loads(row["settings"]))) if row else None

    def _note_copied(self, key: str, value) -> None:
        rerun = self.get("rerun_of")
        rerun[key] += value
        self.put("rerun_of", rerun)

    def _reused_screening(self, papers: list[Paper], round_: str) -> set[str]:
        """In a rerun, copy the original's decision on each of these papers it screened, when it used
        the same model, effort and interpretation of the question. Returns the bibcodes copied."""
        found = self._source()
        if found is None or not papers:
            return set()
        source, before = found
        s = self.settings
        same = ((before.models["screen"], before.efforts["screen"]) == (s.models["screen"], s.efforts["screen"])
                and self.lib.get(source, "plan")["interpretation"] == self.get("plan")["interpretation"])
        earlier = {r["bibcode"]: r for r in self.lib.screening(source)} if same else {}
        rows = [(p.bibcode, earlier[p.bibcode]["score"], earlier[p.bibcode]["reason"])
                for p in papers if p.bibcode in earlier]
        if rows:
            self.lib.add_screening(self.run_id, rows, round_)
            self._note_copied("screening", len(rows))
            self.log(f"  copied {len(rows)} screening decisions from {source}")
        return {b for b, _, _ in rows}

    def _reused_cards(self, bibcodes: list[str]) -> set[str]:
        """In a rerun, copy the original's card of each of these papers that it extracted with the same
        model and effort from byte for byte the text this run reads. The question is the same, so
        Claude would be asked the same thing. Refused cards are asked again. Returns the bibcodes copied."""
        found = self._source()
        if found is None:
            return set()
        source, before = found
        s = self.settings
        if (before.models["extract"], before.efforts["extract"]) != (s.models["extract"], s.efforts["extract"]):
            return set()
        copied = []
        for b in bibcodes:
            row, text = self.lib.card(source, b), self._stored(b)
            if row is None or row["status"] == "refused" or (
                    (row["text_source"], row["text_version"], row["text_checksum"]) != (text.source, text.version, text.checksum)):
                continue
            note = "; ".join(n for n in (row["note"], f"copied from {source}") if n)
            self.lib.put_card(self.run_id, b, json.loads(row["card"]), verified=bool(row["verified"]),
                              status=row["status"], read_depth=row["read_depth"], text=text, note=note)
            copied.append(b)
        if copied:
            self._note_copied("cards", copied)
            self.log(f"  copied {len(copied)} cards from {source}, of papers read from the same text")
        return set(copied)

    @property
    def ads(self) -> ADSClient:
        if self._ads is None:
            self._ads = ADSClient()
        return self._ads

    @property
    def arxiv(self) -> ArxivClient:
        if self._arxiv is None:
            self._arxiv = ArxivClient()
        return self._arxiv

    @property
    def oa(self) -> OpenAccessClient:
        if self._oa is None:
            self._oa = OpenAccessClient()
        return self._oa

    def get(self, key: str, default=None):
        return self.lib.get(self.run_id, key, default)

    def put(self, key: str, value) -> None:
        self.lib.put(self.run_id, key, value)

    # --- driver -------------------------------------------------------------------------

    def execute(self) -> str:
        with run_lock(self.lib, self.run_id):
            return self._execute()

    def _execute(self) -> str:
        row = self.lib.run(self.run_id)
        if row["stage"] == "done":
            self.lib.set_run(self.run_id, status="done", error="")
            return "done"
        if self.get("started_at") is None:
            self.put("started_at", time.time())
        self.lib.set_run(self.run_id, status="running", error="")
        self.put("cap_pause", None)
        try:
            if self._pending_addition() is not None:
                self.log("[add]")
                if not self.add_papers():
                    self.lib.set_run(self.run_id, status="done", stage="done")
                    self.log("nothing to add; the review is unchanged")
                    return "done"
            for name in STAGES[STAGES.index(row["stage"]) : -1]:
                self.log(f"[{name}]")
                getattr(self, f"stage_{name}")()
                self.lib.set_run(self.run_id, stage=STAGES[STAGES.index(name) + 1])
        except (ADSQuotaLow, CostCapReached, Paused) as e:
            if isinstance(e, CostCapReached):
                self.put("cap_pause", {"estimate": e.estimate, "spent": e.spent, "cap": e.cap})
            self.lib.set_run(self.run_id, status="paused", error=str(e))
            self.log(f"paused: {e}\ncontinue with: litreview resume {self.run_id}")
            return "paused"
        except KeyboardInterrupt:
            self.lib.set_run(self.run_id, status="paused", error="interrupted")
            self.log(f"interrupted; continue with: litreview resume {self.run_id}")
            return "paused"
        except Exception as e:
            self.lib.set_run(self.run_id, status="failed", error=f"{e}\n{traceback.format_exc()}")
            self.log(f"failed: {e}")
            raise
        self.lib.set_run(self.run_id, status="done", stage="done")
        self.log(f"done: {self.folder}")
        return "done"

    # --- 1. plan ------------------------------------------------------------------------

    def _query(self, q: str, since, until) -> str:
        return build_query(q, since, until, self.settings.refereed_only)

    def stage_plan(self) -> None:
        s = self.settings
        model, effort = s.models["plan"], s.efforts["plan"]
        p = planning.plan_queries(self.llm, model, effort, s.question)
        since, until = s.since or p.since, s.until or p.until
        counts = {q: self.ads.count(self._query(q, since, until)) for q in p.queries}
        first_counts = dict(counts)
        if any(n == 0 or n > MAX_HITS for n in counts.values()):
            p = planning.plan_queries(self.llm, model, effort, s.question, counts_feedback=counts, previous=p)
            counts = {q: self.ads.count(self._query(q, since, until)) for q in p.queries}
        queries = [q for q in p.queries if counts[q] > 0]
        if not queries:
            raise RuntimeError("none of the planned queries returned ADS results")
        self.put("plan", {"interpretation": p.interpretation, "since": since, "until": until,
                          "planned": {"since": p.since, "until": p.until},
                          "queries": queries, "counts": counts, "first_counts": first_counts})
        for q in queries:
            self.log(f"  {counts[q]:>6}  {q}")

    # --- 2. search ----------------------------------------------------------------------

    def _search(self, q: str, via: str) -> int:
        """One ADS search, recorded as done even when it finds nothing, so a resume skips it."""
        searched = self.get("searched", [])
        if q in searched:
            return 0
        plan = self.get("plan")
        n, docs = self.ads.search(self._query(q, plan["since"], plan["until"]), rows=200)
        if via == "query" and q not in plan["counts"]:  # a rerun with other years or filters
            plan["counts"][q] = n
            self.put("plan", plan)
        bibcodes = []
        for doc in docs:
            paper = paper_from_record(doc)
            self.lib.upsert_paper(paper)
            bibcodes.append(paper.bibcode)
        self.lib.add_hits(self.run_id, q, via, bibcodes)
        self.put("searched", [*searched, q])
        return len(bibcodes)

    def _merge(self) -> list[dict]:
        hits = [(r["query"], r["rank"], self.lib.paper(r["bibcode"])) for r in self.lib.hits(self.run_id)]
        merged = planning.merge([(q, rank, p) for q, rank, p in hits if p is not None])
        alias = {}
        for c in merged:
            self.lib.upsert_paper(c.paper)
            for b in c.paper.all_bibcodes:
                alias[b] = c.paper.bibcode
        rows = [{"bibcode": c.paper.bibcode, "score": round(c.score, 4), "queries": sorted(c.queries)} for c in merged]
        self.put("merged", rows)
        self.put("alias", alias)
        return rows

    def _arxiv_dates(self, bibcodes: list[str]) -> None:
        """First-posting month from each arXiv id, so candidates are dated without any arXiv request."""
        for p in (self.lib.paper(b) for b in bibcodes):
            if p and p.arxiv_id and not p.first_arxiv_date and (month := posting_month(p.arxiv_id)):
                self.lib.set_arxiv(p.bibcode, first_date=month)

    def _arxiv_meta(self, bibcodes: list[str]) -> None:
        """Exact posting date, latest version and arXiv's abstract, for the papers about to be read.
        Optional: if the arXiv API is unavailable the run keeps the month from the id."""
        papers = [p for p in (self.lib.paper(b) for b in bibcodes) if p and p.arxiv_id and not p.arxiv_version]
        if not papers:
            return
        try:
            meta = self.arxiv.metadata([p.arxiv_id for p in papers])
        except httpx.HTTPError as e:
            self.log(f"  arXiv metadata unavailable ({e}); {len(papers)} papers keep the month in their arXiv id"
                     " and, until it can be fetched, their ADS abstract")
            return
        for p in papers:
            m = meta.get(p.arxiv_id)
            if m:
                self.lib.set_arxiv(p.bibcode, version=m.latest_version, first_date=m.published, abstract=m.abstract)

    def stage_search(self) -> None:
        for q in self.get("plan")["queries"]:
            n = self._search(q, "query")
            if n:
                self.log(f"  {n:>4} records  {q}")
        merged = self._merge()
        self.log(f"  {len(merged)} unique papers")
        top = [m["bibcode"] for m in merged[: self.settings.screen_top]]
        self._arxiv_dates(top)
        self._arxiv_meta(top)  # arXiv's abstracts, so screening sends Claude as few ADS abstracts as it can

    # --- 3. screen ----------------------------------------------------------------------

    def _screen(self, bibcodes: list[str], round_: str) -> None:
        s = self.settings
        interpretation = self.get("plan")["interpretation"]
        done = {r["bibcode"] for r in self.lib.screening(self.run_id)}
        todo = [p for p in (self.lib.paper(b) for b in bibcodes) if p and p.bibcode not in done]
        # Recorded for the report while the ADS terms question is open.
        ads_only = set(self.get("screened_on_ads_abstract", [])) | {
            p.bibcode for p in todo if p.ads_abstract and not p.arxiv_abstract}
        self.put("screened_on_ads_abstract", sorted(ads_only))
        copied = self._reused_screening(todo, round_)
        todo = [p for p in todo if p.bibcode not in copied]
        for start in range(0, len(todo), planning.SCREEN_BATCH):
            chunk = todo[start : start + planning.SCREEN_BATCH]
            result = planning.screen(self.llm, s.models["screen"], s.efforts["screen"], s.question,
                                     interpretation, chunk)
            self.lib.add_screening(self.run_id, [(b, sc, r) for b, (sc, r) in result.items()], round_)
            self.log(f"  screened {start + len(chunk)}/{len(todo)}")

    def stage_screen(self) -> None:
        merged = self.get("merged")
        self._screen([m["bibcode"] for m in merged[: self.settings.screen_top]], "search")

    def _ranked_inclusions(self) -> list[tuple[str, int, str]]:
        alias = self.get("alias", {})
        order = {m["bibcode"]: i for i, m in enumerate(self.get("merged", []))}
        best: dict[str, tuple[int, str]] = {}
        for r in self.lib.screening(self.run_id):
            b = alias.get(r["bibcode"], r["bibcode"])
            if b not in best or r["score"] > best[b][0]:
                best[b] = (r["score"], r["reason"])
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], order.get(kv[0], 10**9)))
        return [(b, sc, reason) for b, (sc, reason) in ranked if sc >= 2]

    # --- 4. expand ----------------------------------------------------------------------

    def stage_expand(self) -> None:
        s = self.settings
        if not s.expand:
            return
        state = self.get("expansion")
        if state is None:
            # Saved before searching, so a resumed run expands from the same seeds and screened set.
            seeds = [b for b, _, _ in self._ranked_inclusions()[: s.expand_seeds]]
            if not seeds:
                return
            screened = sorted({r["bibcode"] for r in self.lib.screening(self.run_id)})
            state = {"seeds": seeds, "screened_before": screened, "queries": {}, "new": None}
            self.put("expansion", state)
        seed_q = "bibcode:(" + " OR ".join(f'"{b}"' for b in state["seeds"]) + ")"
        for op in OPERATORS:
            q = f"{op}({seed_q})"
            if q not in state["queries"]:
                state["queries"][q] = self._search(q, op)
                self.put("expansion", state)
        if state["new"] is None:
            merged = self._merge()
            alias = self.get("alias")
            seen = {alias.get(b, b) for b in state["screened_before"]}
            via_graph = {alias.get(r["bibcode"], r["bibcode"]) for r in self.lib.hits(self.run_id) if r["via"] in OPERATORS}
            state["new"] = [m["bibcode"] for m in merged
                            if m["bibcode"] in via_graph and m["bibcode"] not in seen][: s.expand_max]
            self.put("expansion", state)
        new = state["new"]
        self.log(f"  {len(new)} new papers from the citation graph")
        self._arxiv_dates(new)
        self._arxiv_meta(new)
        self._screen(new, "expand")

    # --- 5. select ----------------------------------------------------------------------

    def _citers(self, bibcodes: list[str]) -> dict[str, int]:
        """How many of these papers cite each one, from the reference lists that ADS searches return."""
        papers = self.lib.papers(bibcodes)
        alias = self.lib.aliases()
        owner = {k: b for b, p in papers.items() for k in p.all_bibcodes}
        stored = self.lib.references(list(owner))
        out = dict.fromkeys(bibcodes, 0)
        for b, p in papers.items():
            refs = {r for k in p.all_bibcodes for r in stored.get(k, [])}
            for target in {owner.get(r) or owner.get(alias.get(r, r)) for r in refs} - {None, b}:
                out[target] += 1
        return out

    def stage_select(self) -> None:
        """The papers to read come in turn from two rankings of the included papers: the search ranking,
        and how many of the other included papers cite each one (best screening score first in both).
        Citations bring in the papers the field builds on that the searches ranked low; the search
        ranking keeps recent papers, which have had little time to be cited."""
        included = self._ranked_inclusions()
        score = {b: sc for b, sc, _ in included}
        by_search = [b for b, _, _ in included]
        citers = self._citers(by_search)
        by_citations = sorted((b for b in by_search if citers[b] >= CITED_MIN), key=lambda b: (-score[b], -citers[b]))
        n = self.settings.max_papers
        picks = take_turns([by_search, by_citations], n)
        selected = [b for b, _ in picks]
        # Only a choice when not every included paper can be read.
        for_citations = {b: citers[b] for b, k in picks if k == 1} if len(included) > n else {}
        self.put("selected", selected)
        self.put("chosen_for_citations", for_citations)
        self.put("not_read", [[b, sc, r] for b, sc, r in included if b not in set(selected)])
        if len(included) > n:
            self.log(f"  {len(included)} included; reading {len(selected)}: {len(selected) - len(for_citations)} by "
                     f"search rank, {len(for_citations)} that the other included papers cite most")
        else:
            self.log(f"  {len(included)} included; reading all of them")

    # --- adding papers to a finished review -------------------------------------------------

    def _pending_addition(self) -> dict | None:
        additions = self.get("additions", []) or []
        return additions[-1] if additions and not additions[-1]["done"] else None

    def add_papers(self) -> bool:
        """Take in the papers asked for with request_additions: look up any the library lacks in ADS,
        keep the current outputs in a v<N> folder, and clear the draft and its checks so the stages
        from fetch on read the new papers, and again those with a newer text, and write the review
        again from every card. Each step can be repeated after an interruption. False if none of the
        papers could be added."""
        additions = self.get("additions")
        rec = additions[-1]
        if rec["added"] is None:
            alias = self.lib.aliases()
            unknown = [b for b in rec["requested"] if b not in alias]
            for q in identifier_queries(unknown):
                _, docs = self.ads.search(q, rows=200)
                for doc in docs:
                    self.lib.upsert_paper(paper_from_record(doc))
            alias = self.lib.aliases() if unknown else alias
            read, texts = set(self.get("selected", [])), self.get("texts", {})
            rec["added"] = list(dict.fromkeys(alias[b] for b in rec["requested"] if b in alias and (
                alias[b] not in read or has_newer_text(self.lib, texts, alias[b]))))
            rec["reread"] = [b for b in rec["added"] if b in read]
            rec["not_found"] = [b for b in rec["requested"] if b not in alias]
            rec["previous"] = f"v{sum(1 for a in additions if a.get('previous')) + 1}" if rec["added"] else None
            self.put("additions", additions)
        for b in rec["not_found"]:
            self.log(f"  {b}: ADS has no record of it")
        if not rec["added"]:
            rec["done"] = True
            self.put("additions", additions)
            return False

        keep = self.folder / rec["previous"]
        keep.mkdir(exist_ok=True)
        for f in self.folder.iterdir():
            if f.is_file() and f.name != "worker.log":
                shutil.copy2(f, keep / f.name)
        selected = self.get("selected", [])
        self.put("selected", selected + [b for b in rec["added"] if b not in selected])
        texts = self.get("texts", {})
        for b in rec.get("reread", []):  # fetched and carded again, from the newest text
            texts.pop(b, None)
            self.lib.delete_card(self.run_id, b)
        self.put("texts", texts)
        self.put("not_read", [x for x in self.get("not_read", []) if x[0] not in set(rec["added"])])
        for key in ("cards_text", "draft_v1", "draft_v2", "check_v1", "check_v2", "mend_reply", "mend_labels"):
            self.put(key, None)
        self._arxiv_dates(rec["added"])
        rec["done"] = True
        self.put("additions", additions)
        again = f" ({len(rec['reread'])} of them again, from a newer text)" if rec.get("reread") else ""
        self.log(f"  reading {len(rec['added'])} more{again}: {', '.join(rec['added'])}; "
                 f"the previous version is kept in {rec['previous']}/")
        return True

    # --- 6. fetch -----------------------------------------------------------------------

    def _full_text(self, p: Paper) -> StoredText | None:
        try:
            text, _ = read_full_text(self.lib, p, arxiv=self.arxiv, ads=lambda: self.ads, oa=lambda: self.oa)
        except RateLimited as e:
            # Reading only the abstract because arXiv is throttling would quietly weaken the review.
            raise Paused(f"{e}. {p.bibcode} could not be read in full; papers read so far are kept") from None
        return text

    def _abstract_text(self, p: Paper) -> StoredText | None:
        if not p.abstract:
            self.lib.log_fetch(p.bibcode, p.arxiv_id, "abstract", False, "no abstract")
            return None
        paragraphs = [{"label": "Abstract", "text": p.abstract}]
        # Keyed by content: an abstract that changes upstream becomes a new row, never an edit.
        version = f"{'arxiv' if p.arxiv_abstract else 'ads'}:{checksum(paragraphs)[:12]}"
        return self.lib.add_text(p.bibcode, "abstract", version, paragraphs)

    def stage_fetch(self) -> None:
        texts = self.get("texts", {})
        selected = self.get("selected")
        if not self.settings.abstracts_only:
            self._arxiv_meta([b for b in selected if b not in texts])
        for i, b in enumerate(selected, 1):
            if b in texts:
                continue
            p = self.lib.paper(b)
            text = None if self.settings.abstracts_only else self._full_text(p)
            text = text or self._abstract_text(p)
            if text is not None:
                texts[b] = {"source": text.source, "version": text.version}
                self.put("texts", texts)
            self.log(f"  {i}/{len(selected)} {b}: {text.source if text else 'nothing readable'}")

    # --- 7. extract ---------------------------------------------------------------------

    def _stored(self, b: str) -> StoredText:
        t = self.get("texts")[b]
        return self.lib.text(b, t["source"], t["version"])

    def _wants_summary(self, p: Paper, text: StoredText) -> bool:
        """A question-independent summary is written once per full text, during extraction."""
        return text.source != "abstract" and self.lib.summary_for(p.all_bibcodes, text) is None

    def _extract_params(self, b: str) -> dict:
        p, text = self.lib.paper(b), self._stored(b)
        return request_params(self.settings.question, p, text, summary=self._wants_summary(p, text))

    def _save_card(self, b: str, reply: Reply) -> None:
        p, text = self.lib.paper(b), self._stored(b)
        wanted = self._wants_summary(p, text)
        card = build_card(p, text, parse_tagged(reply.blocks))
        card, status, notes = verify_card(card, len(text.paragraphs))
        summary = summary_of(card)
        card.pop("summary", None)  # it describes the paper, not this run's question, so it is kept apart
        if wanted and summary:
            self.lib.put_summary(b, text, summary, model=reply.model, run_id=self.run_id)
        elif wanted:
            notes.append("no summary with a cited result")
        if reply.stop_reason == "max_tokens":
            notes.append("reply cut off at max_tokens")
        if reply.fallback:
            notes.append(f"fallback: {reply.fallback}")
        self.lib.put_card(self.run_id, b, card, verified=status == "verified", status=status,
                          read_depth=card["read_depth"], text=text, note="; ".join(notes))

    def _extract_one(self, b: str, stage: str = "extract") -> None:
        s = self.settings
        text = self._stored(b)
        params = self._extract_params(b)
        try:
            reply = self.llm.call(stage, s.models["extract"], s.efforts["extract"], params["messages"],
                                  system=params["system"], max_tokens=EXTRACT_MAX_TOKENS)
        except Refused as e:
            card = {"bibcode": b, "read_depth": "abstract_only" if text.source == "abstract" else "full_text"}
            self.lib.put_card(self.run_id, b, card, verified=False, status="refused",
                              read_depth=card["read_depth"], text=text, note=str(e))
            return
        self._save_card(b, reply)

    def stage_extract(self) -> None:
        s = self.settings
        texts = self.get("texts", {})
        done = {r["bibcode"] for r in self.lib.cards(self.run_id)}
        todo = [b for b in self.get("selected") if b in texts and b not in done]
        copied = self._reused_cards(todo)
        todo = [b for b in todo if b not in copied]
        if not todo:
            return
        if not s.use_batch or len(todo) == 1:
            for i, b in enumerate(todo, 1):
                self._extract_one(b)
                self.log(f"  card {i}/{len(todo)} {b}")
            return
        batch = self.get("batch")
        if batch is None:
            ids = {f"p{i}": b for i, b in enumerate(todo)}
            requests = []
            for cid, b in ids.items():
                requests.append((cid, self._extract_params(b)))
            batch_id = self.llm.batch_submit("extract", s.models["extract"], s.efforts["extract"],
                                             requests, max_tokens=EXTRACT_MAX_TOKENS)
            batch = {"id": batch_id, "ids": ids}
            self.put("batch", batch)
            self.log(f"  submitted batch {batch_id} with {len(ids)} papers")
        while True:
            status, counts = self.llm.batch_status(batch["id"])
            if status == "ended":
                break
            self.log(f"  batch {status}: {counts}")
            if self.poll_interval < 0:
                raise Paused(f"batch {batch['id']} is still {status}")
            time.sleep(self.poll_interval)
        handled = set(batch.get("handled", []))
        results = self.llm.batch_results("extract", s.models["extract"], s.efforts["extract"], batch["id"],
                                         skip=handled)
        retry = []
        for cid, b in batch["ids"].items():
            if cid in handled:
                continue
            res = results.get(cid, "missing from results")
            if isinstance(res, Reply) and res.stop_reason != "refusal":
                self._save_card(b, res)
            else:
                retry.append((b, res if isinstance(res, str) else "refused"))
            handled.add(cid)
        batch["handled"] = sorted(handled)
        self.put("batch", batch)
        for b, why in retry:
            # Batches can't carry refusal fallbacks, so retry once interactively, where they apply.
            self.log(f"  retrying {b} outside the batch ({why})")
            self._extract_one(b, stage="extract-retry")
        self.put("batch", None)
        for b in todo:
            if self.lib.card(self.run_id, b) is None:
                self._extract_one(b)

    # --- 8. synthesize and check ------------------------------------------------------------

    def _evidence(self) -> tuple[set[str], dict[str, Paper], str]:
        citable = self.lib.citable(self.run_id)
        papers = self.lib.papers(sorted(citable))
        cards_text = self.get("cards_text")
        if cards_text is None:
            cards = [json.loads(r["card"]) for r in self.lib.cards(self.run_id) if r["bibcode"] in citable]
            cards_text = cards_block(cards, papers)
            self.put("cards_text", cards_text)  # stored so every call sends the same bytes (cache)
        return citable, papers, cards_text

    def stage_synthesize(self) -> None:
        s = self.settings
        citable, papers, cards_text = self._evidence()
        if not citable:
            raise RuntimeError("no paper produced a verified card, so there is nothing to cite")
        self.log(f"  {len(citable)} citable papers")
        md = draft(self.llm, s.models["synthesize"], s.efforts["synthesize"], cards_text, s.question,
                   self.get("plan")["interpretation"], list(papers.values()))
        self.put("draft_v1", md)
        (self.folder / "draft_v1.md").write_text(md)

    def _passages_for(self, citable: set[str]):
        cache: dict[str, list[str]] = {}

        def passages(keys: list[str]) -> list[str]:
            out = []
            for k in (keys or sorted(citable)):
                if k not in citable:
                    continue
                if k not in cache:
                    t = self._stored(k) if k in self.get("texts", {}) else None
                    row = self.lib.card(self.run_id, k)
                    items = []
                    if row:
                        card = json.loads(row["card"])
                        for kind in ("methods", "findings", "limitations"):
                            for it in card.get(kind, []):
                                items.extend(it.get("passage_text", []))
                    cache[k] = [p["text"] for p in (t.paragraphs if t else [])] + items
                out.extend(cache[k])
            return out

        return passages

    def check(self, d: Draft, label: str) -> tuple[list[Issue], dict[int, tuple[str, str]]]:
        """Rules 4-6 and the chronology. Sentences the claim check never rates come back without
        a label; the caller treats them as failing."""
        s = self.settings
        citable, papers, cards_text = self._evidence()
        issues = check_keys(d, citable)
        issues += check_names_and_quotes(d, self._passages_for(citable))
        issues += check_chronology(d, papers, citable)
        labels: dict[int, tuple[str, str]] = {}

        def rate(only: set[int] | None) -> None:
            for _, prompt in claim_check_prompts(d, only):
                reply = ask(self.llm, "check", s.models["synthesize"], s.efforts["synthesize"], cards_text,
                            prompt, max_tokens=16000)
                labels.update(parse_labels(reply.text))

        rate(None)
        missing = {sent.sid for sent, *_ in d.sentences()} - set(labels)
        if missing:
            rate(missing)  # one more try for sentences skipped, cut off or mis-formatted
        for sent, *_ in d.sentences():
            verdict, why = labels.get(sent.sid, ("unrated", ""))
            if verdict == "unsupported":
                issues.append(Issue("rule6", f"unsupported by the cited cards: {why}", sid=sent.sid))
        self.log(f"  {label}: {len(issues)} problems")
        return issues, labels

    def _checked(self, key: str, d: Draft, label: str) -> tuple[list[Issue], dict[int, tuple[str, str]]]:
        """check(), saved under `key` so a resumed run doesn't pay for it twice."""
        saved = self.get(key)
        if saved is not None:
            return [Issue(**i) for i in saved["issues"]], {int(k): tuple(v) for k, v in saved["labels"].items()}
        issues, labels = self.check(d, label)
        self.put(key, {"issues": [i.to_dict() for i in issues], "labels": {str(k): list(v) for k, v in labels.items()}})
        return issues, labels

    def _mend(self, d: Draft, cut: set[int], cut_entries: set[str]) -> tuple[dict, list[str]]:
        """Claude rewords each sentence that the cuts leave pointing back at removed text ("It was ...").
        A rewrite replaces the sentence in `d` only if it keeps the sentence's keys and passes rules 4-6.
        Returns {sid: (old text, new text, label, reason)} for the replaced ones, and notes for the report."""
        targets = mend_targets(d, cut, cut_entries)
        if not targets:
            return {}, []
        s = self.settings
        citable, _, cards_text = self._evidence()
        reply = self.get("mend_reply")
        if reply is None:
            reply = mend(self.llm, s.models["synthesize"], s.efforts["synthesize"], cards_text,
                         [(t.text, before, removed) for t, before, removed in targets])
            self.put("mend_reply", reply)
        proposed = parse_mends(reply)

        notes, old = [], {}  # old: sid -> the text before the rewrite
        sents = {t.sid: t for t, *_ in targets}
        for i, (sent, *_) in enumerate(targets, 1):
            new = " ".join(proposed.get(i, "").split())
            if not new or new == sent.text:
                continue
            if sorted(set(keys_in(new))) != sorted(set(sent.keys)):
                notes.append(f"kept S{sent.sid} as written; its rewrite changed the citations: “{new}”")
            elif split_sentences(new) != [new]:
                notes.append(f"kept S{sent.sid} as written; its rewrite was not one sentence: “{new}”")
            else:
                old[sent.sid] = sent.text
                sent.text, sent.keys = new, keys_in(new)
        if not old:
            return {}, notes

        # Rules 4 and 5 on the rewrites, then the claim check on those that pass them.
        rule_issues = check_keys(d, citable) + check_names_and_quotes(d, self._passages_for(citable))
        failed = {i.sid: i.detail for i in rule_issues if i.sid in old}
        saved = self.get("mend_labels")
        if saved is None:
            labels: dict[int, tuple[str, str]] = {}
            for _, prompt in claim_check_prompts(d, set(old) - set(failed)):
                reply = ask(self.llm, "check", s.models["synthesize"], s.efforts["synthesize"], cards_text,
                            prompt, max_tokens=8000)
                labels.update(parse_labels(reply.text))
            self.put("mend_labels", {str(k): list(v) for k, v in labels.items()})
        else:
            labels = {int(k): tuple(v) for k, v in saved.items()}

        mended = {}
        for sid, text in old.items():
            sent = sents[sid]
            label, why = labels.get(sid, ("unrated", ""))
            if sid in failed or label in ("unsupported", "unrated"):
                reason = failed.get(sid) or f"was rated {label}" + (f" ({why})" if why else "")
                notes.append(f"kept S{sid} as written; its rewrite {reason}: “{sent.text}”")
                sent.text, sent.keys = text, keys_in(text)
            else:
                notes.append(f"reworded S{sid}, which followed removed text: “{text}” → “{sent.text}”")
                mended[sid] = (text, sent.text, label, why)
        return mended, notes

    def stage_check(self) -> None:
        s = self.settings
        citable, papers, cards_text = self._evidence()
        d1 = parse_draft(self.get("draft_v1"))
        issues, labels = self._checked("check_v1", d1, "draft")
        log = [("Draft", [i.describe(d1) for i in issues])]
        final = d1
        if issues:
            md2 = self.get("draft_v2")
            if md2 is None:
                md2 = revise(self.llm, s.models["synthesize"], s.efforts["synthesize"], cards_text,
                             d1.to_markdown(), [i.describe(d1) for i in issues])
                self.put("draft_v2", md2)
                (self.folder / "draft_v2.md").write_text(md2)
            final = parse_draft(md2)
            issues, labels = self._checked("check_v2", final, "revision")
            log.append(("Revision", [i.describe(final) for i in issues]))

        notes = []
        cut = {i.sid for i in issues if i.sid is not None and i.rule in ("rule4", "rule5", "rule6")}
        cut_entries = {i.key for i in issues if i.rule == "rule4" and i.key}
        bad_headings = {i.heading for i in issues if i.heading is not None}
        partial, partial_reasons, unrated, verdicts = [], {}, 0, {}
        for sent, *_ in final.sentences():
            verdict = labels.get(sent.sid, ("unrated", ""))[0]
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if sent.sid in cut:
                notes.append(f"removed S{sent.sid}: “{sent.text}”")
            elif verdict == "unrated":
                # Fail closed: a sentence the claim check never rated is not kept.
                cut.add(sent.sid)
                unrated += 1
                notes.append(f"removed S{sent.sid}, which the claim check did not rate: “{sent.text}”")
            elif verdict == "partial":
                partial.append(sent.text)
                partial_reasons[sent.text] = labels.get(sent.sid, ("", ""))[1]
        for k in sorted(cut_entries):
            notes.append(f"removed the entry for [@{k}], whose key is not citable")
        mended, mend_notes = self._mend(final, cut, cut_entries)
        for sid, (old, new, label, why) in mended.items():
            labels[sid] = (label, why)
            if old in partial:
                partial.remove(old)
                partial_reasons.pop(old, None)
            if label == "partial":
                partial.append(new)
                partial_reasons[new] = why
        notes += mend_notes
        final = remove_sentences(final, cut, cut_entries)
        final, heading_notes = strip_heading_problems(final, bad_headings)
        final, chron_notes = fix_chronology(final, papers)
        self.put("checks", log)
        self.put("final_draft", final.to_markdown())
        self.put("final_notes", notes + heading_notes + chron_notes)
        self.put("partial", partial)
        self.put("partial_reasons", partial_reasons)
        self.put("unrated", unrated)
        # Labels on the checked draft, before cuts, for evaluation.
        self.put("claim_labels", verdicts)
        self.put("sentence_labels", {s.text: labels.get(s.sid, ("unrated", ""))[0] for s, *_ in final.sentences()})

    # --- 9. render ----------------------------------------------------------------------

    def final_draft(self) -> Draft:
        d = parse_draft(self.get("final_draft"))
        partial = set(self.get("partial", []))
        for sent, *_ in d.sentences():
            if sent.text in partial:
                sent.flags.append("partial")
        return d

    def stage_render(self) -> None:
        self.write_outputs(self.final_draft())

    def write_outputs(self, final: Draft) -> None:
        s = self.settings
        citable, papers, _ = self._evidence()
        plan = self.get("plan")
        cited = sorted(final.keys() & citable)
        texts = self.get("texts", {})
        sources: dict[str, int] = {}
        for b in cited:
            src = texts.get(b, {}).get("source", "?")
            sources[src] = sources.get(src, 0) + 1
        cards = {r["bibcode"]: r for r in self.lib.cards(self.run_id)}
        read = [b for b in self.get("selected", []) if b in cards]
        full = sum(1 for b in read if cards[b]["read_depth"] == "full_text")
        stats = {"cited": len(cited), "read": len(read), "full_text": full,
                 "abstract_only": len(read) - full, "sources": sources}
        # The planner wrote the interpretation before reading anything; show it only if it is clean.
        interpretation = plan["interpretation"] if not text_problems(plan["interpretation"]) else None
        review = render_review(final, papers, citable, question=s.question,
                               interpretation=interpretation, since=plan["since"],
                               until=plan["until"], stats=stats, title=f"Literature review: {s.question}")
        (self.folder / "review.md").write_text(review)
        (self.folder / "draft.md").write_text(final.to_markdown())
        (self.folder / "refs.bib").write_text(self.ads.export_bibtex(cited) if cited else "")
        with open(self.folder / "cards.jsonl", "w") as f:
            for b, r in cards.items():
                rec = {"bibcode": b, "status": r["status"], "verified": bool(r["verified"]),
                       "note": r["note"], "card": json.loads(r["card"])}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        (self.folder / "report.md").write_text(render_report(self.report_context(final, cited, stats)))
        (self.folder / "run.json").write_text(json.dumps(self.run_record(stats), indent=2, ensure_ascii=False))

    def report_context(self, final: Draft, cited: list[str], stats: dict) -> dict:
        cards = {r["bibcode"]: r for r in self.lib.cards(self.run_id)}
        texts = self.get("texts", {})
        selected = self.get("selected", [])
        additions = self.get("additions", []) or []
        rerun = self.get("rerun_of")
        added = {b for a in additions for b in a.get("added") or [] if b not in (a.get("reread") or [])}
        added |= set(rerun["added"]) if rerun else set()  # added to the review this one reruns
        reread = {b for a in additions for b in a.get("reread") or []}
        for_citations = self.get("chosen_for_citations", {}) or {}
        read_rows = []
        for b in selected:
            t = texts.get(b)
            r = cards.get(b)
            card_status = r["status"] if r else "no card"
            if r and r["status"] == "excluded":
                card_status += f" ({json.loads(r['card']).get('relevance_reason', '')})"
            text = f"{t['source']} {t['version']}" if t else "nothing readable"
            chosen = ("added later" if b in added else
                      f"cited by {for_citations[b]} included papers" if b in for_citations else "search rank")
            if b in reread:
                chosen += ", read again from a newer text"
            read_rows.append([b + (" (cited)" if b in cited else ""), chosen, text, card_status, r["note"] if r else ""])
        screened = self.lib.screening(self.run_id)
        excluded = [[r["bibcode"], r["score"], r["reason"]] for r in screened if not r["include"]]
        failures = [[r["bibcode"], r["source"], r["reason"]] for r in self.lib.fetch_failures(selected)]
        usage = [[u["stage"], u["model"], u["effort"], u["calls"], u["input_tokens"], u["output_tokens"],
                  u["cache_write_tokens"], u["cache_read_tokens"], f"{u['cost']:.3f}"]
                 for u in self.lib.usage_by_stage(self.run_id)]
        fallbacks = [[f["stage"], f["model"], f["stop_reason"], f["fallback"] or "", f["request_id"] or ""]
                     for f in self.lib.fallbacks(self.run_id)]
        return {
            "run_id": self.run_id, "question": self.settings.question, "plan": self.get("plan"), "rerun_of": rerun,
            "expansion": self.get("expansion"),
            "counts": {"found": len(self.get("merged", [])), "screened": len(screened),
                       "included": sum(1 for r in screened if r["include"]), "read": stats["read"],
                       "full_text": stats["full_text"], "abstract_only": stats["abstract_only"],
                       "citable": len(self.lib.citable(self.run_id)), "cited": len(cited),
                       "screened_on_ads_abstract": len(self.get("screened_on_ads_abstract", []))},
            "read_rows": read_rows, "additions": additions, "not_read": self.get("not_read", []),
            "fetch_failures": failures,
            "excluded": excluded, "checks": self.get("checks", []), "final_notes": self.get("final_notes", []),
            "n_partial": len(self.get("partial", [])), "unrated": self.get("unrated", 0),
            "fallbacks": fallbacks, "usage": usage, "total_cost": self.lib.run_cost(self.run_id),
        }

    def run_record(self, stats: dict) -> dict:
        started = self.get("started_at") or time.time()
        return {
            "run_id": self.run_id, "litreview_version": VERSION, "settings": self.settings.to_dict(),
            "plan": self.get("plan"), "stats": stats, "elapsed_seconds": round(time.time() - started),
            "total_cost_usd": round(self.lib.run_cost(self.run_id), 4),
            "usage_by_stage": [dict(u) for u in self.lib.usage_by_stage(self.run_id)],
            "fallbacks_and_refusals": [dict(f) for f in self.lib.fallbacks(self.run_id)],
        }
