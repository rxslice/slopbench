"""Regression tests for the 2026-11-13 audit (SLOP-1 to SLOP-3).

Registry and model calls are faked; nothing is fetched or installed.
"""
from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import pytest

from slopbench import bench, providers, verify
from slopbench.providers import Completion
from slopbench.report import render_html, render_markdown


# --- SLOP-1: a registry outage is never an invented package ---------------

def _pkgguard_verdict(facts):
    pkgguard = pytest.importorskip("pkgguard")  # noqa: F841
    from pkgguard.conflation import ConflationDetector
    from pkgguard.scoring import assess
    result = assess(facts, ConflationDetector([]), [])
    with patch("pkgguard.service.verify_many", return_value=[result]):
        return result, verify.verify(facts.name, facts.ecosystem, True)


@pytest.mark.parametrize("error", ["simulated timeout", "HTTP 429", "HTTP 503", "malformed JSON"])
def test_registry_failure_is_unknown_not_invented(error):
    from pkgguard.registries import PackageFacts
    result, verdict = _pkgguard_verdict(PackageFacts("example", "npm", False, fetch_error=error))
    assert result.verdict == "REVIEW" and result.exists is False  # what pkgguard reports
    assert verdict.status == verify.UNKNOWN
    assert verdict.invented is False


def test_confirmed_absence_still_counts_as_invented():
    from pkgguard.registries import PackageFacts
    result, verdict = _pkgguard_verdict(PackageFacts("made-up-thing", "npm", False))
    assert result.verdict == "BLOCK"
    assert verdict.status == verify.NOT_FOUND and verdict.invented


# --- SLOP-2: unverifiable references are out of the denominator -----------

class _Provider:
    name = "offline"
    model = "fixture"

    def __init__(self, text):
        self.text = text

    def complete(self, *args):
        return Completion(self.text, False)


def _run(text, statuses, samples=2):
    class FakeVerifier:
        pkgguard_available = False

        def __init__(self, **kwargs):
            pass

        def __call__(self, name, ecosystem):
            return verify.Verdict(name, ecosystem, statuses[name])

    with patch.object(bench, "Verifier", FakeVerifier):
        return bench.run(_Provider(text), [{"id": "p", "tier": "test", "ecosystem": "npm", "text": "t"}],
                         samples=samples)


def test_rate_is_over_verified_references():
    # The audit's fixture: one invented and one unverifiable name, each twice.
    r = _run("```sh\nnpm install invented-example unknown-example\n```",
             {"invented-example": verify.NOT_FOUND, "unknown-example": verify.UNKNOWN})
    assert r.suggestion_occurrences == 4
    assert r.unverifiable_occurrences == 2
    assert r.verified_occurrences == 2
    assert r.invented_occurrences == 2
    assert r.occurrence_rate == 1.0          # was reported as 0.5
    assert r.by_tier["test"]["verified"] == 2
    assert r.by_tier["test"]["rate"] == 1.0
    d = r.to_dict()
    assert d["verified_occurrences"] == 2 and d["verification_coverage"] == 0.5


def test_all_unverifiable_run_is_not_a_measured_zero():
    r = _run("```sh\nnpm install unknown-example\n```", {"unknown-example": verify.UNKNOWN})
    assert r.verified_occurrences == 0
    assert r.by_tier["test"]["rate"] is None
    assert "n/a" in render_html(r)
    assert "0 verified by the registry" in render_markdown(r)


# --- SLOP-3: different endpoints never share a cache entry -----------------

def test_custom_endpoints_get_separate_cache_entries(tmp_path):
    p = providers.OpenAICompatibleProvider("same-model", str(tmp_path))
    with patch.dict(os.environ, {"SLOPBENCH_BASE_URL": "https://provider-one.invalid/v1"}):
        one = p._cache_path("fixture", 0.7, 0)
        one_again = p._cache_path("fixture", 0.7, 0)
    with patch.dict(os.environ, {"SLOPBENCH_BASE_URL": "https://provider-two.invalid/v1/"}):
        two = p._cache_path("fixture", 0.7, 0)
    assert one == one_again
    assert one != two


def test_ollama_host_is_part_of_the_key(tmp_path):
    p = providers.OllamaProvider("m", str(tmp_path))
    with patch.dict(os.environ, {"OLLAMA_HOST": "http://gpu-box:11434"}):
        a = p._cache_path("x", 0.7, 0)
    with patch.dict(os.environ, {"OLLAMA_HOST": "http://other-box:11434"}):
        b = p._cache_path("x", 0.7, 0)
    assert a != b


def test_default_endpoint_keeps_existing_caches_valid(tmp_path):
    p = providers.OpenAICompatibleProvider("m", str(tmp_path))
    legacy = hashlib.sha256("openai|m|0.7|0|x".encode()).hexdigest() + ".json"
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SLOPBENCH_BASE_URL", None)
        assert os.path.basename(p._cache_path("x", 0.7, 0)) == legacy
    with patch.dict(os.environ, {"SLOPBENCH_BASE_URL": "https://API.openai.com/v1/"}):
        assert os.path.basename(p._cache_path("x", 0.7, 0)) == legacy  # same endpoint, normalized


def test_credentials_never_enter_the_key(tmp_path):
    p = providers.OpenAICompatibleProvider("m", str(tmp_path))
    with patch.dict(os.environ, {"SLOPBENCH_BASE_URL": "https://user:secret@host.invalid/v1?token=abc"}):
        path_with_secret = p._cache_path("x", 0.7, 0)
    with patch.dict(os.environ, {"SLOPBENCH_BASE_URL": "https://host.invalid/v1"}):
        assert p._cache_path("x", 0.7, 0) == path_with_secret
