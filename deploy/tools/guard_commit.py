"""Pre-commit guard: refuse a commit that would publish what must stay local.

    deploy/tools/install_hooks.sh          # once per checkout: installs the hook
    python deploy/tools/guard_commit.py          # what the hook runs (staged files)
    python deploy/tools/guard_commit.py --all    # every tracked file instead

This repository is public. The checks, on each staged file's staged content:

  1. private patterns   every regex in .git/company_patterns.txt and in
                        .git/scan_private.json, when this machine has them
                        (they name a private test set and are never
                        committed); matched against the content and the path;
  2. local-only paths   data/ (except data/test_holdout/), handoff/, reports/,
                        ui/.inbox/, ui/.cache/, ui/.local/, deploy/space/,
                        deploy/bundle/, .venv/, tools/;
  3. personal traces    a home-directory path, or an e-mail address other
                        than a no-reply or example address;
  4. language           the repository is English-only: no CJK text, except
                        in the few files that keep Chinese program keys on
                        purpose (ALLOW_CJK).

Only counts and file names are printed, never the matched text.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

LOCAL_ONLY = ("data/", "handoff/", "reports/", "ui/.inbox/", "ui/.cache/",
              "ui/.local/", "deploy/space/", "deploy/bundle/", ".venv/", "tools/")
LOCAL_ONLY_EXCEPT = ("data/test_holdout/",)
# Chinese strings kept as program keys (bootstrap seeds, log actions parsed by
# code, basket and variant names): changing them would change results.
ALLOW_CJK = {"src/holdout_eval.py", "src/index_weighting.py",
             "src/competition_and_crosswalk.py", "src/parse_sanantonio_manual.py"}
CJK = re.compile("[\\u3000-\\u303f\\u3400-\\u9fff\\uff00-\\uffef]")
HOME = re.compile("/" + "Users/[^/\\s\"'<>]|(?<![A-Za-z0-9._-])/" + "home/[a-z_][a-z0-9_-]*/")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"@(users\.noreply\.github\.com|github\.com$|example\.(com|org|net)|noreply\.|anthropic\.com$)|^noreply@|^git@", re.I)
TEXT = (".py", ".md", ".txt", ".json", ".js", ".html", ".css", ".sh", ".csv",
        ".yml", ".yaml", ".toml", ".cfg", ".ini", ".ps1", "")


def git(*args, binary=False):
    r = subprocess.run(["git"] + list(args), capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def private_patterns(root):
    pats = []
    p = os.path.join(root, ".git", "company_patterns.txt")
    if os.path.exists(p):
        pats += [l.strip() for l in open(p, encoding="utf-8") if l.strip()]
    p = os.path.join(root, ".git", "scan_private.json")
    if os.path.exists(p):
        cfg = json.load(open(p, encoding="utf-8"))
        for k in ("employer", "personal"):
            pats += cfg.get(k) or []
    return re.compile("|".join("(?:%s)" % x for x in pats), re.I) if pats else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = git("rev-parse", "--show-toplevel").strip()
    everything = "--all" in argv
    if everything:
        files = git("ls-files").splitlines()
    else:
        files = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    rx = private_patterns(root)
    problems = {}

    def flag(f, what):
        problems.setdefault(f, [])
        if what not in problems[f]:
            problems[f].append(what)

    for f in files:
        if f.startswith(LOCAL_ONLY) and not f.startswith(LOCAL_ONLY_EXCEPT):
            flag(f, "local-only path")
        if rx is not None and rx.search(f):
            flag(f, "private pattern in the file name")
        blob = (open(os.path.join(root, f), "rb").read() if everything
                else git("show", ":" + f, binary=True))
        text = blob.decode("utf-8", "ignore")
        if rx is not None and rx.search(text):
            flag(f, "private pattern")
        ext = os.path.splitext(f)[1].lower()
        if ext not in TEXT:
            continue                      # binary: patterns only
        if HOME.search(text):
            flag(f, "home-directory path")
        if any(not EMAIL_OK.search(m.group(0)) for m in EMAIL.finditer(text)):
            flag(f, "e-mail address")
        if f not in ALLOW_CJK and CJK.search(text):
            flag(f, "CJK text (the repository is English-only)")
    if problems:
        print("BLOCKED: %d file(s) must not be committed as they are:" % len(problems),
              file=sys.stderr)
        for f in sorted(problems):
            print("  %s: %s" % (f, "; ".join(problems[f])), file=sys.stderr)
        return 1
    if everything:
        print("guard: %d tracked files, nothing to block%s" % (
            len(files), "" if rx is not None else
            " (no private pattern files on this machine)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
