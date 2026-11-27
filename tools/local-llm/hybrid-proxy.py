#!/usr/bin/env python3
"""Claude Code router on 127.0.0.1: local Ollama for dispatched requests, Anthropic for everything else.

Routing, per request. The x-local-dispatch header selects the mode (claude-route sends it from LOCAL_DISPATCH):
- `x-local-dispatch: all` (LOCAL_DISPATCH=all): a POST whose JSON body has a string `model` goes to Ollama as
  LOCAL_LLM_MODEL, whatever the model name, for the main session and subagents alike. Checked first. No fallback:
  if Ollama fails, the client gets 502.
- `x-local-dispatch: subagents` or `1` (LOCAL_DISPATCH=subagents or 1): a request whose JSON body's model is a
  Claude Haiku model (claude-haiku*, claude-3-5-haiku*, claude-3-haiku*) goes to Ollama as LOCAL_LLM_MODEL.
  Any other header value is ignored here (off).
- Model route (backward compatibility): the body's model names LOCAL_LLM_MODEL. No fallback for it.
- Everything else goes to HYBRID_ANTHROPIC_URL (default https://api.anthropic.com) with the caller's
  body bytes and headers unchanged, so the Max-plan OAuth login passes through.
- The x-local-dispatch header is never forwarded anywhere.
- Fallback: only a subagents-routed (header) request that fails at Ollama before any response byte reaches the client
  (connection error, timeout, or HTTP status >= 400) is resent with its original body and headers to
  Anthropic; the log line says backend=anthropic-fallback and why.

Stdlib only. Run as: python3 -I hybrid-proxy.py [--port N]
Env: HYBRID_PORT (4000), OLLAMA_HOST (127.0.0.1:11434), LOCAL_LLM_MODEL (qwen3.8:27b),
HYBRID_ANTHROPIC_URL (https://api.anthropic.com; tests point it at a fake server).
Listens on 127.0.0.1 only. Logs one line per request to stderr. Never logs headers or bodies.
"""
import argparse
import datetime
import http.server
import json
import os
import sys
import time
import urllib.error
import urllib.request

ANTHROPIC = os.environ.get("HYBRID_ANTHROPIC_URL", "https://api.anthropic.com").rstrip("/")
LOCAL_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen3.8:27b")
LOCAL_NAME = LOCAL_MODEL.split(":", 1)[0]
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
OLLAMA = OLLAMA_HOST if OLLAMA_HOST.startswith("http") else "http://" + OLLAMA_HOST
DISPATCH_HEADER = "x-local-dispatch"
HAIKU_PREFIXES = ("claude-haiku", "claude-3-5-haiku", "claude-3-haiku")
HOP_BY_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length"}
# Probed against Ollama /v1/messages with qwen3.8:27b: a Claude-Code-shaped body (cache_control blocks,
# metadata, thinking, context_management, tool_choice, a web_search_20250305 server tool) plus
# anthropic-beta/anthropic-version headers all returned 200 with streamed text, so nothing is stripped.
# Add a body field or tool type here only after a probe shows Ollama failing with it present and succeeding without it.
STRIP_BODY_FIELDS = ()
STRIP_TOOL_TYPES = ()
# Caller credentials never reach Ollama; Authorization is replaced by a dummy bearer token there.
STRIP_HEADERS = ("x-api-key", "authorization")
TIMEOUT = 600
# The local backend ignores proxy environment variables; the Anthropic backend uses them as usual.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REMOTE_OPENER = urllib.request.build_opener()


def is_hop_by_hop(name):
    low = name.lower()
    return low in HOP_BY_HOP or low.startswith("proxy-")


def is_local_model(model):
    return isinstance(model, str) and (model == LOCAL_MODEL or model.startswith(LOCAL_NAME))


def is_haiku(model):
    return isinstance(model, str) and model.startswith(HAIKU_PREFIXES)


def local_body(body):
    """The body Ollama receives: model set to LOCAL_MODEL, STRIP_BODY_FIELDS and STRIP_TOOL_TYPES removed."""
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("request body is not a JSON object")
    data["model"] = LOCAL_MODEL
    for key in STRIP_BODY_FIELDS:
        data.pop(key, None)
    tools = data.get("tools")
    if STRIP_TOOL_TYPES and isinstance(tools, list):
        data["tools"] = [t for t in tools if not (isinstance(t, dict) and t.get("type") in STRIP_TOOL_TYPES)]
    return json.dumps(data).encode()


def read_body(handler):
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        out = bytearray()
        while True:
            size = int(handler.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(out)
            out += handler.rfile.read(size)
            handler.rfile.readline()
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length > 0 else b""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hybrid-proxy"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def _handle(self):
        started = time.monotonic()
        if not self.path.startswith("/"):  # absolute-form or bare request targets are refused
            self._send_error(400, "hybrid-proxy: request path must start with /")
            self._log(None, "none", 400, started)
            return
        route = self.path.split("?", 1)[0]
        if self.command == "GET" and route == "/_hybrid/health":
            body = json.dumps({"ok": True, "local_model": LOCAL_MODEL, "ollama": OLLAMA_HOST,
                               "routing": "header", "fallback": True}).encode()
            self._send_bytes(200, [("Content-Type", "application/json")], body)
            return
        try:
            body = read_body(self)
        except (ValueError, OSError):
            self._send_error(400, "hybrid-proxy: malformed request body")
            return
        model = None
        if self.command == "POST" and body:
            try:
                model = json.loads(body).get("model")
            except (ValueError, AttributeError):
                model = None
        dispatch_mode = (self.headers.get(DISPATCH_HEADER) or "").strip().lower()
        if dispatch_mode == "all" and self.command == "POST" and isinstance(model, str):
            kind = "all"
        elif dispatch_mode in ("1", "subagents") and is_haiku(model):
            kind = "header"
        elif is_local_model(model):
            kind = "model"
        else:
            kind = None
        original = self._forward_headers(())
        if kind is None:
            self._proxy_remote(body, original, model, "anthropic", started)
            return
        local_headers = self._forward_headers(STRIP_HEADERS)
        local_headers["Authorization"] = "Bearer ollama"
        resp, err = self._open(OLLAMA, local_body(body), local_headers, LOCAL_OPENER)
        reason = f"ollama {err}" if err else ""
        if resp is not None and kind == "header" and resp.getcode() >= 400:
            code = resp.getcode()
            resp.close()
            resp, reason = None, f"ollama HTTP {code}"
        if resp is None and kind == "header":
            self._proxy_remote(body, original, model, "anthropic-fallback", started, reason)
            return
        if resp is None:
            self._send_error(502, f"hybrid-proxy: {reason}")
            self._log(model, "ollama", 502, started)
            return
        self._relay_log(resp, model, "ollama", started)

    def _forward_headers(self, drop):
        headers = {}
        for key, value in self.headers.items():
            low = key.lower()
            if is_hop_by_hop(low) or low in ("accept-encoding", DISPATCH_HEADER) or low in drop:
                continue
            headers[key] = value
        return headers

    def _open(self, base, data, headers, opener):
        """(response, "") on success; (HTTPError, "") for a backend error status; (None, reason) when unreachable."""
        req = urllib.request.Request(base + self.path, data=data or None, headers=headers, method=self.command)
        try:
            return opener.open(req, timeout=TIMEOUT), ""
        except urllib.error.HTTPError as exc:
            return exc, ""  # relayed as-is unless the caller falls back
        except (OSError, ValueError) as exc:
            return None, f"unreachable: {getattr(exc, 'reason', exc)}"

    def _proxy_remote(self, body, headers, model, backend, started, fallback=""):
        resp, err = self._open(ANTHROPIC, body, headers, REMOTE_OPENER)
        if resp is None:
            self._send_error(502, f"hybrid-proxy: anthropic {err}")
            self._log(model, backend, 502, started, fallback=fallback)
            return
        self._relay_log(resp, model, backend, started, fallback=fallback)

    def _relay_log(self, resp, model, backend, started, fallback=""):
        status, err = resp.getcode(), ""
        try:
            self._relay(status, resp)
        except OSError as exc:  # client went away or upstream stream broke
            err = type(exc).__name__
        finally:
            resp.close()
        self._log(model, backend, status, started, err, fallback)

    def _relay(self, status, resp):
        self.send_response(status)
        for key, value in resp.headers.items():
            if not is_hop_by_hop(key) and key.lower() not in ("date", "server"):  # send_response adds these
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        src = resp.fp if isinstance(resp, urllib.error.HTTPError) and resp.fp is not None else resp
        read = src.read1 if hasattr(src, "read1") else src.read
        while True:
            chunk = read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True

    def _send_bytes(self, status, headers, body):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_error(self, status, message):
        body = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()
        self._send_bytes(status, [("Content-Type", "application/json")], body)

    def _log(self, model, backend, status, started, err="", fallback=""):
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        ms = (time.monotonic() - started) * 1000
        model_text = json.dumps(model)[:80] if model is not None else "-"
        route_text = json.dumps(self.path.split("?", 1)[0])[:200]
        suffix = f" err={err}" if err else ""
        if fallback:
            suffix += f" fallback={json.dumps(fallback)[:200]}"
        sys.stderr.write(f"{stamp} {self.command} {route_text} model={model_text} "
                         f"backend={backend} status={status} {ms:.0f}ms{suffix}\n")
        sys.stderr.flush()

    def log_message(self, format, *args):  # the per-request line above replaces the default log
        pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Claude Code router: Haiku dispatch to Ollama, everything else to Anthropic.")
    parser.add_argument("--port", type=int, default=None, help="listen port (default: HYBRID_PORT or 4000)")
    args = parser.parse_args()
    port = args.port if args.port is not None else os.environ.get("HYBRID_PORT", "4000")
    try:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError(port)
    except ValueError:
        print(f"HYBRID_PORT must be a port number, got: {port}", file=sys.stderr)
        return 2
    try:
        server = ThreadingServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"hybrid-proxy cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        return 1
    print(f"hybrid-proxy on 127.0.0.1:{port}: {LOCAL_MODEL} via {OLLAMA}, other requests to {ANTHROPIC}, pid {os.getpid()}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
