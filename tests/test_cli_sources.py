"""Tests for fetch_transcripts.py's --sources flag parser."""

# conftest.py adds scripts/ to sys.path; fetch_transcripts is a top-level
# script there.
import fetch_transcripts
import pytest
import typer


def test_default_when_spec_is_none():
    names = fetch_transcripts.parse_sources_arg(None)
    assert names == ["subslikescript", "springfield", "fandom"]


def test_default_when_spec_is_empty_string():
    names = fetch_transcripts.parse_sources_arg("")
    assert names == ["subslikescript", "springfield", "fandom"]


def test_default_when_spec_is_whitespace():
    names = fetch_transcripts.parse_sources_arg("   ")
    assert names == ["subslikescript", "springfield", "fandom"]


def test_custom_order_preserved():
    """Order = precedence; the user puts fandom first, fandom comes first."""
    names = fetch_transcripts.parse_sources_arg("fandom,subslikescript")
    assert names == ["fandom", "subslikescript"]


def test_subset_skips_omitted_sources():
    names = fetch_transcripts.parse_sources_arg("fandom")
    assert names == ["fandom"]


def test_whitespace_in_spec_tolerated():
    names = fetch_transcripts.parse_sources_arg(" fandom , subslikescript ")
    assert names == ["fandom", "subslikescript"]


def test_dedup_preserves_first_occurrence():
    names = fetch_transcripts.parse_sources_arg("fandom,subslikescript,fandom")
    assert names == ["fandom", "subslikescript"]


def test_unknown_name_raises_bad_parameter():
    """A typo should fail loudly, not silently produce a shorter ladder."""
    with pytest.raises(typer.BadParameter) as excinfo:
        fetch_transcripts.parse_sources_arg("subslikescript,opensubtitles")
    msg = str(excinfo.value)
    assert "opensubtitles" in msg


def test_only_unknown_names_raises_bad_parameter():
    with pytest.raises(typer.BadParameter):
        fetch_transcripts.parse_sources_arg("not-a-source")


def test_empty_tokens_only_raises_bad_parameter():
    """`,,,` parses to no names; that's a bug in the user's command line."""
    with pytest.raises(typer.BadParameter):
        fetch_transcripts.parse_sources_arg(",,,")
