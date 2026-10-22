"""Full-text search over the library, kept in its own SQLite file so the web UI never writes to
library.sqlite. The index is rebuilt whenever the library's papers or texts have changed; at
thousands of papers and hundreds of texts that takes a second or two.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
    bibcode UNINDEXED, title, authors, abstract, tokenize = 'porter unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS para_fts USING fts5(
    bibcode UNINDEXED, source UNINDEXED, version UNINDEXED, idx UNINDEXED, label, text,
    tokenize = 'porter unicode61 remove_diacritics 2');
"""

# Snippet markers that can't occur in paper text; the client turns them into highlights.
MARK_OPEN, MARK_CLOSE = "\u0002", "\u0003"
_PHRASE = re.compile(r'"([^"]+)"')
_WORD = re.compile(r"[\w][\w'\-]*", re.UNICODE)


def fts_query(text: str) -> str | None:
    """User text -> a safe FTS5 query: quoted phrases stay phrases, other words match as prefixes,
    and everything must match. FTS operators typed by the user are treated as words."""
    phrases = [p.strip() for p in _PHRASE.findall(text) if p.strip()]
    rest = _PHRASE.sub(" ", text)
    words = _WORD.findall(rest)
    parts = ['"' + " ".join(_WORD.findall(p)) + '"' for p in phrases if _WORD.findall(p)]
    parts += [f'"{w}"*' for w in words]
    return " ".join(parts) if parts else None


class SearchIndex:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def signature(lib_db: sqlite3.Connection) -> str:
        papers = lib_db.execute("SELECT COUNT(*), COALESCE(MAX(updated_at), 0) FROM papers").fetchone()
        texts = lib_db.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM texts").fetchone()
        return json.dumps([list(papers), list(texts)])

    def ensure_fresh(self, lib_db: sqlite3.Connection) -> None:
        sig = self.signature(lib_db)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
            if row and row["value"] == sig:
                return
            db.execute("DELETE FROM paper_fts")
            db.execute("DELETE FROM para_fts")
            for p in lib_db.execute("SELECT bibcode, title, authors, ads_abstract, arxiv_abstract FROM papers"):
                db.execute("INSERT INTO paper_fts VALUES (?, ?, ?, ?)",
                           (p["bibcode"], p["title"] or "", " ".join(json.loads(p["authors"] or "[]")),
                            p["arxiv_abstract"] or p["ads_abstract"] or ""))
            for t in lib_db.execute(
                "SELECT bibcode, source, version, paragraphs FROM texts WHERE source != 'abstract'"
            ):
                for i, para in enumerate(json.loads(t["paragraphs"])):
                    db.execute("INSERT INTO para_fts VALUES (?, ?, ?, ?, ?, ?)",
                               (t["bibcode"], t["source"], t["version"], i, para.get("label", ""), para.get("text", "")))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (sig,))

    def papers(self, text: str) -> list[str] | None:
        """Bibcodes whose title, authors or abstract match, best first; None for an empty query."""
        q = fts_query(text)
        if q is None:
            return None
        with self._connect() as db:
            rows = db.execute("SELECT bibcode FROM paper_fts WHERE paper_fts MATCH ? ORDER BY rank", (q,)).fetchall()
        return [r["bibcode"] for r in rows]

    def paragraphs(self, text: str, limit: int = 50) -> list[dict]:
        """Matching paragraphs of stored full texts, best first, with a highlighted snippet."""
        q = fts_query(text)
        if q is None:
            return []
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT bibcode, source, version, idx, label,
                       snippet(para_fts, 5, '{MARK_OPEN}', '{MARK_CLOSE}', ' … ', 32) AS snippet
                    FROM para_fts WHERE para_fts MATCH ? ORDER BY rank LIMIT ?""",
                (q, limit),
            ).fetchall()
        return [dict(r) for r in rows]
