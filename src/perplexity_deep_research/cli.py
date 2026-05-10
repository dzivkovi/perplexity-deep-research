"""Perplexity Deep Research CLI — single-shot research call with citations saved to markdown.

Calls Perplexity's Sonar Deep Research model via OpenRouter, captures the synthesis
plus all url_citation annotations, and writes a readable markdown file plus the raw
JSON response. One round trip per invocation; cost ~$0.30-0.90 per call (see README).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from dotenv import load_dotenv

try:
    from importlib.metadata import PackageNotFoundError, metadata as _pkg_metadata

    _pkg_meta = _pkg_metadata("perplexity-deep-research")
    __version__ = _pkg_meta["Version"]
    __description__ = _pkg_meta["Summary"]
except (ImportError, PackageNotFoundError):
    __version__ = "0.1.0"
    __description__ = "Single-shot Perplexity Deep Research CLI via OpenRouter"


DEFAULT_MODEL = "perplexity/sonar-deep-research"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_TIMEOUT_S = 600
DEFAULT_COST_HINT = "$0.30 to $0.90 per call (varies with citation count and reasoning depth)"
ENV_KEY = "OPENROUTER_API_KEY"


@dataclass
class Citation:
    url: str
    title: str = ""


@dataclass
class SynthesisResult:
    synthesis: str = ""
    citations: list[Citation] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------


def slugify(topic: str, max_len: int = 60) -> str:
    """Lowercase, hyphen-separated, filesystem-safe slug derived from a topic.

    Empty or non-alphanumeric input falls back to "topic" so the caller always
    gets a usable filename component.
    """
    s = topic.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return "topic"
    return s[:max_len].rstrip("-") or "topic"


def resolve_api_key(
    env_path: Optional[Path] = None,
    env_var: str = ENV_KEY,
) -> Optional[str]:
    """Resolve the OpenRouter API key.

    Precedence (matches python-dotenv `override=False`):
      1. Environment variable already set in the shell
      2. The given .env file (or `.env` in cwd / parent dirs if env_path is None)
    """
    if env_path is not None:
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
    else:
        # load_dotenv() with no args searches for `.env` from cwd upward.
        load_dotenv(override=False)
    return os.environ.get(env_var)


def build_prompt(topic: str, from_date: date, to_date: date) -> str:
    """The Sonar Deep Research prompt template — topic + a date window."""
    return (
        f"What has been happening with {topic} between {from_date.isoformat()} "
        f"and {to_date.isoformat()}? Include specific dates, names, numbers, and sources."
    )


def build_request_body(topic: str, from_date: date, to_date: date, model: str) -> bytes:
    """Construct the OpenRouter chat-completions request body."""
    return json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(topic, from_date, to_date)}],
    }).encode("utf-8")


def default_output_path(topic: str, when: Optional[datetime] = None, suffix: str = "md") -> Path:
    """Auto-named output: perplexity-deep-research-<slug>-<YYYY-MM-DD-HHMM>.<suffix>"""
    when = when or datetime.now()
    stamp = when.strftime("%Y-%m-%d-%H%M")
    return Path(f"perplexity-deep-research-{slugify(topic)}-{stamp}.{suffix}")


def parse_response(raw: dict[str, Any]) -> SynthesisResult:
    """Extract synthesis, deduplicated citations, and usage block from an OpenRouter response."""
    choices = raw.get("choices") or []
    msg = choices[0].get("message", {}) if choices else {}
    synthesis = msg.get("content", "") or ""
    annotations = msg.get("annotations") or []

    citations: list[Citation] = []
    seen: set[str] = set()
    for a in annotations:
        uc = (a or {}).get("url_citation") or {}
        url = uc.get("url", "")
        if url and url not in seen:
            seen.add(url)
            citations.append(Citation(url=url, title=uc.get("title", "") or ""))

    return SynthesisResult(
        synthesis=synthesis,
        citations=citations,
        usage=raw.get("usage") or {},
        model=raw.get("model", ""),
        raw=raw,
    )


def render_markdown(
    result: SynthesisResult,
    topic: str,
    from_date: date,
    to_date: date,
    elapsed_s: float,
    http_status: int,
    run_time: Optional[datetime] = None,
) -> str:
    """Render a human-readable markdown summary of the run."""
    run_time = run_time or datetime.now()
    lines = [
        f"# Perplexity Deep Research — {topic}",
        "",
        f"- **Run:** {run_time.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}",
        f"- **Model:** {result.model or DEFAULT_MODEL}",
        f"- **Window passed in prompt:** {from_date} to {to_date}",
        f"- **Latency:** {elapsed_s:.1f}s",
        f"- **HTTP status:** {http_status}",
        f"- **Synthesis length:** {len(result.synthesis)} chars",
        f"- **Unique citations:** {len(result.citations)}",
        f"- **Token usage:** prompt={result.usage.get('prompt_tokens', '?')}, "
        f"completion={result.usage.get('completion_tokens', '?')}, "
        f"total={result.usage.get('total_tokens', '?')}",
        "",
        "## Synthesis",
        "",
        result.synthesis or "_(empty)_",
        "",
        "## Citations",
        "",
    ]
    if result.citations:
        for i, c in enumerate(result.citations, 1):
            title = c.title.strip() or "(no title)"
            lines.append(f"{i}. [{title}]({c.url})")
    else:
        lines.append("_(none)_")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Network call (mocked in tests)
# ---------------------------------------------------------------------------


def call_openrouter(
    body: bytes,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict[str, Any]]:
    """Make the actual HTTP POST. Raises urllib.error.HTTPError on non-2xx."""
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw_text = resp.read().decode("utf-8")
        return resp.status, json.loads(raw_text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="perplexity-deep-research",
        description=__description__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic — auto-named output: perplexity-deep-research-<slug>-<date-time>.md
  perplexity-deep-research "Toronto real estate"

  # Explicit output file
  perplexity-deep-research "GLP-1 weight loss drugs" -o glp1.md

  # Custom window
  perplexity-deep-research "AI agents" --days 60

  # Dry run — no API call, no charge; prints what WOULD be sent
  perplexity-deep-research "Toronto real estate" --dry-run

API key is read from $OPENROUTER_API_KEY first, then from a `.env` file
in the current directory (or its parents). Override with --env-file.
        """,
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}\n{__description__}",
    )
    parser.add_argument(
        "topic",
        help="The text to research (e.g. \"Toronto real estate\")",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output markdown file. If omitted, auto-named from topic + timestamp.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Research window in days (default: {DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model id (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env file (default: search for `.env` in cwd and parent dirs)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Socket-recv timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't call the API. Print what would be sent and the would-be filename.",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip writing the raw .json response (only the .md summary)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress messages on stderr (errors still print).",
    )
    return parser.parse_args(argv)


def _say(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    to_date = date.today()
    from_date = to_date - timedelta(days=args.days)

    md_path: Path = args.output or default_output_path(args.topic, suffix="md")
    json_path = md_path.with_suffix(".json")

    body = build_request_body(args.topic, from_date, to_date, args.model)

    if args.dry_run:
        _say(f"[pdr] DRY RUN — no API call, no charge.", args.quiet)
        _say(f"[pdr] Would call: {DEFAULT_ENDPOINT}", args.quiet)
        _say(f"[pdr] Model: {args.model}", args.quiet)
        _say(f"[pdr] Window: {from_date} to {to_date}", args.quiet)
        _say(f"[pdr] Would write markdown to: {md_path}", args.quiet)
        if not args.no_json:
            _say(f"[pdr] Would write raw JSON to:   {json_path}", args.quiet)
        # Print the prompt body to stdout so users can pipe / inspect it.
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
        return 0

    api_key = resolve_api_key(env_path=args.env_file)
    if not api_key:
        env_disp = args.env_file or Path(".env")
        print(
            f"ERROR: {ENV_KEY} not found in environment or {env_disp}.\n"
            f"  Get a key at https://openrouter.ai/ and either:\n"
            f"    export {ENV_KEY}=sk-or-...\n"
            f"  or copy .env.example to .env and edit (see README).",
            file=sys.stderr,
        )
        return 2

    _say(f"[pdr] Firing {args.model} for '{args.topic}'...", args.quiet)
    _say(f"[pdr] Window: {from_date} to {to_date}", args.quiet)
    _say(f"[pdr] Cost expectation: ~{DEFAULT_COST_HINT}", args.quiet)
    _say(f"[pdr] Latency expectation: 1-5+ minutes (Deep Research is slow by design)", args.quiet)

    t0 = time.time()
    try:
        status, raw = call_openrouter(body, api_key, timeout_s=args.timeout)
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"[pdr] HTTPError {e.code} after {elapsed:.1f}s", file=sys.stderr)
        print(f"[pdr] Response body: {body_text[:500]}", file=sys.stderr)
        err_path = md_path.with_suffix(".error.json")
        err_path.write_text(
            json.dumps({"status": e.code, "elapsed_s": elapsed, "body": body_text}, indent=2),
            encoding="utf-8",
        )
        print(f"[pdr] Error detail saved to {err_path}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — top-level CLI fallback
        elapsed = time.time() - t0
        print(f"[pdr] Exception after {elapsed:.1f}s: {e}", file=sys.stderr)
        return 4

    elapsed = time.time() - t0
    _say(f"[pdr] HTTP {status} in {elapsed:.1f}s", args.quiet)

    if not args.no_json:
        json_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        _say(f"[pdr] Raw JSON saved to {json_path}", args.quiet)

    result = parse_response(raw)
    md_path.write_text(
        render_markdown(result, args.topic, from_date, to_date, elapsed, status),
        encoding="utf-8",
    )
    _say(f"[pdr] Markdown summary saved to {md_path}", args.quiet)

    print(json.dumps({
        "model": result.model or args.model,
        "latency_s": round(elapsed, 1),
        "synthesis_chars": len(result.synthesis),
        "citation_count": len(result.citations),
        "usage": result.usage,
        "output_md": str(md_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
