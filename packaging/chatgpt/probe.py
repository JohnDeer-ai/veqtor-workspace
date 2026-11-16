# SPDX-License-Identifier: Apache-2.0
"""Read-only MCP handshake before changing a desktop client's configuration."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client


async def probe(root: Path, author: str, version: str):
    params = StdioServerParameters(command=sys.executable,
        args=["-I", str(root / "veqtor-launch.py"), "--author", author,
              "--state-dir", str(root / "installation-check")], cwd=str(root))
    async with Client(stdio_client(params)) as client:
        reply = await client.list_tools()
        tools = reply.tools if hasattr(reply, "tools") else reply
        assert {t.name for t in tools} == {"workspace_info", "document_task", "run_python", "check_docx"}
        reply = await client.call_tool("workspace_info", {})
        assert not reply.is_error
        value = reply.structured_content or json.loads(reply.content[0].text)
        value = value.get("result", value)
        assert value["prototype"] == version and value["author"] == author
        print(json.dumps({"version": version, "tools": 4, "author_matches": True}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--author", required=True)
    p.add_argument("--version", required=True)
    a = p.parse_args()
    asyncio.run(probe(a.root, a.author, a.version))
