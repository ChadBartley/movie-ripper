"""Shared types and HTTP helper for transcript source adapters.

Each adapter in this package implements the ``Source`` Protocol; the
shared ``_polite_get`` enforces 1 req/sec, identifying User-Agent, and a
single 5xx retry. ``verify_show_match`` is the post-resolution sanity
check that protects against same-name-different-show false positives
(e.g. UK vs. US Office). Sits on its own so each adapter module imports
only what it needs without pulling in siblings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from statistics import median
from typing import Protocol

import requests
from rapidfuzz import fuzz

from transcripts import USER_AGENT
from transcripts.cache import Episode

logger = logging.getLogger(__name__)

POLITE_DELAY_SEC = 1.0

# Threshold tuned against UK-vs-US Office: UK index titles score median ≈ 15
# against US episode titles via partial_ratio; correct matches score ≥90.
SHOW_MATCH_MIN_SCORE = 70
SHOW_MATCH_MIN_OVERLAP = 3


@dataclass(frozen=True)
class ShowRef:
    """Opaque per-source pointer to a resolved show."""

    source: str
    url: str


class Source(Protocol):
    name: str

    def find_show(
        self,
        name: str,
        year: int | None,
        tmdb_id: int | None,
        expected_episodes: list[Episode],
    ) -> ShowRef | None: ...

    def fetch_episode(self, show: ShowRef, season: int, number: int, title: str) -> str | None: ...


def _polite_get(url: str, session: requests.Session | None = None) -> str:
    """GET with rate limiting, real UA, single retry on 5xx.

    Returns response text on 200. Returns "" on 404 (no such resource) and
    on persistent 5xx (server broken, not our fault) — sources treat both
    as "try the next source." Raises on 4xx other than 404; those indicate
    a client-side bug worth surfacing.
    """
    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}
    for attempt in (1, 2):
        time.sleep(POLITE_DELAY_SEC)
        r = sess.get(url, headers=headers, timeout=30)
        if r.status_code == 404:
            logger.debug("404 %s", url)
            return ""
        if r.status_code >= 500:
            if attempt == 1:
                logger.warning("HTTP %d on %s — retrying once", r.status_code, url)
                continue
            logger.warning("HTTP %d on %s — giving up, treating as miss", r.status_code, url)
            return ""
        r.raise_for_status()
        return r.text
    return ""


def verify_show_match(
    candidate_index: list[tuple[int, int, str, str]],
    expected_episodes: list[Episode],
) -> bool:
    """Return True iff the candidate index plausibly belongs to the expected show.

    Compares titles for overlapping (season, episode) pairs via rapidfuzz
    partial_ratio (tolerates leading "N. " prefixes from Springfield, etc.).
    Requires at least SHOW_MATCH_MIN_OVERLAP overlapping pairs and a median
    score of at least SHOW_MATCH_MIN_SCORE. Both empty inputs return False.
    """
    if not candidate_index or not expected_episodes:
        return False
    expected_by_key = {(e.season, e.number): e.title for e in expected_episodes}
    scores: list[float] = []
    for season, number, title, _url in candidate_index:
        want = expected_by_key.get((season, number))
        if want is None:
            continue
        scores.append(fuzz.partial_ratio(title.lower(), want.lower()))
    if len(scores) < SHOW_MATCH_MIN_OVERLAP:
        return False
    return median(scores) >= SHOW_MATCH_MIN_SCORE
