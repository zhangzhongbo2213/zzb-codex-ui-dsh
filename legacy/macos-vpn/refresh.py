#!/usr/bin/python3
"""Refresh only Japan S02 from the local iKuuu subscription; never print secrets."""
from pathlib import Path
import json
import os
import subprocess
import sys
from core_rpc import Core
BASE = Path(__file__).resolve().parent
NODE = '🇯🇵 日本S02 | IEPL'

def refresh():
    os.umask(0o077)
    staged = BASE / 'subscription.private'
    core = None
    try:
        result = subprocess.run(['/usr/bin/swift', str(BASE / 'read-vpn-config.swift')], capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('Cannot read the local iKuuu subscription through Keychain')
        core = Core(BASE / 'iKuuuVPNCore', BASE)
        response = core.call('getConfig', str(staged))
        data = response.get('data')
        if isinstance(data, str):
            data = json.loads(data)
        if response.get('code') != 0 or not isinstance(data, dict):
            raise RuntimeError('Subscription parser failed')
        matches = [p for p in data.get('proxies', []) if p.get('name') == NODE]
        if len(matches) != 1:
            raise RuntimeError('Exact Japan S02 node missing from local subscription')
        path = BASE / 'state/config.json'
        config = json.loads(path.read_text())
        if config['proxies'] == matches:
            print('Japan S02 configuration is current.')
            return
        config['proxies'] = matches
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        os.chmod(temp, 0o600)
        temp.replace(path)
        result = subprocess.run(['/bin/launchctl', 'kickstart', '-k', 'gui/' + str(os.getuid()) + '/com.zzb.codex-vpn'], capture_output=True)
        if result.returncode:
            raise RuntimeError('Configuration updated; start the proxy service to apply it')
        print('Japan S02 configuration refreshed and service restarted.')
    finally:
        if core is not None:
            core.close()
        if staged.exists():
            staged.unlink()

if __name__ == '__main__':
    try:
        refresh()
    except Exception as exc:
        print('Refresh failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
