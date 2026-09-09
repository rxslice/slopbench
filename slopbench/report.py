"""
slopbench — built by Blvkware (https://blvkware.dev)

Render a run as a document someone will forward.

This is the part that has to survive a hostile reader. The recipient did not
ask for this report, and their first instinct is to find the flaw in it. So the
document is built like a test certificate rather than a pitch: conditions of
measurement stated before the result, every finding reproducible from the
command printed at the bottom, and the limits section written to be genuinely
limiting rather than a disclaimer.

Fonts are system stacks and there are no external requests. The file has to
render inside a corporate network that blocks third-party origins, offline, and
after being saved to PDF and mailed on.
"""
from __future__ import annotations

import html
from typing import List

from .bench import Finding, Result

CSS = """
:root {
  --paper: #ffffff;
  --ink: #16181d;
  --muted: #606874;
  --rule: #dfe3e9;
  --oxide: #a32e22;
  --ochre: #8a6d14;
  --measure: 68ch;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 3.5rem 1.5rem 6rem;
  background: var(--paper);
  color: var(--ink);
  font-family: Charter, "Bitstream Charter", "Iowan Old Style", Georgia, serif;
  font-size: 17px;
  line-height: 1.62;
  -webkit-text-size-adjust: 100%;
}
.sheet { max-width: 78rem; margin: 0 auto; }
p, li { max-width: var(--measure); }
h1, h2 { font-weight: 600; letter-spacing: -0.012em; line-height: 1.2; }
h1 { font-size: 1.6rem; margin: 0 0 0.35rem; }
h2 { font-size: 1.12rem; margin: 3.25rem 0 0.9rem; }
a { color: var(--oxide); }

.masthead { border-top: 2px solid var(--ink); padding-top: 0.9rem; }
.subject { color: var(--muted); margin: 0; max-width: var(--measure); }

.headline { margin: 2.75rem 0 0; display: flex; align-items: baseline; gap: 1.25rem; flex-wrap: wrap; }
.figure {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-size: clamp(4.25rem, 15vw, 7.5rem);
  line-height: 0.84;
  letter-spacing: -0.045em;
  color: var(--oxide);
}
.figure.clear { color: var(--ink); }
.figure-note { max-width: 30ch; color: var(--muted); font-size: 0.95rem; line-height: 1.45; }
.verdict { margin: 1.5rem 0 0; font-size: 1.05rem; }

.conditions { margin: 1.5rem 0 0; border-top: 1px solid var(--rule); }
.conditions div {
  display: flex; gap: 1rem; justify-content: space-between;
  padding: 0.42rem 0; border-bottom: 1px solid var(--rule);
  max-width: 34rem; font-size: 0.94rem;
}
.conditions dt { color: var(--muted); margin: 0; }
.conditions dd {
  margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums; font-size: 0.88rem; text-align: right;
}

table { border-collapse: collapse; width: 100%; margin-top: 0.75rem; font-size: 0.92rem; }
caption { text-align: left; color: var(--muted); padding-bottom: 0.6rem; max-width: var(--measure); }
th, td { text-align: left; padding: 0.5rem 0.85rem 0.5rem 0; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { font-weight: 600; border-bottom: 1px solid var(--ink); white-space: nowrap; }
td.pkg, td.num, td.eco {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums; font-size: 0.86rem;
}
td.num, th.num { text-align: right; white-space: nowrap; }
tr.targetable td.pkg { color: var(--oxide); font-weight: 600; }
td.note { color: var(--muted); font-size: 0.84rem; line-height: 1.4; }
.status-not_found { color: var(--oxide); }
.status-suspicious { color: var(--ochre); }

pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.84rem; line-height: 1.55; background: #f6f7f9;
  border-left: 2px solid var(--rule); padding: 0.85rem 1rem;
  overflow-x: auto; max-width: var(--measure);
}
.limits li { margin-bottom: 0.5rem; }
footer { margin-top: 4rem; padding-top: 1rem; border-top: 1px solid var(--rule); color: var(--muted); font-size: 0.88rem; }

@media print {
  body { padding: 0; font-size: 11pt; }
  .figure { font-size: 3.4rem; }
  h2 { break-after: avoid; }
  tr { break-inside: avoid; }
}
"""


def _e(value) -> str:
    return html.escape(str(value))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _verdict_sentence(result: Result) -> str:
    n = result.targetable_distinct
    model = f"{result.model}"
    if n == 0:
        if result.invented_distinct == 0:
            return (
                f"Across {result.total_samples} samples, {model} named no package that "
                f"does not resolve to an established entry on its registry. Nothing here "
                f"needs action."
            )
        return (
            f"{model} invented {result.invented_distinct} package "
            f"{'name' if result.invented_distinct == 1 else 'names'}, but none recurred "
            f"across independent samples of the same prompt. Invented names that do not "
            f"repeat cannot be predicted, so they cannot be pre-registered. This is a "
            f"build-breakage finding, not a supply-chain one."
        )
    return (
        f"{model} invented {n} package "
        f"{'name that recurred' if n == 1 else 'names that recurred'} across independent "
        f"samples of the same prompt. Recurrence is the property that makes a hallucinated "
        f"name registrable: an attacker who can reproduce the name can claim it and wait "
        f"for the install."
    )


def _findings_rows(findings: List[Finding], samples: int) -> str:
    rows = []
    for f in findings:
        status = "does not exist" if f.status == "not_found" else "exists, unadopted"
        notes = "; ".join(f.signals[:3]) if f.signals else ""
        if f.status == "not_found" and not notes:
            notes = "unclaimed today, registrable tomorrow"
        rows.append(
            f'<tr class="{"targetable" if f.targetable else ""}">'
            f'<td class="pkg">{_e(f.name)}</td>'
            f'<td class="eco">{_e(f.ecosystem)}</td>'
            f'<td class="num">{f.max_repeat_in_prompt}/{samples}</td>'
            f'<td class="num">{len(f.prompt_ids)}</td>'
            f'<td class="status-{_e(f.status)}">{status}</td>'
            f'<td class="note">{_e(notes)}</td></tr>'
        )
    return "\n".join(rows)


def render_html(result: Result, subject: str = "") -> str:
    targetable = [f for f in result.findings if f.targetable]
    shown = targetable if targetable else result.findings[:12]
    figure_class = "figure" if targetable else "figure clear"

    subject_line = (
        f"Package-hallucination measurement for {_e(subject)}."
        if subject else "Package-hallucination measurement."
    )

    conditions = [
        ("Model", f"{result.provider}/{result.model}"),
        ("Prompts", result.prompts_run),
        ("Samples per prompt", result.samples),
        ("Temperature", result.temperature),
        ("Completions analysed", result.total_samples),
        ("Package references extracted", result.suggestion_occurrences),
        ("Distinct packages named", result.distinct_packages),
        ("Measured", result.started),
    ]
    if result.unverifiable_distinct:
        conditions.append(("Names registry could not confirm", result.unverifiable_distinct))

    tier_rows = "\n".join(
        f'<tr><td>{_e(tier)}</td><td class="num">{c["occurrences"]}</td>'
        f'<td class="num">{c["invented"]}</td><td class="num">{_pct(c["rate"])}</td></tr>'
        for tier, c in result.by_tier.items()
    )

    table_caption = (
        "Every row recurred across independent samples of one prompt."
        if targetable else
        "No name recurred across samples. Listed for completeness; none is targetable."
    )

    unverifiable_note = ""
    if result.unverifiable_distinct:
        unverifiable_note = (
            f"<p>{result.unverifiable_distinct} distinct "
            f"{'name' if result.unverifiable_distinct == 1 else 'names'} could not be "
            f"confirmed against the registry during this run and "
            f"{'is' if result.unverifiable_distinct == 1 else 'are'} excluded from every "
            f"count above rather than assumed clean.</p>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Package hallucination measurement — {_e(result.model)}</title>
<style>{CSS}</style></head>
<body><div class="sheet">

<header class="masthead">
  <h1>Package hallucination measurement</h1>
  <p class="subject">{subject_line} Measures how often the model names a software
  dependency that no established package answers to, and how often it names the
  same one twice.</p>
</header>

<div class="headline">
  <div class="{figure_class}">{result.targetable_distinct}</div>
  <p class="figure-note">recurring invented package names, out of
  {result.distinct_packages} distinct packages the model named across
  {result.total_samples} completions.</p>
</div>

<p class="verdict">{_verdict_sentence(result)}</p>

<h2>Conditions</h2>
<dl class="conditions">
{"".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in conditions)}
</dl>

<h2>Findings</h2>
<table>
  <caption>{table_caption}</caption>
  <thead><tr>
    <th>Package</th><th>Registry</th><th class="num">Recurrence</th>
    <th class="num">Prompts</th><th>Status</th><th>Observed</th>
  </tr></thead>
  <tbody>
{_findings_rows(shown, result.samples) or '<tr><td colspan="6">No invented package names were extracted.</td></tr>'}
  </tbody>
</table>
{unverifiable_note}

<h2>Where it happened</h2>
<p>Prompts are grouped by how well covered the task is in public code. Routine
tasks are the control: invention there means the failure is not confined to
obscure corners of the ecosystem.</p>
<table>
  <thead><tr><th>Task type</th><th class="num">References</th>
  <th class="num">Invented</th><th class="num">Rate</th></tr></thead>
  <tbody>{tier_rows}</tbody>
</table>

<h2>What this does and does not show</h2>
<ul class="limits">
  <li>An invented name that recurs can be registered by anyone. Several
  documented npm and PyPI packages exist today for exactly this reason, placed
  after the name was observed in model output.</li>
  <li>A name marked <span class="status-suspicious">exists, unadopted</span> is
  the more serious case, not the milder one. The install succeeds and nothing
  fails loudly.</li>
  <li>This measures one model under one set of conditions on one date. It is not
  a claim about the model family, and re-running it after a model update will
  give a different number.</li>
  <li>Names are extracted conservatively. Standard-library modules, relative
  imports, files the response defines itself, and import names that differ from
  their distribution names are all excluded, so the counts here are a floor
  rather than a ceiling.</li>
  <li>Nothing here says the model is unsafe to use. It says that between the
  model's output and a package manager there is an unguarded step.</li>
</ul>

<h2>Reproduce this</h2>
<p>The corpus, the extraction rules, and the scoring are open. Every completion
behind this report is cached, so the analysis re-runs without spending anything.</p>
<pre>slopbench run --provider {_e(result.provider)} --model {_e(result.model)} \\
  --samples {result.samples} --temperature {result.temperature} --report out.html</pre>

<footer>
Generated by slopbench, built by <a href="https://blvkware.dev">Blvkware</a>.
Registry verification {"via pkgguard" if result.pkgguard_available else "via public registry APIs"}.
Run completed in {result.duration_seconds}s.
</footer>

</div></body></html>"""


def render_markdown(result: Result, subject: str = "") -> str:
    lines = [
        f"# Package hallucination measurement — {result.model}",
        "",
        (f"Subject: {subject}" if subject else "").strip(),
        "",
        f"**{result.targetable_distinct} recurring invented package names** out of "
        f"{result.distinct_packages} distinct packages named across "
        f"{result.total_samples} completions.",
        "",
        _verdict_sentence(result),
        "",
        "## Conditions",
        "",
        f"- Model: `{result.provider}/{result.model}`",
        f"- Prompts: {result.prompts_run}, sampled {result.samples}x at temperature {result.temperature}",
        f"- Package references extracted: {result.suggestion_occurrences}",
        f"- Measured: {result.started}",
    ]
    if result.unverifiable_distinct:
        lines.append(
            f"- Excluded as unverifiable: {result.unverifiable_distinct} "
            f"(registry unreachable; not counted as clean)"
        )
    lines += ["", "## Findings", "",
              "| Package | Registry | Recurrence | Prompts | Status |",
              "|---|---|---|---|---|"]
    shown = [f for f in result.findings if f.targetable] or result.findings[:12]
    for f in shown:
        status = "does not exist" if f.status == "not_found" else "exists, unadopted"
        lines.append(
            f"| `{f.name}` | {f.ecosystem} | {f.max_repeat_in_prompt}/{result.samples} "
            f"| {len(f.prompt_ids)} | {status} |"
        )
    if not shown:
        lines.append("| — | — | — | — | no invented names extracted |")
    lines += [
        "", "## Limits", "",
        "- One model, one date, one set of conditions.",
        "- Extraction is conservative, so these counts are a floor.",
        "- `exists, unadopted` is the more serious status, not the milder one.",
        "",
        f"Generated by slopbench, built by Blvkware (https://blvkware.dev).",
    ]
    return "\n".join(line for line in lines if line is not None)
