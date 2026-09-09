"""slopbench — built by Blvkware (https://blvkware.dev). Command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .bench import (
    CorpusFormatError,
    Result,
    count_corpus_names,
    load_prompts,
    merge_pkgguard_corpus,
    run,
    to_pkgguard_corpus,
)
from .providers import ProviderError, build
from .report import render_html, render_markdown


def _write_corpus(path: str, result: Result) -> int:
    """Write, or extend, the pkgguard corpus at `path`. Returns names added.

    Merging when the file already exists is the default rather than an option,
    because the corpus is the part that accumulates across runs: a scheduled run
    that happened to see nothing must not erase what an earlier one found.
    """
    incoming = to_pkgguard_corpus(result)
    existing = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)

    before = count_corpus_names(existing) if existing is not None else 0
    merged = merge_pkgguard_corpus(existing, incoming) if existing is not None else incoming

    with open(path, "w", encoding="utf-8") as fh:
        print(json.dumps(merged, indent=2), file=fh)
    return count_corpus_names(merged) - before


def _filter(prompts, ecosystem: Optional[str], tier: Optional[str], limit: Optional[int]):
    if ecosystem:
        prompts = [p for p in prompts if p.get("ecosystem") == ecosystem]
    if tier:
        prompts = [p for p in prompts if p.get("tier") == tier]
    return prompts[:limit] if limit else prompts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slopbench",
        description="Measure how often a model invents software dependencies, "
                    "and how often it invents the same one twice.",
    )
    parser.add_argument("--version", action="version", version=f"slopbench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run the benchmark and write a report.")
    r.add_argument("--provider", default="ollama", choices=["anthropic", "openai", "ollama"])
    r.add_argument("--model", required=True)
    r.add_argument("--samples", type=int, default=5,
                   help="Completions per prompt. Below 2, recurrence cannot be measured.")
    r.add_argument("--temperature", type=float, default=0.7)
    r.add_argument("--ecosystem", choices=["pypi", "npm", "crates"])
    r.add_argument("--tier", choices=["routine", "niche", "emerging"])
    r.add_argument("--limit", type=int, help="Use only the first N prompts.")
    r.add_argument("--prompts", help="Path to a custom prompt corpus.")
    r.add_argument("--report", help="Write an HTML report here.")
    r.add_argument("--markdown", help="Write a Markdown report here.")
    r.add_argument("--json", dest="json_path", help="Write the raw result here.")
    r.add_argument("--corpus",
                   help="Write recurring findings in pkgguard's known-hallucination "
                        "format. Merges into the file if it already exists.")
    r.add_argument("--subject", default="", help="Who this report is about, for the header.")
    r.add_argument("--no-pkgguard", action="store_true",
                   help="Use public registry APIs directly even if pkgguard is installed.")
    r.add_argument("--quiet", action="store_true")

    p = sub.add_parser("prompts", help="List the prompt corpus.")
    p.add_argument("--ecosystem", choices=["pypi", "npm", "crates"])
    p.add_argument("--tier", choices=["routine", "niche", "emerging"])

    args = parser.parse_args(argv)

    if args.command == "prompts":
        for prompt in _filter(load_prompts(), args.ecosystem, args.tier, None):
            print(f"{prompt['id']:18} {prompt['tier']:9} {prompt['ecosystem']:7} {prompt['text'][:70]}")
        return 0

    if args.samples < 2:
        print("--samples must be at least 2; recurrence is the measurement.", file=sys.stderr)
        return 2

    prompts = _filter(load_prompts(args.prompts), args.ecosystem, args.tier, args.limit)
    if not prompts:
        print("No prompts matched those filters.", file=sys.stderr)
        return 2

    try:
        provider = build(args.provider, args.model)
    except ProviderError as error:
        print(str(error), file=sys.stderr)
        return 2

    def progress(message: str) -> None:
        if not args.quiet:
            print(f"  {message}", file=sys.stderr, end="\r")

    result = run(provider, prompts, samples=args.samples, temperature=args.temperature,
                 use_pkgguard=not args.no_pkgguard, progress=progress)

    if not args.quiet:
        print(" " * 60, file=sys.stderr, end="\r")

    if result.total_samples == 0:
        # Zero completions is not a clean result. Falling through would write a
        # report headlined "0 recurring invented names" and exit 0 - a clean
        # bill of health manufactured out of a missing API key.
        print("No completions were obtained; nothing was measured. "
              "No report written.", file=sys.stderr)
        for error in result.errors[:5]:
            print(f"error: {error}", file=sys.stderr)
        return 2

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(render_html(result, args.subject))
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(result, args.subject))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2)
    if args.corpus:
        try:
            added = _write_corpus(args.corpus, result)
        except (CorpusFormatError, ValueError) as error:
            print(f"corpus: refusing to write {args.corpus}: {error}", file=sys.stderr)
            return 2
        print(f"corpus: {added} new name(s) written to {args.corpus}")

    print(f"{result.model}: {result.targetable_distinct} recurring invented "
          f"name(s); {result.invented_distinct} invented of "
          f"{result.distinct_packages} distinct packages across "
          f"{result.total_samples} completions.")
    if result.unverifiable_distinct:
        print(f"{result.unverifiable_distinct} name(s) unverifiable and excluded.")
    for error in result.errors[:5]:
        print(f"error: {error}", file=sys.stderr)

    return 1 if result.targetable_distinct else 0


if __name__ == "__main__":
    raise SystemExit(main())
