"""The Textual application."""

from __future__ import annotations

import asyncio
import contextlib
from typing import ClassVar

from textual import containers, widgets
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import var
from textual_diff_view import LoadError

from diffuse.commit import CommitScreen
from diffuse.gitrepo import GitError, GitRepo, Target, TargetKind, is_binary
from diffuse.model import FileChange
from diffuse.widgets import (
    ALL_CONTEXT,
    ContextDiffView,
    FileItem,
    FileList,
    change_is_staged,
)


class DiffApp(App[None]):
    """Browse a repository diff one file at a time."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    FileList {
        width: 20%;
        min-width: 20;
        border-right: solid $panel;
    }

    FileList.-hidden {
        display: none;
    }

    #diff-container {
        width: 1fr;
        padding: 0 1;
    }

    .placeholder {
        padding: 1 2;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_split", "Split"),
        Binding("A", "toggle_annotations", "Annotations"),
        Binding("w", "toggle_whitespace", "Whitespace"),
        Binding("r", "toggle_wrap", "Wrap"),
        Binding("n", "next_file", "Next"),
        Binding("p", "previous_file", "Previous"),
        Binding("alt+n", "next_chunk", "nxt,prv chunk", key_display="alt+n/p"),
        Binding("alt+p", "previous_chunk", "Previous chunk", show=False),
        Binding("s", "stage_file", "Stage"),
        Binding("S", "toggle_sidebar", "Sidebar"),
        Binding("c", "commit", "Commit"),
        Binding("t", "cycle_theme", "Theme"),
        Binding("j", "scroll_diff_down", "Scroll down"),
        Binding("k", "scroll_diff_up", "Scroll up"),
        Binding("=", "more_context", "Context", key_display="=/-"),
        Binding("-", "less_context", "Less context", show=False),
        Binding("a", "toggle_all_context", "All lines"),
        Binding("pageup", "page_diff_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_diff_down", "Page down", show=False, priority=True),
    ]

    split = var(True)
    annotations = var(True)
    wrap = var(False)
    ignore_whitespace = var(False)
    context = var(3)

    def __init__(
        self,
        repo: GitRepo,
        changes: list[FileChange],
        left: Target,
        right: Target,
        context: int = 3,
        *,
        paths: list[str] | None = None,
        find_renames: bool = True,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.changes = changes
        self.left = left
        self.right = right
        self.paths = list(paths or [])
        self.find_renames = find_renames
        self.context = context
        self._default_context = context
        self.sub_title = self._subtitle(left, right)
        self._current: tuple[str | None, str | None] | None = None
        self._request = 0
        self._chunk_index = -1
        self._left_is_head = self._is_head(left)
        self._staged_paths = self._load_staged_paths()

    def compose(self) -> ComposeResult:
        yield widgets.Header(show_clock=False)
        with containers.Horizontal(id="body"):
            yield FileList(self.changes, staged=self._staged_paths, id="files")
            yield containers.VerticalScroll(id="diff-container")
        yield widgets.Footer()

    def on_mount(self) -> None:
        files = self.query_one(FileList)
        files.focus()
        if self.changes:
            self.run_worker(self._show_change(self.changes[0]), exclusive=True)

    def _is_head(self, target: Target) -> bool:
        if target.kind is not TargetKind.COMMIT:
            return False
        try:
            return target.ref == self.repo.resolve("HEAD")
        except GitError:
            return False

    def _load_staged_paths(self) -> set[str]:
        if self.left.kind is TargetKind.FILE or self.right.kind is TargetKind.FILE:
            return set()
        try:
            return self.repo.staged_paths()
        except GitError:
            return set()

    def _can_modify_index(self) -> bool:
        return (
            self.left.kind is not TargetKind.FILE
            and self.right.kind is not TargetKind.FILE
            and self.repo.is_repo()
        )

    def _can_stage(self) -> bool:
        return self.repo.is_repo() and self.right.kind in (
            TargetKind.WORKTREE,
            TargetKind.INDEX,
        )

    def _current_item(self) -> FileItem | None:
        item = self.query_one(FileList).highlighted_child
        return item if isinstance(item, FileItem) else None

    def action_stage_file(self) -> None:
        if not self._can_stage():
            self.notify(
                "Staging is only available in working tree views",
                severity="warning",
            )
            return

        item = self._current_item()
        if item is None:
            return

        try:
            if item.staged:
                self.repo.unstage(item.change)
            else:
                self.repo.stage(item.change)
        except GitError as error:
            self.notify(str(error), title="Git error", severity="error", timeout=10)
            return

        staged = not item.staged
        item.set_staged(staged)
        self.notify(f"{'Staged' if staged else 'Unstaged'} {item.change.display_path}")

    def action_commit(self) -> None:
        if not self._can_modify_index():
            self.notify("Commit is only available in git views", severity="warning")
            return

        if not self._load_staged_paths():
            self.notify("No staged changes to commit", severity="warning")
            return

        self.push_screen(CommitScreen(self.repo), self._commit_finished)

    def _commit_finished(self, committed: bool | None) -> None:
        if committed:
            self.notify("Commit created")
            self.run_worker(self._reload_changes(), exclusive=True)
        self.query_one(FileList).focus()

    async def _reload_changes(self) -> None:
        if self._left_is_head and self.left.kind is TargetKind.COMMIT:
            with contextlib.suppress(GitError):
                self.left = Target.commit(self.repo.resolve("HEAD"))
            self.sub_title = self._subtitle(self.left, self.right)

        files = self.query_one(FileList)
        selected = files.highlighted_child
        selected_key = selected.change.key if isinstance(selected, FileItem) else None

        try:
            changes = await asyncio.to_thread(
                self.repo.changes,
                self.left,
                self.right,
                paths=self.paths or None,
                find_renames=self.find_renames,
            )
        except GitError as error:
            self.notify(str(error), title="Could not refresh", severity="error", timeout=10)
            return

        self.changes = changes
        self._staged_paths = await asyncio.to_thread(self._load_staged_paths)
        files.staged_paths = self._staged_paths
        self._current = None
        self._chunk_index = -1

        container = self.query_one("#diff-container", containers.VerticalScroll)
        await container.remove_children()

        if not changes:
            self.title = "No changes"
            await files.clear()
            files.changes = []
            await container.mount(widgets.Static("No changes to display.", classes="placeholder"))
            return

        await files.clear()
        files.changes = changes
        await files.extend(
            FileItem(change, staged=change_is_staged(change, self._staged_paths))
            for change in changes
        )

        index = 0
        if selected_key is not None:
            for candidate, change in enumerate(changes):
                if change.key == selected_key:
                    index = candidate
                    break

        files.index = index
        files.focus()
        await self._show_change(changes[index])

    async def on_list_view_highlighted(self, event: widgets.ListView.Highlighted) -> None:
        if isinstance(event.item, FileItem):
            await self._show_change(event.item.change)

    async def _show_change(self, change: FileChange) -> None:
        container = self.query_one("#diff-container", containers.VerticalScroll)

        if change.key == self._current:
            return
        self._current = change.key
        self._chunk_index = -1
        self.title = self._change_title(change)
        self._request += 1
        request = self._request

        await container.remove_children()
        await container.mount(widgets.Static("Loading\u2026", classes="placeholder"))

        old_bytes = await asyncio.to_thread(self.repo.blob, self.left, change.old_path)
        new_bytes = await asyncio.to_thread(self.repo.blob, self.right, change.new_path)

        if request != self._request:
            return

        await container.remove_children()

        if is_binary(old_bytes) or is_binary(new_bytes):
            await container.mount(widgets.Static("Binary file not shown.", classes="placeholder"))
            return

        old_label = self._side_label(self.left, change.old_path)
        new_label = self._side_label(self.right, change.new_path)
        try:
            view = ContextDiffView(
                old_label,
                new_label,
                old_bytes.decode("utf-8", "replace"),
                new_bytes.decode("utf-8", "replace"),
                context=self.context,
                ignore_whitespace=self.ignore_whitespace,
            )
        except LoadError as error:  # pragma: no cover - defensive
            await container.mount(widgets.Static(str(error), classes="placeholder"))
            return

        view.data_bind(
            DiffApp.split,
            DiffApp.annotations,
            DiffApp.wrap,
            DiffApp.context,
            DiffApp.ignore_whitespace,
        )

        if request != self._request:
            return
        await container.mount(view)

    @staticmethod
    def _side_label(target: Target, path: str | None) -> str:
        if path is None:
            return "/dev/null"
        if target.kind is TargetKind.FILE:
            return path
        return f"{target.label}:{path}"

    @staticmethod
    def _change_title(change: FileChange) -> str:
        """Title for the header bar, showing both paths for renames/copies."""
        if change.old_path and change.new_path and change.old_path != change.new_path:
            return f"{change.old_path} | {change.new_path}"
        return change.display_path

    def _subtitle(self, left: Target, right: Target) -> str:
        """Describe the two sides, e.g. ``local | HEAD``."""
        if left.kind is TargetKind.FILE and right.kind is TargetKind.FILE:
            return f"{left.label} | {right.label}"
        if left.kind is TargetKind.COMMIT and right.kind is TargetKind.COMMIT:
            return f"{self._commit_label(left)} | {self._commit_label(right)}"
        base = self._commit_label(left) if left.kind is TargetKind.COMMIT else "HEAD"
        return f"local | {base}"

    def _commit_label(self, target: Target) -> str:
        ref = target.ref or "HEAD"
        try:
            if ref == self.repo.resolve("HEAD"):
                return "HEAD"
        except GitError:
            pass
        return ref[:7]

    def action_toggle_split(self) -> None:
        self.split = not self.split
        self._chunk_index = -1

    def action_toggle_annotations(self) -> None:
        self.annotations = not self.annotations
        self._chunk_index = -1

    def action_toggle_whitespace(self) -> None:
        self.ignore_whitespace = not self.ignore_whitespace
        self._chunk_index = -1

    def action_toggle_wrap(self) -> None:
        self.wrap = not self.wrap
        self._chunk_index = -1

    def action_more_context(self) -> None:
        if self.context < ALL_CONTEXT:
            self._set_context(self.context + 1)

    def action_less_context(self) -> None:
        if self.context == ALL_CONTEXT:
            self._set_context(self._default_context)
        elif self.context > 0:
            self._set_context(self.context - 1)

    def action_toggle_all_context(self) -> None:
        if self.context == ALL_CONTEXT:
            self._set_context(self._default_context)
        else:
            self._set_context(ALL_CONTEXT)

    def _set_context(self, value: int) -> None:
        """Set the context size (the view preserves the scroll anchor)."""
        if value != self.context:
            self.context = value
            self._chunk_index = -1

    def action_next_file(self) -> None:
        self.query_one(FileList).action_cursor_down()

    def action_previous_file(self) -> None:
        self.query_one(FileList).action_cursor_up()

    def action_next_chunk(self) -> None:
        self._scroll_to_chunk(1)

    def action_previous_chunk(self) -> None:
        self._scroll_to_chunk(-1)

    def _scroll_to_chunk(self, direction: int) -> None:
        views = list(self.query(ContextDiffView))
        if not views:
            return
        offsets = views[0].chunk_offsets()
        if not offsets:
            return

        container = self.query_one("#diff-container", containers.VerticalScroll)
        index = self._chunk_index
        if index < 0:
            current = container.scroll_y
            passed = [i for i, offset in enumerate(offsets) if offset <= current + 0.5]
            if direction > 0:
                index = passed[-1] + 1 if passed else 0
            else:
                index = passed[-1] if passed else 0
        else:
            index += direction

        index = max(0, min(index, len(offsets) - 1))
        self._chunk_index = index
        container.scroll_to(y=offsets[index], animate=False)

    def action_toggle_sidebar(self) -> None:
        self.query_one(FileList).toggle_class("-hidden")

    def action_cycle_theme(self) -> None:
        themes = sorted(self.available_themes)
        try:
            index = themes.index(self.theme)
        except ValueError:
            index = -1
        self.theme = themes[(index + 1) % len(themes)]
        self.notify(f"Theme: {self.theme}", timeout=15)

    def action_scroll_diff_down(self) -> None:
        self.query_one("#diff-container", containers.VerticalScroll).scroll_relative(y=2)

    def action_scroll_diff_up(self) -> None:
        self.query_one("#diff-container", containers.VerticalScroll).scroll_relative(y=-2)

    def action_page_diff_down(self) -> None:
        self.query_one("#diff-container", containers.VerticalScroll).scroll_page_down(animate=False)

    def action_page_diff_up(self) -> None:
        self.query_one("#diff-container", containers.VerticalScroll).scroll_page_up(animate=False)
