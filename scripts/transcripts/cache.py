"""On-disk transcript cache + ASR/transcript text normalization.

The cache layout is documented in the plan header. Normalization is shared
between cache-write time (transcripts from scrapers) and transcribe time
(Whisper output) so the matcher sees apples-to-apples text. Apostrophes are
preserved because they sit inside words; removing them harms fuzzy match.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Speaker labels at line start: "WORD:" or "Word Word:" up to ~30 chars
_SPEAKER_RE = re.compile(r"^\s*[A-Z][A-Za-z .'\-]{0,30}:\s*", re.MULTILINE)
# Stage directions in [..] or (..)
_BRACKETED_RE = re.compile(r"[\[(][^\]\)]*[\])]")
# SRT timestamp line: "00:01:23,456 --> 00:01:25,789"
_SRT_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
# SRT cue index — a digits-only line
_SRT_INDEX_RE = re.compile(r"^\s*\d+\s*$", re.MULTILINE)
# Punctuation we drop entirely (keep apostrophes — they sit inside words)
_PUNCT_RE = re.compile(r"[.,!?;:\"\-—–…/\\#*_<>{}&]")
# Unicode quote translation: curly quotes → ASCII so cache + ASR match byte-for-byte
_QUOTE_TRANSLATION = str.maketrans(
    {
        "‘": "'",  # left single
        "’": "'",  # right single
        "‚": "'",  # low-9
        "‛": "'",  # high-reversed-9
        "“": '"',  # left double
        "”": '"',  # right double
        "„": '"',  # low-9 double
        "‟": '"',  # high-reversed-9 double
    }
)


def normalize_text(text: str) -> str:
    """Lowercase, strip speaker labels, stage directions, SRT cruft, punctuation,
    HTML entities; collapse whitespace. Apostrophes preserved."""
    if not text:
        return ""
    text = html.unescape(text)
    text = text.translate(_QUOTE_TRANSLATION)
    text = _SRT_TIMESTAMP_RE.sub(" ", text)
    text = _SRT_INDEX_RE.sub(" ", text)
    text = _BRACKETED_RE.sub(" ", text)
    text = _SPEAKER_RE.sub("", text)
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class Episode:
    season: int
    number: int
    title: str


_FS_HOSTILE_RE = re.compile(r'[/\\:*?"<>|]')


def _sanitize_for_filename(s: str, max_len: int = 80) -> str:
    s = _FS_HOSTILE_RE.sub("", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:max_len] or "untitled"


class Cache:
    """File-on-disk transcript cache.

    Layout:
        <root>/<tmdb_id>/show.json
        <root>/<tmdb_id>/sNN/eNN-<sanitized_title>.txt
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _show_dir(self, tmdb_id: int) -> Path:
        return self.root / str(tmdb_id)

    def _episode_path(self, tmdb_id: int, season: int, number: int, title: str) -> Path:
        d = self._show_dir(tmdb_id) / f"s{season:02d}"
        return d / f"e{number:02d}-{_sanitize_for_filename(title)}.txt"

    def write_show_meta(self, tmdb_id: int, name: str, year: int, episodes: list[Episode]) -> None:
        d = self._show_dir(tmdb_id)
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "tmdb_id": tmdb_id,
            "name": name,
            "year": year,
            "episodes": [asdict(e) for e in episodes],
        }
        (d / "show.json").write_text(json.dumps(meta, indent=2))

    def read_show_meta(self, tmdb_id: int) -> dict | None:
        p = self._show_dir(tmdb_id) / "show.json"
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def write_episode(self, tmdb_id: int, season: int, number: int, title: str, text: str) -> None:
        p = self._episode_path(tmdb_id, season, number, title)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def read_episode(self, tmdb_id: int, season: int, number: int, title: str) -> str | None:
        p = self._episode_path(tmdb_id, season, number, title)
        return p.read_text() if p.exists() else None

    def has_episode(self, tmdb_id: int, season: int, number: int, title: str) -> bool:
        return self._episode_path(tmdb_id, season, number, title).exists()

    def iter_all_episodes(self, tmdb_id: int) -> Iterator[tuple[Episode, str]]:
        meta = self.read_show_meta(tmdb_id)
        if not meta:
            return
        for e in meta["episodes"]:
            ep = Episode(season=e["season"], number=e["number"], title=e["title"])
            text = self.read_episode(tmdb_id, ep.season, ep.number, ep.title)
            if text is not None:
                yield ep, text
