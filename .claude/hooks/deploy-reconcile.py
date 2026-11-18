#!/usr/bin/env python3
"""Idempotent deploy reconciler: replaces the GitHub Actions deploy workflows with local runs.

Each pass compares the state in ~/.local/state/deploy-hooks/<name>.json with origin/master and
with the open same-repo PRs, then deploys what is missing:
  prod     origin/master -> the Vercel prod project, verified against the live bundle
  preview  each open PR head -> a Vercel preview, aliased, verified, sticky PR comment
  cleanup  a PR that is no longer open -> its alias removed, state entry dropped

State (per repo): {"prod": {"sha", "failed_sha"}, "previews": {"<N>": {"sha", "url", "bundle",
"failed_sha"}}}. A failed sha is not retried until the ref moves to a new sha, unless run with --retry-failed.

Config per repo (deploy-hooks.local.json): "strategy" "remote" (default: vite build here, vercel
deploy builds remotely) or "prebuilt" (build.command in a snapshot, then vercel deploy --prebuilt);
"deploys_log": true appends prod deploys to ~/.local/state/deploy-hooks/deploys.jsonl
(override the path with top-level "deploys_log_path"); "health":
{"path", "expect"} gates a deploy on a JSON health response after the bundle is verified.

CLI: python3 -I deploy-reconcile.py [--repo NAME] [--dry-run] [--seed] [--force-prod] [--retry-failed]
  --dry-run       print the planned actions as JSON, deploy nothing
  --seed          record origin/master and the open PR heads as already deployed, deploy nothing
  --force-prod    also deploy origin/master to prod even when it is already recorded
  --retry-failed  plan a prod or preview sha recorded in failed_sha again (prod and previews)
A pass does nothing until the state file exists (run --seed first).
"""

import argparse
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "deploy-hooks.local.json")
STATE_DIR = os.path.expanduser("~/.local/state/deploy-hooks")
CACHE_DIR = os.path.expanduser("~/.cache/deploy-hooks")
DEPLOYS_LOG = os.path.expanduser("~/.local/state/deploy-hooks/deploys.jsonl")
EVENTS_LOG = os.path.join(STATE_DIR, "events.jsonl")
GUARD_PATHS = (os.path.expanduser("~/.vercel"), os.path.expanduser("~/.env.local"))
BUNDLE_RE = re.compile(r'assets/index-[^"]*\.js')
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+\.vercel\.app")
ALIAS_GONE_RE = re.compile(r"(?i)not.?found|does not exist|no such alias")
STICKY = "<!-- pr-preview -->"
POLL_TRIES = 20
POLL_SECONDS = 6
TIMEOUT_BUILD = 600
TIMEOUT_DEPLOY = 900
TIMEOUT_CMD = 120
PASS_DELAY = 5
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")


class DeployError(Exception):
    pass


# ----------------------------------------------------------------- pure helpers

def plan(state, master_sha, open_prs, force_prod=False, retry_failed=False):
    """Pure planner: (state, master sha, open PRs) -> ordered list of action dicts.

    state None (no state file) plans nothing. open_prs None (gh failed) plans no previews and no
    cleanups. Cross-repo PRs are ignored. Order: prod, then previews, then cleanups.
    force_prod redeploys prod even when master is already recorded. retry_failed plans a sha
    recorded in failed_sha again (prod and previews) instead of skipping it.
    """
    if state is None:
        return []
    actions = []
    prod = state.get("prod") or {}
    previews = state.get("previews") or {}
    prod_skip_failed = not retry_failed and master_sha == prod.get("failed_sha")
    if master_sha and (force_prod or (master_sha != prod.get("sha") and not prod_skip_failed)):
        actions.append({"kind": "prod", "sha": master_sha})
    if open_prs is None:
        return actions
    seen = set()
    for pr in open_prs:
        if pr.get("isCrossRepository"):
            continue
        n = str(pr["number"])
        seen.add(n)
        head = pr.get("headRefOid")
        rec = previews.get(n) or {}
        if head and head != rec.get("sha") and (retry_failed or head != rec.get("failed_sha")):
            actions.append({"kind": "preview", "pr": int(n), "sha": head})
    for n in sorted(previews, key=int):
        if n not in seen:
            actions.append({"kind": "cleanup", "pr": int(n)})
    return actions


def _json_candidates(out):
    text = (out or "").strip()
    try:
        yield json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                pass


def parse_deploy_url(out):
    """Deployment URL from `vercel deploy` output: JSON deployment.url first, else the last vercel.app URL."""
    for cand in _json_candidates(out):
        try:
            url = cand["deployment"]["url"]
        except (KeyError, TypeError):
            continue
        if url:
            url = str(url)
            return url if url.startswith("http") else "https://" + url
    found = URL_RE.findall(out or "")
    return found[-1] if found else None


def bundle_from_html(body):
    m = BUNDLE_RE.search(body or "")
    return os.path.basename(m.group(0)) if m else None


def alias_for(entry, n):
    return entry["preview"]["alias"].replace("{n}", str(n))


def strategy_of(entry):
    """"remote" (default): vite build here, then a remote Vercel build. "prebuilt": the repo's own
    build command here, then vercel deploy --prebuilt."""
    strategy = entry.get("strategy", "remote")
    if strategy not in ("remote", "prebuilt"):
        raise DeployError(f"unknown strategy {strategy!r} for {entry.get('name')}")
    return strategy


def health_ok(body_text, expect):
    """True when body_text is JSON and every key in expect equals the body's value (type-strict, so
    1 does not match true). An empty expect accepts any JSON body."""
    try:
        data = json.loads(body_text)
    except (TypeError, ValueError):
        return False
    if not expect:
        return True
    if not isinstance(data, dict):
        return False
    return all(k in data and type(data[k]) is type(v) and data[k] == v for k, v in expect.items())


def build_command(entry, out):
    """The repo's build.command with every {out} replaced by the out dir path."""
    cmd = (entry.get("build") or {}).get("command")
    if not isinstance(cmd, list) or not cmd:
        raise DeployError(f"{entry.get('name')}: prebuilt strategy needs build.command")
    return [str(arg).replace("{out}", out) for arg in cmd]


def build_env(entry, sha):
    """os.environ plus build.env, with VERCEL_GIT_COMMIT_SHA always set to the sha."""
    env = dict(os.environ)
    env.update((entry.get("build") or {}).get("env") or {})
    env["VERCEL_GIT_COMMIT_SHA"] = sha
    return env


def remote_deploy_args(scope, sha, prod):
    return ["deploy"] + (["--prod"] if prod else []) + [
        "--yes", "--scope", scope, "--build-env", f"VERCEL_GIT_COMMIT_SHA={sha}"]


def prebuilt_deploy_args(scope, prod):
    return ["deploy", "--prebuilt"] + (["--prod"] if prod else []) + [
        "--archive=tgz", "--yes", "--scope", scope]


def wants_deploys_log(entry):
    """Only repos that set "deploys_log": true write the deploys log (see deploys_log_path)."""
    return entry.get("deploys_log") is True


def lock_mode(force_prod=False, retry_failed=False, dry_run=False):
    """How a pass takes the per-repo lock: "none" (dry-run takes no lock), "block" (a manual
    --force-prod or --retry-failed waits for the running pass), or "try" (an unflagged hook run
    queues a rerun and exits when a pass holds the lock)."""
    if dry_run:
        return "none"
    if force_prod or retry_failed:
        return "block"
    return "try"


# ----------------------------------------------------------------- logging and IO

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(name, msg):
    line = f"{now_iso()} [{name}] {msg}"
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, name + ".log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def _text(x):
    if x is None:
        return ""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x


def tail(text, n=25):
    lines = (text or "").rstrip().splitlines()
    return "\n".join("    | " + line for line in lines[-n:])


def run(args, cwd, timeout, env=None):
    """Run a command. Returns (rc, combined output); never raises."""
    try:
        p = subprocess.run(args, cwd=cwd, env=env or GIT_ENV, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return -1, f"timed out after {timeout}s\n" + _text(e.stdout) + _text(e.stderr)
    except OSError as e:
        return -1, f"could not run {args[0]}: {e}"


def sh(name, args, cwd, timeout, env=None):
    """Run a command that must succeed; log its output tail; return stdout+stderr or raise DeployError."""
    rc, out = run(args, cwd, timeout, env)
    log(name, f"$ {' '.join(args)} (rc={rc}, cwd={cwd})\n{tail(out)}")
    if rc != 0:
        raise DeployError(f"{' '.join(args[:3])} failed (rc={rc})")
    return out


def vercel(name, args, cwd, timeout):
    """A vercel call. Its cwd must be a scratch directory under CACHE_DIR, never a checkout."""
    real = os.path.realpath(cwd)
    if not real.startswith(os.path.realpath(CACHE_DIR) + os.sep):
        raise DeployError(f"refusing to run vercel outside the scratch dir: {cwd}")
    return sh(name, ["vercel", *args], cwd, timeout)


def guard_home():
    """Vercel runs must not create ~/.vercel or ~/.env.local. Returns an error string or None."""
    bad = [p for p in GUARD_PATHS if os.path.exists(p)]
    if not bad:
        return None
    return "FATAL: " + ", ".join(bad) + " exists (a vercel command ran outside the snapshot)"


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True) + "\n")


def event(entry, kind, pr=None, sha=None, url=None, bundle=None, verified=None, error=None):
    append_jsonl(EVENTS_LOG, {"ts": now_iso(), "repo": entry["name"], "kind": kind, "pr": pr,
                              "sha": sha, "url": url, "bundle": bundle, "verified": verified,
                              "error": error})


def git(entry, args, timeout=TIMEOUT_CMD):
    checkout = entry["checkout"]
    return sh(entry["name"], ["git", "-C", checkout, *args], checkout, timeout)


def commit_info(entry, sha):
    try:
        subject = git(entry, ["log", "-1", "--format=%s", sha]).strip()
        short = git(entry, ["rev-parse", "--short", sha]).strip()
    except DeployError:
        subject, short = "", sha[:7]
    return short, subject


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def state_file(name):
    return os.path.join(STATE_DIR, name + ".json")


def load_state(entry):
    path = state_file(entry["name"])
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
    st.setdefault("prod", {})
    st.setdefault("previews", {})
    return st


def save_state(entry, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_file(entry["name"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def acquire(entry, block=False):
    """Exclusive lock. Non-blocking by default: returns an fd to keep open while held, or None when
    busy. block=True waits until the holder releases and then always returns the fd."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(os.path.join(STATE_DIR, entry["name"] + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if block else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def touch(path):
    with open(path, "a", encoding="utf-8"):
        os.utime(path, None)


def unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ----------------------------------------------------------------- deploy steps

def first_node_modules(entry):
    cands = entry.get("node_modules") or []
    for cand in cands:
        if os.path.isdir(cand):
            return os.path.realpath(cand)
    raise DeployError("no node_modules candidate exists: " + ", ".join(cands))


def fetch_text(url):
    """GET url and return the body text, or None on any network or HTTP error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "deploy-hooks/1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(4_000_000).decode("utf-8", "replace")
    except Exception:
        return None


def fetch_bundle(url):
    body = fetch_text(url)
    return None if body is None else bundle_from_html(body)


def poll_bundle(name, url, expected):
    live = None
    for i in range(POLL_TRIES):
        live = fetch_bundle(url)
        if live == expected:
            log(name, f"verified {url} serves {live}")
            return live, True
        if i < POLL_TRIES - 1:
            time.sleep(POLL_SECONDS)
    log(name, f"MISMATCH {url}: live={live!r} expected={expected!r}")
    return live, False


def poll_health(name, base_url, health):
    """GET base_url + health.path until the body matches health.expect (retried like poll_bundle).
    Returns (True, None) or (False, "health check failed: <body tail>")."""
    url = base_url + health["path"]
    expect = health.get("expect") or {}
    body = None
    for i in range(POLL_TRIES):
        body = fetch_text(url)
        if body is not None and health_ok(body, expect):
            log(name, f"health OK {url}")
            return True, None
        if i < POLL_TRIES - 1:
            time.sleep(POLL_SECONDS)
    shown = " ".join((body or "no response").split())[-200:]
    msg = f"health check failed: {shown}"
    log(name, f"{url}: {msg}")
    return False, msg


def build_remote(entry, root, sha, prod, project, res):
    """strategy "remote": vite build in the snapshot (for the expected bundle name), then vercel
    deploy, which builds the snapshot remotely. Sets res expected and url. Returns the dir the
    vercel alias runs in (the snapshot)."""
    name = entry["name"]
    scope = entry["scope"]
    snap = os.path.join(root, "snap")
    os.symlink(first_node_modules(entry), os.path.join(snap, "node_modules"))
    # The bundle hash depends on the sha (vite.config.ts defines __BUILD_SHA__), so the local
    # build and the remote build must both see VERCEL_GIT_COMMIT_SHA=<sha>.
    env = dict(os.environ, VERCEL_GIT_COMMIT_SHA=sha)
    sh(name, ["npx", "vite", "build"], snap, TIMEOUT_BUILD, env=env)
    assets = sorted(glob.glob(os.path.join(snap, "dist", "assets", "index-*.js")))
    if not assets:
        raise DeployError("vite build produced no dist/assets/index-*.js")
    res["expected"] = os.path.basename(assets[0])
    os.unlink(os.path.join(snap, "node_modules"))
    shutil.rmtree(os.path.join(snap, "dist"), ignore_errors=True)

    vercel(name, ["link", "--yes", "--project", project, "--scope", scope], snap, TIMEOUT_CMD)
    unlink_quiet(os.path.join(snap, ".env.local"))
    out = vercel(name, remote_deploy_args(scope, sha, prod), snap, TIMEOUT_DEPLOY)
    url = parse_deploy_url(out)
    if not url:
        raise DeployError("no deployment URL in vercel deploy output")
    res["url"] = url
    return snap


def build_prebuilt(entry, root, sha, prod, project, res):
    """strategy "prebuilt": clean install and the repo's own build command in the snapshot, which
    writes <root>/out/.vercel/ (so nothing is linked into out before the build), then vercel deploy
    --prebuilt from out. Sets res expected and url. Returns the dir the vercel alias runs in (out)."""
    name = entry["name"]
    scope = entry["scope"]
    snap = os.path.join(root, "snap")
    out = os.path.join(root, "out")
    env = build_env(entry, sha)
    # No node_modules symlink: the snapshot gets its own install from the frozen lockfile.
    sh(name, ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"], snap, TIMEOUT_BUILD, env=env)
    os.makedirs(out, exist_ok=True)
    sh(name, build_command(entry, out), snap, TIMEOUT_BUILD, env=env)
    assets = sorted(glob.glob(os.path.join(out, ".vercel", "output", "static", "assets", "index-*.js")))
    if not assets:
        raise DeployError("build produced no .vercel/output/static/assets/index-*.js")
    res["expected"] = os.path.basename(assets[0])

    vercel(name, ["link", "--yes", "--project", project, "--scope", scope], out, TIMEOUT_CMD)
    unlink_quiet(os.path.join(out, ".env.local"))
    args = prebuilt_deploy_args(scope, prod)
    if prod:
        try:
            text = vercel(name, args, out, TIMEOUT_DEPLOY)
        except DeployError:
            # The first --prod deploy sometimes fails with "Not authorized"; an identical retry works.
            log(name, "prod deploy failed; retrying once after 10s")
            time.sleep(10)
            text = vercel(name, args, out, TIMEOUT_DEPLOY)
    else:
        text = vercel(name, args, out, TIMEOUT_DEPLOY)
    url = parse_deploy_url(text)
    if not url:
        raise DeployError("no deployment URL in vercel deploy output")
    res["url"] = url
    return out


def deploy_version(entry, sha, prod, project, alias, target_url):
    """Snapshot sha, build it with the repo's strategy, alias it, verify the live bundle, then run the
    optional health check. Never raises. Returns a dict with verified, url, expected, live, error, snapshot.
    """
    name = entry["name"]
    scope = entry["scope"]
    res = {"verified": False, "url": None, "expected": None, "live": None, "error": None, "snapshot": None}
    root = None
    try:
        strategy = strategy_of(entry)
        os.makedirs(CACHE_DIR, exist_ok=True)
        root = tempfile.mkdtemp(prefix=f"{name}-{'prod' if prod else 'pr'}-", dir=CACHE_DIR)
        res["snapshot"] = root
        snap = os.path.join(root, "snap")
        os.mkdir(snap)
        tar_path = os.path.join(root, "src.tar")
        sh(name, ["git", "-C", entry["checkout"], "archive", "--format=tar", "-o", tar_path, sha],
           entry["checkout"], TIMEOUT_CMD)
        sh(name, ["tar", "-x", "-f", tar_path, "-C", snap], root, TIMEOUT_CMD)
        os.unlink(tar_path)

        if strategy == "prebuilt":
            vdir = build_prebuilt(entry, root, sha, prod, project, res)
        else:
            vdir = build_remote(entry, root, sha, prod, project, res)
        url = res["url"]
        if alias:
            vercel(name, ["alias", "set", url, alias, "--scope", scope], vdir, TIMEOUT_CMD)
        live, ok = poll_bundle(name, target_url, res["expected"])
        res["live"] = live
        if not ok:
            raise DeployError(f"{target_url} serves {live!r}, expected {res['expected']!r}")
        if entry.get("health"):
            healthy, err = poll_health(name, target_url, entry["health"])
            if not healthy:
                raise DeployError(err)
        res["verified"] = True
    except Exception as e:  # noqa: BLE001 - a pass must never raise out
        res["error"] = str(e) or e.__class__.__name__
        if not isinstance(e, DeployError):
            log(name, "traceback:\n" + traceback.format_exc())
        log(name, f"FAILED {'prod' if prod else 'preview'} {sha[:7]}: {res['error']}")
    finally:
        guard = guard_home()
        if guard:
            log(name, guard)
            res["verified"] = False
            res["error"] = (res["error"] + "; " if res["error"] else "") + guard
        if root and res["verified"] and not res["error"]:
            shutil.rmtree(root, ignore_errors=True)
            res["snapshot"] = None
        elif root:
            log(name, f"snapshot kept for inspection: {root}")
    return res


def deploy_prod(entry, sha, st):
    name = entry["name"]
    prod = entry["prod"]
    log(name, f"prod deploy {sha[:7]} -> {prod['project']}")
    res = deploy_version(entry, sha, True, prod["project"], None, prod["url"])
    ok = res["verified"] and not res["error"]
    if wants_deploys_log(entry):
        short, subject = commit_info(entry, sha)
        append_jsonl(DEPLOYS_LOG, {
            "ts": now_iso(), "sha": sha, "short": short, "subject": subject, "target": "prod",
            "expected": res["expected"] or "", "live": res["live"] or "", "verified": ok, "dryRun": False,
            "by": "deploy-hook", "smoke": None, "smokeQuery": None,
        })
    event(entry, "prod", sha=sha, url=res["url"], bundle=res["expected"], verified=ok, error=res["error"])
    if ok:
        st["prod"] = {"sha": sha, "failed_sha": None, "url": res["url"], "bundle": res["expected"]}
        log(name, f"prod OK {sha[:7]} bundle {res['expected']}")
    else:
        st["prod"] = {**st.get("prod", {}), "failed_sha": sha}
        log(name, f"prod FAILED {sha[:7]}; will not retry this sha unless run with --retry-failed")


def upsert_comment(entry, n, sha, expected, url):
    name = entry["name"]
    gh = entry["gh"]
    body = f"{STICKY}\n**Preview:** {url}\ncommit `{sha[:7]}` (head `{sha}`), bundle `{expected}`"
    listing = sh(name, ["gh", "api", f"repos/{gh}/issues/{n}/comments", "--paginate", "--jq",
                        f'.[] | select(.body | startswith("{STICKY}")) | .id'], STATE_DIR, TIMEOUT_CMD)
    ids = [x.strip() for x in listing.splitlines() if x.strip().isdigit()]
    if ids:
        sh(name, ["gh", "api", "-X", "PATCH", f"repos/{gh}/issues/comments/{ids[0]}", "-f", f"body={body}"],
           STATE_DIR, TIMEOUT_CMD)
    else:
        sh(name, ["gh", "api", f"repos/{gh}/issues/{n}/comments", "-f", f"body={body}"],
           STATE_DIR, TIMEOUT_CMD)


def deploy_preview(entry, n, sha, st):
    name = entry["name"]
    alias = alias_for(entry, n)
    url = "https://" + alias
    res = deploy_version(entry, sha, False, entry["preview"]["project"], alias, url)
    ok = res["verified"] and not res["error"]
    error = res["error"]
    if ok:
        try:
            upsert_comment(entry, n, sha, res["expected"], url)
        except DeployError as e:
            error = f"comment: {e}"
            log(name, f"PR #{n} sticky comment failed (deploy is still verified): {e}")
        st["previews"][str(n)] = {"sha": sha, "url": url, "bundle": res["expected"]}
        log(name, f"preview OK PR #{n} {sha[:7]} at {url}")
    else:
        rec = dict(st["previews"].get(str(n)) or {})
        rec["failed_sha"] = sha
        st["previews"][str(n)] = rec
        log(name, f"preview FAILED PR #{n} {sha[:7]}; will not retry this sha unless run with --retry-failed")
    event(entry, "preview", pr=n, sha=sha, url=url, bundle=res["expected"], verified=ok, error=error)


def cleanup_preview(entry, n, st):
    name = entry["name"]
    alias = alias_for(entry, n)
    os.makedirs(CACHE_DIR, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=f"{name}-cleanup-", dir=CACHE_DIR)
    try:
        rc, out = run(["vercel", "alias", "rm", alias, "--yes", "--scope", entry["scope"]], scratch, TIMEOUT_CMD)
        log(name, f"alias rm {alias} (rc={rc})\n{tail(out)}")
        guard = guard_home()
        if guard:
            log(name, guard)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    ok = rc == 0 or bool(ALIAS_GONE_RE.search(out or ""))
    error = None if ok else f"alias rm failed (rc={rc})"
    event(entry, "cleanup", pr=n, url="https://" + alias, verified=ok, error=error)
    if ok:
        st["previews"].pop(str(n), None)
        log(name, f"cleaned up PR #{n} ({alias})")
    else:
        log(name, f"cleanup of PR #{n} failed; kept in state to retry")


# ----------------------------------------------------------------- passes

def gather(entry):
    """Fetch, then return (origin/master sha, open PRs or None when gh fails)."""
    name = entry["name"]
    # PR heads go under refs/pr-heads/* (outside refs/remotes) so they never show up as
    # remote branches in `git branch -r` or the status "unmerged" list. No --prune here:
    # a failed PR-head fetch must not lose the refs that archive the previews.
    git(entry, ["fetch", "origin"], TIMEOUT_CMD)
    try:
        git(entry, ["fetch", "origin", "+refs/pull/*/head:refs/pr-heads/*"], TIMEOUT_CMD)
    except DeployError as e:
        log(name, f"warning: PR head fetch failed: {e}")
    master = git(entry, ["rev-parse", "origin/master"]).strip()
    try:
        raw = sh(name, ["gh", "pr", "list", "-R", entry["gh"], "--state", "open",
                        "--json", "number,headRefOid,isCrossRepository", "--limit", "100"],
                 STATE_DIR, TIMEOUT_CMD)
        open_prs = json.loads(raw.strip() or "[]")
    except (DeployError, ValueError) as e:
        log(name, f"gh pr list failed; no preview or cleanup this pass: {e}")
        open_prs = None
    return master, open_prs


def decide(st, master, open_prs, force_prod, retry_failed=False):
    if st is None:
        return []
    return plan(st, master, open_prs, force_prod, retry_failed)


def dry_run(entry, force_prod, retry_failed=False):
    st = load_state(entry)
    master, open_prs = gather(entry)
    actions = decide(st, master, open_prs, force_prod, retry_failed)
    out = {"repo": entry["name"], "master": master, "state_present": st is not None,
           "open_prs": open_prs, "actions": actions}
    if st is None:
        out["note"] = "no state file: a real pass does nothing until --seed"
    print(json.dumps(out, indent=2))


def seed(entry):
    name = entry["name"]
    master, open_prs = gather(entry)
    if open_prs is None:
        raise DeployError("gh pr list failed; cannot seed")
    st = {"prod": {"sha": master, "failed_sha": None}, "previews": {}}
    for pr in open_prs:
        if pr.get("isCrossRepository"):
            continue
        n = str(pr["number"])
        st["previews"][n] = {"sha": pr["headRefOid"], "url": "https://" + alias_for(entry, n), "bundle": None}
    save_state(entry, st)
    log(name, f"seeded: prod {master[:7]}, previews {sorted(st['previews'], key=int)}")


def run_pass(entry, force_prod, retry_failed=False):
    name = entry["name"]
    st = load_state(entry)
    if st is None:
        log(name, "no state file: run --seed first; nothing deployed")
        return
    master, open_prs = gather(entry)
    actions = decide(st, master, open_prs, force_prod, retry_failed)
    log(name, f"pass: master {master[:7]}, open PRs {None if open_prs is None else len(open_prs)}, "
              f"actions {actions}")
    for act in actions:
        try:
            if act["kind"] == "prod":
                deploy_prod(entry, act["sha"], st)
            elif act["kind"] == "preview":
                deploy_preview(entry, act["pr"], act["sha"], st)
            elif act["kind"] == "cleanup":
                cleanup_preview(entry, act["pr"], st)
        except Exception:  # noqa: BLE001
            log(name, f"action {act} crashed:\n" + traceback.format_exc())
        save_state(entry, st)
    log(name, "pass done")


def locked_pass(entry, force_prod, retry_failed=False):
    name = entry["name"]
    rerun = os.path.join(STATE_DIR, name + ".rerun")
    fd = acquire(entry)
    if fd is None and lock_mode(force_prod, retry_failed) == "block":
        log(name, "waiting for running pass…")
        fd = acquire(entry, block=True)
    if fd is None:
        touch(rerun)
        log(name, "a pass is running; queued a rerun")
        return
    try:
        time.sleep(PASS_DELAY)
        while True:
            unlink_quiet(rerun)
            try:
                run_pass(entry, force_prod, retry_failed)
            except Exception:  # noqa: BLE001
                log(name, "pass crashed:\n" + traceback.format_exc())
            force_prod = False
            retry_failed = False
            if os.path.exists(rerun):
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)
            if not os.path.exists(rerun):
                break
            # A trigger landed between the check and the unlock: take the lock back and go again.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                break
    finally:
        os.close(fd)


def main(argv=None):
    global DEPLOYS_LOG
    ap = argparse.ArgumentParser(description="Deploy reconciler (see module docstring).")
    ap.add_argument("--repo", help="only this repo name from deploy-hooks.local.json")
    ap.add_argument("--dry-run", action="store_true", help="print planned actions, deploy nothing")
    ap.add_argument("--seed", action="store_true", help="record current state as deployed, deploy nothing")
    ap.add_argument("--force-prod", action="store_true", help="deploy origin/master to prod even if recorded")
    ap.add_argument("--retry-failed", action="store_true",
                    help="plan a prod or preview sha that failed before (failed_sha) again")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        print(f"cannot read {CONFIG_PATH}: {e}", file=sys.stderr)
        return 2
    if cfg.get("deploys_log_path"):
        DEPLOYS_LOG = os.path.expanduser(str(cfg["deploys_log_path"]))
    repos = [r for r in cfg.get("repos") or [] if not args.repo or r.get("name") == args.repo]
    if not repos:
        print("no matching repo in config", file=sys.stderr)
        return 2
    rc = 0
    for entry in repos:
        name = entry["name"]
        try:
            if args.dry_run:
                dry_run(entry, args.force_prod, args.retry_failed)
            elif args.seed:
                fd = acquire(entry)
                if fd is None:
                    log(name, "seed refused: a pass holds the lock")
                    rc = 1
                    continue
                try:
                    seed(entry)
                finally:
                    os.close(fd)
            else:
                locked_pass(entry, args.force_prod, args.retry_failed)
        except Exception:  # noqa: BLE001
            rc = 1
            log(name, "crashed:\n" + traceback.format_exc())
    return rc


if __name__ == "__main__":
    sys.exit(main())
