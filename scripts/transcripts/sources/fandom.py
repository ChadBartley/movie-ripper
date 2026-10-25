"""Fandom (wikia) wiki adapter via the MediaWiki API.

Each show wiki lives on its own subdomain (``spongebob.fandom.com``,
``theoffice.fandom.com``, ``simpsons.fandom.com``). We talk to it
through ``/api.php`` rather than scraping rendered pages — the rendered
pages sit behind Cloudflare's JS challenge, but the API is exempt and
returns clean JSON.

Show resolution:
    1. Generate ranked slug candidates from the show name (e.g.
       "SpongeBob SquarePants" → ["spongebob", "spongebobsquarepants",
       "spongebob-squarepants"]).
    2. For each candidate, batch-query ``action=query&titles=...``
       asking whether the first ~5 expected episode titles exist as
       ``<Title>/transcript``, ``<Title>/Transcript`` or ``<Title>/Script``
       pages on that wiki. The subpage name varies per wiki (SpongeBob:
       lowercase, Courage: capital-T, Bluey: Script) and MediaWiki subpage
       names are case-sensitive past the title's first character, so every
       variant must be probed.
    3. Accept the first wiki where ≥3 of those pages are present, and
       remember which subpage name the wiki uses so episode fetches don't
       pay a missingtitle roundtrip each.

Episode fetch:
    ``action=parse&page=<Title>/transcript&prop=text`` returns rendered
    HTML; we strip the infobox, editor chrome and italic narration blocks
    and emit text (see ``_fandom_text``). When every guessed page name
    misses, one ``list=search`` query catches titles the wiki spells
    differently from TMDB ("Wagonride" vs. "Wagon Ride").

Politeness:
    - 1 req/sec via the shared ``_polite_get`` helper.
    - ``maxlag=5`` on every API call (canonical MediaWiki signal that
      asks the server to refuse our request when replication lag is
      high). On a maxlag error we sleep ``lag+1`` seconds and retry
      once before giving up.
    - Identifying ``User-Agent`` (set ``SCRAPER_CONTACT`` in ``.env``).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Iterator

import requests

from transcripts.cache import Episode
from transcripts.sources._base import SHOW_MATCH_MIN_OVERLAP, ShowRef, _polite_get
from transcripts.sources._fandom_text import (
    _EPISODE_DISAMBIGUATORS,
    _TRANSCRIPT_SUFFIXES,
    _pick_search_hit,
    _slug_candidates,
    _strip_rendered_html,
    _title_to_page,
)

logger = logging.getLogger(__name__)

VERIFY_SAMPLE_SIZE = 5
MAXLAG_SECONDS = 5
MAXLAG_RETRY_SLEEP_CAP = 10.0
SEARCH_RESULT_LIMIT = 10


class FandomSource:
    name = "fandom"

    def __init__(self):
        self._session = requests.Session()
        # host → transcript-subpage casing discovered for that wiki. Primed by
        # _verify_via_titles and updated whenever a fetch succeeds.
        self._suffix_by_host: dict[str, str] = {}

    def _get(self, url: str) -> str:
        return _polite_get(url, self._session)

    def _api_call(self, host: str, params: dict[str, str]) -> dict | None:
        """Issue an api.php call, parse JSON, handle maxlag with one retry.

        Returns ``None`` for transport-level misses (404, persistent 5xx,
        non-JSON body) and for persistent maxlag errors. Returns the
        parsed JSON dict (which may itself contain ``error``) otherwise —
        callers inspect ``data["error"]["code"]`` for missingtitle, etc.
        """
        url = f"{host}/api.php?{urllib.parse.urlencode(params)}"
        for attempt in (1, 2):
            text = self._get(url)
            if not text:
                return None
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                logger.warning("fandom: non-JSON response from %s: %s", url, e)
                return None
            err = data.get("error")
            if err and err.get("code") == "maxlag":
                if attempt == 1:
                    lag = err.get("lag", MAXLAG_SECONDS)
                    try:
                        sleep_for = min(float(lag) + 1.0, MAXLAG_RETRY_SLEEP_CAP)
                    except (TypeError, ValueError):
                        sleep_for = MAXLAG_SECONDS
                    logger.info(
                        "fandom: maxlag %ss on %s — backing off %.1fs",
                        lag,
                        host,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    continue
                logger.warning("fandom: maxlag persisted on %s — giving up", host)
                return None
            return data
        return None

    def _verify_via_titles(self, host: str, expected_episodes: list[Episode]) -> bool:
        """Return True iff ≥SHOW_MATCH_MIN_OVERLAP sample transcripts exist on host.

        Probes every subpage name in _TRANSCRIPT_SUFFIXES in one batch query; an
        episode counts as found if any name exists. On success, records the
        wiki's dominant subpage name in _suffix_by_host for fetch_episode.
        """
        sample = expected_episodes[:VERIFY_SAMPLE_SIZE]
        if len(sample) < SHOW_MATCH_MIN_OVERLAP:
            return False
        titles = "|".join(
            f"{_title_to_page(ep.title)}/{suffix}"
            for ep in sample
            for suffix in _TRANSCRIPT_SUFFIXES
        )
        data = self._api_call(
            host,
            {
                "action": "query",
                "titles": titles,
                "format": "json",
                "redirects": "1",
                "maxlag": str(MAXLAG_SECONDS),
            },
        )
        if data is None or "query" not in data:
            return False
        pages = data["query"].get("pages", {})
        # MediaWiki marks non-existent pages with a "missing" key; a page
        # with a real pageid lacks it.
        found_by_suffix: dict[str, set[str]] = {s: set() for s in _TRANSCRIPT_SUFFIXES}
        for p in pages.values():
            if "missing" in p:
                continue
            base, _, suffix = p.get("title", "").rpartition("/")
            if base and suffix in found_by_suffix:
                found_by_suffix[suffix].add(base)
        found = set().union(*found_by_suffix.values())
        if len(found) < SHOW_MATCH_MIN_OVERLAP:
            return False
        self._suffix_by_host[host] = max(
            _TRANSCRIPT_SUFFIXES, key=lambda s: len(found_by_suffix[s])
        )
        return True

    def find_show(
        self,
        name: str,
        year: int | None,
        tmdb_id: int | None,
        expected_episodes: list[Episode],
    ) -> ShowRef | None:
        if len(expected_episodes) < SHOW_MATCH_MIN_OVERLAP:
            logger.info(
                "fandom: skipping resolution for tmdb_id=%s — too few expected episodes",
                tmdb_id,
            )
            return None
        for slug in _slug_candidates(name):
            host = f"https://{slug}.fandom.com"
            try:
                if self._verify_via_titles(host, expected_episodes):
                    return ShowRef(source=self.name, url=host)
            except requests.exceptions.ConnectionError as e:
                # DNS miss for a slug that doesn't have a wiki — common, expected.
                logger.debug("fandom: %s unreachable: %s", host, e)
                continue
            logger.info(
                "fandom: %s did not verify for tmdb_id=%s (sample titles missing)",
                host,
                tmdb_id,
            )
        return None

    def _search_transcript_page(self, host: str, title: str) -> str | None:
        """Full-text search the wiki for ``title``'s transcript subpage.

        Last resort for titles TMDB spells differently from the wiki, where
        no redirect bridges the two. Returns the page title to parse, or None
        when search fails or no hit passes ``_pick_search_hit``.
        """
        data = self._api_call(
            host,
            {
                "action": "query",
                "list": "search",
                "srsearch": title,
                "srlimit": str(SEARCH_RESULT_LIMIT),
                "format": "json",
                "maxlag": str(MAXLAG_SECONDS),
            },
        )
        if data is None:
            return None
        if "query" not in data:
            logger.warning("fandom: search for %r on %s failed: %s", title, host, data.get("error"))
            return None
        hits = [h.get("title", "") for h in data["query"].get("search", [])]
        page = _pick_search_hit(title, hits)
        logger.info("fandom: search for %r on %s → %s (hits: %s)", title, host, page, hits)
        return page

    def _candidate_pages(self, host: str, title: str) -> Iterator[str]:
        """Yield transcript page names to try, cheapest-likely first.

        The search fallback is lazy: it only fires once every guessed name
        has missed.
        """
        preferred = self._suffix_by_host.get(host, _TRANSCRIPT_SUFFIXES[0])
        suffixes = (preferred, *(s for s in _TRANSCRIPT_SUFFIXES if s != preferred))
        page_bases = tuple(_title_to_page(title + d) for d in ("", *_EPISODE_DISAMBIGUATORS))
        for suffix in suffixes:
            for base in page_bases:
                yield f"{base}/{suffix}"
        hit = self._search_transcript_page(host, title)
        if hit:
            yield hit

    def fetch_episode(self, show: ShowRef, season: int, number: int, title: str) -> str | None:
        """Fetch a transcript by title. season/number unused — Fandom keys on title.

        Tries the wiki's known transcript-subpage name first (discovered at
        verify time), falling back to the others on missingtitle so a wiki
        with mixed conventions still resolves. Within each subpage name, also
        tries the '<Title> (episode)' / '<Title> (Episode)' disambiguations
        wikis use when an episode title collides with another page (e.g. a
        character of the same name). If all of those miss, one wiki search
        runs. A full miss costs 10 requests (~10s at the polite rate).
        """
        for page in self._candidate_pages(show.url, title):
            data = self._api_call(
                show.url,
                {
                    "action": "parse",
                    "page": page,
                    "format": "json",
                    "prop": "text",
                    "redirects": "1",
                    "maxlag": str(MAXLAG_SECONDS),
                },
            )
            if data is None:
                return None
            err = data.get("error")
            if err:
                code = err.get("code")
                if code == "missingtitle":
                    logger.info("fandom: no transcript page %s on %s", page, show.url)
                    continue
                logger.warning(
                    "fandom: API error %s for %s on %s: %s",
                    code,
                    page,
                    show.url,
                    err.get("info"),
                )
                return None
            html = data.get("parse", {}).get("text", {}).get("*", "")
            if not html:
                return None
            self._suffix_by_host[show.url] = page.rpartition("/")[2]
            return _strip_rendered_html(html) or None
        return None
