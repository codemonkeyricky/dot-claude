# local-llm

Runs this harness (Claude Code with the dot-claude config) on a local model served by [Ollama](https://ollama.com), either for the whole session, for Haiku executors only, or for every request through a small router. Plain `claude` is not affected and stays on the Anthropic subscription.

## Requirements

- Ollama 0.14 or newer, serving the Anthropic-compatible `/v1/messages` API that Claude Code talks to.
- A GPU with enough VRAM for the default model (`qwen3.8:27b`) at its 160k context with a q8 KV cache. A smaller GPU can run a smaller model through `LOCAL_LLM_MODEL` with a smaller `LOCAL_LLM_CTX`.
- `claude` (Claude Code) on the PATH, plus `bash`, `curl`, `setsid` and `python3`.

## Files

- `config.sh`: shared defaults (model, context, host, auto-compact window, router port, state dir) and `strip_dispatch_header`. Each value can be overridden from the environment.
- `ollama-up.sh`: starts `ollama serve` detached with the local settings (flash attention, q8_0 KV cache, one parallel slot, one loaded model, 30 min keep-alive). Does nothing if a server already answers.
- `ollama-down.sh`: stops the server started by `ollama-up.sh`.
- `setup.sh`: idempotent one-time setup. Checks ollama, starts the server, verifies the model is pulled, warms it, and links `claude-local` and `claude-anthropic` into `~/.local/bin`.
- `claude-local`: the wrapper for a fully local session. Starts the server if needed, points Claude Code at it, writes the generated settings file and runs `claude` with that settings file. Sets `CLAUDE_LOCAL=1`.
- `claude-anthropic`: runs plain `claude` on the subscription. It unsets the router and local variables and runs the daemon cleanup for anthropic mode.
- `claude-kill-bg.sh`: lists and stops the Claude Code daemon tree (see below).
- `hybrid-proxy.py`: the Claude router, a stdlib loopback proxy (see Router modes).
- `hybrid-proxy.sh`: starts, stops and inspects the router. Uses the systemd user unit when one is installed.
- `hybrid-proxy.service`: optional systemd user unit template for the router.
- `claude-route`: starts Claude Code through the router, per `CLAUDE_ROUTER` and `LOCAL_DISPATCH`.
- `claude-hybrid`: thin wrapper, the same as `LOCAL_DISPATCH=1 claude-route`.
- `claude-local-all`: thin wrapper, the same as `LOCAL_DISPATCH=all claude-route`.

## Install

```
tools/local-llm/setup.sh
```

Run it from the repo checkout. It never pulls a model. If `qwen3-coder:30b` is named by an older config but only `qwen3-coder:latest` is pulled, setup creates the 30b tag from it with `ollama cp`. Otherwise it prints the `ollama pull` command and exits 1. Re-running setup is safe.

Make sure `~/.local/bin` is on your PATH. Setup prints the line to add if it is not.

## Usage

```
claude-local                  # fully local session, interactive
claude-local -p "..."        # one-shot prompt
claude-anthropic              # plain Claude Code on the subscription
claude-route                  # plain claude unless CLAUDE_ROUTER or LOCAL_DISPATCH is set
claude-hybrid                 # LOCAL_DISPATCH=1 session: Haiku runs locally, everything else on Anthropic
claude-local-all              # LOCAL_DISPATCH=all session: every request runs locally, no Anthropic fallback
```

Switch model for one run:

```
LOCAL_LLM_MODEL=qwen3.8:27b claude-local
```

## Router modes

The router (`hybrid-proxy.py`) listens on `127.0.0.1:$HYBRID_PORT` and routes each request:

- A request carrying `x-local-dispatch: all` (LOCAL_DISPATCH=all, set by `claude-local-all`) whose JSON `model` is a string goes to Ollama as `LOCAL_LLM_MODEL`, whatever the model name. This covers the main session and every subagent. There is no fallback: if Ollama fails, the client gets 502 and nothing reaches Anthropic. Checked first.
- A request carrying `x-local-dispatch: subagents` or `x-local-dispatch: 1` (LOCAL_DISPATCH=subagents or 1, set by `claude-hybrid`) whose JSON `model` is a Haiku model (`claude-haiku*`, `claude-3-5-haiku*`, `claude-3-haiku*`) goes to Ollama as `LOCAL_LLM_MODEL`. Any other header value is off.
- A request whose `model` names the local model goes to Ollama. This is the older model-name route and has no fallback.
- Everything else goes to `HYBRID_ANTHROPIC_URL` (default `https://api.anthropic.com`) with the caller's body and headers unchanged, so the Max-plan OAuth login passes through. Opus and Sonnet calls, reviewers and Opus escalations stay on Anthropic (unless `LOCAL_DISPATCH=all`).
- If a subagents-routed request (`x-local-dispatch: subagents` or `1`) fails at Ollama before any response byte reaches the client (unreachable, timeout, or HTTP 400 or more), it is resent to Anthropic with the original body and headers. The log line says `backend=anthropic-fallback` and why.
- The `x-local-dispatch` header is never forwarded. Caller credentials never reach Ollama; Authorization is replaced by a dummy bearer token there.
- The proxy logs one line per request (time, method, path, model, backend, status, duration). It never logs headers or bodies, since those carry OAuth tokens.

The variables that select a mode are set only in the launcher process. Never put them in a settings file.

| Variable | Set by | Effect |
| --- | --- | --- |
| `CLAUDE_LOCAL=1` | `claude-local` | Local mode. Hooks tell the model to work directly, without executors. Nothing goes through the router. |
| `CLAUDE_ROUTER=1` | `claude-route` | `ANTHROPIC_BASE_URL` points at the router on `HYBRID_PORT`. No dispatch header. |
| `LOCAL_DISPATCH=1` or `LOCAL_DISPATCH=subagents` | `claude-hybrid`, or by hand | Implies `CLAUDE_ROUTER=1` (`claude-route` prints one note when it does this). `1` is the alias of `subagents`. Adds `x-local-dispatch: subagents` to `ANTHROPIC_CUSTOM_HEADERS`, on its own line after any other headers. Haiku requests run locally. |
| `LOCAL_DISPATCH=all` | `claude-local-all`, or by hand | Implies `CLAUDE_ROUTER=1`. Adds `x-local-dispatch: all`. Every model request, main session and subagents, runs locally with no Anthropic fallback. Empty or `0` is off. Any other value is off too, and `claude-route` warns. |
| `CLAUDE_ROUTE_ACTIVE=1` | `claude-route` | Guard for the shell shim, so it never re-enters `claude-route`. |

`CLAUDE_LOCAL=1` wins over the router variables in the SessionStart hook (`.claude/hooks/local-mode.py`). Otherwise the hook checks the router's `/_hybrid/health` and reports the result: a healthy router is reported as OK, and with `LOCAL_DISPATCH=1` or `subagents` the hook also says that Haiku executors run on the local model, and with `LOCAL_DISPATCH=all` that every request runs on the local model. A router that is down, an `ANTHROPIC_BASE_URL` that does not point at it, or a LOCAL_DISPATCH session whose `x-local-dispatch` header is missing or has a different value gives a WARNING. The hook is silent when `CLAUDE_LOCAL`, `CLAUDE_ROUTER` and `LOCAL_DISPATCH` are all unset or `0`.

`claude-local` and `claude-anthropic` unset `CLAUDE_ROUTER`, `LOCAL_DISPATCH` and `CLAUDE_ROUTE_ACTIVE`, and remove the `x-local-dispatch` line from `ANTHROPIC_CUSTOM_HEADERS`, so a shell that was left in router mode cannot leak it into them.

`CLAUDE_LOCAL=1` (`claude-local`) and `LOCAL_DISPATCH=all` (`claude-local-all`) both run on the local model, but they differ in the harness. `CLAUDE_LOCAL=1` is a direct session with no router: the harness rules (executors, tasks, reviews) are off and the model works directly. `LOCAL_DISPATCH=all` goes through the router and keeps the harness rules on: the main session and every subagent run on the local model, and nothing falls back to Anthropic, so an Ollama failure surfaces as a 502. Expect slower, weaker output in both.

## Shell shim (optional)

`claude-route` runs real `claude` from `type -P claude`, so a shell function named `claude` can call it without recursion. This bash snippet in `~/.bashrc` makes plain `claude` go through the router when `CLAUDE_ROUTER` is 1 or `LOCAL_DISPATCH` is set to anything other than `0`:

```
claude() { if [[ ( "${CLAUDE_ROUTER:-}" == 1 || ( -n "${LOCAL_DISPATCH:-}" && "${LOCAL_DISPATCH:-}" != 0 ) ) && -z "${CLAUDE_ROUTE_ACTIVE:-}" ]]; then command claude-route "$@"; else command claude "$@"; fi; }
```

Without the shim, call `claude-route`, `claude-hybrid` or `claude-local-all` directly.

## Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOCAL_LLM_MODEL` | `qwen3.8:27b` | Model tag served by Ollama. Must already be pulled. The router and health check report this name. |
| `LOCAL_LLM_CTX` | `163840` | Ollama context length. |
| `LOCAL_LLM_COMPACT_WINDOW` | `130000` | Context size at which auto-compact triggers. |
| `OLLAMA_HOST` | `127.0.0.1:11434` | Address of the Ollama server. |
| `HYBRID_PORT` | `4000` | Loopback port for the router (`127.0.0.1` only). Must be 1 to 65535. |
| `HYBRID_ANTHROPIC_URL` | `https://api.anthropic.com` | Upstream for every request that is not routed locally. |
| `HYBRID_UNIT` | `hybrid-proxy` | systemd user unit name for the router. Set it empty to ignore systemd and run the proxy detached. |
| `LOCAL_LLM_STATE_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/claude-local` | Where runtime state is written. |
| `CLAUDE_LOCAL_KILL` | `ask` | `yes` stops stale daemon-tree processes without asking, `no` skips the cleanup, unset asks (refuses without a terminal). |
| `CLAUDE_LOCAL_KEEP` | unset | Set it in a process's environment or arguments to keep that process and its subtree out of the cleanup. |

## Router start and stop

```
tools/local-llm/hybrid-proxy.sh up        # start unless already answering with the expected local model
tools/local-llm/hybrid-proxy.sh status    # running or not, plus health JSON
tools/local-llm/hybrid-proxy.sh down      # stop the process named in the pid file
```

`up` waits for `/_hybrid/health` to report `ok` and the expected `local_model`. If another process answers on the port with a different model, `up` fails and tells you to stop it or match `LOCAL_LLM_MODEL`. `down` signals only a process whose argv is exactly `python3 -I <this checkout>/hybrid-proxy.py`.

With the systemd user unit installed, `up`, `down` and `status` defer to it. To install it, copy `tools/local-llm/hybrid-proxy.service` to `~/.config/systemd/user/`, replace the `ExecStart` path with this checkout's absolute path, then run `systemctl --user daemon-reload && systemctl --user enable --now hybrid-proxy`. The unit uses `Restart=always`.

## State

Runtime state goes to `LOCAL_LLM_STATE_DIR`, never into the script directory: `ollama.log`, `ollama.pid`, `hybrid-proxy.log`, `hybrid-proxy.pid`, `.mode` (an informational marker written by the wrappers), and the generated `claude-local.settings.json`.

## Sticky daemon and background jobs

Claude Code background jobs keep the settings they started with, and those settings come from the Claude Code daemon (`claude daemon run`). So `claude-local`, `claude-anthropic`, `claude-route` and `claude-hybrid` check the daemon before they start. `claude-hybrid` and `claude-route` do not run this check themselves; the wrappers above do. If the daemon tree runs in the other mode, they report how many processes are stale and apply `CLAUDE_LOCAL_KILL`.

- A daemon's mode is read from `ANTHROPIC_BASE_URL` in `/proc/<pid>/environ`: `hybrid` when it points at the router (`127.0.0.1:$HYBRID_PORT`, or `localhost` on that port, with no path), `local` when it points at the Ollama host, otherwise `anthropic`. An unreadable environment counts as stale.
- The target set is the daemon plus all of its descendants. Interactive sessions, the chrome native host and other processes outside the daemon tree are not touched.
- The cleanup script, its ancestors and anything marked with `CLAUDE_LOCAL_KEEP` are always excluded.

**Warning: killing the daemon tree ends any running background Claude jobs**, including the daemon and its sessions. Keep a process with `CLAUDE_LOCAL_KEEP=1` in its environment, or stop it yourself first.

Check what would be stopped without stopping anything:

```
tools/local-llm/claude-kill-bg.sh --dry-run
tools/local-llm/claude-kill-bg.sh --dry-run --stale-for local
tools/local-llm/claude-kill-bg.sh --dry-run --all
```

The script's options are `--dry-run` (list only), `--yes` (no prompt), `--count` (print the number), `--all` (every claude process, not just the daemon tree) and `--stale-for local|hybrid|anthropic`.

## Caveats

- Subagents and executors share one GPU. With `OLLAMA_NUM_PARALLEL=1` they queue, so parallel dispatch is slower than on the hosted API.
- A local model is weaker than the hosted Claude models on long agentic tasks. Expect more mistakes and shorter reliable runs.
- `claude-local` only: the generated settings file overrides the subagent model, auto-compact window and model for those runs. The `haiku`, `sonnet` and `opus` aliases all map to the local model there. Plain `claude` and `~/.claude/settings.json` are not edited.
- Router mode does not remap aliases. Routing is by header and model name, so Opus and Sonnet calls stay on Anthropic (except with `LOCAL_DISPATCH=all`).
- The router's Ollama requests are sent with a dummy bearer token and the caller's API key is dropped. The router does not log credentials.
- Earlier drafts of `claude-hybrid` also set a subagent model, ran Ollama setup and cleaned stale daemon processes. The current `claude-hybrid` does none of these: it validates `HYBRID_PORT` and runs `claude-route`. Run `setup.sh` and `hybrid-proxy.sh up` yourself, and run `claude-kill-bg.sh --stale-for hybrid` when needed.
