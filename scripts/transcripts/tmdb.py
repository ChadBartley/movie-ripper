"""TMDB show resolver — convert (name | tmdb_id) → canonical episode list.

Used by fetch_transcripts.py to seed the cache. Episode titles returned
here are authoritative; sources.py looks up by title-fuzz against these
because subslikescript and Springfield disagree on S/E numbering for some
shows (DVD-vs-broadcast order, splits, specials).
"""

from __future__ import annotations

import logging
import time

import requests

from transcripts import USER_AGENT
from transcripts.cache import Episode

logger = logging.getLogger(__name__)

POLITE_DELAY_SEC = 1.0
TMDB_BASE = "https://api.themoviedb.org/3"


def _parse_tmdb_show_search(payload: dict, year_hint: int | None) -> tuple[int, str, int] | None:
    """Return (tmdb_id, name, year) or None."""
    results = payload.get("results") or []
    if not results:
        return None
    if year_hint is not None:
        for r in results:
            air = (r.get("first_air_date") or "")[:4]
            if air.isdigit() and int(air) == year_hint:
                return r["id"], r["name"], int(air)
    # Default: highest popularity (results from TMDB are pre-sorted).
    r = results[0]
    air = (r.get("first_air_date") or "")[:4]
    return r["id"], r["name"], int(air) if air.isdigit() else 0


def resolve_show_via_tmdb(
    api_key: str, show_name: str | None, year_hint: int | None, tmdb_id: int | None
) -> tuple[int, str, int, list[Episode]]:
    """Resolve a show identity + episode list. Raises if not found.

    Two HTTP calls minimum: search (only if no ``tmdb_id``) → ``/tv/{id}`` for
    canonical name+year+season-count → one ``/tv/{id}/season/{s}`` per season.
    """
    sess = requests.Session()
    headers = {"User-Agent": USER_AGENT}

    # Step 1: resolve tmdb_id from show_name if needed.
    if tmdb_id is None:
        if not show_name:
            raise ValueError("Need either show_name or tmdb_id")
        time.sleep(POLITE_DELAY_SEC)
        r = sess.get(
            f"{TMDB_BASE}/search/tv",
            headers=headers,
            params={"api_key": api_key, "query": show_name},
            timeout=30,
        )
        r.raise_for_status()
        hit = _parse_tmdb_show_search(r.json(), year_hint)
        if not hit:
            raise RuntimeError(f"TMDB has no series matching {show_name!r}")
        tmdb_id, _, _ = hit  # discard search-result name/year; trust /tv/{id} below.

    # Step 2: one /tv/{id} call — canonical name, year, season count.
    time.sleep(POLITE_DELAY_SEC)
    r = sess.get(
        f"{TMDB_BASE}/tv/{tmdb_id}", headers=headers, params={"api_key": api_key}, timeout=30
    )
    r.raise_for_status()
    d = r.json()
    show_name = d["name"]
    air = (d.get("first_air_date") or "")[:4]
    year = int(air) if air.isdigit() else 0
    n_seasons = d["number_of_seasons"]

    # Step 3: per-season episode fetch.
    episodes: list[Episode] = []
    for s in range(1, n_seasons + 1):
        time.sleep(POLITE_DELAY_SEC)
        r = sess.get(
            f"{TMDB_BASE}/tv/{tmdb_id}/season/{s}",
            headers=headers,
            params={"api_key": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning(
                "TMDB season fetch failed for tmdb_id=%s season=%s: HTTP %s",
                tmdb_id,
                s,
                r.status_code,
            )
            continue
        for ep in r.json().get("episodes", []):
            episodes.append(
                Episode(
                    season=s,
                    number=ep["episode_number"],
                    title=ep.get("name") or f"Episode {ep['episode_number']}",
                )
            )
    return tmdb_id, show_name, year, episodes
