from transcripts.cache import Cache, Episode


def test_cache_init_creates_show_dir(tmp_path):
    cache = Cache(tmp_path)
    cache.write_show_meta(
        2316,
        "The Office",
        2005,
        [
            Episode(season=1, number=1, title="Pilot"),
            Episode(season=1, number=2, title="Diversity Day"),
        ],
    )
    assert (tmp_path / "2316" / "show.json").exists()


def test_cache_write_and_read_episode(tmp_path):
    cache = Cache(tmp_path)
    cache.write_episode(
        2316, season=5, number=15, title="Stress Relief", text="hello world dialogue"
    )
    text = cache.read_episode(2316, season=5, number=15, title="Stress Relief")
    assert text == "hello world dialogue"


def test_cache_filename_sanitization(tmp_path):
    cache = Cache(tmp_path)
    cache.write_episode(123, season=1, number=1, title="Take/me*home? please", text="foo")
    files = list((tmp_path / "123" / "s01").iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name
    assert "*" not in files[0].name
    assert "?" not in files[0].name


def test_cache_has_episode(tmp_path):
    cache = Cache(tmp_path)
    assert not cache.has_episode(2316, season=1, number=1, title="Pilot")
    cache.write_episode(2316, season=1, number=1, title="Pilot", text="x")
    assert cache.has_episode(2316, season=1, number=1, title="Pilot")


def test_cache_missing_show_meta_returns_none(tmp_path):
    cache = Cache(tmp_path)
    assert cache.read_show_meta(2316) is None


def test_cache_show_meta_roundtrip(tmp_path):
    cache = Cache(tmp_path)
    eps = [
        Episode(season=1, number=1, title="Pilot"),
        Episode(season=1, number=2, title="Diversity Day"),
    ]
    cache.write_show_meta(2316, "The Office", 2005, eps)
    meta = cache.read_show_meta(2316)
    assert meta["name"] == "The Office"
    assert meta["year"] == 2005
    assert len(meta["episodes"]) == 2
    assert meta["episodes"][0]["title"] == "Pilot"


def test_iter_all_episodes(tmp_path):
    cache = Cache(tmp_path)
    eps = [
        Episode(1, 1, "Pilot"),
        Episode(1, 2, "Diversity Day"),
        Episode(2, 1, "The Dundies"),
    ]
    cache.write_show_meta(2316, "The Office", 2005, eps)
    cache.write_episode(2316, 1, 1, "Pilot", "a")
    cache.write_episode(2316, 1, 2, "Diversity Day", "b")
    cache.write_episode(2316, 2, 1, "The Dundies", "c")
    out = list(cache.iter_all_episodes(2316))
    assert len(out) == 3
    assert out[0] == (Episode(1, 1, "Pilot"), "a")
