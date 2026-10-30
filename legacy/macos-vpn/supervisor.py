#!/usr/bin/python3
"""Keep a private iKuuu core alive independently of its graphical application."""
import fcntl
import json
import logging
import logging.handlers
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from core_rpc import Core

BASE = Path(__file__).resolve().parent
STATE = BASE / 'state'
NODE = '🇯🇵 日本S02 | IEPL'
PORT = 17890
STOP = threading.Event()


def status(**fields):
    fields.update(timestamp=time.strftime('%Y-%m-%dT%H:%M:%S%z'), supervisor_pid=os.getpid(),
                  node=NODE, proxy='http://127.0.0.1:' + str(PORT))
    path = STATE / 'status.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(fields, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temp, 0o600)
    temp.replace(path)


def internet_probe():
    result = subprocess.run(['/usr/bin/curl', '--proxy', 'http://127.0.0.1:' + str(PORT),
                             '--noproxy', '', '--connect-timeout', '5', '--max-time', '12',
                             '-s', '-o', '/dev/null', '-w', '%{http_code}',
                             'https://www.gstatic.com/generate_204'],
                            capture_output=True, text=True, timeout=15)
    return result.returncode == 0 and result.stdout == '204'


def main():
    os.umask(0o077)
    lock = (STATE / 'supervisor.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Proxy supervisor is already running')
    handler = logging.handlers.RotatingFileHandler(BASE / 'logs/service.log', maxBytes=1048576, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    restarts = 0
    while not STOP.is_set():
        core = None
        try:
            config = json.loads((STATE / 'config.json').read_text())
            assert [p['name'] for p in config['proxies']] == [NODE]
            assert config['rules'] == ['MATCH,' + NODE]
            assert config['mixed-port'] == PORT and config['allow-lan'] is False
            assert not config['tun']['enable']
            with socket.socket() as test:
                test.bind(('127.0.0.1', PORT))
            core = Core(BASE / 'iKuuuVPNCore', STATE)
            result = core.call('initClash', json.dumps({'home-dir': str(STATE), 'version': 0}))
            if result.get('code') != 0 or result.get('data') is not True:
                raise RuntimeError('Core initialization failed')
            params = {'config': json.dumps(config), 'selected-map': {},
                      'test-url': 'https://www.gstatic.com/generate_204', 'skip-cert-verify': False}
            result = core.call('setupConfig', json.dumps(params))
            if result.get('code') != 0 or result.get('data') not in ('', None):
                raise RuntimeError('Core configuration rejected')
            result = core.call('getProxies')
            if result.get('code') != 0 or NODE not in result.get('data', {}):
                raise RuntimeError('Requested node missing')
            result = core.call('startListener')
            if result.get('code') != 0 or result.get('data') is not True:
                raise RuntimeError('Proxy listener failed')
            with socket.create_connection(('127.0.0.1', PORT), 3):
                pass
            logging.info('Started private iKuuu core pid=%s port=%s node=%s', core.proc.pid, PORT, NODE)
            failures = 0
            next_check = 0
            while not STOP.is_set():
                if core.proc.poll() is not None:
                    raise RuntimeError('Core process exited')
                if time.monotonic() >= next_check:
                    ok = internet_probe()
                    failures = 0 if ok else failures + 1
                    status(state='online' if ok else 'retrying', core_pid=core.proc.pid,
                           consecutive_failures=failures, restarts=restarts)
                    if not ok:
                        logging.warning('Connectivity check failed (%s/3); retaining fixed Japan route', failures)
                    if failures >= 3:
                        raise RuntimeError('Consecutive connectivity checks failed')
                    next_check = time.monotonic() + 30
                STOP.wait(1)
        except Exception as exc:
            restarts += 1
            logging.warning('Restarting isolated core: %s', type(exc).__name__)
            status(state='restarting', core_pid=None, restarts=restarts, error_type=type(exc).__name__)
        finally:
            if core is not None:
                core.close()
        STOP.wait(5)
    status(state='stopped', core_pid=None, restarts=restarts)
    logging.info('Proxy supervisor stopped')


if __name__ == '__main__':
    main()
