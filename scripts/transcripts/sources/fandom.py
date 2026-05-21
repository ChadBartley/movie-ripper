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
       ``<Title>/transcript`` pages on that wiki.
    3. Accept the first wiki where ≥3 of those pages are present.

Episode fetch:
    ``action=parse&page=<Title>/transcript&prop=text`` returns rendered
    HTML; we strip the infobox and editor chrome and emit text.

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
import re
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

from transcripts.cache import Episode
from transcripts.sources._base import SHOW_MATCH_MIN_OVERLAP, ShowRef, _polite_get

logger = logging.getLogger(__name__)

VERIFY_SAMPLE_SIZE = 5
MAXLAG_SECONDS = 5
MAXLAG_RETRY_SLEEP_CAP = 10.0

# Selectors for chrome we strip from the rendered transcript HTML. The
# infobox and edit-section spans aren't dialogue; navboxes and references
# are footers; mw-empty-elt is wiki-template scaffolding.
_STRIP_SELECTORS = (
    "aside.portable-infobox",
    "span.mw-editsection",
    ".navbox",
    ".reference",
    ".navigation-not-searchable",
    ".mw-empty-elt",
    "table.toc",
)


def _slug_candidates(name: str) -> list[str]:
    """Return ranked Fandom subdomain slug guesses for a show name.

    Order: first significant word, all-words concatenated (sans "the"),
    all-words concatenated (with "the" if originally present), and the
    dashed form. Duplicates are removed while preserving order so the
    cheapest correct guess wins.
    """
    s = name.lower()
    # Elide apostrophes outright ("Bob's Burgers" → "bobs burgers", which
    # matches Fandom's bobs-burgers.fandom.com convention). Other
    # punctuation collapses to a separator instead.
    s = s.replace("’", "").replace("'", "")
    no_punct = re.sub(r"[^a-z0-9 ]+", " ", s)
    no_punct = re.sub(r"\s+", " ", no_punct).strip()
    if not no_punct:
        return []
    no_the = re.sub(r"^the\s+", "", no_punct)
    words = no_the.split()

    candidates: list[str] = []

    def _add(c: str) -> None:
        if c and c not in candidates:
            candidates.append(c)

    if words:
        _add(words[0])
    _add(no_the.replace(" ", ""))
    _add(no_punct.replace(" ", ""))
    _add(re.sub(r"\s+", "-", no_the).strip("-"))
    return candidates


def _title_to_page(title: str) -> str:
    """Convert a TMDB episode title to its MediaWiki page name (no encoding).

    MediaWiki normalizes spaces to underscores; we do the same up-front so
    the API's ``redirects=1`` doesn't have to do extra work and so logs
    show what we asked for verbatim.
    """
    return title.replace(" ", "_")


def _strip_rendered_html(html: str) -> str:
    """Strip wiki chrome (infobox, edit links, navboxes) and return text."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in _STRIP_SELECTORS:
        for el in soup.select(selector):
            el.decompose()
    return soup.get_text(separator="\n").strip()


class FandomSource:
    name = "fandom"

    def __init__(self):
        self._session = requests.Session()

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
        """Return True iff ≥SHOW_MATCH_MIN_OVERLAP sample transcripts exist on host."""
        sample = expected_episodes[:VERIFY_SAMPLE_SIZE]
        if len(sample) < SHOW_MATCH_MIN_OVERLAP:
            return False
        titles = "|".join(f"{_title_to_page(ep.title)}/transcript" for ep in sample)
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
        found = sum(1 for p in pages.values() if "missing" not in p)
        return found >= SHOW_MATCH_MIN_OVERLAP

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

    def fetch_episode(self, show: ShowRef, season: int, number: int, title: str) -> str | None:
        """Fetch a transcript by title. season/number unused — Fandom keys on title."""
        page = f"{_title_to_page(title)}/transcript"
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
            else:
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
        return _strip_rendered_html(html) or None
