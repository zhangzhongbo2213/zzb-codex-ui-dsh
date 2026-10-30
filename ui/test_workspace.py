import os
import sys
import tempfile
import time
import unittest
import uuid

import workspace

NAV = [sys.executable, "-c", "import time; time.sleep(60)"]
CHAT = """import os, sys
print("PID:" + str(os.getpid()), flush=True)
for line in sys.stdin:
    line = line.rstrip("\\n")
    if line == "quit":
        break
    print("ECHO:" + line, flush=True)
"""


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zzb-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.start = os.path.join(self.temp.name, "start")
        self.state = os.path.join(self.temp.name, "state")
        os.mkdir(self.start)
        os.mkdir(self.state)
        self.socket = "zzb-test-" + uuid.uuid4().hex[:12]
        self.session = "test-main"
        self.ws = workspace.Workspace(self.socket, self.session)
        self.addCleanup(self._cleanup)
        self.ws.ensure(
            self.start, self.state, cols=100, rows=30, navigation_argv=NAV
        )

    def _cleanup(self):
        try:
            self.ws.run("kill-server")
        except Exception:
            pass

    def _option(self, name):
        return self.ws.run("show-options", "-v", "-t", self.session, name).strip()

    def _panes(self):
        out = self.ws.run(
            "list-panes", "-s", "-t", self.session,
            "-F", "#{window_id}\t#{pane_id}\t#{pane_top}\t#{pane_height}\t#{pane_dead}",
        )
        rows = []
        for line in out.splitlines():
            window, pane, top, height, dead = line.split("\t")
            rows.append((window, pane, int(top), int(height), dead == "1"))
        return rows

    def _pane_pid(self, pane):
        return self.ws.run(
            "display-message", "-p", "-t", pane, "#{pane_pid}"
        ).strip()

    def _wait_for(self, pane, needle, timeout=5.0):
        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            output = self.ws.run("capture-pane", "-p", "-J", "-S", "-", "-t", pane)
            if needle in output:
                return output
            time.sleep(0.05)
        self.fail("pane %s 未输出 %r：\n%s" % (pane, needle, output))

    def _send(self, pane, text):
        self.ws.run("send-keys", "-l", "-t", pane, text)
        self.ws.run("send-keys", "-t", pane, "Enter")

    def test_late_session_id_reuses_original_new_conversation(self):
        from unittest import mock
        original = self.ws.open([sys.executable, "-c", CHAT], self.start)
        self._wait_for(original, "PID:")
        pid = self._pane_pid(original)
        other = self.ws.open([sys.executable, "-c", CHAT], self.start, "other")
        identifier = "11111111-1111-4111-8111-111111111111"
        with mock.patch.object(self.ws, "_native_owner", return_value=(self.session, original)):
            restored = self.ws.open([sys.executable, "-c", CHAT], self.start, "resume:" + identifier)
        self.assertEqual(restored, original)
        self.assertEqual(pid, self._pane_pid(restored))
        self._send(restored, "after-switch")
        self._wait_for(restored, "ECHO:after-switch")
        self.assertEqual(len(self._panes()), 5)  # 3 initial panes + two conversations

    def test_other_workspace_owner_prevents_duplicate_start(self):
        from unittest import mock
        before = self._panes()
        with mock.patch.object(self.ws, "_native_owner", return_value=("another-workspace", "%999")):
            with self.assertRaisesRegex(RuntimeError, "已在工作台"):
                self.ws.open([sys.executable, "-c", CHAT], self.start,
                             "resume:11111111-1111-4111-8111-111111111111")
        self.assertEqual(before, self._panes())

    def test_01_ensure_has_nav_and_chat_in_main_window(self):
        main = self._option("@zzb-main-window")
        nav = self._option("@zzb-nav-pane")
        chat = self._option("@zzb-chat-pane")
        right = self._option("@zzb-right-pane")
        panes = self._panes()
        main_panes = [p for p in panes if p[0] == main]
        self.assertEqual(self.ws.run("show-options", "-wv", "-t", main, "window-size").strip(), "latest")
        self.assertEqual(len(main_panes), 3)
        self.assertEqual({p[1] for p in main_panes}, {nav, chat, right})
        nav_row = next(p for p in main_panes if p[1] == nav)
        chat_row = next(p for p in main_panes if p[1] == chat)
        self.assertFalse(nav_row[4])
        self.assertEqual(nav_row[2], 0)
        self.assertGreaterEqual(nav_row[3], 8)
        self.assertEqual(chat_row[2], nav_row[2])
        self.assertEqual(chat_row[3], nav_row[3])
        positions = {}
        for pane in (nav, chat, right):
            x, width = self.ws.run("display-message", "-p", "-t", pane, "#{pane_left} #{pane_width}").split()
            positions[pane] = (int(x), int(width))
        self.assertLess(positions[nav][0], positions[chat][0])
        self.assertLess(positions[chat][0], positions[right][0])
        self.assertGreater(positions[chat][1], positions[nav][1])
        self.assertGreater(positions[chat][1], positions[right][1])

    def test_02_a_b_a_keeps_pane_and_pid_and_nav(self):
        pane_a = self.ws.open([sys.executable, "-c", CHAT], self.start, "A")
        self._wait_for(pane_a, "PID:")
        self._send(pane_a, "hello A")
        self._wait_for(pane_a, "ECHO:hello A")
        pid_a = self._pane_pid(pane_a)

        pane_b = self.ws.open([sys.executable, "-c", CHAT], self.start, "B")
        self._wait_for(pane_b, "PID:")
        self.assertNotEqual(pane_a, pane_b)

        again = self.ws.open([sys.executable, "-c", CHAT], self.start, "A")
        self.assertEqual(again, pane_a)
        self.assertEqual(self._pane_pid(again), pid_a)
        self._send(again, "again")
        self._wait_for(again, "ECHO:again")

        main = self._option("@zzb-main-window")
        nav = self._option("@zzb-nav-pane")
        self.assertEqual(
            self.ws.run("display-message", "-p", "-t", nav, "#{window_id}").strip(),
            main,
        )
        self.assertFalse(
            self.ws.run("display-message", "-p", "-t", nav, "#{pane_dead}").strip()
            == "1"
        )
        self.assertEqual(len([p for p in self._panes() if p[0] == main]), 3)

        self._send(again, "quit")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.ws.run(
                "display-message", "-p", "-t", pane_a, "#{pane_dead}"
            ).strip() == "1":
                break
            time.sleep(0.05)
        self.assertEqual(
            self.ws.run(
                "display-message", "-p", "-t", pane_a, "#{pane_dead}"
            ).strip(),
            "1",
        )
        self.assertEqual(
            self.ws.run("display-message", "-p", "-t", nav, "#{window_id}").strip(),
            main,
        )
        self.assertEqual(
            self.ws.run("display-message", "-p", "-t", nav, "#{pane_dead}").strip(),
            "0",
        )

    def test_migrate_old_layout_preserves_running_chat(self):
        chat = self.ws.open([sys.executable, "-c", CHAT], self.start, "migration")
        self._wait_for(chat, "PID:")
        pid = self._pane_pid(chat)
        right = self._option("@zzb-right-pane")
        main = self._option("@zzb-main-window")
        self.ws.run("kill-pane", "-t", right)  # Only this test's sidebar.
        self.ws.run("set-option", "-u", "-t", self.session, "@zzb-right-pane")
        self.ws.run("set-option", "-u", "-t", self.session, "@zzb-layout-version")
        self.ws.run("select-layout", "-t", main, "even-vertical")
        self.ws.ensure(self.start, self.state, cols=100, rows=30, navigation_argv=NAV)
        self.assertEqual(self._pane_pid(chat), pid)
        self.assertEqual(self._option("@zzb-chat-pane"), chat)
        self.assertEqual(self._option("@zzb-layout-version"), "3")
        self.assertEqual(len([p for p in self._panes() if p[0] == main]), 3)
        self._send(chat, "after migration")
        self._wait_for(chat, "ECHO:after migration")

    def test_shared_directory_and_role_focus(self):
        directory = os.path.join(self.start, "目录 with spaces \n")
        self.ws.set_directory(directory)
        self.assertEqual(self.ws.get_directory(), directory)
        self.ws.focus_role("sessions")
        right = self._option("@zzb-right-pane")
        self.assertEqual(self.ws.run("display-message", "-p", "-t", right, "#{pane_active}").strip(), "1")
        main = self._option("@zzb-main-window")
        left = self._option("@zzb-nav-pane")
        self.ws.run("resize-pane", "-t", left, "-x", "19")
        before = self.ws.run("list-panes", "-t", main, "-F", "#{pane_id} #{pane_left} #{pane_width} #{pane_height}").strip()
        self.ws.ensure(self.start, self.state, cols=100, rows=30, navigation_argv=NAV)
        after = self.ws.run("list-panes", "-t", main, "-F", "#{pane_id} #{pane_left} #{pane_width} #{pane_height}").strip()
        self.assertEqual(before, after)
        self.assertEqual(self.ws.get_directory(), directory)

    def test_03_shell_special_argv_is_not_executed(self):
        cwd = os.path.join(self.temp.name, "cwd with spaces; $HOME")
        os.mkdir(cwd)
        marker = os.path.join(self.temp.name, "INJECTED")
        special = "x; touch " + marker + "; echo injected #"
        code = (
            "import os, sys, time\n"
            "print('ARGS=' + repr(sys.argv[1:]), flush=True)\n"
            "print('CWD=' + os.getcwd(), flush=True)\n"
            "time.sleep(30)\n"
        )
        pane = self.ws.open(
            [sys.executable, "-c", code, special, "two words"], cwd
        )
        output = self._wait_for(pane, "ARGS=")
        self.assertIn(special, output)
        self.assertIn(cwd, output)
        self.assertFalse(os.path.exists(marker), "argv 被 shell 注入执行")


class ResumeKeyTest(unittest.TestCase):
    def test_option_values_are_not_session_ids(self):
        cases = [
            (["cli", "resume", "id"], "resume:id"),
            (["cli", "resume", "-C", "/tmp", "id"], "resume:id"),
            (["cli", "-m", "resume", "resume", "id"], "resume:id"),
            (["cli", "resume", "--last", "-C", "/tmp"], None),
            (["cli", "resume", "--all"], None),
            (["cli", "resume", "--future", "value"], None),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(workspace._resume_key(argv), expected)


if __name__ == "__main__":
    unittest.main()