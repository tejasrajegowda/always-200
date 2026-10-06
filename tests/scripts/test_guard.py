import json

import pytest

from scripts.guard import (
    find_secrets,
    has_trailer,
    is_forbidden_path,
    load_config,
    scan_text,
    size_limit,
    word_matcher,
)


def test_short_words_match_whole_words_only():
    m = word_matcher("abc")
    assert m("x abc y") and not m("xabcy")


def test_long_words_match_inside_other_words():
    assert word_matcher("secretword")("mysecretwordhere")


def test_absolute_user_path_is_refused():
    # Built from pieces so this file never holds the pattern itself (the guard scans it too).
    assert scan_text("f", "see C:" + "\\Users\\someone\\x", [])
    assert scan_text("f", "see /c" + "/Users/someone/x", [])
    assert not scan_text("f", "GET /api/users/42", [])


def test_trailer_lines_are_refused():
    assert has_trailer("Fix thing\n\nSigned-off-by: x <y@z>")
    assert has_trailer("Fix thing\n\nReviewed-By: x <y@z>")
    assert not has_trailer("Fix thing")


def test_built_in_paths_are_forbidden():
    for p in [".env", ".local/guard.json", "config/owners.yaml", "recordings/A1/x"]:
        assert is_forbidden_path(p, extra=[])
    assert not is_forbidden_path(".env.example", extra=[])


def test_secret_shapes_are_found():
    assert find_secrets("f", "JIRA_API_TOKEN=abc123")
    assert not find_secrets("f", "JIRA_API_TOKEN=")
    assert not find_secrets("f", "JIRA_API_TOKEN=\nJIRA_EMAIL=")
    assert find_secrets("f", "x ATATT" + "A" * 30)


def test_only_the_demo_gif_is_exempt_from_the_2mb_rule():
    assert size_limit("docs/demo.gif") == 10 * 1024 * 1024
    for p in ["docs/other.gif", "demo.gif", "tests/fixtures/ci_slice/samples.jsonl"]:
        assert size_limit(p) == 2 * 1024 * 1024


def test_config_fails_closed_when_missing(tmp_path):
    with pytest.raises(SystemExit):
        load_config(tmp_path)


def test_config_fails_closed_when_word_list_is_empty(tmp_path):
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "guard.json").write_text(json.dumps({"forbiddenWords": []}))
    with pytest.raises(SystemExit):
        load_config(tmp_path)


def test_config_loads_when_words_are_present(tmp_path):
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "guard.json").write_text(json.dumps({"forbiddenWords": ["placeholder"]}))
    assert load_config(tmp_path)["forbiddenWords"] == ["placeholder"]
