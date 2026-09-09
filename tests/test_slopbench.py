"""Tests for slopbench.

Extraction gets the most coverage because every published number rests on it.
A false extraction becomes a false accusation about someone else's product.
"""
from __future__ import annotations

import json
import re

import pytest

from slopbench.bench import (
    CorpusFormatError,
    Result,
    count_corpus_names,
    merge_pkgguard_corpus,
    run,
    to_pkgguard_corpus,
)
from slopbench.extract import extract
from slopbench.providers import Completion, Provider
from slopbench.report import render_html, render_markdown
from slopbench.verify import ESTABLISHED, NOT_FOUND, SUSPICIOUS, UNKNOWN, Verdict, _classify


# --------------------------------------------------------------------------
# extraction: what must be kept
# --------------------------------------------------------------------------

def names(text, ecosystem=None):
    return {s.name for s in extract(text, ecosystem)}


def test_extracts_python_imports_and_installs():
    text = """
```bash
pip install requests pandas
```
```python
import requests
from pandas import DataFrame
```
"""
    assert names(text, "pypi") == {"requests", "pandas"}


def test_extracts_scoped_npm_package():
    text = """```js
import { z } from '@scope/thing';
const x = require('lodash');
```"""
    assert names(text, "npm") == {"@scope/thing", "lodash"}


def test_npm_subpath_resolves_to_owning_package():
    text = """```js
import debounce from 'lodash/debounce';
import { Foo } from '@acme/ui/button';
```"""
    assert names(text, "npm") == {"lodash", "@acme/ui"}


def test_extracts_cargo_dependencies():
    text = """```toml
[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = "1"

[dev-dependencies]
criterion = "0.5"
```"""
    found = names(text, "crates")
    assert {"serde", "tokio"} <= found
    assert "criterion" not in found  # dev-dependencies are a different section


def test_cross_ecosystem_install_is_caught_regardless_of_prompt():
    """A Python prompt answered with an npm install is itself a finding."""
    found = {(s.name, s.ecosystem) for s in extract("Run `npm install left-pad`", "pypi")}
    assert ("left-pad", "npm") in found


# --------------------------------------------------------------------------
# extraction: what must be dropped
# --------------------------------------------------------------------------

def test_stdlib_is_not_a_dependency():
    text = """```python
import os, sys, json
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
```"""
    assert names(text, "pypi") == set()


def test_node_builtins_are_not_dependencies():
    text = """```js
const fs = require('fs');
import path from 'node:path';
import crypto from 'crypto';
```"""
    assert names(text, "npm") == set()


def test_rust_std_is_not_a_dependency():
    text = """```rust
use std::collections::HashMap;
use core::fmt;
```"""
    assert names(text, "crates") == set()


def test_relative_imports_are_dropped():
    text = """```js
import a from './helper';
import b from '../lib/util';
import c from '/abs/path';
```"""
    assert names(text, "npm") == set()


def test_locally_defined_modules_are_not_hallucinations():
    """The model wrote utils.py in the same response. Importing it is coherent."""
    text = """
Create `utils.py`:
```python
def helper(): ...
```
Then in `main.py`:
```python
from utils import helper
```
"""
    assert "utils" not in names(text, "pypi")


def test_import_name_maps_to_distribution_name():
    text = """```python
import yaml
import cv2
from PIL import Image
import sklearn
```"""
    assert names(text, "pypi") == {"PyYAML", "opencv-python", "Pillow", "scikit-learn"}


def test_version_specifiers_are_stripped():
    text = "```bash\npip install 'requests>=2.31.0' pandas==2.1.0 uvicorn[standard]\n```"
    assert names(text, "pypi") == {"requests", "pandas", "uvicorn"}


def test_flags_and_urls_are_not_packages():
    text = "```bash\npip install --upgrade --no-cache-dir requests https://x.tld/p.whl\n```"
    assert names(text, "pypi") == {"requests"}


def test_prose_around_an_install_command_is_not_a_dependency():
    # The worst failure this tool can have: `to`, `load` and `the` all 404, all
    # recur in every sample because they are English, and all would be reported
    # as targetable hallucinations in somebody else's product.
    text = "To get started, run pip install pandas to load the data."
    assert names(text) & {"to", "load", "the", "data."} == set()


def test_requirements_file_is_not_a_package():
    assert names("```bash\npip install -r requirements.txt\n```") == set()


def test_constraint_file_does_not_hide_the_package_after_it():
    assert names("```bash\npip install -c constraints.txt django\n```") == {"django"}


def test_cargo_feature_is_not_a_crate():
    assert names("```bash\ncargo add serde --features derive\n```") == {"serde"}


def test_trailing_punctuation_is_not_a_package():
    assert names("You will need to run npm install axios.") == set()


def test_install_in_an_inline_code_span_is_still_read():
    assert names("Install it with `pip install pandas` first.") == {"pandas"}


def test_install_on_its_own_line_is_still_read():
    assert names("Run this:\n\nnpm install express cors\n\nThen start it.") == {
        "express", "cors"
    }


def test_flags_do_not_stop_the_package_list():
    assert names("```bash\nnpm install --save-dev jest @types/node\n```") == {
        "jest", "@types/node"
    }


def test_submodule_import_reports_root_package():
    text = "```python\nimport matplotlib.pyplot as plt\nfrom scipy.stats import norm\n```"
    assert names(text, "pypi") == {"matplotlib", "scipy"}


def test_duplicate_mentions_collapse_to_one_suggestion():
    text = """```bash
pip install requests
```
```python
import requests
from requests import Session
```"""
    assert len(extract(text, "pypi")) == 1


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_new_but_adopted_package_is_not_suspicious():
    v = _classify("fresh-lib", "npm", age=20, downloads=500_000, versions=12)
    assert v.status == ESTABLISHED


def test_new_and_unadopted_package_is_suspicious():
    v = _classify("fresh-lib", "npm", age=9, downloads=11, versions=1)
    assert v.status == SUSPICIOUS
    assert v.invented


def test_old_and_quiet_package_is_not_suspicious():
    """A niche ten-year-old library with low traffic is normal, not a squat."""
    v = _classify("quiet-lib", "npm", age=3650, downloads=40, versions=8)
    assert v.status == ESTABLISHED


def test_planted_name_cannot_age_into_legitimacy():
    """react-codeshift's real shape: 237 days old, one version, 21 downloads."""
    v = _classify("react-codeshift", "npm", age=237, downloads=21, versions=1)
    assert v.status == SUSPICIOUS
    assert "published once and never updated" in v.signals


def test_single_version_with_real_adoption_is_established():
    """A one-release library people actually install is fine."""
    v = _classify("tiny-util", "npm", age=900, downloads=250_000, versions=1)
    assert v.status == ESTABLISHED


def test_unknown_verdict_is_not_treated_as_clean():
    v = Verdict("x", "npm", UNKNOWN, error="registry down")
    assert not v.invented
    assert v.status == UNKNOWN


def test_not_found_counts_as_invented():
    assert Verdict("nope-nope", "npm", NOT_FOUND).invented


# --------------------------------------------------------------------------
# benchmark mechanics, with a scripted provider (no network)
# --------------------------------------------------------------------------

class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, responses, tmp_path):
        super().__init__("test-model", cache_dir=str(tmp_path))
        self.responses = responses

    def complete(self, prompt, temperature, index):  # bypass caching entirely
        return Completion(self.responses[index % len(self.responses)], cached=False)


def _prompts():
    return [{"id": "p1", "ecosystem": "pypi", "tier": "routine", "text": "task"}]


def _patch_verify(monkeypatch, mapping):
    def fake(self, name, ecosystem):
        return mapping.get(name, Verdict(name, ecosystem, ESTABLISHED))
    monkeypatch.setattr("slopbench.verify.Verifier.__call__", fake)


def test_recurring_name_is_targetable(monkeypatch, tmp_path):
    provider = ScriptedProvider(["```bash\npip install ghost-lib\n```"] * 3, tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)
    assert result.targetable_distinct == 1
    assert result.findings[0].max_repeat_in_prompt == 3


def test_one_off_invention_is_reported_but_not_targetable(monkeypatch, tmp_path):
    provider = ScriptedProvider(
        ["```bash\npip install ghost-lib\n```", "```bash\npip install requests\n```",
         "```bash\npip install requests\n```"], tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)
    assert result.invented_distinct == 1
    assert result.targetable_distinct == 0


def test_unverifiable_names_are_excluded_not_counted_clean(monkeypatch, tmp_path):
    provider = ScriptedProvider(["```bash\npip install mystery\n```"] * 2, tmp_path)
    _patch_verify(monkeypatch, {"mystery": Verdict("mystery", "pypi", UNKNOWN, error="down")})
    result = run(provider, _prompts(), samples=2, use_pkgguard=False)
    assert result.unverifiable_distinct == 1
    assert result.invented_distinct == 0


def test_empty_completions_are_counted_separately(monkeypatch, tmp_path):
    provider = ScriptedProvider(["", ""], tmp_path)
    _patch_verify(monkeypatch, {})
    result = run(provider, _prompts(), samples=2, use_pkgguard=False)
    assert result.empty_samples == 2
    assert result.suggestion_occurrences == 0


def test_corpus_export_excludes_one_off_inventions(monkeypatch, tmp_path):
    provider = ScriptedProvider(
        ["```bash\npip install ghost-lib\n```", "```bash\npip install other-ghost\n```",
         "```bash\npip install ghost-lib\n```"], tmp_path)
    _patch_verify(monkeypatch, {
        "ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND),
        "other-ghost": Verdict("other-ghost", "pypi", NOT_FOUND),
    })
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)
    corpus = to_pkgguard_corpus(result)
    assert corpus["pypi"] == ["ghost-lib"]
    assert "other-ghost" not in corpus["pypi"]
    assert "pypi:ghost-lib" in corpus["_provenance"]["evidence"]


def _as_pkgguard_reads_it(corpus):
    """pkgguard's own loader, pinned.

    This one expression is the entire contract between the two tools, and both
    sides fail silently when it is broken: a corpus in the wrong shape loads
    without error and contributes no names. Copied from
    pkgguard.service.load_known_hallucinations so CI catches drift without
    needing pkgguard installed.
    """
    return {eco: {n.lower() for n in names} for eco, names in corpus.items()}


def test_corpus_is_in_the_format_pkgguard_actually_reads(monkeypatch, tmp_path):
    provider = ScriptedProvider(
        ["```bash\npip install ghost-lib\n```"] * 3, tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)

    loaded = _as_pkgguard_reads_it(to_pkgguard_corpus(result))

    assert "ghost-lib" in loaded["pypi"], (
        "pkgguard would load this corpus and find no names in it"
    )


def test_corpus_survives_a_json_round_trip(monkeypatch, tmp_path):
    provider = ScriptedProvider(["```bash\npip install ghost-lib\n```"] * 3, tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)

    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(to_pkgguard_corpus(result), indent=2), encoding="utf-8")
    reloaded = json.loads(path.read_text(encoding="utf-8"))

    assert _as_pkgguard_reads_it(reloaded)["pypi"] == {"ghost-lib"}


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("pkgguard") is None,
    reason="pkgguard is an optional dependency",
)
def test_corpus_loads_in_a_real_pkgguard(monkeypatch, tmp_path):
    from pkgguard.service import load_known_hallucinations

    provider = ScriptedProvider(["```bash\npip install ghost-lib\n```"] * 3, tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    result = run(provider, _prompts(), samples=3, use_pkgguard=False)

    data_dir = tmp_path / "pkgguard-data"
    data_dir.mkdir()
    (data_dir / "known_hallucinations.json").write_text(
        json.dumps(to_pkgguard_corpus(result)), encoding="utf-8"
    )
    monkeypatch.setenv("PKGGUARD_DATA_DIR", str(data_dir))
    monkeypatch.setattr("pkgguard.service.DATA_DIR", data_dir)
    load_known_hallucinations.cache_clear()
    try:
        assert "ghost-lib" in load_known_hallucinations().get("pypi", set())
    finally:
        load_known_hallucinations.cache_clear()


# --------------------------------------------------------------------------
# corpus accumulation
# --------------------------------------------------------------------------

def _corpus(names_by_eco, source="slopbench a/b", when="2026-09-01"):
    return {
        "_provenance": {"note": "n", "sources": [source], "last_updated": when,
                        "evidence": {f"{eco}:{n.lower()}": {"name": n, "measured": when}
                                     for eco, ns in names_by_eco.items() for n in ns}},
        **{eco: list(ns) for eco, ns in names_by_eco.items()},
    }


def test_merge_never_drops_a_name_an_earlier_run_found():
    merged = merge_pkgguard_corpus(
        _corpus({"npm": ["react-codeshift"], "pypi": []}),
        _corpus({"npm": ["vue-codeshift"], "pypi": []}, source="slopbench c/d",
                when="2026-09-08"),
    )
    assert merged["npm"] == ["react-codeshift", "vue-codeshift"]
    assert merged["_provenance"]["last_updated"] == "2026-09-08"
    assert len(merged["_provenance"]["sources"]) == 2


def test_merging_the_same_run_twice_changes_nothing():
    incoming = _corpus({"npm": ["react-codeshift"]})
    once = merge_pkgguard_corpus(_corpus({"npm": []}), incoming)
    twice = merge_pkgguard_corpus(once, incoming)
    assert once == twice


def test_merge_keeps_the_date_a_name_was_first_seen():
    first = _corpus({"npm": ["react-codeshift"]}, when="2026-01-04")
    later = _corpus({"npm": ["react-codeshift"]}, source="slopbench c/d",
                    when="2026-09-08")
    merged = merge_pkgguard_corpus(first, later)
    entry = merged["_provenance"]["evidence"]["npm:react-codeshift"]
    assert entry["first_seen"] == "2026-01-04"
    assert entry["measured"] == "2026-09-08"


def test_merge_refuses_a_file_that_is_not_a_corpus():
    # The shape this tool used to emit: keyed by name, holding evidence objects.
    not_a_corpus = {"npm:react-codeshift": {"name": "react-codeshift"}}
    with pytest.raises(CorpusFormatError):
        merge_pkgguard_corpus(not_a_corpus, _corpus({"npm": []}))


def test_merge_accepts_a_corpus_with_no_provenance_block():
    merged = merge_pkgguard_corpus({"npm": ["react-codeshift"]}, _corpus({"npm": ["x"]}))
    assert merged["npm"] == ["react-codeshift", "x"]
    assert count_corpus_names(merged) == 2


def test_a_run_that_measured_nothing_is_not_reported_as_clean(monkeypatch, tmp_path):
    """A missing API key must not become a clean bill of health.

    Every completion failing leaves zero findings, which reads identically to a
    model that invented nothing. The CLI has to tell those apart, because the
    output of this one is a document someone forwards.
    """
    from slopbench import cli
    from slopbench.providers import ProviderError

    class DeadProvider(Provider):
        name = "dead"

        def __init__(self):
            super().__init__("test-model", cache_dir=str(tmp_path))

        def complete(self, prompt, temperature, index):
            raise ProviderError("no key")

    monkeypatch.setattr(cli, "build", lambda provider, model: DeadProvider())
    report = tmp_path / "out.html"
    code = cli.main([
        "run", "--provider", "ollama", "--model", "m", "--samples", "2",
        "--limit", "1", "--quiet", "--report", str(report),
    ])

    assert code == 2
    assert not report.exists(), "a report was written from zero completions"


def test_report_states_how_many_samples_failed(monkeypatch, tmp_path):
    result = _result(monkeypatch, tmp_path)
    result.errors = ["p1#0: upstream timeout", "p1#1: upstream timeout"]

    assert "Samples that failed" in render_html(result)
    assert "2" in render_html(result)
    assert "Samples that failed and were not measured: 2" in render_markdown(result)


def test_rates_are_zero_not_divide_by_zero():
    empty = Result(
        provider="x", model="y", samples=2, temperature=0.7, started="", duration_seconds=0,
        prompts_run=0, total_samples=0, empty_samples=0, suggestion_occurrences=0,
        distinct_packages=0, invented_occurrences=0, invented_distinct=0,
        targetable_distinct=0, unverifiable_distinct=0,
    )
    assert empty.occurrence_rate == 0.0
    assert empty.targetable_rate == 0.0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def _result(monkeypatch, tmp_path):
    provider = ScriptedProvider(["```bash\npip install ghost-lib requests\n```"] * 2, tmp_path)
    _patch_verify(monkeypatch, {"ghost-lib": Verdict("ghost-lib", "pypi", NOT_FOUND)})
    return run(provider, _prompts(), samples=2, use_pkgguard=False)


def test_html_report_is_self_contained(monkeypatch, tmp_path):
    page = render_html(_result(monkeypatch, tmp_path), subject="Acme")
    assert "ghost-lib" in page and "Acme" in page
    # No external requests: the file must render offline and behind a proxy.
    for marker in ("http://", "cdn.", "<script", "@import"):
        assert marker not in page
    assert page.count("https://blvkware.dev") >= 1


def test_report_states_the_limits(monkeypatch, tmp_path):
    page = render_html(_result(monkeypatch, tmp_path))
    assert "does not show" in page.lower()
    assert "floor" in page.lower()


def test_markdown_report_renders(monkeypatch, tmp_path):
    text = render_markdown(_result(monkeypatch, tmp_path), subject="Acme")
    assert "ghost-lib" in text and "## Limits" in text


def test_clean_result_does_not_manufacture_a_finding(monkeypatch, tmp_path):
    provider = ScriptedProvider(["```bash\npip install requests\n```"] * 2, tmp_path)
    _patch_verify(monkeypatch, {})
    result = run(provider, _prompts(), samples=2, use_pkgguard=False)
    page = render_html(result)
    assert result.targetable_distinct == 0
    assert "needs action" in page or "not a supply-chain one" in page


def test_json_result_round_trips(monkeypatch, tmp_path):
    payload = json.loads(json.dumps(_result(monkeypatch, tmp_path).to_dict()))
    assert payload["targetable_distinct"] == 1
    assert payload["findings"][0]["targetable"] is True


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------

class CountingProvider(Provider):
    name = "counting"

    def __init__(self, tmp_path):
        super().__init__("m", cache_dir=str(tmp_path))
        self.calls = 0

    def _generate(self, prompt, temperature):
        self.calls += 1
        return Completion(f"call {self.calls}", cached=False)


def test_completions_are_cached_to_disk(tmp_path):
    provider = CountingProvider(tmp_path)
    first = provider.complete("p", 0.7, 0)
    second = provider.complete("p", 0.7, 0)
    assert provider.calls == 1
    assert second.cached and second.text == first.text


def test_sample_index_is_part_of_the_cache_key(tmp_path):
    provider = CountingProvider(tmp_path)
    provider.complete("p", 0.7, 0)
    provider.complete("p", 0.7, 1)
    assert provider.calls == 2


def test_temperature_is_part_of_the_cache_key(tmp_path):
    provider = CountingProvider(tmp_path)
    provider.complete("p", 0.7, 0)
    provider.complete("p", 1.0, 0)
    assert provider.calls == 2


# --------------------------------------------------------------------------
# corpus integrity
# --------------------------------------------------------------------------

def test_prompts_never_name_a_package():
    """A prompt that names a library measures instruction-following, not invention."""
    from slopbench.bench import load_prompts
    banned = [
        "requests", "pandas", "numpy", "flask", "django", "fastapi", "pillow",
        "boto3", "pytest", "express", "axios", "react", "lodash", "vue",
        "serde", "tokio", "clap", "reqwest", "polars",
    ]
    for prompt in load_prompts():
        lowered = prompt["text"].lower()
        for name in banned:
            assert not re.search(rf"\b{re.escape(name)}\b", lowered), \
                f"{prompt['id']} names {name}"


def test_prompt_ids_are_unique_and_tiered():
    from slopbench.bench import load_prompts
    prompts = load_prompts()
    assert len({p["id"] for p in prompts}) == len(prompts)
    assert {p["tier"] for p in prompts} == {"routine", "niche", "emerging"}
