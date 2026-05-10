# perplexity-deep-research

## Why this exists

Sometimes you just need to know enough about a topic to have a real conversation — a market, a drug, a regulation, a city's housing data, a paper everyone's quoting. Not write a thesis. Just *be useful in the room*.

Perplexity Sonar Deep Research is the closest thing I've found to **expertise in a single API call**: a cited 5,000-word brief in about two minutes, with every fact backed by an inline link you can verify with one click. Independent benchmarks put its citation accuracy at **94–98%** — the highest of the major deep-research tools.

This CLI gives you that experience at the command line, pay-per-call through OpenRouter (~$1 per broad-topic brief). No subscription. No rate limit. No login flow. One Python file, one runtime dependency.

> Heads up — Perplexity Pro recently cut its Deep Research quota from unlimited to **20/month**. Calling Sonar Deep Research directly via OpenRouter, billed per call, is now both cheaper *and* more flexible for anyone running serious research.

**If this saves you time, leaving a ⭐ helps others find it.**

## A real run

```text
$ perplexity-deep-research "Canadian Sovereign AI" --since 1y
[pdr] Firing perplexity/sonar-deep-research for 'Canadian Sovereign AI'...
[pdr] Window: since 2025-05-10 (no upper bound)
[pdr] HTTP 200 in 128.9s
[pdr] Raw JSON saved to perplexity-deep-research-canadian-sovereign-ai-2026-05-10-1803.json
[pdr] Markdown summary saved to perplexity-deep-research-canadian-sovereign-ai-2026-05-10-1803.md
{
  "model": "perplexity/sonar-deep-research",
  "latency_s": 128.9,
  "synthesis_chars": 43721,
  "citation_count": 50,
  "usage": { "total_tokens": 8926, "cost": 0.85539 }
}
```

Top citation sources from that run: `canada.ca` (4), `ised-isde.canada.ca` (4), NRC (2), BCE (2) — primary federal sources, exactly what you want for policy research.

A markdown file with the full synthesis (typically 5,000–10,000 words) plus a numbered list of all 30–50 citations — ready to read, forward, or convert to PDF/DOCX/HTML with pandoc.

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

## Two ways to use it

There are really only two modes worth knowing about. Pick based on what you're trying to learn.

### 1. **Recency search** — "what's new about X?" *(default)*

You're trying to keep up with something that changes: news, markets, a regulation, a sport, a company. The default with no flags gives you the **past 30 days**, which is the right answer for most "what changed?" questions:

```bash
perplexity-deep-research "Toronto resale condos"      # past 30 days
perplexity-deep-research "GLP-1 weight loss drugs"    # past 30 days
perplexity-deep-research "Mark Carney AI policy"      # past 30 days
```

Want a different window? Use `--since`:

```bash
perplexity-deep-research "Q2 earnings season" --since 7d
perplexity-deep-research "Toronto resale condos" --since 1y       # past year
perplexity-deep-research "EU AI Act enforcement" --since 2024-09-01
```

`--since` accepts relative offsets (`7d`, `4w`, `3m`, `1y`) or ISO dates (`2025-04-01`). Use `--until` to bound the upper side too:

```bash
perplexity-deep-research "Mark Carney AI policy" --since 2025-04-01 --until 2026-04-30
```

### 2. **Cold-start research** — "what is X, and why should I care?"

You know *nothing* about the topic. You're not asking what changed — you're asking what *is*. You want canonical sources, foundational papers, the project's own docs — the stuff that's been true for years, not 30 days. Use `--since all`:

```bash
perplexity-deep-research "What is differential privacy?" --since all
perplexity-deep-research "Canadian Sovereign AI Compute Strategy" --since all
perplexity-deep-research "history of Rust async runtimes" --since all
```

Without `--since all`, the default 30-day window would force the model into "what happened with differential privacy *recently?*" framing — which is exactly wrong for a primer. The unbounded mode lets Perplexity find the best sources regardless of when they were written.

### How the two modes get to the API

Under the hood, `--since`/`--until` map to Perplexity's [official date filter parameters](https://docs.perplexity.ai/docs/sonar/filters) — `search_after_date_filter` and `search_before_date_filter`. These actually constrain which web pages Perplexity searches; they're not just hints to the model.

`--since all` (used alone, no `--until`) sends *neither* filter — Perplexity searches its full index. If you combine `--since all --until 2025-12-31`, the upper bound is still applied; only the lower bound is dropped.

## CLI reference

```text
perplexity-deep-research [-h] [-V] [-o OUTPUT]
                         [--since VALUE] [--until VALUE]
                         [--model M] [--env-file PATH] [--timeout S]
                         [--dry-run] [--no-json] [--quiet] topic
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `topic` (positional) | — | Text to research, e.g. `"Toronto resale condos"` |
| `-o`, `--output` | auto-named | Output markdown path |
| `--since` | none — default window is past 30 days **only** when both `--since` and `--until` are omitted | Lower bound. ISO date (`2026-01-01`), relative (`7d`/`4w`/`3m`/`1y`), or `all` for cold-start. |
| `--until` | today **only** when both bounds are omitted; otherwise omitting `--until` means open upper bound | Upper bound. Same formats as `--since`. |
| `--model` | `perplexity/sonar-deep-research` | Any OpenRouter chat-completions model id |
| `--env-file` | `.env` (cwd, walks up) | Where to look for `OPENROUTER_API_KEY` |
| `--timeout` | `600` | Socket-recv timeout in seconds |
| `--dry-run` | off | **No API call, no charge.** Print prompt and would-be filename. |
| `--no-json` | off | Skip writing the raw `.json` (markdown only) |
| `-q`, `--quiet` | off | Suppress progress messages on stderr |
| `-V`, `--version` | — | Print version and exit |

### `--dry-run` is your friend before you spend

Before firing the first paid call on a new topic, run with `--dry-run` to confirm the prompt, the time window, and the auto-generated filename look right:

```bash
perplexity-deep-research "Q2 earnings season" --since 7d --dry-run
```

It prints the exact JSON body that *would* be sent to OpenRouter — including the date filters, the model id, and the path it would write to. No tokens spent.

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

## How it compares to ChatGPT, Gemini, and Claude Deep Research

Now that you've used it, here's how Perplexity Sonar Deep Research sits next to the other deep-research tools — based on actually running each in real workflows, not a leaderboard:

| Tool | Strength | Friction |
| --- | --- | --- |
| **ChatGPT Deep Research** | Deepest synthesis, longest reports — factually solid | Reads dry — strong on facts, light on the human angle; gated behind paid plans |
| **Gemini Deep Research** | Widest source coverage (100+ pages); reads like a scholarly literature review | Dense, university-research voice — takes patience to get through |
| **Claude Deep Research** (claude.ai) | Distinct angles — frequently surfaces what the other two miss | Slow: 30–40 minutes per query, easy to lose patience waiting |
| **Perplexity Sonar Deep Research** | The most *readable* brief — fast (2–4 min), inline citations on every claim | Less synthesis-rich than the big three; not built for original analysis |

**The honest take.** The highest-quality output comes from running the big three on the same question and synthesizing the answers via a Venn-diagram pass — convergence + unique insights + outliers. I wrote a [Multi-AI Research Synthesis prompt](https://github.com/dzivkovi/AI-assisted-SDLC-Project-scaffolding/blob/main/prompts/Multi-AI_Research_synthesis_prompt.md) for exactly that workflow. But that's the obsessive route — three subscriptions, an hour of waiting, and a synthesis pass.

For everyone else — anyone in a rush, anyone who just needs to be useful in the room — Perplexity Sonar Deep Research is the right tool. It's not the deepest. It's the one where the 80/20 is good enough, the citations are inline, and the answer arrives in two minutes.

## Resources

- **OpenRouter dashboard** — sign up, fund balance, get key: <https://openrouter.ai/>
- **Sonar Deep Research model card on OpenRouter** (pricing, model spec, request format): <https://openrouter.ai/perplexity/sonar-deep-research>
- **Perplexity's own Sonar Deep Research docs** (model behavior, prompting tips): <https://docs.perplexity.ai/docs/sonar/models/sonar-deep-research>
- **OpenRouter quickstart** (the chat-completions endpoint format this tool uses): <https://openrouter.ai/docs/quickstart>

## License

MIT
