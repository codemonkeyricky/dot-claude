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
| `.claude/hooks/main-readonly.py` | PreToolUse. Blocks main-session Write/Edit/Bash except plans, memory and job tmp dirs; allows a read-only shell allowlist. Kill switch `CLAUDE_MAIN_WRITE=1`. Reads `readonly_scripts` and `exempt_env` from the local config (see below). |
| `.claude/hooks/task-gate.py` | Pre/PostToolUse. An Agent dispatch requires an in-progress, unblocked task owned by that executor; rejects cycles. Kill switch `CLAUDE_REQUIRE_TASK=0`. Sessions matched by `exempt_env` in the local config are exempt too. |
| `.claude/hooks/agent-names.py` | Names each subagent: executors get their fixed name, others a round-robin name. Kill switch `CLAUDE_AGENT_NAMES=0`. |
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

`deploy.sh` copies the tracked files under `.claude/` into `~/.claude` (or `CLAUDE_DIR`, if set; a relative `CLAUDE_DIR` is taken from the directory you run the script in), so `.claude/hooks/x.py` installs to `~/.claude/hooks/x.py`. Nothing outside `.claude/` is installed, and it never deletes anything there. `.claude/settings.harness.json` is never copied as a file: it is deep-merged into the target's `settings.json`. Keys from this repo win, and any other settings already in the target (permissions, theme, plugins, personal model choices) are kept. Dicts merge recursively; lists are replaced wholesale. So a list the repo sets (for example each `hooks.<Event>` array) replaces the target's list for that key, and any hook registered only in the target under the same event is dropped. Keys removed from this repo are not removed from the target; delete them there by hand. The merge runs before any file is copied, so an invalid `settings.json` (in the target) or `settings.harness.json` (in the repo) stops the install with an error and changes nothing.

After editing `.claude/scripts/executor-template.md` or `.claude/scripts/opus-reviewer.md`, run `python3 .claude/scripts/gen_executors.py --root .claude` in the repo to regenerate `.claude/agents/`, then `./deploy.sh`.

## Deploy hooks

Two hooks in `.claude/hooks/` deploy a repo's prod branch and open PRs to Vercel without a CI workflow:

- `deploy-trigger.py` (PostToolUse on Bash) starts a pass when the command is `git push` or `gh pr create|merge|close|reopen|ready`. It also runs on every SessionStart, which catches up on merges done on github.com.
- `deploy-reconcile.py` is the pass itself. It compares `origin/<branch>` and the open PR heads with the saved state, then deploys what is missing: prod, a preview per open PR, and cleanup of closed PRs. A pass lists up to 1000 open PRs; if gh returns that many, the list may be truncated, so that pass removes no aliases or previews.

The reconciler is spawned fully detached and the trigger returns at once. It never fails the tool: every error is swallowed and the hook exits 0. Kill switch: `DEPLOY_HOOKS=0`.

Host config is in `.claude/hooks/deploy-hooks.local.json`, which is gitignored. Copy `.claude/hooks/deploy-hooks.example.json` to that name and edit it. Each repo entry picks a `strategy`:

- `remote` (default): vite builds the snapshot locally to learn the expected bundle name, then `vercel deploy` builds it remotely.
- `prebuilt`: the repo's `build.command` runs in a snapshot, then `vercel deploy --prebuilt`.

`build.env` is applied to the local build. For `remote` it is also passed to the remote build as `--build-env K=V`; `VERCEL_GIT_COMMIT_SHA` is always the deployed sha, and a `build.env` value for it is ignored with a warning.

Each repo entry can also set:

- `branch`: the prod branch to deploy (`origin/<branch>`). When it is absent, the reconciler reads the checkout's default branch from `refs/remotes/origin/HEAD` and falls back to `master` if that ref is missing.
- `health`: gates a deploy on a JSON response after the bundle is verified.
- `deploys_log: true`: appends prod deploys to a log.

The top-level `deploys_log_path` sets where those log rows go (`~` is expanded). It defaults to `~/.local/state/deploy-hooks/deploys.jsonl`.

A pass does nothing until the state file exists, so run `python3 -I .claude/hooks/deploy-reconcile.py --seed` once per repo.

A failed snapshot is kept under `~/.cache/deploy-hooks` for debugging. Each pass deletes the older failed snapshots of that repo and keeps the newest one. A snapshot dir is named `<repo>-prod-` or `<repo>-pr-` plus 8 random characters, and only that exact shape is pruned, so a repo named `app` never prunes the snapshots of `app-pr` or `app-prod`.

If a `~/.vercel` or `~/.env.local` appears during a pass, only the deploy that sees it fails: it is not recorded as verified, and the pass goes on with its remaining actions. A guard path that stays present also fails the later deploys that check it, until it is removed. Paths that already exist when the pass starts are not fatal; every pass logs a warning for each of them.

Prod deploys go live before verification; a failed verification is recorded so a revert redeploys. The state keeps the failed sha as the prod sha with `"verified": false`, so reverting master to the previous sha differs from it and plans a prod redeploy. The failed sha itself is not retried, so a broken build does not loop; `--retry-failed` retries it on demand.

A preview cleanup drops its state entry only when `vercel alias rm` succeeds or reports that the alias itself is not found or does not exist. An auth, scope or network error keeps the entry and logs an error, and the cleanup is retried on the next pass.

Builds run with the reconciler's full environment (`build.env` adds to it and does not replace it), so a PR's build scripts can read your environment, tokens included. Only install hooks for repos whose PR branches you trust. Every command runs in its own process group, and a timed-out command is killed together with its whole group.

State, per-repo logs and events are kept in `~/.local/state/deploy-hooks`. `deploy.sh` installs the hook files and registrations; the local config is never installed.

## Tests

```sh
python3 -m pytest -q .claude/hooks
```

Or, without pytest:

```sh
python3 -m unittest discover -s .claude/hooks -p 'test_*.py'
```
