"""Commit message dialog."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from typing import ClassVar

from textual import containers
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button, LoadingIndicator, Static, TextArea

from diffuse.gitrepo import GitError, GitRepo

CM_TIMEOUT = 10


def load_cm_message(repo: GitRepo) -> str:
    """Return a commit message from ``cm``, or an empty string if unavailable."""
    executable = shutil.which("cm")
    if executable is None:
        return ""

    try:
        proc = subprocess.run(
            [executable, "--staged", "plain"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CM_TIMEOUT,
            check=False,
        )
    except (GitError, OSError, subprocess.SubprocessError):
        return ""

    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


class CommitScreen(ModalScreen[bool]):
    """Edit and submit a commit message over the diff view."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    CommitScreen {
        align: center middle;
    }

    #commit-dialog {
        width: 80;
        max-width: 80;
        height: 1fr;
        max-height: 40;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }

    #commit-loading {
        layout: vertical;
        height: 1fr;
        align: center middle;
    }

    #commit-loading LoadingIndicator {
        height: 1;
        width: 100%;
        content-align: center middle;
    }

    #commit-loading.-hidden,
    #commit-message.-hidden {
        display: none;
    }

    .commit-status {
        width: 100%;
        height: 1;
        margin-top: 1;
        text-align: center;
        color: $text-muted;
    }

    #commit-message {
        height: 1fr;
        max-height: 34;
    }

    #commit-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
        padding-right: 1;
    }

    #commit-buttons Button {
        width: 10;
        min-width: 10;
        height: 1;
        padding: 0 1;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        repo: GitRepo,
        *,
        message_loader: Callable[[GitRepo], str] | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._message_loader = message_loader or load_cm_message
        self._loading = message_loader is not None or shutil.which("cm") is not None
        self._committing = False

    def compose(self) -> ComposeResult:
        loading_classes = "" if self._loading else "-hidden"
        editor_classes = "-hidden" if self._loading else ""
        with containers.Vertical(id="commit-dialog"):
            with containers.Vertical(id="commit-loading", classes=loading_classes):
                yield LoadingIndicator()
                yield Static("Generating commit message\u2026", classes="commit-status")
            yield TextArea(
                id="commit-message",
                placeholder="Commit message",
                soft_wrap=False,
                classes=editor_classes,
            )
            with containers.Horizontal(id="commit-buttons"):
                yield Button("Cancel", id="cancel", compact=True)
                yield Button(
                    "Commit",
                    id="commit",
                    variant="primary",
                    compact=True,
                    disabled=self._loading,
                )

    async def on_mount(self) -> None:
        if self._loading:
            self.run_worker(self._load_message(), exclusive=True)
        else:
            self.query_one("#commit-message", TextArea).focus()

    async def _load_message(self) -> None:
        message = await asyncio.to_thread(self._message_loader, self.repo)
        if not self.is_mounted:
            return
        self._show_editor(message)

    def _show_editor(self, message: str) -> None:
        self._loading = False
        self.query_one("#commit-loading").add_class("-hidden")
        text_area = self.query_one("#commit-message", TextArea)
        text_area.remove_class("-hidden")
        text_area.text = message
        self.query_one("#commit", Button).disabled = False
        text_area.focus()

    def action_cancel(self) -> None:
        if not self._committing:
            self.dismiss(False)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            if not self._committing:
                self.dismiss(False)
        elif event.button.id == "commit":
            await self._commit()

    async def _commit(self) -> None:
        message = self.query_one("#commit-message", TextArea).text.strip()
        if not message:
            self.notify("Enter a commit message", severity="warning")
            return

        commit_button = self.query_one("#commit", Button)
        cancel_button = self.query_one("#cancel", Button)
        self._committing = True
        commit_button.disabled = True
        cancel_button.disabled = True

        try:
            await asyncio.to_thread(self.repo.commit, message)
        except GitError as error:
            self._committing = False
            self.notify(str(error), title="Commit failed", severity="error", timeout=10)
            commit_button.disabled = False
            cancel_button.disabled = False
            return

        self.dismiss(True)
