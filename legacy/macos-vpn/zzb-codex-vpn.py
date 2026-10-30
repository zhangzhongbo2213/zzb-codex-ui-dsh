#!/usr/bin/python3
"""Manage the independent iKuuu Japan proxy for zzb-codex."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path.home() / 'local/share/zzb-codex-vpn'
PLIST = Path.home() / 'Library/LaunchAgents/com.zzb.codex-vpn.plist'
TARGET = 'gui/' + str(os.getuid()) + '/com.zzb.codex-vpn'


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


def show_status():
    result = run(['/bin/launchctl', 'print', TARGET])
    print('Service: ' + ('loaded' if result.returncode == 0 else 'stopped'))
    status_path = BASE / 'state/status.json'
    if status_path.exists():
        status = json.loads(status_path.read_text())
        print('Node: ' + status['node'])
        print('Proxy: ' + status['proxy'])
        print('Last health check: ' + status['timestamp'] + ' ' + status['state'])
        print('Core PID: ' + str(status.get('core_pid')))
    return result.returncode


def check():
    result = run(['/usr/bin/curl', '--proxy', 'http://127.0.0.1:17890', '--noproxy', '',
                  '--connect-timeout', '5', '--max-time', '15', '-sS',
                  'https://www.cloudflare.com/cdn-cgi/trace'])
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode or fields.get('loc') != 'JP':
        print('Japan proxy check failed. No direct fallback is configured.')
        return 1
    print('Japan proxy online. Exit IP: ' + fields.get('ip', 'unknown'))
    return 0


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'refresh':
        return subprocess.call(['/usr/bin/python3', str(BASE / 'refresh.py')])
    if action == 'status':
        return show_status()
    if action == 'check':
        return check()
    if action == 'start':
        loaded = run(['/bin/launchctl', 'print', TARGET]).returncode == 0
        command = ['/bin/launchctl', 'kickstart', TARGET] if loaded else [
            '/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(PLIST)]
        result = run(command)
        print('Service start requested.' if result.returncode == 0 else result.stderr)
        return result.returncode
    if action == 'restart':
        result = run(['/bin/launchctl', 'kickstart', '-k', TARGET])
        print('Service restart requested.' if result.returncode == 0 else result.stderr)
        return result.returncode
    if action == 'stop':
        result = run(['/bin/launchctl', 'bootout', TARGET])
        print('Service stopped; new zzb-codex network sessions will refuse to start.' if result.returncode == 0 else result.stderr)
        return result.returncode
    print('Usage: zzb-codex-vpn [status|check|start|stop|restart|refresh]')
    return 0 if action in ('help', '--help', '-h') else 2

if __name__ == '__main__':
    raise SystemExit(main())
