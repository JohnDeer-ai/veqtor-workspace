# SPDX-License-Identifier: Apache-2.0
"""Expected recovery instructions survive MCP 2.2; unexpected failures stay masked."""

import asyncio
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from veqtor_workspace.operations import Operations
from veqtor_workspace.runner import Workspace
from veqtor_workspace.server import build_server


@pytest.mark.parametrize("expected", [True, False])
def test_tool_errors_expose_only_expected_failures(tmp_path, monkeypatch, expected):
    async def fail(self, operation, arguments):
        if expected:
            raise ValueError("Start a new task before further edits.")
        raise RuntimeError("unexpected private implementation detail")

    monkeypatch.setattr(Operations, "call", fail)
    server = build_server(Workspace(tmp_path, Path(sys.executable), "Test Author"))
    with pytest.raises(ToolError if expected else UnexpectedToolError) as caught:
        asyncio.run(server.call_tool("workspace_info", {}))
    if expected:
        assert "Start a new task" in str(caught.value)
    else:
        assert "private implementation detail" not in str(caught.value)
