import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("setup_installer", "scripts/install.py")
launcher = load("setup_launcher", "launchers/zzb_codex.py")


class SetupTests(unittest.TestCase):
    def test_platform_selection_and_unsupported_system(self):
        expected = [("Darwin", "arm64", "aarch64-apple-darwin"),
                    ("Darwin", "x86_64", "x86_64-apple-darwin"),
                    ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
                    ("Linux", "amd64", "x86_64-unknown-linux-musl")]
        for system, arch, target in expected:
            self.assertEqual(installer.target_for(system, arch), target)
        with self.assertRaises(ValueError):
            installer.target_for("Windows", "amd64")

    def test_tampered_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive"
            archive.write_bytes(b"original")
            digest = hashlib.sha256(b"original").hexdigest()
            installer.verify_archive(archive, digest)
            archive.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                installer.verify_archive(archive, digest)

    def test_parent_identity_and_proxy_are_isolated(self):
        env = launcher.isolated_environment({"CODEX_HOME": "/parent", "CODEX_FOO": "x",
                    "OPENAI_API_KEY": "parent", "CHATGPT_TEST": "parent", "_CODEX_TEST": "parent",
                    "HTTP_PROXY": "http://old", "PATH": "/usr/bin", "EDITOR": "vim"}, Path("/isolated"), "http://localhost:17890")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_FOO", env)
        self.assertNotIn("CHATGPT_TEST", env)
        self.assertNotIn("_CODEX_TEST", env)
        self.assertEqual(env["CODEX_HOME"], "/isolated")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/isolated/xdg/runtime")
        self.assertEqual(env["TMPDIR"], "/isolated/tmp/")
        self.assertEqual(env["HTTP_PROXY"], env["https_proxy"])
        self.assertEqual(env["HTTP_PROXY"], "http://localhost:17890")
        self.assertEqual(env["EDITOR"], "vim")

    def test_protected_overrides_and_normal_resume(self):
        protected = launcher.protected_settings(Path("/test-state"))
        for args in (["-c", "sqlite_home='/elsewhere'"], ["--config=cli_auth_credentials_store='keyring'"],
                     ["--enable=system_proxy_fallback"], ["--enable", "respect_system_proxy,other"],
                     ["-cfeatures={respect_system_proxy=true}"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                launcher.validate_args(args, protected)
        launcher.validate_args(["resume", "id", "-C", "/project with spaces"], protected)

    def test_unavailable_proxy_and_wrong_route_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(launcher.socket, "create_connection", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    launcher.require_proxy({"url": "http://127.0.0.1:17890"}, state)
            (state / "proxy.json").write_text(json.dumps({"secret": "test-controller"}))
            proxy = {"url": "http://127.0.0.1:17890", "controller_url": "http://127.0.0.1:17891",
                     "config_file": "proxy.json", "expected_node": "chosen"}
            with mock.patch.object(launcher.urllib.request, "build_opener"), mock.patch.object(launcher.json, "load", return_value={"rules": [{"type": "Match", "proxy": "DIRECT"}]}), mock.patch.object(launcher.socket, "create_connection") as connect:
                with self.assertRaisesRegex(ValueError, "single MATCH"):
                    launcher.require_proxy(proxy, state)
                connect.assert_not_called()

    @unittest.skipUnless(shutil.which("tmux"), "tmux required for installation")
    def test_install_in_paths_with_spaces_and_safe_repeat_update(self):
        with tempfile.TemporaryDirectory(prefix="zzb setup ") as tmp:
            prefix, state = Path(tmp) / "apps here", Path(tmp) / "state here"
            command = [sys.executable, str(ROOT / "scripts/install.py"), "--skip-runtime", "--prefix", str(prefix),
                       "--state-dir", str(state), "--proxy-url", "http://127.0.0.1:17890"]
            env = {k:v for k,v in os.environ.items() if not k.startswith("ZZB_")}
            first = subprocess.run(command, capture_output=True, text=True, env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            config = tomllib.loads((state / "config.toml").read_text())
            self.assertIn("context-used", config["tui"]["status_line"])
            self.assertEqual(config["model"], "gpt-6-astra")
            self.assertNotIn("controller_url", json.loads((state / "local-settings.json").read_text())["proxy"])
            for path in [prefix / "bin/zzb-codex", prefix / "bin/zzb-codex-ui",
                         prefix / "share/zzb-codex-setup/dsh-bridge/dsh_mcp.py", state / "skills/dsh-delegate/SKILL.md"]:
                self.assertTrue(path.is_file(), path)
            self.assertEqual(stat.S_IMODE((state / "config.toml").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            ui = prefix / "bin/zzb-codex-ui"
            self.assertEqual({p.name for p in (prefix / "share/zzb-codex-ui").iterdir()},
                             {"app.py", "workspace.py", "README.md"})
            help_result = subprocess.run([sys.executable, str(ui), "--help"], capture_output=True, text=True, env=env)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            listing = subprocess.run([sys.executable, str(ui), "--list-json", tmp], capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(listing.stdout)["count"], 0)
            content = (state / "config.toml").read_text() + '\n# user customization\n'
            (state / "config.toml").write_text(content)
            self.assertNotEqual(subprocess.run(command, capture_output=True, env=env).returncode, 0)
            updated = subprocess.run(command + ["--update"], capture_output=True, text=True, env=env)
            self.assertEqual(updated.returncode, 0, updated.stderr)
            self.assertEqual((state / "config.toml").read_text(), content)


if __name__ == "__main__":
    unittest.main()
