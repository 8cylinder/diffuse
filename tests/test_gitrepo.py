from __future__ import annotations

from pathlib import Path

import pytest

from diffuse.gitrepo import GitRepo, Target, _parse_name_status, is_binary
from diffuse.model import ChangeStatus

from .conftest import commit, git, write


def test_parse_name_status_add_modify_delete() -> None:
    data = b"A\0new.py\0M\0mod.py\0D\0gone.py\0"
    changes = _parse_name_status(data)
    assert [(c.status, c.old_path, c.new_path) for c in changes] == [
        (ChangeStatus.ADDED, None, "new.py"),
        (ChangeStatus.MODIFIED, "mod.py", "mod.py"),
        (ChangeStatus.DELETED, "gone.py", None),
    ]


def test_parse_name_status_rename() -> None:
    data = b"R100\0old.py\0new.py\0"
    (change,) = _parse_name_status(data)
    assert change.status is ChangeStatus.RENAMED
    assert change.old_path == "old.py"
    assert change.new_path == "new.py"
    assert change.display_path == "old.py \u2192 new.py"


def test_worktree_vs_index(repo: Path) -> None:
    write(repo, "README.md", "hello world\n")
    git_repo = GitRepo(repo)
    changes = git_repo.changes(Target.index(), Target.worktree())
    assert [(c.status, c.new_path) for c in changes] == [(ChangeStatus.MODIFIED, "README.md")]


def test_untracked_files_are_included(repo: Path) -> None:
    write(repo, "untracked.txt", "new\n")
    changes = GitRepo(repo).changes(Target.index(), Target.worktree())
    assert [(c.status, c.old_path, c.new_path) for c in changes] == [
        (ChangeStatus.UNTRACKED, None, "untracked.txt")
    ]


def test_untracked_files_are_included_in_unborn_repo(tmp_path: Path) -> None:
    git(tmp_path, "init")
    write(tmp_path, "untracked.txt", "new\n")
    changes = GitRepo(tmp_path).changes(Target.index(), Target.worktree())
    assert [(c.status, c.new_path) for c in changes] == [(ChangeStatus.UNTRACKED, "untracked.txt")]


def test_untracked_files_included_for_commit_to_worktree(repo: Path) -> None:
    write(repo, "untracked.txt", "new\n")
    changes = GitRepo(repo).changes(Target.commit("HEAD"), Target.worktree())
    assert [(c.status, c.new_path) for c in changes] == [(ChangeStatus.UNTRACKED, "untracked.txt")]


def test_untracked_files_excluded_from_staged_diff(repo: Path) -> None:
    write(repo, "untracked.txt", "new\n")
    changes = GitRepo(repo).changes(Target.commit("HEAD"), Target.index())
    assert changes == []


def test_untracked_files_respect_paths(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    write(repo, "b.txt", "b\n")
    changes = GitRepo(repo).changes(Target.index(), Target.worktree(), paths=["a.txt"])
    assert [c.new_path for c in changes] == ["a.txt"]


def test_ignored_files_are_not_untracked(repo: Path) -> None:
    write(repo, ".gitignore", "ignored.txt\n")
    commit(repo, "ignore")
    write(repo, "ignored.txt", "ignored\n")
    assert GitRepo(repo).changes(Target.index(), Target.worktree()) == []


def test_untracked_paths_are_repo_relative_from_subdir(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(repo, "sub/tracked.txt", "one\n")
    commit(repo, "sub")
    monkeypatch.chdir(repo / "sub")
    write(repo, "sub/untracked.txt", "new\n")

    changes = GitRepo(Path.cwd()).changes(Target.index(), Target.worktree())
    assert [(c.status, c.new_path) for c in changes] == [
        (ChangeStatus.UNTRACKED, "sub/untracked.txt")
    ]


def test_staged_changes(repo: Path) -> None:
    write(repo, "README.md", "hello world\n")
    write(repo, "added.txt", "new file\n")
    git_repo = GitRepo(repo)
    git_repo._run(["add", "-A"])
    changes = git_repo.changes(Target.commit("HEAD"), Target.index())
    statuses = {c.new_path: c.status for c in changes}
    assert statuses["README.md"] is ChangeStatus.MODIFIED
    assert statuses["added.txt"] is ChangeStatus.ADDED


def test_two_commit_range(repo: Path) -> None:
    write(repo, "README.md", "first\n")
    first = commit(repo, "c1")
    write(repo, "README.md", "second\n")
    write(repo, "extra.py", "print('hi')\n")
    second = commit(repo, "c2")

    changes = GitRepo(repo).changes(Target.commit(first), Target.commit(second))
    assert {(c.status, c.new_path) for c in changes} == {
        (ChangeStatus.MODIFIED, "README.md"),
        (ChangeStatus.ADDED, "extra.py"),
    }


def test_blob_reads_both_sides(repo: Path) -> None:
    write(repo, "file.txt", "one\n")
    first = commit(repo, "c1")
    write(repo, "file.txt", "two\n")
    second = commit(repo, "c2")

    git_repo = GitRepo(repo)
    assert git_repo.blob(Target.commit(first), "file.txt") == b"one\n"
    assert git_repo.blob(Target.commit(second), "file.txt") == b"two\n"
    assert git_repo.blob(Target.commit(second), "missing.txt") == b""
    assert git_repo.blob(Target.commit(second), None) == b""


def test_rename_detection(repo: Path) -> None:
    write(repo, "long_name.py", "x = 1\n" * 40)
    first = commit(repo, "c1")
    (repo / "long_name.py").rename(repo / "renamed.py")
    second = commit(repo, "c2")

    changes = GitRepo(repo).changes(Target.commit(first), Target.commit(second))
    assert len(changes) == 1
    assert changes[0].status is ChangeStatus.RENAMED
    assert changes[0].old_path == "long_name.py"
    assert changes[0].new_path == "renamed.py"


def test_path_filter(repo: Path) -> None:
    write(repo, "a.txt", "a\n")
    write(repo, "b.txt", "b\n")
    git_repo = GitRepo(repo)
    git_repo._run(["add", "-A"])
    commit(repo, "add files")

    write(repo, "a.txt", "changed a\n")
    write(repo, "b.txt", "changed b\n")
    changes = git_repo.changes(Target.index(), Target.worktree(), paths=["a.txt"])
    assert [c.new_path for c in changes] == ["a.txt"]


def test_file_targets_read_from_disk(repo: Path) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    git_repo = GitRepo(repo)

    changes = git_repo.changes(Target.file("a.txt"), Target.file("b.txt"))
    assert [(c.status, c.old_path, c.new_path) for c in changes] == [
        (ChangeStatus.MODIFIED, "a.txt", "b.txt")
    ]
    assert git_repo.blob(Target.file("a.txt"), "a.txt") == b"one\n"
    assert git_repo.blob(Target.file("b.txt"), "b.txt") == b"two\n"


def test_file_targets_with_equal_contents_have_no_changes(repo: Path) -> None:
    write(repo, "a.txt", "same\n")
    write(repo, "b.txt", "same\n")

    changes = GitRepo(repo).changes(Target.file("a.txt"), Target.file("b.txt"))
    assert changes == []


def test_file_targets_support_absolute_paths(repo: Path) -> None:
    write(repo, "a.txt", "one\n")
    write(repo, "b.txt", "two\n")
    git_repo = GitRepo(repo)

    old = str(repo / "a.txt")
    new = str(repo / "b.txt")
    changes = git_repo.changes(Target.file(old), Target.file(new))
    assert changes[0].old_path == old
    assert changes[0].new_path == new
    assert git_repo.blob(Target.file(old), old) == b"one\n"


def test_stage_and_unstage_file(repo: Path) -> None:
    write(repo, "README.md", "changed\n")
    git_repo = GitRepo(repo)
    change = git_repo.changes(Target.index(), Target.worktree())[0]

    git_repo.stage(change)
    assert git_repo.staged_paths() == {"README.md"}
    assert git_repo.changes(Target.index(), Target.worktree()) == []

    git_repo.unstage(change)
    assert git_repo.staged_paths() == set()
    assert git_repo.changes(Target.index(), Target.worktree())


def test_stage_untracked_file(repo: Path) -> None:
    write(repo, "new.txt", "new\n")
    git_repo = GitRepo(repo)
    change = git_repo.changes(Target.index(), Target.worktree())[0]
    assert change.status is ChangeStatus.UNTRACKED

    git_repo.stage(change)
    assert git_repo.staged_paths() == {"new.txt"}


def test_unstage_in_unborn_repo(tmp_path: Path) -> None:
    git(tmp_path, "init")
    write(tmp_path, "new.txt", "new\n")
    git_repo = GitRepo(tmp_path)
    change = git_repo.changes(Target.index(), Target.worktree())[0]

    git_repo.stage(change)
    assert git_repo.staged_paths() == {"new.txt"}

    git_repo.unstage(change)
    assert git_repo.staged_paths() == set()


def test_commit_uses_message_on_stdin(repo: Path) -> None:
    write(repo, "README.md", "committed\n")
    git_repo = GitRepo(repo)
    change = git_repo.changes(Target.index(), Target.worktree())[0]
    git_repo.stage(change)
    git_repo.commit("subject line\n\nbody text")

    message = git(repo, "log", "-1", "--format=%B").stdout.decode().strip()
    assert message == "subject line\n\nbody text"
    assert git_repo.staged_paths() == set()


def test_binary_detection() -> None:
    assert not is_binary(b"plain text\n")
    assert not is_binary(b"\x89PNG\r\n\x1a\n")
    assert is_binary(b"abc\0def")
