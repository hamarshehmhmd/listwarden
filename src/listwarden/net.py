"""HTTP plumbing: a tiny transport (real, or replayed from a cassette), GitHub and web link checks."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__

UA = f"listwarden/{__version__} (+https://github.com/hamarshehmhmd/listwarden)"
Response = Tuple[int, Dict[str, str], bytes]
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # surface 3xx to the caller
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float = 12.0) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(2_000_000)
    except urllib.error.HTTPError as e:
        try:
            data = e.read(200_000)
        except Exception:
            data = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        return 0, {"x-error": str(reason)[:200]}, b""


def cassette_transport(path: str) -> Transport:
    """Replay responses from a JSON cassette: {"METHOD url": {"status":..,"headers":..,"json"|"body":..}}.

    GraphQL requests are matched on "POST https://api.github.com/graphql#<alias list>"; see tests.
    Used by the tests and the demo so they run offline and deterministically.
    """
    with open(path) as fh:
        tape = json.load(fh)

    def t(method, url, headers, body):
        key = f"{method} {url}"
        if url.endswith("/graphql") and body:
            q = json.loads(body)["query"]
            key = "POST graphql"
            data = {}
            errors = []
            for alias, owner, name in re.findall(r"\b(r\d+): repository\(owner: \"([^\"]+)\", name: \"([^\"]+)\"\)", q):
                rec = tape.get("repos", {}).get(f"{owner}/{name}".lower())
                data[alias] = rec
                if rec is None:
                    errors.append({"type": "NOT_FOUND", "path": [alias]})
            out = {"data": data}
            if errors:
                out["errors"] = errors
            return 200, {}, json.dumps(out).encode()
        rec = tape.get(key)
        if rec is None:
            # tolerate query-string order differences for search
            rec = next((v for k, v in tape.items() if isinstance(v, dict) and k.split("?")[0] == key.split("?")[0]
                        and k.startswith("GET https://api.github.com/search")), None) if "search" in url else None
        if rec is None:
            return 0, {"x-error": f"not in cassette: {key}"}, b""
        body_out = json.dumps(rec["json"]).encode() if "json" in rec else rec.get("body", "").encode()
        return rec.get("status", 200), {k.lower(): v for k, v in rec.get("headers", {}).items()}, body_out

    return t


def default_transport() -> Transport:
    path = os.environ.get("LISTWARDEN_CASSETTE")
    return cassette_transport(path) if path else http_transport


def find_token() -> Optional[str]:
    for k in ("LISTWARDEN_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    if os.environ.get("LISTWARDEN_CASSETTE"):
        return "cassette"
    if shutil.which("gh"):
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
    return None


class GitHubError(RuntimeError):
    pass


REPO_FIELDS = """nameWithOwner url isArchived isFork isDisabled isPrivate stargazerCount createdAt pushedAt
      description licenseInfo { spdxId } owner { login }
      defaultBranchRef { target { ... on Commit { committedDate history(first: 1) { totalCount } } } }"""


class GitHub:
    def __init__(self, token: Optional[str] = None, transport: Optional[Transport] = None,
                 api: str = "https://api.github.com"):
        self.token = token
        self.t = transport or default_transport()
        self.api = api.rstrip("/")
        self.calls = 0

    def _headers(self) -> Dict[str, str]:
        h = {"User-Agent": UA, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token and self.token != "cassette":
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def rest(self, method: str, path: str, payload: Optional[dict] = None, ok=(200,)) -> Tuple[int, dict, Dict[str, str]]:
        url = path if path.startswith("http") else self.api + path
        body = json.dumps(payload).encode() if payload is not None else None
        h = self._headers()
        if body:
            h["Content-Type"] = "application/json"
        self.calls += 1
        status, headers, data = self.t(method, url, h, body)
        try:
            js = json.loads(data) if data else {}
        except ValueError:
            js = {}
        if status not in ok and status not in (404,):
            raise GitHubError(f"GitHub {method} {path}: HTTP {status} {str(js.get('message', headers.get('x-error', '')))[:160]}")
        return status, js, headers

    def get(self, path: str):
        status, js, _ = self.rest("GET", path)
        return js if status == 200 else None

    def count(self, path: str) -> int:
        """Count items of a paginated list endpoint with per_page=1 via the Link header."""
        sep = "&" if "?" in path else "?"
        status, js, headers = self.rest("GET", f"{path}{sep}per_page=1")
        if status != 200:
            return 0
        m = re.search(r'[?&]page=(\d+)>; rel="last"', headers.get("link", ""))
        if m:
            return int(m.group(1))
        return len(js) if isinstance(js, list) else 0

    def repos(self, names: List[str], batch: int = 40) -> Dict[str, Optional[dict]]:
        """Look up many repos. GraphQL follows renames; NOT_FOUND -> None. Falls back to REST without a token."""
        out: Dict[str, Optional[dict]] = {}
        uniq = list(dict.fromkeys(n.lower() for n in names))
        if not self.token:
            for n in uniq:
                out[n] = self._rest_repo(n)
            return out
        for i in range(0, len(uniq), batch):
            chunk = uniq[i:i + batch]
            parts = []
            for j, n in enumerate(chunk):
                owner, name = n.split("/", 1)
                parts.append(f'r{j}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {REPO_FIELDS} }}')
            q = "query { " + "\n".join(parts) + " }"
            self.calls += 1
            status, headers, data = self.t("POST", self.api + "/graphql",
                                           dict(self._headers(), **{"Content-Type": "application/json"}),
                                           json.dumps({"query": q}).encode())
            if status != 200:
                raise GitHubError(f"GitHub GraphQL: HTTP {status} {headers.get('x-error', '')}")
            js = json.loads(data)
            data_ = js.get("data") or {}
            bad = [e for e in js.get("errors", []) if e.get("type") not in ("NOT_FOUND", "FORBIDDEN")]
            if bad and not data_:
                raise GitHubError(f"GitHub GraphQL error: {bad[0].get('message')}")
            for j, n in enumerate(chunk):
                out[n] = _flatten(data_.get(f"r{j}"))
        return out

    def _rest_repo(self, n: str) -> Optional[dict]:
        status, js, headers = self.rest("GET", f"/repos/{n}", ok=(200, 301))
        if status == 301 and js.get("url"):
            status, js, _ = self.rest("GET", js["url"])
        if status != 200:
            return None
        return {
            "nameWithOwner": js.get("full_name"), "url": js.get("html_url"), "isArchived": js.get("archived", False),
            "isFork": js.get("fork", False), "isDisabled": js.get("disabled", False), "stars": js.get("stargazers_count", 0),
            "createdAt": js.get("created_at"), "pushedAt": js.get("pushed_at"), "lastCommit": js.get("pushed_at"),
            "description": js.get("description"), "license": (js.get("license") or {}).get("spdx_id"),
            "owner": (js.get("owner") or {}).get("login"), "commits": None,
        }


def _flatten(r: Optional[dict]) -> Optional[dict]:
    if not r:
        return None
    tgt = ((r.get("defaultBranchRef") or {}).get("target") or {})
    return {
        "nameWithOwner": r.get("nameWithOwner"), "url": r.get("url"), "isArchived": r.get("isArchived", False),
        "isFork": r.get("isFork", False), "isDisabled": r.get("isDisabled", False), "stars": r.get("stargazerCount", 0),
        "createdAt": r.get("createdAt"), "pushedAt": r.get("pushedAt"),
        "lastCommit": tgt.get("committedDate") or r.get("pushedAt"),
        "commits": (tgt.get("history") or {}).get("totalCount"),
        "description": r.get("description"), "license": (r.get("licenseInfo") or {}).get("spdxId"),
        "owner": (r.get("owner") or {}).get("login"),
    }


# ---------------------------------------------------------------- web links

@dataclass
class LinkResult:
    url: str
    status: int                 # final HTTP status (0 = network error)
    final_url: str
    permanent: bool             # all redirects were 301/308
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.status == -1:
            return "skipped"
        if self.status == 0:
            return "dead" if _is_dns_error(self.error) else "unreachable"
        if self.status in (404, 410) or (self.status >= 500 and self.status != 503):
            return "dead" if self.status in (404, 410) else "error"
        if self.status in (401, 403, 429, 503, 999):
            return "blocked"
        if 200 <= self.status < 400:
            if _host(self.final_url) != _host(self.url) and not _same_site(self.url, self.final_url):
                return "moved"
            return "ok"
        return "error"


def _host(u: str) -> str:
    h = (urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _same_site(a: str, b: str) -> bool:
    """example.org -> docs.example.org, or a login/consent bounce, isn't worth rewriting."""
    ha, hb = _host(a), _host(b)
    base = lambda h: ".".join(h.split(".")[-2:])
    return base(ha) == base(hb) or hb.startswith(("accounts.", "login.", "consent.", "auth."))


def _is_dns_error(err: str) -> bool:
    e = err.lower()
    return any(s in e for s in ("name or service not known", "nodename nor servname", "no address associated",
                                "name resolution", "getaddrinfo failed", "certificate has expired"))


BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 listwarden"


# Sites that answer 404/999/login walls to anything that isn't a logged-in browser.
UNVERIFIABLE = {"twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "t.me", "medium.com",
                "quora.com", "reddit.com", "tiktok.com"}


def check_link(url: str, transport: Optional[Transport] = None, max_redirects: int = 8) -> LinkResult:
    t = transport or default_transport()
    host = _host(url)
    if host in UNVERIFIABLE or any(host.endswith("." + h) for h in UNVERIFIABLE):
        return LinkResult(url=url, status=-1, final_url=url, permanent=False, error="login-walled site, not checked")
    res = _check(url, t, max_redirects)
    if url.startswith("http://") and res.verdict != "ok":
        alt = _check("https://" + url[7:], t, max_redirects)
        if alt.verdict in ("ok", "moved"):
            return LinkResult(url=url, status=alt.status, final_url=alt.final_url, permanent=alt.permanent,
                              error=alt.error)
    return res


def _check(url: str, t: Transport, max_redirects: int) -> LinkResult:
    cur, permanent = url, True
    status, err = 0, ""
    for _ in range(max_redirects + 1):
        status, headers, _ = t("HEAD", cur, {"User-Agent": BROWSER_UA, "Accept": "*/*"}, None)
        if status in (400, 403, 405, 404, 429, 501) or status == 0 and "timed out" not in headers.get("x-error", ""):
            # many servers mishandle HEAD; confirm with GET
            s2, h2, _ = t("GET", cur, {"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"}, None)
            if s2 != 0 or status == 0:
                status, headers = s2, h2
        if cur.startswith("http://") and (status == 0 or status >= 500):
            s3, h3, _ = t("GET", "https://" + cur[7:], {"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"}, None)
            if s3 and s3 < 500:
                cur, status, headers = "https://" + cur[7:], s3, h3
        err = headers.get("x-error", "")
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            permanent = permanent and status in (301, 308)
            loc = headers["location"]
            if loc.startswith("/"):
                s = urlsplit(cur)
                loc = f"{s.scheme}://{s.netloc}{loc}"
            elif not loc.startswith("http"):
                loc = cur.rsplit("/", 1)[0] + "/" + loc
            cur = loc
            continue
        break
    return LinkResult(url=url, status=status, final_url=cur, permanent=permanent, error=err)


def check_links(urls: List[str], transport: Optional[Transport] = None, workers: int = 24,
                per_host: int = 4) -> Dict[str, LinkResult]:
    """Check links in parallel, at most `per_host` at a time per host (CRAN & co. rate-limit hard)."""
    import threading
    uniq = list(dict.fromkeys(urls))
    locks: Dict[str, threading.Semaphore] = {}
    guard = threading.Lock()

    def one(u):
        h = _host(u)
        with guard:
            sem = locks.setdefault(h, threading.Semaphore(per_host))
        with sem:
            return check_link(u, transport)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        res = list(ex.map(one, uniq))
    return dict(zip(uniq, res))
