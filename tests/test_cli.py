"""Unit tests for perplexity_deep_research.cli — fully mocked, no real API calls."""
from __future__ import annotations

import io
import json
import os
import urllib.error
from datetime import date, datetime
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
# Prompt + request body
# ---------------------------------------------------------------------------


def test_build_prompt_contains_topic_and_dates():
    p = cli.build_prompt("Toronto real estate", date(2026, 4, 7), date(2026, 5, 7))
    assert "Toronto real estate" in p
    assert "2026-04-07" in p
    assert "2026-05-07" in p


def test_build_request_body_shape():
    body = cli.build_request_body(
        "x", date(2026, 4, 7), date(2026, 5, 7), cli.DEFAULT_MODEL
    )
    decoded = json.loads(body)
    assert decoded["model"] == cli.DEFAULT_MODEL
    assert len(decoded["messages"]) == 1
    assert decoded["messages"][0]["role"] == "user"
    assert "x" in decoded["messages"][0]["content"]


def test_build_request_body_custom_model():
    body = cli.build_request_body(
        "x", date(2026, 4, 7), date(2026, 5, 7), "perplexity/sonar-pro"
    )
    assert json.loads(body)["model"] == "perplexity/sonar-pro"


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


def test_render_markdown_includes_all_sections():
    result = cli.SynthesisResult(
        synthesis="Body.",
        citations=[cli.Citation(url="https://a.example", title="A")],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        model="perplexity/sonar-deep-research",
    )
    md = cli.render_markdown(
        result, "x", date(2026, 4, 7), date(2026, 5, 7), 12.3, 200,
        run_time=datetime(2026, 5, 7, 22, 30),
    )
    assert "# Perplexity Deep Research — x" in md
    assert "Latency:** 12.3s" in md
    assert "HTTP status:** 200" in md
    assert "Body." in md
    assert "[A](https://a.example)" in md


def test_render_markdown_empty_citations_renders_sentinel():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="hi", citations=[]),
        "x", date(2026, 4, 7), date(2026, 5, 7), 1.0, 200,
    )
    assert "_(none)_" in md


def test_render_markdown_empty_synthesis_renders_sentinel():
    md = cli.render_markdown(
        cli.SynthesisResult(synthesis="", citations=[]),
        "x", date(2026, 4, 7), date(2026, 5, 7), 1.0, 200,
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
