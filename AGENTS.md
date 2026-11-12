# AGENTS.md

Notes for AI coding agents working in this repository.

## What this is

slopbench measures package hallucination in model output: how often a model
names a PyPI, npm or crates.io package that does not exist, and how often the
same invented name recurs across samples of one prompt. A name that recurs in at
least two samples is "targetable". The `README.md` explains why that is the
measurement.

## Setup and checks

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The tests make no network calls and need no API keys. CI runs them on Python
3.9 and 3.12 (`.github/workflows/ci.yml`). The package has no runtime
dependencies. Keep it that way: it uses only the standard library.

## Layout

| Path | Role |
|---|---|
| `slopbench/cli.py` | `slopbench run` and `slopbench prompts`. Exit 1 means a targetable name was found; exit 2 means nothing was measured. |
| `slopbench/providers.py` | Model adapters (`ollama`, `anthropic`, `openai`-compatible) and the on-disk completion cache. |
| `slopbench/extract.py` | Reads package names from model output. It is conservative on purpose. |
| `slopbench/verify.py` | Registry checks. pkgguard is used when installed, with a direct-API fallback. |
| `slopbench/bench.py` | Sampling, recurrence counting, and pkgguard corpus output and merging. |
| `slopbench/report.py` | HTML and Markdown reports. |
| `slopbench/data/prompts.json` | The 30-prompt corpus. No prompt may name a package; a test enforces this. |
| `reports/` | Published aggregate results (CC BY 4.0). |

## Rules that protect the numbers

- A registry failure is never a pass. Unverifiable names are excluded and
  reported, never scored as clean.
- Do not widen extraction to running prose. Install commands are read only from
  fenced blocks, inline code and lines that begin with a package manager.
- Do not publish invented names that anyone could register. `reports/` holds
  aggregates only.
- Any change to what counts as invented or targetable needs a test showing the
  input that motivated it.
