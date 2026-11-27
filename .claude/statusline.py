"""Claude Code status line: one line of model and effort, the context bar, prompt-cache
state, cost and wall time, e.g.

  [Opus 5.5 · medium] · ctx [###############-----] 152k/200k · cache warm (1h) 59m left · $8.55 · 1h00m
"""
import json
import os
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
CONFIG_PATH = CLAUDE_DIR / "statusline.json"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

DEFAULTS = {
    "bar_width": 20,
    # Tokens in context at which `ctx` turns amber, then red. Cost per call rises
    # with context (observed 2026-09-13 and 09-16), so these are absolute, not a
    # share of the window; CTX_AMBER_PCT and CTX_RED_PCT below still warn a small
    # window that fills first.
    "ctx_amber_tokens": 200_000,
    "ctx_red_tokens": 300_000,
    # Base input $/MTok, matched as a substring of the model id, the longest
    # matching key winning, so a model priced apart from its family (opus-5-5)
    # beats the family key. An id no key matches falls back to a token count; a
    # new model in a listed family takes the family rate until it is added here.
    # opus-5-5 is the claude-api skill's table (cached 2026-06-24), and matched
    # the client's own api_request cost_usd to the cent on 2026-09-22.
    "cache_pricing": {"opus": 5.0, "opus-5-5": 4.0, "sonnet": 2.0, "haiku": 1.0, "fable": 10.0},
    "color": True,
}

# ANSI SGR colours.
RESET, DIM = "\033[0m", "\033[2m"
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(user, dict):
        # Valid JSON that is not an object (`[1, 2, 3]`) has nothing to merge;
        # treat it like a malformed file rather than crash the render on .items().
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def read_json(path, tries=3, wait_s=0.05):
    """The JSON in the file at `path`, or None. A read refused with
    PermissionError is tried again, `tries` in all: on Windows a reader meets
    it while Claude Code renames a new installed_plugins.json over the old,
    once in 231 reads during an update (MEASURED 2026-10-17)."""
    for attempt in range(tries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt + 1 < tries:
                time.sleep(wait_s)
        except (OSError, ValueError):
            return None
    return None


# The auto-compact window, when one is set: the window /context reads a
# session's tokens against, where the payload keeps the model's own. Under
# CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000, 2.1.316's /context read `51.4k/300k
# tokens (17%)` while its payload gave 51,398 tokens of a 1,000,000
# `context_window_size` at 5 per cent, with auto-compact on and with it off
# (MEASURED 2026-11-02). The rule is the client's `autocompact_context`, read
# from the 2.1.287 binary: the variable in digits, held to COMPACT_WINDOW_RANGE,
# 0 falling through; else the first settings file holding an integer
# `autoCompactWindow` in that range -- the managed file with its drop-ins
# merged after it, then the start directory's local and project files, then
# the user's. The client copies each settings file's `env` block into the
# environment this process inherits. Not copied: the reporter's reading of a
# linked worktree's local settings from its main checkout.
COMPACT_WINDOW_ENV, COMPACT_WINDOW_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "autoCompactWindow"
COMPACT_WINDOW_RANGE = (100_000, 1_000_000)
MANAGED_SETTINGS = Path({"darwin": "/Library/Application Support/ClaudeCode",
                         "win32": "C:\\Program Files\\ClaudeCode"}.get(sys.platform, "/etc/claude-code"),
                        "managed-settings.json")


def compact_window(project_dir, env=None, managed=None):
    """The auto-compact window in tokens as the client resolves it, or None
    when none is set (see COMPACT_WINDOW_ENV). `project_dir` is the session's
    start directory; `managed` the managed settings file. Never raises."""
    env = os.environ if env is None else env
    low, high = COMPACT_WINDOW_RANGE
    raw = (env.get(COMPACT_WINDOW_ENV) or "").strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 9 and int(raw):
        return max(low, min(int(raw), high))
    managed = Path(managed or MANAGED_SETTINGS)
    # glob yields nothing for a directory that is missing or cannot be listed.
    drop_ins = sorted(p for p in (managed.parent / "managed-settings.d").glob("*.json")
                      if not p.name.startswith("."))
    # The policy layer is one merge: the last of its files to set the key wins.
    policy = [v for v in (read_json(p) for p in (managed, *drop_ins))
              if isinstance(v, dict) and COMPACT_WINDOW_KEY in v]
    layers = [policy[-1]] if policy else []
    if project_dir:
        layers += [read_json(Path(project_dir) / ".claude" / name)
                   for name in ("settings.local.json", "settings.json")]
    layers.append(read_json(SETTINGS_PATH))
    for data in layers:
        value = data.get(COMPACT_WINDOW_KEY) if isinstance(data, dict) else None
        if type(value) is int and low <= value <= high:
            return value
    return None


# ----------------------------------------------------------------------------- render

# Percent of the window used at which `ctx` turns amber, then red, whatever the
# token count: a 200k window compacts before it reaches the token tiers.
CTX_AMBER_PCT, CTX_RED_PCT = 70, 90


def ctx_color(tokens, used, cfg):
    """The `ctx` colour: the worse of two tiers, by tokens in context against
    `ctx_amber_tokens` and `ctx_red_tokens` when the count is known, and by
    percent of the window used against CTX_AMBER_PCT and CTX_RED_PCT. A
    threshold in statusline.json that is not a number reads as the shipped
    default, so a typo there never costs the render."""
    tier = 0 if used < CTX_AMBER_PCT else (1 if used < CTX_RED_PCT else 2)
    if tokens is not None:

        def limit(key):
            v = cfg.get(key)
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else DEFAULTS[key]
        tier = max(tier, 0 if tokens < limit("ctx_amber_tokens") else (1 if tokens < limit("ctx_red_tokens") else 2))
    return (GREEN, YELLOW, RED)[tier]


def fmt_tokens(n):
    if n is None:
        return "?"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M" if n % 1_000_000 == 0 else f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def fmt_duration(ms):
    s = int((ms or 0) / 1000)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def fmt_age(seconds):
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s >= 172_800:
        return f"{s // 86400}d"
    return f"{s}s" if s < 90 else (f"{s // 60}m" if s < 5400 else f"{s // 3600}h")


def miss_cause_text(why):
    """`last_miss_cause` is a bare string on Claude Code 2.1.267 and an object with
    a `causes` list on 2.1.270+. Render whichever is there, and render nothing at
    all -- never a Python repr -- for a shape neither of those anticipated."""
    if isinstance(why, dict):
        why = why.get("causes") or why.get("cause")
    if isinstance(why, (list, tuple)):
        why = ", ".join(str(c) for c in why if c)
    if not isinstance(why, str):
        return ""
    return why.replace("_", " ").strip()


def cache_rebuild_usd(model_id, tokens, ttl, rates):
    """What re-writing a cold prefix costs. Cache writes bill 1.25x base input on
    the 5m TTL and 2x on 1h. None when the model is not in the rate table."""
    if not tokens or not model_id:
        return None
    mid = model_id.lower()
    keys = [k for k in rates if k in mid]
    rate = rates[max(keys, key=len)] if keys else None
    if rate is None:
        return None
    return tokens / 1e6 * rate * (2.0 if ttl == "1h" else 1.25)


def clean(text):
    """Collapse control whitespace so a segment can never break the single line."""
    return " ".join(str(text).split())


def render(d, cfg, now=None, compact=None):
    """Pure: the payload in, the one status line out. Colours are disabled by
    cfg['color']. `compact` is compact_window's auto-compact window, or None."""
    now = now or time.time()
    c = (lambda code, s: f"{code}{s}{RESET}" if code else s) if cfg.get("color", True) else (lambda code, s: s)
    sep = c(DIM, " · ")
    top = []
    m = d.get("model") or {}
    model = clean(m.get("display_name") or m.get("id") or "?")
    effort = (d.get("effort") or {}).get("level")
    top.append(c(CYAN, f"[{model}{' · ' + effort if effort else ''}]"))

    cw = d.get("context_window") or {}
    used = cw.get("used_percentage")
    if used is not None:
        tokens, size = cw.get("total_input_tokens"), cw.get("context_window_size")
        # The tokens the colour reads: the payload's own count, else the percent
        # of the window it gives; with neither, ctx_color reads the percent alone.
        basis = tokens if tokens is not None else (used * size / 100 if size else None)
        # An auto-compact window below the model's is the one /context reads
        # the tokens against: the hashes, the percent tier and the label take it.
        if compact and basis is not None and not (size and size <= compact):
            used, size = basis * 100 / compact, compact
        used = int(used)
        w = cfg["bar_width"]
        # Hashes fill against the model's window; the colour says what the next
        # call costs, which a 1M window's percent hides until far past the point
        # where context has become the bill.
        filled = min(w, max(0, int(used * w / 100)))
        color = ctx_color(basis, used, cfg)
        ctx = f"{c(color, 'ctx')} [{c(color, '#' * filled)}{c(DIM, '-' * (w - filled))}]"
        if tokens is not None and size:
            ctx += c(DIM, f" {fmt_tokens(tokens)}/{fmt_tokens(size)}")
        top.append(ctx)
    else:
        top.append(c(DIM, "ctx n/a"))

    pc = d.get("prompt_cache")
    if pc:
        ttl = pc.get("ttl") or ""
        toks = pc.get("recache_tokens_if_cold")
        usd = cache_rebuild_usd((d.get("model") or {}).get("id"), toks, ttl, cfg["cache_pricing"])
        price = f"${usd:.2f}" if usd is not None else (fmt_tokens(toks) if toks else "")
        expires = pc.get("expires_at")
        runway = (expires - now) if expires else None
        # `warm` describes the request that built this payload, but the status line
        # re-renders long after it. An idle session outlives its own entry: the flag
        # still reads warm while the clock has already run past the TTL.
        lapsed = bool(pc.get("warm")) and runway is not None and runway <= 0
        if pc.get("warm") and not lapsed:
            state = "cache warm"
            if ttl:
                state += f" ({ttl})"
            if runway is not None:
                state += f" {fmt_age(runway)} left"
            state = c(GREEN, state)
            # Priced only near expiry, the one point where walking away from the
            # session actually costs the rebuild.
            if runway is not None and runway < 300 and price:
                state += " " + c(YELLOW, f"{price} if it lapses")
            top.append(state)
        elif lapsed or pc.get("caching_observed"):
            # `lapsed` is ours, read off the clock; `cold` is Claude Code's, and
            # last_miss_cause names the tier that broke.
            if lapsed:
                head = "cache lapsed"
            else:
                why = miss_cause_text(pc.get("last_miss_cause"))
                head = "cache cold" + (f" ({why})" if why else "")
            top.append(f"{c(DIM, head)} {c(YELLOW, price + ' to rebuild')}" if price
                       else c(DIM, head))

    cost = d.get("cost") or {}
    if cost.get("total_cost_usd") is not None:
        top.append(c(YELLOW, f"${cost['total_cost_usd']:.2f}"))
    if cost.get("total_duration_ms"):
        top.append(fmt_duration(cost["total_duration_ms"]))

    return sep.join(x for x in top if x)


def main(argv):
    # utf-8-sig on stdin: Windows PowerShell prepends a BOM to what it pipes
    # to a native command. Claude Code sends none, and utf-8-sig reads input without one as utf-8.
    for stream, encoding in ((sys.stdin, "utf-8-sig"), (sys.stdout, "utf-8")):
        try:
            stream.reconfigure(encoding=encoding, errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        d = json.load(sys.stdin)
    except ValueError:
        print("[status line: no input]")
        return 0
    if not isinstance(d, dict):  # valid JSON that is not an object has nothing to render
        d = {}

    cfg = load_config()
    try:
        compact = compact_window((d.get("workspace") or {}).get("project_dir") or d.get("cwd"))
        try:
            now = float(os.environ.get("STATUSLINE_NOW") or time.time())  # env override: tests
        except ValueError:
            now = time.time()
        print(render(d, cfg, now=now, compact=compact))
    except Exception as e:  # a broken status line must never take the session with it
        m = d.get("model") or {}
        model = clean(m.get("display_name") or m.get("id") or "?")
        cost = (d.get("cost") or {}).get("total_cost_usd")
        base = f"[{model}] ${cost:.2f}" if cost is not None else f"[{model}]"
        print(f"{base} {DIM}status line error: {type(e).__name__}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))