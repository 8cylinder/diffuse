from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import LoadingIndicator, TextArea

from diffuse.commit import CommitScreen, load_cm_message
from diffuse.gitrepo import GitError, GitRepo, Target

from .conftest import git, write


class CommitHost(App[None]):
    """A tiny app that opens a :class:`CommitScreen` on startup."""

    def __init__(
        self,
        repo: GitRepo,
        *,
        message_loader: Callable[[GitRepo], str] | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.message_loader = message_loader
        self.result: bool | None = None

    def on_mount(self) -> None:
        self.push_screen(
            CommitScreen(self.repo, message_loader=self.message_loader),
            self._save_result,
        )

    def _save_result(self, result: bool | None) -> None:
        self.result = result


def test_load_cm_message_uses_cm(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="feat: generated\n", stderr="")

    monkeypatch.setattr("diffuse.commit.shutil.which", lambda command: "/usr/bin/cm")
    monkeypatch.setattr("diffuse.commit.subprocess.run", fake_run)
    git_repo = GitRepo(repo)
    git_repo._root = repo

    assert load_cm_message(git_repo) == "feat: generated"
    assert calls[0][0] == ["/usr/bin/cm", "--staged", "plain"]
    assert calls[0][1]["cwd"] == repo


def test_load_cm_message_missing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("diffuse.commit.shutil.which", lambda command: None)
    assert load_cm_message(GitRepo(repo)) == ""


def test_load_cm_message_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("diffuse.commit.shutil.which", lambda command: "/usr/bin/cm")
    monkeypatch.setattr(
        "diffuse.commit.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="no"),
    )
    git_repo = GitRepo(repo)
    git_repo._root = repo
    assert load_cm_message(git_repo) == ""


async def test_commit_screen_prefills_generated_message(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "feat: generated")

    async with host.run_test() as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        assert host.screen.query_one(TextArea).text == "feat: generated"


async def test_commit_screen_skips_spinner_without_cm(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("diffuse.commit.shutil.which", lambda command: None)
    host = CommitHost(GitRepo(repo))

    async with host.run_test() as pilot:
        await pilot.pause()
        assert host.screen.query_one("#commit-loading").display is False
        area = host.screen.query_one(TextArea)
        assert area.display is True
        assert area.text == ""


async def test_commit_screen_shows_spinner_until_message_ready(repo: Path) -> None:
    release = threading.Event()

    def loader(repo: GitRepo) -> str:
        release.wait(timeout=5)
        return "feat: generated"

    host = CommitHost(GitRepo(repo), message_loader=loader)

    async with host.run_test() as pilot:
        await pilot.pause()
        loading = host.screen.query_one("#commit-loading")
        spinner = host.screen.query_one(LoadingIndicator)
        status = host.screen.query_one(".commit-status")
        area = host.screen.query_one(TextArea)
        commit_button = host.screen.query_one("#commit")
        try:
            assert loading.display is True
            assert area.display is False
            assert commit_button.disabled is True
            assert status.region.y == spinner.region.bottom + 1
            assert spinner.region.x == status.region.x
            assert spinner.region.width == status.region.width
            assert status.styles.text_align == "center"
        finally:
            release.set()

        await host.workers.wait_for_complete()
        await pilot.pause()
        assert loading.display is False
        assert area.display is True
        assert area.text == "feat: generated"
        assert commit_button.disabled is False


async def test_commit_screen_is_blank_without_generated_message(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        assert host.screen.query_one(TextArea).text == ""


async def test_commit_screen_size_and_buttons(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "")

    async with host.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        dialog = host.screen.query_one("#commit-dialog")
        area = host.screen.query_one(TextArea)
        assert dialog.outer_size.width == 80
        assert dialog.outer_size.height == 40
        assert area.soft_wrap is False
        commit_button = host.screen.query_one("#commit")
        cancel_button = host.screen.query_one("#cancel")
        assert commit_button.outer_size.width == cancel_button.outer_size.width == 10
        assert commit_button.size.height == cancel_button.size.height == 1
        assert commit_button.region.right == area.region.right - 1

        area.text = "\n".join(f"line {i}" for i in range(100))
        await pilot.pause()
        assert dialog.outer_size.height == 40


async def test_commit_screen_cancel(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
        assert host.result is False


async def test_commit_screen_escape_cancels(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert host.result is False


async def test_commit_screen_rejects_empty_message(repo: Path) -> None:
    host = CommitHost(GitRepo(repo), message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#commit")
        await pilot.pause()
        assert host.result is None
        assert isinstance(host.screen, CommitScreen)


async def test_commit_screen_commits_staged_changes(repo: Path) -> None:
    write(repo, "README.md", "changed\n")
    git_repo = GitRepo(repo)
    change = git_repo.changes(Target.index(), Target.worktree())[0]
    git_repo.stage(change)
    host = CommitHost(git_repo, message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        host.screen.query_one(TextArea).text = "feat: commit from dialog"
        await pilot.click("#commit")
        for _ in range(50):
            await pilot.pause(0.01)
            if host.result is not None:
                break

        assert host.result is True
        message = git(repo, "log", "-1", "--format=%s").stdout.decode().strip()
        assert message == "feat: commit from dialog"


async def test_commit_screen_keeps_dialog_on_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_repo = GitRepo(repo)

    def fail(message: str) -> None:
        raise GitError("commit failed")

    monkeypatch.setattr(git_repo, "commit", fail)
    host = CommitHost(git_repo, message_loader=lambda repo: "")

    async with host.run_test() as pilot:
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        host.screen.query_one(TextArea).text = "will fail"
        await pilot.click("#commit")
        await pilot.pause()

        assert host.result is None
        assert isinstance(host.screen, CommitScreen)
