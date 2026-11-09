"""Audit an awesome list: dead, archived, renamed/moved, stale and duplicate entries."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .net import GitHub, LinkResult, check_links
from .parse import Entry, github_repo, parse_markdown

SEVERITY = {"dead": 3, "archived": 2, "duplicate": 2, "renamed": 1, "moved": 1, "stale": 1,
            "fork": 0, "blocked": 0, "unreachable": 0, "error": 0}
FIXABLE = {"renamed", "moved"}


@dataclass
class Finding:
    entry: Entry
    kind: str                  # dead | archived | renamed | moved | stale | duplicate | fork | blocked | unreachable | error
    detail: str
    fix_url: Optional[str] = None

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 0)


@dataclass
class AuditResult:
    path: str
    entries: List[Entry]
    findings: List[Finding] = field(default_factory=list)
    repo_info: Dict[str, Optional[dict]] = field(default_factory=dict)
    links: Dict[str, LinkResult] = field(default_factory=dict)
    skipped_web: bool = False
    now: Optional[datetime] = None

    def by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out


def parse_time(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def months_between(a: datetime, b: datetime) -> float:
    return (b - a).days / 30.44


def human_age(dt: Optional[datetime], now: datetime) -> str:
    if not dt:
        return "?"
    days = (now - dt).days
    if days < 60:
        return f"{days}d"
    if days < 730:
        return f"{round(days / 30.44)}mo"
    return f"{days / 365.25:.1f}y"


def audit_text(text: str, gh: GitHub, path: str = "README.md", stale_months: float = 24,
               check_web: bool = True, transport=None, now: Optional[datetime] = None,
               ignore: Optional[List[str]] = None) -> AuditResult:
    now = now or datetime.now(timezone.utc)
    ignore = [i.lower() for i in (ignore or [])]
    entries = [e for e in parse_markdown(text) if not any(i in e.url.lower() for i in ignore)]
    res = AuditResult(path=path, entries=entries, now=now, skipped_web=not check_web)

    # duplicates (same repo or same normalized URL listed twice)
    first: Dict[str, Entry] = {}
    for e in entries:
        if e.key in first:
            res.findings.append(Finding(e, "duplicate", f"also listed on line {first[e.key].line}"))
        else:
            first[e.key] = e

    repos = [e.repo for e in entries if e.repo]
    res.repo_info = gh.repos(repos) if repos else {}
    for e in entries:
        if not e.repo:
            continue
        info = res.repo_info.get(e.repo.lower())
        if info is None:
            res.findings.append(Finding(e, "dead", "repository not found (deleted, private or never existed)"))
            continue
        if info.get("isDisabled"):
            res.findings.append(Finding(e, "dead", "repository disabled by GitHub"))
            continue
        if info.get("nameWithOwner") and info["nameWithOwner"].lower() != e.repo.lower():
            new = e.url.replace(e.repo, info["nameWithOwner"], 1)
            if new == e.url:  # case-insensitive replace
                i = e.url.lower().find(e.repo.lower())
                new = e.url[:i] + info["nameWithOwner"] + e.url[i + len(e.repo):]
            res.findings.append(Finding(e, "renamed", f"now {info['nameWithOwner']}", fix_url=new))
        if info.get("isArchived"):
            res.findings.append(Finding(e, "archived", f"archived, last commit {human_age(parse_time(info.get('lastCommit')), now)} ago"))
        else:
            last = parse_time(info.get("lastCommit"))
            if last and months_between(last, now) >= stale_months:
                res.findings.append(Finding(e, "stale", f"no commits for {human_age(last, now)}"))
        if info.get("isFork"):
            res.findings.append(Finding(e, "fork", "is a fork"))

    if check_web:
        web = [e for e in entries if not e.repo and not github_repo(e.url)]
        res.links = check_links([e.url for e in web], transport=transport)
        for e in web:
            lr = res.links[e.url]
            v = lr.verdict
            if v in ("ok", "skipped"):
                continue
            if v == "dead":
                why = f"HTTP {lr.status}" if lr.status else (lr.error or "no response")
                res.findings.append(Finding(e, "dead", why))
            elif v == "moved":
                res.findings.append(Finding(e, "moved", f"redirects to {lr.final_url}",
                                            fix_url=lr.final_url if lr.permanent else None))
            elif v == "blocked":
                res.findings.append(Finding(e, "blocked", f"HTTP {lr.status} (bot protection? check by hand)"))
            elif v == "unreachable":
                res.findings.append(Finding(e, "unreachable", lr.error or "timed out"))
            else:
                res.findings.append(Finding(e, "error", f"HTTP {lr.status} (server error, may be temporary)"))
    res.findings.sort(key=lambda f: (-f.severity, f.entry.line))
    return res


def apply_fixes(text: str, res: AuditResult, prune: Optional[List[str]] = None) -> (str, List[str]):
    """Rewrite renamed/moved URLs in place; optionally drop entries whose findings are in `prune`.

    Returns (new_text, change descriptions). Only the exact URL on the finding's line is touched.
    """
    prune = set(prune or [])
    lines = text.splitlines(keepends=True)
    changes: List[str] = []
    drop = set()
    for f in res.findings:
        if f.kind in prune:
            drop.add(f.entry.line)
    for f in res.findings:
        if f.kind in FIXABLE and f.fix_url and f.entry.line not in drop:
            i = f.entry.line - 1
            if f.entry.url in lines[i]:
                lines[i] = lines[i].replace(f.entry.url, f.fix_url, 1)
                changes.append(f"L{f.entry.line}: {f.entry.url} -> {f.fix_url}")
    for ln in sorted(drop):
        f = next(x for x in res.findings if x.entry.line == ln and x.kind in prune)
        changes.append(f"L{ln}: removed ({f.kind}) {f.entry.text or f.entry.url}")
    out = "".join(l for i, l in enumerate(lines, 1) if i not in drop)
    return out, changes
