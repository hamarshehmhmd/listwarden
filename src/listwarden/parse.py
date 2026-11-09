"""Extract list entries (links) from a Markdown awesome list."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit

LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<url><[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")
BARE_RE = re.compile(r"<(?P<url>https?://[^>\s]+)>")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
TABLE_RE = re.compile(r"^\s*\|")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

# github.com/<x> paths that are not repositories
GH_RESERVED = {
    "about", "apps", "orgs", "topics", "sponsors", "features", "marketplace", "collections",
    "settings", "notifications", "explore", "trending", "login", "join", "pricing", "site",
    "security", "enterprise", "readme", "customer-stories", "events", "search", "issues",
    "pulls", "codespaces", "new", "contact", "team", "github-copilot", "users",
}


@dataclass
class Entry:
    line: int            # 1-based line number
    text: str            # link text
    url: str             # url as written
    section: str         # nearest heading
    raw: str             # the full line
    repo: Optional[str] = None      # "owner/name" when it's a GitHub repo link
    tags: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.repo.lower() if self.repo else normalize_url(self.url)


def normalize_url(url: str) -> str:
    s = urlsplit(url.strip())
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = s.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}{path}".lower() + (f"?{s.query}" if s.query else "")


def github_repo(url: str) -> Optional[str]:
    """Return 'owner/name' for a github.com repository URL (incl. /tree/..., /blob/...), else None."""
    s = urlsplit(url.strip())
    host = (s.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in s.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() in GH_RESERVED:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name):
        return None
    return f"{owner}/{name}"


def extract_links(line: str):
    """Yield (text, url) for inline links and <bare> links in a line, skipping images."""
    seen = []
    for m in LINK_RE.finditer(line):
        url = m.group("url").strip("<>")
        seen.append((m.group("text"), url))
    for m in BARE_RE.finditer(line):
        if not any(m.group("url") == u for _, u in seen):
            seen.append((m.group("url"), m.group("url")))
    return seen


def is_external(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def parse_markdown(text: str, list_items_only: bool = True) -> List[Entry]:
    """Return one Entry per list item, using its first external link (the item's subject).

    Badge images, in-page anchors, relative links and links inside code fences are ignored.
    Table rows count as items too (many lists use tables).
    """
    entries: List[Entry] = []
    section = ""
    in_fence = False
    for i, raw in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        h = HEADING_RE.match(raw)
        if h:
            section = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", h.group(2)).strip()
            continue
        if list_items_only and not (LIST_RE.match(raw) or TABLE_RE.match(raw)):
            continue
        for ltext, url in extract_links(raw):
            if not is_external(url):
                continue
            entries.append(Entry(line=i, text=ltext.strip(), url=url, section=section, raw=raw,
                                 repo=github_repo(url)))
            break  # one subject link per item
    return entries


