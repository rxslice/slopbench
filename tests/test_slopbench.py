"""Tests for slopbench.

Extraction gets the most coverage because every published number rests on it.
A false extraction becomes a false accusation about someone else's product.
"""
from __future__ import annotations

import json
import re

import pytest

from slopbench.bench import Result, run, to_pkgguard_corpus
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
    assert "pypi:ghost-lib" in corpus
    assert "pypi:other-ghost" not in corpus


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
