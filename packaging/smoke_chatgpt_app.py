# SPDX-License-Identifier: Apache-2.0
"""Exercise the native installer engine and installed MCP server in a fresh profile.

The real ChatGPT configuration and real documents are never opened or changed.
This verifies the installation engine, not ChatGPT's native UI or model routing.
"""
import argparse
import asyncio
import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

from mcp import StdioServerParameters
from smoke_mcpb import exercise_documents


def verify(app: Path, driver: Path):
    resources = app / "Contents/Resources"
    version = json.loads((resources / "release.json").read_text())["version"]
    with tempfile.TemporaryDirectory(prefix="veqtor-chatgpt-test-") as temporary:
        root = Path(temporary).resolve()
        profile = root / "profile with spaces"
        subprocess.run([str(driver), str(resources), str(profile)], check=True, timeout=720)
        config = tomllib.loads((profile / ".codex/config.toml").read_text())
        entry = config["mcp_servers"]["veqtor_workspace"]
        runtime = Path(entry["cwd"])
        assert runtime.is_relative_to(profile)
        assert entry["command"] == str(runtime / ".venv/bin/python3")
        assert entry["args"] == ["-I", str(runtime / "veqtor-launch.py"), "--author", "Installer Test"]
        assert entry["startup_timeout_sec"] == 180 and entry["tool_timeout_sec"] == 330
        managed = profile / "Library/Application Support/Veqtor Workspace/ChatGPT/python"
        assert any(managed.glob("cpython-*")), "The installer did not prepare its own Python"
        params = StdioServerParameters(command=entry["command"],
            args=entry["args"] + ["--state-dir", str(root / "private state")], cwd=entry["cwd"],
            env={"HOME": str(profile), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        asyncio.run(exercise_documents(params, root, version, "Installer Test"))
        print(json.dumps({"version": version, "native_installer_engine": True, "fresh_runtime": True,
            "tools": 4, "tracked_change": True, "comment": True, "save_as": True,
            "in_place_save": True, "source_preserved": True, "saved_conflict_refused": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("driver", type=Path)
    args = parser.parse_args()
    verify(args.app.resolve(), args.driver.resolve())
