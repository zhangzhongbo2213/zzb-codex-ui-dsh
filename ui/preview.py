"""Read-only, bounded file preview; native image windows when available."""
from __future__ import annotations
import curses
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import unicodedata

LIMIT = 256 * 1024
IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.avif'}


def is_image(path):
    return Path(path).suffix.lower() in IMAGES


def read_preview(path):
    """Never read a special file, and bound memory even for enormous lines."""
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError('只能预览普通文件')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('只能预览普通文件')
        data = stream.read(LIMIT + 1)
    truncated = len(data) > LIMIT
    data = data[:LIMIT]
    if b'\0' in data:
        return [f'二进制文件 · {info.st_size:,} 字节', '此窗口不显示二进制内容。'], False
    text = data.decode('utf-8-sig', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(c if c in '\n\t' or unicodedata.category(c) not in {'Cc','Cf','Cs','Zl','Zp'} else ' ' for c in text)
    return text.expandtabs(4).split('\n'), truncated


def image_command(path):
    path = os.path.abspath(path)
    if not is_image(path) or not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError('请选择普通图片文件')
    if sys.platform == 'darwin' and shutil.which('qlmanage'):
        return [shutil.which('qlmanage'), '-p', path]
    if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('xdg-open'):
        return [shutil.which('xdg-open'), path]
    raise ValueError('当前环境无法显示图片：macOS 需要 Quick Look，Linux 需要图形桌面和 xdg-open')


def open_image(path):
    proc = subprocess.Popen(image_command(path), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        code = proc.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, daemon=True).start()
    else:
        if code:
            raise OSError('图片查看器启动失败（退出码 %s）' % code)


def view(screen, path):
    from app import _clip_add, sanitize_text
    try:
        lines, truncated = read_preview(path)
    except (OSError, ValueError) as exc:
        lines, truncated = ['无法预览：' + sanitize_text(str(exc))], False
    try:
        curses.curs_set(0)
        curses.start_color(); curses.use_default_colors()
        curses.init_pair(1, 252, 234); curses.init_pair(2, 109, 234)
        normal, accent = curses.color_pair(1), curses.color_pair(2)
    except curses.error:
        normal, accent = 0, curses.A_BOLD
    screen.keypad(True)
    screen.bkgd(' ', normal)
    screen.timeout(200)
    try: curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error: pass
    offset = horizontal = 0
    while True:
        height, width = screen.getmaxyx()
        viewport = max(1, height - 3)
        offset = max(0, min(offset, max(0, len(lines) - viewport)))
        screen.erase()
        _clip_add(screen, 0, 0, '预览 · ' + sanitize_text(path), accent, width - 1)
        for row, line in enumerate(lines[offset:offset + viewport], 1):
            if row >= height - 1: break
            _clip_add(screen, row, 0, line[horizontal:], normal, width - 1)
        footer = f'{offset + 1}/{len(lines)} · ↑↓/jk 滚动 · h/l 横移 · q/Esc 关闭'
        if truncated: footer += ' · 已截断至 256 KiB'
        _clip_add(screen, max(0, height - 1), 0, footer, accent, width - 1)
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), 27): break
        if key in (ord('j'), curses.KEY_DOWN): offset += 1
        elif key in (ord('k'), curses.KEY_UP): offset -= 1
        elif key in (ord('l'), curses.KEY_RIGHT): horizontal = min(LIMIT, horizontal + 4)
        elif key in (ord('h'), curses.KEY_LEFT): horizontal = max(0, horizontal - 4)
        elif key in (curses.KEY_NPAGE, ord(' ')): offset += viewport
        elif key == curses.KEY_PPAGE: offset -= viewport
        elif key in (ord('g'), curses.KEY_HOME): offset = 0
        elif key in (ord('G'), curses.KEY_END): offset = len(lines)
        elif key == curses.KEY_MOUSE:
            try:
                buttons = curses.getmouse()[4]
                if buttons & getattr(curses, 'BUTTON4_PRESSED', 0): offset -= 3
                if buttons & getattr(curses, 'BUTTON5_PRESSED', 0): offset += 3
            except curses.error: pass


if __name__ == '__main__':
    import locale
    locale.setlocale(locale.LC_ALL, '')
    if len(sys.argv) != 2: raise SystemExit('usage: preview.py FILE')
    curses.wrapper(view, os.path.abspath(sys.argv[1]))
