"""Transcript source adapters and the fallback orchestrator.

Each adapter implements the ``Source`` Protocol: ``find_show`` resolves a
``ShowRef`` from a name + optional (year, tmdb_id), then ``fetch_episode``
returns the raw transcript text for a single episode (or None on miss).
Adapters live in their own submodules; this ``__init__`` re-exports the
public API so call sites import from ``transcripts.sources`` without
caring about layout.

Polite scraping is enforced by ``_base._polite_get``: 1 req/sec, real
User-Agent, single retry on 5xx. 404 is treated as a quiet miss, not an
error. Fandom additionally honors MediaWiki ``maxlag``.
"""

from __future__ import annotations

from transcripts.sources._base import (
    POLITE_DELAY_SEC,
    SHOW_MATCH_MIN_OVERLAP,
    SHOW_MATCH_MIN_SCORE,
    ShowRef,
    Source,
    verify_show_match,
)
from transcripts.sources._orchestrator import fetch_episode_with_fallback
from transcripts.sources.fandom import FandomSource
from transcripts.sources.springfield import (
    SpringfieldSource,
    parse_springfield_episode,
    parse_springfield_index,
)
from transcripts.sources.subslikescript import (
    SubsLikeScriptSource,
    parse_subslikescript_episode,
    parse_subslikescript_index,
    parse_subslikescript_search,
)

# Registry of source name → constructor. Used by the CLI's --sources flag
# and by anything else that wants to instantiate a ladder by name.
SOURCE_REGISTRY: dict[str, type] = {
    SubsLikeScriptSource.name: SubsLikeScriptSource,
    SpringfieldSource.name: SpringfieldSource,
    FandomSource.name: FandomSource,
}

# Default ladder order: site-specific scrapers first (cheap, predictable),
# Fandom last (auto-discovery + 5-title verify costs an HTTP roundtrip).
DEFAULT_SOURCES: tuple[str, ...] = (
    SubsLikeScriptSource.name,
    SpringfieldSource.name,
    FandomSource.name,
)

__all__ = [
    "DEFAULT_SOURCES",
    "FandomSource",
    "POLITE_DELAY_SEC",
    "SHOW_MATCH_MIN_OVERLAP",
    "SHOW_MATCH_MIN_SCORE",
    "SOURCE_REGISTRY",
    "ShowRef",
    "Source",
    "SpringfieldSource",
    "SubsLikeScriptSource",
    "fetch_episode_with_fallback",
    "parse_springfield_episode",
    "parse_springfield_index",
    "parse_subslikescript_episode",
    "parse_subslikescript_index",
    "parse_subslikescript_search",
    "verify_show_match",
]
