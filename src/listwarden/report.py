"""Render audit and PR reports as colored text, Markdown or JSON."""
from __future__ import annotations

import json
import os
import sys
from typing import Dict

from .audit import AuditResult, human_age, parse_time
from .prcheck import MARKER, PRReport

ORDER = ["dead", "archived", "duplicate", "renamed", "moved", "stale", "error", "fork", "blocked", "unreachable"]
ICON = {"fail": "❌", "warn": "⚠️", "info": "ℹ️"}


class Color:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.on else s


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


KCOL = {"dead": "31", "archived": "31", "duplicate": "33", "renamed": "36", "moved": "36", "stale": "33",
        "error": "31", "fork": "2", "blocked": "2", "unreachable": "2"}


def audit_text(res: AuditResult, c: Color, limit: int = 0) -> str:
    out = []
    n_repo = sum(1 for e in res.entries if e.repo)
    out.append(c("1", f"{res.path}: {len(res.entries)} entries ({n_repo} GitHub repos, {len(res.entries) - n_repo} other links)"))
    shown = res.findings if not limit else res.findings[:limit]
    for f in shown:
        tag = c(KCOL.get(f.kind, "0"), f"{f.kind:<9}")
        label = f.entry.repo or f.entry.url
        fix = c("2", f"  → fix: {f.fix_url}") if f.fix_url else ""
        out.append(f"  L{f.entry.line:<5} {tag} {label[:60]:<60} {f.detail}{fix}")
    if limit and len(res.findings) > limit:
        out.append(c("2", f"  … {len(res.findings) - limit} more (use --limit 0 for all)"))
    out.append("")
    out.append(summary_line(res, c))
    return "\n".join(out)


def summary_line(res: AuditResult, c: Color) -> str:
    k = res.by_kind()
    parts = [f"{k[x]} {x}" for x in ORDER if k.get(x)]
    checked = [e for e in res.entries if e.repo or not res.skipped_web]
    lines = {e.line for e in checked}
    healthy = len(checked) - len({f.entry.line for f in res.findings
                                  if f.entry.line in lines and f.kind not in ("fork", "blocked", "unreachable", "error")})
    fixable = sum(1 for f in res.findings if f.fix_url)
    s = f"{healthy}/{len(checked)} {'checked ' if res.skipped_web else ''}entries healthy · " + (", ".join(parts) if parts else "no problems found")
    if fixable:
        s += f" · {fixable} auto-fixable (listwarden fix)"
    if res.skipped_web:
        s += " · web links not checked (--no-web)"
    return c("1", s)


def audit_markdown(res: AuditResult) -> str:
    k = res.by_kind()
    out = [f"### listwarden audit: `{res.path}`", "",
           f"{len(res.entries)} entries · " + (", ".join(f"**{k[x]}** {x}" for x in ORDER if k.get(x)) or "no problems found"), ""]
    if res.findings:
        out += ["| Line | Problem | Entry | Detail |", "|---:|---|---|---|"]
        for f in res.findings:
            label = (f.entry.repo or f.entry.url).replace("|", "\\|")
            det = f.detail.replace("|", "\\|") + (f" → `{f.fix_url}`" if f.fix_url else "")
            out.append(f"| {f.entry.line} | {f.kind} | {label} | {det} |")
    return "\n".join(out) + "\n"


def audit_json(res: AuditResult) -> str:
    return json.dumps({
        "path": res.path, "entries": len(res.entries), "summary": res.by_kind(),
        "findings": [{"line": f.entry.line, "kind": f.kind, "url": f.entry.url, "repo": f.entry.repo,
                      "section": f.entry.section, "detail": f.detail, "fix_url": f.fix_url} for f in res.findings],
    }, indent=2)


# ---------------------------------------------------------------- PR

def _repo_facts(l, now) -> str:
    i = l.info or {}
    if not l.repo:
        return f"web link, {l.status or 'unchecked'}"
    if not i:
        return "not found"
    bits = [f"★ {i.get('stars', 0)}", f"created {human_age(parse_time(i.get('createdAt')), now)} ago",
            f"last commit {human_age(parse_time(i.get('lastCommit')), now)} ago"]
    if i.get("commits") is not None:
        bits.append(f"{i['commits']} commits")
    if l.contributors is not None:
        bits.append(f"{l.contributors} contributor{'s' if l.contributors != 1 else ''}")
    bits.append(i.get("license") or "no license")
    return " · ".join(bits)


def pr_text(rep: PRReport, c: Color, now) -> str:
    col = {"fail": "31", "warn": "33", "info": "36"}
    head = f"{rep.repo}#{rep.number}" if rep.number else rep.repo
    out = [c("1", f"{head}  by @{rep.author}") + (f"  “{rep.title}”" if rep.title else "")]
    if not rep.links:
        out.append("  no new external links added")
    for l in rep.links:
        out.append(f"  + {c('1', l.repo or l.url)}  ({l.file})")
        out.append(c("2", f"      {_repo_facts(l, now)}"))
        for s in l.signals:
            out.append(f"      {c(col[s.level], s.level.upper()):<5} {s.text}")
    f = rep.author_facts
    out.append(f"  @{rep.author}: account {f.get('account_age')} old · {f.get('prs_window', 0)} PRs in last "
               f"{f.get('window_days')}d, to {f.get('awesome_repos_window', 0)} other awesome lists")
    for s in rep.author_signals:
        out.append(f"      {c(col[s.level], s.level.upper()):<5} {s.text}")
    out.append("")
    out.append(c("1", f"result: {rep.worst.upper()}"))
    return "\n".join(out)


def pr_markdown(rep: PRReport, now) -> str:
    out = [MARKER, f"### listwarden: links added in this PR", ""]
    if not rep.links:
        out.append("No new external links added.")
    for l in rep.links:
        out.append(f"**{l.repo or l.url}** (`{l.file}`)  ")
        out.append(f"{_repo_facts(l, now)}")
        for s in l.signals:
            out.append(f"- {ICON[s.level]} {s.text}")
        out.append("")
    f = rep.author_facts
    out.append(f"**@{rep.author}**: account {f.get('account_age')} old · {f.get('prs_window', 0)} PRs opened in the last "
               f"{f.get('window_days')} days, to {f.get('awesome_repos_window', 0)} other awesome lists")
    for s in rep.author_signals:
        out.append(f"- {ICON[s.level]} {s.text}")
    out += ["", "<sub>Automated facts from the GitHub API, not a judgment. Maintainers decide. "
            "Generated by [listwarden](https://github.com/hamarshehmhmd/listwarden).</sub>"]
    return "\n".join(out) + "\n"


def pr_json(rep: PRReport) -> str:
    return json.dumps({
        "repo": rep.repo, "number": rep.number, "author": rep.author, "result": rep.worst,
        "links": [{"url": l.url, "file": l.file, "repo": l.repo, "info": l.info, "contributors": l.contributors,
                   "web_status": l.status, "signals": [s.__dict__ for s in l.signals]} for l in rep.links],
        "author_facts": rep.author_facts, "author_signals": [s.__dict__ for s in rep.author_signals],
    }, indent=2, default=str)


def write_step_summary(md: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as fh:
            fh.write(md + "\n")
