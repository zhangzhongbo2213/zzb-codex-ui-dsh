import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import app
import preview

class PreviewTests(unittest.TestCase):
    def test_finder_reveal_files_directories_and_cancel_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/"文件 ; $(echo nope).txt"; file.write_text('test')
            browser = app.BrowserApp(mock.Mock(), tmp, tmp)
            with mock.patch.object(app.sys, 'platform', 'darwin'), mock.patch.object(app.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                for path in (tmp, str(file)):
                    browser.tree.select_path(path)
                    browser._pending_preview = (0, path)
                    browser._handle_key(ord('o'))
                    self.assertIsNone(browser._pending_preview)
                    self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '-R', path])
                    self.assertNotIn('shell', run.call_args.kwargs)
                file.unlink(); run.reset_mock()
                browser._handle_key(ord('o')); run.assert_not_called()
                self.assertEqual(browser.status_kind, 'error')

    def test_bounded_text_and_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"text"
            p.write_text("    indent\n\tTAB\x1b[31mCONTROL\u202e")
            lines, truncated = preview.read_preview(p)
            self.assertFalse(truncated)
            self.assertEqual(lines[0], "    indent")
            self.assertTrue(lines[1].startswith("    TAB"))
            self.assertNotIn("\x1b", "".join(lines))
            self.assertNotIn("\u202e", "".join(lines))
            p.write_bytes(b'a' * (preview.LIMIT + 100))
            lines, truncated = preview.read_preview(p)
            self.assertTrue(truncated)
            self.assertEqual(len(lines[0]), preview.LIMIT)
            p.write_bytes(b'a\0b')
            self.assertIn('二进制', preview.read_preview(p)[0][0])

    def test_special_files_rejected_without_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp)/'fifo'; os.mkfifo(fifo)
            for path in [tmp, fifo]:
                with mock.patch.object(preview.os, 'open') as opened:
                    with self.assertRaises(ValueError): preview.read_preview(path)
                    opened.assert_not_called()

    def test_image_viewer_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'image ; $(touch nope).PNG'; p.write_bytes(b'fake')
            with mock.patch.object(preview.sys, 'platform', 'darwin'), mock.patch.object(preview.shutil, 'which', return_value='/usr/bin/qlmanage'), mock.patch.object(preview.subprocess, 'Popen') as popen:
                popen.return_value.wait.return_value = 0
                preview.open_image(p)
                self.assertEqual(popen.call_args.args[0], ['/usr/bin/qlmanage', '-p', str(p)])
                self.assertNotIn('shell', popen.call_args.kwargs)
            with mock.patch.object(preview.sys, 'platform', 'linux'), mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError): preview.image_command(p)

    def test_click_delay_double_click_and_selection_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'text.txt'; p.write_text('hello')
            b = app.BrowserApp(mock.Mock(), tmp, tmp)
            b._layout.left = app.Rect(0, 0, 15, 30)
            idx = next(i for i,n in enumerate(b.tree.visible_nodes) if n.path == str(p))
            with mock.patch.object(b, '_preview_selected') as show, mock.patch.object(b, '_copy_selected_directory') as copy:
                def click(button):
                    with mock.patch.object(app.curses, 'getmouse', return_value=(0, 1, idx + 1, 0, button)):
                        b._handle_mouse()
                click(app.curses.BUTTON1_CLICKED)
                b._poll_preview(); show.assert_not_called()
                click(app.curses.BUTTON1_DOUBLE_CLICKED)
                self.assertIsNone(b._pending_preview); copy.assert_called_once()
                b._last_dir_click = None
                click(app.curses.BUTTON1_CLICKED)
                b._pending_preview = (0, str(p))
                b._poll_preview(); show.assert_called_once()
                show.reset_mock(); b._pending_preview = (0, str(p))
                b.tree.move_to_first(); b._poll_preview()
                show.assert_not_called(); self.assertIsNone(b._pending_preview)
                b.tree.select_path(str(p)); b._handle_key(ord('p')); show.assert_called_once()
                b._pending_preview = (0, str(p)); b._handle_key(ord('j'))
                self.assertIsNone(b._pending_preview)

if __name__ == '__main__': unittest.main()
