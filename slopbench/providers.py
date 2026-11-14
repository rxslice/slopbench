"""
slopbench — built by Blvkware (https://blvkware.dev)

Model adapters.

Every completion is cached to disk before it is ever parsed. Generation is the
only step that costs money; extraction and scoring are free and get iterated on
constantly. Separating them means a scoring change costs nothing to re-run, and
a published result stays reproducible from the cache after the model behind the
endpoint has moved on.

The cache key covers provider, model, prompt, temperature, sample index, and
the endpoint that answered. Changing any of them is a different measurement
and gets a different key. Endpoints are identified by URL only, never by
credential. A provider's default endpoint adds nothing to the key, so caches
written before the endpoint was part of it stay valid for default setups;
any other endpoint keys separately.
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
    # False when the endpoint refused the requested temperature and the sample
    # was drawn at the model's own default sampling instead. Recorded, never
    # hidden: it changes what the recurrence figure means.
    temperature_applied: bool = True


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


def _normalize_endpoint(url: str) -> str:
    """Scheme, lowercased host and path, without trailing slash, query or userinfo."""
    import urllib.parse

    if not url:
        return ""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


class Provider:
    """Base adapter. Subclasses implement `_generate`."""

    name = "base"

    def __init__(self, model: str, cache_dir: str = DEFAULT_CACHE) -> None:
        self.model = model
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    #: The endpoint this provider talks to when nothing overrides it.
    default_endpoint = ""

    def endpoint(self) -> str:
        """The normalized URL this provider is configured to call. Never a secret."""
        return self.default_endpoint

    def _cache_path(self, prompt: str, temperature: float, index: int) -> str:
        identity = f"{self.name}|{self.model}|{temperature}|{index}|{prompt}"
        endpoint = _normalize_endpoint(self.endpoint())
        if endpoint and endpoint != _normalize_endpoint(self.default_endpoint):
            identity = f"v2|endpoint={endpoint}|" + identity
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
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
                    temperature_applied=data.get("temperature_applied", True),
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
                    "endpoint": _normalize_endpoint(self.endpoint()),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "temperature_applied": result.temperature_applied,
                }, fh)
            os.replace(temp, path)
        except OSError:
            pass
        return result

    def _generate(self, prompt: str, temperature: float) -> Completion:
        raise NotImplementedError


class AnthropicProvider(Provider):
    """Claude via the Messages API.

    Current Claude models sample only at their own defaults: Opus 4.7 and
    later, Sonnet 5 and the Fable models reject any `temperature` with a 400,
    and Sonnet 5.5 and Haiku 5.5 reject non-default values. The first such
    rejection switches this provider to default sampling for the rest of the
    run, and every affected sample is marked so the report can say so, rather
    than the run failing every sample and reporting nothing.

    `max_tokens` is higher than the other providers' because on current models
    thinking is always on and its tokens count against the same limit; at 2048
    the visible answer could be cut off before its install command.
    """

    name = "anthropic"
    default_endpoint = "https://api.anthropic.com/v1"
    MAX_TOKENS = 16000

    def __init__(self, model: str, cache_dir: str = DEFAULT_CACHE) -> None:
        super().__init__(model, cache_dir)
        self._send_temperature = True

    def _generate(self, prompt: str, temperature: float) -> Completion:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set.")
        headers = {"content-type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
        payload = {"model": self.model, "max_tokens": self.MAX_TOKENS,
                   "messages": [{"role": "user", "content": prompt}]}

        data = None
        if self._send_temperature:
            try:
                data = _post("https://api.anthropic.com/v1/messages",
                             {**payload, "temperature": temperature}, headers)
            except ProviderError as error:
                message = str(error)
                if not (message.startswith("HTTP 400") and "temperature" in message):
                    raise
                self._send_temperature = False
        if data is None:
            data = _post("https://api.anthropic.com/v1/messages", payload, headers)

        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(text, False, usage.get("input_tokens"), usage.get("output_tokens"),
                          temperature_applied=self._send_temperature)


class OpenAICompatibleProvider(Provider):
    """Any /v1/chat/completions endpoint: OpenAI, Groq, Together, OpenRouter,
    vLLM, LM Studio. Set SLOPBENCH_BASE_URL and SLOPBENCH_API_KEY."""

    name = "openai"
    default_endpoint = "https://api.openai.com/v1"

    def endpoint(self) -> str:
        return os.environ.get("SLOPBENCH_BASE_URL", self.default_endpoint)

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
    default_endpoint = "http://127.0.0.1:11434"

    def endpoint(self) -> str:
        return os.environ.get("OLLAMA_HOST", self.default_endpoint)

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
