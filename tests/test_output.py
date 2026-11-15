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
from veqtor_workspace.output import MAX_REPLY, output_text, read_stream
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


def test_output_cursor_keeps_captured_prefix_when_log_grows(tmp_path):
    path = tmp_path / "output"
    original = "a" * 11999 + "🙂текст" * 2000
    path.write_text(original)
    first = read_stream(path)
    with path.open("a") as stream:
        stream.write("later output" * 1000)
    text, page = first["text"], first
    while page["remaining_bytes"]:
        page = read_stream(path, page["_next_offset"], first["total_bytes"])
        text += page["text"]
        assert page["total_bytes"] == len(original.encode())
    assert text == original


def test_initial_output_page_never_reads_beyond_captured_size(tmp_path, monkeypatch):
    path = tmp_path / "output"
    path.write_bytes(b"initial")
    fstat = os.fstat

    def append_after_stat(fd):
        info = fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"later output")
        return info

    monkeypatch.setattr(os, "fstat", append_after_stat)
    page = read_stream(path)
    assert page["text"] == "initial" and page["remaining_bytes"] == 0
    assert page["_next_offset"] == page["total_bytes"] == 7


@pytest.mark.parametrize("initial_bytes", [0, 24000])
def test_child_output_after_main_exit_is_captured_before_cursors(tmp_path, monkeypatch, initial_bytes):
    w = Workspace(tmp_path / "state", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Test", dynamic=True)
    late_stdout, late_stderr = "late stdout🙂" * 1600, "late stderrЯ" * 1600
    child = f'''
import os,time
from pathlib import Path
os.write(1,b'x'*{initial_bytes}); os.write(2,b'e'*{initial_bytes})
Path('work/ready').touch()
while not Path('work/continue').exists(): time.sleep(.002)
os.write(1,{late_stdout.encode()!r}); os.write(2,{late_stderr.encode()!r})
Path('work/finished').touch()
while True: time.sleep(1)
'''
    code = f'''
import subprocess,sys,time
from pathlib import Path
count=Path('work/count')
count.write_text((count.read_text() if count.exists() else '')+'1')
subprocess.Popen([sys.executable,'-u','-c',{child!r}])
while not Path('work/ready').exists(): time.sleep(.002)
'''
    # Force the child to append after the runner builds its initial reply, before
    # the supervisor stops the group. This reproduces the race without relying on timing.
    prelude = '''
import time
from veqtor_workspace.runner import Workspace
original = Workspace._run
def append_before_worker_reply(self, *args):
    result = original(self, *args)
    (self.root / 'work/continue').touch()
    deadline = time.monotonic() + 5
    while not (self.root / 'work/finished').exists():
        if time.monotonic() > deadline: raise RuntimeError('Test child did not finish output')
        time.sleep(.002)
    return result
Workspace._run = append_before_worker_reply
from veqtor_workspace.operations import main
main()
'''
    spawn = asyncio.create_subprocess_exec

    async def injected(*command, **kwargs):
        assert command[-2:] == ("-m", "veqtor_workspace.operations")
        return await spawn(sys.executable, "-c", prelude, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", injected)

    async def exercise():
        operations = Operations(w)
        result = await operations.call("run_python", {"code": code})
        assert result["exit_code"] == 0
        for kind, expected in (("stdout", "x" * initial_bytes + late_stdout),
                               ("stderr", "e" * initial_bytes + late_stderr)):
            assert result[f"{kind}_truncated"]
            assert result[f"{kind}_remaining_bytes"] == len(expected.encode()) - len(result[kind].encode())
            text, cursor = result[kind], result[f"{kind}_next_cursor"]
            while cursor:
                page = await operations.call("run_python", {"output_cursor": cursor})
                text += page["text"]
                assert page["remaining_bytes"] == len(expected.encode()) - len(text.encode())
                cursor = page["next_cursor"]
            assert text == expected
        assert (w.root / "work/count").read_text() == "1"
    asyncio.run(exercise())


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
            catalog = (await client.list_tools()).tools
            assert len(catalog) == 4
            assert all(len(tool.description) < 2048 for tool in catalog)
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
        result = await operations.call("run_python", {"code": f"print('x'*{MAX_REPLY * 3})"})
        cursor = result["stdout_next_cursor"]
        assert len(cursor) == 62
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


def test_shared_output_ceiling_smaller_pages_visible_markers_and_exact_raw_text(tmp_path):
    w = Workspace(tmp_path / 'state', Path(os.environ.get('VEQTOR_TEST_PYTHON', sys.executable)), 'Test', dynamic=True,
                  max_reply_bytes=4096)
    stdout, stderr = 'строка🙂' * 2000, 'failure details ' * 800

    async def exercise():
        operations = Operations(w)
        result = await operations.call('run_python', {'reply_bytes': 100000, 'code':
            f"import sys; print({stdout!r}, end=''); print({stderr!r}, end='', file=sys.stderr)"})
        assert result['reply_bytes'] == 4096
        assert len(result['stdout'].encode()) + len(result['stderr'].encode()) <= 4096
        assert result['stdout'] and result['stderr']
        rendered = output_text(result)
        assert rendered.count('OUTPUT CONTINUES') == 2
        assert rendered.index(result['stdout']) < rendered.index(result['stdout_next_cursor'])
        for kind, expected in [('stdout', stdout), ('stderr', stderr)]:
            text, cursor = result[kind], result[f'{kind}_next_cursor']
            while cursor:
                page = await operations.call('run_python', {'output_cursor': cursor, 'reply_bytes': 1024})
                assert len(page['text'].encode()) <= 1024 and page['reply_bytes'] == 1024
                assert ('OUTPUT CONTINUES' in output_text(page)) == bool(page['next_cursor'])
                text += page['text']
                cursor = page['next_cursor']
            assert text == expected
        with pytest.raises(ValueError, match='positive integer'):
            await operations.call('run_python', {'code': "print('not run')", 'reply_bytes': -1})
    asyncio.run(exercise())


def test_invalid_utf8_does_not_expand_past_visible_output_budget(tmp_path):
    raw = b'\xff' * 25000
    path = tmp_path / 'raw'
    path.write_bytes(raw)
    page = read_stream(path)
    assert len(page['text'].encode()) <= MAX_REPLY
    assert page['remaining_bytes'] > 0 and page['_next_offset'] > 0


def test_results_list_only_current_artifacts_and_do_not_imply_mandatory_verification(tmp_path):
    w = Workspace(tmp_path / 'state', Path(os.environ.get('VEQTOR_TEST_PYTHON', sys.executable)), 'Test', dynamic=True)
    (w.root / 'work/previous.txt').write_text('earlier work')
    assert 'outputs' not in w.info()
    result = w.run("from pathlib import Path; Path('work/new.txt').write_text('new')")
    assert result['artifacts'] == ['work/new.txt'] and 'verification' not in result
    result = w.run("print('read-only call')")
    assert result['artifacts'] == []
    result = w.run("from pathlib import Path; Path('work/previous.txt').write_text('changed')")
    assert result['artifacts'] == ['work/previous.txt']


@pytest.mark.parametrize(('code', 'expected'), [
    ("Path('work/z-new.txt').write_text('new')", ['work/z-new.txt']),
    ("Path('work/file-100.txt').write_text('updated')", ['work/file-100.txt']),
    ("Path('work/file-000.txt').unlink()", []),
])
def test_artifacts_are_current_changes_even_with_more_than_100_existing_files(tmp_path, code, expected):
    w = Workspace(tmp_path / 'state', Path(os.environ.get('VEQTOR_TEST_PYTHON', sys.executable)), 'Test', dynamic=True)
    for i in range(101):
        (w.root / 'work' / f'file-{i:03}.txt').write_text('unchanged')
    result = w.run('from pathlib import Path\n' + code)
    assert result['exit_code'] == 0, result['stderr']
    assert result['artifacts'] == expected


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_missing_or_changed_output_does_not_rerun_code(tmp_path, failure):
    w = Workspace(tmp_path / "state", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Test", dynamic=True)

    async def exercise():
        operations = Operations(w)
        result = await operations.call("run_python", {"code": "from pathlib import Path; Path('work/count').write_text('once'); print('x'*50000)"})
        path = w.root / result["stdout_path"]
        if failure == "missing":
            path.unlink()
        else:
            path.write_text("changed")
        with pytest.raises(ValueError, match="Stored output"):
            await operations.call("run_python", {"output_cursor": result["stdout_next_cursor"]})
        assert (w.root / "work/count").read_text() == "once"
    asyncio.run(exercise())
