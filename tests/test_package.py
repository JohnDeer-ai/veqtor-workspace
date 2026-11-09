# SPDX-License-Identifier: Apache-2.0
"""The extension package carries the server, its lock file and the launch command Claude Desktop uses."""

import importlib.util
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "packaging/build_mcpb.py")
build_mcpb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_mcpb)


def test_package_contents_and_launch_command(tmp_path):
    target = build_mcpb.build(tmp_path, author="Test Author")
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "pyproject.toml", "uv.lock", "veqtor_workspace/server.py",
            "veqtor_workspace/checks.py", "veqtor_workspace/runner.py", "veqtor_workspace/RECIPE.md",
            "veqtor_workspace/files.py", "veqtor_workspace/tasks.py", "veqtor_workspace/operations.py", "LICENSE"} <= names
    assert not any(n.startswith(("tests/", ".venv/")) or "__pycache__" in n for n in names)
    args = manifest["server"]["mcp_config"]["args"]
    assert args[:6] == ["run", "--frozen", "--no-dev", "--directory", "${__dirname}", "veqtor-workspace"]
    assert args[6:] == ["--author", "${user_config.author}"]
    assert "workspace" not in manifest["user_config"]
    assert manifest["user_config"]["author"]["default"] == "Test Author"
    assert {t["name"] for t in manifest["tools"]} == {"workspace_info", "document_task", "run_python", "check_docx"}
    # The repository manifest itself stays free of machine-specific defaults.
    plain = json.loads((ROOT / "packaging/manifest.json").read_text())
    assert all("default" not in field for field in plain["user_config"].values())
