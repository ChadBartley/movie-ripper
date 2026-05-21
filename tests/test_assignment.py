from transcripts.cache import Episode
from transcripts.matching import assign_episodes


def _eps(*tuples):
    return [Episode(s, n, t) for s, n, t in tuples]


def test_obvious_assignment():
    eps = _eps((1, 1, "A"), (1, 2, "B"), (1, 3, "C"))
    scores = {
        ("t01", eps[0]): 95.0,
        ("t01", eps[1]): 30.0,
        ("t01", eps[2]): 20.0,
        ("t02", eps[0]): 25.0,
        ("t02", eps[1]): 90.0,
        ("t02", eps[2]): 35.0,
        ("t03", eps[0]): 30.0,
        ("t03", eps[1]): 28.0,
        ("t03", eps[2]): 92.0,
    }
    result = assign_episodes(["t01", "t02", "t03"], eps, scores)
    assert result["t01"].episode == eps[0]
    assert result["t02"].episode == eps[1]
    assert result["t03"].episode == eps[2]
    assert result["t01"].confidence == "high"


def test_contiguity_overrides_score_when_within_bonus():
    eps = _eps((1, 1, "A"), (1, 2, "B"))
    scores = {
        ("t01", eps[0]): 80.0,
        ("t01", eps[1]): 82.0,
        ("t02", eps[0]): 82.0,
        ("t02", eps[1]): 80.0,
    }
    result = assign_episodes(["t01", "t02"], eps, scores)
    assert result["t01"].episode.number == 1
    assert result["t02"].episode.number == 2


def test_collision_resolved_by_score():
    eps = _eps((1, 1, "A"), (1, 2, "B"))
    scores = {
        ("t01", eps[0]): 80.0,
        ("t01", eps[1]): 70.0,
        ("t02", eps[0]): 85.0,
        ("t02", eps[1]): 60.0,
    }
    result = assign_episodes(["t01", "t02"], eps, scores)
    assert result["t02"].episode.number == 1
    assert result["t01"].episode.number == 2


def test_low_confidence_when_margin_thin():
    eps = _eps((1, 1, "A"), (1, 2, "B"))
    scores = {
        ("t01", eps[0]): 62.0,
        ("t01", eps[1]): 60.0,
        ("t02", eps[0]): 30.0,
        ("t02", eps[1]): 90.0,
    }
    result = assign_episodes(["t01", "t02"], eps, scores)
    assert result["t01"].confidence == "low"
    assert result["t02"].confidence == "high"


def test_medium_confidence():
    eps = _eps((1, 1, "A"), (1, 2, "B"))
    scores = {
        ("t01", eps[0]): 65.0,
        ("t01", eps[1]): 50.0,
        ("t02", eps[0]): 30.0,
        ("t02", eps[1]): 90.0,
    }
    result = assign_episodes(["t01", "t02"], eps, scores)
    assert result["t01"].confidence == "medium"
