#!/usr/bin/env python3
"""SessionStart hook: tells the model how this session is routed.

- CLAUDE_LOCAL=1 (local mode) wins: the session works directly, without executors. Nothing else is checked.
- CLAUDE_ROUTER=1 or LOCAL_DISPATCH=1|subagents|all (a LOCAL_DISPATCH value implies the router; 1 is the alias
  of subagents): checks the router's /_hybrid/health (2 s timeout, proxy environment ignored). A healthy router is
  reported. LOCAL_DISPATCH=1 or subagents says that Haiku executors run on the local model. LOCAL_DISPATCH=all says
  that every request, main session and subagents, runs on the local model with no Anthropic fallback. A dispatch
  value warns when ANTHROPIC_CUSTOM_HEADERS lacks `x-local-dispatch` with the same value. Other LOCAL_DISPATCH
  values count as off.
- A router that is down, or an ANTHROPIC_BASE_URL that does not point at it, gives WARNING text in the context
  and a systemMessage.
- With CLAUDE_LOCAL, CLAUDE_ROUTER and LOCAL_DISPATCH all unset or 0, prints nothing. Always exits 0.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HEALTH_TIMEOUT = 2
DEFAULT_PORT = "4000"
DISPATCH_LINE = re.compile(r"^\s*x-local-dispatch\s*:\s*(\S+)\s*$", re.IGNORECASE)
DISPATCH_MODES = {"1": "subagents", "subagents": "subagents", "all": "all"}
# The router listens on loopback. Proxy variables such as HTTP_PROXY must not send the health check elsewhere.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LOCAL_TEXT = ("CLAUDE_LOCAL=1: local mode. Work directly in this session. Do not dispatch executors or Agent "
              "subagents for implementation; the executor, task and review rules do not apply.")
HAIKU_TEXT = "Haiku executors run on the local model: dispatch them one at a time."
HEADER_TEXT = ("LOCAL_DISPATCH={raw} but ANTHROPIC_CUSTOM_HEADERS lacks x-local-dispatch: {raw}, so those requests go "
               "to Anthropic, not the local model. Start the session with claude-route, claude-hybrid or claude-local-all.")
ALL_TEXT = ("LOCAL_DISPATCH=all: every request, main session and subagents, runs on the local model with no Anthropic "
            "fallback. Expect slower, weaker output; dispatch executors one at a time.")


def flag(name):
    return os.environ.get(name, "").strip() == "1"


def hybrid_port():
    raw = os.environ.get("HYBRID_PORT", "").strip() or DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def base_points_at_router(base, port):
    try:
        parts = urllib.parse.urlsplit(base)
        host, got = parts.hostname, parts.port
    except ValueError:
        return False
    return (parts.scheme == "http" and host in ("127.0.0.1", "localhost")
            and got == port and parts.path in ("", "/"))


def dispatch_mode():
    """The mode ("subagents" or "all") when LOCAL_DISPATCH is on; None when it is off, unset or unknown."""
    return DISPATCH_MODES.get(os.environ.get("LOCAL_DISPATCH", "").strip().lower())


def header_mode():
    """The mode the x-local-dispatch header carries, or None when the header is missing or unknown."""
    headers = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    for line in headers.splitlines():
        match = DISPATCH_LINE.match(line)
        if match:
            return DISPATCH_MODES.get(match.group(1).lower())
    return None


def health(base):
    """(health dict, "") when the router answers ok; (None, reason) otherwise."""
    try:
        with DIRECT.open(base.rstrip("/") + "/_hybrid/health", timeout=HEALTH_TIMEOUT) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        return None, str(getattr(exc, "reason", exc))
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None, "health reply is not {ok: true}"
    return data, ""


def router_findings(port):
    """(warnings, info, healthy) for the router side of the session."""
    if port is None:
        return ["HYBRID_PORT is not a port number (1-65535)."], [], False
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return [f"ANTHROPIC_BASE_URL is unset; it should be http://127.0.0.1:{port}."], [], False
    if not base_points_at_router(base, port):
        return [f"ANTHROPIC_BASE_URL does not point at the Claude router on :{port}."], [], False
    data, reason = health(base)
    if data is None:
        warning = (f"Claude router on :{port} is down (health check on 127.0.0.1:{port}: {reason}); "
                   "API calls will fail. Start it with hybrid-proxy.sh up, or restart the hybrid-proxy unit. "
                   "To work without the router, unset CLAUDE_ROUTER and LOCAL_DISPATCH.")
        return [warning], [], False
    model = str(data.get("local_model", "unknown"))
    return [], [f"Claude router OK on :{port} (local model {model})."], True


def emit(context, system_message=None):
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    if system_message:
        out["systemMessage"] = system_message
    sys.stdout.write(json.dumps(out))


def main():
    try:
        sys.stdin.read()  # the SessionStart payload is not needed; any content is tolerated
    except (OSError, ValueError):
        pass
    if flag("CLAUDE_LOCAL"):
        emit(LOCAL_TEXT)
        return
    mode = dispatch_mode()
    if mode is None and not flag("CLAUDE_ROUTER"):
        return
    warnings, info, healthy = router_findings(hybrid_port())
    if mode is not None:
        if header_mode() != mode:
            raw = os.environ.get("LOCAL_DISPATCH", "").strip()
            warnings.append(HEADER_TEXT.format(raw=raw))
        elif healthy:
            info.append(ALL_TEXT if mode == "all" else HAIKU_TEXT)
    if warnings:
        lines = [f"WARNING: {w}" for w in warnings]
        emit("\n".join(lines + info), "\n".join(lines))
    elif info:
        emit("\n".join(info))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the hook must never block a session
        sys.stderr.write(f"local-mode hook error: {type(exc).__name__}\n")
    sys.exit(0)
