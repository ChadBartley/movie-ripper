"""springfieldspringfield.co.uk adapter.

Probes ``<slug>-us``, ``<slug>``, ``<slug>-uk``, then ``<slug>-<year>``
(when a year is known) in that order — most TMDB shows we care about are
US-origin, and verification makes ordering non-load-bearing for
correctness, just for HTTP economy. The year suffix is how Springfield
disambiguates reboots and same-name shows (e.g.
``courage-the-cowardly-dog-1999``). Uses the shared ``verify_show_match``
to reject same-name siblings.
"""

from __future__ import annotations

import logging
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from transcripts.cache import Episode
from transcripts.sources._base import ShowRef, _polite_get, verify_show_match

logger = logging.getLogger(__name__)

_EPISODE_HREF_RE = re.compile(r"view_episode_scripts\.php\?tv-show=([^&]+)&episode=s(\d+)e(\d+)")
_TITLE_AFTER_DASH_RE = re.compile(r"-\s*(.+)$")


def _slugify_show_for_springfield(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)  # drop "(US)" etc
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def parse_springfield_index(html: str, base_url: str) -> list[tuple[int, int, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[int, int, str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _EPISODE_HREF_RE.search(href)
        if not m:
            continue
        season, number = int(m.group(2)), int(m.group(3))
        text = a.get_text(strip=True)
        title_m = _TITLE_AFTER_DASH_RE.search(text)
        title = title_m.group(1).strip() if title_m else text
        url = urllib.parse.urljoin(base_url, href)
        key = (season, number)
        if key in seen:
            continue
        seen.add(key)
        out.append((season, number, title, url))
    return out


def parse_springfield_episode(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("div", class_="scrolling-script-container")
    if not el:
        # Older variant: plain text inside body
        el = soup.find("body")
    return el.get_text(separator="\n").strip() if el else ""


class SpringfieldSource:
    name = "springfield"
    BASE = "https://www.springfieldspringfield.co.uk"

    def __init__(self):
        self._session = requests.Session()
        self._index_cache: dict[str, list[tuple[int, int, str, str]]] = {}

    def _get(self, url: str) -> str:
        return _polite_get(url, self._session)

    def find_show(
        self,
        name: str,
        year: int | None,
        tmdb_id: int | None,
        expected_episodes: list[Episode],
    ) -> ShowRef | None:
        base_slug = _slugify_show_for_springfield(name)
        # -us first: most TMDB shows we care about are US-origin. Verification
        # makes order non-load-bearing for correctness, but ordering well saves
        # HTTP calls in the common case.
        candidate_slugs = [base_slug + "-us", base_slug, base_slug + "-uk"]
        if year is not None:
            # Springfield's disambiguator for reboots / same-name shows is a
            # first-air-year suffix (courage-the-cowardly-dog-1999).
            candidate_slugs.append(f"{base_slug}-{year}")
        for candidate_slug in candidate_slugs:
            url = f"{self.BASE}/episode_scripts.php?tv-show={candidate_slug}"
            html = self._get(url)
            if not html:
                continue
            index = parse_springfield_index(html, self.BASE)
            if not index:
                continue
            if not verify_show_match(index, expected_episodes):
                logger.info(
                    "springfield: rejected %s — title overlap below threshold for tmdb_id=%s",
                    candidate_slug,
                    tmdb_id,
                )
                continue
            self._index_cache[url] = index
            return ShowRef(source=self.name, url=url)
        return None

    def fetch_episode(self, show: ShowRef, season: int, number: int, title: str) -> str | None:
        if show.url not in self._index_cache:
            html = self._get(show.url)
            self._index_cache[show.url] = parse_springfield_index(html, self.BASE) if html else []
        index = self._index_cache[show.url]
        # Title-verify (s,n) hits and fall back to title fuzz within the season,
        # matching SubsLikeScriptSource — protects against aired-vs-DVD numbering
        # drift on shows with hour-long episodes split across two slots.
        url = None
        for s, n, t, u in index:
            if s == season and n == number:
                if fuzz.ratio(title.lower(), t.lower()) >= 80:
                    url = u
                break
        if url is None:
            best, best_score = None, 0
            for s, _n, t, u in index:
                if s != season:
                    continue
                score = fuzz.ratio(title.lower(), t.lower())
                if score > best_score:
                    best_score = score
                    best = u
            if best and best_score >= 80:
                url = best
        if url is None:
            return None
        html = self._get(url)
        return parse_springfield_episode(html) if html else None
