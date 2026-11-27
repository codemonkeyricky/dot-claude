# dot-claude

An orchestration harness for Claude Code. The main session plans a task DAG and dispatches the implementation work to cheap Haiku "executor" subagents, while hooks enforce that split. The harness lives under `.claude/` and holds only the shareable config: workflow rules, hooks, agent definitions, generator scripts and the hook registrations in `.claude/settings.harness.json`. The repo also carries a `tools/` directory with standalone tools (see Tools below).

> **Note:** because the harness sits in `.claude/`, opening Claude Code inside this repo still loads `.claude/CLAUDE.md` and `.claude/agents/` as project config. A project-level agent in `.claude/agents/` overrides a same-named user agent, so the repo definitions can differ from the installed ones until `./deploy.sh` is run; `.claude/CLAUDE.md` is loaded as project instructions on top of the user's global CLAUDE.md. Hooks come only from the installed `~/.claude/settings.json`, never from this repo: the repo file is named `.claude/settings.harness.json` so Claude Code does not read it as project settings, and it takes effect only after `./deploy.sh` merges it into `~/.claude/settings.json`.

## How it works

- **The main session is read-only.** It may not Write, Edit or run arbitrary Bash. It plans, reads, and dispatches.
- **Work is tracked as tasks.** Each dispatchable unit is a task (TaskCreate/TaskUpdate). Dependencies are wired as a DAG; a task is ready when it is pending, unowned, and all its blockers are completed.
- **Executors are dispatched by readiness.** A task is set `in_progress` with an owner equal to an executor name, then one Agent call per ready task runs it. Executors run on Haiku at medium effort. A failed executor is escalated to `diagnoser-1`, a read-only Opus root-cause diagnoser that writes a fix spec for a Haiku retry; Sonnet executors return only behind the `model_gate.sonnet_fallback` kill switch, and Opus is never used by executors. Reviewers (`pr-reviewer-N`) and the diagnoser run on Opus, read-only. The main session runs on Opus and is read-only.
- **Reports are verified, not trusted.** The caller checks each report against the brief's acceptance checks before marking a task completed.
- **Hooks enforce the rules.** Agent dispatch is refused without a matching in-progress, unblocked task, and every completion is logged.

`.claude/CLAUDE.md` holds the full rules; this file only summarizes them.

## Components

| Path | What it is |
| --- | --- |
| `.claude/CLAUDE.md` | Workflow rules for the main session: task DAG, executor dispatch, briefs, verification. |
| `.claude/agents/` | Ten Haiku executors (`qorvax-tel` … `plimm-oraxi`) generated from `.claude/scripts/executor-template.md` by `.claude/scripts/gen_executors.py`, plus `pr-reviewer-1` … `pr-reviewer-3`, Opus PR reviewers defined in `.claude/scripts/opus-reviewer.md`, plus `diagnoser-1`, the read-only Opus root-cause diagnoser hand-written in `.claude/agents/diagnoser-1.md`. |
| `.claude/hooks/main-readonly.py` | PreToolUse. Blocks main-session Write/Edit/Bash except plans, memory and job tmp dirs; allows a quote-aware read-only shell allowlist (chains with `&&`/`||`/`;`, `2>/dev/null`, `2>&1`). Deny messages state the reason. Kill switch `CLAUDE_MAIN_WRITE=1`. Reads `readonly_scripts` and `exempt_env` from the local config (see below). |
| `.claude/hooks/task-gate.py` | Pre/PostToolUse. An Agent dispatch requires an in-progress, unblocked task owned by that executor; rejects cycles. Opus is denied for executors (escalation is a `diagnoser-1` diagnosis, then a Haiku retry; Sonnet only with `model_gate.sonnet_fallback`), and subagents may not dispatch agents. Diagnoser dispatch needs a diagnose task with `metadata.diagnoses` (caps from `diagnose_gate`); SendMessage to a diagnoser is denied. Kill switch `CLAUDE_REQUIRE_TASK=0` disables only the DAG ownership gate, round cap and task-based escalation proof; Haiku executors still pass, while Sonnet and Opus executors and subagent dispatch stay denied. Model gate: exempt types (Explore, web-fetch, claude-code-guide) and `model_gate.allow_types` entries run on Haiku only (allow_types need an explicit `model: haiku`); `CLAUDE_MAIN_WRITE=1` does not lift the model gate; executors may only use haiku (sonnet only with `model_gate.sonnet_fallback`). Sessions matched by `exempt_env` in the local config are exempt too. Auto-claim: an Agent call whose **description** (never the prompt) contains `· #<n>` (middle dot, optional spaces) or `task #<n>` (case-insensitive) claims that ready task for its executor; any other `#<n>` names nothing. A claim whose call never reaches PostToolUse can be retaken only by the same executor (its re-dispatch); any other executor must reset it with TaskUpdate status pending, owner `""`. |
| `.claude/hooks/subagent-worktree.py` | PreToolUse, Write/Edit/MultiEdit/NotebookEdit/Bash/PowerShell, subagents only. A subagent may change files only inside a linked git worktree or a scratch dir (`$CLAUDE_JOB_DIR/tmp`, the system temp dir). Exempt agent types: Explore, Plan, claude-code-guide, statusline-setup, opus-reviewer, pr-reviewer-*, diagnoser-*. Kill switch `CLAUDE_SUBAGENT_ANYWHERE=1`. Known gap: the Monitor tool is not gated. |
| `.claude/hooks/ready-gate.py` | PreToolUse, Write/Edit/MultiEdit/NotebookEdit/Bash/PowerShell, main and subagents. Denies `gh pr ready` unless the latest review verdict for the PR's head SHA is APPROVE, denies `gh pr create` without `--draft`, stops a PR after 3 CHANGES rounds, and protects `pr-reviews.jsonl`. Kill switch `CLAUDE_SKIP_READY_REVIEW=1`. Ready-gate does not follow `exempt_env`, so it stays on in `CLAUDE_LOCAL` sessions. Its `gh pr ready` and records-file matching is best effort: wrapped forms such as `env -i gh …`, `bash -c '…'`, xargs or variable-expanded commands are not detected. |
| `.claude/hooks/review-record.py` | SubagentStop for `opus-reviewer` and `pr-reviewer-*`. Records the final REVIEW-VERDICT line (APPROVE or CHANGES) in `hooks/state/pr-reviews.jsonl`. A reviewer that ends without a verdict line is blocked once; ABSTAIN records nothing. |
| `.claude/hooks/diagnose-record.py` | SubagentStop for `diagnoser-*`. Checks the diagnosis output contract: headings `## Root cause`, `## Fix spec`, `## Checks`; last line `DIAGNOSIS-VERDICT: SPEC` or `DIAGNOSIS-VERDICT: NEEDS-USER`; at most 8000 characters; no code block over 20 lines. |
| `.claude/hooks/plan-gate.py` | PreToolUse ExitPlanMode, Agent/Task and TaskUpdate; PostToolUse ExitPlanMode. Validates the plan's ```tasks block and its DAG (no cycles, at most `plan_max_writes` writes per dispatchable task, a `serial_reason` for a pure chain of 4 or more), then denies dispatch and edges that do not match the saved plan. Retires after `plan_ttl_sec`. Kill switch `CLAUDE_PLAN_GATE=0`. |
| `.claude/hooks/dispatch-stop.py` | Stop. Blocks ending the turn, once per stop, while executor pool slots are free and ready tasks wait. Does nothing for subagents or in `CLAUDE_LOCAL`/`LOCAL_DISPATCH` modes. Kill switch `CLAUDE_DISPATCH_STOP=0`. |
| `.claude/hooks/harness.local.example.json` | Example local config listing every key the hooks read (see Local config). Copy it to `~/.claude/hooks/harness.local.json` and edit. |
| `.claude/hooks/agent-names.py` | Names each subagent: executors get their fixed name, others a round-robin name. In local mode it also pins the effort when none is given (reviewer `high`, executor `medium`) via `updatedInput`; an explicit effort is kept, and off-local it is a no-op. Kill switches `CLAUDE_AGENT_NAMES=0` (naming) and `CLAUDE_LOCAL_EFFORT=0` (effort). |
| `.claude/hooks/agent-budget.py` | PreToolUse, all tools. Caps subagent tool calls: soft 150 (one wrap-up denial), hard 200; reviewers 80/110; diagnosers 40/60. Config key `agent_budget`; env `AGENT_BUDGET_SOFT`/`AGENT_BUDGET_HARD`. Kill switch `CLAUDE_AGENT_BUDGET=0`. |
| `.claude/hooks/handoff-nudge.py` | SessionStart (matcher `compact`). Counts compactions per session. From the 3rd, nudges toward a handoff in a fresh session; if the previous compaction was under 20 min ago (from the 2nd), flags thrash and says to move big reads to `Explore`/executors. `HANDOFF_NUDGE_AFTER` sets the count threshold, `HANDOFF_NUDGE_RAPID_MIN` the rapid window. Kill switch `CLAUDE_HANDOFF_NUDGE=0`. |
| `.claude/statusline.py` | Claude Code status line: one line of model and effort, context bar, prompt-cache state, cost and wall time. Registered as `statusLine` in the settings. |
| `.claude/hooks/result-capture.py` | Stop. Appends each `result:` line of the final message to `hooks/state/completed-work.jsonl` (under the installed `~/.claude`). |
| `.claude/hooks/session-repo.py` | Titles each session `[repo] branch` followed by its title, and buckets sessions by repo. |
| `.claude/hooks/sessions-by-repo.py` | Report CLI over those per-repo session buckets. |
| `.claude/settings.harness.json` | Hook registrations, `CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-5-5`, model effort settings (Haiku `medium`, Sonnet `high`, Opus `medium`, plus `qwen3.8:27b` `high` so the local-model main session runs at high effort), and `autoCompactWindow` 200000. Merged into the installed `settings.json` by `deploy.sh`. |
| `.claude/scripts/` | Executor and reviewer templates, plus `gen_executors.py` (`--check` reports stale agent files). |
| `deploy.sh` | Installer (repo root). Installs the contents of `.claude/` into `~/.claude`, described below. |
| `tools/session-plot/` | Session plot tool: main and subagent context/fork timeline from a session jsonl. |

The subagent-worktree, ready-gate, review-record, plan-gate and dispatch-stop hooks, the executor pool cap and background rule in task-gate, and the `main_allow_commands` and job-tmp rules in main-readonly were ported from the gist fork `ba2a34caab4f325ac01464878f853a9a`, with local adaptations (no Monitor gating, the local git allowlist unchanged). `.claude/CLAUDE.md` describes what each one does.

## Tools

- **Local model (`tools/local-llm/`)**: runs this harness on a local Ollama model when the Claude plan quota is short. `tools/local-llm/setup.sh` installs the `claude-local` wrapper; see [tools/local-llm/README.md](tools/local-llm/README.md).
- **`tools/session-plot/`**: plots the main-session and subagent context and fork timeline from a Claude Code session jsonl. Its own README has the usage (`npm run check -- <sessionId>` for the checks). It is a separate Node project with its own `package.json`, not part of the installed harness.

## Local config

Some settings are specific to one machine and must not be committed. The hooks read them from a single local file, `harness.local.json`, in the hooks directory of the installed config (`~/.claude/hooks/harness.local.json`). The copy under this repo's `.claude/hooks/` is gitignored, and `deploy.sh` never copies it. Its keys are:

- `pool_size`: executor pool cap, default 8 (env `TASK_GATE_POOL_SIZE` overrides).
- `running_stale_sec`: seconds after which a running executor counts as stale, default 3600 (env `TASK_GATE_RUNNING_STALE_SEC` overrides).
- `executors`: the executor names the pool counts, default the ten pool names.
- `require_background`: whether task-gate denies a foreground executor dispatch while other tasks are ready, default true (env `TASK_GATE_REQUIRE_BACKGROUND=0` or `1` overrides; always off when `LOCAL_DISPATCH` is set).
- `plan_max_writes`: most write entries one dispatchable plan task may declare, default 8.
- `plan_ttl_sec`: seconds after which a plan retires, default 86400.
- `agent_budget`: subagent tool-call soft and hard caps per agent type (default 150/200; reviewers 80/110; diagnosers 40/60).
- `model_gate`: `allow_types` (extra agent types, which need `model: haiku`), `sonnet_fallback` (default `false`; `true` re-enables the old Sonnet escalation), `escalate_to` (default `["sonnet"]`) and `max_rounds` (default 3) for task-gate's model gate.
- `diagnose_gate`: `max_per_task` (default 2) and `max_per_session` (default 4) diagnoses by `diagnoser-1`.
- `main_allow_commands`: glob patterns of single simple commands the main session may run.
- `subagent_allow_commands`: fnmatch patterns of commands subagent-worktree lets run in any directory. File operations are never allowlisted.
- `readonly_scripts`: extra read-only scripts the main session may run through `main-readonly.py`, as `[{"pattern": "<regex>", "flags": ["--json"]}]`. A script runs only when the path is absolute and has no `..` component as typed. A single entry must fully match both the normalized path and its resolved real path (symlinks followed), and that same entry's `flags` must permit every flag given. Two different entries, one matching the typed path and one matching the real path, are not enough. Relative paths and paths containing `..` are refused, even when they would otherwise match.
- `exempt_env`: `{"VAR": ["glob", ...]}`. Honored by `main-readonly.py`, `task-gate.py`, `dispatch-stop.py`, `subagent-worktree.py` and `plan-gate.py`; `ready-gate.py` does not follow it. A session is exempt when some `VAR` is non-empty and matches one glob (case-sensitive `fnmatch`, so `"bot-*"` matches `bot-worker` but not `Bot-worker`). Caution: a broad glob such as `"*"` exempts every session in which that variable is set, which switches off the whole main-session guard for them.

Example to exempt the sessions that `claude-local` starts (it sets `CLAUDE_LOCAL=1`): `{"exempt_env": {"CLAUDE_LOCAL": ["1"]}}`. Add it only on a machine where those sessions should skip the guard.

Set `CLAUDE_HARNESS_CONFIG` to read a different file. A missing, unreadable or malformed file, or a malformed entry, means no extra scripts and no exemptions.

## Install

Preview what differs from the installed config:

```sh
./deploy.sh --diff
```

List what would change, changing nothing:

```sh
./deploy.sh --dry-run
```

Install:

```sh
./deploy.sh
```

`deploy.sh` copies the tracked files under `.claude/` into `~/.claude` (or `CLAUDE_DIR`, if set; a relative `CLAUDE_DIR` is taken from the directory you run the script in), so `.claude/hooks/x.py` installs to `~/.claude/hooks/x.py`. Nothing outside `.claude/` is installed, and it never deletes anything there. `.claude/settings.harness.json` is never copied as a file: it is deep-merged into the target's `settings.json`. Keys from this repo win, and any other settings already in the target (permissions, theme, plugins, personal model choices) are kept. Dicts merge recursively; lists are replaced wholesale, except hook lists. Hook lists are the exception: for each `hooks.<Event>` list that both sides have, the result is the repo's entries, followed by every target entry whose hook commands do not appear among the repo's entries for that event (order kept, no duplicates). So a hook registered only in the target (for example a local `deploy-trigger.py` under `PostToolUse`) survives a deploy, while a stale copy of a repo hook is replaced by the repo's version. Other lists are still replaced wholesale. Keys removed from this repo are not removed from the target; delete them there by hand. Hook lists keep target-only entries deduplicated by command, so a renamed or removed repo hook is not unregistered automatically; remove it from `~/.claude/settings.json` by hand. The merge runs before any file is copied, so an invalid `settings.json` (in the target) or `settings.harness.json` (in the repo) stops the install with an error and changes nothing.

After editing `.claude/scripts/executor-template.md` or `.claude/scripts/opus-reviewer.md`, run `python3 .claude/scripts/gen_executors.py --root .claude` in the repo to regenerate `.claude/agents/`, then `./deploy.sh`.

## Tests

```sh
python3 -m pytest -q .claude/hooks
```

Or, without pytest:

```sh
python3 -m unittest discover -s .claude/hooks -p 'test_*.py'
```
