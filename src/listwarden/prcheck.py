"""Check the links a pull request adds to an awesome list, and who is adding them.

Facts, not verdicts: target repo age, stars, activity, whether it's already listed, whether the
submitter owns it, and how many awesome-list PRs the submitter opened this week.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .audit import human_age, parse_time
from .net import GitHub, check_link
from .parse import extract_links, github_repo, is_external, normalize_url, parse_markdown

MARKER = "<!-- listwarden:pr-check -->"
LEVELS = {"fail": 2, "warn": 1, "info": 0}


@dataclass
class Signal:
    level: str     # fail | warn | info
    text: str


@dataclass
class AddedLink:
    file: str
    url: str
    text: str
    repo: Optional[str] = None
    info: Optional[dict] = None
    contributors: Optional[int] = None
    status: Optional[str] = None       # web link verdict
    signals: List[Signal] = field(default_factory=list)


@dataclass
class PRReport:
    repo: str
    number: Optional[int]
    author: str
    title: str = ""
    links: List[AddedLink] = field(default_factory=list)
    author_signals: List[Signal] = field(default_factory=list)
    author_facts: Dict[str, object] = field(default_factory=dict)

    @property
    def worst(self) -> str:
        allsig = self.author_signals + [s for l in self.links for s in l.signals]
        if not allsig:
            return "info"
        return max(allsig, key=lambda s: LEVELS[s.level]).level


def added_links_from_patch(patch: str) -> List[Tuple[str, str]]:
    """(text, url) for external links on added lines, minus links that were only moved."""
    added, removed = [], set()
    for line in (patch or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            for t, u in extract_links(line[1:]):
                if is_external(u):
                    added.append((t, u))
                    break
        elif line.startswith("-"):
            for _, u in extract_links(line[1:]):
                removed.add(normalize_url(u))
    return [(t, u) for t, u in added if normalize_url(u) not in removed]


def split_diff(diff: str) -> Dict[str, str]:
    """Split a unified git diff into {path: patch}."""
    files: Dict[str, List[str]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur = line.split(" b/", 1)[-1]
            files[cur] = []
        elif line.startswith("+++ b/"):
            cur = line[6:]
            files.setdefault(cur, [])
        elif cur is not None:
            files[cur].append(line)
    return {k: "\n".join(v) for k, v in files.items()}


class PRChecker:
    def __init__(self, gh: GitHub, min_stars: int = 10, min_age_days: int = 30,
                 blast_threshold: int = 10, blast_days: int = 7, now: Optional[datetime] = None,
                 transport=None):
        self.gh = gh
        self.min_stars = min_stars
        self.min_age_days = min_age_days
        self.blast_threshold = blast_threshold
        self.blast_days = blast_days
        self.now = now or datetime.now(timezone.utc)
        self.transport = transport

    # ------------------------------------------------------------ inputs
    def from_pr(self, repo: str, number: int) -> PRReport:
        pr = self.gh.get(f"/repos/{repo}/pulls/{number}")
        if not pr:
            raise SystemExit(f"listwarden: PR {repo}#{number} not found")
        author = pr["user"]["login"]
        base_sha = pr["base"]["sha"]
        files = []
        page = 1
        while True:
            chunk = self.gh.get(f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
            files += chunk
            if len(chunk) < 100 or page >= 30:
                break
            page += 1
        patches = {f["filename"]: f.get("patch", "") for f in files
                   if f["filename"].lower().endswith((".md", ".markdown", ".mdx")) and f.get("status") != "removed"}

        def base_text(path):
            js = self.gh.get(f"/repos/{repo}/contents/{quote(path)}?ref={base_sha}")
            if js and js.get("encoding") == "base64":
                return base64.b64decode(js["content"]).decode("utf-8", "replace")
            return ""
        rep = self.check(repo, author, patches, base_text, number=number)
        rep.title = pr.get("title", "")
        return rep

    def check(self, repo: str, author: str, patches: Dict[str, str], base_text, number: Optional[int] = None) -> PRReport:
        rep = PRReport(repo=repo, number=number, author=author)
        for path, patch in patches.items():
            added = added_links_from_patch(patch)
            if not added:
                continue
            existing = {e.key: e for e in parse_markdown(base_text(path) or "")}
            for text, url in added:
                al = AddedLink(file=path, url=url, text=text, repo=github_repo(url))
                key = al.repo.lower() if al.repo else normalize_url(url)
                if key in existing:
                    al.signals.append(Signal("fail", f"already listed in {path} (line {existing[key].line})"))
                rep.links.append(al)
        self._repos(rep)
        self._web(rep)
        self._author(rep)
        return rep

    # ------------------------------------------------------------ checks
    def _repos(self, rep: PRReport):
        names = [l.repo for l in rep.links if l.repo]
        infos = self.gh.repos(names) if names else {}
        for l in rep.links:
            if not l.repo:
                continue
            info = infos.get(l.repo.lower())
            l.info = info
            if info is None:
                l.signals.append(Signal("fail", "repository not found"))
                continue
            if info.get("isArchived"):
                l.signals.append(Signal("fail", "repository is archived"))
            created = parse_time(info.get("createdAt"))
            age = (self.now - created).days if created else None
            if age is not None and age < self.min_age_days:
                l.signals.append(Signal("warn", f"repo created {age} days ago (< {self.min_age_days})"))
            if self.min_stars and info.get("stars", 0) < self.min_stars:
                l.signals.append(Signal("warn", f"{info.get('stars', 0)} stars (< {self.min_stars})"))
            full = info.get("nameWithOwner") or l.repo
            l.contributors = self.gh.count(f"/repos/{full}/contributors?anon=1")
            owner = (info.get("owner") or full.split("/")[0]).lower()
            if owner == rep.author.lower():
                l.signals.append(Signal("info", "submitted by the repo owner (self-promotion is fine if disclosed)"))
            else:
                contribs = self.gh.get(f"/repos/{full}/contributors?per_page=100") or []
                if any((c.get("login") or "").lower() == rep.author.lower() for c in contribs if isinstance(c, dict)):
                    l.signals.append(Signal("info", "submitter is a contributor to this repo"))
            if info.get("license") in (None, "NOASSERTION"):
                l.signals.append(Signal("info", "no license detected"))

    def _web(self, rep: PRReport):
        for l in rep.links:
            if l.repo:
                continue
            lr = check_link(l.url, transport=self.transport)
            l.status = lr.verdict
            if lr.verdict == "dead":
                l.signals.append(Signal("fail", f"link is dead ({'HTTP %d' % lr.status if lr.status else lr.error})"))
            elif lr.verdict == "moved":
                l.signals.append(Signal("warn", f"redirects to {lr.final_url}"))
            elif lr.verdict in ("blocked", "unreachable", "error", "skipped"):
                l.signals.append(Signal("info", f"could not verify link ({lr.verdict}, HTTP {lr.status})"))

    def _author(self, rep: PRReport):
        user = self.gh.get(f"/users/{rep.author}") or {}
        created = parse_time(user.get("created_at"))
        rep.author_facts["account_age"] = human_age(created, self.now) if created else "?"
        rep.author_facts["public_repos"] = user.get("public_repos")
        if created and (self.now - created).days < 30:
            rep.author_signals.append(Signal("warn", f"account created {(self.now - created).days} days ago"))
        since = (self.now - timedelta(days=self.blast_days)).strftime("%Y-%m-%d")
        q = quote(f"is:pr author:{rep.author} created:>={since}")
        js = self.gh.get(f"/search/issues?q={q}&per_page=100") or {}
        items = js.get("items", [])
        repos = {}
        for it in items:
            r = it.get("repository_url", "").split("/repos/")[-1]
            repos[r] = repos.get(r, 0) + 1
        awesome = sorted(r for r in repos if "awesome" in r.lower() and r.lower() != rep.repo.lower())
        rep.author_facts["prs_window"] = js.get("total_count", len(items))
        rep.author_facts["awesome_repos_window"] = len(awesome)
        rep.author_facts["window_days"] = self.blast_days
        rep.author_facts["awesome_sample"] = awesome[:8]
        if len(awesome) >= self.blast_threshold:
            rep.author_signals.append(Signal(
                "warn", f"opened PRs to {len(awesome)} other awesome lists in the last {self.blast_days} days"))
        # how many times was each target pitched by this author?
        for l in rep.links:
            target = (l.info or {}).get("nameWithOwner") or l.repo or normalize_url(l.url).split("/")[0]
            q2 = quote(f'is:pr author:{rep.author} "{target}"')
            js2 = self.gh.get(f"/search/issues?q={q2}&per_page=1") or {}
            n = js2.get("total_count", 0)
            if n >= 5:
                l.signals.append(Signal("warn", f"{rep.author} mentioned {target} in {n} PRs across GitHub"))
            elif n > 1:
                l.signals.append(Signal("info", f"{rep.author} mentioned {target} in {n} PRs across GitHub"))


# ---------------------------------------------------------------- comments

def upsert_comment(gh: GitHub, repo: str, number: int, body: str) -> str:
    comments = gh.get(f"/repos/{repo}/issues/{number}/comments?per_page=100") or []
    mine = next((c for c in comments if MARKER in (c.get("body") or "")), None)
    if mine:
        _, js, _ = gh.rest("PATCH", f"/repos/{repo}/issues/comments/{mine['id']}", {"body": body}, ok=(200,))
    else:
        _, js, _ = gh.rest("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, ok=(201,))
    return js.get("html_url", "")
