"""Exercise real navigation and interactive chat together, using no model calls."""
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid

from workspace import Workspace

HERE = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required for integration tests')
class WorkspaceUiIntegrationTests(unittest.TestCase):
    def test_navigation_stays_visible_during_chat_and_switching(self):
        with tempfile.TemporaryDirectory(prefix='.test-ui-workspace-', dir=HERE) as tmp:
            root = Path(tmp)
            state = root / 'state'
            (state / 'db').mkdir(parents=True)
            with closing(sqlite3.connect(state / 'db' / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads(id TEXT,cwd TEXT,title TEXT,updated_at INTEGER,archived INTEGER)')
                db.executemany('INSERT INTO threads VALUES(?,?,?,?,?)', [
                    ('session-a', str(root), 'Conversation A', 20, 0),
                    ('session-b', str(root), 'Conversation B', 10, 0),
                ])
                db.execute('ALTER TABLE threads ADD COLUMN model TEXT')
                db.execute('ALTER TABLE threads ADD COLUMN preview TEXT')
                db.execute('ALTER TABLE threads ADD COLUMN tokens_used INTEGER')
                db.execute("UPDATE threads SET model='test-model', preview='A useful preview', tokens_used=1234")
            (root / 'empty').mkdir()
            fake = root / 'fake chat'
            fake.write_text('#!' + sys.executable + '\nimport sys\n'
                            'print("FAKE_CHAT " + repr(sys.argv[1:]), flush=True)\n'
                            'for line in sys.stdin:\n'
                            ' if line.strip() == "exit": break\n'
                            ' print("REPLY " + line.strip(), flush=True)\n')
            fake.chmod(0o700)
            socket = 'zzb-ui-e2e-' + uuid.uuid4().hex[:12]
            session = 'ui-test'
            ws = Workspace(socket_name=socket, session_name=session)
            navcode = ('import sys,locale,curses;sys.path.insert(0,sys.argv[1]);import app;'
                       'locale.setlocale(locale.LC_ALL, "");'
                       'curses.wrapper(lambda screen:app.BrowserApp(screen,sys.argv[2],sys.argv[3],launcher=sys.argv[4]).run())')
            navargv = ['/usr/bin/env', 'ZZB_UI_WORKSPACE=' + session,
                       'ZZB_UI_SOCKET=' + socket, sys.executable, '-c', navcode,
                       str(HERE), str(root), str(state), str(fake)]
            try:
                ws.ensure(str(root), str(state), cols=120, rows=40, navigation_argv=navargv)
                def option(name):
                    return ws.run('show-options', '-v', '-t', session, name).strip()
                def capture(pane):
                    return ws.run('capture-pane', '-p', '-J', '-S', '-', '-t', pane)
                def wait_for(predicate):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        result = predicate()
                        if result:
                            return result
                        time.sleep(0.05)
                    self.fail('Timed out waiting for workspace state: ' + repr({p: capture(p) for p in ws.run('list-panes', '-s', '-t', session, '-F', '#{pane_id}').splitlines()}))
                nav = option('@zzb-nav-pane')
                right = option('@zzb-right-pane')
                idle = option('@zzb-chat-pane')
                main = option('@zzb-main-window')
                wait_for(lambda: 'Conversation A' in capture(right))
                self.assertIn('Summary', capture(right))
                self.assertIn('test-model', capture(right))
                self.assertIn('A useful preview', ''.join(line[1:-1].rstrip() for line in capture(right).splitlines() if line.startswith('│')))
                ws.run('send-keys', '-t', nav, 'j')
                wait_for(lambda: '历史 · 0' in capture(right))
                ws.run('send-keys', '-t', nav, 'k')
                wait_for(lambda: 'Conversation A' in capture(right))
                ws.run('send-keys', '-t', right, 'Enter')
                wait_for(lambda: option('@zzb-chat-pane') != idle)
                first = option('@zzb-chat-pane')
                wait_for(lambda: 'session-a' in capture(first))
                pid = ws.run('display-message', '-p', '-t', first, '#{pane_pid}').strip()
                ws.run('send-keys', '-t', first, '-l', 'hello from test')
                ws.run('send-keys', '-t', first, 'Enter')
                wait_for(lambda: 'REPLY hello from test' in capture(first))
                self.assertIn('Conversation A', capture(right))
                ws.run('send-keys', '-t', right, 'j', 'Enter')
                wait_for(lambda: option('@zzb-chat-pane') != first)
                second = option('@zzb-chat-pane')
                wait_for(lambda: 'session-b' in capture(second))
                ws.run('send-keys', '-t', right, 'k', 'Enter')
                wait_for(lambda: option('@zzb-chat-pane') == first)
                self.assertEqual(pid, ws.run('display-message', '-p', '-t', first, '#{pane_pid}').strip())
                self.assertIn('REPLY hello from test', capture(first))
                panes = ws.run('list-panes', '-t', main, '-F', '#{pane_id}').splitlines()
                self.assertEqual(set(panes), {nav, first, right})
                ws.run('resize-window', '-t', main, '-x', '100', '-y', '30')
                self.assertIn('Conversation A', capture(right))
                ws.run('send-keys', '-t', first, '-l', 'exit')
                ws.run('send-keys', '-t', first, 'Enter')
                wait_for(lambda: ws.run('display-message', '-p', '-t', first, '#{pane_dead}').strip() == '1')
                self.assertEqual(ws.run('display-message', '-p', '-t', nav, '#{pane_dead}').strip(), '0')
                self.assertEqual(ws.run('display-message', '-p', '-t', right, '#{pane_dead}').strip(), '0')
            finally:
                try:
                    ws.run('kill-server')  # This uniquely named test server only.
                except Exception:
                    pass


if __name__ == '__main__':
    unittest.main()
