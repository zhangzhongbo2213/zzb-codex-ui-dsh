"""Unit tests for zzb-codex-ui.

The tests write only to temporary directories below this file (the sandbox does
not permit /var temporary folders).  They never open the real zzb-codex
database and never invoke the real launcher.
"""

from __future__ import annotations

import io
from contextlib import closing
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app  # noqa: E402


def make_temp(testcase: unittest.TestCase, prefix: str = ".test-zzb-ui-") -> str:
    path = tempfile.mkdtemp(prefix=prefix, dir=HERE)
    testcase.addCleanup(shutil.rmtree, path, True)
    return path


def make_threads_db(
    state_dir: str,
    rows: list[tuple[object, ...]],
    create_table: bool = True,
) -> str:
    db_dir = os.path.join(state_dir, "db")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "state_5.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        if create_table:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT, cwd TEXT, title TEXT, updated_at INTEGER, archived INTEGER)"
            )
            if rows:
                connection.executemany(
                    "INSERT INTO threads (id, cwd, title, updated_at, archived) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
        connection.commit()
    finally:
        connection.close()
    return db_path


class TextAndPathTests(unittest.TestCase):
    def test_control_characters_are_neutralized(self) -> None:
        raw = "标题\x1b[31m红\x07\n下一行\u202e反向"
        clean = app.sanitize_text(raw)
        for forbidden in ("\x1b", "\x07", "\n", "\u202e"):
            self.assertNotIn(forbidden, clean)
        self.assertIn("标题", clean)
        self.assertEqual(app.sanitize_text(None), "")

    def test_cjk_truncation_uses_terminal_cells(self) -> None:
        self.assertEqual(app.display_width("中文ab"), 6)
        self.assertEqual(app.truncate_display("abc", 5), "abc")
        clipped = app.truncate_display("中文ab", 4)
        self.assertLessEqual(app.display_width(clipped), 4)
        self.assertTrue(clipped.endswith("…"))
        wide = app.truncate_display("中文", 3)
        self.assertEqual(wide, "中…")
        self.assertLessEqual(app.display_width(wide), 3)
        self.assertEqual(app.truncate_display("中文", 1), "…")

    def test_expanduser_and_realpath_normalization(self) -> None:
        tmp = make_temp(self)
        home = os.path.join(tmp, "home with space")
        target = os.path.join(home, "project")
        os.makedirs(target)
        alias = os.path.join(tmp, "alias with space")
        os.symlink(target, alias)
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(app.normalize_path("~/project"), os.path.realpath(target))
        self.assertEqual(app.normalize_path(alias), os.path.realpath(target))

    def test_state_path_and_readonly_uri(self) -> None:
        state = os.path.join("/tmp", "state with spaces")
        db_path = app.state_database_path(state)
        self.assertEqual(db_path, os.path.join(state, "db", "state_5.sqlite"))
        uri = app.sqlite_readonly_uri(db_path)
        self.assertIn("mode=ro", uri)
        self.assertIn("%20", uri)


class ThreadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp(self)
        self.state = os.path.join(self.tmp, "state with spaces")
        self.project = os.path.join(self.tmp, "project with spaces")
        self.child = os.path.join(self.project, "child")
        self.other = os.path.join(self.tmp, "other")
        os.makedirs(self.project)
        os.makedirs(self.child)
        os.makedirs(self.other)
        self.rows = [
            ("old", self.project, "旧会话", 100, 0),
            ("new", self.project, "\x1b[2J新会话\x07", 300, 0),
            ("archived", self.project, "已归档", 999, 1),
            ("child", self.child, "子目录会话", 500, 0),
            ("other", self.other, "另一个目录", 600, 0),
            ("notitle", self.project, None, 200, 0),
        ]
        self.db = make_threads_db(self.state, self.rows)
        self.store = app.ThreadStore.from_state_dir(self.state)

    def test_rename_uses_name_and_refreshes_without_reopening_store(self) -> None:
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN name TEXT")
            db.execute("UPDATE threads SET name=? WHERE id='old'", ("美术资产",))
            db.execute("UPDATE threads SET name=? WHERE id='new'", ("   ",))
        sessions = {s.id: s for s in self.store.load_sessions(self.project).sessions}
        self.assertEqual(sessions['old'].title, "美术资产")
        self.assertEqual(sessions['new'].title, app.sanitize_text(self.rows[1][2]))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE threads SET name=? WHERE id='old'", ("新的名字\x07",))
        self.assertEqual({s.id: s.title for s in self.store.load_sessions(self.project).sessions}['old'], "新的名字")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE threads SET name=NULL WHERE id='old'")
        self.assertEqual({s.id: s.title for s in self.store.load_sessions(self.project).sessions}['old'], "旧会话")

    def test_exact_cwd_filter_archive_filter_and_recent_sort(self) -> None:
        query = self.store.load_sessions(self.project)
        self.assertIsNone(query.error)
        self.assertFalse(query.missing_database)
        self.assertEqual([session.id for session in query.sessions], ["new", "notitle", "old"])
        self.assertEqual(query.count, 3)
        self.assertNotIn("archived", [session.id for session in query.sessions])
        self.assertNotIn("child", [session.id for session in query.sessions])
        self.assertNotIn("other", [session.id for session in query.sessions])
        self.assertEqual(query.sessions[1].title, "（无标题）")
        for session in query.sessions:
            self.assertNotIn("\x1b", session.title)
            self.assertNotIn("\x07", session.title)

    def test_empty_table_is_not_an_error(self) -> None:
        empty_state = os.path.join(self.tmp, "empty state")
        make_threads_db(empty_state, [])
        query = app.ThreadStore.from_state_dir(empty_state).load_sessions(self.project)
        self.assertEqual(query.sessions, [])
        self.assertIsNone(query.error)
        self.assertFalse(query.missing_database)

    def test_missing_database_is_explicit(self) -> None:
        missing_state = os.path.join(self.tmp, "missing state")
        os.makedirs(missing_state)
        query = app.ThreadStore.from_state_dir(missing_state).load_sessions(self.project)
        self.assertEqual(query.sessions, [])
        self.assertTrue(query.missing_database)
        self.assertIsNone(query.error)
        self.assertTrue(query.database.endswith(os.path.join("db", "state_5.sqlite")))

    def test_broken_database_returns_error_not_empty_success(self) -> None:
        broken_state = os.path.join(self.tmp, "broken state")
        broken_dir = os.path.join(broken_state, "db")
        os.makedirs(broken_dir)
        broken_db = os.path.join(broken_dir, "state_5.sqlite")
        with open(broken_db, "wb") as handle:
            handle.write(b"this is not a sqlite database")
        query = app.ThreadStore.from_state_dir(broken_state).load_sessions(self.project)
        self.assertEqual(query.sessions, [])
        self.assertIsNotNone(query.error)
        self.assertFalse(query.missing_database)
        self.assertIn("数据库", query.error)

    def test_missing_threads_table_returns_error(self) -> None:
        no_table_state = os.path.join(self.tmp, "no table state")
        make_threads_db(no_table_state, [], create_table=False)
        query = app.ThreadStore.from_state_dir(no_table_state).load_sessions(self.project)
        self.assertIsNotNone(query.error)
        self.assertIn("threads", query.error)

    def test_stored_symlink_and_tilde_cwds_are_normalized(self) -> None:
        real_project = os.path.join(self.tmp, "real project")
        os.makedirs(real_project)
        alias = os.path.join(self.tmp, "project alias")
        os.symlink(real_project, alias)
        home = os.path.join(self.tmp, "fake home")
        tilde_project = os.path.join(home, "tilde project")
        os.makedirs(tilde_project)
        state = os.path.join(self.tmp, "normalized state")
        make_threads_db(
            state,
            [
                ("via-alias", alias, "别名目录", 50, 0),
                ("via-tilde", "~/tilde project", "波浪号目录", 60, 0),
            ],
        )
        store = app.ThreadStore.from_state_dir(state)
        alias_query = store.load_sessions(real_project)
        self.assertEqual([session.id for session in alias_query.sessions], ["via-alias"])
        self.assertEqual(alias_query.sessions[0].cwd, os.path.realpath(real_project))
        with mock.patch.dict(os.environ, {"HOME": home}):
            tilde_query = store.load_sessions(tilde_project)
        self.assertEqual([session.id for session in tilde_query.sessions], ["via-tilde"])

    def test_updated_timestamp_display(self) -> None:
        self.assertNotEqual(app.format_updated_at(0), "未知时间")
        self.assertEqual(app.format_updated_at(None), "未知时间")
        self.assertEqual(app.format_updated_at("nonsense"), "未知时间")
        formatted = app.format_updated_at(1_700_000_000)
        self.assertRegex(formatted, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


class CommandAndClipboardTests(unittest.TestCase):
    def test_resume_argv_exact_order_and_spaces(self) -> None:
        directory = "/tmp/Project With Spaces"
        self.assertEqual(
            app.build_resume_argv("session-123", directory),
            [
                app.DEFAULT_LAUNCHER,
                "resume",
                "session-123",
                "-C",
                directory,
            ],
        )
        self.assertIsInstance(app.build_resume_argv("x", directory), list)

    def test_new_argv_and_empty_session_validation(self) -> None:
        directory = "/tmp/Project With Spaces"
        self.assertEqual(
            app.build_new_argv(directory),
            [app.DEFAULT_LAUNCHER, "-C", directory],
        )
        with self.assertRaises(ValueError):
            app.build_resume_argv("", directory)

    @staticmethod
    def _recording_runner(calls, returncode=0, stderr=""):
        class Completed:
            pass

        def runner(argv, **kwargs):
            completed = Completed()
            completed.returncode = returncode
            completed.stderr = stderr
            calls.append((argv, kwargs))
            return completed

        return runner

    def test_clipboard_uses_pbcopy_on_macos_without_shell(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        runner = self._recording_runner(calls)

        with mock.patch.object(app.sys, "platform", "darwin"), mock.patch.object(
            app.shutil, "which", return_value=None
        ) as which:
            self.assertTrue(app.copy_to_clipboard("/tmp/中文 path", runner=runner))
            which.assert_not_called()

        self.assertEqual(calls[0][0], [app.PCOPY_COMMAND])
        self.assertEqual(calls[0][1]["input"], "/tmp/中文 path")
        self.assertFalse(calls[0][1]["check"])
        self.assertFalse(calls[0][1]["shell"])

    def test_clipboard_wayland_uses_existing_wl_copy(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        runner = self._recording_runner(calls)
        probed: list[str] = []

        def which(name: str) -> str | None:
            probed.append(name)
            return "/usr/bin/wl-copy" if name == "wl-copy" else None

        with mock.patch.object(app.sys, "platform", "linux"), mock.patch.object(
            app.shutil, "which", side_effect=which
        ), mock.patch.dict(
            os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True
        ):
            self.assertTrue(app.copy_to_clipboard("/tmp/x", runner=runner))

        self.assertEqual(calls[0][0], ["wl-copy"])
        self.assertEqual(calls[0][1]["input"], "/tmp/x")
        self.assertFalse(calls[0][1]["shell"])
        self.assertEqual(probed, ["wl-copy"])

    def test_clipboard_x11_uses_xclip_then_xsel_arguments(self) -> None:
        cases = [
            ("xclip", ["xclip", "-selection", "clipboard"]),
            ("xsel", ["xsel", "--clipboard", "--input"]),
        ]
        for tool, expected in cases:
            with self.subTest(tool=tool):
                calls: list[tuple[list[str], dict[str, object]]] = []
                runner = self._recording_runner(calls)

                def which(name: str, chosen: str = tool) -> str | None:
                    return "/usr/bin/" + name if name == chosen else None

                with mock.patch.object(app.sys, "platform", "linux"), mock.patch.object(
                    app.shutil, "which", side_effect=which
                ), mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
                    self.assertTrue(app.copy_to_clipboard("/tmp/x", runner=runner))

                self.assertEqual(calls[0][0], expected)
                self.assertFalse(calls[0][1]["shell"])

    def test_clipboard_without_display_reports_clear_error(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        runner = self._recording_runner(calls)

        def which(name: str) -> str | None:
            self.fail("无显示会话时不应探测 X/Wayland 剪贴板工具：" + name)

        with mock.patch.object(app.sys, "platform", "linux"), mock.patch.object(
            app.shutil, "which", side_effect=which
        ), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(app.ClipboardError) as caught:
                app.copy_to_clipboard("/tmp/x", runner=runner)

        self.assertEqual(calls, [])
        message = str(caught.exception)
        self.assertIn("DISPLAY", message)
        self.assertIn("WAYLAND_DISPLAY", message)

    def test_clipboard_missing_tool_suggests_install(self) -> None:
        with mock.patch.object(app.sys, "platform", "linux"), mock.patch.object(
            app.shutil, "which", return_value=None
        ), mock.patch.dict(
            os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True
        ):
            with self.assertRaises(app.ClipboardError) as caught:
                app.copy_to_clipboard("/tmp/x", runner=lambda *a, **k: None)
        self.assertIn("wl-clipboard", str(caught.exception))

        with mock.patch.object(app.sys, "platform", "linux"), mock.patch.object(
            app.shutil, "which", return_value=None
        ), mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            with self.assertRaises(app.ClipboardError) as caught:
                app.copy_to_clipboard("/tmp/x", runner=lambda *a, **k: None)
        self.assertIn("xclip", str(caught.exception))
        self.assertIn("xsel", str(caught.exception))

    def test_clipboard_error_is_reported(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        failed = self._recording_runner(calls, returncode=1, stderr="clipboard unavailable\n")
        with mock.patch.object(app.sys, "platform", "darwin"):
            with self.assertRaises(app.ClipboardError) as caught:
                app.copy_to_clipboard("/tmp/x", runner=failed)
        self.assertEqual(calls[0][0], [app.PCOPY_COMMAND])
        self.assertIn("pbcopy", str(caught.exception))
        self.assertIn("clipboard unavailable", str(caught.exception))

        def missing(*args: object, **kwargs: object) -> object:
            raise FileNotFoundError("pbcopy")

        with mock.patch.object(app.sys, "platform", "darwin"):
            with self.assertRaises(app.ClipboardError) as caught:
                app.copy_to_clipboard("/tmp/x", runner=missing)
        self.assertIn("pbcopy", str(caught.exception))


class DirectoryTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp(self, prefix=".test-tree-")
        self.root = os.path.join(self.tmp, "root with spaces")
        os.makedirs(self.root)
        self.alpha = os.path.join(self.root, "alpha")
        self.beta = os.path.join(self.root, "beta")
        self.hidden = os.path.join(self.root, ".hidden")
        self.alpha_child = os.path.join(self.alpha, "child")
        for directory in (self.alpha, self.beta, self.hidden, self.alpha_child):
            os.makedirs(directory)
        with open(os.path.join(self.root, "a-file.txt"), "w", encoding="utf-8") as handle:
            handle.write("not a directory")

    def names(self, tree: app.DirectoryTree) -> list[str]:
        return [node.name for node in tree.visible_nodes]

    def test_lazy_nonrecursive_root_includes_files(self) -> None:
        tree = app.DirectoryTree(self.root)
        self.assertEqual(
            self.names(tree),
            ["root with spaces", "alpha", "beta", "a-file.txt"],
        )
        self.assertEqual(tree.selected_index, 0)
        self.assertTrue(tree.root.expanded)
        self.assertNotIn("child", self.names(tree))
        self.assertNotIn(".hidden", self.names(tree))
        self.assertIn("a-file.txt", self.names(tree))
        self.assertFalse(tree.visible_nodes[-1].is_directory)

    def test_directories_precede_files_and_hidden_files_toggle(self) -> None:
        for name in ("Zeta 文件.txt", "b-文件.txt", ".secret 文件.txt"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        tree = app.DirectoryTree(self.root)
        self.assertEqual(
            self.names(tree),
            [
                "root with spaces",
                "alpha",
                "beta",
                "a-file.txt",
                "b-文件.txt",
                "Zeta 文件.txt",
            ],
        )
        self.assertTrue(tree.visible_nodes[1].is_directory)
        self.assertFalse(tree.visible_nodes[-1].is_directory)

        self.assertTrue(tree.toggle_hidden())
        self.assertEqual(
            self.names(tree),
            [
                "root with spaces",
                ".hidden",
                "alpha",
                "beta",
                ".secret 文件.txt",
                "a-file.txt",
                "b-文件.txt",
                "Zeta 文件.txt",
            ],
        )

    def test_selected_file_does_not_expand_and_h_returns_to_parent(self) -> None:
        file_path = os.path.join(self.root, "a-file.txt")
        tree = app.DirectoryTree(self.root)
        self.assertTrue(tree.select_path(file_path))
        node = tree.selected_node
        self.assertFalse(node.is_directory)
        self.assertFalse(node.is_symlink)
        with mock.patch.object(app.os, "scandir") as scandir:
            self.assertEqual(tree.expand_selected(), "noop")
            self.assertEqual(tree.toggle_selected(), "noop")
        scandir.assert_not_called()
        self.assertEqual(tree.selected_path, file_path)
        self.assertFalse(node.expanded)
        self.assertEqual(node.children, [])
        self.assertEqual(tree.collapse_or_parent(), "parent")
        self.assertEqual(tree.selected_path, tree.root_path)

    def test_refresh_preserves_selected_file_and_deletion_uses_visible_parent(self) -> None:
        nested = os.path.join(self.alpha, "草稿 文件.txt")
        with open(nested, "w", encoding="utf-8") as handle:
            handle.write("x")
        tree = app.DirectoryTree(self.root)
        tree.select_path(self.alpha)
        self.assertEqual(tree.expand_selected(), "expanded")
        self.assertTrue(tree.select_path(nested))
        tree.refresh()
        self.assertEqual(tree.selected_path, nested)
        self.assertFalse(tree.selected_node.is_directory)

        os.remove(nested)
        tree.refresh()
        self.assertEqual(tree.selected_path, self.alpha)
        self.assertEqual(tree.selected_node.path, self.alpha)
        self.assertTrue(tree.selected_node.expanded)

    def test_external_file_symlink_keeps_visible_parent_and_link_path(self) -> None:
        external = os.path.join(self.tmp, "外部 文件.txt")
        with open(external, "w", encoding="utf-8") as handle:
            handle.write("x")
        link = os.path.join(self.root, "外部 链接.txt")
        os.symlink(external, link)
        visible_parent = os.path.realpath(self.root)

        tree = app.DirectoryTree(self.root)
        self.assertTrue(tree.select_path(link))
        node = tree.selected_node
        self.assertTrue(node.is_symlink)
        self.assertFalse(node.is_directory)
        self.assertEqual(tree.selected_path, link)
        self.assertEqual(tree.selected_directory, visible_parent)

        browser = app.BrowserApp(mock.Mock(), self.root, self.root)
        self.assertTrue(browser.tree.select_path(link))
        with mock.patch.object(app, "copy_to_clipboard") as clipboard:
            browser._copy_selected_directory()
        clipboard.assert_called_once_with(link)
        self.assertIn("文件", browser.status_message or "")

        session = app.SessionInfo("s-1", visible_parent, "标题", 0, "2026-01-01 00:00")
        with mock.patch.object(browser, "_selected_session", return_value=session), \
                mock.patch.object(browser, "_run_launcher") as launcher:
            browser._launch_new()
            browser._launch_resume()
        new_argv = launcher.call_args_list[0].args[0]
        resume_argv = launcher.call_args_list[1].args[0]
        self.assertEqual(new_argv, app.build_new_argv(visible_parent, browser.launcher))
        self.assertEqual(
            resume_argv,
            app.build_resume_argv("s-1", visible_parent, browser.launcher),
        )

        browser.workspace = mock.Mock()
        browser.role = "directories"
        browser._session_directory = None
        empty = app.SessionQuery(database=browser.store.database_path)
        with mock.patch.object(browser.store, "load_sessions", return_value=empty) as load:
            browser._sync_directory()
        browser.workspace.set_directory.assert_called_once_with(visible_parent)
        load.assert_called_once_with(visible_parent)

    def test_parent_root_and_filesystem_boundary(self) -> None:
        tree = app.DirectoryTree(os.path.join(self.root, "alpha"), show_hidden=True)
        self.assertTrue(tree.parent_root())
        self.assertEqual(tree.root_path, self.root)
        self.assertTrue(tree.show_hidden)
        self.assertEqual(tree.selected_path, self.root)
        tree.root.expanded = False
        self.assertEqual(tree.collapse_or_parent(), "root-parent")
        tree = app.DirectoryTree(os.path.abspath(os.sep))
        self.assertFalse(tree.parent_root())
        tree.root.expanded = False
        self.assertEqual(tree.collapse_or_parent(), "noop")

    def test_expand_collapse_parent_and_hidden_toggle(self) -> None:
        tree = app.DirectoryTree(self.root)
        self.assertTrue(tree.select_index(1))
        self.assertEqual(tree.selected_node.name, "alpha")
        self.assertEqual(tree.expand_selected(), "expanded")
        self.assertIn("child", self.names(tree))
        self.assertEqual(tree.collapse_or_parent(), "collapsed")
        self.assertNotIn("child", self.names(tree))
        self.assertEqual(tree.collapse_or_parent(), "parent")
        self.assertEqual(tree.selected_node.path, tree.root_path)

        tree.set_show_hidden(True)
        self.assertIn(".hidden", self.names(tree))
        hidden_index = self.names(tree).index(".hidden")
        tree.select_index(hidden_index)
        tree.set_show_hidden(False)
        self.assertNotIn(".hidden", self.names(tree))
        self.assertEqual(tree.selected_node.path, tree.root_path)

    def test_right_key_expands_then_enters_first_child(self) -> None:
        tree = app.DirectoryTree(self.root)
        tree.select_path(self.alpha)
        self.assertEqual(tree.expand_selected(), "expanded")
        self.assertTrue(tree.selected_node.expanded)
        self.assertEqual(tree.expand_selected(), "child")
        self.assertEqual(tree.selected_node.path, self.alpha_child)
        self.assertEqual(tree.collapse_or_parent(), "parent")
        self.assertEqual(tree.selected_node.path, self.alpha)

    def test_refresh_preserves_expansion_and_selection(self) -> None:
        tree = app.DirectoryTree(self.root)
        tree.select_path(self.alpha)
        tree.expand_selected()
        tree.select_path(self.alpha_child)
        tree.refresh()
        self.assertEqual(tree.selected_node.path, self.alpha_child)
        self.assertIn("child", self.names(tree))
        alpha = tree._find_node(self.alpha)
        self.assertIsNotNone(alpha)
        self.assertTrue(alpha.expanded)

    def test_symlink_loop_is_not_traversed(self) -> None:
        loop = os.path.join(self.root, "loop")
        os.symlink(self.root, loop)
        tree = app.DirectoryTree(self.root)
        loop_index = self.names(tree).index("loop")
        self.assertTrue(tree.select_index(loop_index))
        self.assertEqual(tree.toggle_selected(), "error")
        node = tree.selected_node
        self.assertIn("循环", node.scan_error or "")
        self.assertEqual(node.children, [])
        self.assertLess(len(tree.visible_nodes), 10)

    def test_permission_error_is_visible(self) -> None:
        denied = os.path.join(self.root, "denied")
        os.makedirs(denied)
        tree = app.DirectoryTree(self.root)
        tree.select_path(denied)
        original_scandir = os.scandir

        def guarded_scandir(path: object):
            if os.path.abspath(os.fspath(path)) == denied:
                raise PermissionError(13, "Permission denied", denied)
            return original_scandir(path)

        with mock.patch.object(app.os, "scandir", side_effect=guarded_scandir):
            result = tree.toggle_selected()
        self.assertEqual(result, "error")
        self.assertIn("权限", tree.selected_node.scan_error or "")
        self.assertEqual(tree.selected_node.children, [])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp(self, prefix=".test-cli-")
        self.project = os.path.join(self.tmp, "project with spaces")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.project)
        make_threads_db(
            self.state,
            [
                ("visible", self.project, "\x1b[31m标题\x07", 123, 0),
                ("archived", self.project, "归档", 999, 1),
                ("elsewhere", os.path.join(self.tmp, "elsewhere"), "别处", 222, 0),
            ],
        )

    def run_app(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "app.py"), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            cwd=self.tmp,
        )

    def test_help_is_noninteractive_and_mentions_options(self) -> None:
        result = self.run_app("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--list-json", result.stdout)
        self.assertIn("--state-dir", result.stdout)
        self.assertIn("zzb-codex-ui", result.stdout)

    def test_list_json_filters_and_escapes_titles(self) -> None:
        result = self.run_app(
            "--state-dir", self.state, "--list-json", self.project
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["directory"], os.path.realpath(self.project))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["sessions"][0]["id"], "visible")
        self.assertNotIn("\x1b", payload["sessions"][0]["title"])
        self.assertNotIn("\x07", payload["sessions"][0]["title"])
        self.assertNotIn("body", payload)
        self.assertEqual(payload["error"], None)

    def test_list_json_missing_database_is_explicit(self) -> None:
        missing_state = os.path.join(self.tmp, "no-db-state")
        os.makedirs(missing_state)
        result = self.run_app(
            "--state-dir", missing_state, "--list-json", self.project
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["missing_database"])
        self.assertEqual(payload["sessions"], [])

    def test_list_json_bad_path_and_db_error_return_json(self) -> None:
        bad_path = os.path.join(self.tmp, "does not exist")
        result = self.run_app(
            "--state-dir", self.state, "--list-json", bad_path
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["error"])

        broken_state = os.path.join(self.tmp, "broken")
        os.makedirs(os.path.join(broken_state, "db"))
        with open(os.path.join(broken_state, "db", "state_5.sqlite"), "wb") as handle:
            handle.write(b"not sqlite")
        result = self.run_app(
            "--state-dir", broken_state, "--list-json", self.project
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["error"])


class CliRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp(self, prefix=".test-route-")
        self.project = os.path.join(self.tmp, "project with spaces")
        os.makedirs(self.project)

    def test_ui_routes_resume_and_cd(self) -> None:
        cwd = os.path.realpath(os.getcwd())
        project = os.path.realpath(self.project)
        cases = [
            ([], cwd),
            (["resume"], cwd),
            (["resume", "-C", self.project], project),
            (["-C", self.project, "resume"], project),
            (["resume", "--cd=" + self.project], project),
            (["-C" + self.project, "resume"], project),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(app, "_run_tui", return_value=0) as tui, \
                        mock.patch.object(app, "_run_native") as native:
                    self.assertEqual(app.main(argv), 0)
                tui.assert_called_once_with(expected, app.DEFAULT_STATE_DIR)
                native.assert_not_called()

    def test_resume_keyword_precedes_local_directory_and_ids(self) -> None:
        old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old_cwd)
        os.makedirs(os.path.join(self.tmp, "resume"))
        os.makedirs(os.path.join(self.tmp, "session-as-dir"))

        with mock.patch.object(app, "_run_tui", return_value=0) as tui, \
                mock.patch.object(app, "_run_native") as native:
            self.assertEqual(app.main(["resume"]), 0)
        tui.assert_called_once_with(os.path.realpath(self.tmp), app.DEFAULT_STATE_DIR)
        native.assert_not_called()

        with mock.patch.object(app, "_run_tui", return_value=0) as tui, \
                mock.patch.object(app, "_run_native") as native:
            self.assertEqual(app.main(["./resume"]), 0)
        tui.assert_called_once_with(
            os.path.realpath(os.path.join(self.tmp, "resume")),
            app.DEFAULT_STATE_DIR,
        )
        native.assert_not_called()

        with mock.patch("workspace.launch_workspace", return_value=5) as launch:
            self.assertEqual(app.main(["resume", "session-as-dir", "prompt"]), 5)
        launch.assert_called_once_with(
            os.path.realpath(self.tmp), app.DEFAULT_STATE_DIR,
            initial_argv=[app.DEFAULT_LAUNCHER, "resume", "session-as-dir", "prompt"],
        )

    def test_native_passthrough_preserves_argv(self) -> None:
        old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old_cwd)
        os.makedirs(os.path.join(self.tmp, "existing session"))
        cases = [
            ["resume", "--help"],
            ["--help", "resume"],
            ["-m", "resume", "exec", "hello"],
            ["检查 /tmp 的代码"],
            ["exec", "--json", "prompt with spaces"],
            ["login"],
            ["--version"],
            ["-V"],
            ["--model", "gpt-5", "hello"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(app, "_run_native", return_value=23) as native, \
                        mock.patch.object(app, "_run_tui") as tui:
                    self.assertEqual(app.main(argv), 23)
                native.assert_called_once_with(argv)
                tui.assert_not_called()

    def test_cd_and_directory_conflict_is_reported(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                mock.patch.object(app, "_run_tui") as tui, \
                mock.patch.object(app, "_run_native") as native:
            self.assertEqual(app.main(["-C", self.project, self.tmp]), 2)
        self.assertIn("不能同时", stderr.getvalue())
        tui.assert_not_called()
        native.assert_not_called()

    def test_run_native_returns_child_code_with_inherited_stdio(self) -> None:
        completed = mock.Mock(returncode=17)
        with mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                app._run_native(["resume", "session with spaces"]), 17
            )
        run.assert_called_once_with(
            [app.DEFAULT_LAUNCHER, "resume", "session with spaces"],
            check=False,
        )

    def test_run_native_oserror_and_interrupt(self) -> None:
        with mock.patch.object(
            app.subprocess,
            "run",
            side_effect=OSError(13, "Permission denied", app.DEFAULT_LAUNCHER),
        ):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                code = app._run_native(["login"])
        self.assertNotEqual(code, 0)
        self.assertIn("无法启动", stderr.getvalue())
        self.assertIn(app.DEFAULT_LAUNCHER, stderr.getvalue())

        with mock.patch.object(app.subprocess, "run", side_effect=KeyboardInterrupt):
            self.assertEqual(app._run_native(["login"]), 130)


class BrowserInteractionTests(unittest.TestCase):
    def test_native_double_click_copies_directory(self):
        root = make_temp(self)
        browser = app.BrowserApp(mock.Mock(), root, root)
        browser._layout.left = app.Rect(0, 0, 10, 30)
        with mock.patch.object(app.curses, 'getmouse', return_value=(0, 2, 1, 0, app.curses.BUTTON1_DOUBLE_CLICKED)), mock.patch.object(browser, '_copy_selected_directory') as copy:
            browser._handle_mouse()
        copy.assert_called_once()

    def test_file_y_enter_and_double_click_copy_without_launcher(self) -> None:
        root = make_temp(self)
        file_path = os.path.join(root, "说明 文件.txt")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        browser = app.BrowserApp(mock.Mock(), root, root)
        browser.focus = app.LEFT
        browser._layout.left = app.Rect(0, 0, 10, 30)
        self.assertTrue(browser.tree.select_path(file_path))
        file_index = [node.path for node in browser.tree.visible_nodes].index(file_path)
        mouse_y = file_index + 1

        with (
            mock.patch.object(app, "copy_to_clipboard") as clipboard,
            mock.patch.object(browser, "_sync_directory"),
            mock.patch.object(browser, "_run_launcher") as launcher,
        ):
            with mock.patch.object(
                app.curses,
                "getmouse",
                return_value=(0, 1, mouse_y, 0, app.curses.BUTTON1_CLICKED),
            ):
                browser._handle_mouse()
            self.assertEqual(browser.tree.selected_path, file_path)
            self.assertFalse(browser.tree.selected_node.expanded)
            clipboard.assert_not_called()

            browser._handle_key(ord("y"))
            clipboard.assert_called_once_with(file_path)

            browser._handle_key(ord("\n"))
            self.assertEqual(
                clipboard.call_args_list,
                [mock.call(file_path), mock.call(file_path)],
            )
            self.assertFalse(browser.tree.selected_node.expanded)

            browser._last_dir_click = None
            with mock.patch.object(
                app.curses,
                "getmouse",
                return_value=(0, 1, mouse_y, 0, app.curses.BUTTON1_DOUBLE_CLICKED),
            ):
                browser._handle_mouse()
            self.assertEqual(clipboard.call_args_list, [mock.call(file_path)] * 3)
            launcher.assert_not_called()

    def test_resume_handoff_and_return(self):
        root = make_temp(self)
        browser = app.BrowserApp(mock.Mock(), root, root)
        events = []
        with mock.patch.object(browser, '_suspend_terminal', side_effect=lambda: events.append('suspend')), mock.patch.object(browser, '_resume_terminal', side_effect=lambda: events.append('restore')), mock.patch.object(browser, '_refresh_data'), mock.patch.object(app.subprocess, 'run', side_effect=lambda *a, **k: (events.append('child') or mock.Mock(returncode=0))):
            browser._run_launcher(['fake-launcher'], '测试')
        self.assertEqual(events, ['suspend', 'child', 'restore'])


class PersistentWorkspaceTests(unittest.TestCase):
    def test_resume_options_keep_workspace(self):
        root = make_temp(self)
        cases = [
            (["resume", "--last"], os.path.realpath(os.getcwd())),
            (["resume", "--all"], os.path.realpath(os.getcwd())),
            (["resume", "session-id", "continue this"], os.path.realpath(os.getcwd())),
            (["-C", root, "resume", "id"], os.path.realpath(root)),
            (["resume", "id", "--cd=" + root], os.path.realpath(root)),
        ]
        for argv, directory in cases:
            with self.subTest(argv=argv), mock.patch("workspace.launch_workspace", return_value=0) as launch, mock.patch.object(app, "_run_native") as native:
                self.assertEqual(app.main(argv), 0)
                launch.assert_called_once_with(directory, app.DEFAULT_STATE_DIR,
                                               initial_argv=[app.DEFAULT_LAUNCHER, *argv])
                native.assert_not_called()

    def test_browser_opens_chat_without_suspending_navigation(self):
        root = make_temp(self)
        browser = app.BrowserApp(mock.Mock(), root, root)
        browser.workspace = mock.Mock()
        argv = app.build_resume_argv("session-id", root)
        with mock.patch.object(browser, "_suspend_terminal") as suspend, mock.patch.object(app.subprocess, "run") as run:
            browser._run_launcher(argv, "恢复会话")
            browser.workspace.open.assert_called_once_with(argv, root, key="resume:session-id")
            suspend.assert_not_called()
            run.assert_not_called()
        browser._handle_key(ord("q"))
        browser.workspace.detach.assert_called_once()
        self.assertTrue(browser.running)

    def test_workspace_errors_stay_visible_in_navigation(self):
        root = make_temp(self)
        browser = app.BrowserApp(mock.Mock(), root, root)
        browser.workspace = mock.Mock()
        browser.workspace.open.side_effect = RuntimeError("test failure")
        browser._run_launcher(["fake"], "测试")
        self.assertIn("test failure", browser.status_message)
        self.assertTrue(browser.running)


class SidebarSummaryTests(unittest.TestCase):
    def test_optional_summary_metadata_and_missing_values(self):
        root = make_temp(self)
        dbpath = make_threads_db(root, [("id", root, "Title", 100, 0)])
        store = app.ThreadStore.from_state_dir(root)
        legacy = store.load_sessions(root).sessions[0]
        self.assertIsNone(legacy.tokens_used)
        self.assertIn(("内容预览", "暂无记录"), app.summary_fields(legacy))
        with closing(sqlite3.connect(dbpath)) as db, db:
            for field, kind in [("preview", "TEXT"), ("model", "TEXT"), ("tokens_used", "INTEGER"), ("first_user_message", "TEXT")]:
                db.execute("ALTER TABLE threads ADD COLUMN " + field + " " + kind)
            db.execute("UPDATE threads SET preview=?, model=?, tokens_used=?, first_user_message=?", ("摘要内容", "test-model", 0, "首条请求"))
        session = store.load_sessions(root).sessions[0]
        self.assertIn(("内容预览", "摘要内容"), app.summary_fields(session))
        self.assertIn(("用量", "0 tokens"), app.summary_fields(session))
        self.assertEqual(session.model, "test-model")
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute("UPDATE threads SET preview=NULL, model=NULL, tokens_used=NULL")
        session = store.load_sessions(root).sessions[0]
        self.assertIn(("首条请求", "首条请求"), app.summary_fields(session))
        self.assertIn(("模型", "未记录"), app.summary_fields(session))

    def test_cjk_summary_wrap_has_safe_cell_width(self):
        text = "中文概要 with words" + chr(27) + "[31m"
        lines = app.wrap_sidebar_text(text, 12)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(app.display_width(line) <= 12 for line in lines))
        self.assertNotIn(chr(27), "".join(lines))

    def test_sidebars_share_selection_and_keep_their_role(self):
        root = make_temp(self)
        child = os.path.join(root, "子目录 with spaces")
        os.mkdir(child)
        with mock.patch.dict(os.environ, {"ZZB_UI_ROLE": "directories"}):
            left = app.BrowserApp(mock.Mock(), root, root)
        left.workspace = mock.Mock()
        left.tree.select_path(child)
        left._sync_directory()
        left.workspace.set_directory.assert_called_once_with(os.path.realpath(child))
        left._toggle_focus()
        left.workspace.focus_role.assert_called_once_with("sessions")
        self.assertEqual(left.focus, app.LEFT)
        with mock.patch.dict(os.environ, {"ZZB_UI_ROLE": "sessions"}):
            right = app.BrowserApp(mock.Mock(), root, root)
        right.workspace = mock.Mock()
        right.workspace.get_directory.return_value = child
        right._poll_directory()
        self.assertEqual(right.tree.selected_directory, os.path.realpath(child))
        self.assertEqual(right.focus, app.RIGHT)


if __name__ == "__main__":
    unittest.main()
