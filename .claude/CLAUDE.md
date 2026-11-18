# Default workflow: plan here, execute on Haiku executors

These rules apply to the main (top-level) session only. If you are a subagent/executor working from a brief, do the work yourself: never dispatch further subagents and don't create or update tasks.

## Plan with a task list (a DAG)
- For any non-trivial request (more than a quick answer or a one-line edit), break the work into tasks with TaskCreate (load TaskCreate/TaskUpdate via ToolSearch if deferred): one task per dispatchable unit of work. Keep it updated: in_progress when starting/dispatching, completed only after the report passes its acceptance checks. Add follow-up tasks for anything left open; delete superseded ones.
- Make the task list a DAG. After creating tasks, wire only real dependencies with TaskUpdate `addBlockedBy` (B needs A's output, or A and B touch the same files); never add edges for mere preference. A task is ready when it is pending, unowned, and all its blockers are completed.
- Tasks that are yours or the user's (reviews, decisions, the final report) get `metadata: {"dispatch": false}`.
- Dispatch by readiness: one message with a TaskUpdate per ready task (status `in_progress`, owner = a distinct executor name), then the next message with one Agent call per task. Never dispatch ready tasks one per turn. When a report passes its checks, mark the task completed and dispatch everything the hook reports as newly ready the same way.

## Dispatch work to Haiku executors
- Don't do implementation work yourself (edits, commands, test runs, bulk searches). Dispatch it with the Agent tool to an executor from this pool (as `subagent_type`): `qorvax-tel`, `zyrene-vosk`, `thal-ummir`, `kreeva-osh`, `vexlor-ain`, `nyxa-prell`, `oomrak-zhee`, `xilth-varo`, `juun-ketrax`, `plimm-oraxi`. They are defined in `~/.claude/agents/<name>.md` and run on Haiku at medium effort. The task's owner must equal the executor name.
- Executors default to medium effort (agent frontmatter). Dispatch with `effort: "high"` for debugging or open-ended investigation, multi-file changes where the executor must infer how code fits together, or a retry after a report failed its acceptance checks.
- Parallel executors are encouraged for independent work, one Agent call each in the same message, each with a distinct name and its own brief. Work that touches the same files goes to one executor or runs in sequence. When parallel executors edit code, give each its own worktree (`isolation: "worktree"`).
- Every brief is self-contained (Haiku has no context from this conversation): goal, files, exact steps, **acceptance checks**, and a report format. End with an explicit format, e.g. "Report in under 150 words: files changed, pass/fail per acceptance check, errors or blockers. No code dumps, no restating the task."
- Stay the orchestrator: review reports, integrate/merge worktree results, resolve conflicts, and verify each report against its acceptance checks before telling the user it's done. Don't trust a "done" without checking.
- Do the work directly (no subagent) only when it's trivial, needs the full conversation context, or is faster than writing a brief — within what the read-only hook allows.

## PR review tiers (token budget)
- Pick the tier from the PR diff (`gh pr view <ref> --json additions,deletions,files`), ignoring lockfiles and generated files:
  - **Skip review:** docs/comments only, renames or moves with no logic change, data/config value tweaks. Say in the report that review was skipped and why.
  - **Haiku review:** under ~50 changed lines with no logic, geometry, hook or security change. Dispatch a `pr-reviewer-N` with `model: "haiku"`.
  - **Opus review (default):** everything else (logic, geometry, hooks/security, or 50+ changed lines). Dispatch `pr-reviewer-N` with no model override.
- At most 3 review rounds per PR, stated in the brief as `Review round N of 3`. Rounds 2 and 3 are re-reviews: give the reviewer the previously reviewed SHA and the previous round's findings so it reviews only the fix diff. If round 3 still returns CHANGES, stop and ask the user before more fixes.

## Main-session Bash (token budget)
- The main-readonly hook rejects many shell shapes, and each rejection wastes a full-context turn. Allowed: single read commands (`ls`, `cat`, `head`, `wc`, `grep`, `find -name`, `jq`, `sed -n`, `git -C <path> log`), a read command piped into `head`/`grep`/`sort`/`uniq` (`cmd | head`), and `grep -e a -e b`. Rejected: `;`, `&&`, `||`, `>` (incl. `2>/dev/null`), `echo`, `diff`, `python3 -c`, and `$`/`{` expansion. Don't send a rejected shape.
- Read files with the Read tool. For several independent reads, send several Bash/Read calls in one message: they run in parallel and cost no extra turn.
- If a read needs a script, `python -c` or a rejected shape, fold it into an executor brief you are already sending, or skip it; dispatch an executor just for a read only when the answer changes what you do next.

## Enforcement (hooks in ~/.claude/settings.json)
- `main-readonly.py`: main can't Write/Edit/Bash except plans, memory, and job tmp dirs. Kill switch `CLAUDE_MAIN_WRITE=1`.
- `task-gate.py`: an Agent dispatch needs an in_progress, unblocked task owned by that `subagent_type`; rejects cycles, in_progress on a blocked task, and one executor owning two in_progress tasks; after a completion it lists ready tasks. Exempt types: web-fetch, claude-code-guide, Explore, Plan. Kill switch `CLAUDE_REQUIRE_TASK=0`.
- `agent-names.py`: names each subagent (executors get their own name; others get a round-robin name). Kill switch `CLAUDE_AGENT_NAMES=0`.
- `result-capture.py` (Stop): appends each `result:` line of the final message to `~/.claude/hooks/state/completed-work.jsonl` (deduped per session); `task-gate.py` appends a `"kind": "task"` line there on each task completion.
