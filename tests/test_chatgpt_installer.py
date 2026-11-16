# SPDX-License-Identifier: Apache-2.0
import importlib.util
import os
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_chatgpt", ROOT / "packaging/chatgpt/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


@pytest.fixture
def runtime(tmp_path):
    result = tmp_path / "runtime with spaces"
    (result / ".venv/bin").mkdir(parents=True)
    (result / ".venv/bin/python3").touch()
    (result / "veqtor-launch.py").touch()
    return result


def test_first_install_and_quoted_unicode_author(tmp_path, runtime):
    path = tmp_path / "profile/config.toml"
    name = 'Илья "Review" Shilov'
    result = configure.configure(path, runtime, name, configure.digest(b""))
    data = tomllib.loads(path.read_text())
    server = data["mcp_servers"]["veqtor_workspace"]
    assert server["args"][-1] == name
    assert server["command"] == str(runtime / ".venv/bin/python3")
    assert server["tool_timeout_sec"] == 330
    assert server["startup_timeout_sec"] == 180
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["backup"] is None


def test_preserves_models_comments_other_servers_and_policy(tmp_path, runtime):
    path = tmp_path / "config.toml"
    original = b'''# My existing preferences\nmodel = "my-model"\n[projects."/some/project"]\ntrust_level = "trusted"\n[mcp_servers.mail]\ncommand = "/some/mail" # retained\n[mcp_servers.mail.env]\nEXAMPLE_TOKEN = "not-a-real-secret"\n[mcp_servers.veqtor_workspace_pilot]\ncommand = "uv"\nargs = ["run", "veqtor-workspace", "--author", "Old Author"]\nenabled = false\ndefault_tools_approval_mode = "prompt"\n'''
    path.write_bytes(original)
    result = configure.configure(path, runtime, "New Author", configure.digest(original))
    data = tomllib.loads(path.read_text())
    before = tomllib.loads(original.decode())
    assert data["model"] == before["model"]
    assert data["projects"] == before["projects"]
    assert data["mcp_servers"]["mail"] == before["mcp_servers"]["mail"]
    server = data["mcp_servers"]["veqtor_workspace_pilot"]
    assert server["args"][-1] == "New Author"
    assert server["enabled"] is False
    assert server["default_tools_approval_mode"] == "prompt"
    assert "# My existing preferences" in path.read_text()
    assert "# retained" in path.read_text()
    assert Path(result["backup"]).read_bytes() == original
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert not result["enabled"]


def test_reinstall_updates_one_connection_and_keeps_previous_backup(tmp_path, runtime):
    path = tmp_path / "config.toml"
    configure.configure(path, runtime, "First Author", configure.digest(b""))
    first = path.read_bytes()
    result = configure.configure(path, runtime, "Second Author", configure.digest(first))
    assert list(tomllib.loads(path.read_text())["mcp_servers"]) == ["veqtor_workspace"]
    assert Path(result["backup"]).read_bytes() == first
    assert result["updated"]


@pytest.mark.parametrize("content", [
    '[mcp_servers.veqtor_workspace]\ncommand = "other-program"\n',
    '[mcp_servers.veqtor_workspace]\nurl = "https://example.com"\n',
    '[mcp_servers.veqtor_workspace]\ncommand = "veqtor-workspace"\nenvironment_id = "remote"\n',
    '[mcp_servers.veqtor_workspace]\ncommand = "veqtor-workspace"\n[mcp_servers.veqtor_workspace_pilot]\ncommand = "veqtor-workspace"\n',
    'not valid = [ TOML',
    'mcp_servers = "unexpected"\n',
    '[mcp_servers.veqtor_workspace]\ncommand = "veqtor-workspace"\nargs = 3\n',
])
def test_refuses_ambiguous_foreign_or_invalid_config_without_mutation(tmp_path, runtime, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        configure.configure(path, runtime, "Author", configure.digest(content.encode()))
    assert path.read_text() == content
    assert list(tmp_path.glob("config.toml.before-*")) == []


def test_concurrent_update_is_preserved(tmp_path, runtime):
    path = tmp_path / "config.toml"
    original = b'model = "old"\n'
    latest = b'model = "changed-while-downloading"\n'
    path.write_bytes(latest)
    with pytest.raises(ValueError, match="changed"):
        configure.configure(path, runtime, "Author", configure.digest(original))
    assert path.read_bytes() == latest


def test_symlink_is_not_followed(tmp_path, runtime):
    other = tmp_path / "original.toml"
    other.write_text('model = "keep"\n')
    path = tmp_path / "config.toml"
    path.symlink_to(other)
    with pytest.raises(ValueError, match="symbolic link"):
        configure.configure(path, runtime, "Author", configure.digest(other.read_bytes()))
    assert other.read_text() == 'model = "keep"\n'


def test_replace_failure_leaves_original_settings(tmp_path, runtime, monkeypatch):
    path = tmp_path / "config.toml"
    original = b'model = "unchanged"\n'
    path.write_bytes(original)
    def fail(*_args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        configure.configure(path, runtime, "Author", configure.digest(original))
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".veqtor-config-*")) == []


@pytest.mark.parametrize("author", ["", "  ", "x\ncommand", "x" * 121])
def test_invalid_author_does_not_touch_settings(tmp_path, runtime, author):
    path = tmp_path / "config.toml"
    with pytest.raises(ValueError):
        configure.configure(path, runtime, author, configure.digest(b""))
    assert not path.exists()
