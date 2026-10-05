#!/usr/bin/env python3
"""Phase 4 M5 verification server.

Serves ~/workspace/misa/web/ statically (so shell.html + the built
misa.js/webmodel.js load) AND answers POST /api/chat/completions with a
canned OpenAI-protocol reply. Same-origin, so the in-page wasm POST
needs no CORS. Logs every stubbed request (method, headers, body
shape) to stdout as proof the transport wiring works end-to-end.

Usage: python3 stub_server.py [port]   (default 8471, matches the
web harness's BASE default)
"""
import http.server
import json
import os
import sys

WEB_DIR = os.environ.get("MISA_WEB_DIR", os.path.expanduser("~/workspace/misa/web"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8471

REPLY = {
    "id": "chatcmpl-misa-stub",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": json.dumps({"answer": "four"}),
        },
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 120, "completion_tokens": 60,
              "total_tokens": 180},
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        sys.stdout.write("stub: " + fmt % args + "\n")
        sys.stdout.flush()

    def _cors(self):
        # file:// test pages are cross-origin: allow them in.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # wrap SimpleHTTPRequestHandler to add CORS headers
        self._cors_mixin = True
        super().do_GET()

    def end_headers(self):
        if getattr(self, "_cors_mixin", False):
            self._cors()
            self._cors_mixin = False
        super().end_headers()

    def do_POST(self):
        if self.path.rstrip("/").endswith("/chat/completions"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {"<unparseable>": body[:200].decode("utf-8", "replace")}
            sys.stdout.write(
                "STUB_REQUEST method=POST path=%s auth=%s model=%s "
                "response_format=%s body_bytes=%d\n" % (
                    self.path,
                    (self.headers.get("Authorization") or "MISSING")[:28],
                    (payload.get("model") if isinstance(payload, dict) else "?"),
                    (payload.get("response_format") if isinstance(payload, dict) else "?"),
                    len(body)))
            sys.stdout.flush()
            data = json.dumps(REPLY).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404, "no stub for " + self.path)


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"M5 stub server: web/ on http://127.0.0.1:{PORT}/ "
          f"+ POST /api/chat/completions stub", flush=True)
    srv.serve_forever()
