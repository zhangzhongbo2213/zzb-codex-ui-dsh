#!/usr/bin/env python3
"""zzb-codex-ui: a tiny, dependency-free terminal browser for zzb-codex sessions.

The program only reads thread metadata from the independent zzb-codex SQLite
database.  File preview reads only the explicitly selected file; session browsing does not read rollout bodies.
Interactive work is delegated to the existing ``zzb-codex`` launcher.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import locale
import math
import os
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Default installation paths (overridable via environment variables)
# ---------------------------------------------------------------------------

APP_TITLE = "zzb-codex-ui"
DEFAULT_STATE_DIR = os.path.expanduser(
    os.environ.get("ZZB_CODEX_HOME", "~/.zzb-codex")
)
DEFAULT_LAUNCHER = os.path.expanduser(os.environ.get(
    "ZZB_CODEX_LAUNCHER",
    os.path.join(
        os.environ.get("ZZB_CODEX_PREFIX", os.path.expanduser("~/local")),
        "bin",
        "zzb-codex",
    ),
))
DB_SUBDIR = ("db", "state_5.sqlite")
PCOPY_COMMAND = "pbcopy"
DOUBLE_CLICK_SECONDS = 0.35

LEFT = 0
RIGHT = 1

# ---------------------------------------------------------------------------
# Small pure helpers (path, text and time handling)
# ---------------------------------------------------------------------------


def normalize_path(path: object) -> str:
    """Expand ``~`` and turn *path* into a canonical absolute path."""
    raw = os.fsdecode(os.fspath(path))
    expanded = os.path.expanduser(raw)
    return os.path.realpath(os.path.abspath(expanded))


def state_database_path(state_dir: object) -> str:
    """Return the database path for a state directory (normally ~/.zzb-codex)."""
    root = os.path.abspath(os.path.expanduser(os.fsdecode(os.fspath(state_dir))))
    return os.path.join(root, *DB_SUBDIR)


def sqlite_readonly_uri(database_path: object) -> str:
    """Build a SQLite URI that guarantees a read-only connection."""
    path = os.path.abspath(os.fsdecode(os.fspath(database_path)))
    quoted = urllib.parse.quote(path, safe="/")
    return "file:" + quoted + "?mode=ro"


def sanitize_text(value: object, limit: int | None = 500) -> str:
    """Make untrusted text safe for a terminal and compact it to one line.

    C0/C1 controls, ESC, bidi/format controls and Unicode line separators are
    replaced by spaces.  This prevents database titles and file names from
    injecting terminal escape sequences.  Whitespace is collapsed so every
    returned value is suitable for a single status/title line.
    """
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else os.fsdecode(value) if isinstance(value, bytes) else str(value)
    except Exception:
        return ""
    chars = []
    for char in text:
        if char in "\t\r\n":
            chars.append(" ")
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            chars.append(" ")
        else:
            chars.append(char)
    clean = " ".join("".join(chars).split())
    if limit is not None and limit > 0 and len(clean) > limit:
        clean = clean[: max(0, limit - 1)].rstrip() + "…"
    return clean


def character_width(char: str) -> int:
    """Terminal cell width for one Unicode code point (no wcwidth dependency)."""
    if not char:
        return 0
    code = ord(char)
    if code == 0:
        return 0
    category = unicodedata.category(char)
    if category in {"Cc", "Cs"}:
        return 0
    if unicodedata.combining(char) or category in {"Mn", "Me", "Cf"}:
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def display_width(text: object) -> int:
    """Number of terminal cells occupied by *text*."""
    return sum(character_width(char) for char in str(text))


def truncate_display(text: object, max_width: int, ellipsis: str = "…") -> str:
    """Truncate by terminal cells so wide CJK characters are never split."""
    text = str(text)
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    ellipsis_width = display_width(ellipsis)
    if ellipsis_width > max_width:
        return "." * max_width
    budget = max_width - ellipsis_width
    kept: list[str] = []
    used = 0
    for char in text:
        width = character_width(char)
        if used + width > budget:
            break
        kept.append(char)
        used += width
    return "".join(kept).rstrip() + ellipsis


def _timestamp_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    seconds: float | None = None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            try:
                parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                seconds = parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
    if seconds is None or not math.isfinite(seconds):
        return None
    # The current schema stores seconds; tolerate millisecond timestamps too.
    if abs(seconds) >= 100_000_000_000:
        seconds /= 1000.0
    return seconds


def format_updated_at(value: object) -> str:
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return "未知时间"
    try:
        return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知时间"


def _json_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return sanitize_text(value, limit=1000)


def describe_os_error(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        detail = exc.strerror or "权限不足"
        return "权限不足：" + str(exc.filename or detail)
    if isinstance(exc, FileNotFoundError):
        return "目录不存在：" + str(exc.filename or exc)
    text = sanitize_text(str(exc), limit=500) or exc.__class__.__name__
    return text


# ---------------------------------------------------------------------------
# Read-only SQLite metadata store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionInfo:
    id: str
    cwd: str
    title: str
    updated_at: object
    updated_display: str
    preview: str = ""
    first_user_message: str = ""
    model: str = ""
    tokens_used: int | None = None
    reasoning_effort: str = ""

    @property
    def short_id(self) -> str:
        clean = sanitize_text(self.id, limit=12)
        return clean or "未知ID"


@dataclass
class SessionQuery:
    database: str
    sessions: list[SessionInfo] = field(default_factory=list)
    error: str | None = None
    missing_database: bool = False

    @property
    def count(self) -> int:
        return len(self.sessions)


class ThreadStore:
    """Read-only access to the independent zzb-codex thread database."""

    def __init__(self, database_path: object):
        self.database_path = os.path.abspath(os.fsdecode(os.fspath(database_path)))

    @classmethod
    def from_state_dir(cls, state_dir: object) -> "ThreadStore":
        return cls(state_database_path(state_dir))

    def _result(self, directory: str, **kwargs: object) -> SessionQuery:
        return SessionQuery(database=self.database_path, sessions=[], **kwargs)  # type: ignore[arg-type]

    def load_sessions(self, directory: object) -> SessionQuery:
        try:
            directory_key = normalize_path(directory)
        except (OSError, TypeError, ValueError) as exc:
            return self._result("", error="目录路径无效：" + describe_os_error(exc))

        try:
            info = os.stat(self.database_path)
        except FileNotFoundError:
            return self._result(directory_key, missing_database=True)
        except OSError as exc:
            return self._result(directory_key, error="无法访问数据库：" + describe_os_error(exc))
        if not stat.S_ISREG(info.st_mode):
            return self._result(directory_key, error="数据库路径不是普通文件：" + self.database_path)

        connection: sqlite3.Connection | None = None
        rows = []
        try:
            connection = sqlite3.connect(
                sqlite_readonly_uri(self.database_path), uri=True, timeout=2.0
            )
            connection.row_factory = sqlite3.Row
            available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            extras = [name for name in ("preview", "first_user_message", "model", "tokens_used", "reasoning_effort") if name in available]
            columns = ", ".join(["id", "cwd", "title", "updated_at", "archived", *extras])
            rows = connection.execute(
                "SELECT " + columns + " FROM threads WHERE COALESCE(archived, 0) = 0"
            ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            return self._result(directory_key, error="无法读取会话数据库：" + sanitize_text(exc, 500))
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

        sessions: list[SessionInfo] = []
        for row in rows:
            try:
                raw_id = row["id"]
                raw_cwd = row["cwd"]
                if raw_id is None or raw_cwd is None:
                    continue
                session_id = os.fsdecode(raw_id) if isinstance(raw_id, bytes) else str(raw_id)
                cwd_value = os.fsdecode(raw_cwd) if isinstance(raw_cwd, bytes) else str(raw_cwd)
                if not session_id or not cwd_value:
                    continue
                try:
                    cwd_key = normalize_path(cwd_value)
                except (OSError, TypeError, ValueError):
                    continue
                if cwd_key != directory_key:
                    continue
                raw_updated = row["updated_at"]
                if not isinstance(raw_updated, (str, int, float, bool, type(None))):
                    raw_updated = str(raw_updated)
                title = sanitize_text(row["title"], limit=500) or "（无标题）"
                sessions.append(
                    SessionInfo(
                        id=session_id,
                        cwd=cwd_key,
                        title=title,
                        updated_at=raw_updated,
                        updated_display=format_updated_at(raw_updated),
                        preview=sanitize_text(row["preview"] or "", 4000) if "preview" in row.keys() else "",
                        first_user_message=sanitize_text(row["first_user_message"] or "", 4000) if "first_user_message" in row.keys() else "",
                        model=sanitize_text(row["model"] or "", 100) if "model" in row.keys() else "",
                        reasoning_effort=sanitize_text(row["reasoning_effort"] or "", 40) if "reasoning_effort" in row.keys() else "",
                        tokens_used=row["tokens_used"] if "tokens_used" in row.keys() and isinstance(row["tokens_used"], int) else None,
                    )
                )
            except (TypeError, ValueError, OSError, UnicodeError):
                # One malformed metadata row must not blank the whole list.
                continue

        def sort_key(item: SessionInfo) -> tuple[float, str]:
            timestamp = _timestamp_seconds(item.updated_at)
            return (float("-inf") if timestamp is None else timestamp, item.id)

        sessions.sort(key=sort_key, reverse=True)
        return SessionQuery(database=self.database_path, sessions=sessions)


# ---------------------------------------------------------------------------
# File tree model (lazy, non-recursive directory reads)
# ---------------------------------------------------------------------------


@dataclass
class DirectoryNode:
    path: str
    name: str
    depth: int
    parent: "DirectoryNode | None" = field(default=None, repr=False, compare=False)
    expanded: bool = False
    loaded: bool = False
    scan_error: str | None = None
    children: list["DirectoryNode"] = field(default_factory=list)
    is_directory: bool = True
    is_symlink: bool = False

    @property
    def is_root(self) -> bool:
        return self.parent is None


class DirectoryTree:
    """Lazily expandable tree containing directories and file leaves.

    Each directory is read exactly once per expansion (or during an explicit
    refresh), using a non-recursive ``os.scandir``.  Symlinks which point back
    to an ancestor are still shown as rows but are not traversed further.
    """

    def __init__(self, root_path: object, show_hidden: bool = False):
        self.root_path = normalize_path(root_path)
        self.show_hidden = bool(show_hidden)
        self.root = self._make_root()
        self.selected_path = self.root.path
        self.root.expanded = True
        self._load_children(self.root)

    def _make_root(self) -> DirectoryNode:
        name = os.path.basename(self.root_path.rstrip(os.sep)) or self.root_path or os.sep
        return DirectoryNode(path=self.root_path, name=name, depth=0)

    def _make_node(self, path: str, name: str, depth: int, parent: DirectoryNode) -> DirectoryNode:
        return DirectoryNode(path=path, name=name, depth=depth, parent=parent)

    def _find_cycle_ancestor(self, node: DirectoryNode) -> DirectoryNode | None:
        parent = node.parent
        if parent is None:
            return None
        try:
            target = os.path.realpath(node.path)
        except OSError:
            return None
        ancestor: DirectoryNode | None = parent
        while ancestor is not None:
            try:
                if os.path.realpath(ancestor.path) == target:
                    return ancestor
            except OSError:
                pass
            ancestor = ancestor.parent
        return None

    def _load_children(self, node: DirectoryNode, force: bool = False) -> None:
        if not node.is_directory:
            return
        if node.loaded and not force:
            return
        node.scan_error = None
        node.children = []
        cycle = self._find_cycle_ancestor(node)
        if cycle is not None:
            node.loaded = True
            node.scan_error = "已跳过符号链接循环：" + cycle.path
            return
        try:
            with os.scandir(node.path) as iterator:
                entries = list(iterator)
        except OSError as exc:
            node.loaded = True
            node.scan_error = describe_os_error(exc)
            return

        children: list[DirectoryNode] = []
        for entry in entries:
            if not self.show_hidden and entry.name.startswith("."):
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=True)
                is_symlink = entry.is_symlink()
            except OSError:
                # Still show an unreadable/broken entry without traversing it.
                is_directory, is_symlink = False, entry.is_symlink()
            path = os.path.abspath(entry.path)
            child = self._make_node(path, entry.name, node.depth + 1, node)
            child.is_directory = is_directory
            child.is_symlink = is_symlink
            children.append(child)
        children.sort(key=lambda child: (not child.is_directory, child.name.casefold(), child.name))
        node.children = children
        node.loaded = True

    @property
    def visible_nodes(self) -> list[DirectoryNode]:
        result: list[DirectoryNode] = []

        def walk(node: DirectoryNode) -> None:
            result.append(node)
            if node.expanded:
                for child in node.children:
                    walk(child)

        walk(self.root)
        return result

    def _all_nodes(self) -> list[DirectoryNode]:
        result: list[DirectoryNode] = []

        def walk(node: DirectoryNode) -> None:
            result.append(node)
            for child in node.children:
                walk(child)

        walk(self.root)
        return result

    def _find_node(self, path: str) -> DirectoryNode | None:
        for node in self._all_nodes():
            if node.path == path:
                return node
        return None

    @property
    def selected_node(self) -> DirectoryNode:
        node = self._find_node(self.selected_path)
        if node is None:
            node = self.root
            self.selected_path = node.path
        return node

    @property
    def selected_index(self) -> int:
        for index, node in enumerate(self.visible_nodes):
            if node.path == self.selected_path:
                return index
        return 0

    @property
    def selected_directory(self) -> str:
        node = self.selected_node
        path = node.path if node.is_directory else os.path.dirname(node.path)
        try:
            return normalize_path(path)
        except (OSError, ValueError):
            return path

    def select_index(self, index: int) -> bool:
        nodes = self.visible_nodes
        if index < 0 or index >= len(nodes):
            return False
        self.selected_path = nodes[index].path
        return True

    def select_path(self, path: str) -> bool:
        for node in self.visible_nodes:
            if node.path == path:
                self.selected_path = node.path
                return True
        return False

    def move(self, delta: int) -> bool:
        nodes = self.visible_nodes
        if not nodes:
            return False
        current = self.selected_index
        target = max(0, min(len(nodes) - 1, current + delta))
        self.selected_path = nodes[target].path
        return target != current

    def move_to_first(self) -> None:
        nodes = self.visible_nodes
        if nodes:
            self.selected_path = nodes[0].path

    def move_to_last(self) -> None:
        nodes = self.visible_nodes
        if nodes:
            self.selected_path = nodes[-1].path

    def _expand_node(self, node: DirectoryNode) -> bool:
        if not node.is_directory:
            return False
        if node.expanded:
            return True
        self._load_children(node)
        node.expanded = True
        return node.scan_error is None

    def toggle_selected(self) -> str:
        """Fold or unfold the selected directory without moving selection."""
        node = self.selected_node
        if not node.is_directory:
            return "noop"
        if node.expanded:
            node.expanded = False
            return "collapsed"
        self._expand_node(node)
        return "error" if node.scan_error else "expanded"

    def expand_selected(self) -> str:
        """``l``/right: expand, then step into the first child if already open."""
        node = self.selected_node
        if not node.is_directory:
            return "noop"
        if node.expanded:
            if node.children:
                self.selected_path = node.children[0].path
                return "child"
            return "noop"
        self._expand_node(node)
        return "error" if node.scan_error else "expanded"

    def collapse_or_parent(self) -> str:
        """``h``/left: fold the selection, or move to its parent."""
        node = self.selected_node
        if node.expanded:
            node.expanded = False
            return "collapsed"
        if node.parent is not None:
            self.selected_path = node.parent.path
            return "parent"
        return "noop"

    def set_show_hidden(self, show_hidden: bool) -> None:
        show_hidden = bool(show_hidden)
        if show_hidden == self.show_hidden:
            return
        self.show_hidden = show_hidden
        self._rebuild(preserve=True)

    def toggle_hidden(self) -> bool:
        self.set_show_hidden(not self.show_hidden)
        return self.show_hidden

    def refresh(self) -> None:
        self._rebuild(preserve=True)

    def _rebuild(self, preserve: bool) -> None:
        old_expanded = {
            node.path for node in self._all_nodes() if node.expanded
        } if preserve else {self.root.path}
        old_selected = self.selected_path if preserve else self.root_path
        fallback_candidates: list[str] = []
        old_selected_node = self._find_node(old_selected)
        if old_selected_node is not None:
            cursor: DirectoryNode | None = old_selected_node
            while cursor is not None:
                fallback_candidates.append(cursor.path)
                cursor = cursor.parent
        else:
            fallback_candidates.append(self.root_path)
        old_root_expanded = self.root.expanded if preserve else True

        self.root = self._make_root()
        self.root.expanded = old_root_expanded
        if self.root.expanded:
            self._load_children(self.root)

            def restore(node: DirectoryNode) -> None:
                for child in node.children:
                    if not child.is_directory or child.path not in old_expanded:
                        continue
                    self._load_children(child)
                    child.expanded = True
                    restore(child)

            restore(self.root)

        visible_paths = {node.path for node in self.visible_nodes}
        chosen = self.root.path
        for candidate in fallback_candidates:
            if candidate in visible_paths:
                chosen = candidate
                break
        self.selected_path = chosen


# ---------------------------------------------------------------------------
# Command construction, clipboard and list-json data
# ---------------------------------------------------------------------------


def build_resume_argv(
    session_id: object, directory: object, launcher: object = DEFAULT_LAUNCHER
) -> list[str]:
    """Build an argv list.  This is always executed without a shell."""
    sid = os.fsdecode(os.fspath(session_id)) if isinstance(session_id, bytes) else str(session_id)
    if not sid:
        raise ValueError("会话 ID 不能为空")
    path = os.fsdecode(os.fspath(directory))
    return [os.fsdecode(os.fspath(launcher)), "resume", sid, "-C", path]


def build_new_argv(directory: object, launcher: object = DEFAULT_LAUNCHER) -> list[str]:
    path = os.fsdecode(os.fspath(directory))
    return [os.fsdecode(os.fspath(launcher)), "-C", path]


class ClipboardError(RuntimeError):
    pass


def _clipboard_command() -> list[str]:
    """Return the clipboard argv list for the current platform."""
    if sys.platform == "darwin":
        return [PCOPY_COMMAND]
    if sys.platform.startswith("linux"):
        wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        if wayland and shutil.which("wl-copy"):
            return ["wl-copy"]
        if os.environ.get("DISPLAY"):
            if shutil.which("xclip"):
                return ["xclip", "-selection", "clipboard"]
            if shutil.which("xsel"):
                return ["xsel", "--clipboard", "--input"]
            raise ClipboardError(
                "未找到可用的 X11 剪贴板工具：请安装 xclip"
                "（如 sudo apt install xclip）或 xsel"
                "（如 sudo apt install xsel）"
            )
        if wayland:
            raise ClipboardError(
                "Wayland 会话中未找到 wl-copy：请安装 wl-clipboard"
                "（如 sudo apt install wl-clipboard）"
            )
        raise ClipboardError(
            "未检测到图形显示会话（DISPLAY 或 WAYLAND_DISPLAY 未设置）："
            "无法复制到剪贴板。请在图形终端中运行，"
            "或配置 wl-clipboard、xclip 或 xsel"
        )
    raise ClipboardError("当前系统不支持剪贴板复制：" + sys.platform)


def copy_to_clipboard(text: object, runner: object = None) -> bool:
    """Copy *text* to the platform clipboard with an argv list and no shell."""
    payload = str(text)
    if not payload:
        raise ClipboardError("没有可复制的路径")
    command = _clipboard_command()
    tool = os.path.basename(command[0])
    command_runner = runner or subprocess.run
    try:
        completed = command_runner(
            command,
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
    except OSError as exc:
        detail = sanitize_text(getattr(exc, "strerror", None) or str(exc), limit=300)
        raise ClipboardError("无法调用 " + tool + "：" + detail) from exc
    if getattr(completed, "returncode", 1) != 0:
        detail = sanitize_text(getattr(completed, "stderr", ""), limit=300)
        raise ClipboardError(tool + " 失败" + (("：" + detail) if detail else ""))
    return True


def list_json_payload(directory: str, state_dir: object) -> dict[str, object]:
    store = ThreadStore.from_state_dir(state_dir)
    query = store.load_sessions(directory)
    return {
        "directory": directory,
        "database": store.database_path,
        "missing_database": query.missing_database,
        "error": query.error,
        "count": query.count,
        "sessions": [
            {
                "id": session.id,
                "cwd": session.cwd,
                "title": session.title,
                "updated_at": _json_scalar(session.updated_at),
                "updated": session.updated_display,
            }
            for session in query.sessions
        ],
    }


# ---------------------------------------------------------------------------
# Curses theme and geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    height: int
    width: int

    def contains(self, y: int, x: int) -> bool:
        return self.y <= y < self.y + self.height and self.x <= x < self.x + self.width

    def inner_contains(self, y: int, x: int) -> bool:
        return (
            self.y + 1 <= y < self.y + self.height - 1
            and self.x + 1 <= x < self.x + self.width - 1
        )


@dataclass
class ViewLayout:
    left: Rect | None = None
    right: Rect | None = None
    compact: bool = False


# Classify names only: drawing the tree never opens files to inspect content.
_FILE_SUFFIX_GROUPS = {
    "file_code": ".py .pyi .js .jsx .mjs .cjs .ts .tsx .vue .svelte .html .htm .css .scss .sass .less .c .h .cc .cpp .hpp .rs .go .java .kt .kts .swift .m .mm .rb .php .lua .r .jl .sql .sh .bash .zsh .fish .ps1 .bat .cmd .ipynb",
    "file_document": ".md .markdown .mdx .rst .txt .pdf .doc .docx .odt .rtf .tex .bib .epub .ppt .pptx .odp",
    "file_config": ".json .jsonc .json5 .yaml .yml .toml .ini .conf .cfg .xml .env .properties .lock .editorconfig",
    "file_image": ".png .jpg .jpeg .gif .webp .svg .ico .bmp .tif .tiff .heic .avif .psd .ai .sketch",
    "file_media": ".mp3 .wav .flac .aac .ogg .m4a .aiff .opus .mp4 .mov .mkv .webm .avi .m4v .srt .vtt",
    "file_archive": ".zip .tar .gz .tgz .bz2 .xz .zst .7z .rar .dmg .iso .pkg .deb .rpm .whl .jar",
    "file_data": ".csv .tsv .xls .xlsx .ods .parquet .arrow .feather .sqlite .sqlite3 .db .h5 .hdf5 .npy .npz .pkl .pickle .rds",
}
_FILE_SUFFIX_STYLES = {
    suffix: style for style, suffixes in _FILE_SUFFIX_GROUPS.items()
    for suffix in suffixes.split()
}
_FILE_NAME_STYLES = {
    **dict.fromkeys(("makefile", "gnumakefile", "dockerfile", "containerfile", "justfile",
                     "rakefile", ".bashrc", ".bash_profile", ".zshrc", ".zprofile",
                     ".profile"), "file_code"),
    **dict.fromkeys(("readme", "license", "licence", "changelog", "authors", "notice"),
                    "file_document"),
    **dict.fromkeys((".env", ".gitignore", ".gitattributes", ".gitmodules", ".npmrc",
                     ".yarnrc", ".editorconfig", ".dockerignore", ".prettierrc",
                     ".eslintrc"), "file_config"),
}


def tree_node_style(node: DirectoryNode) -> str:
    if node.is_directory:
        return "directory"
    if node.is_symlink:
        return "symlink"
    name = node.name.casefold()
    if name.startswith(".env."):
        return "file_config"
    return _FILE_NAME_STYLES.get(name) or _FILE_SUFFIX_STYLES.get(os.path.splitext(name)[1], "file")


# Fixed xterm colours: no palette redefinition and no bold/bright ANSI colours.
# Selected variants keep their semantic hue on a quiet charcoal highlight.
_FOREGROUNDS = {
    "text": 250, "muted": 245, "accent": 109, "header": 109,
    "warning": 137, "error": 138, "good": 108,
    "border_active": 244, "border_inactive": 239, "help_heading": 139,
    "directory": 109, "directory_arrow": 245, "file": 245, "symlink": 103,
    "file_code": 108, "file_document": 250, "file_config": 137,
    "file_image": 139, "file_media": 110, "file_archive": 138, "file_data": 144,
    "session": 139,
    "date": 137, "identifier": 245, "model": 103, "usage": 108,
    "preview": 250, "path": 109, "label": 245,
}
_SELECTED_FOREGROUNDS = {
    "text": 252, "muted": 247, "directory": 109, "directory_arrow": 247,
    "file": 247, "symlink": 146,
    "file_code": 108, "file_document": 250, "file_config": 180,
    "file_image": 146, "file_media": 110, "file_archive": 181, "file_data": 144,
    "session": 146, "date": 144, "identifier": 247,
    "warning": 144, "error": 181,
}
_PAIR_SPECS = (
    [(name, color, 234, 0) for name, color in _FOREGROUNDS.items()]
    + [("selected", 252, 237, 0), ("selected_inactive", 250, 235, 0)]
    + [("selected_" + name, color, 237, 0)
       for name, color in _SELECTED_FOREGROUNDS.items()]
    + [("selected_inactive_" + name, _FOREGROUNDS[name], 235, 0)
       for name in _SELECTED_FOREGROUNDS]
)

_FALLBACK_ATTRS = {
    "text": 0,
    "muted": curses.A_DIM,
    "accent": curses.A_BOLD,
    "selected": curses.A_REVERSE | curses.A_BOLD,
    "selected_inactive": curses.A_REVERSE,
    "header": curses.A_REVERSE | curses.A_BOLD,
    "warning": curses.A_BOLD,
    "error": curses.A_BOLD,
    "good": curses.A_BOLD,
    "border_active": curses.A_BOLD,
    "border_inactive": curses.A_DIM,
    "help_heading": curses.A_BOLD,
}
for _name in _FOREGROUNDS:
    _FALLBACK_ATTRS.setdefault(_name, 0)
for _name in _SELECTED_FOREGROUNDS:
    _FALLBACK_ATTRS["selected_" + _name] = curses.A_REVERSE | curses.A_BOLD
    _FALLBACK_ATTRS["selected_inactive_" + _name] = curses.A_REVERSE


class Theme:
    def __init__(self) -> None:
        self.use_color = False
        self._attrs: dict[str, int] = {}
        try:
            if curses.has_colors():
                curses.start_color()
                if curses.COLORS >= 256 and curses.COLOR_PAIRS > len(_PAIR_SPECS):
                    for index, (name, foreground, background, extra) in enumerate(_PAIR_SPECS, 1):
                        curses.init_pair(index, foreground, background)
                        self._attrs[name] = curses.color_pair(index) | extra
                    self.use_color = True
                else:
                    # Small palettes vary with terminal settings; use readable
                    # monochrome rather than remapping muted colours to neon.
                    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
                    self._attrs = {name: curses.color_pair(1) | attr
                                   for name, attr in _FALLBACK_ATTRS.items()}
                    self.use_color = True
        except curses.error:
            self.use_color = False
            self._attrs.clear()
        if not self.use_color:
            self._attrs = dict(_FALLBACK_ATTRS)

    def attr(self, name: str) -> int:
        return self._attrs.get(name, 0)


def ensure_visible(selected: int, count: int, viewport: int, offset: int) -> int:
    """Return a clamped scroll offset which keeps *selected* visible."""
    viewport = max(1, viewport)
    if count <= 0:
        return 0
    offset = max(0, min(offset, max(0, count - viewport)))
    selected = max(0, min(selected, count - 1))
    if selected < offset:
        offset = selected
    elif selected >= offset + viewport:
        offset = selected - viewport + 1
    return max(0, min(offset, max(0, count - viewport)))


def _clip_add(
    win: "curses.window", y: int, x: int, text: object, attr: int = 0, max_width: int | None = None
) -> None:
    """Curses addstr wrapper which clips on cells and avoids corner wrapping."""
    try:
        height, width = win.getmaxyx()
    except curses.error:
        return
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = width - x
    if y == height - 1:
        available -= 1
    if max_width is not None:
        available = min(available, max_width)
    if available <= 0:
        return
    clipped = truncate_display(text, available)
    if not clipped:
        return
    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def _fill_row(win: "curses.window", y: int, x: int, width: int, attr: int = 0) -> None:
    try:
        height, total_width = win.getmaxyx()
    except curses.error:
        return
    if y < 0 or y >= height or x < 0 or x >= total_width or width <= 0:
        return
    width = min(width, total_width - x)
    if y == height - 1:
        width -= 1
    if width <= 0:
        return
    try:
        win.addstr(y, x, " " * width, attr)
    except curses.error:
        pass


def _window_text_width(win: "curses.window") -> int:
    try:
        return win.getmaxyx()[1]
    except curses.error:
        return 0


_HELP_LINES: tuple[tuple[str, str], ...] = (
    ("heading", "文件树导航"),
    ("text", "  Tab / Shift+Tab      在目录栏与会话栏之间切换焦点"),
    ("text", "  ↑/↓ 或 k/j           移动选择"),
    ("text", "  g / G                跳到第一项 / 最后一项"),
    ("text", "  PgUp / PgDn          翻页滚动"),
    ("text", "  ← 或 h               折叠所选；若已折叠则回到父目录"),
    ("text", "  → 或 l               展开目录；若已展开则进入首个子项"),
    ("text", "  Enter（文件夹上）    展开 / 折叠"),
    ("text", "  鼠标单击             选中文件；文件夹同时展开 / 折叠"),
    ("text", "  鼠标双击文件/目录    复制所选项的绝对路径"),
    ("text", "  y / 文件上 Enter    复制所选项的绝对路径"),
    ("text", "  p / 单击文件         打开只读预览；双击仍复制路径"),
    ("text", "  .                    显示 / 隐藏以 . 开头的文件和目录"),
    ("text", "  选中文件时，右侧显示该文件所在目录的历史会话。"),
    ("blank", ""),
    ("heading", "会话与进程"),
    ("text", "  Enter（会话栏）      恢复选中的会话"),
    ("text", "  鼠标单击会话         恢复该会话"),
    ("text", "  n                    在所选目录新建会话"),
    ("text", "  r                    刷新目录树与历史会话"),
    ("text", "  ?                    打开本帮助"),
    ("text", "  q                    暂离工作台（保留会话进程）"),
    ("blank", ""),
    ("heading", "恢复会话时发生什么"),
    ("text", "  左侧目录，中央原版对话，右侧历史会话与 Summary。"),
    ("text", "    zzb-codex resume SESSION_ID -C 所选目录"),
    ("text", "  F6 循环三栏；Tab 切换左右侧栏；q 暂离后可重连。"),
    ("text", "  n 键同样调用现有 launcher：zzb-codex -C 所选目录。"),
    ("blank", ""),
    ("heading", "数据与安全"),
    ("text", "  以 SQLite mode=ro 读取会话信息、模型、用量与已有预览。"),
    ("text", "  不读取凭据或完整 rollout；缺失预览可显示首条请求。"),
    ("text", "  数据库默认位于 ~/.zzb-codex/db/state_5.sqlite；可用 --state-dir 指定。"),
)


# ---------------------------------------------------------------------------
# Interactive curses application
# ---------------------------------------------------------------------------


def wrap_sidebar_text(text: str, width: int) -> list[str]:
    width = max(1, width)
    lines, line, used = [], "", 0
    for char in sanitize_text(text, 4000):
        cells = character_width(char)
        if cells > width:
            continue
        if used + cells > width:
            lines.append(line)
            line, used = "", 0
        line += char
        used += cells
    if line:
        lines.append(line)
    return lines


def summary_fields(session: SessionInfo | None) -> list[tuple[str, str]]:
    if session is None:
        return [("提示", "选择一条历史会话查看信息")]
    model = session.model or "未记录"
    if session.reasoning_effort:
        model += " · " + session.reasoning_effort
    tokens = f"{session.tokens_used:,} tokens" if session.tokens_used is not None else "未记录"
    fields = [("模型", model), ("用量", tokens), ("更新", session.updated_display)]
    if session.preview:
        fields.append(("内容预览", session.preview))
    elif session.first_user_message:
        fields.append(("首条请求", session.first_user_message))
    else:
        fields.append(("内容预览", "暂无记录"))
    return fields


class BrowserApp:
    def __init__(
        self,
        stdscr: "curses.window",
        start_dir: str,
        state_dir: object,
        launcher: object = DEFAULT_LAUNCHER,
    ) -> None:
        self.stdscr = stdscr
        self.launcher = os.fsdecode(os.fspath(launcher))
        self.workspace = None
        self.role = os.environ.get("ZZB_UI_ROLE", "combined")
        if self.role not in ("directories", "sessions"):
            self.role = "combined"
        if os.environ.get("ZZB_UI_WORKSPACE"):
            from workspace import Workspace
            self.workspace = Workspace(
                socket_name=os.environ.get("ZZB_UI_SOCKET", "zzb-codex-ui"),
                session_name=os.environ["ZZB_UI_WORKSPACE"],
            )
        self.theme = Theme()
        self.tree = DirectoryTree(start_dir)
        self.store = ThreadStore.from_state_dir(state_dir)
        self.focus = RIGHT if self.role == "sessions" else LEFT
        self.left_scroll = 0
        self.session_scroll = 0
        self.session_index = -1
        self.session_result = SessionQuery(database=self.store.database_path)
        self._session_directory: str | None = None
        self.status_message: str | None = None
        self.status_kind = "info"
        self.help_visible = False
        self.help_scroll = 0
        self.running = True
        self._suspended = False
        self._layout = ViewLayout()
        self._last_dir_click: tuple[float, str] | None = None
        self._pending_preview = None
        self._mouse_mask = self._build_mouse_mask()
        if self.workspace is not None and self.role != "combined":
            saved = self.workspace.get_directory()
            if saved and os.path.isdir(saved):
                if self.role == "sessions":
                    self.tree = DirectoryTree(saved)
                else:
                    self._reveal_directory(saved)
        self._load_sessions(preserve=False)

    # -- setup and terminal handoff -------------------------------------

    @staticmethod
    def _build_mouse_mask() -> int:
        mask = 0
        for name in (
            "BUTTON1_CLICKED",
            "BUTTON1_DOUBLE_CLICKED",
            "BUTTON4_PRESSED",
            "BUTTON4_CLICKED",
            "BUTTON5_PRESSED",
            "BUTTON5_CLICKED",
        ):
            mask |= int(getattr(curses, name, 0))
        if not mask:
            mask = curses.ALL_MOUSE_EVENTS
        return mask

    def _enable_mouse(self) -> None:
        try:
            curses.mousemask(self._mouse_mask)
        except curses.error:
            pass

    def _disable_mouse(self) -> None:
        try:
            curses.mousemask(0)
        except curses.error:
            pass

    def _setup(self) -> None:
        try:
            self.stdscr.keypad(True)
            self.stdscr.scrollok(False)
        except curses.error:
            pass
        try:
            self.stdscr.bkgd(" ", self.theme.attr("text"))
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.mouseinterval(250)
        except curses.error:
            pass
        self._enable_mouse()

    def _suspend_terminal(self) -> None:
        """Leave curses and mouse reporting before delegating the terminal."""
        if self._suspended:
            return
        self._disable_mouse()
        try:
            curses.def_prog_mode()
        except curses.error:
            pass
        try:
            self.stdscr.keypad(False)
        except curses.error:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.doupdate()
        except curses.error:
            pass
        try:
            curses.flushinp()
        except curses.error:
            pass
        try:
            curses.endwin()
        except curses.error:
            pass
        self._suspended = True

    def _resume_terminal(self) -> None:
        """Reinitialize curses after the child process has returned."""
        if not self._suspended:
            return
        try:
            curses.reset_prog_mode()
        except curses.error:
            pass
        try:
            self.stdscr.keypad(True)
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._enable_mouse()
        try:
            curses.flushinp()
        except curses.error:
            pass
        try:
            self.stdscr.clear()
            self.stdscr.refresh()
        except curses.error:
            pass
        self._layout = ViewLayout()
        self._suspended = False

    # -- data refresh ---------------------------------------------------

    def _selected_session(self) -> SessionInfo | None:
        sessions = self.session_result.sessions
        if 0 <= self.session_index < len(sessions):
            return sessions[self.session_index]
        return None

    def _load_sessions(self, preserve: bool) -> None:
        directory = self.tree.selected_directory
        previous = self._selected_session()
        previous_id = previous.id if previous is not None else None
        try:
            query = self.store.load_sessions(directory)
        except Exception as exc:  # defensive: storage should return errors itself
            query = SessionQuery(
                database=self.store.database_path,
                error="读取会话失败：" + sanitize_text(exc, limit=500),
            )
        self.session_result = query
        self._session_directory = directory
        sessions = query.sessions
        if not sessions:
            self.session_index = -1
            self.session_scroll = 0
            return
        index = 0
        if preserve and previous_id is not None and previous is not None and previous.cwd == directory:
            for candidate, session in enumerate(sessions):
                if session.id == previous_id:
                    index = candidate
                    break
        self.session_index = index
        self.session_scroll = 0

    def _reveal_directory(self, directory: str) -> None:
        relative = os.path.relpath(directory, self.tree.root_path)
        if relative == ".":
            return
        if relative == ".." or relative.startswith(".." + os.sep):
            self.tree = DirectoryTree(directory)
            return
        node = self.tree.root
        for part in relative.split(os.sep):
            self.tree._expand_node(node)
            child = next((item for item in node.children if item.name == part), None)
            if child is None:
                return
            node = child
        self.tree.selected_path = node.path

    def _poll_directory(self) -> None:
        if self.workspace is None or self.role != "sessions":
            return
        try:
            directory = self.workspace.get_directory()
            if directory and normalize_path(directory) != self.tree.selected_directory:
                if not os.path.isdir(directory):
                    self._set_status("所选目录不可访问", "error")
                    return
                self.tree = DirectoryTree(directory)
                self.status_message = None
                self._load_sessions(preserve=False)
        except Exception as exc:
            self._set_status("目录同步失败：" + sanitize_text(exc), "error")

    def _sync_directory(self) -> None:
        directory = self.tree.selected_directory
        if self.role == "directories" and self.workspace is not None:
            try:
                self.workspace.set_directory(directory)
            except Exception as exc:
                self._set_status("目录同步失败：" + sanitize_text(exc), "error")
        if directory != self._session_directory:
            self.status_message = None
            self._load_sessions(preserve=False)

    def _refresh_data(self, preserve_sessions: bool, announce: bool = False) -> None:
        previous_dir = self.tree.selected_directory
        try:
            self.tree.refresh()
        except Exception as exc:
            self.status_message = "刷新目录失败：" + sanitize_text(exc, limit=400)
            self.status_kind = "error"
            return
        new_dir = self.tree.selected_directory
        self._load_sessions(preserve=preserve_sessions and old_same_directory(previous_dir, new_dir))
        if announce:
            if self.session_result.error:
                self.status_message = "会话刷新失败：" + self.session_result.error
                self.status_kind = "error"
            elif self.session_result.missing_database:
                self.status_message = "数据库不存在：" + self.store.database_path
                self.status_kind = "warning"
            else:
                self.status_message = "已刷新目录与会话"
                self.status_kind = "good"

    # -- status ---------------------------------------------------------

    def _set_status(self, message: str, kind: str = "info") -> None:
        self.status_message = message
        self.status_kind = kind

    def _copy_selected_directory(self) -> None:
        # Copy the visible entry, preserving symlink spelling; cwd selection is
        # separate and always remains a directory for conversation launches.
        node = self.tree.selected_node
        path = node.path
        try:
            copy_to_clipboard(path)
        except ClipboardError as exc:
            self._set_status("复制路径失败：" + str(exc), "error")
        except Exception as exc:
            self._set_status("复制路径失败：" + sanitize_text(exc, limit=300), "error")
        else:
            kind = "目录" if node.is_directory else "文件"
            self._set_status("已复制" + kind + "路径：" + path, "good")

    # -- selection movement ---------------------------------------------

    def _tree_move(self, delta: int) -> None:
        if self.tree.move(delta):
            self.status_message = None
        self._sync_directory()

    def _session_move(self, delta: int) -> None:
        sessions = self.session_result.sessions
        if not sessions:
            return
        self.session_index = max(0, min(len(sessions) - 1, self.session_index + delta))

    def _session_move_to_first(self) -> None:
        if self.session_result.sessions:
            self.session_index = 0

    def _session_move_to_last(self) -> None:
        if self.session_result.sessions:
            self.session_index = len(self.session_result.sessions) - 1

    def _tree_page_size(self) -> int:
        if self._layout.left is not None:
            return max(1, self._layout.left.height - 3)
        return 8

    def _session_page_size(self) -> int:
        if self._layout.right is not None:
            inner = max(1, self._layout.right.height - 2)
            return max(1, inner // self._session_item_height(inner))
        if self._layout.compact and self.focus == RIGHT and self._layout.right is None:
            return 8
        return 8

    @staticmethod
    def _session_item_height(inner_height: int) -> int:
        return 2 if inner_height >= 4 else 1

    def _session_page(self, direction: int) -> None:
        amount = max(1, self._session_page_size() - 1)
        self._session_move(direction * amount)

    def _tree_page(self, direction: int) -> None:
        amount = max(1, self._tree_page_size() - 1)
        self._tree_move(direction * amount)

    # -- external launcher ----------------------------------------------

    def _valid_selected_directory(self) -> bool:
        directory = self.tree.selected_directory
        if not os.path.isdir(directory):
            self._set_status("所选目录不存在或不可访问：" + directory, "error")
            return False
        return True

    def _launch_resume(self) -> None:
        session = self._selected_session()
        if session is None:
            self._set_status("没有可恢复的会话", "warning")
            return
        if not self._valid_selected_directory():
            return
        argv = build_resume_argv(session.id, self.tree.selected_directory, self.launcher)
        self._run_launcher(argv, "恢复会话")

    def _launch_new(self) -> None:
        if not self._valid_selected_directory():
            return
        argv = build_new_argv(self.tree.selected_directory, self.launcher)
        self._run_launcher(argv, "新建会话")

    def _run_launcher(self, argv: list[str], label: str) -> None:
        if self.workspace is not None:
            try:
                key = "resume:" + argv[2] if len(argv) > 2 and argv[1] == "resume" else None
                self.workspace.open(argv, self.tree.selected_directory, key=key)
                self._set_status("对话已打开 · F6 返回导航 · 切换会话会保留进程", "good")
            except Exception as exc:
                self._set_status("无法打开对话：" + sanitize_text(exc), "error")
            return
        self._set_status("正在" + label + "…", "info")
        process_error: str | None = None
        return_code: int | None = None
        self._suspend_terminal()
        try:
            completed = subprocess.run(argv, check=False)  # argv list, never shell=True
            return_code = completed.returncode
        except OSError as exc:
            process_error = "无法启动会话进程：" + describe_os_error(exc)
        except KeyboardInterrupt:
            process_error = "会话进程已中断"
        finally:
            self._resume_terminal()
        self._refresh_data(preserve_sessions=True, announce=False)
        if process_error is not None:
            self._set_status(process_error, "error")
        elif self.session_result.error:
            self._set_status("会话进程已返回，但会话刷新失败：" + self.session_result.error, "error")
        elif return_code not in (None, 0):
            self._set_status(f"{label}进程退出，代码 {return_code}；已刷新", "warning")
        else:
            self._set_status(f"{label}已结束，已刷新", "good")

    # -- key handling ---------------------------------------------------

    def _toggle_focus(self) -> None:
        if self.workspace is not None and self.role != "combined":
            try:
                self.workspace.focus_role("sessions" if self.role == "directories" else "directories")
            except Exception as exc:
                self._set_status("切区失败：" + sanitize_text(exc), "error")
            return
        self.focus = RIGHT if self.focus == LEFT else LEFT
        if self._layout.compact:
            self._layout = ViewLayout()

    def _open_help(self) -> None:
        self.help_visible = True
        self.help_scroll = 0

    def _leave_workspace(self) -> None:
        if self.workspace is None:
            self.running = False
            return
        try:
            self.workspace.detach()
        except Exception as exc:
            self._set_status("暂离失败：" + sanitize_text(exc), "error")

    def _preview_selected(self) -> None:
        self._pending_preview = None
        node = self.tree.selected_node
        if node.is_directory:
            self._set_status("请选择文件后按 p 预览", "info")
            return
        from preview import is_image, open_image
        try:
            if is_image(node.path):
                open_image(node.path)
                self._set_status("已打开图片预览窗口", "info")
                return
            command = [sys.executable, os.path.join(os.path.dirname(__file__), "preview.py"), node.path]
            if self.workspace:
                from workspace import TMUX
                clients = self.workspace.run("list-clients", "-t", self.workspace.session_name,
                                             "-F", "#{client_name}").splitlines()
                if not clients:
                    raise ValueError("当前工作台没有连接的终端，无法显示浮窗")
                command = [TMUX, "-L", self.workspace.socket_name, "display-popup", "-E",
                           "-c", clients[0], "-w", "80%", "-h", "80%",
                           "-s", "fg=colour252,bg=colour234", "-S", "fg=colour109",
                           shlex.join(command)]
            self._suspend_terminal()
            try:
                result = subprocess.run(command, check=False)
                if result.returncode:
                    self._set_status("预览窗口未能打开", "error")
            finally:
                self._resume_terminal()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._set_status("预览失败：" + sanitize_text(str(exc)), "error")

    def _poll_preview(self) -> None:
        pending = self._pending_preview
        if pending is None:
            return
        deadline, path = pending
        if self.focus != LEFT or self.tree.selected_path != path or self.help_visible:
            self._pending_preview = None
        elif time.monotonic() >= deadline:
            self._preview_selected()

    def _handle_key(self, key: int) -> None:
        self._pending_preview = None
        if key == ord("p") and self.focus == LEFT:
            self._preview_selected()
            return
        key_enter = getattr(curses, "KEY_ENTER", 343)
        key_ppage = getattr(curses, "KEY_PPAGE", 339)
        key_npage = getattr(curses, "KEY_NPAGE", 338)
        key_home = getattr(curses, "KEY_HOME", 262)
        key_end = getattr(curses, "KEY_END", 360)
        key_backtab = getattr(curses, "KEY_BTAB", 353)
        key_up = getattr(curses, "KEY_UP", 259)
        key_down = getattr(curses, "KEY_DOWN", 258)
        key_left = getattr(curses, "KEY_LEFT", 260)
        key_right = getattr(curses, "KEY_RIGHT", 261)

        if key == ord("q"):
            self._leave_workspace()
            return
        if key == ord("?"):
            self._open_help()
            return
        if key in (ord("\t"), key_backtab):
            self._toggle_focus()
            return
        if key in (ord("r"),):
            self._refresh_data(preserve_sessions=True, announce=True)
            return
        if key == ord("n"):
            self._launch_new()
            return
        if key == ord("y"):
            self._copy_selected_directory()
            return
        if key == ord("."):
            shown = self.tree.toggle_hidden()
            self._sync_directory()
            self._set_status(
                "已显示隐藏文件和目录" if shown else "已隐藏以 . 开头的文件和目录",
                "info",
            )
            return

        if self.focus == LEFT:
            if key in (ord("j"), key_down):
                self._tree_move(1)
            elif key in (ord("k"), key_up):
                self._tree_move(-1)
            elif key == ord("g") or key == key_home:
                self.tree.move_to_first()
                self._sync_directory()
            elif key == ord("G") or key == key_end:
                self.tree.move_to_last()
                self._sync_directory()
            elif key in (ord("h"), key_left):
                before = self.tree.selected_path
                self.tree.collapse_or_parent()
                if self.tree.selected_path != before:
                    self._sync_directory()
            elif key in (ord("l"), key_right):
                before = self.tree.selected_path
                result = self.tree.expand_selected()
                if self.tree.selected_path != before:
                    self._sync_directory()
                node = self.tree.selected_node
                if result == "error" and node.scan_error:
                    self._set_status("目录读取失败：" + node.scan_error, "error")
            elif key in (ord("\n"), ord("\r"), key_enter):
                if not self.tree.selected_node.is_directory:
                    self._copy_selected_directory()
                    return
                result = self.tree.toggle_selected()
                node = self.tree.selected_node
                if result == "error" and node.scan_error:
                    self._set_status("目录读取失败：" + node.scan_error, "error")
            elif key == key_ppage:
                self._tree_page(-1)
            elif key == key_npage:
                self._tree_page(1)
        else:
            if key in (ord("j"), key_down):
                self._session_move(1)
            elif key in (ord("k"), key_up):
                self._session_move(-1)
            elif key == ord("g") or key == key_home:
                self._session_move_to_first()
            elif key == ord("G") or key == key_end:
                self._session_move_to_last()
            elif key in (ord("\n"), ord("\r"), key_enter):
                self._launch_resume()
            elif key == key_ppage:
                self._session_page(-1)
            elif key == key_npage:
                self._session_page(1)

    def _handle_help_key(self, key: int) -> None:
        key_ppage = getattr(curses, "KEY_PPAGE", 339)
        key_npage = getattr(curses, "KEY_NPAGE", 338)
        key_home = getattr(curses, "KEY_HOME", 262)
        key_end = getattr(curses, "KEY_END", 360)
        key_up = getattr(curses, "KEY_UP", 259)
        key_down = getattr(curses, "KEY_DOWN", 258)
        if key == ord("q"):
            self._leave_workspace()
        elif key in (ord("?"), 27):
            self.help_visible = False
        elif key in (ord("j"), key_down):
            self.help_scroll += 1
        elif key in (ord("k"), key_up):
            self.help_scroll -= 1
        elif key == ord("g") or key == key_home:
            self.help_scroll = 0
        elif key == ord("G") or key == key_end:
            self.help_scroll = 10**9
        elif key == key_ppage:
            self.help_scroll -= 10
        elif key == key_npage:
            self.help_scroll += 10

    # -- mouse handling -------------------------------------------------

    @staticmethod
    def _mouse_bits(*names: str) -> int:
        bits = 0
        for name in names:
            bits |= int(getattr(curses, name, 0))
        return bits

    def _handle_mouse(self) -> None:
        self._pending_preview = None
        try:
            _, mouse_x, mouse_y, _, button_state = curses.getmouse()
        except curses.error:
            return
        wheel_up = self._mouse_bits("BUTTON4_PRESSED", "BUTTON4_CLICKED")
        wheel_down = self._mouse_bits("BUTTON5_PRESSED", "BUTTON5_CLICKED")
        if wheel_up and button_state & wheel_up:
            self._scroll_at(mouse_y, mouse_x, -3)
            return
        if wheel_down and button_state & wheel_down:
            self._scroll_at(mouse_y, mouse_x, 3)
            return
        clicked = self._mouse_bits("BUTTON1_CLICKED", "BUTTON1_DOUBLE_CLICKED")
        if clicked and not (button_state & clicked):
            return
        if not clicked and not (button_state & curses.BUTTON1_CLICKED):
            return

        layout = self._layout
        if layout.left is not None and layout.left.contains(mouse_y, mouse_x):
            self.focus = LEFT
            if not layout.left.inner_contains(mouse_y, mouse_x):
                return
            inner_y = mouse_y - (layout.left.y + 1)
            index = self.left_scroll + inner_y
            nodes = self.tree.visible_nodes
            if not (0 <= index < len(nodes)):
                return
            if self.tree.selected_path != nodes[index].path:
                self.status_message = None
            self.tree.select_index(index)
            self._sync_directory()
            path = self.tree.selected_node.path
            now = time.monotonic()
            is_double = bool(button_state & self._mouse_bits("BUTTON1_DOUBLE_CLICKED")) or (
                self._last_dir_click is not None
                and self._last_dir_click[1] == path
                and now - self._last_dir_click[0] <= DOUBLE_CLICK_SECONDS
            )
            if is_double:
                self._last_dir_click = None
                self._copy_selected_directory()
            else:
                self._last_dir_click = (now, path)
                if not self.tree.selected_node.is_directory:
                    self._pending_preview = (now + DOUBLE_CLICK_SECONDS, path)
                    return
                result = self.tree.toggle_selected()
                if result == "error" and self.tree.selected_node.scan_error:
                    self._set_status("目录读取失败：" + self.tree.selected_node.scan_error, "error")
                self._sync_directory()
            return

        if layout.right is not None and layout.right.contains(mouse_y, mouse_x):
            self.focus = RIGHT
            if not layout.right.inner_contains(mouse_y, mouse_x):
                return
            inner_y = mouse_y - (layout.right.y + 1)
            inner_height = layout.right.height - 2
            item_height = self._session_item_height(inner_height)
            index = self.session_scroll + inner_y // item_height
            sessions = self.session_result.sessions
            if not (0 <= index < len(sessions)):
                return
            self.session_index = index
            self._launch_resume()

    def _scroll_at(self, mouse_y: int, mouse_x: int, delta: int) -> None:
        layout = self._layout
        if layout.left is not None and layout.left.contains(mouse_y, mouse_x):
            self.focus = LEFT
            self._tree_move(delta)
        elif layout.right is not None and layout.right.contains(mouse_y, mouse_x):
            self.focus = RIGHT
            self._session_move(delta)

    # -- rendering ------------------------------------------------------

    def _screen_size(self) -> tuple[int, int]:
        try:
            return self.stdscr.getmaxyx()
        except curses.error:
            return 0, 0

    def _make_pane(
        self, rect: Rect, title: str, active: bool, title_style: str = "header"
    ) -> "curses.window | None":
        if rect.height < 3 or rect.width < 8:
            return None
        try:
            window = curses.newwin(rect.height, rect.width, rect.y, rect.x)
            window.bkgd(" ", self.theme.attr("text"))
            window.erase()
        except curses.error:
            return None
        border_attr = self.theme.attr("border_active" if active else "border_inactive")
        badge_attr = self.theme.attr(title_style)
        try:
            window.attrset(border_attr)
            window.box()
        except curses.error:
            pass
        marker = "◆ " if active else "◇ "
        badge = " " + marker + sanitize_text(title, limit=120) + " "
        badge_width = display_width(badge)
        badge_space = rect.width - 4
        if badge_space > 2 and badge_width > badge_space:
            badge = truncate_display(badge, badge_space)
            badge_width = display_width(badge)
        if badge_space > 2 and badge_width <= badge_space:
            _fill_row(window, 0, 2, badge_width, badge_attr)
            _clip_add(window, 0, 2, badge, badge_attr, badge_space)
        return window

    def _draw_scrollbar(
        self,
        window: "curses.window",
        top: int,
        left: int,
        height: int,
        count: int,
        viewport: int,
        offset: int,
    ) -> None:
        if count <= viewport or height <= 0:
            return
        thumb_height = max(1, int(height * viewport / max(1, count)))
        max_scroll = max(1, count - viewport)
        thumb_top = int(offset / max_scroll * max(0, height - thumb_height))
        for row in range(height):
            is_thumb = thumb_top <= row < thumb_top + thumb_height
            attr = self.theme.attr("accent" if is_thumb else "muted")
            _clip_add(window, top + row, left, "█" if is_thumb else "│", attr, 1)

    def _draw_tree_content(self, window: "curses.window") -> None:
        try:
            height, width = window.getmaxyx()
        except curses.error:
            return
        inner_height = height - 2
        inner_width = width - 2
        if inner_height <= 0 or inner_width <= 0:
            return
        nodes = self.tree.visible_nodes
        selected = self.tree.selected_index
        self.left_scroll = ensure_visible(selected, len(nodes), inner_height, self.left_scroll)
        overflow = len(nodes) > inner_height
        content_width = max(1, inner_width - (1 if overflow else 0))
        accent = self.theme.attr("directory_arrow")
        selection = "selected" if self.focus == LEFT else "selected_inactive"
        selected_attr = self.theme.attr(selection)
        warning_attr = self.theme.attr("warning")
        for row in range(min(inner_height, len(nodes))):
            index = self.left_scroll + row
            node = nodes[index]
            selected_row = index == selected
            y = 1 + row
            if selected_row:
                _fill_row(window, y, 1, inner_width, selected_attr)
            max_indent = max(0, content_width - 4)
            indent_width = min(node.depth * 2, max_indent)
            if node.is_directory:
                arrow = "▾ " if node.expanded else "▸ "
            else:
                arrow = "↗ " if node.is_symlink else "· "
            prefix_width = indent_width + display_width(arrow)
            kind = tree_node_style(node)
            line_attr = (
                self.theme.attr(selection + "_" + ("warning" if node.scan_error else kind))
                if selected_row else (warning_attr if node.scan_error else self.theme.attr(kind))
            )
            if selected_row:
                _clip_add(window, y, 1, " " * indent_width + arrow,
                          self.theme.attr(selection + "_directory_arrow"), content_width)
            else:
                _clip_add(window, y, 1 + indent_width, arrow, accent, content_width - indent_width)
            name = sanitize_text(node.name, limit=300) or sanitize_text(node.path, limit=300)
            if node.scan_error:
                name = (name + "  ! ") if name else "! "
            name_x = 1 + prefix_width
            name_width = content_width - prefix_width
            if name_width > 0:
                _clip_add(window, y, name_x, name, line_attr, name_width)
        if overflow:
            self._draw_scrollbar(
                window, 1, width - 2, inner_height, len(nodes), inner_height, self.left_scroll
            )

    def _session_notice(
        self,
        window: "curses.window",
        heading: str,
        details: list[tuple[str, str]],
    ) -> None:
        try:
            height, width = window.getmaxyx()
        except curses.error:
            return
        content_width = max(1, width - 4)
        heading_attr = self.theme.attr("warning")
        _clip_add(window, 1, 2, heading, heading_attr, content_width)
        row = 2
        for text, kind in details:
            if row >= height - 1:
                break
            _clip_add(window, row, 2, text, self.theme.attr(kind), content_width)
            row += 1

    def _draw_session_content(self, window: "curses.window") -> None:
        try:
            height, width = window.getmaxyx()
        except curses.error:
            return
        inner_height = height - 2
        inner_width = width - 2
        if inner_height <= 0 or inner_width <= 0:
            return
        query = self.session_result
        sessions = query.sessions
        if query.error:
            self._session_notice(
                window,
                "读取历史会话失败",
                [(sanitize_text(query.error, 500), "error"), ("按 r 重试", "muted")],
            )
            return
        if query.missing_database:
            self._session_notice(
                window,
                "数据库不存在",
                [
                    (query.database, "muted"),
                    ("按 r 重试，或使用 --state-dir 指定状态目录", "muted"),
                ],
            )
            return
        if not sessions:
            self._session_notice(
                window,
                "该目录暂无历史会话",
                [("按 n 在此目录新建会话", "muted"), ("按 r 刷新", "muted")],
            )
            return

        if self.session_index < 0 or self.session_index >= len(sessions):
            self.session_index = 0
        item_height = self._session_item_height(inner_height)
        viewport = max(1, inner_height // item_height)
        self.session_scroll = ensure_visible(
            self.session_index, len(sessions), viewport, self.session_scroll
        )
        overflow = len(sessions) > viewport
        content_width = max(1, inner_width - (1 if overflow else 0))
        selection = "selected" if self.focus == RIGHT else "selected_inactive"
        selected_attr = self.theme.attr(selection)
        title_attr = self.theme.attr("session")
        for row_index in range(min(viewport, len(sessions))):
            index = self.session_scroll + row_index
            session = sessions[index]
            selected = index == self.session_index
            y = 1 + row_index * item_height
            if selected:
                _fill_row(window, y, 1, inner_width, selected_attr)
                if item_height == 2:
                    _fill_row(window, y + 1, 1, inner_width, selected_attr)
            if item_height == 2:
                title = "› " + session.title
                _clip_add(
                    window,
                    y,
                    1,
                    title,
                    self.theme.attr(selection + "_session") if selected else title_attr,
                    content_width,
                )
                date = f"  {session.updated_display}"
                date_width = min(display_width(date), content_width)
                prefix = selection + "_" if selected else ""
                _clip_add(window, y + 1, 1, date,
                          self.theme.attr(prefix + "date"), content_width)
                _clip_add(window, y + 1, 1 + date_width, f"  ·  {session.short_id}",
                          self.theme.attr(prefix + "identifier"), content_width - date_width)
            else:
                title = "› " + session.title
                title_width = min(display_width(title), content_width)
                _clip_add(window, y, 1, title,
                          self.theme.attr(selection + "_session") if selected else title_attr,
                          content_width)
                _clip_add(window, y, 1 + title_width, "  " + session.updated_display,
                          self.theme.attr((selection + "_" if selected else "") + "date"),
                          content_width - title_width)
        if overflow:
            self._draw_scrollbar(
                window, 1, width - 2, inner_height, len(sessions), viewport, self.session_scroll
            )

    def _tree_pane_title(self) -> str:
        suffix = " · 含隐藏" if self.tree.show_hidden else ""
        return "文件树" + suffix

    def _session_pane_title(self) -> str:
        return f"历史会话 · {self.session_result.count}"

    def _draw_frame_for_focus(self, h: int, w: int) -> list["curses.window"]:
        windows: list["curses.window"] = []
        header_height = 1 if h >= 1 else 0
        footer_height = 1 if h >= 2 else 0
        status_height = 1 if h >= 6 else 0
        main_y = header_height
        main_height = h - header_height - footer_height - status_height
        if main_height <= 0:
            return windows
        compact = not (w >= 60 and main_height >= 5)

        if compact:
            if main_height < 3 or w < 20:
                lines = [
                    "终端窗口过小，请调整大小",
                    f"焦点：{'目录' if self.focus == LEFT else '会话'}  ·  Tab 切换",
                ]
                selected_path = sanitize_text(self.tree.selected_node.path, limit=200)
                lines.append("路径：" + selected_path)
                if self.session_result.count:
                    lines.append(f"该目录历史会话：{self.session_result.count}")
                for index, line in enumerate(lines[:main_height]):
                    _clip_add(
                        self.stdscr,
                        main_y + index,
                        1,
                        line,
                        self.theme.attr("muted" if index else "warning"),
                        w - 2,
                    )
                return windows
            rect = Rect(main_y, 0, main_height, w)
            if self.focus == LEFT:
                window = self._make_pane(rect, self._tree_pane_title() + " · 单栏", True)
                if window is not None:
                    self._draw_tree_content(window)
                    self._layout.left = rect
                    self._layout.compact = True
                    windows.append(window)
            else:
                window = self._make_pane(rect, self._session_pane_title() + " · 单栏", True)
                if window is not None:
                    self._draw_session_content(window)
                    self._layout.right = rect
                    self._layout.compact = True
                    windows.append(window)
            return windows

        left_width = max(26, min(48, int(w * 0.38)))
        right_width = w - left_width - 1
        if right_width < 28:
            left_width = max(20, w - 29)
            right_width = w - left_width - 1
        left_rect = Rect(main_y, 0, main_height, left_width)
        right_rect = Rect(main_y, left_width + 1, main_height, right_width)
        left_window = self._make_pane(
            left_rect, self._tree_pane_title(), self.focus == LEFT
        )
        if left_window is not None:
            self._draw_tree_content(left_window)
            self._layout.left = left_rect
            windows.append(left_window)
        right_window = self._make_pane(
            right_rect, self._session_pane_title(), self.focus == RIGHT
        )
        if right_window is not None:
            self._draw_session_content(right_window)
            self._layout.right = right_rect
            windows.append(right_window)
        return windows

    def _draw_header(self, w: int) -> None:
        _fill_row(self.stdscr, 0, 0, w, self.theme.attr("header"))
        left = " zzb-codex-ui  ·  Codex 会话浏览器"
        right = f"焦点：{'目录' if self.focus == LEFT else '会话'}  ·  Tab 切换"
        right_width = display_width(right)
        if right_width + display_width(left) + 3 <= w:
            _clip_add(self.stdscr, 0, 1, left, self.theme.attr("header"), w - 2)
            right_x = w - right_width - 1
            _clip_add(
                self.stdscr, 0, right_x, right, self.theme.attr("header"), right_width
            )
        else:
            _clip_add(self.stdscr, 0, 1, left, self.theme.attr("header"), w - 2)

    def _draw_status(self, y: int, w: int) -> None:
        message: str
        kind: str
        if self.status_message is not None:
            message = self.status_message
            kind = self.status_kind
        else:
            selected_node = self.tree.selected_node
            if selected_node.scan_error:
                message = "目录读取失败：" + selected_node.scan_error
                kind = "error"
            elif self.session_result.error:
                message = "会话读取失败：" + self.session_result.error
                kind = "error"
            elif self.session_result.missing_database:
                message = "数据库不存在：" + self.store.database_path
                kind = "warning"
            else:
                message = (
                    ("目录：" if selected_node.is_directory else "文件：")
                    + selected_node.path
                    + f"  ·  会话 {self.session_result.count}"
                )
                kind = "muted"
        _fill_row(self.stdscr, y, 0, w, self.theme.attr("text"))
        if w > 2:
            _clip_add(
                self.stdscr,
                y,
                1,
                sanitize_text(message, limit=2000),
                self.theme.attr(kind if kind in self.theme._attrs else "muted"),
                w - 2,
            )

    def _draw_footer(self, y: int, w: int) -> None:
        _fill_row(self.stdscr, y, 0, w, self.theme.attr("text"))
        if self.help_visible:
            text = "↑↓/PgUp/PgDn 滚动帮助  ·  ? / Esc 关闭帮助  ·  q 退出"
            _clip_add(self.stdscr, y, 1, text, self.theme.attr("accent"), w - 2)
            return
        essentials = "F6 对话  ·  ? 帮助  ·  q 暂离" if self.workspace else "r 刷新  ·  ? 帮助  ·  q 退出"
        if self.focus == LEFT:
            hints = "Tab 焦点  ·  ↑↓/jk 移动  ·  h 折叠/父级  ·  l 展开  ·  Enter 展开/复制  ·  p 预览  ·  y 复制  ·  . 隐藏"
        else:
            hints = "Tab 焦点  ·  ↑↓/jk 移动  ·  Enter 恢复会话  ·  n 新会话  ·  y 复制路径"
        essential_width = display_width(essentials)
        if w >= essential_width + 12:
            right_x = w - essential_width - 1
            _clip_add(
                self.stdscr,
                y,
                1,
                hints,
                self.theme.attr("muted"),
                max(0, right_x - 2),
            )
            _clip_add(
                self.stdscr,
                y,
                right_x,
                essentials,
                self.theme.attr("accent"),
                essential_width,
            )
        else:
            _clip_add(self.stdscr, y, 1, essentials, self.theme.attr("accent"), w - 2)

    def _draw_help(self, h: int, w: int) -> None:
        header_height = 1 if h >= 1 else 0
        footer_height = 1 if h >= 2 else 0
        if header_height:
            _fill_row(self.stdscr, 0, 0, w, self.theme.attr("header"))
            _clip_add(
                self.stdscr,
                0,
                1,
                " zzb-codex-ui  ·  帮助与键位",
                self.theme.attr("header"),
                w - 2,
            )
        body_top = header_height
        body_height = max(0, h - header_height - footer_height)
        if body_height <= 0:
            return
        max_scroll = max(0, len(_HELP_LINES) - body_height)
        self.help_scroll = max(0, min(self.help_scroll, max_scroll))
        for row in range(body_height):
            index = self.help_scroll + row
            if index >= len(_HELP_LINES):
                break
            kind, text = _HELP_LINES[index]
            if kind == "blank":
                continue
            attr = self.theme.attr("help_heading" if kind == "heading" else "text")
            _clip_add(self.stdscr, body_top + row, 2, text, attr, max(0, w - 4))
        if footer_height:
            y = h - 1
            _fill_row(self.stdscr, y, 0, w, self.theme.attr("text"))
            footer = "↑↓/PgUp/PgDn 滚动  ·  ? / Esc 关闭帮助  ·  q 退出"
            _clip_add(self.stdscr, y, 1, footer, self.theme.attr("accent"), w - 2)

    def _draw_sidebar(self, h: int, w: int) -> None:
        windows = []
        body = max(0, h - 2)
        if self.role == "directories":
            rect = Rect(0, 0, body, w)
            window = self._make_pane(rect, self._tree_pane_title(), True, "directory")
            if window is not None:
                self._layout.left = rect
                self._draw_tree_content(window)
                windows.append(window)
        else:
            summary_height = min(16, max(8, int(body * 0.45))) if body >= 14 else 0
            history_height = body - summary_height
            rect = Rect(0, 0, history_height, w)
            window = self._make_pane(rect, f"历史 · {self.session_result.count}", True, "session")
            if window is not None:
                self._layout.right = rect
                self._draw_session_content(window)
                windows.append(window)
            if summary_height:
                rect = Rect(history_height, 0, summary_height, w)
                summary = self._make_pane(rect, "Summary", False, "model")
                if summary is not None:
                    row = 1
                    for label, value in summary_fields(self._selected_session()):
                        lines = wrap_sidebar_text(label + ": " + value, max(1, w - 2))
                        value_style = {"模型": "model", "用量": "usage", "更新": "date",
                                       "内容预览": "preview", "首条请求": "preview"}.get(label, "muted")
                        label_remaining = len(label + ": ")
                        for line in lines:
                            if row >= summary_height - 1:
                                break
                            label_part = line[:label_remaining]
                            value_part = line[len(label_part):]
                            label_remaining -= len(label_part)
                            label_width = display_width(label_part)
                            _clip_add(summary, row, 1, label_part, self.theme.attr("label"), w - 2)
                            _clip_add(summary, row, 1 + label_width, value_part,
                                      self.theme.attr(value_style), w - 2 - label_width)
                            row += 1
                    windows.append(summary)
        if h >= 2:
            text = self.status_message or self.tree.selected_node.path
            status_style = (self.status_kind if self.status_kind in self.theme._attrs else "muted") if self.status_message else "path"
            _clip_add(self.stdscr, h - 2, 0, text, self.theme.attr(status_style), w)
            hint = "jk hl · p预览 · y复制 · ?" if self.role == "directories" else "jk Enter · ? · q"
            _clip_add(self.stdscr, h - 1, 0, hint, self.theme.attr("muted"), w)
        try:
            self.stdscr.noutrefresh()
            for window in windows:
                window.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    def _draw(self) -> None:
        h, w = self._screen_size()
        if h <= 0 or w <= 0:
            return
        self._layout = ViewLayout()
        try:
            self.stdscr.erase()
        except curses.error:
            return
        if self.help_visible:
            self._draw_help(h, w)
            try:
                self.stdscr.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            return

        if self.role != "combined":
            self._draw_sidebar(h, w)
            return
        self._draw_header(w)
        windows = self._draw_frame_for_focus(h, w)
        footer_height = 1 if h >= 2 else 0
        status_height = 1 if h >= 6 else 0
        if status_height:
            self._draw_status(h - footer_height - 1, w)
        if footer_height:
            self._draw_footer(h - 1, w)
        try:
            self.stdscr.noutrefresh()
            for window in windows:
                window.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    # -- main loop ------------------------------------------------------

    def run(self) -> None:
        self._setup()
        self.stdscr.timeout(100)
        last_refresh = time.monotonic()
        try:
            while self.running:
                self._poll_preview()
                self._poll_directory()
                if self.workspace is not None and time.monotonic() - last_refresh >= 3:
                    self._load_sessions(preserve=True)
                    last_refresh = time.monotonic()
                self._draw()
                try:
                    key = self.stdscr.getch()
                except curses.error:
                    continue
                if key == -1:
                    continue
                if key == getattr(curses, "KEY_RESIZE", 410):
                    self._pending_preview = None
                    self._layout = ViewLayout()
                    continue
                if key == getattr(curses, "KEY_MOUSE", 409):
                    if not self.help_visible:
                        self._handle_mouse()
                    continue
                if self.help_visible:
                    self._handle_help_key(key)
                else:
                    self._handle_key(key)
        finally:
            self._disable_mouse()


def old_same_directory(left: str, right: str) -> bool:
    try:
        return normalize_path(left) == normalize_path(right)
    except (OSError, ValueError):
        return left == right


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _looks_like_ui_path(token: str) -> bool:
    """Whether a bare token keeps the legacy "single directory" UI usage."""
    if token in (".", ".."):
        return True
    if token.startswith(("/", "~", "./", "../")):
        return True
    try:
        return os.path.isdir(token)
    except (OSError, ValueError):
        return False


def _option_value(argv: list[str], index: int) -> str | None:
    """Return the value for an option at *index*, rejecting a following flag."""
    if index + 1 >= len(argv):
        return None
    value = argv[index + 1]
    if value.startswith("-") and value != "-":
        return None
    return value


def _route_cli(argv: list[str]) -> tuple[str, list[str]]:
    """Route argv to the UI, the native launcher, or a UI usage error.

    Only the legacy UI grammar (no command, one directory, ``resume`` with no
    ID/name, and the UI options) stays in this program.  Everything else is
    returned untouched for verbatim native passthrough.
    """
    ui_argv: list[str] = []
    positional: str | None = None
    start_dir: str | None = None
    resume_mode = False
    ui_option_seen = False
    help_requested = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return "native", list(argv)
        if token in ("-h", "--help"):
            help_requested = True
            ui_argv.append(token)
            index += 1
            continue
        if token in ("-C", "--cd"):
            value = _option_value(argv, index)
            if value is None:
                return "error", ["选项 " + token + " 需要目录参数"]
            start_dir = value
            ui_argv.extend((token, value))
            index += 2
            continue
        if token.startswith("--cd="):
            start_dir = token.split("=", 1)[1]
            ui_argv.append(token)
            index += 1
            continue
        if token.startswith("-C") and token != "-C":
            start_dir = token[2:]
            ui_argv.append(token)
            index += 1
            continue
        if token == "--state-dir":
            value = _option_value(argv, index)
            if value is None:
                return "error", ["选项 --state-dir 需要目录参数"]
            ui_option_seen = True
            ui_argv.extend((token, value))
            index += 2
            continue
        if token.startswith("--state-dir="):
            ui_option_seen = True
            ui_argv.append(token)
            index += 1
            continue
        if token == "--list-json":
            ui_option_seen = True
            ui_argv.append(token)
            index += 1
            continue
        if token.startswith("-") and token != "-":
            return "native", list(argv)
        if resume_mode:
            # After "resume", every non-option is an ID/name, even if a
            # directory with that name exists.
            return "native", list(argv)
        if token == "resume":
            if positional is not None:
                return "native", list(argv)
            resume_mode = True
            index += 1
            continue
        if positional is not None:
            return "native", list(argv)
        if ui_option_seen or _looks_like_ui_path(token):
            positional = token
            ui_argv.append(token)
            index += 1
            continue
        return "native", list(argv)
    if help_requested and resume_mode:
        return "native", list(argv)
    if start_dir is not None and positional is not None:
        return "error", ["目录位置参数与 -C/--cd 不能同时使用"]
    return "ui", ui_argv


def _run_native(argv: list[str], launcher: object = DEFAULT_LAUNCHER) -> int:
    """Run the real launcher with inherited stdio and an argv list."""
    executable = os.fsdecode(os.fspath(launcher))
    try:
        completed = subprocess.run([executable, *argv], check=False)
    except OSError as exc:
        print(
            "zzb-codex-ui: 无法启动 zzb-codex（"
            + executable
            + "）："
            + describe_os_error(exc),
            file=sys.stderr,
        )
        return 127
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def _resolve_start_directory(path: object | None) -> str:
    raw = path if path is not None else os.getcwd()
    resolved = normalize_path(raw)
    if not os.path.isdir(resolved):
        raise NotADirectoryError("不是目录：" + resolved)
    return resolved


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_TITLE,
        description=(
            "zzb-codex 常驻终端工作台（Python + tmux）；"
            "其它命令和参数原样透传给 zzb-codex。"
        ),
        epilog=(
            "示例：\n"
            "  zzb-codex-ui                          # 浏览当前目录\n"
            "  zzb-codex-ui ~/project                # 从指定目录启动\n"
            "  zzb-codex-ui -C ~/project             # 同上\n"
            "  zzb-codex-ui resume                   # 浏览当前目录（resume 无 ID）\n"
            "  zzb-codex-ui resume -C ~/project      # 浏览指定目录\n"
            "  zzb-codex-ui --list-json ~/project    # 非交互输出会话元数据\n"
            "  zzb-codex-ui resume --last            # 在中央对话区恢复最近会话\n"
            "  zzb-codex-ui exec ...                 # 原生命令原样透传\n"
            "注意：resume 会话ID/--last 也保留导航区；resume --help 显示原版帮助。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="启动时选中的目录，默认当前目录；仅在非 resume 模式接受",
    )
    parser.add_argument(
        "-C",
        "--cd",
        dest="cd",
        metavar="DIR",
        help="指定界面起始目录；不能与目录位置参数同时使用",
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        metavar="DIR",
        help="zzb-codex 状态目录（数据库位于其 db/state_5.sqlite），默认 "
        + DEFAULT_STATE_DIR,
    )
    parser.add_argument(
        "--list-json",
        action="store_true",
        help="不进入界面，以 JSON 输出所选目录的历史会话元数据",
    )
    return parser


def _run_tui(start_dir: str, state_dir: object) -> int:
    if not os.environ.get("ZZB_UI_WORKSPACE"):
        from workspace import launch_workspace
        return launch_workspace(start_dir, os.fspath(state_dir))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "zzb-codex-ui: 交互界面需要 TTY；非交互请使用 --list-json",
            file=sys.stderr,
        )
        return 2
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    def wrapped(stdscr: "curses.window") -> None:
        app = BrowserApp(stdscr, start_dir, state_dir)
        app.run()

    try:
        curses.wrapper(wrapped)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            "zzb-codex-ui: 交互界面启动/运行失败：" + sanitize_text(exc, limit=500),
            file=sys.stderr,
        )
        return 1
    return 0


def _persistent_resume_directory(argv: list[str]) -> str | None:
    """Recognize the resume command without treating option values as commands."""
    controls = argv[:argv.index("--")] if "--" in argv else argv
    if any(arg in ("-h", "--help", "-V", "--version") for arg in controls):
        return None
    value_options = {"-C", "--cd", "-c", "--config", "-m", "--model",
                     "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval",
                     "--enable", "--disable", "--remote", "--remote-auth-token-env",
                     "--local-provider", "--add-dir", "-i", "--image"}
    index = 0
    while index < len(controls):
        arg = controls[index]
        if arg in value_options:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            if arg != "resume":
                return None
            break
    else:
        return None
    directory = os.getcwd()
    index = 0
    while index < len(controls):
        arg = controls[index]
        if arg in ("-C", "--cd") and index + 1 < len(controls):
            directory = controls[index + 1]
            index += 2
        elif arg.startswith("--cd="):
            directory = arg.split("=", 1)[1]
            index += 1
        elif arg.startswith("-C") and len(arg) > 2:
            directory = arg[2:]
            index += 1
        elif arg in value_options:
            index += 2
        else:
            index += 1
    return normalize_path(directory)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    route, routed_argv = _route_cli(raw_argv)
    if route == "native":
        resume_dir = _persistent_resume_directory(routed_argv)
        if resume_dir is not None:
            from workspace import launch_workspace
            return launch_workspace(resume_dir, DEFAULT_STATE_DIR,
                                    initial_argv=[DEFAULT_LAUNCHER, *routed_argv])
        return _run_native(routed_argv)
    if route == "error":
        print("zzb-codex-ui: " + routed_argv[0], file=sys.stderr)
        return 2

    parser = build_parser()
    args = parser.parse_args(routed_argv)
    state_dir = args.state_dir
    selected_directory = args.cd if args.cd is not None else args.path

    if args.list_json:
        raw_directory = selected_directory if selected_directory is not None else os.getcwd()
        try:
            directory = _resolve_start_directory(selected_directory)
        except (OSError, ValueError, TypeError) as exc:
            payload: dict[str, object] = {
                "directory": os.path.abspath(os.path.expanduser(str(raw_directory))),
                "database": state_database_path(state_dir),
                "missing_database": False,
                "error": "目录路径无效：" + describe_os_error(exc),
                "count": 0,
                "sessions": [],
            }
            _print_json(payload)
            return 2
        payload = list_json_payload(directory, state_dir)
        _print_json(payload)
        if payload.get("error"):
            return 1
        return 0

    try:
        start_dir = _resolve_start_directory(selected_directory)
    except (OSError, ValueError, TypeError) as exc:
        print("zzb-codex-ui: " + describe_os_error(exc), file=sys.stderr)
        return 2
    return _run_tui(start_dir, state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
