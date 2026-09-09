"""
slopbench — built by Blvkware (https://blvkware.dev)

Model adapters.

Every completion is cached to disk before it is ever parsed. Generation is the
only step that costs money; extraction and scoring are free and get iterated on
constantly. Separating them means a scoring change costs nothing to re-run, and
a published result stays reproducible from the cache after the model behind the
endpoint has moved on.

The cache key covers provider, model, prompt, temperature, and sample index.
Changing any of them is a different measurement and gets a different key.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "slopbench")
TIMEOUT = 120
RETRIES = 3


class ProviderError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    cached: bool
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last: Optional[Exception] = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            # 4xx other than rate limiting will not fix themselves.
            if error.code != 429 and 400 <= error.code < 500:
                raise ProviderError(f"HTTP {error.code}: {detail}") from error
            last = ProviderError(f"HTTP {error.code}: {detail}")
        except Exception as error:  # noqa: BLE001 - surfaced after retries
            last = error
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
    raise ProviderError(str(last))


class Provider:
    """Base adapter. Subclasses implement `_generate`."""

    name = "base"

    def __init__(self, model: str, cache_dir: str = DEFAULT_CACHE) -> None:
        self.model = model
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_path(self, prompt: str, temperature: float, index: int) -> str:
        digest = hashlib.sha256(
            f"{self.name}|{self.model}|{temperature}|{index}|{prompt}".encode("utf-8")
        ).hexdigest()
        return os.path.join(self.cache_dir, f"{digest}.json")

    def complete(self, prompt: str, temperature: float, index: int) -> Completion:
        path = self._cache_path(prompt, temperature, index)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                return Completion(
                    text=data["text"], cached=True,
                    input_tokens=data.get("input_tokens"),
                    output_tokens=data.get("output_tokens"),
                )
            except (OSError, ValueError, KeyError):
                pass

        result = self._generate(prompt, temperature)
        try:
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump({
                    "provider": self.name, "model": self.model,
                    "temperature": temperature, "index": index,
                    "prompt": prompt, "text": result.text,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }, fh)
            os.replace(temp, path)
        except OSError:
            pass
        return result

    def _generate(self, prompt: str, temperature: float) -> Completion:
        raise NotImplementedError


class AnthropicProvider(Provider):
    name = "anthropic"

    def _generate(self, prompt: str, temperature: float) -> Completion:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set.")
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {"model": self.model, "max_tokens": 2048, "temperature": temperature,
             "messages": [{"role": "user", "content": prompt}]},
            {"content-type": "application/json", "x-api-key": key,
             "anthropic-version": "2023-06-01"},
        )
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(text, False, usage.get("input_tokens"), usage.get("output_tokens"))


class OpenAICompatibleProvider(Provider):
    """Any /v1/chat/completions endpoint: OpenAI, Groq, Together, OpenRouter,
    vLLM, LM Studio. Set SLOPBENCH_BASE_URL and SLOPBENCH_API_KEY."""

    name = "openai"

    def _generate(self, prompt: str, temperature: float) -> Completion:
        base = os.environ.get("SLOPBENCH_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        key = os.environ.get("SLOPBENCH_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("SLOPBENCH_API_KEY (or OPENAI_API_KEY) is not set.")
        data = _post(
            f"{base}/chat/completions",
            {"model": self.model, "temperature": temperature, "max_tokens": 2048,
             "messages": [{"role": "user", "content": prompt}]},
            {"content-type": "application/json", "authorization": f"Bearer {key}"},
        )
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "") if choices else ""
        usage = data.get("usage") or {}
        return Completion(text, False, usage.get("prompt_tokens"), usage.get("completion_tokens"))


class OllamaProvider(Provider):
    """Local models. No key, no spend — the way to get a first run for free."""

    name = "ollama"

    def _generate(self, prompt: str, temperature: float) -> Completion:
        host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        data = _post(
            f"{host}/api/generate",
            {"model": self.model, "prompt": prompt, "stream": False,
             "options": {"temperature": temperature, "num_predict": 2048}},
            {"content-type": "application/json"},
        )
        return Completion(data.get("response", ""), False,
                          data.get("prompt_eval_count"), data.get("eval_count"))


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatibleProvider,
    "ollama": OllamaProvider,
}


def build(provider: str, model: str, cache_dir: str = DEFAULT_CACHE) -> Provider:
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider}'. Available: {sorted(PROVIDERS)}")
    return PROVIDERS[provider](model, cache_dir)
