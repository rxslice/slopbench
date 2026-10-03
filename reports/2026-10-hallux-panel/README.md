# Where AI coding models invent packages: HALLUX panel, 22 Sep to 2 Oct 2026

Full write-up with charts: **https://blvkware.dev/ai-package-hallucination-data/**

Four AI model families (gpt-oss-120b, Qwen3.8 27B, Nemotron 3 Super, Gemini 2.5 Flash) were
given prompts from this repository's bank (`slopbench/data/prompts.json`: 30 prompts across
PyPI, npm and crates.io in three tiers, none naming a package), three samples per prompt at
temperature 0.7, a rotating third of the bank per day, on seven collection days. Packages were
read off the command surface only (`slopbench/extract.py`) and checked against the live
registries.

| | Suggestions | Invented | Rate | 95% Wilson interval |
|---|---:|---:|---:|---|
| Routine prompts | 582 | 1 | 0.17% | 0.03% to 0.97% |
| Niche prompts | 870 | 33 | 3.79% | 2.71% to 5.28% |
| Emerging prompts | 1,080 | 63 | 5.83% | 4.59% to 7.39% |
| **All** | **2,532** | **97** | **3.83%** | 3.15% to 4.65% |

- The four families are statistically indistinguishable (3.4% to 4.9%, overlapping intervals).
- 28 distinct invented names; 20 appeared once. The two most repeated came from three of the four
  families, in 17 samples each, on two days.
- Registrability on 2 Oct 2026: 19 of 28 could be registered by anyone (all 7 PyPI, all 4
  crates.io, 8 of 17 npm). npm org scopes (5), `@types` (1) and npm's punctuation rule (2)
  protected the rest; 1 unclear.
- One parsing artefact (`and` on PyPI) is excluded from every figure.

Names that anyone could register are deliberately not published here. Maintainers who want to
know whether models invent names around their project: russ@blvkware.dev.

## Files

- `hallux-panel-2026-10.csv`: suggestions, inventions and rates by tier and by model.
- `hallux-panel-2026-10-registrability.csv`: invented names by registry and what protected them.

Data licence: CC BY 4.0. Cite https://blvkware.dev/ai-package-hallucination-data/
