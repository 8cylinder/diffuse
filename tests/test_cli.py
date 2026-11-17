from __future__ import annotations

from pathlib import Path

import click
import pytest

from diffuse import __version__
from diffuse.cli import ALL_CONTEXT, Options, main, parse_args, resolve_targets, run
from diffuse.gitrepo import GitRepo, Target, TargetKind
from diffuse.model import FileChange

from .conftest import commit, git, write


def test_defaults() -> None:
    options = parse_args([])
    assert options.revs == []
    assert options.paths == []
    assert options.files == []
    assert options.staged is False
    assert options.find_renames is True


def test_two_revisions_and_paths() -> None:
    options = parse_args(["main", "feature", "--", "a.py", "b.py"])
    assert options.revs == ["main", "feature"]
    assert options.paths == ["a.py", "b.py"]


def test_staged_and_cached_aliases() -> None:
    assert parse_args(["--staged"]).staged is True
    assert parse_args(["--cached"]).staged is True


def test_no_renames() -> None:
    assert parse_args(["--no-renames"]).find_renames is False
    assert parse_args(["-M"]).find_renames is True


def test_context_default_and_flags() -> None:
    assert parse_args([]).context == 3
    assert parse_args(["-c", "5"]).context == 5
    assert parse_args(["--context", "0"]).context == 0
    assert parse_args(["--context=10"]).context == 10


def test_context_a_means_whole_file() -> None:
    assert parse_args(["-c", "a"]).context == ALL_CONTEXT
    assert parse_args(["--context=A"]).context == ALL_CONTEXT


def test_context_rejects_negative() -> None:
    with pytest.raises(click.UsageError):
        parse_args(["--context", "-1"])


def test_too_many_revisions() -> None:
    with pytest.raises(click.UsageError):
        parse_args(["a", "b", "c"])


def test_two_existing_files_are_compared(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    monkeypatch.chdir(repo)

    options = parse_args(["a.txt", "b.txt"])
    assert options.files == ["a.txt", "b.txt"]
    assert options.revs == []


def test_two_revisions_are_not_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    options = parse_args(["main", "feature"])
    assert options.files == []
    assert options.revs == ["main", "feature"]


def _capture_tty_options(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Options]:
    """Run ``main`` as if attached to a TTY, capturing the parsed options."""
    captured: list[Options] = []

    def fake_run(options: Options) -> int:
        captured.append(options)
        return 0

    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("diffuse.cli.run", fake_run)
    return captured


def test_tty_two_existing_files_are_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    captured = _capture_tty_options(monkeypatch)

    assert main([str(first), str(second)]) == 0
    (options,) = captured
    assert options.files == [str(first), str(second)]
    assert options.revs == []


def test_tty_two_revisions_are_not_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    captured = _capture_tty_options(monkeypatch)

    assert main(["main", "feature"]) == 0
    (options,) = captured
    assert options.files == []
    assert options.revs == ["main", "feature"]


def test_run_compares_file_pair_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    captured: list[tuple[list[FileChange], Target, Target, int]] = []

    class FakeApp:
        def __init__(
            self,
            repo: GitRepo,
            changes: list[FileChange],
            left: Target,
            right: Target,
            context: int,
        ) -> None:
            captured.append((changes, left, right, context))

        def run(self) -> None:
            pass

    monkeypatch.setattr("diffuse.cli.DiffApp", FakeApp)

    assert run(Options(files=[str(first), str(second)])) == 0
    ((changes, left, right, _),) = captured
    assert left == Target.file(str(first))
    assert right == Target.file(str(second))
    assert len(changes) == 1


def test_resolve_default_is_index_worktree(repo: Path) -> None:
    left, right = resolve_targets(GitRepo(repo), parse_args([]))
    assert left.kind is TargetKind.INDEX
    assert right.kind is TargetKind.WORKTREE


def test_resolve_default_does_not_require_head(tmp_path: Path) -> None:
    git(tmp_path, "init")
    left, right = resolve_targets(GitRepo(tmp_path), parse_args([]))
    assert left.kind is TargetKind.INDEX
    assert right.kind is TargetKind.WORKTREE


def test_resolve_staged_is_head_index(repo: Path) -> None:
    left, right = resolve_targets(GitRepo(repo), parse_args(["--staged"]))
    assert left.kind is TargetKind.COMMIT
    assert right.kind is TargetKind.INDEX


def test_resolve_two_commits(repo: Path) -> None:
    write(repo, "README.md", "first\n")
    first = commit(repo, "c1")
    write(repo, "README.md", "second\n")
    second = commit(repo, "c2")
    left, right = resolve_targets(GitRepo(repo), parse_args([first, second]))
    assert left == Target.commit(first)
    assert right == Target.commit(second)


def test_resolve_one_commit_hash(repo: Path) -> None:
    write(repo, "README.md", "first\n")
    first = commit(repo, "c1")
    left, right = resolve_targets(GitRepo(repo), parse_args([first]))
    assert left == Target.commit(first)
    assert right.kind is TargetKind.WORKTREE


def test_resolve_two_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    monkeypatch.chdir(repo)

    left, right = resolve_targets(GitRepo(repo), parse_args(["a.txt", "b.txt"]))
    assert left == Target.file("a.txt")
    assert right == Target.file("b.txt")


def test_resolve_one_commit_hash_staged(repo: Path) -> None:
    write(repo, "README.md", "first\n")
    first = commit(repo, "c1")
    left, right = resolve_targets(GitRepo(repo), parse_args([first, "--staged"]))
    assert left == Target.commit(first)
    assert right.kind is TargetKind.INDEX


def test_help_is_handled_without_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "Usage: diffuse" in out
    assert "-h, --help" in out
    assert "diffuse HEAD" in out
    assert "a commit hash" in out
    assert "diffuse file1 file2" in out
    assert "two existing files" in out
    assert "plus untracked files" in out
    assert "diffuse HEAD -- a.py b.py" in out
    assert "pathspecs" in out


def test_short_help_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    assert main(["-h"]) == 0
    assert "Usage: diffuse" in capsys.readouterr().out


def test_version_is_handled_without_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"diffuse {__version__}"


def test_completions_writes_bash_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)

    assert main(["--completions"]) == 0

    script = tmp_path / ".local/share/bash-completion/completions/diffuse"
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "_diffuse_completion" in body
    assert "complete -o nosort -F _diffuse_completion diffuse" in body

    out = capsys.readouterr().out
    assert f"wrote completion script to {script}" in out
    assert f"source it with: source {script}" in out


def test_passthrough_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.subprocess.call", lambda args: calls.append(args) or 0)

    assert main(["HEAD~1", "HEAD", "--", "a.py"]) == 0
    assert calls == [["git", "diff", "HEAD~1", "HEAD", "--", "a.py"]]


def test_passthrough_compares_two_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    monkeypatch.chdir(repo)

    calls: list[list[str]] = []
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.subprocess.call", lambda args: calls.append(args) or 0)

    assert main(["a.txt", "b.txt"]) == 0
    assert calls == [["git", "diff", "--no-index", "--", "a.txt", "b.txt"]]


def test_passthrough_compares_two_files_with_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    monkeypatch.chdir(repo)

    calls: list[list[str]] = []
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.subprocess.call", lambda args: calls.append(args) or 0)

    assert main(["-c", "5", "a.txt", "b.txt"]) == 0
    assert calls == [["git", "diff", "--no-index", "--unified=5", "--", "a.txt", "b.txt"]]


def test_passthrough_translates_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.subprocess.call", lambda args: calls.append(args) or 0)

    assert main(["-c", "7", "HEAD", "--", "a.py"]) == 0
    assert calls == [["git", "diff", "--unified=7", "HEAD", "--", "a.py"]]


def test_passthrough_translates_full_context(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("diffuse.cli.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("diffuse.cli.subprocess.call", lambda args: calls.append(args) or 0)

    assert main(["-c", "a", "HEAD"]) == 0
    assert calls == [["git", "diff", f"--unified={ALL_CONTEXT}", "HEAD"]]
