"""listwarden command line."""
from __future__ import annotations

import argparse
import base64
import difflib
import os
import re
import sys
from datetime import datetime, timezone

from . import __version__, report
from .audit import FIXABLE, apply_fixes, audit_text
from .net import GitHub, GitHubError, default_transport, find_token
from .prcheck import PRChecker, split_diff, upsert_comment

REPO_RE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)/?$")


def _now(args):
    v = os.environ.get("LISTWARDEN_NOW")
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else datetime.now(timezone.utc)


def load_list(target: str, gh: GitHub):
    """A local file path, '-' for stdin, or owner/repo[:path] to read a list from GitHub."""
    if target == "-":
        return "<stdin>", sys.stdin.read(), True
    if os.path.exists(target):
        with open(target, encoding="utf-8") as fh:
            return target, fh.read(), True
    path = None
    if ":" in target and not target.startswith("http"):
        target, path = target.split(":", 1)
    m = REPO_RE.match(target)
    if not m:
        raise SystemExit(f"listwarden: {target}: no such file, and not owner/repo")
    repo = m.group(1)
    js = gh.get(f"/repos/{repo}/contents/{path}") if path else gh.get(f"/repos/{repo}/readme")
    if not js or "content" not in js:
        raise SystemExit(f"listwarden: could not read {'%s from ' % path if path else 'README of '}{repo}")
    return f"{repo}:{js.get('path', path)}", base64.b64decode(js["content"]).decode("utf-8", "replace"), False


def make_gh() -> GitHub:
    return GitHub(token=find_token(), transport=default_transport())


def cmd_audit(args) -> int:
    gh = make_gh()
    name, text, _ = load_list(args.target, gh)
    res = audit_text(text, gh, path=name, stale_months=args.stale_months, check_web=not args.no_web,
                     now=_now(args), ignore=args.ignore)
    if args.format == "json":
        print(report.audit_json(res))
    elif args.format == "markdown":
        print(report.audit_markdown(res))
    else:
        print(report.audit_text(res, report.Color(report.use_color(args.color)), limit=args.limit))
    report.write_step_summary(report.audit_markdown(res))
    fail = set()
    if args.fail_on == "any":
        fail = {"dead", "archived", "duplicate", "renamed", "moved", "stale", "error"}
    elif args.fail_on != "none":
        fail = set(args.fail_on.split(","))
    return 1 if any(f.kind in fail for f in res.findings) else 0


def cmd_fix(args) -> int:
    gh = make_gh()
    name, text, local = load_list(args.target, gh)
    prune = [p for p in (args.prune or "").split(",") if p]
    bad = set(prune) - {"dead", "archived", "duplicate", "stale"}
    if bad:
        raise SystemExit(f"listwarden: --prune accepts dead,archived,duplicate,stale (got {','.join(sorted(bad))})")
    res = audit_text(text, gh, path=name, stale_months=args.stale_months, check_web=not args.no_web,
                     now=_now(args), ignore=args.ignore)
    new, changes = apply_fixes(text, res, prune=prune)
    if not changes:
        print("nothing to fix")
        return 0
    if args.write:
        if not local:
            raise SystemExit("listwarden: --write needs a local file")
        with open(args.target, "w", encoding="utf-8") as fh:
            fh.write(new)
        for ch in changes:
            print(ch)
        print(f"wrote {len(changes)} change{'s' if len(changes) != 1 else ''} to {args.target}")
    else:
        sys.stdout.writelines(difflib.unified_diff(text.splitlines(True), new.splitlines(True),
                                                   f"a/{os.path.basename(name)}", f"b/{os.path.basename(name)}", n=0))
        print(f"\n(dry run: {len(changes)} change{'s' if len(changes) != 1 else ''}; add --write to apply)")
    return 0


def cmd_pr(args) -> int:
    gh = make_gh()
    now = _now(args)
    chk = PRChecker(gh, min_stars=args.min_stars, min_age_days=args.min_age_days,
                    blast_threshold=args.blast_threshold, blast_days=args.blast_days, now=now,
                    transport=default_transport())
    if args.diff:
        if not (args.author and args.repo_name):
            raise SystemExit("listwarden: --diff needs --repo and --author")
        with open(args.diff) as fh:
            patches = split_diff(fh.read())

        def base_text(path):
            p = args.base or path
            return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        rep = chk.check(args.repo_name, args.author, patches, base_text)
    else:
        if not args.repo or not args.number:
            raise SystemExit("listwarden pr: give OWNER/REPO NUMBER (or a PR URL), or --diff")
        rep = chk.from_pr(args.repo, args.number)
    md = report.pr_markdown(rep, now)
    if args.format == "json":
        print(report.pr_json(rep))
    elif args.format == "markdown":
        print(md)
    else:
        print(report.pr_text(rep, report.Color(report.use_color(args.color)), now))
    report.write_step_summary(md)
    if args.comment and rep.number:
        url = upsert_comment(gh, rep.repo, rep.number, md)
        print(f"comment: {url}", file=sys.stderr)
    order = {"none": 99, "fail": 2, "warn": 1}
    lvl = {"info": 0, "warn": 1, "fail": 2}[rep.worst]
    return 1 if lvl >= order[args.fail_on] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="listwarden",
                                description="Keep awesome lists awesome: find dead, archived, renamed and stale entries, "
                                            "and check the links a PR adds (and who's adding them).")
    p.add_argument("--version", action="version", version=f"listwarden {__version__}")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("target", nargs="?", default="README.md",
                        help="list file, '-', or owner/repo[:path] on GitHub (default README.md)")
        sp.add_argument("--stale-months", type=float, default=24, help="flag repos with no commits for this long (24)")
        sp.add_argument("--no-web", action="store_true", help="only check GitHub repos, skip other links")
        sp.add_argument("--ignore", action="append", default=[], metavar="SUBSTR", help="skip URLs containing this")

    a = sub.add_parser("audit", help="report dead, archived, renamed, moved, stale and duplicate entries")
    common(a)
    a.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    a.add_argument("--limit", type=int, default=40, help="max findings in text output (0 = all)")
    a.add_argument("--fail-on", default="dead", help="comma list of kinds that make exit 1, 'any' or 'none' (dead)")
    a.set_defaults(func=cmd_audit)

    f = sub.add_parser("fix", help="rewrite renamed/redirected URLs; optionally drop dead/archived entries")
    common(f)
    f.add_argument("--prune", help="also remove entries that are: dead,archived,duplicate,stale")
    f.add_argument("--write", action="store_true", help="apply changes (default: show a diff)")
    f.set_defaults(func=cmd_fix)

    r = sub.add_parser("pr", help="check links added by a pull request, and the submitter's PR activity")
    r.add_argument("repo", nargs="?", help="OWNER/REPO of the list, or a PR URL")
    r.add_argument("number", nargs="?", type=int)
    r.add_argument("--diff", help="check a local unified diff instead of a GitHub PR")
    r.add_argument("--repo", dest="repo_name", help="(with --diff) the list's OWNER/REPO")
    r.add_argument("--author", help="(with --diff) GitHub login of the submitter")
    r.add_argument("--base", help="(with --diff) the list file before the change, for duplicate checks")
    r.add_argument("--min-stars", type=int, default=10)
    r.add_argument("--min-age-days", type=int, default=30)
    r.add_argument("--blast-threshold", type=int, default=10,
                   help="warn when the submitter opened PRs to this many other awesome lists recently (10)")
    r.add_argument("--blast-days", type=int, default=7)
    r.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    r.add_argument("--comment", action="store_true", help="post/update one sticky comment on the PR")
    r.add_argument("--fail-on", choices=["fail", "warn", "none"], default="fail")
    r.set_defaults(func=cmd_pr)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "pr" and args.repo and args.repo.startswith("https://github.com/"):
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", args.repo)
        if not m:
            raise SystemExit("listwarden: not a PR URL")
        args.repo, args.number = m.group(1), int(m.group(2))
    try:
        return args.func(args)
    except GitHubError as e:
        print(f"listwarden: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
