import json
import os
import shutil

from listwarden.cli import main

from conftest import FIX


def test_audit_text_exit_code(cassette, capsys):
    rc = main(["--color", "never", "audit", os.path.join(FIX, "list.md")])
    out = capsys.readouterr().out
    assert rc == 1  # dead entries present
    assert "12 entries" in out and "auto-fixable" in out
    assert "renamed" in out and "now vercel/widgets" in out


def test_audit_fail_on_none_and_json(cassette, capsys):
    rc = main(["audit", os.path.join(FIX, "list.md"), "--format", "json", "--fail-on", "none"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert d["summary"]["dead"] == 2 and d["summary"]["archived"] == 1


def test_audit_markdown_and_step_summary(cassette, capsys, tmp_path, monkeypatch):
    summ = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    main(["audit", os.path.join(FIX, "list.md"), "--format", "markdown", "--fail-on", "none"])
    out = capsys.readouterr().out
    assert "| Line | Problem |" in out
    assert "listwarden audit" in summ.read_text()


def test_fix_dry_run_then_write(cassette, capsys, tmp_path):
    p = tmp_path / "README.md"
    shutil.copy(os.path.join(FIX, "list.md"), p)
    before = p.read_text()
    assert main(["fix", str(p)]) == 0
    out = capsys.readouterr().out
    assert "+- [renamed](https://github.com/vercel/widgets)" in out and "dry run" in out
    assert p.read_text() == before
    assert main(["fix", str(p), "--prune", "dead", "--write"]) == 0
    after = p.read_text()
    assert "vercel/widgets" in after and "ghost/gone" not in after


def test_fix_rejects_bad_prune(cassette):
    import pytest
    with pytest.raises(SystemExit):
        main(["fix", os.path.join(FIX, "list.md"), "--prune", "renamed"])


def test_pr_command_and_comment(cassette, capsys):
    rc = main(["--color", "never", "pr", "https://github.com/lists/awesome-widgets/pull/42", "--comment"])
    cap = capsys.readouterr()
    assert rc == 1
    assert "newbie/shinytool" in cap.out and "result: FAIL" in cap.out
    assert "issuecomment-1" in cap.err


def test_pr_diff_mode(cassette, capsys, tmp_path):
    d = tmp_path / "x.diff"
    d.write_text("diff --git a/README.md b/README.md\n+++ b/README.md\n@@\n+- [w](https://widgets.example.com/) ok\n")
    base = tmp_path / "base.md"
    base.write_text("# x\n")
    rc = main(["pr", "--diff", str(d), "--repo", "lists/awesome-widgets", "--author", "newbie",
               "--base", str(base), "--format", "json", "--fail-on", "warn"])
    js = json.loads(capsys.readouterr().out)
    assert js["links"][0]["web_status"] == "ok"
    assert rc == 1  # author warnings (blast + young account) with --fail-on warn


def test_version(capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "listwarden" in capsys.readouterr().out
