from listwarden.parse import extract_links, github_repo, normalize_url, parse_markdown


def test_github_repo_forms():
    assert github_repo("https://github.com/acme/lib") == "acme/lib"
    assert github_repo("https://www.github.com/acme/lib.git") == "acme/lib"
    assert github_repo("https://github.com/acme/lib/tree/main/docs") == "acme/lib"
    assert github_repo("https://github.com/topics/awesome") is None
    assert github_repo("https://github.com/sponsors/someone") is None
    assert github_repo("https://github.com/acme") is None
    assert github_repo("https://gitlab.com/acme/lib") is None


def test_normalize_url():
    assert normalize_url("https://www.Example.com/a/") == normalize_url("http://example.com/a")


def test_extract_skips_images_keeps_bare():
    links = extract_links("- ![b](https://img/x.svg) [t](https://a.io/x \"title\") <https://b.io/y>")
    assert links == [("t", "https://a.io/x"), ("https://b.io/y", "https://b.io/y")]


def test_parse_markdown_entries(list_md):
    es = parse_markdown(list_md)
    urls = [e.url for e in es]
    # anchors (#libraries), heading badge, image badge, fenced code are ignored
    assert not any(u.startswith("#") for u in urls)
    assert "https://awesome.re" not in urls
    assert "https://github.com/acme/incode" not in urls
    assert "https://img.shields.io/badge/x-y-green" not in urls
    assert "https://github.com/acme/tabletool" in urls  # table rows count
    good = [e for e in es if e.repo == "acme/goodlib"]
    assert good[0].section == "Libraries"
    assert {e.key for e in es if e.repo and e.repo.lower() == "acme/goodlib"} == {"acme/goodlib"}
    assert len(es) == 12


def test_one_link_per_item():
    es = parse_markdown("- [a](https://a.io) uses [b](https://b.io)\n")
    assert [e.url for e in es] == ["https://a.io"]
