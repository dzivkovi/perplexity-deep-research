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

# Default lower bound when neither --since nor --until is given.
DEFAULT_SINCE_DAYS = 30


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
    """Time bounds for the search. Either side may be None for an open bound.

    - since=None, until=None → no date filter at all (cold-start)
    - since=X,    until=None → 'from X onwards'
    - since=None, until=Y    → 'up to Y'
    - since=X,    until=Y    → closed range

    See https://docs.perplexity.ai/docs/sonar/filters — the two date filters
    are independent at the API level, so we keep the same shape here.
    """
    since: Optional[date] = None
    until: Optional[date] = None


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


_RELATIVE_UNITS: dict[str, int] = {"d": 1, "w": 7, "m": 30, "y": 365}
_MAX_RELATIVE_OFFSET_DAYS = 36_500  # 100 years — past this we reject as garbage
_TIME_SPEC_HELP = (
    "Expected ISO date (e.g. 2026-01-01), "
    "relative (e.g. 7d, 4w, 3m, 1y), or 'all'/'none' for unbounded."
)


def parse_when(spec: str, today: Optional[date] = None) -> Optional[date]:
    """Parse a `--since`/`--until` value into an absolute date.

    Accepted forms (case-insensitive, surrounding whitespace ignored):
    - Relative:   ``7d`` / ``4w`` / ``3m`` / ``1y`` (today minus N units)
    - ISO date:   ``2026-01-01``
    - Unbounded:  ``all`` / ``none`` → returns ``None``

    Months and years are calendar-approximate (30d, 365d) — the values
    people use informally when saying "the past 3 months".
    """
    today = today or date.today()
    s = spec.strip().lower()
    if s in ("all", "none"):
        return None
    # Try relative first — it's the most common form and has a strict shape.
    m = re.fullmatch(r"(\d+)([dwmy])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * _RELATIVE_UNITS[unit]
        if days > _MAX_RELATIVE_OFFSET_DAYS:
            raise ValueError(
                f"Invalid time spec {spec!r} — relative offset too large "
                f"(max {_MAX_RELATIVE_OFFSET_DAYS} days ≈ 100 years). {_TIME_SPEC_HELP}"
            )
        return today - timedelta(days=days)
    # Fall back to ISO date. Errors from either shape produce one unified message.
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Invalid time spec {spec!r}. {_TIME_SPEC_HELP}") from e


def build_prompt(topic: str, window: TimeWindow) -> str:
    """Sonar Deep Research prompt template — branches on window shape.

    The bounded variants embed ISO dates in the prose for model alignment;
    the unbounded (cold-start) variant omits date framing entirely so the
    model isn't biased toward recent content.
    """
    if window.since is None and window.until is None:
        return (
            f"Provide a comprehensive cited research brief on {topic}. "
            f"Include specific dates, names, numbers, and sources."
        )
    if window.since is not None and window.until is not None:
        return (
            f"What has been happening with {topic} between {window.since.isoformat()} "
            f"and {window.until.isoformat()}? Include specific dates, names, numbers, and sources."
        )
    if window.since is not None:
        return (
            f"What has been happening with {topic} since {window.since.isoformat()}? "
            f"Include specific dates, names, numbers, and sources."
        )
    return (
        f"What was happening with {topic} up to {window.until.isoformat()}? "
        f"Include specific dates, names, numbers, and sources."
    )


def build_request_body(topic: str, window: TimeWindow, model: str) -> bytes:
    """Construct the OpenRouter chat-completions request body.

    Emits Perplexity's date-filter parameters per
    https://docs.perplexity.ai/docs/sonar/filters — either or both depending
    on which bounds are set. Both unset = no date filter (cold-start).
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(topic, window)}],
    }
    if window.since is not None:
        body["search_after_date_filter"] = window.since.strftime("%m/%d/%Y")
    if window.until is not None:
        body["search_before_date_filter"] = window.until.strftime("%m/%d/%Y")
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
    if window.since is None and window.until is None:
        return "unbounded (cold-start, no date filter)"
    if window.since is not None and window.until is not None:
        return f"{window.since.isoformat()} to {window.until.isoformat()}"
    if window.since is not None:
        return f"since {window.since.isoformat()} (no upper bound)"
    return f"up to {window.until.isoformat()} (no lower bound)"


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
  # Default — past 30 days. Best for news, trends, recent developments.
  perplexity-deep-research "Toronto resale condos"

  # Cold-start: you know nothing about the topic, want canonical sources
  perplexity-deep-research "What is differential privacy?" --since all

  # Relative window — past 90 days
  perplexity-deep-research "GLP-1 weight loss drugs" --since 90d -o glp1.md

  # Past year — for slow-moving trends
  perplexity-deep-research "Toronto resale condos" --since 1y

  # Explicit date range
  perplexity-deep-research "Mark Carney AI policy" --since 2025-04-01 --until 2026-04-30

  # Everything since a date (open upper bound)
  perplexity-deep-research "EU AI Act enforcement" --since 2024-09-01

  # Dry run — no API call, no charge; prints what WOULD be sent
  perplexity-deep-research "Toronto resale condos" --dry-run

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

    # Time-window flags. Independent (not mutex). Either or both may be omitted.
    # Default when both omitted: past 30 days.
    parser.add_argument(
        "--since",
        metavar="VALUE",
        default=None,
        help=(
            "Lower bound. ISO date (2026-01-01), relative (7d, 4w, 3m, 1y), "
            "or 'all' for no lower bound (cold-start). Default if --since "
            f"and --until both omitted: {DEFAULT_SINCE_DAYS}d ago."
        ),
    )
    parser.add_argument(
        "--until",
        metavar="VALUE",
        default=None,
        help=(
            "Upper bound. Same formats as --since. Default if --since and "
            "--until both omitted: today. Omit for open upper bound."
        ),
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

    # Parse --since / --until ONCE here, with a single `today` snapshot.
    # The resolved dates are stashed on the namespace as args._since_date /
    # args._until_date for resolve_time_window() to read directly — this avoids
    # double-parsing and any midnight TOCTOU between validation and resolution.
    today = date.today()
    args._today = today
    args._since_date = None
    args._until_date = None
    for flag, value, attr in (
        ("--since", args.since, "_since_date"),
        ("--until", args.until, "_until_date"),
    ):
        if value is not None:
            try:
                setattr(args, attr, parse_when(value, today=today))
            except ValueError as e:
                parser.error(f"{flag}: {e}")

    # Reject inverted ranges (--since AFTER --until). The API would silently
    # accept these and the user would pay ~$1 for an impossible query.
    if (
        args._since_date is not None
        and args._until_date is not None
        and args._since_date > args._until_date
    ):
        parser.error(
            f"--since ({args._since_date.isoformat()}) is after "
            f"--until ({args._until_date.isoformat()}). "
            f"Use --since {args._until_date.isoformat()} "
            f"--until {args._since_date.isoformat()} if you meant that range."
        )

    return args


def resolve_time_window(args: argparse.Namespace, today: Optional[date] = None) -> TimeWindow:
    """Convert parsed argparse Namespace into a TimeWindow.

    Reads pre-parsed dates stashed by parse_args() — no re-parsing here.
    Default (neither --since nor --until given) → past 30 days.
    Either or both may be unbounded individually.
    """
    # Prefer the snapshot taken at parse_args time so validation and resolution
    # see the same `today` (kills midnight TOCTOU). Fall back to date.today()
    # only when resolve_time_window is invoked outside the normal CLI flow
    # (e.g., synthetic Namespace in a test).
    today = today or getattr(args, "_today", None) or date.today()
    if args.since is None and args.until is None:
        return TimeWindow(since=today - timedelta(days=DEFAULT_SINCE_DAYS), until=today)
    since_date = getattr(args, "_since_date", None)
    until_date = getattr(args, "_until_date", None)
    # Fallback path: if someone constructs args without going through parse_args.
    if args.since is not None and since_date is None:
        since_date = parse_when(args.since, today=today)
    if args.until is not None and until_date is None:
        until_date = parse_when(args.until, today=today)
    return TimeWindow(since=since_date, until=until_date)


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
