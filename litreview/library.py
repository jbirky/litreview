"""The SQLite library.

Papers and their texts are shared across runs, so a paper read once is not downloaded again.
Cards, screening decisions and search hits belong to a run, because they depend on the question.
`citable()` is the closed set of papers a review may cite (rule 1 of the citation guarantee).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    bibcode            TEXT PRIMARY KEY,
    alternate_bibcodes TEXT NOT NULL DEFAULT '[]',
    title              TEXT NOT NULL DEFAULT '',
    authors            TEXT NOT NULL DEFAULT '[]',
    pubdate            TEXT NOT NULL DEFAULT '',
    pub                TEXT NOT NULL DEFAULT '',
    doi                TEXT,
    arxiv_id           TEXT,
    arxiv_version      TEXT,
    first_arxiv_date   TEXT,
    sort_date          TEXT NOT NULL DEFAULT '',
    doctype            TEXT NOT NULL DEFAULT '',
    refereed           INTEGER NOT NULL DEFAULT 0,
    citation_count     INTEGER NOT NULL DEFAULT 0,
    ads_abstract       TEXT,
    arxiv_abstract     TEXT,
    esources           TEXT NOT NULL DEFAULT '[]',
    updated_at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_lists (
    bibcode    TEXT PRIMARY KEY,
    refs       TEXT NOT NULL,     -- JSON: the bibcodes this paper cites, from ADS `reference`
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS texts (
    bibcode    TEXT NOT NULL,
    source     TEXT NOT NULL,     -- arxiv_src | arxiv_html | arxiv_pdf | ads_scan | unpaywall | upload | abstract
    version    TEXT NOT NULL,     -- e.g. 2101.01234v3, arxiv:<id>v<n> / ads for abstracts, <sha>[:p<a>-<b>][:ocr] for other PDFs
    paragraphs TEXT NOT NULL,     -- JSON [{label, text}], exactly as sent to extraction; never overwritten
    truncated  INTEGER NOT NULL DEFAULT 0,
    checksum   TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (bibcode, source, version)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    bibcode TEXT NOT NULL, arxiv_id TEXT, source TEXT NOT NULL,
    ok INTEGER NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    settings   TEXT NOT NULL,
    status     TEXT NOT NULL,     -- running | paused | done | failed
    stage      TEXT NOT NULL,
    folder     TEXT NOT NULL,
    error      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_data (
    run_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS search_hits (
    run_id TEXT NOT NULL, query TEXT NOT NULL, bibcode TEXT NOT NULL,
    rank INTEGER NOT NULL, via TEXT NOT NULL,
    PRIMARY KEY (run_id, query, bibcode)
);
CREATE TABLE IF NOT EXISTS screening (
    run_id TEXT NOT NULL, bibcode TEXT NOT NULL, score INTEGER NOT NULL,
    include INTEGER NOT NULL, reason TEXT NOT NULL, round TEXT NOT NULL,
    PRIMARY KEY (run_id, bibcode)
);
CREATE TABLE IF NOT EXISTS cards (
    run_id       TEXT NOT NULL,
    bibcode      TEXT NOT NULL,
    card         TEXT NOT NULL,
    verified     INTEGER NOT NULL,
    status       TEXT NOT NULL,   -- verified | excluded | no_findings | refused | error
    read_depth   TEXT NOT NULL,   -- full_text | abstract_only
    text_source  TEXT NOT NULL,
    text_version TEXT NOT NULL,
    text_checksum TEXT NOT NULL,  -- of the stored text the card was built and verified against
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, bibcode)
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    at     REAL NOT NULL,
    line   TEXT NOT NULL,  -- what the run printed, one line per row
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,     -- fetch | summarize | refs
    bibcode    TEXT NOT NULL,
    status     TEXT NOT NULL,     -- queued | running | done | failed
    error      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (
    bibcode       TEXT NOT NULL,
    text_source   TEXT NOT NULL,
    text_version  TEXT NOT NULL,
    text_checksum TEXT NOT NULL,  -- of the stored text the summary's passages point into
    summary       TEXT NOT NULL,  -- JSON: aim, methods, results, limitations; each item cites passages
    model         TEXT NOT NULL,
    run_id        TEXT NOT NULL,  -- the run whose extraction wrote it
    created_at    REAL NOT NULL,
    PRIMARY KEY (bibcode, text_source, text_version, text_checksum)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, stage TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
    batch INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
    cost REAL NOT NULL, stop_reason TEXT, fallback TEXT, request_id TEXT, at REAL NOT NULL
);
"""


FULL_TEXT_SOURCES = ("arxiv_src", "arxiv_html", "arxiv_pdf", "ads_scan", "unpaywall", "upload")
BOOK_DOCTYPES = {"book": "book", "inbook": "book chapter"}


def normalize_date(date: str | None) -> str:
    """ADS '2019-04-00' or arXiv '2019-01-15' -> '2019-04-01'; '' if unusable."""
    if not date or len(date) < 4 or not date[:4].isdigit():
        return ""
    parts = (date[:10].split("-") + ["01", "01"])[:3]
    year, month, day = parts[0], parts[1] or "01", parts[2] or "01"
    month = "01" if month in ("00", "") else month
    day = "01" if day in ("00", "") else day
    return f"{year}-{month}-{day}"


@dataclass
class Paper:
    bibcode: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    pubdate: str = ""
    pub: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    arxiv_version: str | None = None
    first_arxiv_date: str | None = None
    doctype: str = ""
    refereed: bool = False
    citation_count: int = 0
    ads_abstract: str | None = None
    arxiv_abstract: str | None = None
    esources: list[str] = field(default_factory=list)
    alternate_bibcodes: list[str] = field(default_factory=list)
    references: list[str] | None = None  # what it cites, from an ADS record; None when not loaded

    @property
    def sort_date(self) -> str:
        """The earlier of the first arXiv posting and the journal date."""
        dates = [d for d in (normalize_date(self.first_arxiv_date), normalize_date(self.pubdate)) if d]
        return min(dates) if dates else ""

    @property
    def year(self) -> int | None:
        d = self.sort_date
        return int(d[:4]) if d else None

    @property
    def pub_year(self) -> int | None:
        d = normalize_date(self.pubdate)
        return int(d[:4]) if d else None

    @property
    def abstract(self) -> str | None:
        """arXiv's copy when there is one (see the ADS terms open question), else ADS's."""
        return self.arxiv_abstract or self.ads_abstract

    @property
    def all_bibcodes(self) -> list[str]:
        return [self.bibcode, *self.alternate_bibcodes]

    @property
    def book_kind(self) -> str | None:
        """What kind of book the record is ("book" or "book chapter"), or None if it isn't one."""
        return BOOK_DOCTYPES.get(self.doctype)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Paper":
        return cls(
            bibcode=row["bibcode"],
            title=row["title"],
            authors=json.loads(row["authors"]),
            pubdate=row["pubdate"],
            pub=row["pub"],
            doi=row["doi"],
            arxiv_id=row["arxiv_id"],
            arxiv_version=row["arxiv_version"],
            first_arxiv_date=row["first_arxiv_date"],
            doctype=row["doctype"],
            refereed=bool(row["refereed"]),
            citation_count=row["citation_count"],
            ads_abstract=row["ads_abstract"],
            arxiv_abstract=row["arxiv_abstract"],
            esources=json.loads(row["esources"]),
            alternate_bibcodes=json.loads(row["alternate_bibcodes"]),
        )


@dataclass
class StoredText:
    bibcode: str
    source: str
    version: str
    paragraphs: list[dict]  # [{"label": ..., "text": ...}]
    truncated: bool
    checksum: str = ""


def checksum(paragraphs: list[dict]) -> str:
    blob = json.dumps(paragraphs, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


class Library:
    def __init__(self, path: Path | str, *, schema: bool = True, check_same_thread: bool = True):
        """schema=False skips creating tables, for short-lived readers (the web UI's per-request
        connections) of a library whose schema is already in place."""
        self.path = None if str(path) == ":memory:" else Path(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        # The CLI and the web UI share the file; wait for a writer instead of failing at once.
        self.db.execute("PRAGMA busy_timeout = 5000")
        if schema:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- papers -------------------------------------------------------------------------

    def upsert_paper(self, p: Paper) -> None:
        """Insert or refresh a paper. Known arXiv details survive a refresh that lacks them."""
        self.db.execute(
            """
            INSERT INTO papers (bibcode, alternate_bibcodes, title, authors, pubdate, pub, doi,
                arxiv_id, arxiv_version, first_arxiv_date, sort_date, doctype, refereed,
                citation_count, ads_abstract, arxiv_abstract, esources, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bibcode) DO UPDATE SET
                alternate_bibcodes = excluded.alternate_bibcodes,
                title = excluded.title, authors = excluded.authors, pubdate = excluded.pubdate,
                pub = excluded.pub, doi = COALESCE(excluded.doi, papers.doi),
                arxiv_id = COALESCE(excluded.arxiv_id, papers.arxiv_id),
                arxiv_version = COALESCE(excluded.arxiv_version, papers.arxiv_version),
                first_arxiv_date = COALESCE(excluded.first_arxiv_date, papers.first_arxiv_date),
                doctype = excluded.doctype, refereed = excluded.refereed,
                citation_count = excluded.citation_count,
                ads_abstract = COALESCE(excluded.ads_abstract, papers.ads_abstract),
                arxiv_abstract = COALESCE(excluded.arxiv_abstract, papers.arxiv_abstract),
                esources = excluded.esources, updated_at = excluded.updated_at
            """,
            (
                p.bibcode, json.dumps(p.alternate_bibcodes), p.title, json.dumps(p.authors),
                p.pubdate, p.pub, p.doi, p.arxiv_id, p.arxiv_version, p.first_arxiv_date,
                p.sort_date, p.doctype, int(p.refereed), p.citation_count, p.ads_abstract,
                p.arxiv_abstract, json.dumps(p.esources), time.time(),
            ),
        )
        # sort_date depends on fields COALESCE may have kept, so recompute from the stored row.
        stored = self.paper(p.bibcode)
        if stored is not None:
            self.db.execute(
                "UPDATE papers SET sort_date = ? WHERE bibcode = ?", (stored.sort_date, p.bibcode)
            )
        if p.references is not None:
            self._put_references(p.bibcode, p.references)
        self.db.commit()

    def _put_references(self, bibcode: str, refs: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO reference_lists (bibcode, refs, fetched_at) VALUES (?, ?, ?)",
                        (bibcode, json.dumps(refs), time.time()))

    def put_references(self, bibcode: str, refs: list[str]) -> None:
        self._put_references(bibcode, refs)
        self.db.commit()

    def references(self, bibcodes: list[str]) -> dict[str, list[str]]:
        """bibcode -> what it cites, for those of these bibcodes whose reference list is stored."""
        marks = ",".join("?" * len(bibcodes))
        rows = self.db.execute(f"SELECT bibcode, refs FROM reference_lists WHERE bibcode IN ({marks})", bibcodes)
        return {r["bibcode"]: json.loads(r["refs"]) for r in rows}

    def aliases(self) -> dict[str, str]:
        """Every stored bibcode, canonical or alternate, -> the paper it belongs to. A preprint record
        that another paper lists as an alternate belongs to that paper, as in find_paper."""
        out: dict[str, str] = {}
        for row in self.db.execute("SELECT bibcode, alternate_bibcodes FROM papers"):
            out.setdefault(row["bibcode"], row["bibcode"])
            for alt in json.loads(row["alternate_bibcodes"]):
                out[alt] = row["bibcode"]
        return out

    def set_arxiv(
        self, bibcode: str, *, arxiv_id: str | None = None, version: str | None = None,
        first_date: str | None = None, abstract: str | None = None,
    ) -> None:
        p = self.paper(bibcode)
        if p is None:
            return
        p.arxiv_id = arxiv_id or p.arxiv_id
        p.arxiv_version = version or p.arxiv_version
        p.first_arxiv_date = first_date or p.first_arxiv_date
        p.arxiv_abstract = abstract or p.arxiv_abstract
        self.upsert_paper(p)

    def paper(self, bibcode: str) -> Paper | None:
        row = self.db.execute("SELECT * FROM papers WHERE bibcode = ?", (bibcode,)).fetchone()
        return Paper.from_row(row) if row else None

    def papers(self, bibcodes: list[str]) -> dict[str, Paper]:
        out = {}
        for b in bibcodes:
            p = self.paper(b)
            if p is not None:
                out[b] = p
        return out

    # --- texts --------------------------------------------------------------------------

    def add_text(
        self, bibcode: str, source: str, version: str, paragraphs: list[dict], truncated: bool = False
    ) -> StoredText:
        """Store a text once. An existing row is kept as it is, so cards built from it stay valid."""
        self.db.execute(
            """INSERT OR IGNORE INTO texts
               (bibcode, source, version, paragraphs, truncated, checksum, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bibcode, source, version, json.dumps(paragraphs, ensure_ascii=False),
             int(truncated), checksum(paragraphs), time.time()),
        )
        self.db.commit()
        return self.text(bibcode, source, version)

    def touch_text(self, text: StoredText) -> None:
        """Make a stored text the newest of its paper's, so best_text picks it. Its content stays as it is."""
        self.db.execute("UPDATE texts SET fetched_at = ? WHERE bibcode = ? AND source = ? AND version = ?",
                        (time.time(), text.bibcode, text.source, text.version))
        self.db.commit()

    def text(self, bibcode: str, source: str, version: str) -> StoredText | None:
        row = self.db.execute(
            "SELECT * FROM texts WHERE bibcode = ? AND source = ? AND version = ?",
            (bibcode, source, version),
        ).fetchone()
        return self._text(row) if row else None

    def best_text(self, bibcodes: list[str], full_text: bool = True) -> StoredText | None:
        """Most recent stored text for any of a paper's bibcodes; full text preferred over abstracts."""
        marks = ",".join("?" * len(bibcodes))
        sources = FULL_TEXT_SOURCES if full_text else ("abstract",)
        row = self.db.execute(
            f"""SELECT * FROM texts WHERE bibcode IN ({marks}) AND source IN ({",".join("?" * len(sources))})
                ORDER BY fetched_at DESC LIMIT 1""",
            (*bibcodes, *sources),
        ).fetchone()
        return self._text(row) if row else None

    @staticmethod
    def _text(row: sqlite3.Row) -> StoredText:
        return StoredText(
            bibcode=row["bibcode"], source=row["source"], version=row["version"],
            paragraphs=json.loads(row["paragraphs"]), truncated=bool(row["truncated"]),
            checksum=row["checksum"],
        )

    def log_fetch(self, bibcode: str, arxiv_id: str | None, source: str, ok: bool, reason: str) -> None:
        self.db.execute(
            "INSERT INTO fetch_log (bibcode, arxiv_id, source, ok, reason, at) VALUES (?, ?, ?, ?, ?, ?)",
            (bibcode, arxiv_id, source, int(ok), reason, time.time()),
        )
        self.db.commit()

    def fetch_failures(self, bibcodes: list[str]) -> list[sqlite3.Row]:
        if not bibcodes:
            return []
        marks = ",".join("?" * len(bibcodes))
        return self.db.execute(
            f"SELECT * FROM fetch_log WHERE ok = 0 AND bibcode IN ({marks}) ORDER BY at", bibcodes
        ).fetchall()

    # --- runs ---------------------------------------------------------------------------

    def create_run(self, run_id: str, question: str, settings: dict, folder: str, status: str = "running") -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO runs (run_id, question, settings, status, stage, folder, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'plan', ?, ?, ?)""",
            (run_id, question, json.dumps(settings), status, folder, now, now),
        )
        self.db.commit()

    def reruns(self, run_id: str) -> list[str]:
        """The runs that ran this one again, oldest first."""
        return [r["run_id"] for r in self.db.execute(
            """SELECT d.run_id FROM run_data d JOIN runs r ON r.run_id = d.run_id
               WHERE d.key = 'rerun_of' AND json_extract(d.value, '$.run_id') = ? ORDER BY r.created_at""",
            (run_id,))]

    def set_settings(self, run_id: str, settings: dict) -> None:
        self.db.execute("UPDATE runs SET settings = ?, updated_at = ? WHERE run_id = ?",
                        (json.dumps(settings), time.time(), run_id))
        self.db.commit()

    def add_event(self, run_id: str, line: str) -> None:
        self.db.execute(
            """INSERT INTO run_events (run_id, seq, at, line)
               SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM run_events WHERE run_id = ?""",
            (run_id, time.time(), line, run_id),
        )
        self.db.commit()

    def events(self, run_id: str, after: int = 0, limit: int = 1000) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT seq, at, line FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after, limit),
        ).fetchall()

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def runs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()

    def set_run(self, run_id: str, *, status: str | None = None, stage: str | None = None,
                error: str | None = None) -> None:
        sets, args = ["updated_at = ?"], [time.time()]
        for col, val in (("status", status), ("stage", stage), ("error", error)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        self.db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?", (*args, run_id))
        self.db.commit()

    def put(self, run_id: str, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO run_data (run_id, key, value) VALUES (?, ?, ?)",
            (run_id, key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def get(self, run_id: str, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM run_data WHERE run_id = ? AND key = ?", (run_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    # --- search hits and screening --------------------------------------------------------

    def add_hits(self, run_id: str, query: str, via: str, bibcodes: list[str]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO search_hits (run_id, query, bibcode, rank, via) VALUES (?, ?, ?, ?, ?)",
            [(run_id, query, b, rank, via) for rank, b in enumerate(bibcodes, start=1)],
        )
        self.db.commit()

    def hits(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM search_hits WHERE run_id = ? ORDER BY query, rank", (run_id,)
        ).fetchall()

    def add_screening(self, run_id: str, rows: list[tuple[str, int, str]], round_: str) -> None:
        self.db.executemany(
            """INSERT OR REPLACE INTO screening (run_id, bibcode, score, include, reason, round)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(run_id, b, score, int(score >= 2), reason, round_) for b, score, reason in rows],
        )
        self.db.commit()

    def screening(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM screening WHERE run_id = ? ORDER BY score DESC", (run_id,)
        ).fetchall()

    # --- cards ----------------------------------------------------------------------------

    def put_card(self, run_id: str, bibcode: str, card: dict, *, verified: bool, status: str,
                 read_depth: str, text: StoredText, note: str = "") -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO cards (run_id, bibcode, card, verified, status, read_depth,
                   text_source, text_version, text_checksum, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, bibcode, json.dumps(card, ensure_ascii=False), int(verified), status,
             read_depth, text.source, text.version, text.checksum, note),
        )
        self.db.commit()

    def delete_card(self, run_id: str, bibcode: str) -> None:
        self.db.execute("DELETE FROM cards WHERE run_id = ? AND bibcode = ?", (run_id, bibcode))
        self.db.commit()

    # --- paper tasks (from the web UI's library: fetch a full text, write a summary) --------

    def add_task(self, kind: str, bibcode: str) -> int:
        """Queue a task, or return the one already queued or running for this paper and kind."""
        row = self.db.execute(
            "SELECT id FROM tasks WHERE kind = ? AND bibcode = ? AND status IN ('queued', 'running')",
            (kind, bibcode),
        ).fetchone()
        if row:
            return row["id"]
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO tasks (kind, bibcode, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
            (kind, bibcode, now, now),
        )
        self.db.commit()
        return cur.lastrowid

    def claim_task(self) -> sqlite3.Row | None:
        """Take the oldest queued task and mark it running."""
        row = self.db.execute(
            """UPDATE tasks SET status = 'running', updated_at = ?
               WHERE id = (SELECT id FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1)
               RETURNING *""",
            (time.time(),),
        ).fetchone()
        self.db.commit()
        return row

    def claim_tasks(self, kind: str, n: int) -> list[sqlite3.Row]:
        """Take up to n more queued tasks of this kind, oldest first, for one batched request."""
        rows = self.db.execute(
            """UPDATE tasks SET status = 'running', updated_at = ?
                WHERE id IN (SELECT id FROM tasks WHERE status = 'queued' AND kind = ? ORDER BY id LIMIT ?)
                RETURNING *""",
            (time.time(), kind, n),
        ).fetchall()
        self.db.commit()
        return sorted(rows, key=lambda r: r["id"])

    def finish_task(self, task_id: int, status: str, error: str = "") -> None:
        self.db.execute("UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                        (status, error, time.time(), task_id))
        self.db.commit()

    def latest_tasks(self, bibcodes: list[str] | None = None) -> dict[tuple[str, str], sqlite3.Row]:
        """(bibcode, kind) -> the newest task, for the given papers or all."""
        sql = "SELECT * FROM tasks"
        args: list = []
        if bibcodes is not None:
            sql += f" WHERE bibcode IN ({','.join('?' * len(bibcodes))})"
            args = list(bibcodes)
        out: dict[tuple[str, str], sqlite3.Row] = {}
        for row in self.db.execute(sql + " ORDER BY id", args):
            out[(row["bibcode"], row["kind"])] = row
        return out

    # --- question-independent summaries ----------------------------------------------------

    def put_summary(self, bibcode: str, text: StoredText, summary: dict, *, model: str, run_id: str) -> bool:
        """Store a summary for this exact text; an existing one is kept. Returns whether it was new."""
        cur = self.db.execute(
            """INSERT OR IGNORE INTO summaries (bibcode, text_source, text_version, text_checksum,
                   summary, model, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (bibcode, text.source, text.version, text.checksum, json.dumps(summary, ensure_ascii=False),
             model, run_id, time.time()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def summary_for(self, bibcodes: list[str], text: StoredText) -> sqlite3.Row | None:
        """The summary written from this text (same source, version and checksum) under any of the paper's bibcodes."""
        marks = ",".join("?" * len(bibcodes))
        return self.db.execute(
            f"""SELECT * FROM summaries WHERE bibcode IN ({marks}) AND text_source = ? AND text_version = ?
                AND text_checksum = ? ORDER BY created_at DESC LIMIT 1""",
            (*bibcodes, text.source, text.version, text.checksum),
        ).fetchone()

    def summaries(self, bibcodes: list[str]) -> list[sqlite3.Row]:
        """Every summary of a paper, newest first."""
        marks = ",".join("?" * len(bibcodes))
        return self.db.execute(
            f"SELECT * FROM summaries WHERE bibcode IN ({marks}) ORDER BY created_at DESC", bibcodes
        ).fetchall()

    def cards(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM cards WHERE run_id = ?", (run_id,)).fetchall()

    def card(self, run_id: str, bibcode: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM cards WHERE run_id = ? AND bibcode = ?", (run_id, bibcode)
        ).fetchone()

    def citable(self, run_id: str) -> set[str]:
        """Bibcodes whose ADS record, stored text and verified evidence card all exist,
        where the text is byte-for-byte the one the card was verified against."""
        rows = self.db.execute(
            """SELECT c.bibcode FROM cards c
               JOIN papers p ON p.bibcode = c.bibcode
               JOIN texts t ON t.bibcode = c.bibcode AND t.source = c.text_source
                           AND t.version = c.text_version AND t.checksum = c.text_checksum
               WHERE c.run_id = ? AND c.verified = 1 AND c.status = 'verified'""",
            (run_id,),
        ).fetchall()
        return {r["bibcode"] for r in rows}

    # --- usage ----------------------------------------------------------------------------

    def log_call(self, run_id: str, stage: str, model: str, effort: str, batch: bool,
                 usage: dict, cost: float, stop_reason: str | None, fallback: str | None,
                 request_id: str | None) -> None:
        self.db.execute(
            """INSERT INTO llm_calls (run_id, stage, model, effort, batch, input_tokens, output_tokens,
                   cache_write_tokens, cache_read_tokens, cost, stop_reason, fallback, request_id, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, stage, model, effort, int(batch), usage.get("input_tokens", 0),
             usage.get("output_tokens", 0), usage.get("cache_write_tokens", 0),
             usage.get("cache_read_tokens", 0), cost, stop_reason, fallback, request_id, time.time()),
        )
        self.db.commit()

    def run_cost(self, run_id: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) AS c FROM llm_calls WHERE run_id = ?", (run_id,)
        ).fetchone()
        return float(row["c"])

    def usage_by_stage(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT stage, model, effort, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens, SUM(cache_write_tokens) AS cache_write_tokens,
                      SUM(cache_read_tokens) AS cache_read_tokens, SUM(cost) AS cost
               FROM llm_calls WHERE run_id = ? GROUP BY stage, model, effort ORDER BY MIN(id)""",
            (run_id,),
        ).fetchall()

    def fallbacks(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT stage, model, stop_reason, fallback, request_id FROM llm_calls
               WHERE run_id = ? AND (fallback IS NOT NULL OR stop_reason = 'refusal')""",
            (run_id,),
        ).fetchall()
