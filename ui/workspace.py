"""常驻 tmux 工作台：左侧目录，中央真实交互 CLI，右侧历史。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid

_TMUX_COMMAND = os.path.expanduser(os.environ.get("ZZB_TMUX") or "tmux")
TMUX = shutil.which(_TMUX_COMMAND) or _TMUX_COMMAND
DEFAULT_SOCKET = "zzb-codex-ui"
_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
_MAIN = "@zzb-main-window"
_NAV = "@zzb-nav-pane"
_RIGHT = "@zzb-right-pane"
_CHAT = "@zzb-chat-pane"
_LAYOUT = "@zzb-layout-version"
_LAYOUT_VERSION = "3"
_DIRECTORY = "@zzb-directory"
_KEY = "@zzb-key"
_PANES = "#{window_id}\t#{pane_id}\t#{pane_dead}\t#{@zzb-key}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()


def _encode_directory(path: str) -> str:
    value = os.fsdecode(os.fspath(path))
    return base64.b64encode(value.encode("utf-8", "surrogatepass")).decode("ascii")


def _decode_directory(value: str) -> str | None:
    if not value:
        return None
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
        return raw.decode("utf-8", "surrogatepass")
    except (ValueError, UnicodeError, binascii.Error):
        return None


def _sizes(cols: int, rows: int) -> tuple[int, int, int, int]:
    """返回窗口尺寸与左右侧栏的目标宽度，中央占据剩余宽度。"""
    cols = max(3, int(cols))
    rows = max(4, int(rows))
    left = min(28, max(14, cols // 6))
    right = min(38, max(20, cols // 5))
    # 极小终端先给中央留出空间，再按比例收缩两侧，避免算出负宽度。
    reserve = min(40, max(1, cols // 2))
    room = max(2, cols - reserve)
    if left + right > room:
        total = left + right
        left = max(1, room * left // total)
        right = max(1, room - left)
    return cols, rows, left, right


class Workspace:
    def __init__(self, socket_name: str = DEFAULT_SOCKET, session_name: str = "zzb-codex-ui"):
        self.socket_name = socket_name
        self.session_name = session_name

    def run(self, *tmuxargs: str) -> str:
        """运行 tmux 并返回 stdout；不经过 shell。"""
        completed = subprocess.run(
            [TMUX, "-L", self.socket_name, "-f", "/dev/null", *tmuxargs],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout

    def _exists(self) -> bool:
        try:
            self.run("has-session", "-t", self.session_name)
        except subprocess.CalledProcessError:
            return False
        return True

    def _get(self, option: str) -> str:
        try:
            return self.run(
                "show-options", "-qv", "-t", self.session_name, option
            ).strip()
        except subprocess.CalledProcessError:
            return ""

    def _set(self, option: str, value: str) -> None:
        self.run("set-option", "-t", self.session_name, option, value)

    def _panes(self) -> list[dict[str, str]]:
        result = []
        for line in self.run(
            "list-panes", "-s", "-t", self.session_name, "-F", _PANES
        ).splitlines():
            window, pane, dead, key = line.split("\t")
            result.append(
                {"window": window, "pane": pane, "dead": dead == "1", "key": key}
            )
        return result

    def _configure(self) -> None:
        self.run("set-option", "-g", "mouse", "on")
        self.run("set-window-option", "-g", "remain-on-exit", "on")
        self.run("set-window-option", "-g", "pane-border-style", "fg=colour239")
        self.run("set-window-option", "-g", "pane-active-border-style", "fg=colour244")
        self.run("set-option", "-g", "status", "on")
        self.run("set-option", "-g", "status-style", "fg=colour245,bg=colour234")
        self.run("set-window-option", "-g", "remain-on-exit-format",
                 "对话已结束 · F6 返回导航选择会话")
        self.run("set-option", "-g", "status-left",
                 "#[fg=colour109] F6#[fg=colour245] 切区（目录/对话/历史）"
                 " · 鼠标点击切区 · #[fg=colour139]q#[fg=colour245] 暂离保留")
        self.run("set-option", "-g", "status-right", "")
        self.run("set-option", "-g", "status-left-length", "120")
        self.run("set-window-option", "-g", "window-status-format", "")
        self.run("set-window-option", "-g", "window-status-current-format", "")
        self.run("bind-key", "-n", "F6", "select-pane", "-t", ":.+")

    def _nav_command(
        self,
        start_dir: str,
        state_dir: str,
        navigation_argv: list[str] | None,
        role: str = "directories",
    ) -> str:
        if navigation_argv is None:
            argv = [sys.executable, _APP, "--state-dir", state_dir, start_dir]
        else:
            argv = [str(arg) for arg in navigation_argv]
        return shlex.join(
            [
                "env",
                "ZZB_UI_WORKSPACE=" + self.session_name,
                "ZZB_UI_SOCKET=" + self.socket_name,
                "ZZB_UI_ROLE=" + role,
                *argv,
            ]
        )

    @staticmethod
    def _idle_command() -> str:
        return shlex.join([sys.executable, os.path.abspath(__file__), "--idle"])

    @staticmethod
    def _command(argv: list[str]) -> str:
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("argv 必须是非空字符串列表")
        return "exec " + shlex.join(argv)

    def _split_placeholder(self, target: str, start_dir: str) -> str:
        return self.run(
            "split-window", "-d", "-h", "-t", target,
            "-c", start_dir,
            "-P", "-F", "#{pane_id}", self._idle_command(),
        ).strip()

    def _split_right(self, chat: str, start_dir: str, command: str) -> str:
        return self.run(
            "split-window", "-d", "-h", "-t", chat,
            "-c", start_dir,
            "-P", "-F", "#{pane_id}", command,
        ).strip()

    def _current_chat(self, main: str, nav: str, panes: list[dict[str, str]]) -> str:
        """只认 @zzb-chat-pane，绝不把右侧历史栏当聊天区。"""
        chat = self._get(_CHAT)
        right = self._get(_RIGHT)
        record = next((p for p in panes if p["pane"] == chat), None)
        if (
            not chat
            or chat == nav
            or chat == right
            or record is None
            or record["window"] != main
        ):
            raise RuntimeError("工作台 chat pane 无效")
        return chat

    def set_directory(self, path: str) -> None:
        self._set(_DIRECTORY, _encode_directory(path))

    def get_directory(self) -> str | None:
        return _decode_directory(self._get(_DIRECTORY))

    def focus_role(self, role: str) -> None:
        option = {"directories": _NAV, "sessions": _RIGHT, "chat": _CHAT}[role]
        pane = self._get(option)
        if not pane:
            raise RuntimeError("侧栏尚未就绪")
        self.run("select-window", "-t", self._get(_MAIN))
        self.run("select-pane", "-t", pane)

    def ensure(
        self, start_dir: str, state_dir: str, cols: int = 120, rows: int = 40,
        navigation_argv: list[str] | None = None,
    ) -> "Workspace":
        cols, rows, left_width, right_width = _sizes(cols, rows)
        cols = max(12, cols)
        start_dir, state_dir = os.fspath(start_dir), os.fspath(state_dir)
        left_command = self._nav_command(start_dir, state_dir, navigation_argv, "directories")
        right_command = self._nav_command(start_dir, state_dir, navigation_argv, "sessions")
        created = not self._exists()
        if created:
            main, nav = self.run(
                "new-session", "-d", "-s", self.session_name, "-n", "main",
                "-x", str(cols), "-y", str(rows), "-c", start_dir,
                "-P", "-F", "#{window_id}\t#{pane_id}", left_command,
            ).strip().split("\t")
            self._set(_MAIN, main)
            self._set(_NAV, nav)
        self._configure()
        if self.get_directory() is None:
            self.set_directory(start_dir)
        main, nav = self._get(_MAIN), self._get(_NAV)
        records = {record["pane"]: record for record in self._panes()}
        if nav not in records or records[nav]["window"] != main:
            raise RuntimeError("工作台目录面板不在主窗口")
        self.run("resize-window", "-t", main, "-x", str(cols), "-y", str(max(3, rows - 1)))
        chat = self._get(_CHAT)
        if not chat and created:
            chat = self._split_placeholder(nav, start_dir)
            self._set(_CHAT, chat)
        elif chat not in records or records[chat]["window"] != main or chat == nav:
            raise RuntimeError("工作台对话面板无效，已保留所有进程")
        right = self._get(_RIGHT)
        new_right = right not in records
        if new_right:
            # Full-height split also works when migrating the old stacked layout.
            right = self.run(
                "split-window", "-d", "-h", "-f", "-t", main,
                "-c", start_dir, "-P", "-F", "#{pane_id}", right_command,
            ).strip()
            self._set(_RIGHT, right)
        elif right in (chat, nav) or records[right]["window"] != main:
            raise RuntimeError("工作台历史面板无效，已保留所有进程")
        migrating = self._get(_LAYOUT) != _LAYOUT_VERSION
        if migrating and not created:
            # Only restart our navigation UI. Never respawn or terminate chat.
            self.run("respawn-pane", "-k", "-t", nav, "-c", start_dir, left_command)
            if not new_right:
                self.run("respawn-pane", "-k", "-t", right, "-c", start_dir, right_command)
        else:
            for pane, command in ((nav, left_command), (right, right_command)):
                if pane in records and records[pane]["dead"]:
                    self.run("respawn-pane", "-t", pane, "-c", start_dir, command)
        if created or migrating or new_right:
            self.run("select-layout", "-t", main, "even-horizontal")
            self.run("resize-pane", "-t", nav, "-x", str(left_width))
            self.run("resize-pane", "-t", right, "-x", str(right_width))
            self._set(_LAYOUT, _LAYOUT_VERSION)
        self.run("set-window-option", "-t", main, "window-size", "latest")
        self.run("select-window", "-t", main)
        self.run("select-pane", "-t", nav)
        return self

    def open(self, argv: list[str], cwd: str, key: str | None = None) -> str:
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("argv 必须是非空字符串列表")
        cwd = os.fspath(cwd)
        raw_key = key if key is not None else "auto:" + uuid.uuid4().hex
        hashed = _hash_key(raw_key)
        command = self._command(argv)

        main = self._get(_MAIN)
        nav = self._get(_NAV)
        panes = self._panes()
        target = None
        if key is not None:
            matches = [
                p for p in panes if p["key"] == hashed and p["pane"] not in (nav, self._get(_RIGHT))
            ]
            if matches:
                record = next((p for p in matches if not p["dead"]), matches[0])
                target = record["pane"]
                if record["dead"]:
                    self.run("respawn-pane", "-t", target, "-c", cwd, command)
                self.run("set-option", "-p", "-t", target, _KEY, hashed)

        if target is None:
            target = self.run(
                "new-window", "-d", "-t", f"{self.session_name}:", "-n", "park",
                "-c", cwd, "-P", "-F", "#{pane_id}", command,
            ).strip()
            self.run("set-option", "-p", "-t", target, _KEY, hashed)

        panes = self._panes()
        if not main:
            main = self._get(_MAIN)
        if not nav:
            nav = self._get(_NAV)
        target_record = next(p for p in panes if p["pane"] == target)
        if target_record["window"] != main:
            chat = self._current_chat(main, nav, panes)
            if target in (nav, self._get(_RIGHT)) or chat in (nav, self._get(_RIGHT)):
                raise RuntimeError("拒绝交换侧栏 pane")
            self.run("swap-pane", "-d", "-s", target, "-t", chat)
            self._set(_CHAT, target)
        else:
            self._set(_CHAT, target)
        self.run("select-window", "-t", f"{self.session_name}:{main}")
        self.run("select-pane", "-t", target)
        return target

    def detach(self) -> None:
        self.run("detach-client", "-s", self.session_name)


def _resume_key(argv: list[str]) -> str | None:
    values = {"-C", "--cd", "-c", "--config", "-m", "--model", "-p", "--profile",
              "-s", "--sandbox", "-a", "--ask-for-approval", "--enable", "--disable",
              "--remote", "--remote-auth-token-env", "--local-provider", "--add-dir",
              "-i", "--image"}
    flags = {"--all", "--include-non-interactive", "--oss", "--strict-config",
             "--search", "--no-alt-screen", "--no-daemon", "--worktree",
             "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
             "--dangerously-bypass-hook-trust"}
    found_command = False
    index = 1  # argv includes the launcher executable.
    while index < len(argv):
        token = argv[index]
        if token == "--last":
            return None
        if token in values:
            index += 2
            continue
        if token in flags or token.split("=", 1)[0] in values:
            index += 1
            continue
        if any(token.startswith(short) and len(token) > 2 for short in ("-C", "-c", "-m", "-p", "-s", "-a", "-i")):
            index += 1
            continue
        if token == "--" and found_command:
            return "resume:" + argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("-"):
            return None  # Unknown option arity: never mistake its value for an ID.
        if found_command:
            return "resume:" + token
        if token != "resume":
            return None
        found_command = True
        index += 1
    return None


def _session_name(start_dir: str, state_dir: str) -> str:
    start = os.path.realpath(os.path.expanduser(os.fspath(start_dir)))
    state = os.path.realpath(os.path.expanduser(os.fspath(state_dir)))
    digest = hashlib.sha256(
        (start + "\0" + state).encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]
    return "zzb-" + digest


def launch_workspace(
    start_dir: str, state_dir: str, initial_argv: list[str] | None = None
) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("工作台需要 TTY 终端", file=sys.stderr)
        return 2
    if not (os.path.isfile(TMUX) and os.access(TMUX, os.X_OK)):
        print(
            "未找到可执行的 tmux：" + TMUX + "\n"
            "请安装 tmux：macOS 可用 brew install tmux；"
            "Linux 可用 apt install tmux；"
            "也可用 ZZB_TMUX 指定 tmux 可执行文件。",
            file=sys.stderr,
        )
        return 2

    workspace = Workspace(session_name=_session_name(start_dir, state_dir))
    size = shutil.get_terminal_size((120, 40))
    try:
        workspace.ensure(start_dir, state_dir, size.columns, size.lines)
        if initial_argv:
            workspace.open(initial_argv, start_dir, key=_resume_key(initial_argv))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("无法打开工作台：" + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    main = workspace._get(_MAIN)
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    command = [
        TMUX, "-L", workspace.socket_name, "-f", "/dev/null",
        "attach-session", "-t", f"{workspace.session_name}:{main}",
    ]
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except OSError as exc:
        print("无法连接工作台：" + str(exc), file=sys.stderr)
        return 1


def _idle() -> None:
    print("等待 Codex 对话（F6 切换导航/聊天）", flush=True)
    while True:
        try:
            if not sys.stdin.readline():
                time.sleep(1)
        except (OSError, KeyboardInterrupt):
            time.sleep(1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--idle"]:
        _idle()
    else:
        print("用法: python3 workspace.py --idle", file=sys.stderr)
        raise SystemExit(2)
