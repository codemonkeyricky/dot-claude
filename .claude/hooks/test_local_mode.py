import json, os, socket, subprocess, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local-mode.py")
HEALTH = {"ok": True, "local_model": "qwen3.8:27b", "ollama": "127.0.0.1:11434"}
HEADER = "x-local-dispatch: 1"
# Every variable the hook reads. Each test starts from none of them, then sets only what it needs.
HOOK_VARS = ("CLAUDE_LOCAL", "CLAUDE_ROUTER", "LOCAL_DISPATCH", "ANTHROPIC_BASE_URL", "HYBRID_PORT",
             "ANTHROPIC_CUSTOM_HEADERS", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
             "ALL_PROXY", "all_proxy")


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/_hybrid/health":
            body = json.dumps(HEALTH).encode()
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, port=None):
        return "http://127.0.0.1:%d" % (self.port if port is None else port)

    def run_hook(self, stdin="{}", **env):
        e = dict(os.environ)
        for k in HOOK_VARS:
            e.pop(k, None)
        e.update(env)
        r = subprocess.run([sys.executable, "-I", "-B", HOOK], input=stdin, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def parse(self, out):
        obj = json.loads(out)
        return obj["hookSpecificOutput"]["additionalContext"], obj.get("systemMessage")

    # no flags, or flags off: silent
    def test_no_flags_is_silent(self):
        self.assertEqual(self.run_hook(), "")

    def test_zero_flags_are_silent(self):
        self.assertEqual(self.run_hook(CLAUDE_LOCAL="0", CLAUDE_ROUTER="0", LOCAL_DISPATCH="0"), "")

    def test_zero_flags_are_silent_even_with_bad_router(self):
        self.assertEqual(self.run_hook(LOCAL_DISPATCH="0", ANTHROPIC_BASE_URL=self.url(closed_port())), "")

    # CLAUDE_LOCAL
    def test_local_mode_context(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_LOCAL="1"))
        self.assertIn("CLAUDE_LOCAL=1", ctx)
        self.assertIn("Work directly in this session", ctx)
        self.assertIsNone(sysmsg)

    def test_local_mode_wins_over_router_flags(self):
        port = closed_port()
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_LOCAL="1", CLAUDE_ROUTER="1", LOCAL_DISPATCH="1",
                                               ANTHROPIC_BASE_URL=self.url(port), HYBRID_PORT=str(port)))
        self.assertIn("CLAUDE_LOCAL=1", ctx)
        self.assertNotIn("WARNING", ctx)
        self.assertIsNone(sysmsg)

    def test_garbage_stdin_exits_zero(self):
        ctx, _ = self.parse(self.run_hook("not json {{{", CLAUDE_LOCAL="1"))
        self.assertIn("CLAUDE_LOCAL=1", ctx)
        self.assertEqual(self.run_hook("\x00\xff garbage"), "")

    # CLAUDE_ROUTER
    def test_router_healthy_reports_ok(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port)))
        self.assertIn("Claude router OK on :%d (local model qwen3.8:27b)." % self.port, ctx)
        self.assertNotIn("Haiku executors", ctx)
        self.assertIsNone(sysmsg)

    def test_router_down_warns(self):
        port = closed_port()
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url(port),
                                               HYBRID_PORT=str(port)))
        self.assertTrue(ctx.startswith("WARNING: Claude router on :%d is down" % port), ctx)
        self.assertIn("health check on 127.0.0.1:%d" % port, ctx)
        self.assertIn("hybrid-proxy.sh up", ctx)
        self.assertIn("unset CLAUDE_ROUTER and LOCAL_DISPATCH", ctx)
        self.assertTrue(sysmsg.startswith("WARNING: Claude router on :%d" % port))

    def test_router_flag_with_base_unset_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", HYBRID_PORT=str(self.port)))
        self.assertIn("WARNING: ANTHROPIC_BASE_URL is unset", ctx)
        self.assertIn("WARNING", sysmsg)

    # LOCAL_DISPATCH (implies the router)
    def test_dispatch_implies_router_and_reports_haiku(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port), ANTHROPIC_CUSTOM_HEADERS=HEADER))
        self.assertIn("Claude router OK on :%d" % self.port, ctx)
        self.assertIn("Haiku executors run on the local model: dispatch them one at a time.", ctx)
        self.assertNotIn("WARNING", ctx)
        self.assertIsNone(sysmsg)

    def test_dispatch_header_missing_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port)))
        self.assertIn("WARNING: LOCAL_DISPATCH=1 but ANTHROPIC_CUSTOM_HEADERS lacks x-local-dispatch: 1", ctx)
        self.assertNotIn("Haiku executors run", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_dispatch_header_wrong_value_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port), ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: 0"))
        self.assertIn("lacks x-local-dispatch: 1", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_dispatch_header_on_second_line_accepted(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port),
                                               ANTHROPIC_CUSTOM_HEADERS="X-Other: a\nX-Local-Dispatch: 1"))
        self.assertIn("Haiku executors run on the local model", ctx)
        self.assertNotIn("WARNING", ctx)
        self.assertIsNone(sysmsg)

    def test_dispatch_router_down_warns_without_haiku_line(self):
        port = closed_port()
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(port),
                                               HYBRID_PORT=str(port), ANTHROPIC_CUSTOM_HEADERS=HEADER))
        self.assertIn("WARNING: Claude router on :%d is down" % port, ctx)
        self.assertNotIn("Haiku executors run", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_dispatch_base_unset_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", HYBRID_PORT=str(self.port),
                                               ANTHROPIC_CUSTOM_HEADERS=HEADER))
        self.assertIn("ANTHROPIC_BASE_URL is unset", ctx)
        self.assertIn("WARNING", sysmsg)

    # ANTHROPIC_BASE_URL must point at the router on HYBRID_PORT
    def test_base_url_wrong_port_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(closed_port())))
        self.assertIn("does not point at the Claude router", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_base_url_with_path_warns(self):
        ctx, _ = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url() + "/v1",
                                          HYBRID_PORT=str(self.port)))
        self.assertIn("does not point at the Claude router", ctx)

    def test_localhost_base_url_accepted(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1",
                                               ANTHROPIC_BASE_URL="http://localhost:%d" % self.port,
                                               HYBRID_PORT=str(self.port)))
        self.assertIn("Claude router OK", ctx)
        self.assertIsNone(sysmsg)

    def test_health_ignores_proxy_environment(self):
        proxy = "http://127.0.0.1:%d" % closed_port()
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port), HTTP_PROXY=proxy, http_proxy=proxy))
        self.assertIn("Claude router OK", ctx)
        self.assertIsNone(sysmsg)

    def test_invalid_hybrid_port_warns_and_exits_zero(self):
        ctx, sysmsg = self.parse(self.run_hook(CLAUDE_ROUTER="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT="abc"))
        self.assertIn("HYBRID_PORT is not a port number", ctx)
        self.assertIn("WARNING", sysmsg)

    # LOCAL_DISPATCH=all (every request through the router) and the subagents alias
    def test_all_mode_healthy_reports_all(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="all", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port), ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: all"))
        self.assertIn("LOCAL_DISPATCH=all: every request, main session and subagents, runs on the local model", ctx)
        self.assertIn("Claude router OK on :%d" % self.port, ctx)
        self.assertNotIn("Haiku executors run", ctx)
        self.assertNotIn("WARNING", ctx)
        self.assertIsNone(sysmsg)

    def test_all_mode_header_missing_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="all", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port)))
        self.assertIn("WARNING: LOCAL_DISPATCH=all but ANTHROPIC_CUSTOM_HEADERS lacks x-local-dispatch: all", ctx)
        self.assertNotIn("LOCAL_DISPATCH=all: every request", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_all_mode_with_subagents_header_warns(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="all", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port),
                                               ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: subagents"))
        self.assertIn("lacks x-local-dispatch: all", ctx)
        self.assertIn("WARNING", sysmsg)

    def test_all_mode_router_down_warns(self):
        port = closed_port()
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="all", ANTHROPIC_BASE_URL=self.url(port),
                                               HYBRID_PORT=str(port), ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: all"))
        self.assertTrue(ctx.startswith("WARNING: Claude router on :%d is down" % port), ctx)
        self.assertIn("WARNING", sysmsg)

    def test_all_mode_with_local_mode_local_text_wins(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="all", CLAUDE_LOCAL="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port), ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: all"))
        self.assertIn("CLAUDE_LOCAL=1", ctx)
        self.assertNotIn("LOCAL_DISPATCH=all:", ctx)
        self.assertIsNone(sysmsg)

    def test_subagents_value_reports_haiku(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="subagents", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port),
                                               ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: subagents"))
        self.assertIn("Haiku executors run on the local model", ctx)
        self.assertNotIn("WARNING", ctx)
        self.assertIsNone(sysmsg)

    def test_value_one_accepts_subagents_header(self):
        ctx, sysmsg = self.parse(self.run_hook(LOCAL_DISPATCH="1", ANTHROPIC_BASE_URL=self.url(),
                                               HYBRID_PORT=str(self.port),
                                               ANTHROPIC_CUSTOM_HEADERS="x-local-dispatch: subagents"))
        self.assertIn("Haiku executors run on the local model", ctx)
        self.assertIsNone(sysmsg)

    def test_unknown_dispatch_value_is_off(self):
        self.assertEqual(self.run_hook(LOCAL_DISPATCH="yes"), "")
        self.assertEqual(self.run_hook(LOCAL_DISPATCH="ALL-ish"), "")


if __name__ == "__main__":
    unittest.main()
