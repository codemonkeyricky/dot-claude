"""Generate the named Haiku executor and Opus reviewer agent definitions.

Pools:
  executor  <- <root>/scripts/executor-template.md   -> <root>/agents/qorvax-tel.md ... plimm-oraxi.md
               (edit the template, placeholders: {slug}, {full}, {first})
  reviewer  <- <root>/scripts/opus-reviewer.md       -> <root>/agents/pr-reviewer-1.md ... pr-reviewer-3.md
               (source is a frontmatter + body definition; edit it directly)

<root> defaults to ~/.claude (the installed config). In this repo, the harness is in .claude/, so use --root .claude

Run after editing either source:

    python3 .claude/scripts/gen_executors.py --root .claude

Usage: python3 gen_executors.py [--root DIR] [--check]
  --root DIR  config root holding scripts/ and agents/ (default ~/.claude; this repo: .claude)
  (no flag)   write each <root>/agents/<slug>.md for every pool
  --check     write nothing; print "stale: <path>" for each missing or
              differing file (covers every pool) and exit 1 if any, else exit 0

Env override: EXECUTOR_TEMPLATE=<path> uses another executor template file
(used for negative checks).
"""
import os
import sys

HOME = os.path.expanduser("~")
DEFAULT_ROOT = os.path.join(HOME, ".claude")

POOL = [
    ("qorvax-tel", "Qorvax Tel"),
    ("zyrene-vosk", "Zyrene Vosk"),
    ("thal-ummir", "Thal Ummir"),
    ("kreeva-osh", "Kreeva Osh"),
    ("vexlor-ain", "Vexlor Ain"),
    ("nyxa-prell", "Nyxa Prell"),
    ("oomrak-zhee", "Oomrak Zhee"),
    ("xilth-varo", "Xilth Varo"),
    ("juun-ketrax", "Juun Ketrax"),
    ("plimm-oraxi", "Plimm Oraxi"),
]

REVIEWER_POOL = [
    ("pr-reviewer-1", "PR Reviewer 1"),
    ("pr-reviewer-2", "PR Reviewer 2"),
    ("pr-reviewer-3", "PR Reviewer 3"),
]


def paths(root):
    """(agents_dir, executor_template, reviewer_source) for a config root."""
    template = os.environ.get(
        "EXECUTOR_TEMPLATE",
        os.path.join(root, "scripts", "executor-template.md"),
    )
    return (os.path.join(root, "agents"),
            template,
            os.path.join(root, "scripts", "opus-reviewer.md"))


def render(template, slug, full):
    first = full.split()[0]
    return (template.replace("{slug}", slug)
                    .replace("{full}", full)
                    .replace("{first}", first))


def split_source(text):
    lines = text.split("\n")
    if lines[0] != "---":
        raise SystemExit("reviewer source has no frontmatter")
    end = lines.index("---", 1)
    return lines[1:end], "\n".join(lines[end + 1:]).lstrip("\n")


def render_reviewer(source_name, fm, body, slug, full):
    out = []
    for line in fm:
        if line.startswith("# "):
            out.append("# GENERATED from %s by python3 scripts/gen_executors.py --root . "
                       "(from the repo root) - do not edit by hand" % source_name)
        elif line.startswith("name:"):
            out.append("name: " + slug)
        elif line.startswith("description:"):
            out.append("description: Reviewer (%s) — " % full + line.split(":", 1)[1].strip())
        else:
            out.append(line)
    return ("---\n" + "\n".join(out) + "\n---\n\n"
            + "Your name is %s; sign your final report with it.\n\n" % full
            + body)


def targets(root):
    """Yield (path, rendered_text) for every generated agent file under root."""
    agents, template_path, reviewer_source = paths(root)
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    for slug, full in POOL:
        yield os.path.join(agents, slug + ".md"), render(template, slug, full)
    with open(reviewer_source, encoding="utf-8", newline="") as f:
        fm, body = split_source(f.read().replace("\r\n", "\n"))
    for slug, full in REVIEWER_POOL:
        yield (os.path.join(agents, slug + ".md"),
               render_reviewer(os.path.basename(reviewer_source), fm, body, slug, full))


def parse_args(argv):
    """(check, root). Unknown arguments are ignored, as before --root existed."""
    check = "--check" in argv
    root = DEFAULT_ROOT
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            if i + 1 >= len(argv):
                raise SystemExit("gen_executors.py: --root needs a directory")
            root = argv[i + 1]
            i += 1
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
        i += 1
    return check, os.path.abspath(os.path.expanduser(root))


def main():
    check, root = parse_args(sys.argv[1:])
    stale = []
    for path, data in targets(root):
        if check:
            try:
                with open(path, encoding="utf-8", newline="") as f:
                    current = f.read()
            except FileNotFoundError:
                current = None
            if current != data:
                stale.append(path)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
            print("wrote", path)
    if check:
        for p in stale:
            print("stale:", p)
        sys.exit(1 if stale else 0)


if __name__ == "__main__":
    main()
