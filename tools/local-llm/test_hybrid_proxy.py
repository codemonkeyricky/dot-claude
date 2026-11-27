"""Tests for hybrid-proxy.py: routing, header stripping, fallback, streaming, logging.

Run: python3 -I tools/local-llm/test_hybrid_proxy.py (from the repo root), or python3 -I test_hybrid_proxy.py inside tools/local-llm
Starts the real proxy as a subprocess against fake Ollama and fake Anthropic HTTP servers (threads) that
record what they receive. Stdlib only.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "hybrid-proxy.py")
LOCAL_MODEL = "qwen3.8:27b"
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"
AUTH = "sk-ant-oat01-TEST-OAUTH-TOKEN"
API_KEY = "sk-ant-api-TEST-KEY"
MARKER = "SECRET-MARKER-4242"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def claude_body(model, marker=MARKER):
    return json.dumps({
        "model": model,
        "max_tokens": 32,
        "system": [{"type": "text", "text": f"system {marker}", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"hello {marker}"}],
        "metadata": {"user_id": "u1"},
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                  {"type": "web_search_20250305", "name": "web_search"}],
        "stream": False,
    }).encode()


class Backend:
    """A fake upstream: records requests, answers with `status` and `reply` (bytes) or streams `events`."""

    def __init__(self, name):
        self.name = name
        self.requests = []
        self.status = 200
        self.reply = json.dumps({"id": f"msg-{name}", "type": "message"}).encode()
        self.events = None  # list of byte chunks streamed with a gap; set for the streaming test
        self.release = threading.Event()
        self.gap_ok = None
        backend = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                backend.requests.append({
                    "method": self.command, "path": self.path, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                if backend.events is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for i, chunk in enumerate(backend.events):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if i == 0:  # hold the rest until the client has read the first event
                            backend.gap_ok = backend.release.wait(5)
                    return
                self.send_response(backend.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(backend.reply)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(backend.reply)

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Proxy:
    def __init__(self, ollama_port, anthropic_port):
        self.port = free_port()
        self.log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update({
            "HYBRID_PORT": str(self.port),
            "OLLAMA_HOST": f"127.0.0.1:{ollama_port}",
            "LOCAL_LLM_MODEL": LOCAL_MODEL,
            "HYBRID_ANTHROPIC_URL": f"http://127.0.0.1:{anthropic_port}",
            "NO_PROXY": "127.0.0.1,localhost",
        })
        self.proc = subprocess.Popen([sys.executable, "-I", PROXY], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                self.request("GET", "/_hybrid/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("proxy did not start")

    def request(self, method, path, body=None, headers=None):
        """(status, body bytes, response headers) via a raw HTTP/1.0 connection to the proxy."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, data, resp)
        conn.close()
        return out

    def log_text(self):
        self.log.flush()
        with open(self.log.name) as fh:
            return fh.read()

    def wait_log(self, needle, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.log_text()
            if needle in text:
                return text
            time.sleep(0.05)
        return self.log_text()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        os.unlink(self.log.name)


def post(proxy, body, model_headers):
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               "anthropic-beta": "prompt-caching-2024-07-31"}
    headers.update(model_headers)
    return proxy.request("POST", "/v1/messages", body=body, headers=headers)


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = Backend("ollama")
        cls.anthropic = Backend("anthropic")
        cls.proxy = Proxy(cls.ollama.port, cls.anthropic.port)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.stop()
        cls.ollama.close()
        cls.anthropic.close()

    def setUp(self):
        self.ollama.requests.clear()
        self.anthropic.requests.clear()
        self.ollama.status = 200
        self.ollama.reply = json.dumps({"id": "msg-ollama", "type": "message", "content": [{"text": "OK"}]}).encode()
        self.anthropic.status = 200
        self.ollama.events = None
        self.anthropic.events = None

    def auth_headers(self):
        return {"authorization": f"Bearer {AUTH}", "x-api-key": API_KEY}

    def test_haiku_with_header_goes_to_ollama(self):
        body = claude_body(HAIKU)
        status, data, _ = post(self.proxy, body, {**self.auth_headers(), "x-local-dispatch": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["id"], "msg-ollama")
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertEqual(self.anthropic.requests, [])
        req = self.ollama.requests[0]
        sent = json.loads(req["body"])
        self.assertEqual(sent["model"], LOCAL_MODEL)
        self.assertEqual(sent["metadata"], {"user_id": "u1"})  # nothing in STRIP_* today: fields kept
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(req["headers"]["authorization"], "Bearer ollama")
        self.assertNotIn("x-api-key", req["headers"])
        self.assertNotIn("x-local-dispatch", req["headers"])

    def test_header_name_is_case_insensitive(self):
        status, _, _ = post(self.proxy, claude_body(HAIKU), {"X-Local-Dispatch": "1", **self.auth_headers()})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertNotIn("x-local-dispatch", self.ollama.requests[0]["headers"])

    def test_header_value_other_than_1_is_not_routed(self):
        body = claude_body(HAIKU)
        post(self.proxy, body, {"x-local-dispatch": "0", **self.auth_headers()})
        self.assertEqual(self.ollama.requests, [])
        self.assertEqual(self.anthropic.requests[0]["body"], body)

    def test_haiku_without_header_goes_to_anthropic_byte_identical(self):
        body = claude_body(HAIKU)
        status, data, _ = post(self.proxy, body, self.auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(self.ollama.requests, [])
        req = self.anthropic.requests[0]
        self.assertEqual(req["body"], body)
        self.assertEqual(req["headers"]["authorization"], f"Bearer {AUTH}")
        self.assertEqual(req["headers"]["x-api-key"], API_KEY)
        self.assertNotIn("x-local-dispatch", req["headers"])

    def test_opus_with_header_goes_to_anthropic(self):
        body = claude_body(OPUS)
        status, _, _ = post(self.proxy, body, {"x-local-dispatch": "1", **self.auth_headers()})
        self.assertEqual(status, 200)
        self.assertEqual(self.ollama.requests, [])
        req = self.anthropic.requests[0]
        self.assertEqual(req["body"], body)
        self.assertEqual(req["headers"]["authorization"], f"Bearer {AUTH}")
        self.assertNotIn("x-local-dispatch", req["headers"])

    def all_headers(self):
        return {"x-local-dispatch": "all", **self.auth_headers()}

    def test_all_mode_routes_opus_to_ollama_with_model_rewritten(self):
        body = claude_body(OPUS)
        status, data, _ = post(self.proxy, body, self.all_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["id"], "msg-ollama")
        self.assertEqual(self.anthropic.requests, [])
        self.assertEqual(len(self.ollama.requests), 1)
        req = self.ollama.requests[0]
        self.assertEqual(json.loads(req["body"])["model"], LOCAL_MODEL)
        self.assertEqual(req["headers"]["authorization"], "Bearer ollama")
        self.assertNotIn("x-api-key", req["headers"])
        self.assertNotIn("x-local-dispatch", req["headers"])
        self.assertIn("backend=ollama", self.proxy.wait_log("backend=ollama"))

    def test_all_mode_routes_haiku_to_ollama(self):
        status, _, _ = post(self.proxy, claude_body(HAIKU), self.all_headers())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertEqual(self.anthropic.requests, [])

    def test_all_mode_ollama_down_is_502_and_never_reaches_anthropic(self):
        proxy = Proxy(free_port(), self.anthropic.port)  # nothing listens on the Ollama port
        try:
            status, data, _ = post(proxy, claude_body(OPUS), self.all_headers())
            self.assertEqual(status, 502)
            self.assertIn(b"hybrid-proxy: ollama unreachable:", data)
            self.assertEqual(self.anthropic.requests, [])
            text = proxy.wait_log("backend=ollama status=502")
            self.assertIn("backend=ollama status=502", text)
            self.assertNotIn("fallback=", text)
        finally:
            proxy.stop()

    def test_all_mode_ollama_error_status_is_relayed_not_retried(self):
        self.ollama.status = 500
        status, _, _ = post(self.proxy, claude_body(OPUS), self.all_headers())
        self.assertEqual(status, 500)
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertEqual(self.anthropic.requests, [])

    def test_all_mode_model_less_request_goes_to_anthropic_without_header(self):
        body = json.dumps({"max_tokens": 8, "messages": []}).encode()
        status, _, _ = post(self.proxy, body, self.all_headers())
        self.assertEqual(status, 200)
        self.assertEqual(self.ollama.requests, [])
        req = self.anthropic.requests[0]
        self.assertEqual(req["body"], body)
        self.assertNotIn("x-local-dispatch", req["headers"])

    def test_subagents_value_routes_only_haiku(self):
        post(self.proxy, claude_body(OPUS), {"x-local-dispatch": "subagents", **self.auth_headers()})
        self.assertEqual(self.ollama.requests, [])
        self.assertEqual(len(self.anthropic.requests), 1)
        post(self.proxy, claude_body(HAIKU), {"x-local-dispatch": "subagents", **self.auth_headers()})
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertNotIn("x-local-dispatch", self.ollama.requests[0]["headers"])

    def test_unknown_dispatch_value_is_off(self):
        body = claude_body(HAIKU)
        post(self.proxy, body, {"x-local-dispatch": "yes", **self.auth_headers()})
        self.assertEqual(self.ollama.requests, [])
        self.assertEqual(self.anthropic.requests[0]["body"], body)

    def test_no_header_opus_goes_to_anthropic(self):
        body = claude_body(OPUS)
        status, _, _ = post(self.proxy, body, self.auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(self.ollama.requests, [])
        self.assertEqual(self.anthropic.requests[0]["body"], body)

    def test_legacy_local_model_name_still_routes_to_ollama(self):
        body = json.dumps({"model": LOCAL_MODEL, "max_tokens": 8, "messages": []}).encode()
        post(self.proxy, body, self.auth_headers())
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertEqual(self.ollama.requests[0]["headers"]["authorization"], "Bearer ollama")
        self.assertEqual(self.anthropic.requests, [])

    def test_ollama_500_falls_back_to_anthropic_with_original_request(self):
        self.ollama.status = 500
        body = claude_body(HAIKU)
        status, data, _ = post(self.proxy, body, {**self.auth_headers(), "x-local-dispatch": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["id"], "msg-anthropic")
        self.assertEqual(len(self.ollama.requests), 1)
        self.assertEqual(len(self.anthropic.requests), 1)
        req = self.anthropic.requests[0]
        self.assertEqual(req["body"], body)
        self.assertEqual(req["headers"]["authorization"], f"Bearer {AUTH}")
        self.assertEqual(req["headers"]["x-api-key"], API_KEY)
        self.assertNotIn("x-local-dispatch", req["headers"])
        text = self.proxy.wait_log("backend=anthropic-fallback")
        self.assertIn('backend=anthropic-fallback status=200', text)
        self.assertIn('fallback="ollama HTTP 500"', text)

    def test_ollama_down_falls_back_to_anthropic(self):
        dead = free_port()  # nothing listens here: connection refused
        proxy = Proxy(dead, self.anthropic.port)
        try:
            body = claude_body(HAIKU)
            status, data, _ = post(proxy, body, {**self.auth_headers(), "x-local-dispatch": "1"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["id"], "msg-anthropic")
            req = self.anthropic.requests[-1]
            self.assertEqual(req["body"], body)
            self.assertEqual(req["headers"]["authorization"], f"Bearer {AUTH}")
            text = proxy.wait_log("backend=anthropic-fallback")
            self.assertIn("backend=anthropic-fallback", text)
            self.assertIn("fallback=\"ollama unreachable:", text)
        finally:
            proxy.stop()

    def test_path_not_starting_with_slash_is_refused_400(self):
        sock = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
        data = b""
        try:
            sock.sendall(b"POST v1/messages HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertTrue(data.startswith(b"HTTP/1.0 400"), data[:120])
        self.assertIn(b"request path must start with /", data)
        self.assertEqual(self.ollama.requests, [])
        self.assertEqual(self.anthropic.requests, [])

    def test_streaming_is_relayed_as_it_arrives(self):
        self.ollama.events = [b"event: message_start\ndata: {}\n\n", b"event: message_stop\ndata: {}\n\n"]
        self.ollama.release = threading.Event()
        conn = __import__("http.client").client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        headers = {"content-type": "application/json", "x-local-dispatch": "1", **self.auth_headers()}
        body = claude_body(HAIKU)
        conn.request("POST", "/v1/messages", body=body, headers=headers)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        first = resp.readline()
        self.assertEqual(first, b"event: message_start\n")  # arrived before the fake sent the second event
        self.ollama.release.set()
        rest = resp.read()
        conn.close()
        self.assertTrue(self.ollama.gap_ok, "first SSE event was not relayed before the upstream sent the rest")
        self.assertIn(b"event: message_stop", rest)
        self.assertEqual(self.anthropic.requests, [])

    def test_health_reports_routing_and_fallback(self):
        status, data, _ = self.proxy.request("GET", "/_hybrid/health")
        self.assertEqual(status, 200)
        info = json.loads(data)
        self.assertTrue(info["ok"])
        self.assertEqual(info["routing"], "header")
        self.assertIs(info["fallback"], True)
        self.assertEqual(info["local_model"], LOCAL_MODEL)

    def test_log_never_contains_headers_or_bodies(self):
        self.ollama.status = 500
        post(self.proxy, claude_body(HAIKU), {**self.auth_headers(), "x-local-dispatch": "1"})
        text = self.proxy.wait_log("backend=anthropic-fallback")
        for secret in (AUTH, API_KEY, MARKER, "x-api-key", "authorization"):
            self.assertNotIn(secret, text)

    def test_local_body_strips_configured_fields_only(self):
        spec = importlib.util.spec_from_file_location("hybrid_proxy_mod", PROXY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = claude_body(HAIKU)
        self.assertEqual(json.loads(mod.local_body(raw))["model"], mod.LOCAL_MODEL)
        saved_fields, saved_types = mod.STRIP_BODY_FIELDS, mod.STRIP_TOOL_TYPES
        try:
            mod.STRIP_BODY_FIELDS = ("metadata",)
            mod.STRIP_TOOL_TYPES = ("web_search_20250305",)
            out = json.loads(mod.local_body(raw))
            self.assertNotIn("metadata", out)
            self.assertEqual([t["name"] for t in out["tools"]], ["Bash"])
            self.assertEqual(out["system"][0]["cache_control"], {"type": "ephemeral"})
        finally:
            mod.STRIP_BODY_FIELDS, mod.STRIP_TOOL_TYPES = saved_fields, saved_types


if __name__ == "__main__":
    unittest.main(verbosity=2)
