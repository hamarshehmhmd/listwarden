from listwarden.net import GitHub, LinkResult, check_link


def tape(routes):
    calls = []

    def t(method, url, headers, body):
        calls.append((method, url))
        r = routes.get((method, url)) or routes.get(("*", url))
        if r is None:
            return 0, {"x-error": "Name or service not known"}, b""
        return r[0], r[1] if len(r) > 1 else {}, b""
    t.calls = calls
    return t


def test_head_fallback_to_get():
    t = tape({("HEAD", "https://a.io/"): (405,), ("GET", "https://a.io/"): (200,)})
    assert check_link("https://a.io/", t).verdict == "ok"


def test_dns_failure_is_dead():
    t = tape({})
    assert check_link("https://nope.invalid/", t).verdict == "dead"


def test_http_falls_back_to_https():
    t = tape({("*", "http://a.io/"): (500,), ("GET", "https://a.io/"): (200,), ("HEAD", "https://a.io/"): (200,)})
    r = check_link("http://a.io/", t)
    assert r.verdict == "ok"


def test_temporary_redirect_not_fixable():
    t = tape({("HEAD", "https://a.io/"): (302, {"location": "https://b.io/x"}), ("HEAD", "https://b.io/x"): (200,)})
    r = check_link("https://a.io/", t)
    assert r.verdict == "moved" and not r.permanent


def test_relative_redirect_and_same_site():
    t = tape({("HEAD", "https://a.io/x"): (301, {"location": "/y"}), ("HEAD", "https://a.io/y"): (200,)})
    r = check_link("https://a.io/x", t)
    assert r.final_url == "https://a.io/y" and r.verdict == "ok"
    t = tape({("HEAD", "https://a.io/"): (301, {"location": "https://docs.a.io/"}), ("HEAD", "https://docs.a.io/"): (200,)})
    assert check_link("https://a.io/", t).verdict == "ok"


def test_verdicts():
    assert LinkResult("u", 403, "u", True).verdict == "blocked"
    assert LinkResult("u", 410, "u", True).verdict == "dead"
    assert LinkResult("u", 502, "u", True).verdict == "error"
    assert LinkResult("u", -1, "u", True).verdict == "skipped"


def test_count_uses_link_header():
    def t(m, url, h, b):
        return 200, {"link": '<https://api.github.com/x?per_page=1&page=2>; rel="next", <https://api.github.com/x?per_page=1&page=31>; rel="last"'}, b"[{}]"
    assert GitHub(token="x", transport=t).count("/repos/a/b/contributors") == 31


def test_rest_fallback_follows_rename_without_token():
    import json

    def t(m, url, h, b):
        assert "Authorization" not in h
        if url.endswith("/repos/zeit/next.js"):
            return 301, {}, json.dumps({"url": "https://api.github.com/repositories/1"}).encode()
        return 200, {}, json.dumps({"full_name": "vercel/next.js", "archived": False, "stargazers_count": 5,
                                    "pushed_at": "2026-01-01T00:00:00Z"}).encode()
    out = GitHub(token=None, transport=t).repos(["zeit/next.js"])
    assert out["zeit/next.js"]["nameWithOwner"] == "vercel/next.js"
