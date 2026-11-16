# dot-claude

An orchestration harness for Claude Code. The main session plans a task DAG and dispatches the implementation work to cheap Haiku "executor" subagents, while hooks enforce that split. The repo holds only the shareable config: workflow rules, hooks, agent definitions, generator scripts and the hook registrations in `settings.json`.

## How it works

- **The main session is read-only.** It may not Write, Edit or run arbitrary Bash. It plans, reads, and dispatches.
- **Work is tracked as tasks.** Each dispatchable unit is a task (TaskCreate/TaskUpdate). Dependencies are wired as a DAG; a task is ready when it is pending, unowned, and all its blockers are completed.
- **Executors are dispatched by readiness.** A task is set `in_progress` with an owner equal to an executor name, then one Agent call per ready task runs it. Executors run on Haiku at medium effort.
- **Reports are verified, not trusted.** The caller checks each report against the brief's acceptance checks before marking a task completed.
- **Hooks enforce the rules.** Agent dispatch is refused without a matching in-progress, unblocked task, and every completion is logged.

`CLAUDE.md` holds the full rules; this file only summarizes them.

## Components

| Path | What it is |
| --- | --- |
| `CLAUDE.md` | Workflow rules for the main session: task DAG, executor dispatch, briefs, verification. |
| `agents/` | Ten Haiku executors (`qorvax-tel` … `plimm-oraxi`) generated from `scripts/executor-template.md` by `scripts/gen_executors.py`, plus `pr-reviewer-1` … `pr-reviewer-3`, Opus PR reviewers defined in `scripts/opus-reviewer.md`. |
| `hooks/main-readonly.py` | PreToolUse. Blocks main-session Write/Edit/Bash except plans, memory and job tmp dirs; allows a read-only shell allowlist. Kill switch `CLAUDE_MAIN_WRITE=1`. Reads `readonly_scripts` and `exempt_env` from the local config (see below). |
| `hooks/task-gate.py` | Pre/PostToolUse. An Agent dispatch requires an in-progress, unblocked task owned by that executor; rejects cycles. Kill switch `CLAUDE_REQUIRE_TASK=0`. Sessions matched by `exempt_env` in the local config are exempt too. |
| `hooks/agent-names.py` | Names each subagent: executors get their fixed name, others a round-robin name. Kill switch `CLAUDE_AGENT_NAMES=0`. |
| `hooks/result-capture.py` | Stop. Appends each `result:` line of the final message to `hooks/state/completed-work.jsonl`. |
| `hooks/session-repo.py` | Titles each session `[repo] branch` followed by its title, and buckets sessions by repo. |
| `hooks/sessions-by-repo.py` | Report CLI over those per-repo session buckets. |
| `settings.json` | Hook registrations, `CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-5-5`, and Haiku effort `medium`. |
| `scripts/` | Executor and reviewer templates, plus `gen_executors.py` (`--check` reports stale agent files). |
| `deploy.sh` | Installer, described below. |

## Local config

Some settings are specific to one machine and must not be committed. The hooks read them from a single local file, `harness.local.json`, in the hooks directory of the installed config (`~/.claude/hooks/harness.local.json`). The copy under this repo's `hooks/` is gitignored, and `deploy.sh` never copies it. Its keys are:

- `readonly_scripts`: extra read-only scripts the main session may run through `main-readonly.py`, as `[{"pattern": "<regex>", "flags": ["--json"]}]`. A script runs only when the path is absolute and has no `..` component as typed. A single entry must fully match both the normalized path and its resolved real path (symlinks followed), and that same entry's `flags` must permit every flag given. Two different entries, one matching the typed path and one matching the real path, are not enough. Relative paths and paths containing `..` are refused, even when they would otherwise match.
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

`deploy.sh` copies the tracked files into `~/.claude` (or `CLAUDE_DIR`, if set) and never deletes anything there. `settings.json` is deep-merged rather than copied: keys from this repo win, and any other settings already in the target (permissions, theme, plugins, personal model choices) are kept. Dicts merge recursively; lists are replaced wholesale. So a list the repo sets (for example each `hooks.<Event>` array) replaces the target's list for that key, and any hook registered only in the target under the same event is dropped. Keys removed from this repo are not removed from the target; delete them there by hand. The merge runs before any file is copied, so an invalid `settings.json` (in the target or the repo) stops the install with an error and changes nothing.

After editing `scripts/executor-template.md` or `scripts/opus-reviewer.md`, run `python3 scripts/gen_executors.py --root .` in the repo to regenerate `agents/`, then `./deploy.sh`.

## Tests

```sh
python3 -m pytest -q hooks
```

Or, without pytest:

```sh
python3 -m unittest discover -s hooks -p 'test_*.py'
```
