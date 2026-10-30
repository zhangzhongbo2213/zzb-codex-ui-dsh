"""Real tmux popup with a PTY client and a live simulated conversation."""
import fcntl
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from workspace import Workspace, TMUX

@unittest.skipUnless(shutil.which(TMUX), 'tmux required')
class PreviewIntegrationTest(unittest.TestCase):
    def test_popup_does_not_interrupt_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); state = root/'state'; state.mkdir()
            file = root/'z file.txt'; file.write_text('PREVIEW_SENTINEL\n' + 'scrolling line\n'*100)
            ws = Workspace('zzb-preview-test-'+uuid.uuid4().hex[:8], 'preview-test')
            master, slave = os.openpty(); client = None
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
                os.set_blocking(master, False)
                ws.ensure(str(root), str(state), cols=140, rows=40)
                nav = ws._get('@zzb-nav-pane')
                chat = ws.open([sys.executable, '-u', '-c', 'import sys; print("CHAT_READY");\nfor line in sys.stdin: print("ECHO:"+line.strip())'], str(root), 'fake')
                pid = ws.run('display-message','-p','-t',chat,'#{pane_pid}').strip()
                env = dict(os.environ, TERM='xterm-256color'); env.pop('TMUX',None); env.pop('TMUX_PANE',None)
                client = subprocess.Popen([TMUX,'-L',ws.socket_name,'attach-session','-t',ws.session_name],stdin=slave,stdout=slave,stderr=slave,env=env)
                def wait(predicate):
                    end = time.monotonic()+8
                    while time.monotonic()<end:
                        try: output.extend(os.read(master,65536))
                        except BlockingIOError: pass
                        if predicate(): return
                        time.sleep(.05)
                    self.fail('Timed out; terminal tail: '+repr(bytes(output[-1500:])))
                output = bytearray()
                wait(lambda: 'z file.txt' in ws.run('capture-pane','-p','-t',nav))
                ws.run('select-pane','-t',nav)
                ws.run('send-keys','-t',nav,'G','p')
                wait(lambda: b'PREVIEW_SENTINEL' in output)
                ws.run('send-keys','-t',chat,'-l','still-running')
                ws.run('send-keys','-t',chat,'Enter')
                wait(lambda: 'ECHO:still-running' in ws.run('capture-pane','-p','-t',chat))
                self.assertEqual(pid,ws.run('display-message','-p','-t',chat,'#{pane_pid}').strip())
                output.clear(); os.write(master,b'q')
                wait(lambda: '文件树'.encode() in output)
                output.clear(); ws.run('send-keys','-t',nav,'p')
                wait(lambda: b'PREVIEW_SENTINEL' in output)
                os.write(master,b'q')
            finally:
                try: ws.run('kill-server')
                except subprocess.SubprocessError: pass
                if client:
                    client.terminate()
                    try: client.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        client.kill(); client.wait(timeout=2)
                os.close(master); os.close(slave)
