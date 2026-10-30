#!/usr/bin/env python3
"""Read-only checks for an installed zzb-codex workspace."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default=os.environ.get("ZZB_CODEX_PREFIX", "~/local"))
    parser.add_argument("--state-dir", default=os.environ.get("ZZB_CODEX_HOME", "~/.zzb-codex"))
    parser.add_argument("--check-proxy", action="store_true")
    args = parser.parse_args()
    prefix, state = Path(args.prefix).expanduser(), Path(args.state_dir).expanduser()
    checks = {
        "Python 3.12+": sys.version_info >= (3, 12),
        "tmux": bool(shutil.which(os.path.expanduser(os.environ.get("ZZB_TMUX") or "tmux"))),
        "UI": (prefix / "share/zzb-codex-ui/app.py").is_file(),
        "native runtime": os.access(prefix / "share/zzb-codex/current/bin/codex", os.X_OK),
        "configuration": (state / "config.toml").is_file(),
        "local proxy settings": (state / "local-settings.json").is_file(),
        "dsh skill": (state / "skills/dsh-delegate/SKILL.md").is_file(),
        "dsh bridge": (prefix / "share/zzb-codex-setup/dsh-bridge/dsh_mcp.py").is_file(),
    }
    for label, ok in checks.items():
        print(("OK      " if ok else "MISSING ") + label)
    for command in ("dsh", "node"):
        print(("OK      " if shutil.which(command) else "OPTIONAL ") + command)
    graphical = sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    clipboard = (shutil.which("pbcopy") if sys.platform == "darwin" else
                 (os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy")) or
                 (os.environ.get("DISPLAY") and (shutil.which("xclip") or shutil.which("xsel"))))
    print(("OK      " if clipboard else "OPTIONAL ") + ("clipboard" if graphical else "clipboard (headless terminal; navigation/chat still work)"))
    if args.check_proxy:
        try:
            module_path = prefix / "share/zzb-codex-setup/launchers/zzb_codex.py"
            spec = importlib.util.spec_from_file_location("installed_zzb_launcher", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.require_proxy(json.loads((state / "local-settings.json").read_text())["proxy"], state)
            print("OK      proxy endpoint / configured route")
        except Exception as exc:
            print("MISSING proxy endpoint / configured route (" + type(exc).__name__ + ")")
            checks["proxy"] = False
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
