# slopbench — measure package hallucination in model output

**Built by [Blvkware](https://blvkware.dev)**

[![CI](https://github.com/rxslice/slopbench/actions/workflows/ci.yml/badge.svg)](https://github.com/rxslice/slopbench/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

slopbench measures how often a language model or coding agent names a software
dependency that no established package answers to, and — the part that matters —
how often it names the same one twice.

```bash
pip install -e .
slopbench run --provider ollama --model qwen2.5-coder:7b --samples 5 --report out.html
```

## Why recurrence is the measurement

A hallucination rate on its own overstates the threat. A package name a model
invents once and never again cannot be pre-registered by anyone, because nobody
can predict it. It breaks a build, loudly, and someone fixes it.

The dangerous names are the ones that come back. A name that recurs across
independent samples of the same prompt is a name an attacker can reproduce,
register, and wait on. That is the entire mechanism behind slopsquatting, and
it is why published hallucination rates and published *risk* are different
numbers.

So slopbench samples every prompt several times at a real temperature, and only
calls an invented name **targetable** once it has appeared in at least two of
those samples. One-off inventions are reported, clearly labelled, and kept out
of the headline.

## What it reports

| Status | Meaning |
|---|---|
| `does not exist` | No package by that name on the registry. Breaks the build. |
| `exists, unadopted` | A package by that name exists, with no adoption behind it. The install succeeds silently. This is the serious one. |
| excluded | The registry could not be reached for this name. Never counted as clean. |

Three things that would each be easy to get wrong, and are deliberately not:

- **A registry failure never becomes a pass.** Unverifiable names are excluded
  from every count and stated in the report, rather than quietly scored as fine.
- **`exists` is not reassurance.** A name a model invents that also exists, with
  one version and no downloads, is worse than one that 404s.
- **Age does not clear a name.** A package published once and never updated,
  still collecting almost nothing, is not established because it got old. Sitting
  untouched is what a planted name does while it waits.

## Extraction is conservative on purpose

Every number rests on deciding what counts as "the model suggested a package,"
so the extractor drops anything it cannot stand behind:

- Standard library, Node builtins, and Rust `std` — `import asyncio` is not a
  dependency. The Python list ships as data and unions across versions.
- Relative and absolute imports, and modules the response defines itself. A model
  that writes `utils.py` and then imports from it wrote coherent code.
- Import names that differ from distribution names: `import yaml` installs
  `PyYAML`, `import cv2` installs `opencv-python`. Reporting `yaml` as invented
  is the fastest possible way to lose a reader.

The counts are a floor, not a ceiling. That is the right direction for a number
you intend to show someone about their own product.

## Providers

| Provider | Configure with | Notes |
|---|---|---|
| `ollama` | `OLLAMA_HOST` | Local, free. Start here. |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `SLOPBENCH_BASE_URL`, `SLOPBENCH_API_KEY` | Any `/v1/chat/completions` endpoint: OpenAI, Groq, Together, OpenRouter, vLLM, LM Studio. |

Every completion is cached to disk keyed by provider, model, prompt,
temperature, and sample index. Generation is the only step that costs money;
extraction and scoring are free and get iterated on constantly. A scoring change
re-runs for nothing, and a published result stays reproducible from the cache
after the endpoint behind it has moved on.

## Usage

```bash
# see the corpus without running anything
slopbench prompts --ecosystem npm

# a cheap first run: one ecosystem, a few prompts
slopbench run --provider ollama --model qwen2.5-coder:7b \
  --ecosystem npm --limit 6 --samples 5 --report out.html

# full run with every output
slopbench run --provider anthropic --model claude-sonnet-4-6 \
  --samples 5 --report out.html --markdown out.md \
  --json out.json --corpus corpus.json --subject "Acme Agents"
```

Exits `1` when any targetable name was found, so it drops into CI as a
regression gate on your own agent.

`--corpus` writes recurring findings in
[pkgguard](https://github.com/rxslice/pkgguard-API)'s known-hallucination
format. Only names that recurred are emitted — pkgguard's own documentation
warns that a padded corpus produces confident wrong blocks, and a name invented
once is not evidence of anything.

## pkgguard is optional

If pkgguard is installed, slopbench uses it for registry scoring and gets its
conflation detection and adoption-ratio typosquat confirmation. If it is not,
slopbench falls back to direct registry calls with a deliberately simpler
classifier. The fallback is weaker and says so: it will call some genuine
squats established that pkgguard catches.

```bash
pip install -e ".[pkgguard]"
```

## The prompt corpus

30 prompts across PyPI, npm, and crates.io, in three tiers:

- **routine** — well-covered tasks. This is the control. Invention here means
  the problem is not confined to obscure corners.
- **niche** — specific integrations with real but thinner public coverage.
- **emerging** — areas where training data is thinnest, which is where invention
  concentrates.

No prompt names a package. A prompt that names a library measures
instruction-following, not invention, and a test enforces this against the
corpus.

Point `--prompts` at your own file to benchmark your own agent's real workload.
The corpus is the part worth customizing.

## Limits

- One model, one date, one set of conditions. Re-running after a model update
  gives a different number, and it should.
- Recurrence is measured within a prompt, so a low sample count understates it.
  Below `--samples 2` the measurement does not exist and the CLI refuses.
- This measures what a model *says*. An agent with retrieval, a lockfile, or a
  private registry may never act on it.
- Finding zero targetable names is a real result and the report says so plainly
  rather than manufacturing a concern.

## License

Apache 2.0. Built by [Blvkware](https://blvkware.dev).
