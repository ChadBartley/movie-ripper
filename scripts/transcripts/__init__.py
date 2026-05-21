"""Shared utilities for transcript fetching and dialogue-based episode ID.

Submodules are imported directly — ``from transcripts.cache import Cache``,
``from transcripts.matching import score_track`` — both from CLIs (with
``scripts/`` on sys.path) and tests (see tests/conftest.py). This file
re-exports only ``USER_AGENT`` (no third-party deps); keeping
``transcripts.sources`` (and its ``requests`` + ``bs4`` deps) out of the
import graph for callers that don't need them. Layout is split by
responsibility per .claude/rules/proactive-file-splitting.md.
"""

from __future__ import annotations

import os


def _build_user_agent() -> str:
    """Polite User-Agent for outbound scraping.

    Read once at import time. Set ``SCRAPER_CONTACT`` in the environment
    (a URL or email the scraped sites can use to reach you) to identify
    yourself; recommended by most public scraping ToS. Defaults to a
    bare ``movie-ripper/1.0`` when unset so a fresh clone never
    impersonates the upstream author.
    """
    contact = os.environ.get("SCRAPER_CONTACT", "").strip()
    if contact:
        return f"movie-ripper/1.0 (+{contact}) requests"
    return "movie-ripper/1.0 requests"


USER_AGENT = _build_user_agent()
