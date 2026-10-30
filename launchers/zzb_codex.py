#!/usr/bin/env python3
"""Launch Codex with independent state and explicitly configured proxy routing."""
import json
import os
from pathlib import Path
import socket
import sys
import urllib.request
from urllib.parse import urlparse


def paths():
    prefix = Path(os.environ.get("ZZB_CODEX_PREFIX", "~/local")).expanduser()
    state = Path(os.environ.get("ZZB_CODEX_HOME", "~/.zzb-codex")).expanduser()
    binary = Path(os.environ.get("ZZB_CODEX_BINARY", str(prefix / "share/zzb-codex/current/bin/codex"))).expanduser()
    return prefix, state, binary


def protected_settings(state):
    return {"cli_auth_credentials_store": "file", "mcp_oauth_credentials_store": "file",
            "sqlite_home": str(state / "db"), "log_dir": str(state / "log"),
            "features.respect_system_proxy": False, "features.system_proxy_fallback": False}


def isolated_environment(source, state, proxy_url):
    env = {k: v for k, v in source.items()
           if not k.startswith(("CODEX_", "OPENAI_", "CHATGPT_", "_CODEX_"))}
    # Keep the native TUI colourful even when launched by a NO_COLOR tool shell.
    env.pop("NO_COLOR", None)
    env.update(CODEX_HOME=str(state), CODEX_SQLITE_HOME=str(state / "db"), TMPDIR=str(state / "tmp") + "/")
    for suffix, name in (("CONFIG", "config"), ("DATA", "data"), ("STATE", "state"),
                         ("CACHE", "cache"), ("RUNTIME", "runtime")):
        key = "XDG_RUNTIME_DIR" if suffix == "RUNTIME" else "XDG_" + suffix + "_HOME"
        env[key] = str(state / "xdg" / name)
    excluded = (str(Path.home() / ".codex"), str(Path.home() / ".cache/codex-runtimes"),
                "/Applications/ChatGPT.app/Contents/Resources", "/Applications/Codex.app/Contents/Resources")
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                 if p and not any(p == x or p.startswith(x + "/") for x in excluded))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = proxy_url
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


def require_proxy(proxy, state):
    url = urlparse(proxy["url"])
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("proxy URL must use http:// or https://")
    if proxy.get("controller_url"):
        config_path = Path(proxy.get("config_file", "proxy/config.json")).expanduser()
        if not config_path.is_absolute():
            config_path = state / config_path
        config = json.loads(config_path.read_text())
        secret = config["secret"]
        if not secret or secret.startswith("REPLACE_"):
            raise ValueError("proxy controller secret is not configured")
        request = urllib.request.Request(proxy["controller_url"].rstrip("/") + "/rules",
                                         headers={"Authorization": "Bearer " + secret})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            rules = json.load(response)["rules"]
        if len(rules) != 1 or rules[0]["type"] != "Match" or rules[0]["proxy"] != proxy["expected_node"]:
            raise ValueError("proxy is not using the configured single MATCH route")
    with socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout=3):
        pass


def validate_args(args, protected):
    for index, arg in enumerate(args):
        if arg == "--":
            break
        value = None
        if arg in ("-c", "--config") and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and len(arg) > 2:
            value = arg[2:]
        if value is not None:
            key = value.split("=", 1)[0].strip().strip("\"'")
            if key in protected or key == "features":
                raise ValueError("storage and proxy isolation settings are fixed by this launcher")
        if arg == "--enable" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--enable="):
            value = arg.split("=", 1)[1]
        else:
            continue
        if {"respect_system_proxy", "system_proxy_fallback"} & set(value.split(",")):
            raise ValueError("system proxy fallback is disabled for this installation")


def main():
    os.umask(0o077)
    _, state, binary = paths()
    if state.is_symlink() or not state.is_dir():
        raise SystemExit("zzb-codex: run scripts/install.py to create an independent state directory")
    if not binary.is_file():
        raise SystemExit("zzb-codex: full runtime package is missing; rerun scripts/install.py")
    args = sys.argv[1:]
    protected = protected_settings(state)
    try:
        validate_args(args, protected)
        settings = json.loads((state / "local-settings.json").read_text())
        proxy = settings["proxy"]
        offline = (args in (["--version"], ["-V"], ["login", "status"])
                   or "--help" in args or "-h" in args or (args and args[0] == "mcp"))
        if not offline:
            try:
                require_proxy(proxy, state)
            except Exception:
                raise ValueError("proxy unavailable or route check failed; configure local-settings.json and your local proxy") from None
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit("zzb-codex: " + str(exc)) from None
    fixed = ["--no-daemon"]
    for key, value in protected.items():
        fixed.extend(["-c", key + "=" + json.dumps(value)])
    os.execve(str(binary), [str(binary), *fixed, *args], isolated_environment(os.environ, state, proxy["url"]))


if __name__ == "__main__":
    main()
