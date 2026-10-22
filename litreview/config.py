"""Paths, credentials, prices and per-run settings."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = "0.1.0"

ROOT = Path(os.environ.get("LITREVIEW_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
DB_PATH = DATA_DIR / "library.sqlite"

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

# USD per million tokens: input, output, 5-minute cache write, 1-hour cache write, cache read.
# From platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-15.
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    OPUS: (4.00, 20.00, 5.00, 8.00, 0.20),
    SONNET: (2.00, 10.00, 2.50, 4.00, 0.10),
    HAIKU: (0.10, 0.50, 0.125, 0.20, 0.01),
}
# Haiku 5.5 bills a whole request at a higher rate once its prompt (input plus cache reads and
# writes) is over 100,000 tokens.
LONG_PROMPT_PRICES: dict[str, tuple[int, tuple[float, float, float, float, float]]] = {
    HAIKU: (100_000, (0.50, 2.50, 0.625, 1.00, 0.05)),
}
# Haiku has no server-side refusal fallback, so a refused Haiku call is retried once on this model.
REFUSAL_RESCUE: dict[str, str] = {HAIKU: SONNET}
# Fallback models and anything unlisted are costed at this conservative rate.
UNKNOWN_PRICE = (5.00, 25.00, 6.25, 10.00, 0.50)
BATCH_DISCOUNT = 0.5


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from ROOT/.env without overriding variables already set.

    Blank values are skipped, so an unfilled ANTHROPIC_API_KEY= doesn't hide an
    `ant auth login` profile behind an empty key."""
    path = path or ROOT / ".env"
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)


def tessdata() -> str | None:
    """The folder with Tesseract's English data (eng.traineddata), which OCR needs: TESSDATA_PREFIX,
    else data/tessdata. None when neither has it, and scans without a text layer can't be read."""
    for folder in (os.environ.get("TESSDATA_PREFIX"), DATA_DIR / "tessdata"):
        if folder and (Path(folder) / "eng.traineddata").is_file():
            return str(folder)
    return None


def unpaywall_email() -> str | None:
    """The address sent to Unpaywall, which asks for one with every request: UNPAYWALL_EMAIL, else an
    email address in LITREVIEW_CONTACT. None without either, and Unpaywall isn't asked."""
    if email := os.environ.get("UNPAYWALL_EMAIL", "").strip():
        return email
    found = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", os.environ.get("LITREVIEW_CONTACT", ""))
    return found.group() if found else None


def user_agent() -> str:
    """User-Agent for ADS and arXiv. Contact details are added only if LITREVIEW_CONTACT is set."""
    contact = os.environ.get("LITREVIEW_CONTACT", "").strip()
    return f"litreview/{VERSION}" + (f" ({contact})" if contact else "")


@dataclass
class Settings:
    question: str
    since: int | None = None
    until: int | None = None
    refereed_only: bool = False
    max_papers: int = 40
    screen_top: int = 200
    expand: bool = True
    expand_seeds: int = 10
    expand_max: int = 100
    abstracts_only: bool = False
    use_batch: bool = True
    cost_cap: float = 10.0
    models: dict[str, str] = field(
        default_factory=lambda: {
            "plan": OPUS,
            # Relevance scores from abstracts: a classification job, at about 1/40 of Opus's price.
            "screen": HAIKU,
            "extract": SONNET,
            # The claim checks reuse the synthesis model and effort so they hit its prompt cache.
            "synthesize": OPUS,
        }
    )
    # Set explicitly because the API defaults differ: medium on Opus 5.5 and Haiku 5.5, high on Sonnet 5.5.
    efforts: dict[str, str] = field(
        default_factory=lambda: {
            "plan": "high",
            "screen": "medium",
            "extract": "medium",
            "synthesize": "high",
        }
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)
