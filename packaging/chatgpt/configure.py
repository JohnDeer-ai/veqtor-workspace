# SPDX-License-Identifier: Apache-2.0
"""Change only the Workspace connection, preserving the rest of the user's TOML.

Called by the native installer after its isolated runtime has passed an MCP
handshake. It never opens documents or changes approval/sandbox settings.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

import tomlkit

SERVER_NAMES = ("veqtor_workspace", "veqtor_workspace_pilot", "veqtor-workspace")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_config(path: Path) -> bytes:
    for item in (path.parent, path):
        if item.is_symlink():
            raise ValueError("Your ChatGPT settings use a symbolic link. Use the manual setup instead.")
        if item.exists() and item.stat().st_uid != os.getuid():
            raise ValueError("Your ChatGPT settings belong to another user.")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Your ChatGPT settings are not a regular file.")
    return path.read_bytes() if path.exists() else b""


def is_workspace(entry: dict) -> bool:
    command = str(entry.get("command", ""))
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return False
    return (
        "veqtor_workspace.server" in args
        or "veqtor-workspace" in args
        or "veqtor-workspace" in Path(command).name
        or any(str(arg).endswith("/veqtor-launch.py") for arg in args)
    )


def connection(runtime: Path, author: str) -> dict:
    author = author.strip()
    if not author or len(author) > 120 or any(ord(c) < 32 for c in author):
        raise ValueError("Enter a name of 1–120 characters without line breaks.")
    runtime = runtime.resolve(strict=True)
    python = runtime / ".venv/bin/python3"
    launcher = runtime / "veqtor-launch.py"
    if not python.is_file() or not launcher.is_file():
        raise ValueError("The prepared Workspace runtime is incomplete.")
    return {
        "command": str(python),
        "args": ["-I", str(launcher), "--author", author],
        "cwd": str(runtime),
        "startup_timeout_sec": 180,
        "tool_timeout_sec": 330,
    }


def configure(config_path: Path, runtime: Path, author: str, expected_digest: str) -> dict:
    desired = connection(runtime, author)
    # Read before creating anything, so custom locations fail without changes.
    original = read_config(config_path)
    if digest(original) != expected_digest:
        raise ValueError("ChatGPT settings changed during installation. Click Connect again to retry.")
    try:
        document = tomlkit.parse(original.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("ChatGPT settings could not be read. They have not been changed.") from exc
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("The MCP settings section has an unsupported format.")
    candidates = [key for key in SERVER_NAMES if key in servers]
    if len(candidates) > 1:
        raise ValueError("There is more than one Workspace connection. Choose which one to keep in ChatGPT first.")
    key = candidates[0] if candidates else SERVER_NAMES[0]
    previous = servers.get(key)
    if previous is not None and (not isinstance(previous, dict) or not is_workspace(previous)):
        raise ValueError("A different tool already uses the Workspace connection name. It has not been changed.")
    if previous is not None and ("url" in previous or "environment_id" in previous and previous["environment_id"] != "local"):
        raise ValueError("The existing Workspace connection is remote. It has not been changed.")
    if "mcp_servers" not in document:
        document.add("mcp_servers", tomlkit.table())
    if key not in document["mcp_servers"]:
        document["mcp_servers"].add(key, tomlkit.table())
    for name, value in desired.items():
        document["mcp_servers"][key][name] = value
    # Preserve the user's enabled/disabled state and tool approval preferences.
    result = tomlkit.dumps(document).encode("utf-8")
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = config_path.parent / ".veqtor-install.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if read_config(config_path) != original:
            raise ValueError("ChatGPT settings changed. Click Connect again to retry.")
        existed = config_path.exists()
        backup = None
        if existed:
            backup = config_path.parent / ("config.toml.before-veqtor-" + uuid.uuid4().hex)
            with os.fdopen(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
                out.write(original)
                out.flush()
                os.fsync(out.fileno())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".veqtor-config-", dir=config_path.parent, delete=False) as out:
                temporary = Path(out.name)
                out.write(result)
                out.flush()
                os.fsync(out.fileno())
            if read_config(config_path) != original or config_path.exists() != existed:
                raise ValueError("ChatGPT settings changed. Click Connect again to retry.")
            if existed:
                os.replace(temporary, config_path)
            else:
                # Do not replace a settings file created by the app meanwhile.
                os.link(temporary, config_path)
            return {"server": key, "backup": str(backup) if backup else None,
                    "updated": previous is not None, "enabled": document["mcp_servers"][key].get("enabled", True)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--expected-digest", required=True)
    args = parser.parse_args()
    try:
        value = configure(args.config, args.runtime, args.author, args.expected_digest)
    except (ValueError, OSError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
