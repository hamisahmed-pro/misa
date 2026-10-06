"""misa serve — local dev server for Misa web apps with a secure key proxy.

Usage:
    misa serve <file.misa> [--port PORT] [--key KEY] [--key-env VAR]
               [--base-url URL]

Builds the app to WebAssembly, serves it on localhost, and exposes a
same-origin /api/openai proxy that injects the provider API key
server-side — the browser never sees the secret. This is the local
twin of the production proxy pattern (see docs/TOOLING.md and the
PlantDoc Live deployment).

The key comes from --key, --key-env (default MISA_MODEL_KEY), or the
MISA_MODEL_KEY environment variable. The upstream base URL comes from
--base-url or MISA_OPENAI_BASE_URL.

Guards (demo-grade, same as production): model allowlist derived from
the app's own `openai("...")` / `gemini("...")` declarations,
max_tokens capped at 600, 30 requests / 10 min per IP, no streaming.
"""
import http.server
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

IMPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


SHELL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — served by misa</title>
<style>
  :root { color-scheme: light; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 16px;
          background: #f6f5ef; color: #171a2b; }
  #misa-root { max-width: 640px; margin: 0 auto; }
  .misa-screen { display: flex; flex-direction: column; gap: 8px; }
  .misa-screen p { margin: 0; }
  .misa-banner { padding: 10px 14px; border-radius: 8px; font-weight: 500; }
  .misa-banner-info { background: #e3e7ff; color: #151a46; }
  .misa-banner-warning { background: #fff3d6; color: #6b4e00; }
  .misa-banner-error { background: #ffe0da; color: #8f1d0e; }
  button { padding: 10px 18px; border: none; border-radius: 8px;
            background: #5548f5; color: #fff; font-size: 15px; cursor: pointer; }
  input[type="text"] { padding: 10px 12px; border: 1px solid #c9c7bd;
            border-radius: 8px; font-size: 15px; width: 100%;
            box-sizing: border-box; }
  .misa-image { max-width: 100%; border-radius: 8px; }
  #misa-loading { color: #6b6f85; }
  .serve-note { max-width: 640px; margin: 0 auto 12px; padding: 10px 14px;
            background: #e3e7ff; color: #151a46; border-radius: 8px;
            font-size: 14px; }
</style>
</head>
<body>
<div class="serve-note">Live AI via <code>misa serve</code> proxy — the API key
stays on this machine.</div>
<div id="misa-root"><p id="misa-loading">loading&hellip;</p></div>
<script>
  window.MISA_ENV = {
    MISA_MODEL_KEY: 'misa-serve-proxy',
    MISA_OPENAI_BASE_URL: '/api/openai',
  };
  window.misaBootError = null;
  window.addEventListener('error', function(ev) {
    window.misaBootError = String((ev && ev.message) || ev);
  });
  (function() {
    var s = document.createElement('script');
    s.src = 'app.js';
    s.onload = function() { bootMisa(); };
    s.onerror = function() {
      window.misaBootError = 'failed to load app.js';
    };
    document.head.appendChild(s);
  })();
  function bootMisa() {
    MisaModule().then(function(Module) {
      var loading = document.getElementById('misa-loading');
      try { Module.ccall('misa_platform_fs_bootstrap', null, [], []); }
      catch (e) { console.error('misa: fs bootstrap failed', e); }
      try { Module.ccall('misa_ui_boot', null, [], []); }
      catch (e) {
        console.error('misa: boot failed', e);
        window.misaBootError = String((e && e.message) || e);
      }
      if (loading && loading.parentNode) loading.parentNode.removeChild(loading);
      window.misaReady = true;
    }).catch(function(e) {
      console.error('misa: module load failed', e);
      window.misaBootError = String((e && e.message) || e);
    });
  }
</script>
</body>
</html>
"""

WINDOW_MS = 10 * 60 * 1000
MAX_REQ_PER_WINDOW = 30
MAX_TOKENS_CAP = 600


def _model_allowlist(source_path):
    """Model ids declared in the app source (v1: main file only)."""
    with open(source_path, encoding="utf-8") as f:
        src = f.read()
    ids = set(re.findall(r'\bopenai\(\s*"([^"]+)"', src))
    ids |= set(re.findall(r'\bgemini\(\s*"([^"]+)"', src))
    return ids


class _ProxyState:
    def __init__(self, key, base_url, allowlist):
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.allowlist = allowlist
        self.buckets = {}
        self.lock = threading.Lock()

    def rate_limited(self, ip):
        now = time.time() * 1000
        with self.lock:
            arr = [t for t in self.buckets.get(ip, []) if now - t < WINDOW_MS]
            if len(arr) >= MAX_REQ_PER_WINDOW:
                return True
            arr.append(now)
            self.buckets[ip] = arr
            return False


def _make_handler(webdir, state):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=webdir, **kw)

        def log_message(self, fmt, *args):
            sys.stderr.write("misa serve: " + fmt % args + "\n")

        def _proxy(self, path):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.rate_limited_client():
                return self._json(429, {"error": "rate limited"})
            if path == "chat/completions":
                if self.command != "POST":
                    return self._json(405, {"error": "method not allowed"})
                try:
                    body = json.loads(raw.decode() or "{}")
                except ValueError:
                    return self._json(400, {"error": "invalid JSON"})
                if body.get("model") not in state.allowlist:
                    return self._json(403, {"error": "model not allowed"})
                if body.get("stream"):
                    return self._json(400, {"error": "streaming not supported"})
                if isinstance(body.get("max_tokens"), (int, float)) \
                        and body["max_tokens"] > MAX_TOKENS_CAP:
                    body["max_tokens"] = MAX_TOKENS_CAP
                raw = json.dumps(body).encode()
            elif path == "models":
                if self.command != "GET":
                    return self._json(405, {"error": "method not allowed"})
                raw = None
            else:
                return self._json(404, {"error": "not found"})
            url = f"{state.base_url}/{path}"
            req = urllib.request.Request(
                url, data=raw, method=self.command,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {state.key}"})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get(
                        "Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json(502, {"error": f"upstream unreachable: {e}"})

        def rate_limited_client(self):
            ip = self.headers.get("X-Forwarded-For",
                                  self.client_address[0]).split(",")[0].strip()
            return state.rate_limited(ip)

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/api/openai/"):
                return self._route()
            return super().do_GET()

        def do_POST(self):
            return self._route()

        def _route(self):
            if self.path.startswith("/api/openai/"):
                sub = self.path[len("/api/openai/"):].split("?")[0]
                return self._proxy(sub)
            # serve index.html at /
            if self.path == "/":
                self.path = "/index.html"
            return super().do_GET()

    # .wasm MIME type (SimpleHTTPRequestHandler lacks it on some systems)
    Handler.extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
    }
    return Handler


def cmd_serve(args):
    """argv after 'serve'. Returns exit code."""
    src, port, key, key_env, base_url = None, 8000, None, "MISA_MODEL_KEY", None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port, i = int(args[i + 1]), i + 2
        elif a == "--key" and i + 1 < len(args):
            key, i = args[i + 1], i + 2
        elif a == "--key-env" and i + 1 < len(args):
            key_env, i = args[i + 1], i + 2
        elif a == "--base-url" and i + 1 < len(args):
            base_url, i = args[i + 1], i + 2
        elif not a.startswith("-") and src is None:
            src, i = a, i + 1
        else:
            sys.stderr.write(f"misa serve: unknown argument: {a}\n")
            return 2
    if src is None:
        sys.stderr.write(
            "usage: misa serve <file.misa> [--port PORT] [--key KEY]\n"
            "               [--key-env VAR] [--base-url URL]\n")
        return 2
    if key is None:
        key = os.environ.get(key_env, "")
    if not key:
        sys.stderr.write(
            f"misa serve: no API key (set {key_env} or pass --key)\n")
        return 2
    if base_url is None:
        base_url = os.environ.get("MISA_OPENAI_BASE_URL", "")
    if not base_url:
        sys.stderr.write(
            "misa serve: no base URL (set MISA_OPENAI_BASE_URL or --base-url)\n")
        return 2

    sys.path.insert(0, IMPL_DIR)
    from misa.aot.build import build as aot_build
    from misa.aot.emit import AOTError

    webdir = tempfile.mkdtemp(prefix="misa-serve-")
    js_out = os.path.join(webdir, "app.js")
    sys.stderr.write(f"misa serve: building {src} ...\n")
    try:
        ok, errors = aot_build(src, js_out, target="wasm32-unknown-emscripten")
    except AOTError as e:
        # C-2: clean E-code, not an unhandled traceback.
        sys.stderr.write(f"misa serve: {e.code}: {e.message}\n")
        return 1
    except RuntimeError as e:
        sys.stderr.write(f"misa serve: build failed: {e}\n")
        return 1
    if not ok:
        for e in errors:
            sys.stderr.write(f"  {e}\n")
        return 1
    # emcc emits app.js + app.wasm alongside
    title = os.path.splitext(os.path.basename(src))[0]
    html = SHELL_HTML.replace("__TITLE__", title)
    with open(os.path.join(webdir, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(html)

    allowlist = _model_allowlist(src)
    state = _ProxyState(key, base_url, allowlist)
    handler = _make_handler(webdir, state)
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler) as srv:
        srv.daemon_threads = True
        sys.stderr.write(
            f"misa serve: {src} at http://127.0.0.1:{port}/\n"
            f"            proxy -> {base_url} "
            f"(models: {sorted(allowlist) or 'none declared'})\n")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
