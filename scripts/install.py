#!/usr/bin/env python3
"""Install a reproducible, per-user zzb-codex workspace (no sudo or shell edits)."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def target_for(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "amd64": "x86_64", "x86_64": "x86_64"}.get(machine)
    suffix = {"Darwin": "apple-darwin", "Linux": "unknown-linux-musl"}.get(system)
    if not arch or not suffix:
        raise ValueError(f"unsupported platform: {system} {machine}")
    return arch + "-" + suffix


def verify_archive(archive, expected):
    with open(archive, "rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("runtime SHA-256 mismatch; archive was not extracted")


def validate_package(directory, target, version):
    metadata = json.loads((directory / "codex-package.json").read_text())
    if metadata.get("target") != target or metadata.get("version") != version:
        raise ValueError("runtime package platform/version does not match the lock file")
    for name in ("bin/codex", "bin/codex-code-mode-host", "bin/codex-exec-server", "codex-path/rg"):
        path = directory / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("incomplete runtime package: " + name)
    if not (directory / "codex-resources").is_dir():
        raise ValueError("runtime resources directory is missing")


def install_runtime(prefix, lock, target, archive=None):
    dest = prefix / "share/zzb-codex/versions" / (lock["version"] + "-" + target)
    if dest.exists():
        validate_package(dest, target, lock["version"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".zzb-runtime-", dir=dest.parent) as temporary:
            staging = Path(temporary)
            package = Path(archive).expanduser().resolve() if archive else staging / "package.tar.gz"
            asset = lock["targets"][target]
            if not archive:
                print("Downloading pinned Codex " + lock["version"] + " for " + target, flush=True)
                request = urllib.request.Request(asset["url"], headers={"User-Agent": "zzb-codex-setup/1.0"})
                with urllib.request.urlopen(request, timeout=60) as response, package.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
            verify_archive(package, asset["sha256"])
            unpacked = staging / "unpacked"
            unpacked.mkdir()
            with tarfile.open(package, "r:gz") as source:
                source.extractall(unpacked, filter="data")
            validate_package(unpacked, target, lock["version"])
            unpacked.rename(dest)
    current = prefix / "share/zzb-codex/current"
    if current.exists() and not current.is_symlink():
        raise ValueError("refusing to replace a non-symlink runtime current directory")
    replacement = current.with_name(".current-" + str(os.getpid()))
    replacement.symlink_to(os.path.relpath(dest, current.parent))
    replacement.replace(current)


def wrapper(script, prefix, state, launcher=None):
    lines = ["#!/usr/bin/env python3", "import os, sys",
             f"os.environ.setdefault('ZZB_CODEX_PREFIX', {str(prefix)!r})",
             f"os.environ.setdefault('ZZB_CODEX_HOME', {str(state)!r})"]
    if launcher:
        lines.append(f"os.environ.setdefault('ZZB_CODEX_LAUNCHER', {str(launcher)!r})")
    lines.append(f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(script)!r}, *sys.argv[1:]])")
    return "\n".join(lines) + "\n"


def install(args):
    prefix, state = Path(args.prefix).expanduser().absolute(), Path(args.state_dir).expanduser().absolute()
    if state.is_symlink():
        raise ValueError("state directory must not be a symlink")
    if prefix == state or prefix in state.parents or state in prefix.parents:
        raise ValueError("installation prefix and state directory must be separate")
    lock = json.loads((ROOT / "config/runtime-lock.json").read_text())
    target = target_for()
    if target not in lock["targets"]:
        raise ValueError("no pinned package for " + target)
    if args.plan:
        print(json.dumps({"prefix": str(prefix), "state_dir": str(state), "target": target,
                          "version": lock["version"], "runtime": not args.skip_runtime,
                          "tmux_available": bool(shutil.which("tmux")), "dsh_enabled": args.with_dsh}, indent=2))
        return
    if not shutil.which("tmux"):
        raise ValueError("tmux is required: macOS brew install tmux; Debian/Ubuntu sudo apt install tmux")
    if not args.update and any(p.exists() for p in (prefix / "bin/zzb-codex", prefix / "bin/zzb-codex-ui", state / "config.toml")):
        raise ValueError("installation already exists; --update updates code and preserves existing configuration")
    dsh = shutil.which("dsh")
    mac_dsh = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    if not dsh and platform.system() == "Darwin" and mac_dsh.is_file():
        dsh = str(mac_dsh)
    if args.with_dsh and (not dsh or not shutil.which("node")):
        raise ValueError("--with-dsh requires an installed dsh CLI and node on PATH")
    if args.with_dsh and (state / "config.toml").exists():
        import tomllib
        existing = tomllib.loads((state / "config.toml").read_text())
        if "dsh" not in existing.get("mcp_servers", {}):
            raise ValueError("existing config is preserved; add dsh manually as documented, or use a fresh state directory")
    os.umask(0o077)
    prefix.mkdir(parents=True, exist_ok=True)
    for rel in ("", "db", "log", "tmp", "proxy", "xdg/config", "xdg/data", "xdg/state", "xdg/cache", "xdg/runtime"):
        path = state / rel
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.skip_runtime:
        install_runtime(prefix, lock, target, args.archive)
    backup = prefix / "share/zzb-codex-backups" / time.strftime("%Y%m%d-%H%M%S")

    def write(path, content, executable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("refusing to overwrite a symlink: " + str(path))
        if path.exists() and path.read_bytes() != content:
            relative = path.relative_to(prefix) if path.is_relative_to(prefix) else Path("state") / path.relative_to(state)
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        path.write_bytes(content)
        path.chmod(0o700 if executable else 0o600)

    sources = (
        ("ui", prefix / "share/zzb-codex-ui", ("app.py", "workspace.py", "preview.py", "README.md")),
        ("launchers", prefix / "share/zzb-codex-setup/launchers", ("zzb_codex.py",)),
        ("vendor/dsh-bridge", prefix / "share/zzb-codex-setup/dsh-bridge", ("dsh_mcp.py", "dsh_runner.py", "SOURCE.md")),
        ("skills/dsh-delegate", state / "skills/dsh-delegate", ("SKILL.md", "agents/openai.yaml", "references/task-templates.md")),
    )
    for folder, destination, names in sources:
        for name in names:
            write(destination / name, (ROOT / folder / name).read_bytes())
    config = state / "config.toml"
    if not config.exists():
        content = (ROOT / "config/base.toml").read_text()
        if args.with_dsh:
            path = str(Path(dsh).parent) + os.pathsep + os.environ.get("PATH", "")
            content += ("\n[mcp_servers.dsh]\ncommand = " + json.dumps(sys.executable)
                        + "\nargs = [" + json.dumps(str(prefix / "share/zzb-codex-setup/dsh-bridge/dsh_mcp.py"))
                        + "]\nstartup_timeout_sec = 20\ntool_timeout_sec = 660\n[mcp_servers.dsh.env]\nPATH = "
                        + json.dumps(path) + "\n")
        write(config, content.encode())
    local = state / "local-settings.json"
    if not local.exists():
        proxy = {"url": args.proxy_url or "http://127.0.0.1:17890"}
        if not args.proxy_url or args.proxy_config:
            previous_proxy = prefix / "share/zzb-codex-vpn/state/config.json"
            config_file = (str(Path(args.proxy_config).expanduser().absolute()) if args.proxy_config
                           else str(previous_proxy) if previous_proxy.is_file() else "proxy/config.json")
            proxy.update(controller_url="http://127.0.0.1:17891", expected_node="🇯🇵 日本S02 | IEPL",
                         config_file=config_file)
        write(local, (json.dumps({"proxy": proxy}, ensure_ascii=False, indent=2) + "\n").encode())
    write(prefix / "bin/zzb-codex", wrapper(prefix / "share/zzb-codex-setup/launchers/zzb_codex.py", prefix, state).encode(), True)
    write(prefix / "bin/zzb-codex-ui", wrapper(prefix / "share/zzb-codex-ui/app.py", prefix, state, prefix / "bin/zzb-codex").encode(), True)
    print("Installed into " + str(prefix))
    print("State: " + str(state) + " (existing configuration preserved)")
    print("Add " + str(prefix / "bin") + " to PATH, configure your local proxy, then run zzb-codex login --device-auth.")


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12+ is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="~/local")
    parser.add_argument("--state-dir", default="~/.zzb-codex")
    parser.add_argument("--archive", help="use a local pinned runtime archive instead of downloading")
    parser.add_argument("--skip-runtime", action="store_true", help="install configuration/UI only")
    parser.add_argument("--update", action="store_true", help="update installed code, back up replaced files, preserve configuration")
    parser.add_argument("--with-dsh", action="store_true")
    parser.add_argument("--proxy-url", help="use an existing HTTP proxy; without --proxy-config only endpoint availability is checked")
    parser.add_argument("--proxy-config", help="private Clash/Mihomo JSON configuration for strict single-node route verification")
    parser.add_argument("--plan", action="store_true", help="print installation paths without writing")
    try:
        install(parser.parse_args())
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        raise SystemExit("Install failed: " + str(exc)) from None


if __name__ == "__main__":
    main()
