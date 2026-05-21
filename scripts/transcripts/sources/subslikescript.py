"""subslikescript.com adapter.

Two-step show resolution: try a slug-direct URL first (cheap, works for
shows with no disambiguator suffix), fall back to /search?q=<name>.
Always verifies the resolved index against the expected TMDB episode
list before accepting — the search endpoint can return a same-named
sibling (UK Office vs. US Office).
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

_EPISODE_HREF_RE = re.compile(r"/series/[^/]+/season-(\d+)/episode-(\d+)-(.+)$")
_SEARCH_HREF_RE = re.compile(r"^/series/[A-Za-z0-9_]+-\d+$")


def parse_subslikescript_index(html: str, base_url: str) -> list[tuple[int, int, str, str]]:
    """Return list of (season, number, title, url) tuples."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[int, int, str, str]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _EPISODE_HREF_RE.search(href)
        if not m:
            continue
        season, number = int(m.group(1)), int(m.group(2))
        title = m.group(3).replace("_", " ")
        url = urllib.parse.urljoin(base_url, href)
        key = (season, number)
        if key in seen:
            continue
        seen.add(key)
        out.append((season, number, title, url))
    return out


def parse_subslikescript_search(html: str) -> list[tuple[str, str]]:
    """Return [(href, link_text), ...] of /series/<Slug>-<id> hits in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _SEARCH_HREF_RE.match(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((href, a.get_text(strip=True)))
    return out


def parse_subslikescript_episode(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("div", class_="full-script")
    return el.get_text(separator="\n").strip() if el else ""


class SubsLikeScriptSource:
    name = "subslikescript"

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
        # Fast path: slug-direct works for shows whose canonical URL has no ID
        # suffix (uncommon, but cheap to probe). Always verified.
        slug = name.replace(" ", "_")
        candidate = f"https://subslikescript.com/series/{slug}"
        html = self._get(candidate)
        if html:
            index = parse_subslikescript_index(html, candidate)
            if index and verify_show_match(index, expected_episodes):
                self._index_cache[candidate] = index
                return ShowRef(source=self.name, url=candidate)

        # Search path: /search?q=<name> returns 0..N /series/<Slug>-<id> hits.
        # Year-filter the link text, then verify each candidate's index in order.
        query = urllib.parse.quote_plus(name)
        search_url = f"https://subslikescript.com/search?q={query}"
        search_html = self._get(search_url)
        if not search_html:
            return None
        hits = parse_subslikescript_search(search_html)
        if year is not None:
            hits = [(h, t) for (h, t) in hits if str(year) in t] or hits
        for href, _text in hits:
            url = urllib.parse.urljoin("https://subslikescript.com/", href)
            idx_html = self._get(url)
            if not idx_html:
                continue
            index = parse_subslikescript_index(idx_html, url)
            if not index:
                continue
            if not verify_show_match(index, expected_episodes):
                logger.info(
                    "subslikescript: rejected %s — title overlap below threshold for tmdb_id=%s",
                    href,
                    tmdb_id,
                )
                continue
            self._index_cache[url] = index
            return ShowRef(source=self.name, url=url)
        return None

    def fetch_episode(self, show: ShowRef, season: int, number: int, title: str) -> str | None:
        # Cache the index per show within this session.
        if show.url not in self._index_cache:
            html = self._get(show.url)
            self._index_cache[show.url] = parse_subslikescript_index(html, show.url) if html else []
        index = self._index_cache[show.url]
        # (season, number) match is only trusted if the slug's title agrees with
        # the title we asked for. SLS sometimes splits hour-long episodes into
        # two slots (e.g. The Office S4 "Money" → episode-7 + episode-8), which
        # offsets every later episode's number relative to TMDB's single-slot
        # numbering. Verifying the title catches that drift before we cache the
        # wrong transcript under the right filename.
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
        if not html:
            return None
        return parse_subslikescript_episode(html) or None
