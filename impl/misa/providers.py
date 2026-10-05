"""Misa v0.1 model providers: transport layer for `model` calls.

Providers do transport only: send prompt, return parsed JSON + usage.
Validation, repair retries, and budget enforcement live in the
interpreter. Keys come from environment variables, never from source.
"""
from __future__ import annotations
import base64
import json
import mimetypes
import os
import ssl
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from .errors import (ModelError, ProviderConfigError, StreamingUnsupported)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Completion:
    text: str                       # raw model text (expected JSON)
    usage: Usage = field(default_factory=Usage)


# pricing: (input $/1K, output $/1K) — estimates, see spec appendix
PRICING = [
    ("gpt-5-mini", 0.00015, 0.0006),
    ("gpt-5", 0.00125, 0.01),
    ("gemini-2.5-flash", 0.000075, 0.0003),
    ("claude-haiku", 0.00025, 0.00125),
    ("deepseek", 0.00014, 0.00028),
]


def estimate_cost_usd(model_id: str, usage: Usage) -> float:
    mid = model_id.lower()
    for prefix, pi, po in PRICING:
        if mid.startswith(prefix):
            return usage.input_tokens / 1000 * pi \
                + usage.output_tokens / 1000 * po
    return usage.input_tokens / 1000 * 0.001 \
        + usage.output_tokens / 1000 * 0.003


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE") \
        or os.environ.get("CURL_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _post_json(url: str, payload: dict, headers: dict,
               timeout_s: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "misa/0.1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s,
                                    context=_ssl_context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        if e.code in (401, 403):
            raise ProviderConfigError(
                f"provider rejected credentials (HTTP {e.code}): {body}")
        raise ModelError(f"provider HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise ModelError(f"provider network error: {e.reason}")
    except TimeoutError:
        raise ModelError("provider timed out")


def _post_sse(url: str, payload: dict, headers: dict,
              timeout_s: float) -> Iterator[dict]:
    """Yield parsed SSE data chunks from an OpenAI-compatible stream."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": "misa/0.1", **headers})
    try:
        r = urllib.request.urlopen(req, timeout=timeout_s,
                                   context=_ssl_context())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise ModelError(f"provider HTTP {e.code}: {body}")
    with r:
        buf = ""
        while True:
            chunk = r.read(1024)
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_s = line[5:].strip()
                if data_s == "[DONE]":
                    return
                try:
                    yield json.loads(data_s)
                except json.JSONDecodeError:
                    continue


class Provider:
    kind = "base"

    def complete(self, *, model_id: str, prompt: str,
                 images: List[Tuple[str, str]],
                 timeout_s: float) -> Completion:
        raise NotImplementedError

    def stream(self, *, model_id: str, prompt: str,
               images: List[Tuple[str, str]],
               timeout_s: float) -> Iterator[str]:
        raise StreamingUnsupported(
            f"provider '{self.kind}' does not support streaming in v0.1")


class MockProvider(Provider):
    """Deterministic test double. Responses keyed by model alias."""
    kind = "mock"

    def __init__(self, scripts: dict):
        # scripts: alias -> JSON-serializable response (or {"_error": msg})
        self.scripts = scripts
        self.alias = ""

    def complete(self, *, model_id: str, prompt: str,
                 images: List[Tuple[str, str]],
                 timeout_s: float) -> Completion:
        if self.alias not in self.scripts:
            raise ProviderConfigError(
                f"mock provider has no script for alias '{self.alias}' "
                f"(pass --mock with a JSON file)")
        script = self.scripts[self.alias]
        if isinstance(script, dict) and "_error" in script:
            raise ModelError(f"mock error: {script['_error']}")
        if isinstance(script, dict) and "_usage" in script:
            usage = Usage(**script["_usage"])
            text = json.dumps(script["response"])
        else:
            usage = Usage(input_tokens=120, output_tokens=60,
                          latency_ms=5)
            text = json.dumps(script)
        return Completion(text=text, usage=usage)


class OpenAICompatProvider(Provider):
    kind = "openai"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        if not api_key:
            raise ProviderConfigError(
                "openai provider needs a key "
                "(set the env var named in the provider block)")
        self.api_key = api_key
        self.base_url = (base_url
                         or os.environ.get("MISA_OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _content(self, prompt: str,
                 images: List[Tuple[str, str]]) -> list:
        if not images:
            return prompt
        parts: list = [{"type": "text", "text": prompt}]
        for path, mime in images:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return parts

    def complete(self, *, model_id: str, prompt: str,
                 images: List[Tuple[str, str]],
                 timeout_s: float) -> Completion:
        t0 = time.monotonic()
        payload = {
            "model": model_id,
            "messages": [{"role": "user",
                          "content": self._content(prompt, images)}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        resp = _post_json(self.base_url + "/chat/completions", payload,
                          self._headers(), timeout_s)
        try:
            text = resp["choices"][0]["message"]["content"]
            u = resp.get("usage", {})
        except (KeyError, IndexError, TypeError):
            raise ModelError(f"malformed provider response: "
                             f"{str(resp)[:300]}")
        usage = Usage(input_tokens=u.get("prompt_tokens", 0),
                      output_tokens=u.get("completion_tokens", 0),
                      latency_ms=int((time.monotonic() - t0) * 1000))
        return Completion(text=text or "", usage=usage)

    def stream(self, *, model_id: str, prompt: str,
               images: List[Tuple[str, str]],
               timeout_s: float) -> Iterator[str]:
        payload = {
            "model": model_id,
            "messages": [{"role": "user",
                          "content": self._content(prompt, images)}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        for chunk in _post_sse(self.base_url + "/chat/completions",
                               payload, self._headers(), timeout_s):
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {}).get("content")
                if delta:
                    yield delta


class GeminiProvider(Provider):
    kind = "gemini"

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderConfigError(
                "gemini provider needs a key "
                "(set the env var named in the provider block)")
        self.api_key = api_key

    def _parts(self, prompt: str,
               images: List[Tuple[str, str]]) -> list:
        parts: list = [{"text": prompt + "\nRespond with JSON only."}]
        for path, mime in images:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        return parts

    def complete(self, *, model_id: str, prompt: str,
                 images: List[Tuple[str, str]],
                 timeout_s: float) -> Completion:
        t0 = time.monotonic()
        base = os.environ.get("MISA_GEMINI_BASE_URL",
                              "https://generativelanguage.googleapis.com")
        # key travels as ?key= query param: the egress proxy swaps
        # credential surrogates (hsurr:...) for the real key there.
        key_q = urllib.parse.quote(self.api_key, safe="")
        url = (f"{base}/v1beta/models/{model_id}:generateContent"
               f"?key={key_q}")
        payload = {
            "contents": [{"role": "user",
                         "parts": self._parts(prompt, images)}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0},
        }
        resp = _post_json(url, payload, {}, timeout_s)
        try:
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            u = resp.get("usageMetadata", {})
        except (KeyError, IndexError, TypeError):
            raise ModelError(f"malformed provider response: "
                             f"{str(resp)[:300]}")
        usage = Usage(input_tokens=u.get("promptTokenCount", 0),
                      output_tokens=u.get("candidatesTokenCount", 0),
                      latency_ms=int((time.monotonic() - t0) * 1000))
        return Completion(text=text or "", usage=usage)


def make_provider(kind: str, key_env: str,
                  mock_scripts: Optional[dict] = None,
                  mock_alias: str = "") -> Provider:
    key = os.environ.get(key_env, "") if key_env else ""
    if kind == "mock":
        p = MockProvider(mock_scripts or {})
        p.alias = mock_alias
        return p
    if kind == "openai":
        return OpenAICompatProvider(key)
    if kind == "gemini":
        return GeminiProvider(key)
    raise ProviderConfigError(f"unknown provider kind '{kind}' "
                              f"(v0.1: mock, openai, gemini)")


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"
