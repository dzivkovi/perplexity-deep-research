"""Unit tests for perplexity_deep_research.cli — fully mocked, no real API calls."""
from __future__ import annotations

import io
import json
import os
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from perplexity_deep_research import cli


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic, expected",
    [
        ("Toronto real estate", "toronto-real-estate"),
        ("AI agents 2026!", "ai-agents-2026"),
        # Non-ASCII collapses to hyphens like any other non-[a-z0-9] run.
        ("GLP-1 / weight-loss drugs", "glp-1-weight-loss-drugs"),
        ("", "topic"),
        ("   ", "topic"),
        ("!!!", "topic"),
    ],
    ids=["simple", "special-chars", "unicode-punct", "empty", "whitespace", "all-symbols"],
)
def test_slugify(topic, expected):
    assert cli.slugify(topic) == expected


def test_slugify_max_len_cap():
    assert len(cli.slugify("a" * 200)) <= 60


def test_slugify_no_leading_or_trailing_hyphens():
    s = cli.slugify("---weird-input---")
    assert not s.startswith("-")
    assert not s.endswith("-")


# ---------------------------------------------------------------------------
# resolve_api_key — delegates parsing to python-dotenv; we test the contract
# ---------------------------------------------------------------------------


def test_env_var_wins_when_already_set(tmp_path):
    """Existing shell env var should beat .env file (python-dotenv override=False)."""
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")

    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-env"}, clear=True):
        assert cli.resolve_api_key(env_path=env_file) == "from-env"


def test_falls_back_to_file_when_env_unset(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")

    with mock.patch.dict(os.environ, {}, clear=True):
        assert cli.resolve_api_key(env_path=env_file) == "from-file"


def test_returns_none_when_neither_present():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cli.resolve_api_key(env_path=Path("/no/such/file")) is None


# ---------------------------------------------------------------------------
# parse_when — relative + absolute time specs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,expected_offset_days", [
    ("7d", 7),
    ("30d", 30),
    ("4w", 28),
    ("1m", 30),
    ("3m", 90),
    ("1y", 365),
    ("2y", 730),
    ("0d", 0),
])
def test_parse_when_relative(spec, expected_offset_days):
    today = date(2026, 5, 10)
    assert cli.parse_when(spec, today=today) == today - timedelta(days=expected_offset_days)


def test_parse_when_iso_date():
    assert cli.parse_when("2026-01-01") == date(2026, 1, 1)


@pytest.mark.parametrize("spec", ["all", "none", "ALL", "  all  ", "None"])
def test_parse_when_unbounded_sentinels(spec):
    assert cli.parse_when(spec) is None


@pytest.mark.parametrize("spec,expected", [
    ("7D", date(2026, 5, 3)),           # uppercase unit
    (" 7d ", date(2026, 5, 3)),         # surrounding whitespace
    ("1Y", date(2025, 5, 10)),          # uppercase year
])
def test_parse_when_case_and_whitespace_tolerated(spec, expected):
    """Case and surrounding whitespace are normalized; date is correct."""
    assert cli.parse_when(spec, today=date(2026, 5, 10)) == expected


# Failure cases — all should raise ValueError with the unified help text mentioned.
# Internal whitespace ("3 m"), garbage strings, malformed dates, empty input.
@pytest.mark.parametrize("spec", [
    "",            # empty string
    "bad",         # plain garbage
    "7",           # number, no unit
    "10z",         # bad unit
    "abcd",        # not numeric
    "2026-13-99",  # invalid ISO date (month 13, day 99)
    "3 m",         # internal whitespace inside the token
    "-7d",         # leading minus — regex \d+ doesn't accept
])
def test_parse_when_rejects_garbage(spec):
    with pytest.raises(ValueError, match=r"Invalid time spec"):
        cli.parse_when(spec)


# ---------------------------------------------------------------------------
# build_prompt — branches on which bounds are set
# ---------------------------------------------------------------------------


def test_build_prompt_closed_range_mentions_both_dates():
    w = cli.TimeWindow(since=date(2026, 4, 7), until=date(2026, 5, 7))
    p = cli.build_prompt("Toronto real estate", w)
    assert "Toronto real estate" in p
    assert "2026-04-07" in p
    assert "2026-05-07" in p
    assert "between" in p


def test_build_prompt_open_upper_bound_says_since():
    w = cli.TimeWindow(since=date(2025, 1, 1), until=None)
    p = cli.build_prompt("EU AI Act", w)
    assert "since 2025-01-01" in p
    assert "between" not in p


def test_build_prompt_open_lower_bound_says_up_to():
    w = cli.TimeWindow(since=None, until=date(2025, 12, 31))
    p = cli.build_prompt("x", w)
    assert "up to 2025-12-31" in p


def test_build_prompt_unbounded_has_no_date_prose():
    p = cli.build_prompt("differential privacy", cli.TimeWindow())
    assert "differential privacy" in p
    assert "between" not in p
    assert "since" not in p
    assert "up to" not in p
    assert "2026-" not in p


# ---------------------------------------------------------------------------
# build_request_body — emits only the date filters that are set
# ---------------------------------------------------------------------------


def test_build_request_body_closed_range_emits_both_filters():
    body = json.loads(cli.build_request_body(
        "x",
        cli.TimeWindow(since=date(2026, 4, 7), until=date(2026, 5, 7)),
        cli.DEFAULT_MODEL,
    ))
    # Perplexity wants %m/%d/%Y format, not ISO.
    assert body["search_after_date_filter"] == "04/07/2026"
    assert body["search_before_date_filter"] == "05/07/2026"
    assert "search_recency_filter" not in body


def test_build_request_body_open_upper_emits_only_after_filter():
    body = json.loads(cli.build_request_body(
        "x", cli.TimeWindow(since=date(2025, 1, 1)), cli.DEFAULT_MODEL
    ))
    assert body["search_after_date_filter"] == "01/01/2025"
    assert "search_before_date_filter" not in body


def test_build_request_body_open_lower_emits_only_before_filter():
    body = json.loads(cli.build_request_body(
        "x", cli.TimeWindow(until=date(2025, 12, 31)), cli.DEFAULT_MODEL
    ))
    assert "search_after_date_filter" not in body
    assert body["search_before_date_filter"] == "12/31/2025"


def test_build_request_body_unbounded_emits_no_filters():
    body = json.loads(cli.build_request_body("x", cli.TimeWindow(), cli.DEFAULT_MODEL))
    assert "search_recency_filter" not in body
    assert "search_after_date_filter" not in body
    assert "search_before_date_filter" not in body


def test_build_request_body_custom_model():
    body = json.loads(cli.build_request_body("x", cli.TimeWindow(), "perplexity/sonar-pro"))
    assert body["model"] == "perplexity/sonar-pro"


# ---------------------------------------------------------------------------
# parse_args + resolve_time_window
# ---------------------------------------------------------------------------


def test_default_window_is_past_30_days():
    args = cli.parse_args(["topic"])
    today = date(2026, 5, 10)
    w = cli.resolve_time_window(args, today=today)
    assert w.since == today - timedelta(days=30)
    assert w.until == today


def test_since_all_makes_window_unbounded():
    args = cli.parse_args(["topic", "--since", "all"])
    w = cli.resolve_time_window(args, today=date(2026, 5, 10))
    assert w.since is None
    assert w.until is None


def test_since_relative_sets_lower_bound_only():
    args = cli.parse_args(["topic", "--since", "7d"])
    today = date(2026, 5, 10)
    w = cli.resolve_time_window(args, today=today)
    assert w.since == today - timedelta(days=7)
    # No --until given → open upper bound (NOT auto-filled to today, because
    # the user opted out of the default-bounded mode by passing --since).
    assert w.until is None


def test_until_only_sets_upper_bound_only():
    args = cli.parse_args(["topic", "--until", "2025-12-31"])
    w = cli.resolve_time_window(args, today=date(2026, 5, 10))
    assert w.since is None
    assert w.until == date(2025, 12, 31)


def test_since_and_until_together():
    args = cli.parse_args(["topic", "--since", "2025-01-01", "--until", "2025-12-31"])
    w = cli.resolve_time_window(args, today=date(2026, 5, 10))
    assert w.since == date(2025, 1, 1)
    assert w.until == date(2025, 12, 31)


@pytest.mark.parametrize("flag,value", [
    ("--since", "bad"),
    ("--since", "7"),       # missing unit
    ("--since", "10z"),     # bad unit
    ("--since", "2026-13-99"),  # invalid date
    ("--until", "garbage"),
])
def test_parse_args_rejects_bad_time_spec(flag, value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["topic", flag, value])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert flag in err  # error message names which flag was bad
    assert "Invalid time spec" in err  # unified error message body


def test_parse_args_rejects_inverted_range(capsys):
    """--since AFTER --until is a typo, not a valid window — fail loud at parse time."""
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["topic", "--since", "2026-12-31", "--until", "2026-01-01"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "is after" in err
    # Helpful suggestion: shows the user what swapped order would look like.
    assert "Use --since 2026-01-01 --until 2026-12-31" in err


def test_parse_args_rejects_inverted_relative_range(capsys):
    """--since 7d --until 30d is inverted (since=today-7, until=today-30); reject."""
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["topic", "--since", "7d", "--until", "30d"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "is after" in err


@pytest.mark.parametrize("spec", ["100000d", "200y", "1500m"])
def test_parse_when_rejects_excessive_offset(spec):
    """Relative offsets beyond ~100 years (36,500 days) are garbage, not legitimate queries."""
    with pytest.raises(ValueError, match=r"too large"):
        cli.parse_when(spec, today=date(2026, 5, 10))


# ---------------------------------------------------------------------------
# default_output_path
# ---------------------------------------------------------------------------


def test_default_output_path_pattern():
    when = datetime(2026, 5, 7, 22, 30)
    p = cli.default_output_path("Toronto real estate", when=when, suffix="md")
    assert p.name == "perplexity-deep-research-toronto-real-estate-2026-05-07-2230.md"


def test_default_output_path_handles_messy_topic():
    when = datetime(2026, 5, 7, 22, 30)
    p = cli.default_output_path("AI / agents 2026!", when=when)
    assert p.name == "perplexity-deep-research-ai-agents-2026-2026-05-07-2230.md"


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------


def test_parse_response_typical():
    raw = {
        "model": "perplexity/sonar-deep-research",
        "choices": [{
            "message": {
                "content": "# Headline\n\nBody text.",
                "annotations": [
                    {"url_citation": {"url": "https://a.example/1", "title": "A"}},
                    {"url_citation": {"url": "https://b.example/2", "title": "B"}},
                    {"url_citation": {"url": "https://a.example/1", "title": "A dup"}},
                ],
            },
        }],
        "usage": {"prompt_tokens": 30, "completion_tokens": 200, "total_tokens": 230},
    }
    result = cli.parse_response(raw)
    assert "Headline" in result.synthesis
    assert len(result.citations) == 2, "duplicate URL should be deduped"
    assert result.citations[0].url == "https://a.example/1"
    assert result.citations[0].title == "A"
    assert result.usage["total_tokens"] == 230
    assert result.model == "perplexity/sonar-deep-research"


def test_parse_response_empty():
    result = cli.parse_response({"choices": []})
    assert result.synthesis == ""
    assert result.citations == []


def test_parse_response_missing_annotations():
    raw = {"choices": [{"message": {"content": "hi"}}]}
    result = cli.parse_response(raw)
    assert result.synthesis == "hi"
    assert result.citations == []


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def test_render_markdown_closed_range_includes_all_sections():
    result = cli.SynthesisResult(
        synthesis="Body.",
        citations=[cli.Citation(url="https://a.example", title="A")],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        model="perplexity/sonar-deep-research",
    )
    window = cli.TimeWindow(since=date(2026, 4, 7), until=date(2026, 5, 7))
    md = cli.render_markdown(
        result, "x", window, 12.3, 200, run_time=datetime(2026, 5, 7, 22, 30),
    )
    assert "# Perplexity Deep Research — x" in md
    assert "Latency:** 12.3s" in md
    assert "HTTP status:** 200" in md
    assert "Body." in md
    assert "[A](https://a.example)" in md
    assert "2026-04-07 to 2026-05-07" in md


def test_render_markdown_open_upper_says_since():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="x", citations=[]),
        "topic", cli.TimeWindow(since=date(2025, 1, 1)), 1.0, 200,
    )
    assert "since 2025-01-01" in md
    assert "no upper bound" in md


def test_render_markdown_unbounded_shows_cold_start():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="x", citations=[]),
        "topic", cli.TimeWindow(), 1.0, 200,
        run_time=datetime(2026, 5, 10, 14, 51),
    )
    assert "**Window:** unbounded" in md
    assert "cold-start" in md
    assert "to 2026-" not in md
    assert "between 2026-" not in md


def test_render_markdown_empty_citations_renders_sentinel():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="hi", citations=[]),
        "x", cli.TimeWindow(since=date(2026, 4, 7), until=date(2026, 5, 7)), 1.0, 200,
    )
    assert "_(none)_" in md


def test_render_markdown_empty_synthesis_renders_sentinel():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="", citations=[]),
        "x", cli.TimeWindow(since=date(2026, 4, 7), until=date(2026, 5, 7)), 1.0, 200,
    )
    assert "_(empty)_" in md


# ---------------------------------------------------------------------------
# main() — fully mocked at the network boundary, no $$ spent
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_network_call(capsys):
    with mock.patch.object(cli, "call_openrouter") as mocked:
        rc = cli.main(["any topic", "--dry-run"])
    assert rc == 0
    mocked.assert_not_called()
    # Dry run prints the prompt body to stdout for inspection
    printed = capsys.readouterr().out
    assert "any topic" in printed
    assert cli.DEFAULT_MODEL in printed


def test_dry_run_does_not_require_api_key():
    """No env var, no .env file — dry run must still succeed."""
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch.object(cli, "call_openrouter") as mocked:
            rc = cli.main(["x", "--dry-run", "--env-file", "/nope"])
    assert rc == 0
    mocked.assert_not_called()


def test_main_returns_2_when_key_missing():
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch.object(cli, "call_openrouter") as mocked:
            rc = cli.main(["x", "--env-file", "/nope"])
    assert rc == 2
    mocked.assert_not_called()


def test_main_writes_md_and_json_and_exit_0(tmp_path):
    canned = {
        "model": "perplexity/sonar-deep-research",
        "choices": [{
            "message": {
                "content": "Synthesis.",
                "annotations": [{"url_citation": {"url": "https://a.example", "title": "A"}}],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    out_md = tmp_path / "out.md"
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake-key"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", return_value=(200, canned)) as mocked:
            rc = cli.main(["x", "-o", str(out_md)])
    assert rc == 0
    mocked.assert_called_once()
    assert out_md.exists(), "markdown file should exist"
    assert out_md.with_suffix(".json").exists(), "raw JSON file should exist"
    md = out_md.read_text(encoding="utf-8")
    assert "Synthesis." in md
    assert "[A](https://a.example)" in md


def test_main_since_flag_propagates_to_request_body(tmp_path):
    """End-to-end: --since 7d makes it from argparse all the way into the API request.

    Without this test, the plumbing (argparse → resolve_time_window → build_request_body)
    is only unit-tested in pieces. This test pins the integration.
    """
    canned = {"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {}}
    out_md = tmp_path / "out.md"
    captured_body = {}

    def fake_call(body, api_key, **kwargs):
        captured_body["bytes"] = body
        return (200, canned)

    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", side_effect=fake_call):
            rc = cli.main(["x", "--since", "7d", "-o", str(out_md)])

    assert rc == 0
    body = json.loads(captured_body["bytes"])
    # --since 7d should set the after-date filter, leave before-date open.
    assert "search_after_date_filter" in body
    assert "search_before_date_filter" not in body
    # The rendered markdown should reflect the open upper bound.
    md = out_md.read_text(encoding="utf-8")
    assert "no upper bound" in md


def test_main_all_time_flag_propagates_to_request_body(tmp_path):
    """End-to-end: --since all produces a request with no date filters at all."""
    canned = {"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {}}
    out_md = tmp_path / "out.md"
    captured_body = {}

    def fake_call(body, api_key, **kwargs):
        captured_body["bytes"] = body
        return (200, canned)

    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", side_effect=fake_call):
            rc = cli.main(["x", "--since", "all", "-o", str(out_md)])

    assert rc == 0
    body = json.loads(captured_body["bytes"])
    assert "search_after_date_filter" not in body
    assert "search_before_date_filter" not in body
    assert "search_recency_filter" not in body
    md = out_md.read_text(encoding="utf-8")
    assert "unbounded" in md
    assert "cold-start" in md


def test_main_no_json_skips_raw_file(tmp_path):
    canned = {"model": "m", "choices": [{"message": {"content": "x"}}], "usage": {}}
    out_md = tmp_path / "out.md"
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", return_value=(200, canned)):
            rc = cli.main(["x", "-o", str(out_md), "--no-json"])
    assert rc == 0
    assert out_md.exists()
    assert not out_md.with_suffix(".json").exists()


def test_main_http_error_writes_error_file_and_exits_3(tmp_path):
    err = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=io.BytesIO(b'{"error":{"code":429,"message":"rate limited"}}'),
    )
    out_md = tmp_path / "out.md"
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", side_effect=err):
            rc = cli.main(["x", "-o", str(out_md)])
    assert rc == 3
    err_file = out_md.with_suffix(".error.json")
    assert err_file.exists()
    err_payload = json.loads(err_file.read_text(encoding="utf-8"))
    assert err_payload["status"] == 429
    assert "rate limited" in err_payload["body"]


def test_main_auto_names_file_in_cwd_when_no_output_arg(tmp_path, monkeypatch):
    canned = {"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {}}
    monkeypatch.chdir(tmp_path)
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True):
        with mock.patch.object(cli, "call_openrouter", return_value=(200, canned)):
            rc = cli.main(["Toronto real estate"])
    assert rc == 0
    produced = list(tmp_path.glob("perplexity-deep-research-toronto-real-estate-*.md"))
    assert len(produced) == 1, f"expected one auto-named file, got {produced}"
