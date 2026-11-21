# dot-claude

An orchestration harness for Claude Code. The main session plans a task DAG and dispatches the implementation work to cheap Haiku "executor" subagents, while hooks enforce that split. The harness lives under `.claude/` and holds only the shareable config: workflow rules, hooks, agent definitions, generator scripts and the hook registrations in `.claude/settings.harness.json`. The repo also carries a `tools/` directory with standalone tools (see Tools below).

> **Note:** because the harness sits in `.claude/`, opening Claude Code inside this repo still loads `.claude/CLAUDE.md` and `.claude/agents/` as project config. A project-level agent in `.claude/agents/` overrides a same-named user agent, so the repo definitions can differ from the installed ones until `./deploy.sh` is run; `.claude/CLAUDE.md` is loaded as project instructions on top of the user's global CLAUDE.md. Hooks come only from the installed `~/.claude/settings.json`, never from this repo: the repo file is named `.claude/settings.harness.json` so Claude Code does not read it as project settings, and it takes effect only after `./deploy.sh` merges it into `~/.claude/settings.json`.

## How it works

- **The main session is read-only.** It may not Write, Edit or run arbitrary Bash. It plans, reads, and dispatches.
- **Work is tracked as tasks.** Each dispatchable unit is a task (TaskCreate/TaskUpdate). Dependencies are wired as a DAG; a task is ready when it is pending, unowned, and all its blockers are completed.
- **Executors are dispatched by readiness.** A task is set `in_progress` with an owner equal to an executor name, then one Agent call per ready task runs it. Executors run on Haiku at medium effort.
- **Reports are verified, not trusted.** The caller checks each report against the brief's acceptance checks before marking a task completed.
- **Hooks enforce the rules.** Agent dispatch is refused without a matching in-progress, unblocked task, and every completion is logged.

`.claude/CLAUDE.md` holds the full rules; this file only summarizes them.

## Components

| Path | What it is |
| --- | --- |
| `.claude/CLAUDE.md` | Workflow rules for the main session: task DAG, executor dispatch, briefs, verification. |
| `.claude/agents/` | Ten Haiku executors (`qorvax-tel` … `plimm-oraxi`) generated from `.claude/scripts/executor-template.md` by `.claude/scripts/gen_executors.py`, plus `pr-reviewer-1` … `pr-reviewer-3`, Opus PR reviewers defined in `.claude/scripts/opus-reviewer.md`. |
| `.claude/hooks/main-readonly.py` | PreToolUse. Blocks main-session Write/Edit/Bash except plans, memory and job tmp dirs; allows a quote-aware read-only shell allowlist (chains with `&&`/`||`/`;`, `2>/dev/null`, `2>&1`). Deny messages state the reason. Kill switch `CLAUDE_MAIN_WRITE=1`. Reads `readonly_scripts` and `exempt_env` from the local config (see below). |
| `.claude/hooks/task-gate.py` | Pre/PostToolUse. An Agent dispatch requires an in-progress, unblocked task owned by that executor; rejects cycles. Kill switch `CLAUDE_REQUIRE_TASK=0`. Sessions matched by `exempt_env` in the local config are exempt too. Auto-claim: an Agent call whose **description** (never the prompt) contains `· #<n>` (middle dot, optional spaces) or `task #<n>` (case-insensitive) claims that ready task for its executor; any other `#<n>` names nothing. A claim whose call never reaches PostToolUse can be retaken only by the same executor (its re-dispatch); any other executor must reset it with TaskUpdate status pending, owner `""`. |
| `.claude/hooks/agent-names.py` | Names each subagent: executors get their fixed name, others a round-robin name. Kill switch `CLAUDE_AGENT_NAMES=0`. |
| `.claude/hooks/agent-budget.py` | PreToolUse, all tools. Caps subagent tool calls: soft 150 (one wrap-up denial), hard 200; reviewers 80/110. Config key `agent_budget`; env `AGENT_BUDGET_SOFT`/`AGENT_BUDGET_HARD`. Kill switch `CLAUDE_AGENT_BUDGET=0`. |
| `.claude/hooks/handoff-nudge.py` | SessionStart (matcher `compact`). From the 3rd compaction in a session, nudges toward a handoff in a fresh session. `HANDOFF_NUDGE_AFTER` sets the threshold. Kill switch `CLAUDE_HANDOFF_NUDGE=0`. |
| `.claude/statusline.py` | Claude Code status line: model, live executors with call count and idle age, task counts, last deploy (log path from `STATUSLINE_DEPLOY_LOG` or `statusline.deploy_log`; no deploy segment when neither is set). Registered as `statusLine` in the settings. |
| `.claude/hooks/result-capture.py` | Stop. Appends each `result:` line of the final message to `hooks/state/completed-work.jsonl` (under the installed `~/.claude`). |
| `.claude/hooks/session-repo.py` | Titles each session `[repo] branch` followed by its title, and buckets sessions by repo. |
| `.claude/hooks/sessions-by-repo.py` | Report CLI over those per-repo session buckets. |
| `.claude/settings.harness.json` | Hook registrations, `CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-5-5`, and Haiku effort `medium`. Merged into the installed `settings.json` by `deploy.sh`. |
| `.claude/scripts/` | Executor and reviewer templates, plus `gen_executors.py` (`--check` reports stale agent files). |
| `deploy.sh` | Installer (repo root). Installs the contents of `.claude/` into `~/.claude`, described below. |
| `tools/session-plot/` | Session plot tool: main and subagent context/fork timeline from a session jsonl. |

## Tools

- **`tools/session-plot/`**: plots the main-session and subagent context and fork timeline from a Claude Code session jsonl. Its own README has the usage (`npm run check -- <sessionId>` for the checks). It is a separate Node project with its own `package.json`, not part of the installed harness.

## Local config

Some settings are specific to one machine and must not be committed. The hooks read them from a single local file, `harness.local.json`, in the hooks directory of the installed config (`~/.claude/hooks/harness.local.json`). The copy under this repo's `.claude/hooks/` is gitignored, and `deploy.sh` never copies it. Its keys are:

- `readonly_scripts`: extra read-only scripts the main session may run through `main-readonly.py`, as `[{"pattern": "<regex>", "flags": ["--json"]}]`. A script runs only when the path is absolute and has no `..` component as typed. A single entry must fully match both the normalized path and its resolved real path (symlinks followed), and that same entry's `flags` must permit every flag given. Two different entries, one matching the typed path and one matching the real path, are not enough. Relative paths and paths containing `..` are refused, even when they would otherwise match.
- `statusline.deploy_log`: path to the deploy log the status line reads, as `{"statusline": {"deploy_log": "~/..."}}`. Unset means no deploy segment.
- `exempt_env`: `{"VAR": ["glob", ...]}`. A session is exempt from `main-readonly.py` and `task-gate.py` when some `VAR` is non-empty and matches one glob (case-sensitive `fnmatch`, so `"bot-*"` matches `bot-minion` but not `Bot-minion`). Caution: a broad glob such as `"*"` exempts every session in which that variable is set, which switches off the whole main-session guard for them.

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
