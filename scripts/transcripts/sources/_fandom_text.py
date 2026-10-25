"""Pure text helpers for the Fandom adapter: naming conventions and HTML cleanup.

Everything here is network-free so it can be unit-tested against fixtures.
``fandom.py`` owns the MediaWiki API client and imports these. The naming
tables (subpage suffixes, disambiguators) encode per-wiki conventions seen in
the wild; the HTML helpers decide what text reaches the matcher, which scores
Whisper output against it, so anything that isn't spoken dialogue costs score.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz

# Transcript-subpage names seen in the wild, most common first. Wikis are
# internally consistent but disagree with each other (spongebob: lowercase;
# courage: capital-T; bluey: Script).
_TRANSCRIPT_SUFFIXES = ("transcript", "Transcript", "Script")

# Disambiguators wikis append when an episode title collides with another
# page (a character of the same name). Casing varies per wiki and sometimes
# within one: courage uses "(episode)", bluey mostly "(Episode)".
_EPISODE_DISAMBIGUATORS = (" (episode)", " (Episode)")

# Minimum fuzz.ratio between a TMDB title and a search hit's page title, both
# reduced to lowercase alphanumerics. Same bar the Springfield and
# SubsLikeScript adapters use for title verification. Rejects near-miss
# siblings: "Cricket" vs. "Christmas Cricket" scores 61.
SEARCH_TITLE_MIN_SCORE = 80

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

_DISAMBIGUATOR_RE = re.compile(r"\s*\(episode\)\s*$", re.IGNORECASE)
# A speaker label whose colon sits outside the bold (``<b>Bandit</b>: Hi``)
# renders as "Bandit\n: Hi" under get_text("\n"); rejoining it lets
# normalize_text's line-start speaker regex strip the name.
_DETACHED_COLON_RE = re.compile(r"\n[ \t]*:")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


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


def _is_italic_narration(block: Tag) -> bool:
    """True iff the block's only non-blank child is a single <i> element."""
    kids = [c for c in block.contents if not (isinstance(c, NavigableString) and not c.strip())]
    return len(kids) == 1 and isinstance(kids[0], Tag) and kids[0].name == "i"


def _strip_rendered_html(html: str) -> str:
    """Strip wiki chrome, prose narration and detached speaker colons.

    Chrome is the infobox, edit links, navboxes and references. Narration is
    a paragraph or indented block that is entirely italic
    (``:''The episode opens…''``), the Bluey wiki's convention for scene
    description. It runs to roughly half of each script; against
    Springfield's dialogue-only text for the same episodes, stripping it
    lifts mean match score from 58 to 92. Only fully-italic blocks go:
    Courage indents real dialogue (``<dd><b>Muriel:</b> …</dd>``) with the
    same markup. Side effect: a fully-italic sung or off-screen line is
    dropped too.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in _STRIP_SELECTORS:
        for el in soup.select(selector):
            el.decompose()
    for block in soup.find_all(["p", "dd"]):
        if _is_italic_narration(block):
            block.decompose()
    text = soup.get_text(separator="\n").strip()
    return _DETACHED_COLON_RE.sub(":", text)


def _comparable_title(title: str) -> str:
    """Lowercase alphanumerics only, '&' spelled out, '(episode)' dropped."""
    title = _DISAMBIGUATOR_RE.sub("", title).lower().replace("&", "and")
    return _NON_ALNUM_RE.sub("", title)


def _pick_search_hit(title: str, hit_titles: list[str]) -> str | None:
    """Return the first search hit that is a transcript subpage for ``title``.

    Hits are full-text search results in rank order. A hit qualifies when it
    ends in a known transcript suffix and its base page title matches
    ``title`` at ≥SEARCH_TITLE_MIN_SCORE after reducing both to alphanumerics
    (absorbs "Wagonride" vs. "Wagon Ride" and "Show & Tell" vs. "Show and
    Tell"). Returns None when nothing qualifies — search is fuzzy and its
    top hits are often unrelated pages.
    """
    want = _comparable_title(title)
    if not want:
        return None
    for hit in hit_titles:
        base, _, suffix = hit.rpartition("/")
        if not base or suffix not in _TRANSCRIPT_SUFFIXES:
            continue
        if fuzz.ratio(want, _comparable_title(base)) >= SEARCH_TITLE_MIN_SCORE:
            return hit
    return None
