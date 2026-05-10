# perplexity-deep-research

A one-shot CLI for Perplexity's **Sonar Deep Research** model via OpenRouter. Give it a topic, get back a multi-page cited research brief saved to markdown.

```text
$ perplexity-deep-research "Toronto real estate"
[pdr] Firing perplexity/sonar-deep-research for 'Toronto real estate'...
[pdr] Window: 2026-04-07 to 2026-05-07
[pdr] Cost expectation: ~$0.30 to $0.90 per call
[pdr] Latency expectation: 1-5+ minutes (Deep Research is slow by design)
[pdr] HTTP 200 in 126.9s
[pdr] Raw JSON saved to perplexity-deep-research-toronto-real-estate-2026-05-07-2222.json
[pdr] Markdown summary saved to perplexity-deep-research-toronto-real-estate-2026-05-07-2222.md
{
  "model": "perplexity/sonar-deep-research",
  "latency_s": 126.9,
  "synthesis_chars": 34215,
  "citation_count": 41,
  "usage": { "prompt_tokens": 37, "completion_tokens": 8918, "total_tokens": 8955 }
}
```

## What it does in one sentence

Sends a single Sonar Deep Research request to OpenRouter for any topic you name, captures the synthesis (typically 5,000–10,000 words), deduplicates URL citations (typically 30–50 per query), and writes a readable markdown summary plus the raw JSON response to your current directory.

## Quick start

```bash
# 1. Install (Python 3.8+, one runtime dep: python-dotenv)
pip install git+https://github.com/dzivkovi/perplexity-deep-research.git
# Already cloned the repo? See "Development" below for the editable-install path.

# 2. Get an OpenRouter key at https://openrouter.ai and put it in your shell
export OPENROUTER_API_KEY=sk-or-...

# 3. Research anything
perplexity-deep-research "GLP-1 weight loss drugs"
```

That's it. Two minutes later you'll have a `.md` file with a research brief and 30+ citations, and a `.json` file with the raw response if you ever want to re-render it.

## What you should expect to spend

OpenRouter bills per token plus a small per-search fee:

| Per-call cost | Typical (narrow topic) | Broad query, deep reasoning |
| --- | --- | --- |
| Token cost | ~$0.10 | ~$0.20 |
| Per-search fees | ~$0.20 | ~$0.40 |
| **Total** | **~$0.30** | **~$1.00–1.20** |

Rule of thumb: budget **$0.50 per call** for narrow topics and **$1.00 per call** for broad ones. Real data point: a "GLP-1 weight loss drugs" query in May 2026 returned a 7,500-word brief with 43 citations (11,284 completion tokens) and OpenRouter billed `$1.00535`. Verify the `usage` block in the printed JSON tally against [OpenRouter's posted pricing](https://openrouter.ai/perplexity/sonar-deep-research) if you want exact numbers.

## What you should expect to wait

Sonar Deep Research is non-deterministic latency by design — it does multi-step web crawls and synthesis.

- **0–60s:** Routing + planning. No output yet.
- **1–4 min:** Typical synthesis window.
- **4–8 min:** Still normal for broad queries.
- **8 min+:** Investigate; OpenRouter may be queueing under load.

The default socket-recv timeout is 600 seconds (10 minutes). Adjust with `--timeout` if needed. Caveat: this is a *per-recv* timeout, not a total-elapsed timeout — as long as the server sends any byte (even keepalive), the timer resets.

## What you get back

Two files (or one with `--no-json`):

```text
perplexity-deep-research-<topic-slug>-YYYY-MM-DD-HHMM.md     ← human-readable
perplexity-deep-research-<topic-slug>-YYYY-MM-DD-HHMM.json   ← raw response
```

The `.md` file contains:

- **Run metadata** — model, time window, latency, HTTP status, token usage
- **Synthesis** — the model's full multi-section essay
- **Citations** — numbered list of unique URLs with titles, in citation order

The `.json` file is the verbatim OpenRouter response — useful if you want to re-render markdown later, count tokens, audit citations, or feed the result into another tool.

The auto-generated filename includes a minute-precision timestamp so re-running the same topic the next day produces a new file rather than overwriting (handy for tracking how a topic evolves).

## Convert to PDF, DOCX, HTML, etc. (pandoc)

The output is plain markdown, so [pandoc](https://pandoc.org/) handles every conversion you'd want:

```bash
# PDF (requires a LaTeX engine like TeX Live or MiKTeX)
pandoc perplexity-deep-research-toronto-real-estate-2026-05-07-2222.md -o brief.pdf

# Microsoft Word
pandoc brief.md -o brief.docx

# HTML with embedded styling
pandoc brief.md -s -o brief.html

# EPUB (for tablet / Kindle reading)
pandoc brief.md -o brief.epub
```

The citations in the markdown are inline links, so pandoc preserves them in every output format — clickable in HTML/PDF/DOCX, footnote-style in print.

## Configuration

Only one secret is needed: `OPENROUTER_API_KEY`. The CLI looks for it in this order:

1. Environment variable `OPENROUTER_API_KEY`
2. A `.env` file in the current directory (or any parent directory — `.env` discovery walks up the tree, courtesy of [python-dotenv](https://github.com/theskipper/python-dotenv))
3. An explicit path passed via `--env-file PATH`

Either:

```bash
# Shell-scoped
export OPENROUTER_API_KEY=sk-or-...

# Or per-project — copy the example and edit
cp .env.example .env
$EDITOR .env
chmod 600 .env   # tighten perms (file holds a paid key)
```

The included `.env.example` is the canonical template — single line, no surprises. `.env` is gitignored by default; `.env.example` is checked in.

If a shell env var is already set, it wins — `.env` cannot override it (`override=False`). That keeps CI / production env vars authoritative even when a stray `.env` ends up in the working dir.

## CLI reference

```text
perplexity-deep-research [-h] [-V] [-o OUTPUT] [--days N] [--model M]
                         [--env-file PATH] [--timeout S] [--dry-run]
                         [--no-json] [--quiet] topic
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `topic` (positional) | — | Text to research, e.g. `"Toronto real estate"` |
| `-o`, `--output` | auto-named | Output markdown path |
| `--days` | `30` | Research window in days (passed into prompt as a date range) |
| `--model` | `perplexity/sonar-deep-research` | Any OpenRouter chat-completions model id |
| `--env-file` | `.env` (cwd, walks up) | Where to look for `OPENROUTER_API_KEY` |
| `--timeout` | `600` | Socket-recv timeout in seconds |
| `--dry-run` | off | **No API call, no charge.** Print prompt and would-be filename. |
| `--no-json` | off | Skip writing the raw `.json` (markdown only) |
| `-q`, `--quiet` | off | Suppress progress messages on stderr |
| `-V`, `--version` | — | Print version and exit |

### `--dry-run` is your friend before you spend

Before firing the first paid call on a new topic, run with `--dry-run` to confirm the prompt and the auto-generated filename look right:

```bash
perplexity-deep-research "Q2 earnings season" --dry-run
```

It prints the exact JSON body that *would* be sent to OpenRouter — including the model id, the date range it baked into the prompt, and the path it would write to. No tokens spent.

## Why does it call Perplexity through OpenRouter?

OpenRouter is a wholesale router that sits between your code and ~50 model providers (Anthropic, OpenAI, Google, **Perplexity**, Meta, etc.). Calling Sonar Deep Research through OpenRouter means:

- One API key for many providers — including Perplexity, Anthropic, OpenAI, Google, etc.
- Single billing dashboard
- No need for a separate Perplexity API account or `PERPLEXITY_API_KEY`

You can also call Perplexity directly with their native API key — but this CLI doesn't (yet) support that path. The OpenRouter-only design is a deliberate tradeoff for setup simplicity.

If you want a deeper explanation of where Sonar Deep Research fits in the Perplexity model family, see the **Resources** section at the bottom.

## Updating to the latest version

```bash
pip install --upgrade git+https://github.com/dzivkovi/perplexity-deep-research.git
perplexity-deep-research --version
```

## Development

If you cloned the repo (instead of `pip install`-ing from GitHub), this is the path to set up local execution and tests:

```bash
git clone https://github.com/dzivkovi/perplexity-deep-research.git
cd perplexity-deep-research
pip install -e ".[dev]"                # editable install + test deps (pytest)

# Verify the CLI is wired up
perplexity-deep-research --version

# Run tests (no API calls — fully mocked, costs $0)
pytest -v
```

The `-e` flag points pip at your source tree, so any edit to `src/perplexity_deep_research/cli.py` shows up on the next run with no reinstall needed. Uninstall any time with `pip uninstall perplexity-deep-research`.

The package has **one runtime dependency** — [python-dotenv](https://github.com/theskipper/python-dotenv) for `.env` file loading. Everything else is stdlib (`urllib`, `json`, `argparse`). Tests use `pytest` + stdlib `unittest.mock`; the suite is fully mocked at the network boundary, runs in under a second, and never spends a token.

## How this came to be

This CLI started as a one-off probe written to see what Sonar Deep Research actually returns when you give it a single broad query and let it run. The first call came back with a 6,800-word brief and 41 citations — clearly useful on its own, with no surrounding pipeline needed. So the probe got generalized into a standalone CLI: one topic in, one cited markdown brief out, no scaffolding required.

## Resources

- **OpenRouter dashboard** — sign up, fund balance, get key: <https://openrouter.ai/>
- **Sonar Deep Research model card on OpenRouter** (pricing, model spec, request format): <https://openrouter.ai/perplexity/sonar-deep-research>
- **Perplexity's own Sonar Deep Research docs** (model behavior, prompting tips): <https://docs.perplexity.ai/docs/sonar/models/sonar-deep-research>
- **OpenRouter quickstart** (the chat-completions endpoint format this tool uses): <https://openrouter.ai/docs/quickstart>

## License

MIT
