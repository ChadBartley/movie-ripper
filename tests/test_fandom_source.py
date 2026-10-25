"""Tests for the Fandom transcript adapter.

Covers:
  - slug-candidate generation (the "spongebob" / "theoffice" / "simpsons"
    cases, plus the "the"-prefix dedup behavior)
  - rendered-HTML stripping (infobox, edit links, navboxes, refs gone;
    dialogue preserved)
  - fetch_episode response paths: happy, missingtitle, generic API error,
    maxlag with retry, persistent maxlag
  - _verify_via_titles: ≥3 hits accepts, <3 rejects, empty/short input
    rejects, ConnectionError-during-find_show is treated as a miss
  - find_show selects the first slug that verifies and skips others.
  - italic-narration stripping (Bluey) without losing indented dialogue
    (Courage), and the list=search fallback for titles the wiki spells
    differently from TMDB.

Tests stub the network at ``_get`` (returns response body strings) so no
HTTP actually fires — same convention as test_sources.py.
"""

import json
import urllib.parse
from pathlib import Path

import pytest
import requests
from transcripts.cache import Episode, normalize_text
from transcripts.sources import FandomSource, ShowRef
from transcripts.sources._fandom_text import (
    _pick_search_hit,
    _slug_candidates,
    _strip_rendered_html,
)

FIX = Path(__file__).parent / "fixtures"


# --- slug candidates ----------------------------------------------------


def test_slug_candidates_spongebob():
    """First candidate is the short form fans actually use as the wiki host."""
    cands = _slug_candidates("SpongeBob SquarePants")
    assert cands[0] == "spongebob"
    assert "spongebobsquarepants" in cands
    assert "spongebob-squarepants" in cands


def test_slug_candidates_drops_the_prefix_first():
    """'The Office' wiki is at theoffice.fandom.com — both forms are tried."""
    cands = _slug_candidates("The Office")
    assert "office" in cands
    assert "theoffice" in cands
    # Without "the" comes first: matches the more common Fandom convention
    assert cands.index("office") < cands.index("theoffice")


def test_slug_candidates_simpsons_short_form_first():
    cands = _slug_candidates("The Simpsons")
    assert cands[0] == "simpsons"


def test_slug_candidates_dedups():
    """Single-word names produce identical first/concat forms — only one entry."""
    cands = _slug_candidates("Futurama")
    assert cands.count("futurama") == 1


def test_slug_candidates_strips_punctuation():
    """Apostrophes and other punctuation collapse, no slugs leak special chars."""
    cands = _slug_candidates("Bob's Burgers")
    assert all("'" not in c for c in cands)
    assert "bobs" in cands


def test_slug_candidates_empty_input():
    assert _slug_candidates("") == []
    assert _slug_candidates("   ") == []


# --- HTML stripping -----------------------------------------------------


def test_strip_rendered_html_drops_infobox_and_chrome():
    html = (FIX / "fandom_episode.html").read_text()
    text = _strip_rendered_html(html)
    # Dialogue preserved
    assert "I'm ready!" in text
    assert "He's hired!" in text
    assert "[The episode opens" in text
    # Infobox metadata stripped
    assert "Air date" not in text
    assert "May 1, 1999" not in text
    # Edit links stripped
    assert "[edit]" not in text
    # Navbox stripped
    assert "navigation" not in text.lower() or "Bikini Bottom" not in text  # weak smoke
    assert "References elided" not in text


def test_strip_rendered_html_handles_empty():
    assert _strip_rendered_html("") == ""


def test_strip_rendered_html_drops_italic_narration_blocks():
    """Bluey wiki: scene description sits in fully-italic indented blocks."""
    html = (
        '<p><b>Bandit:</b> "Come on, start!"</p>'
        "<dl><dd><i>The barbecue finally lights with a <a href='/x'>whoosh</a>.</i></dd></dl>"
        '<p><b>Bluey:</b> "Whoosh!"</p>'
    )
    text = _strip_rendered_html(html)
    assert "Come on, start!" in text
    assert "Whoosh!" in text
    assert "barbecue finally lights" not in text


def test_strip_rendered_html_keeps_indented_dialogue():
    """Courage wiki indents real dialogue with the same <dl><dd> markup."""
    html = (
        "<dl><dd><b>Muriel:</b> I can't wait for that wee lad Fred.</dd></dl>"
        "<dl><dd><i>(sighs)</i> Courage, you're so silly.</dd></dl>"
    )
    text = _strip_rendered_html(html)
    assert "I can't wait for that wee lad Fred." in text
    assert "Courage, you're so silly." in text


def test_strip_rendered_html_drops_italic_narration_paragraphs():
    """Bluey 'Show and Tell': narration in a plain fully-italic paragraph."""
    html = (
        "<p><b>Bandit</b>: Yep. 16 Pelton Drive.</p>"
        "<p><i>Bandit hangs up and pumps his arm.</i></p>"
        "<p><b>Bandit</b>: Oos!</p>"
    )
    text = _strip_rendered_html(html)
    assert "16 Pelton Drive." in text
    assert "pumps his arm" not in text


def test_strip_rendered_html_rejoins_colon_outside_bold_speaker():
    """'<b>Bandit</b>: Hi' must come out as 'Bandit: Hi' so normalize_text
    strips the speaker; split across lines, the name leaks into the text."""
    html = "<p><b><a href='/wiki/Bandit_Heeler'>Bandit</a></b>: It's in good nick?</p>"
    text = _strip_rendered_html(html)
    assert "Bandit: It's in good nick?" in text
    assert normalize_text(text) == "it's in good nick"


# --- search-hit selection -----------------------------------------------


def test_pick_search_hit_matches_respaced_title():
    """Regression (Bluey): TMDB 'Wagonride' is 'Wagon Ride' on the wiki."""
    hits = ["Wagon Ride", "Wagon Ride/Gallery", "Wagon Ride/Script", "Bus Stop Dog"]
    assert _pick_search_hit("Wagonride", hits) == "Wagon Ride/Script"


def test_pick_search_hit_matches_ampersand_spelled_out():
    """Regression (Bluey): TMDB 'Show & Tell' is 'Show and Tell' on the wiki."""
    hits = ["Show and Tell", "Show and Tell/Script", "Surfer (Show and Tell)"]
    assert _pick_search_hit("Show & Tell", hits) == "Show and Tell/Script"


def test_pick_search_hit_rejects_similar_sibling_episode():
    """A higher-ranked transcript for a different episode must not win."""
    hits = ["Christmas Cricket/Script", "Cricket/Script"]
    assert _pick_search_hit("Cricket", hits) == "Cricket/Script"


def test_pick_search_hit_returns_none_without_transcript_subpage():
    hits = ["Wagon Ride", "Wagon Ride/Gallery"]
    assert _pick_search_hit("Wagonride", hits) is None


# --- fetch_episode ------------------------------------------------------


def _fake_get_factory(responses: dict[str, str]):
    """Build a `_get` stub that returns canned bodies for known URLs.

    Unknown URLs return "" (the standard "miss" sentinel).
    """

    def fake(url):
        return responses.get(url, "")

    return fake


def test_fetch_episode_happy_path(monkeypatch):
    src = FandomSource()
    html_body = (FIX / "fandom_episode.html").read_text()
    api_response = {"parse": {"title": "Help Wanted/transcript", "text": {"*": html_body}}}
    fake_url = (
        "https://spongebob.fandom.com/api.php?"
        "action=parse&page=Help_Wanted%2Ftranscript&format=json"
        "&prop=text&redirects=1&maxlag=5"
    )
    monkeypatch.setattr(src, "_get", _fake_get_factory({fake_url: json.dumps(api_response)}))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is not None
    assert "I'm ready!" in text
    assert "Air date" not in text  # infobox stripped


def test_fetch_episode_missing_title_returns_none(monkeypatch):
    src = FandomSource()
    api_response = {
        "error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}
    }
    monkeypatch.setattr(src, "_get", lambda url: json.dumps(api_response))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Definitely Not An Episode",
    )
    assert text is None


def test_fetch_episode_generic_error_returns_none(monkeypatch):
    src = FandomSource()
    api_response = {"error": {"code": "ratelimited", "info": "Slow down."}}
    monkeypatch.setattr(src, "_get", lambda url: json.dumps(api_response))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is None


def test_fetch_episode_handles_non_json_response(monkeypatch):
    src = FandomSource()
    monkeypatch.setattr(src, "_get", lambda url: "<html>cloudflare challenge</html>")
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is None


def test_fetch_episode_returns_none_on_empty_body(monkeypatch):
    src = FandomSource()
    monkeypatch.setattr(src, "_get", lambda url: "")
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is None


def test_fetch_episode_maxlag_retries_then_succeeds(monkeypatch):
    """maxlag on first call, success on second — verifies the retry path."""
    src = FandomSource()
    html_body = "<p>SpongeBob: hi</p>"
    success_response = json.dumps({"parse": {"text": {"*": html_body}}})
    maxlag_response = json.dumps(
        {"error": {"code": "maxlag", "info": "Waiting for replication.", "lag": 6.5}}
    )
    sleeps: list[float] = []
    monkeypatch.setattr("transcripts.sources.fandom.time.sleep", sleeps.append)

    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return maxlag_response if calls["n"] == 1 else success_response

    monkeypatch.setattr(src, "_get", fake_get)
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is not None
    assert "SpongeBob: hi" in text
    assert calls["n"] == 2
    # We slept lag+1 seconds (capped at 10) before retrying.
    assert sleeps and 7.0 <= sleeps[0] <= 10.0


def test_fetch_episode_maxlag_persistent_returns_none(monkeypatch):
    src = FandomSource()
    maxlag_response = json.dumps({"error": {"code": "maxlag", "info": "Waiting.", "lag": 6.5}})
    monkeypatch.setattr("transcripts.sources.fandom.time.sleep", lambda _: None)
    monkeypatch.setattr(src, "_get", lambda url: maxlag_response)
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is None


# --- find_show / verification ------------------------------------------


def _verify_response(found: int, total: int) -> str:
    """Build an action=query JSON response with `found` real pages, rest missing."""
    pages = {}
    for i in range(found):
        pages[str(100 + i)] = {"pageid": 100 + i, "title": f"Real_Episode_{i}/transcript"}
    for i in range(total - found):
        pages[str(-(i + 1))] = {"missing": "", "title": f"Missing_Episode_{i}/transcript"}
    return json.dumps({"query": {"pages": pages}})


def _spongebob_episodes(n: int = 5) -> list[Episode]:
    titles = [
        "Help Wanted",
        "Reef Blower",
        "Tea at the Treedome",
        "Bubblestand",
        "Ripped Pants",
    ]
    return [Episode(season=1, number=i + 1, title=titles[i]) for i in range(n)]


def test_find_show_accepts_first_verified_slug(monkeypatch):
    """First slug that returns ≥3 hits is accepted; later candidates not probed."""
    src = FandomSource()
    calls: list[str] = []

    def fake_get(url):
        calls.append(url)
        # spongebob.fandom.com hits 5/5; downstream candidates would also hit
        # but we should never reach them.
        if "spongebob.fandom.com" in url and "action=query" in url:
            return _verify_response(found=5, total=5)
        return ""

    monkeypatch.setattr(src, "_get", fake_get)
    ref = src.find_show(
        "SpongeBob SquarePants",
        year=1999,
        tmdb_id=387,
        expected_episodes=_spongebob_episodes(),
    )
    assert ref is not None
    assert ref.url == "https://spongebob.fandom.com"
    assert ref.source == "fandom"
    # Only the first slug should have been probed.
    assert all("spongebob.fandom.com" in c for c in calls)


def test_find_show_skips_low_hit_slugs(monkeypatch):
    """A wiki that exists but has <3 transcript pages is not the right wiki."""
    src = FandomSource()

    def fake_get(url):
        if "spongebob.fandom.com" in url:
            # Wiki responds but only one transcript page exists — not our show
            return _verify_response(found=1, total=5)
        if "spongebobsquarepants.fandom.com" in url:
            return _verify_response(found=4, total=5)
        return ""

    monkeypatch.setattr(src, "_get", fake_get)
    ref = src.find_show(
        "SpongeBob SquarePants",
        year=1999,
        tmdb_id=387,
        expected_episodes=_spongebob_episodes(),
    )
    assert ref is not None
    assert ref.url == "https://spongebobsquarepants.fandom.com"


def test_find_show_returns_none_when_no_slug_verifies(monkeypatch):
    src = FandomSource()
    monkeypatch.setattr(src, "_get", lambda url: _verify_response(found=0, total=5))
    ref = src.find_show(
        "Nonexistent Show",
        year=2020,
        tmdb_id=999,
        expected_episodes=_spongebob_episodes(),
    )
    assert ref is None


def test_find_show_handles_connection_error(monkeypatch):
    """DNS misses for unknown wiki hosts are common; treat as miss, try next slug."""
    src = FandomSource()
    seen_hosts: list[str] = []

    def fake_get(url):
        if "office.fandom.com" in url and "theoffice" not in url:
            seen_hosts.append("office")
            raise requests.exceptions.ConnectionError("dns miss")
        if "theoffice.fandom.com" in url:
            seen_hosts.append("theoffice")
            return _verify_response(found=4, total=5)
        return ""

    monkeypatch.setattr(src, "_get", fake_get)
    eps = [Episode(season=1, number=i, title=f"E{i}") for i in range(1, 6)]
    ref = src.find_show("The Office", year=2005, tmdb_id=2316, expected_episodes=eps)
    assert ref is not None
    assert ref.url == "https://theoffice.fandom.com"
    assert "office" in seen_hosts and "theoffice" in seen_hosts


def test_find_show_too_few_episodes_returns_none(monkeypatch):
    """With <3 expected episodes we can't verify; skip rather than guess."""
    src = FandomSource()
    called = {"n": 0}

    def fake_get(url):
        called["n"] += 1
        return ""

    monkeypatch.setattr(src, "_get", fake_get)
    ref = src.find_show(
        "Tiny Show",
        year=2020,
        tmdb_id=1,
        expected_episodes=[Episode(season=1, number=1, title="Pilot")],
    )
    assert ref is None
    assert called["n"] == 0  # never even tried


# --- transcript-subpage casing ------------------------------------------


def _wiki_stub(existing_pages: set[str]):
    """Simulate a MediaWiki api.php faithfully enough for casing tests.

    Answers ``action=query`` by marking each requested title missing unless
    it is in ``existing_pages`` (compared with spaces, exact case — the same
    case-sensitivity real MediaWiki applies past a title's first character).
    Answers ``action=parse`` with a canned body or ``missingtitle``.
    """

    def fake_get(url):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if params.get("list") == ["search"]:
            # Crude stand-in for full-text search: a page matches when it
            # contains the query, spacing ignored.
            q = params["srsearch"][0].lower().replace(" ", "")
            hits = [{"title": p} for p in sorted(existing_pages) if q in p.lower().replace(" ", "")]
            return json.dumps({"query": {"search": hits}})
        if params.get("action") == ["query"]:
            pages = {}
            for i, t in enumerate(params["titles"][0].split("|")):
                norm = t.replace("_", " ")
                if norm in existing_pages:
                    pages[str(100 + i)] = {"pageid": 100 + i, "title": norm}
                else:
                    pages[str(-(i + 1))] = {"missing": "", "title": norm}
            return json.dumps({"query": {"pages": pages}})
        if params.get("action") == ["parse"]:
            page = params["page"][0].replace("_", " ")
            if page in existing_pages:
                return json.dumps({"parse": {"text": {"*": f"<p>dialogue of {page}</p>"}}})
            return json.dumps({"error": {"code": "missingtitle", "info": "no such page"}})
        return ""

    return fake_get


def test_find_show_accepts_wiki_with_capital_t_transcript_pages(monkeypatch):
    """Regression (Courage wiki): transcripts live at <Title>/Transcript.

    MediaWiki subpage names are case-sensitive past the first character, so
    querying only the lowercase /transcript variant reports every page
    missing and rejects the correct wiki.
    """
    src = FandomSource()
    eps = _spongebob_episodes()
    monkeypatch.setattr(src, "_get", _wiki_stub({f"{ep.title}/Transcript" for ep in eps}))
    ref = src.find_show(
        "SpongeBob SquarePants",
        year=1999,
        tmdb_id=387,
        expected_episodes=eps,
    )
    assert ref is not None
    assert ref.url == "https://spongebob.fandom.com"


def test_fetch_episode_falls_back_to_capital_t_transcript(monkeypatch):
    """When /transcript is missing, /Transcript is tried before giving up."""
    src = FandomSource()
    monkeypatch.setattr(src, "_get", _wiki_stub({"Help Wanted/Transcript"}))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title="Help Wanted",
    )
    assert text is not None
    assert "dialogue of Help Wanted/Transcript" in text


def test_fetch_episode_reuses_discovered_casing(monkeypatch):
    """Once a wiki's casing is known, later episodes ask for it directly.

    Without this, a capital-T wiki costs a wasted missingtitle roundtrip
    per episode (102 extra seconds for a Courage-sized show at 1 req/sec).
    """
    src = FandomSource()
    calls: list[str] = []
    stub = _wiki_stub({"Help Wanted/Transcript", "Reef Blower/Transcript"})

    def counting_get(url):
        calls.append(url)
        return stub(url)

    monkeypatch.setattr(src, "_get", counting_get)
    show = ShowRef(source="fandom", url="https://spongebob.fandom.com")
    assert src.fetch_episode(show, season=1, number=1, title="Help Wanted") is not None
    calls.clear()
    assert src.fetch_episode(show, season=1, number=2, title="Reef Blower") is not None
    assert len(calls) == 1
    assert "page=Reef_Blower%2FTranscript" in calls[0]


def test_fetch_episode_falls_back_to_episode_disambiguated_page(monkeypatch):
    """Regression (Courage wiki): when an episode title collides with a
    character page ('Freaky Fred' the barber), the wiki keeps the transcript
    at '<Title> (episode)/Transcript'. The plain page misses; the
    disambiguated variant must be tried before giving up."""
    src = FandomSource()
    monkeypatch.setattr(src, "_get", _wiki_stub({"Freaky Fred (episode)/Transcript"}))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://courage.fandom.com"),
        season=1,
        number=8,
        title="Freaky Fred",
    )
    assert text is not None
    assert "dialogue of Freaky Fred (episode)/Transcript" in text


def test_find_show_accepts_wiki_with_script_pages(monkeypatch):
    """Regression (Bluey wiki): transcripts live at <Title>/Script.

    Probing only /transcript and /Transcript reported every sample missing,
    so the wiki was rejected and 120 of 153 episodes went uncached.
    """
    src = FandomSource()
    eps = _spongebob_episodes()
    monkeypatch.setattr(src, "_get", _wiki_stub({f"{ep.title}/Script" for ep in eps}))
    ref = src.find_show(
        "SpongeBob SquarePants",
        year=1999,
        tmdb_id=387,
        expected_episodes=eps,
    )
    assert ref is not None
    assert ref.url == "https://spongebob.fandom.com"


def test_fetch_episode_falls_back_to_capital_e_episode_disambiguation(monkeypatch):
    """Regression (Bluey wiki): 'Bingo' is the character, so the episode
    transcript lives at 'Bingo (Episode)/Script' with a capital E."""
    src = FandomSource()
    monkeypatch.setattr(src, "_get", _wiki_stub({"Bingo (Episode)/Script"}))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://bluey.fandom.com"),
        season=2,
        number=9,
        title="Bingo",
    )
    assert text is not None
    assert "dialogue of Bingo (Episode)/Script" in text


def test_fetch_episode_falls_back_to_wiki_search(monkeypatch):
    """Regression (Bluey wiki): TMDB 'Wagonride' has no page or redirect;
    the transcript is at 'Wagon Ride/Script' and only search finds it."""
    src = FandomSource()
    monkeypatch.setattr(src, "_get", _wiki_stub({"Wagon Ride/Script"}))
    text = src.fetch_episode(
        ShowRef(source="fandom", url="https://bluey.fandom.com"),
        season=1,
        number=24,
        title="Wagonride",
    )
    assert text is not None
    assert "dialogue of Wagon Ride/Script" in text


@pytest.mark.parametrize(
    "title,expected_page",
    [
        ("Help Wanted", "Help_Wanted"),
        ("Tea at the Treedome", "Tea_at_the_Treedome"),
    ],
)
def test_fetch_episode_constructs_expected_page_name(monkeypatch, title, expected_page):
    """Spaces become underscores; punctuation passes through to URL encoding."""
    src = FandomSource()
    seen_urls: list[str] = []

    def fake_get(url):
        seen_urls.append(url)
        return json.dumps({"parse": {"text": {"*": "<p>x</p>"}}})

    monkeypatch.setattr(src, "_get", fake_get)
    src.fetch_episode(
        ShowRef(source="fandom", url="https://spongebob.fandom.com"),
        season=1,
        number=1,
        title=title,
    )
    assert seen_urls
    # urllib.parse.urlencode percent-encodes the slash; underscores stay as-is.
    assert f"page={expected_page}%2Ftranscript" in seen_urls[0]
