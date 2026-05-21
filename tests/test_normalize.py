from transcripts.cache import normalize_text


def test_lowercase():
    assert normalize_text("Hello WORLD") == "hello world"


def test_strips_speaker_labels():
    src = "MICHAEL: Hello there.\nPAM: Hi Michael."
    assert normalize_text(src) == "hello there hi michael"


def test_strips_bracketed_directions():
    src = "[door slams]\nHe walks in.\n(quietly) Hello."
    assert normalize_text(src) == "he walks in hello"


def test_collapses_whitespace():
    assert normalize_text("a  b\n\nc\t d") == "a b c d"


def test_strips_punctuation_keeps_apostrophes():
    # apostrophes preserved — they're inside words, removing them harms match
    assert normalize_text("Don't, you know!") == "don't you know"


def test_strips_html_entities():
    assert normalize_text("foo &amp; bar &#8217;s") == "foo bar 's"


def test_strips_srt_timestamps():
    src = "1\n00:01:23,456 --> 00:01:25,789\nHello.\n\n2\n00:01:26,000 --> 00:01:28,000\nWorld."
    assert normalize_text(src) == "hello world"


def test_empty_input():
    assert normalize_text("") == ""
    assert normalize_text("   \n\n  ") == ""
