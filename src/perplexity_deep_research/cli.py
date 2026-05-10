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
    # Only seen when running uninstalled from source (no dist-info present).
    # In a normal install (editable or otherwise), the version above comes from setuptools-scm.
    __version__ = "0.0.0+unknown"
    __description__ = "Single-shot Perplexity Deep Research CLI via OpenRouter"


DEFAULT_MODEL = "perplexity/sonar-deep-research"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT_S = 600
DEFAULT_COST_HINT = "$0.30 narrow / ~$1.00-1.20 broad query (varies with citation count and reasoning depth)"
ENV_KEY = "OPENROUTER_API_KEY"

# Time-window modes — see https://docs.perplexity.ai/docs/sonar/filters
VALID_RECENCY = ("day", "week", "month", "year")
DEFAULT_RECENCY = "month"
MAX_DAYS = 365  # past this, use --all-time (search_recency_filter caps at "year")


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


@dataclass
class TimeWindow:
    """Represents the time-bounding mode for a research request.

    Three mutually-exclusive modes mapping to Perplexity's API:
    - mode="recency": uses search_recency_filter (day/week/month/year)
    - mode="days":    uses search_after/before_date_filter (computed window)
    - mode="all_time": no filter, no date prose
    """
    mode: str  # "recency" | "days" | "all_time"
    recency: Optional[str] = None       # set when mode == "recency"
    days: Optional[int] = None           # set when mode == "days"
    from_date: Optional[date] = None     # set when mode == "days"
    to_date: Optional[date] = None       # set when mode == "days"


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


_RECENCY_PHRASE = {
    "day": "the past 24 hours",
    "week": "the past week",
    "month": "the past month",
    "year": "the past year",
}


def build_prompt(topic: str, window: TimeWindow) -> str:
    """The Sonar Deep Research prompt template — branches on time-window mode."""
    if window.mode == "all_time":
        return (
            f"Provide a comprehensive cited research brief on {topic}. "
            f"Include specific dates, names, numbers, and sources."
        )
    if window.mode == "days":
        return (
            f"What has been happening with {topic} between {window.from_date.isoformat()} "
            f"and {window.to_date.isoformat()}? Include specific dates, names, numbers, and sources."
        )
    # recency
    return (
        f"What has been happening with {topic} in {_RECENCY_PHRASE[window.recency]}? "
        f"Include specific dates, names, numbers, and sources."
    )


def build_request_body(topic: str, window: TimeWindow, model: str) -> bytes:
    """Construct the OpenRouter chat-completions request body.

    Sets Perplexity's official date-filter parameters per
    https://docs.perplexity.ai/docs/sonar/filters — recency and date filters
    are mutually exclusive at the API level.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(topic, window)}],
    }
    if window.mode == "recency":
        body["search_recency_filter"] = window.recency
    elif window.mode == "days":
        body["search_after_date_filter"] = window.from_date.strftime("%m/%d/%Y")
        body["search_before_date_filter"] = window.to_date.strftime("%m/%d/%Y")
    # all_time: no date filter parameters
    return json.dumps(body).encode("utf-8")


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


def _window_summary(window: TimeWindow) -> str:
    """One-line description of the time window for markdown / stderr output."""
    if window.mode == "all_time":
        return "unbounded (all-time research)"
    if window.mode == "days":
        return f"{window.from_date} to {window.to_date} ({window.days} days)"
    return f"past {window.recency} (search_recency_filter={window.recency!r})"


def render_markdown(
    result: SynthesisResult,
    topic: str,
    window: TimeWindow,
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
        f"- **Window:** {_window_summary(window)}",
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
  # Default: --recency month (≈30 days, uses Perplexity's official filter)
  perplexity-deep-research "Toronto real estate"

  # Time-bound to past year — ideal for slow-moving topics like real estate trends
  perplexity-deep-research "Toronto resale condos" --recency year

  # Custom window in days (1-365)
  perplexity-deep-research "GLP-1 weight loss drugs" --days 90 -o glp1.md

  # Cold-start / evergreen topic — no time filter
  perplexity-deep-research "What is differential privacy?" --all-time

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

    # Time-window flags — mutually exclusive. Default (no flag) = recency=month.
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--recency",
        choices=list(VALID_RECENCY),
        default=None,
        help=f"Predefined recency window (default if no time flag: {DEFAULT_RECENCY!r})",
    )
    time_group.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help=f"Custom window in days (1-{MAX_DAYS}); past {MAX_DAYS} use --all-time",
    )
    time_group.add_argument(
        "--all-time",
        action="store_true",
        help="No time filter — for cold-start / evergreen topics",
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

    args = parser.parse_args(argv)

    # Validate --days range here (argparse 'choices' doesn't support int ranges nicely).
    if args.days is not None and not (1 <= args.days <= MAX_DAYS):
        parser.error(
            f"--days must be between 1 and {MAX_DAYS} "
            f"(use --all-time for unbounded research beyond 1 year)"
        )

    return args


def resolve_time_window(args: argparse.Namespace, today: Optional[date] = None) -> TimeWindow:
    """Convert parsed argparse Namespace into a TimeWindow.

    Default (no time flag passed) → recency=month.
    """
    today = today or date.today()
    if args.all_time:
        return TimeWindow(mode="all_time")
    if args.days is not None:
        return TimeWindow(
            mode="days",
            days=args.days,
            from_date=today - timedelta(days=args.days),
            to_date=today,
        )
    recency = args.recency or DEFAULT_RECENCY
    return TimeWindow(mode="recency", recency=recency)


def _say(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    window = resolve_time_window(args)

    md_path: Path = args.output or default_output_path(args.topic, suffix="md")
    json_path = md_path.with_suffix(".json")

    body = build_request_body(args.topic, window, args.model)

    if args.dry_run:
        _say(f"[pdr] DRY RUN — no API call, no charge.", args.quiet)
        _say(f"[pdr] Would call: {DEFAULT_ENDPOINT}", args.quiet)
        _say(f"[pdr] Model: {args.model}", args.quiet)
        _say(f"[pdr] Window: {_window_summary(window)}", args.quiet)
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
    _say(f"[pdr] Window: {_window_summary(window)}", args.quiet)
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
        render_markdown(result, args.topic, window, elapsed, status),
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
