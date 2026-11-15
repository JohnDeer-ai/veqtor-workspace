# SPDX-License-Identifier: Apache-2.0
"""Continue stored output without rerunning code or opening the private archive."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from veqtor_workspace.operations import Operations
from veqtor_workspace.output import MAX_REPLY, read_stream
from veqtor_workspace.runner import Workspace


@pytest.mark.parametrize("raw", [b"", b"x" * MAX_REPLY, b"x" * (MAX_REPLY + 1),
    b"x" * 11999 + "🙂абв漢字".encode() * 4000,
    b"x" * 11999 + b"\xff\xf0\x9f\x99\x82\xe2\x82"])
def test_output_pages_keep_unicode_boundaries_and_byte_counts(tmp_path, raw):
    path = tmp_path / "output"
    path.write_bytes(raw)
    offset, texts = 0, []
    while True:
        page = read_stream(path, offset, len(raw))
        texts.append(page["text"])
        assert page["total_bytes"] == len(raw)
        assert 0 <= page["_next_offset"] - offset <= MAX_REPLY
        assert page["remaining_bytes"] == len(raw) - page["_next_offset"]
        if not page["remaining_bytes"]:
            break
        assert page["_next_offset"] > offset
        offset = page["_next_offset"]
    assert "".join(texts) == raw.decode("utf-8", errors="replace")


@pytest.mark.parametrize("task_mode", [False, True])
def test_mcp_continuation_both_streams_scratch_and_saved_task(tmp_path, task_mode):
    root = tmp_path / "private state"
    source = tmp_path / "source.txt"
    source.write_text("original")
    stdout = "a" * 11999 + "🙂текст" * 4000 + "\n"
    stderr = "error details " * 2500 + "\n"

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(root), "--author", "Test Author", "--python", os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)])
        async with Client(stdio_client(params)) as client:
            async def call(name, args):
                response = await client.call_tool(name, args)
                assert not response.is_error, response
                data = response.structured_content or json.loads(response.content[0].text)
                return data.get("result", data)

            info = await call("workspace_info", {})
            assert info["scratch_dir"] == str(root / "work")
            assert "read_docx" in info["recipe_text"] and "tempfile" in info["recipe_text"]
            assert len((await client.list_tools()).tools) == 4
            task = await call("document_task", {"action": "start", "file": str(source)}) if task_mode else None
            options = {"task_id": task["task_id"]} if task else {}
            code = f'''
import sys, tempfile
from pathlib import Path
count = Path('work/count.txt')
count.write_text((count.read_text() if count.exists() else '') + '1')
Path('work/extract.txt').write_text('saved extract')
with tempfile.NamedTemporaryFile(mode='w+') as f:
    f.write('temporary extract'); f.flush(); f.seek(0)
    assert f.read() == 'temporary extract'
{f"Path({task['candidate']!r}).write_text('edited once')" if task else ''}
print({stdout!r}, end='')
print({stderr!r}, end='', file=sys.stderr)
'''
            result = await call("run_python", {"code": code, **options})
            assert result["exit_code"] == 0 and "_output_pages" not in result
            scratch = Path(task["cwd"]) / "work" if task else root / "work"
            assert result["scratch_dir"] == str(scratch)
            assert "output_cursor" in result["output_hint"]
            later = await call("run_python", {"code": "from pathlib import Path; print(Path('work/extract.txt').read_text())", **options})
            assert later["stdout"] == "saved extract\n"
            if task:
                assert (await call("document_task", {"action": "save", **options}))["status"] == "saved"
            for kind, expected in (("stdout", stdout), ("stderr", stderr)):
                cursor = result[f"{kind}_next_cursor"]
                text = result[kind]
                offset = len(text.encode())
                assert result[f"{kind}_remaining_bytes"] == len(expected.encode()) - offset
                first = await call("run_python", {"output_cursor": cursor})
                assert await call("run_python", {"output_cursor": cursor}) == first
                while cursor:
                    page = await call("run_python", {"output_cursor": cursor})
                    assert page["run_id"] == result["run_id"] and page["stream"] == kind
                    assert page["offset_bytes"] == offset
                    text += page["text"]
                    offset += len(page["text"].encode())
                    assert page["remaining_bytes"] == len(expected.encode()) - offset
                    cursor = page["next_cursor"]
                assert text == expected
            assert (scratch / "count.txt").read_text() == "1"
            assert source.read_text() == ("edited once" if task else "original")
    asyncio.run(exercise())


def test_cursors_reject_other_connections_tampering_and_code_without_exposing_logs(tmp_path):
    w = Workspace(tmp_path / "state", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Test", dynamic=True)

    async def exercise():
        operations = Operations(w)
        result = await operations.call("run_python", {"code": "print('x'*26000)"})
        cursor = result["stdout_next_cursor"]
        with pytest.raises(ValueError, match="connection"):
            await Operations(w).call("run_python", {"output_cursor": cursor})
        payload, signature = cursor.split(".")
        forged = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
        for bad in (forged, "", "../.runs/private.stdout", cursor + "suffix"):
            with pytest.raises(ValueError, match="cursor"):
                await operations.call("run_python", {"output_cursor": bad})
        for extra in ({"code": "from pathlib import Path; Path('work/should-not-run').touch()"},
                      {"task_id": "0" * 12}, {"read_paths": [str(tmp_path)]}):
            with pytest.raises(ValueError, match="alone"):
                await operations.call("run_python", {"output_cursor": cursor, **extra})
        assert not (w.root / "work/should-not-run").exists()
        probe = await operations.call("run_python", {"code": f"from pathlib import Path; Path({result['stdout_path']!r}).read_text()"})
        assert "PermissionError" in probe["stderr"]
        assert (await operations.call("run_python", {"output_cursor": cursor}))["text"] == "x" * MAX_REPLY
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_missing_or_changed_output_does_not_rerun_code(tmp_path, failure):
    w = Workspace(tmp_path / "state", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Test", dynamic=True)

    async def exercise():
        operations = Operations(w)
        result = await operations.call("run_python", {"code": "from pathlib import Path; Path('work/count').write_text('once'); print('x'*14000)"})
        path = w.root / result["stdout_path"]
        if failure == "missing":
            path.unlink()
        else:
            path.write_text("changed")
        with pytest.raises(ValueError, match="Stored output"):
            await operations.call("run_python", {"output_cursor": result["stdout_next_cursor"]})
        assert (w.root / "work/count").read_text() == "once"
    asyncio.run(exercise())
