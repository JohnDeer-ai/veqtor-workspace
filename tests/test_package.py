# SPDX-License-Identifier: Apache-2.0
"""The extension package carries the server, its lock file and the launch command Claude Desktop uses."""

import importlib.util
import json
import shutil
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "packaging/build_mcpb.py")
build_mcpb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_mcpb)


def test_package_contents_and_launch_command(tmp_path):
    target = build_mcpb.build(tmp_path, author="Test Author")
    assert target.name == "veqtor-workspace-0.1.1-macos-arm64.mcpb"
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "pyproject.toml", "uv.lock", "veqtor_workspace/server.py",
            "veqtor_workspace/checks.py", "veqtor_workspace/runner.py", "veqtor_workspace/RECIPE.md",
            "veqtor_workspace/files.py", "veqtor_workspace/tasks.py", "veqtor_workspace/operations.py", "veqtor_workspace/output.py",
            "veqtor_workspace/REFERENCE.md", "LICENSE", "icon.png", ".python-version"} <= names
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


def test_public_package_is_reproducible_and_machine_independent(tmp_path):
    first = build_mcpb.build(tmp_path / "first")
    second = build_mcpb.build(tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        project = tomllib.loads(archive.read("pyproject.toml").decode())["project"]
        assert manifest["version"] == project["version"] == "0.1.1"
        assert manifest["name"] == "veqtor-workspace-pilot"  # Existing extensions update in place.
        assert manifest["display_name"] == "Veqtor Workspace"
        assert manifest["compatibility"]["platforms"] == ["darwin"]
        assert archive.read(".python-version").strip() == b"3.12.13"
        assert all("default" not in field for field in manifest["user_config"].values())
        assert not any("/Users/" in value for value in manifest["server"]["mcp_config"]["args"])


def test_bundle_excludes_unrelated_files_and_symlinks(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    staged.mkdir()
    for name in ("packaging", "veqtor_workspace"):
        shutil.copytree(ROOT / name, staged / name)
    for name in ("pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"):
        shutil.copyfile(ROOT / name, staged / name)
    (staged / "veqtor_workspace/private.docx").write_bytes(b"not a public document")
    (staged / "veqtor_workspace/.env").write_text("not a public setting")
    (staged / "veqtor_workspace/linked.py").symlink_to(ROOT / "tests/conftest.py")
    monkeypatch.setattr(build_mcpb, "ROOT", staged)
    with zipfile.ZipFile(build_mcpb.build(tmp_path / "out")) as archive:
        assert not {"veqtor_workspace/private.docx", "veqtor_workspace/.env", "veqtor_workspace/linked.py"} & set(archive.namelist())
