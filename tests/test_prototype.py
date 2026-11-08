# SPDX-License-Identifier: Apache-2.0
"""Controls: actual isolation and transport. Checker controls live in test_checks.py."""

import asyncio
import json
import os
import platform
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from veqtor_workspace.runner import Workspace


@pytest.fixture
def workspace(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("OS sandbox tests require macOS")
    return Workspace(tmp_path / "pilot с пробелом", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Ilya")


def test_python_and_files_persist_but_variables_do_not(workspace):
    result = workspace.run("from pathlib import Path; import lxml.etree; x=42; Path('work/result.txt').write_text(str(x)); print('done')")
    assert result["exit_code"] == 0, result
    assert workspace.run("from pathlib import Path; print(Path('work/result.txt').read_text()); print('x' in globals())")["stdout"] == "42\nFalse\n"


def test_real_os_restrictions_not_only_path_validation(workspace, tmp_path):
    source = workspace.root / "inputs/source.txt"
    source.write_text("original")
    secret = tmp_path / "outside.txt"
    secret.write_text("not accessible")
    (workspace.root / "work/escape").symlink_to(secret)
    child_code = f"from pathlib import Path; Path({str(secret)!r}).read_text()"
    code = f'''
from pathlib import Path
import socket, subprocess, sys
for name, action in [
    ("input_write", lambda: Path("inputs/source.txt").write_text("changed")),
    ("outside_read", lambda: Path({str(secret)!r}).read_text()),
    ("symlink_read", lambda: Path("work/escape").read_text()),
    ("runner_log_write", lambda: Path(".runs/injected.py").write_text("changed")),
    ("outside_write", lambda: Path({str(secret)!r}).write_text("changed")),
    ("network", lambda: socket.create_connection(("1.1.1.1", 443), timeout=1)),
]:
    try: action(); print(name, "UNEXPECTED_SUCCESS")
    except OSError: print(name, "blocked")
p=subprocess.run([sys.executable,"-c",{child_code!r}],capture_output=True)
print("child", "blocked" if p.returncode else "UNEXPECTED_SUCCESS")
'''
    result = workspace.run(code)
    assert result["exit_code"] == 0, result
    assert "UNEXPECTED_SUCCESS" not in result["stdout"], result
    assert result["stdout"].count("blocked") == 7, result
    assert source.read_text() == "original"
    assert secret.read_text() == "not accessible"


def test_timeout_and_bounded_output(workspace):
    result = workspace.run("import time; time.sleep(10)", 1)
    assert result["timed_out"] is True
    result = workspace.run("print('a'*20000)")
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 12000
    assert (workspace.root / result["stdout_path"]).stat().st_size > 12000


def test_outputs_skip_hidden_and_word_owner_files(workspace):
    (workspace.root / "work/result.docx").write_bytes(b"x")
    (workspace.root / "work/~$result.docx").write_bytes(b"owner")
    (workspace.root / "work/.hidden").write_bytes(b"x")
    assert workspace.outputs() == ["work/result.docx"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_stdio_end_to_end(workspace, mode):
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "veqtor_workspace.server", "--workspace", str(workspace.root), "--python", str(workspace.python), "--author", "Ilya"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        async with Client(stdio_client(params), mode=mode) as client:
            catalog = await client.list_tools()
            tools = catalog.tools if hasattr(catalog, "tools") else catalog
            assert {t.name for t in tools} == {"workspace_info", "run_python", "check_docx"}
            result = await client.call_tool("run_python", {"code": "print('through MCP')"})
            data = result.structured_content
            if not isinstance(data, dict):
                data = json.loads(result.content[0].text)
            if "result" in data:
                data = data["result"]
            assert data["stdout"] == "through MCP\n", data
    asyncio.run(exercise())
