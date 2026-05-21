"""Source ladder orchestrator.

Walks a list of ``Source`` adapters in order, calling ``find_show`` then
``fetch_episode`` on each, and returning the first hit. Caches the
``ShowRef`` per source across episodes so the show-resolution cost is
paid once.
"""

from __future__ import annotations

from transcripts.cache import Episode
from transcripts.sources._base import ShowRef, Source


def fetch_episode_with_fallback(
    sources: list[Source],
    show_name: str,
    year: int | None,
    tmdb_id: int | None,
    season: int,
    number: int,
    title: str,
    expected_episodes: list[Episode],
    show_ref_cache: dict[str, ShowRef | None] | None = None,
) -> tuple[str | None, str | None]:
    """Walk sources in order; return (text, source_name) at first hit.

    `show_ref_cache` is keyed by source.name and lets the caller reuse
    `find_show` results across many episodes.
    """
    for src in sources:
        if show_ref_cache is not None and src.name in show_ref_cache:
            ref = show_ref_cache[src.name]
        else:
            ref = src.find_show(show_name, year, tmdb_id, expected_episodes)
            if show_ref_cache is not None:
                show_ref_cache[src.name] = ref
        if ref is None:
            continue
        text = src.fetch_episode(ref, season, number, title)
        if text:
            return text, src.name
    return None, None
