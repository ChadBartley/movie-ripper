from transcripts.matching import score_track


def test_score_track_returns_zero_on_empty_transcript():
    """Boundary: our wrapper short-circuits on empty input rather than
    sending it through rapidfuzz (whose behavior on empty strings is its
    own concern, not something we want to depend on)."""
    assert score_track("", "anything") == 0.0


def test_score_track_returns_zero_on_empty_episode():
    assert score_track("anything", "") == 0.0


def test_score_track_returns_zero_on_both_empty():
    assert score_track("", "") == 0.0
