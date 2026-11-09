import json

from listwarden.net import GitHub, default_transport
from listwarden.prcheck import PRChecker, added_links_from_patch, split_diff
from listwarden.report import pr_markdown

from conftest import NOW


def test_added_links_ignore_moved_lines():
    patch = "+- [a](https://a.io) x\n-- [b](https://b.io)\n+- [b](https://b.io) edited\n+++ b/README.md\n"
    assert added_links_from_patch(patch) == [("a", "https://a.io")]


def test_split_diff():
    d = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@\n+- [a](https://a.io)\n"
    assert list(split_diff(d)) == ["README.md"]
    assert added_links_from_patch(split_diff(d)["README.md"]) == [("a", "https://a.io")]


def check():
    gh = GitHub(token="cassette", transport=default_transport())
    return PRChecker(gh, now=NOW, transport=default_transport()).from_pr("lists/awesome-widgets", 42)


def test_pr_report_signals(cassette):
    rep = check()
    assert rep.author == "newbie"
    by = {l.repo: l for l in rep.links}
    assert set(by) == {"newbie/shinytool", "acme/goodlib"}  # moved Old Docs line ignored
    shiny = [s.text for s in by["newbie/shinytool"].signals]
    assert any("created 6 days ago" in s for s in shiny)
    assert any("2 stars" in s for s in shiny)
    assert any("repo owner" in s for s in shiny)
    assert any("23 PRs" in s for s in shiny)
    assert any(s.level == "fail" and "already listed" in s.text for s in by["acme/goodlib"].signals)
    assert by["acme/goodlib"].contributors == 57
    a = [s.text for s in rep.author_signals]
    assert any("11 other awesome lists" in s for s in a)   # own repo and this list excluded, dupes counted once
    assert any("account created 20 days ago" in s for s in a)
    assert rep.worst == "fail"


def test_pr_thresholds_configurable(cassette):
    gh = GitHub(token="cassette", transport=default_transport())
    rep = PRChecker(gh, now=NOW, min_stars=0, min_age_days=0, blast_threshold=50,
                    transport=default_transport()).from_pr("lists/awesome-widgets", 42)
    texts = [s.text for l in rep.links for s in l.signals] + [s.text for s in rep.author_signals]
    assert not any("stars" in t for t in texts)
    assert not any("other awesome lists" in t for t in texts)


def test_markdown_has_marker_and_disclaimer(cassette):
    md = pr_markdown(check(), NOW)
    assert md.startswith("<!-- listwarden:pr-check -->")
    assert "not a judgment" in md
    assert "newbie/shinytool" in md
