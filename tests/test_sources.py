from pathlib import Path

from transcripts.cache import Episode
from transcripts.sources import (
    ShowRef,
    SpringfieldSource,
    SubsLikeScriptSource,
    fetch_episode_with_fallback,
    parse_springfield_episode,
    parse_springfield_index,
    parse_subslikescript_episode,
    parse_subslikescript_index,
    parse_subslikescript_search,
    verify_show_match,
)
from transcripts.tmdb import _parse_tmdb_show_search

FIX = Path(__file__).parent / "fixtures"


def test_parse_subslikescript_index():
    html = (FIX / "subslikescript_index.html").read_text()
    eps = parse_subslikescript_index(html, base_url="https://subslikescript.com")
    # Returns list of (season, number, title, url)
    assert (
        1,
        1,
        "Pilot",
        "https://subslikescript.com/series/The_Office-386676/season-1/episode-1-Pilot",
    ) in eps
    assert (
        5,
        15,
        "Stress Relief",
        "https://subslikescript.com/series/The_Office-386676/season-5/episode-15-Stress_Relief",
    ) in eps
    assert len(eps) == 3


def test_parse_subslikescript_episode():
    html = (FIX / "subslikescript_episode.html").read_text()
    text = parse_subslikescript_episode(html)
    assert "workplace safety" in text.lower()
    assert "demonstration" in text.lower()
    # Speaker labels included in raw extract; normalize_text strips them later.
    assert "Michael" in text or "michael" in text


def test_subslikescript_source_takes_slug_direct_path(monkeypatch):
    """When the slug-direct guess hits an index page that verifies, find_show
    returns immediately without consulting the search endpoint."""
    src = SubsLikeScriptSource()
    search_calls = {"n": 0}

    def fake_get(url):
        if url.startswith("https://subslikescript.com/search"):
            search_calls["n"] += 1
            return ""
        if url == "https://subslikescript.com/series/The_Office":
            return (FIX / "subslikescript_index.html").read_text()
        return ""

    monkeypatch.setattr(src, "_get", fake_get)
    expected = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=5, number=15, title="Stress Relief"),
    ]
    show_ref = src.find_show("The Office", year=2005, tmdb_id=2316, expected_episodes=expected)
    assert show_ref is not None
    assert show_ref.url == "https://subslikescript.com/series/The_Office"
    assert search_calls["n"] == 0


def test_subslikescript_rejects_sn_match_when_title_disagrees(monkeypatch):
    """Regression: SLS splits hour-long episodes into two slots, so its
    (season, number) numbering drifts from TMDB's. When TMDB asks for
    S4E9 'Dinner Party' but the source's S4E9 is titled 'Local Ad', we
    must not return the Local Ad transcript under the Dinner Party slot.
    """
    src = SubsLikeScriptSource()
    src._index_cache["http://example/show"] = [
        (4, 9, "Local Ad", "http://example/s4e9-local-ad"),
        (4, 13, "Dinner Party", "http://example/s4e13-dinner-party"),
    ]
    fetched: list[str] = []

    def fake_get(url):
        fetched.append(url)
        return "<div class='full-script'>real dinner party text</div>"

    monkeypatch.setattr(src, "_get", fake_get)
    text = src.fetch_episode(
        ShowRef(source="subslikescript", url="http://example/show"),
        season=4,
        number=9,
        title="Dinner Party",
    )
    assert text == "real dinner party text"
    assert fetched == ["http://example/s4e13-dinner-party"]


def test_springfield_rejects_sn_match_when_title_disagrees(monkeypatch):
    """Same regression as the SLS case, applied to Springfield."""
    src = SpringfieldSource()
    src._index_cache["http://sf/show"] = [
        (4, 9, "Local Ad", "http://sf/s4e9"),
        (4, 13, "Dinner Party", "http://sf/s4e13"),
    ]
    fetched: list[str] = []

    def fake_get(url):
        fetched.append(url)
        return "<body>real dinner party text</body>"

    monkeypatch.setattr(src, "_get", fake_get)
    text = src.fetch_episode(
        ShowRef(source="springfield", url="http://sf/show"),
        season=4,
        number=9,
        title="Dinner Party",
    )
    assert text == "real dinner party text"
    assert fetched == ["http://sf/s4e13"]


def test_parse_springfield_index():
    html = (FIX / "springfield_index.html").read_text()
    eps = parse_springfield_index(html, base_url="https://www.springfieldspringfield.co.uk/")
    # (season, number, title, url)
    assert any(s == 1 and n == 1 and "pilot" in t.lower() for s, n, t, _ in eps)
    assert any(s == 5 and n == 15 and "stress" in t.lower() for s, n, t, _ in eps)


def test_parse_springfield_episode():
    html = (FIX / "springfield_episode.html").read_text()
    text = parse_springfield_episode(html)
    assert "workplace safety" in text.lower()


def test_springfield_source(monkeypatch):
    src = SpringfieldSource()
    fake_pages = {
        "https://www.springfieldspringfield.co.uk/episode_scripts.php?tv-show=the-office-us": (
            FIX / "springfield_index.html"
        ).read_text(),
        "https://www.springfieldspringfield.co.uk/view_episode_scripts.php?tv-show=the-office-us&episode=s05e15": (
            FIX / "springfield_episode.html"
        ).read_text(),
    }
    monkeypatch.setattr(src, "_get", lambda url: fake_pages.get(url, ""))
    expected = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=5, number=15, title="Stress Relief"),
    ]
    show_ref = src.find_show("The Office (US)", year=2005, tmdb_id=2316, expected_episodes=expected)
    assert show_ref is not None
    text = src.fetch_episode(show_ref, season=5, number=15, title="Stress Relief")
    assert text is not None
    assert "workplace safety" in text.lower()


def test_springfield_rejects_uk_office_when_expected_is_us(monkeypatch):
    """Bare slug 'the-office' returns the UK Office's index. With US-Office
    episodes as expected, find_show must reject it and fall through to
    'the-office-us', which verifies cleanly.
    """
    src = SpringfieldSource()
    fake_pages = {
        "https://www.springfieldspringfield.co.uk/episode_scripts.php?tv-show=the-office-us": (
            FIX / "springfield_index.html"
        ).read_text(),
        "https://www.springfieldspringfield.co.uk/episode_scripts.php?tv-show=the-office": (
            FIX / "springfield_uk_index.html"
        ).read_text(),
        "https://www.springfieldspringfield.co.uk/episode_scripts.php?tv-show=the-office-uk": "",
    }
    monkeypatch.setattr(src, "_get", lambda url: fake_pages.get(url, ""))
    expected_us = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=1, number=3, title="Health Care"),
        Episode(season=1, number=4, title="The Alliance"),
        Episode(season=5, number=15, title="Stress Relief"),
    ]
    show_ref = src.find_show("The Office", year=2005, tmdb_id=2316, expected_episodes=expected_us)
    assert show_ref is not None
    assert "tv-show=the-office-us" in show_ref.url


def test_springfield_returns_none_when_no_slug_verifies(monkeypatch):
    src = SpringfieldSource()
    uk_html = (FIX / "springfield_uk_index.html").read_text()
    monkeypatch.setattr(src, "_get", lambda url: uk_html)
    expected_us = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=1, number=3, title="Health Care"),
        Episode(season=1, number=4, title="The Alliance"),
        Episode(season=5, number=15, title="Stress Relief"),
    ]
    show_ref = src.find_show("The Office", year=2005, tmdb_id=2316, expected_episodes=expected_us)
    assert show_ref is None


class _FakeSource:
    def __init__(self, name, show=None, episode_text=None):
        self.name = name
        self._show = show
        self._episode_text = episode_text
        self.find_calls = 0
        self.fetch_calls = 0

    def find_show(self, name, year, tmdb_id, expected_episodes):
        self.find_calls += 1
        return self._show

    def fetch_episode(self, show, season, number, title):
        self.fetch_calls += 1
        return self._episode_text


def test_fallback_uses_first_hit():
    a = _FakeSource("a", show=ShowRef("a", "x"), episode_text="from-a")
    b = _FakeSource("b", show=ShowRef("b", "y"), episode_text="from-b")
    text, src_name = fetch_episode_with_fallback(
        [a, b], "Show", year=2020, tmdb_id=1, season=1, number=1, title="T", expected_episodes=[]
    )
    assert text == "from-a"
    assert src_name == "a"
    assert a.fetch_calls == 1
    assert b.fetch_calls == 0


def test_fallback_skips_source_with_no_show():
    a = _FakeSource("a", show=None)
    b = _FakeSource("b", show=ShowRef("b", "y"), episode_text="from-b")
    text, src_name = fetch_episode_with_fallback(
        [a, b], "Show", year=2020, tmdb_id=1, season=1, number=1, title="T", expected_episodes=[]
    )
    assert text == "from-b"
    assert src_name == "b"


def test_fallback_skips_source_with_no_episode():
    a = _FakeSource("a", show=ShowRef("a", "x"), episode_text=None)
    b = _FakeSource("b", show=ShowRef("b", "y"), episode_text="from-b")
    text, src_name = fetch_episode_with_fallback(
        [a, b], "Show", year=2020, tmdb_id=1, season=1, number=1, title="T", expected_episodes=[]
    )
    assert text == "from-b"
    assert src_name == "b"


def test_fallback_returns_none_when_all_fail():
    a = _FakeSource("a", show=None)
    b = _FakeSource("b", show=None)
    text, src_name = fetch_episode_with_fallback(
        [a, b], "Show", year=2020, tmdb_id=1, season=1, number=1, title="T", expected_episodes=[]
    )
    assert text is None
    assert src_name is None


# --- TMDB show search ----------------------------------------------------


def test_parse_tmdb_show_search_falls_back_to_first_when_no_year_hint():
    """Without a year hint we trust TMDB's pre-sort and take results[0],
    even when multiple results exist."""
    payload = {
        "results": [
            {"id": 2316, "name": "The Office", "first_air_date": "2005-03-24", "popularity": 200},
            {"id": 1, "name": "The Office", "first_air_date": "2001-07-09", "popularity": 100},
        ]
    }
    show = _parse_tmdb_show_search(payload, year_hint=None)
    assert show == (2316, "The Office", 2005)


def test_parse_tmdb_show_search_disambiguates_by_year():
    payload = {
        "results": [
            {
                "id": 1,
                "name": "The Office",
                "first_air_date": "2001-07-09",
                "popularity": 100,
            },  # UK
            {
                "id": 2316,
                "name": "The Office",
                "first_air_date": "2005-03-24",
                "popularity": 300,
            },  # US
        ]
    }
    show = _parse_tmdb_show_search(payload, year_hint=2001)
    assert show[0] == 1


def test_parse_tmdb_show_search_returns_none_when_empty():
    assert _parse_tmdb_show_search({"results": []}, year_hint=None) is None


def test_verify_show_match_accepts_matching_index():
    expected = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=1, number=3, title="Health Care"),
        Episode(season=1, number=4, title="The Alliance"),
    ]
    candidate = [
        (1, 1, "1. Pilot", "u1"),
        (1, 2, "2. Diversity Day", "u2"),
        (1, 3, "3. Health Care", "u3"),
        (1, 4, "4. The Alliance", "u4"),
    ]
    assert verify_show_match(candidate, expected) is True


def test_verify_show_match_rejects_uk_office_against_us_episodes():
    expected_us = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=1, number=3, title="Health Care"),
        Episode(season=1, number=4, title="The Alliance"),
        Episode(season=1, number=5, title="Basketball"),
        Episode(season=1, number=6, title="Hot Girl"),
    ]
    candidate_uk = [
        (1, 1, "1. Downsize", "u1"),
        (1, 2, "2. Work experience", "u2"),
        (1, 3, "3. The quiz", "u3"),
        (1, 4, "4. Training", "u4"),
        (1, 5, "5. New girl", "u5"),
        (1, 6, "6. Judgement", "u6"),
    ]
    assert verify_show_match(candidate_uk, expected_us) is False


def test_verify_show_match_rejects_when_overlap_too_small():
    expected = [Episode(season=1, number=i, title=f"E{i}") for i in range(1, 11)]
    candidate = [(1, 1, "E1", "u1")]
    assert verify_show_match(candidate, expected) is False


def test_verify_show_match_rejects_empty_candidate():
    expected = [Episode(season=1, number=1, title="Pilot")]
    assert verify_show_match([], expected) is False


def test_verify_show_match_rejects_empty_expected():
    candidate = [(1, 1, "Pilot", "u")]
    assert verify_show_match(candidate, []) is False


def test_parse_subslikescript_search():
    html = (FIX / "subslikescript_search.html").read_text()
    hits = parse_subslikescript_search(html)
    assert ("/series/The_Office-290978", "The Office (2001–2003)") in hits
    assert ("/series/The_Office-386676", "The Office (2005–2013)") in hits


def test_subslikescript_uses_search_when_slug_direct_misses(monkeypatch):
    """When slug-direct 404s, find_show queries /search?q=<name> and picks the
    series whose link text contains the year hint. Verifies the index before
    accepting.
    """
    src = SubsLikeScriptSource()
    fake_pages = {
        "https://subslikescript.com/series/The_Office": "",
        "https://subslikescript.com/search?q=The+Office": (
            FIX / "subslikescript_search.html"
        ).read_text(),
        "https://subslikescript.com/series/The_Office-386676": (
            FIX / "subslikescript_index.html"
        ).read_text(),
        "https://subslikescript.com/series/The_Office-290978": "",
    }
    monkeypatch.setattr(src, "_get", lambda url: fake_pages.get(url, ""))
    expected_us = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
        Episode(season=5, number=15, title="Stress Relief"),
    ]
    show_ref = src.find_show("The Office", year=2005, tmdb_id=2316, expected_episodes=expected_us)
    assert show_ref is not None
    assert "The_Office-386676" in show_ref.url
