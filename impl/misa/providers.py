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

from .errors import (ModelError, ProviderConfigError, StreamingUnsupported,
                    OnDeviceShapeError, OnDeviceSignatureError,
                    OnDeviceRuntimeUnavailable)


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


ONDEVICE_EXTENSIONS = {
    ".onnx": "onnx",
    ".mlpackage": "coreml",
    ".mlmodelc": "coreml",
    ".tflite": "tflite",
}


def ondevice_runtime(model_path: str) -> str:
    """Runtime key for an on-device model file, by extension.

    Raises ProviderConfigError for unknown extensions.
    """
    ext = os.path.splitext(model_path)[1].lower()
    try:
        return ONDEVICE_EXTENSIONS[ext]
    except KeyError:
        raise ProviderConfigError(
            f"unknown on-device model extension '{ext or '(none)'}' "
            f"(want one of: {', '.join(sorted(ONDEVICE_EXTENSIONS))})")


# onnxruntime sessions, keyed by resolved model path (lazy, per process)
_ONNX_SESSIONS: dict = {}


def _ort_session(model_path: str):
    try:
        import onnxruntime as ort
    except ImportError:
        # C-8: clean E503, not a ModuleNotFoundError traceback.
        raise OnDeviceRuntimeUnavailable(
            "ONNX models need the 'onnxruntime' package "
            "(pip install onnxruntime)")
    if model_path not in _ONNX_SESSIONS:
        _ONNX_SESSIONS[model_path] = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"])
    return _ONNX_SESSIONS[model_path]


# ai-edge-litert interpreters, keyed by resolved model path (lazy, per process)
_TFLITE_INTERPS: dict = {}


def _tflite_interp(model_path: str):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        raise OnDeviceRuntimeUnavailable(
            "TFLite models need the 'ai-edge-litert' package "
            "(pip install ai-edge-litert)")
    if model_path not in _TFLITE_INTERPS:
        try:
            itp = Interpreter(model_path=model_path)
            itp.allocate_tensors()
        except Exception as e:  # noqa: BLE001 - corrupt/foreign file
            raise ModelError(
                f"cannot load TFLite model '{model_path}': {e}")
        _TFLITE_INTERPS[model_path] = itp
    return _TFLITE_INTERPS[model_path]


class _LiteMeta:
    """Adapter: ai-edge-litert tensor details -> the {name, shape, type}
    shape OnDeviceProvider.complete() expects (mirrors ORT's metadata)."""

    def __init__(self, detail):
        import numpy as np
        self.name = detail["name"]
        self.shape = tuple(int(d) for d in detail["shape"])
        self.index = int(detail["index"])
        kind = np.dtype(detail["dtype"]).kind
        if kind == "f":
            self.type = "tensor(float)"
        elif kind in "OSU":
            self.type = "tensor(string)"
        else:
            self.type = f"tensor({np.dtype(detail['dtype']).name})"


def _sig_kind(te) -> Optional[str]:
    """v1 on-device type mapping. Returns 'scalar' | 'vec' | 'text'
    or None when the type is not supported on-device (E502)."""
    is_list = bool(getattr(te, "is_list", False))
    is_map = bool(getattr(te, "is_map", False))
    optional = bool(getattr(te, "optional", False))
    name = getattr(te, "name", "")
    if is_map or optional:
        return None
    if name == "Float" and not is_list:
        return "scalar"
    if name == "Float" and is_list:
        return "vec"
    if name == "Text" and not is_list:
        return "text"
    return None


class OnDeviceProvider(Provider):
    """Runs a local model file with zero network traffic.

    v1: ONNX Runtime (.onnx) everywhere; TFLite (.tflite) via ai-edge-litert
    in the interpreter (CPU) and via the NNAPI delegate in Android AOT
    builds. CoreML (.mlpackage/.mlmodelc) raises E503 until Apple-side
    interpreter support lands (AOT covers Apple targets since M3).
    """
    kind = "ondevice"

    def __init__(self, model_path: str):
        if not model_path:
            raise ProviderConfigError(
                "on-device provider needs `model: file(\"...\")` "
                "in its provider block")
        if not os.path.exists(model_path):
            raise ProviderConfigError(
                f"on-device model file not found: '{model_path}'")
        self.model_path = os.path.abspath(model_path)
        self.runtime = ondevice_runtime(self.model_path)
        if self.runtime == "coreml":
            raise OnDeviceRuntimeUnavailable(
                "CoreML models (.mlpackage/.mlmodelc) need Apple "
                "hardware - milestone M3")

    def _backend(self):
        """(in_metas, out_metas, runner) for the selected runtime.

        in/out_metas expose .name/.shape/.type (mirroring ORT metadata);
        runner(feed, out_metas) returns the raw first-output array.
        """
        if self.runtime == "tflite":
            itp = _tflite_interp(self.model_path)
            in_metas = [_LiteMeta(d) for d in itp.get_input_details()]
            out_metas = [_LiteMeta(d) for d in itp.get_output_details()]

            def run(feed, outs):
                for m in in_metas:
                    itp.set_tensor(m.index, feed[m.name])
                itp.invoke()
                return itp.get_tensor(outs[0].index)

            return in_metas, out_metas, run
        sess = _ort_session(self.model_path)
        in_metas = sess.get_inputs()
        out_metas = sess.get_outputs()

        def run(feed, outs):
            return sess.run([outs[0].name], feed)[0]

        return in_metas, out_metas, run

    def _to_input(self, value, kind: str, pname: str, meta):
        import numpy as np
        exp_shape = list(meta.shape)
        exp_type = meta.type
        if kind == "text":
            if not isinstance(value, str):
                raise OnDeviceSignatureError(
                    f"param '{pname}': Text value required, "
                    f"got {type(value).__name__}")
            if "string" not in exp_type:
                raise OnDeviceSignatureError(
                    f"param '{pname}': model input '{meta.name}' is "
                    f"{exp_type}, not a string tensor")
            arr = np.array(value, dtype=object)
            # TFLite scalar (0-d) string tensors are unreliable in the
            # Python binding (ai-edge-litert); the v1 convention is a
            # [1] string tensor (mirrors the ORT backend).
            if list(exp_shape) == [1]:
                arr = arr.reshape(1)
            elif exp_shape not in ([], [1]):
                raise OnDeviceSignatureError(
                    f"param '{pname}': model input '{meta.name}' has "
                    f"unexpected string shape {exp_shape} (v1: [1])")
            return arr
        # numeric kinds need float tensors
        if "float" not in exp_type:
            raise OnDeviceSignatureError(
                f"param '{pname}': model input '{meta.name}' is "
                f"{exp_type}, v1 supports float tensors only")
        if kind == "scalar":
            if isinstance(value, bool) or \
                    not isinstance(value, (int, float)):
                raise OnDeviceSignatureError(
                    f"param '{pname}': Float value required, "
                    f"got {type(value).__name__}")
            arr = np.asarray(float(value), dtype=np.float32)
            if exp_shape == []:
                pass
            elif exp_shape == [1]:
                arr = arr.reshape(1)
            else:
                raise OnDeviceSignatureError(
                    f"param '{pname}': model input '{meta.name}' has "
                    f"shape {exp_shape}, v1 supports scalar/1-D inputs only")
            return arr
        if kind == "vec":
            if not isinstance(value, list) or any(
                    isinstance(x, bool) or not isinstance(x, (int, float))
                    for x in value):
                raise OnDeviceSignatureError(
                    f"param '{pname}': [Float] value required")
            if len(exp_shape) != 1:
                raise OnDeviceSignatureError(
                    f"param '{pname}': model input '{meta.name}' has "
                    f"shape {exp_shape}, v1 supports 1-D inputs only")
            dim = exp_shape[0]
            if isinstance(dim, int) and dim != len(value):
                raise OnDeviceShapeError(
                    f"param '{pname}': model input '{meta.name}' "
                    f"expects {dim} values, got {len(value)}")
            return np.asarray(
                [float(x) for x in value], dtype=np.float32)
        raise OnDeviceSignatureError(  # pragma: no cover
            f"param '{pname}': unsupported")  # guarded by _sig_kind

    @staticmethod
    def _from_output(raw, kind: Optional[str]):
        import numpy as np
        arr = np.asarray(raw)
        if arr.dtype.kind in ("S", "U", "O"):
            dec = [x.decode("utf-8") if isinstance(x, bytes) else str(x)
                   for x in arr.flat]
            if kind == "text" or arr.shape == ():
                return dec[0] if dec else ""
            return dec
        flat = [float(x) for x in arr.flat]
        if kind == "scalar" or arr.shape == ():
            return flat[0] if flat else 0.0
        return flat

    def complete(self, *, model_id: str, prompt: str,
                 images: List[Tuple[str, str]],
                 timeout_s: float, args=None,
                 params=(), ret=None) -> Completion:
        """Run inference. `args` are the raw call values, `params` the
        declared (name, TypeExpr) pairs, `ret` the declared return
        TypeExpr. Returns the first model output as JSON text so the
        interpreter's existing validation applies."""
        t0 = time.monotonic()
        in_metas, out_metas, run = self._backend()
        if args is None:
            args = []
        params = list(params)
        if len(args) != len(in_metas):
            raise OnDeviceSignatureError(
                f"model takes {len(in_metas)} input(s), "
                f"called with {len(args)} arg(s)")
        if len(args) != len(params):
            raise OnDeviceSignatureError("internal: args/params mismatch")
        ret_kind = _sig_kind(ret) if ret is not None else None
        if ret is not None and ret_kind is None:
            raise OnDeviceSignatureError(
                f"return type '{ret.name}'"
                f"{'[]' if getattr(ret, 'is_list', False) else ''} is not "
                f"supported on-device in v1 (Float, [Float], Text)")
        feed = {}
        for (pname, te), value, meta in zip(params, args, in_metas):
            kind = _sig_kind(te)
            if kind is None:
                raise OnDeviceSignatureError(
                    f"param '{pname}': type '{te.name}'"
                    f"{'[]' if te.is_list else ''} is not supported "
                    f"on-device in v1 (Float, [Float], Text)")
            feed[meta.name] = self._to_input(value, kind, pname, meta)
        if not out_metas:
            raise OnDeviceSignatureError("model has no outputs")
        raw = run(feed, out_metas)
        value = self._from_output(raw, ret_kind)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return Completion(text=json.dumps(value),
                          usage=Usage(latency_ms=latency_ms))


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
