#!/usr/bin/env python3
"""Group Claude Code sessions by the git repo they worked on.

Reads per-session state files ``<STATE_DIR>/*.json`` (written by the session-repo
hook) and the results log (``completed-work.jsonl``, written by result-capture),
then prints sessions bucketed by their dominant repo root, newest first.

STATE_DIR: env SESSION_REPO_STATE_DIR, else ~/.claude/hooks/state/session-repo.
Results log: env RESULT_CAPTURE_LOG, else ~/.claude/hooks/state/completed-work.jsonl.

Options:
  --days N     only sessions updated in the last N days (default 14; 0 = all)
  --repo NAME  only sessions whose dominant repo name equals NAME, or whose
               dominant root contains NAME as a substring
  --results K  latest K result texts per session (default 2, newest first)
  --json       machine-readable output: a list of buckets

Corrupt state files are skipped with a warning on stderr. Missing files are not
an error. Stdlib only.
"""

import argparse
import datetime
import json
import os
import sys

NO_REPO = "(no repo)"
TITLE_PROMPT_CHARS = 60
RESULT_TEXT_CHARS = 120
UTC = datetime.timezone.utc


def state_dir():
    return os.environ.get("SESSION_REPO_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "state", "session-repo")


def results_log_path():
    return os.environ.get("RESULT_CAPTURE_LOG") or os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "state", "completed-work.jsonl")


def parse_ts(value):
    """Parse a UTC ``%Y-%m-%dT%H:%M:%SZ`` string; return an aware datetime or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def warn(msg):
    sys.stderr.write("warning: " + msg + "\n")


def load_states(directory):
    """Return a list of valid state dicts. Corrupt files are skipped with a warning."""
    states = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return states
    for name in names:
        if not name.endswith(".json"):
            continue  # ignores *.lock, *.tmp and anything else
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("top-level value is not an object")
            if not isinstance(data.get("session_id"), str) or not data["session_id"]:
                raise ValueError("missing session_id")
            if parse_ts(data.get("updated_ts")) is None:
                raise ValueError("bad updated_ts")
            repos = data.get("repos")
            if repos is not None and not isinstance(repos, dict):
                raise ValueError("repos is not an object")
        except (OSError, ValueError) as exc:
            warn("skipping corrupt state file %s (%s)" % (path, exc))
            continue
        states.append(data)
    return states


def load_results(path):
    """Return {session_id: [text, ...]} in file order. Malformed lines are skipped."""
    by_sid = {}
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError:
        return by_sid
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "result":
                continue
            sid = rec.get("session_id")
            text = rec.get("text")
            if isinstance(sid, str) and isinstance(text, str):
                by_sid.setdefault(sid, []).append(text)
    return by_sid


def session_view(state, results_by_sid, k):
    """Build the per-session dict for output."""
    sid = state["session_id"]
    title_obj = state.get("title")
    applied = title_obj.get("applied") if isinstance(title_obj, dict) else None
    prompt = state.get("first_prompt")
    if isinstance(applied, str) and applied.strip():
        title = applied.strip()
    elif isinstance(prompt, str) and prompt.strip():
        title = " ".join(prompt.split())[:TITLE_PROMPT_CHARS]
    else:
        title = "(untitled)"

    repos = state.get("repos") or {}
    dom = state.get("dominant")
    dom_root = dom.get("root") if isinstance(dom, dict) else None
    dom_name = dom.get("name") if isinstance(dom, dict) else None
    if not isinstance(dom_root, str) or not dom_root:
        dom_root = None

    counts = {}
    for root, info in repos.items():
        c = info.get("count") if isinstance(info, dict) else None
        counts[root] = c if isinstance(c, int) and not isinstance(c, bool) else 0
    total = sum(counts.values())

    share = None
    other = []
    if dom_root is not None:
        share = round(100.0 * counts.get(dom_root, 0) / total) if total else 0
        others = [(counts[r], (repos[r].get("name") if isinstance(repos[r], dict) else None) or r)
                  for r in repos if r != dom_root]
        others.sort(key=lambda t: (-t[0], t[1]))
        other = [name for _, name in others]

    texts = results_by_sid.get(sid, [])
    latest = list(reversed(texts[-k:])) if k > 0 else []

    return {
        "session_id": sid,
        "title": title,
        "updated_ts": state["updated_ts"],
        "share": share,
        "other_repos": other,
        "results": latest,
        "_dom_root": dom_root,
        "_dom_name": dom_name if isinstance(dom_name, str) and dom_name else None,
        "_dom_repo_name_for_filter": dom_name if isinstance(dom_name, str) else "",
    }


def build_buckets(sessions):
    """Group session views by dominant root; newest bucket first, newest session first."""
    buckets = {}
    for s in sessions:
        root = s["_dom_root"]
        key = root if root is not None else None
        b = buckets.get(key)
        if b is None:
            name = s["_dom_name"] or (os.path.basename(root.rstrip("/")) if root else NO_REPO)
            if root is None:
                name = NO_REPO
            b = {"name": name, "root": root, "sessions": []}
            buckets[key] = b
        b["sessions"].append(s)
    out = list(buckets.values())
    for b in out:
        b["sessions"].sort(key=lambda s: s["updated_ts"], reverse=True)
    out.sort(key=lambda b: b["sessions"][0]["updated_ts"], reverse=True)
    for b in out:
        for s in b["sessions"]:
            for private in ("_dom_root", "_dom_name", "_dom_repo_name_for_filter"):
                s.pop(private, None)
        b["sessions"] = [{k: s[k] for k in ("session_id", "title", "updated_ts", "share",
                                             "other_repos", "results")}
                         for s in b["sessions"]]
    return out


def filter_states(states, days, repo):
    if days > 0:
        cutoff = datetime.datetime.now(UTC) - datetime.timedelta(days=days)
        states = [s for s in states if parse_ts(s["updated_ts"]) >= cutoff]
    if repo:
        kept = []
        for s in states:
            dom = s.get("dominant")
            if not isinstance(dom, dict):
                continue
            name = dom.get("name")
            root = dom.get("root")
            if (isinstance(name, str) and name == repo) or (
                    isinstance(root, str) and repo in root):
                kept.append(s)
        states = kept
    return states


def local_time(ts):
    return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def truncate(text, n):
    return text if len(text) <= n else text[:n] + "…"


def render_text(buckets):
    if not buckets:
        return "No sessions found.\n"
    lines = []
    for b in buckets:
        root_part = ("  " + b["root"]) if b["root"] else ""
        lines.append("%s%s  (%d session%s)" % (
            b["name"], root_part, len(b["sessions"]), "" if len(b["sessions"]) == 1 else "s"))
        for s in b["sessions"]:
            tag = "" if b["root"] is None else "[%s] " % b["name"]
            share = "" if s["share"] is None else "  %d%%" % s["share"]
            also = ("  also: " + ", ".join(s["other_repos"])) if s["other_repos"] else ""
            lines.append("  %s  %s%s  %s%s%s" % (
                local_time(s["updated_ts"]), tag, s["title"], s["session_id"][:8], share, also))
            for r in s["results"]:
                lines.append("      result: " + truncate(r, RESULT_TEXT_CHARS))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Group Claude Code sessions by git repo.")
    ap.add_argument("--days", type=int, default=14,
                    help="only sessions updated in the last N days (0 = all; default 14)")
    ap.add_argument("--repo", default=None,
                    help="dominant repo name (exact) or root substring")
    ap.add_argument("--results", type=int, default=2,
                    help="latest K result texts per session (default 2)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)
    if args.days < 0:
        ap.error("--days must be >= 0")

    states = load_states(state_dir())
    states = filter_states(states, args.days, args.repo)
    results = load_results(results_log_path())
    sessions = [session_view(s, results, args.results) for s in states]
    buckets = build_buckets(sessions)

    if args.json:
        sys.stdout.write(json.dumps(buckets, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(buckets))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
