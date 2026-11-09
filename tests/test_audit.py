from listwarden.audit import apply_fixes, audit_text
from listwarden.net import GitHub, default_transport

from conftest import NOW


def run(list_md, **kw):
    gh = GitHub(token="cassette", transport=default_transport())
    return audit_text(list_md, gh, now=NOW, transport=default_transport(), **kw), gh


def kinds(res):
    return {(f.entry.repo or f.entry.url, f.kind) for f in res.findings}


def test_audit_finds_each_problem(cassette, list_md):
    res, gh = run(list_md)
    k = kinds(res)
    assert ("acme/oldlib", "archived") in k
    assert ("zeit/widgets", "renamed") in k
    assert ("ghost/gone", "dead") in k
    assert ("acme/sleepy", "stale") in k
    assert ("acme/sleepy", "fork") in k
    assert ("ACME/goodlib", "duplicate") in k
    assert ("https://blog.example.net/post", "dead") in k
    assert ("http://old.example.org/docs", "moved") in k
    assert ("https://shy.example.io/", "blocked") in k
    # healthy ones produce nothing; login-walled sites are skipped, not "dead"
    assert not any(r == "acme/goodlib" for r, _ in k)
    assert not any("twitter.com" in r for r, _ in k)
    assert not any("widgets.example.com" in r for r, _ in k)
    assert gh.calls == 1  # all repos in one GraphQL batch


def test_findings_sorted_by_severity(cassette, list_md):
    res, _ = run(list_md)
    sev = [f.severity for f in res.findings]
    assert sev == sorted(sev, reverse=True)
    assert res.findings[0].kind == "dead"


def test_stale_threshold(cassette, list_md):
    res, _ = run(list_md, stale_months=48)
    assert not any(f.kind == "stale" for f in res.findings)


def test_no_web(cassette, list_md):
    res, _ = run(list_md, check_web=False)
    assert all(f.entry.repo for f in res.findings if f.kind != "duplicate")
    assert res.skipped_web


def test_ignore(cassette, list_md):
    res, _ = run(list_md, ignore=["blog.example.net"])
    assert not any("blog.example.net" in f.entry.url for f in res.findings)


def test_fix_rewrites_renamed_and_moved(cassette, list_md):
    res, _ = run(list_md)
    new, changes = apply_fixes(list_md, res)
    assert "https://github.com/vercel/widgets" in new
    assert "https://docs.example.com/" in new
    assert "zeit/widgets" not in new
    assert len(changes) == 2
    # nothing else touched
    assert len(new.splitlines()) == len(list_md.splitlines())


def test_fix_prune(cassette, list_md):
    res, _ = run(list_md)
    new, changes = apply_fixes(list_md, res, prune=["dead", "archived"])
    assert "ghost/gone" not in new and "acme/oldlib" not in new and "blog.example.net" not in new
    assert "acme/goodlib" in new
    assert len(list_md.splitlines()) - len(new.splitlines()) == 3
