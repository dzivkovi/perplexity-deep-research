"""Single-shot Perplexity Deep Research CLI via OpenRouter."""

from .cli import (
    Citation,
    SynthesisResult,
    __version__,
    build_prompt,
    build_request_body,
    default_output_path,
    main,
    parse_response,
    render_markdown,
    resolve_api_key,
    slugify,
)

__all__ = [
    "Citation",
    "SynthesisResult",
    "__version__",
    "build_prompt",
    "build_request_body",
    "default_output_path",
    "main",
    "parse_response",
    "render_markdown",
    "resolve_api_key",
    "slugify",
]
