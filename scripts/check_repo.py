#!/usr/bin/env python3
"""Check the tracked snapshot for accidental machine state and common secrets."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
forbidden_names = {"auth.json", "auth.lock", "history.jsonl", "installation_id", "models_cache.json",
                   "local-settings.json", "subscription.private", "session_index.jsonl"}
forbidden_parts = {"sessions", "shell_snapshots", "state", "cache", "logs", "__pycache__", "private"}
problems = []
for name in filter(None, tracked):
    p = Path(name)
    if p.name in forbidden_names or forbidden_parts.intersection(p.parts) or p.suffix in {".sqlite", ".db", ".pem", ".key", ".pyc"}:
        problems.append(name + ": runtime/private file must not be tracked")
        continue
    data = (root / p).read_bytes()
    if len(data) > 2_000_000:
        problems.append(name + ": unexpected large file")
    text = data.decode("utf-8", errors="replace")
    if re.search(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----)", text):
        problems.append(name + ": possible secret; inspect locally")
    if re.search(r"/Users/zhangzhognbo(?:/|[\"'])", text):
        problems.append(name + ": machine-specific home path")
if problems:
    print("\n".join(problems), file=sys.stderr)
    raise SystemExit(1)
print("Tracked-file check passed (no runtime state, known secret patterns or original home paths).")
