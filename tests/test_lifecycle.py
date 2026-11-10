# SPDX-License-Identifier: Apache-2.0
"""Failures across the actual process/MCP boundary; all documents are disposable."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from veqtor_workspace.operations import Operations
from veqtor_workspace.tasks import DocumentTasks


def prepared(project):
    workspace, tasks = project
    source = workspace.project / "contract.txt"
    source.write_text("before")
    task = tasks.start(str(source))
    candidate = Path(task["cwd"]) / task["candidate"]
    candidate.write_text("after")
    return workspace, tasks, source, task, candidate


@pytest.mark.parametrize("save_as", [False, True])
def test_final_status_write_failure_is_reported_as_saved_and_recovers(project, monkeypatch, save_as):
    w, tasks, source, task, _ = prepared(project)
    target = source.with_name("new result.txt") if save_as else source
    record = DocumentTasks._record

    def full_after_replace(folder, data):
        if data["saved"]:
            raise OSError("simulated full state storage")
        record(folder, data)

    with monkeypatch.context() as patch:
        patch.setattr(DocumentTasks, "_record", staticmethod(full_after_replace))
        result = tasks.save(task["task_id"], str(target) if save_as else None)
        assert result["status"] == "saved" and result["state_recovery_pending"]
        assert target.read_text() == "after"
        if save_as:
            assert source.read_text() == "before"
        restarted = DocumentTasks(w)
        with pytest.raises(ValueError, match="Start a new task"):
            restarted.context(task["task_id"], writing=True)
        assert restarted.save(task["task_id"])["status"] == "already_saved"
        assert restarted.status(task["task_id"])["status"] == "saved"
    # Recovery is written once storage works again, so later external edits remain distinguishable.
    assert tasks.status(task["task_id"])["state_recovery_pending"] is False
    target.write_text("user's later version")
    assert tasks.status(task["task_id"])["status"] == "saved_then_changed"
    assert tasks.save(task["task_id"])["current_file_matches_saved"] is False
    assert target.read_text() == "user's later version"


def test_failed_intent_write_prevents_replacement(project, monkeypatch):
    _, tasks, source, task, _ = prepared(project)

    def full(folder, data):
        raise OSError("full disk before replacement")

    monkeypatch.setattr(DocumentTasks, "_record", staticmethod(full))
    with pytest.raises(OSError, match="full disk"):
        tasks.save(task["task_id"])
    assert source.read_text() == "before"
    assert not tasks.context(task["task_id"], writing=True)[1]["saved"]


def inject_worker(monkeypatch, prelude):
    """Fault injection runs in the real worker process, not in a mock transport."""
    spawn = asyncio.create_subprocess_exec

    async def injected(*command, **kwargs):
        assert command[-2:] == ("-m", "veqtor_workspace.operations")
        return await spawn(sys.executable, "-c", prelude + "\nfrom veqtor_workspace.operations import main\nmain()\n", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", injected)


@pytest.mark.parametrize("after_replace", [False, True])
@pytest.mark.parametrize("save_as", [False, True])
def test_hard_worker_exit_before_or_after_replacement_recovers(project, monkeypatch, after_replace, save_as):
    w, _, source, task, _ = prepared(project)
    target = source.with_name("new result.txt") if save_as else source
    prelude = f'''
import os
from veqtor_workspace.tasks import DocumentTasks
record = DocumentTasks._record
def interrupted(folder, data):
    if data['saved'] == {after_replace!r}:
        if not data['saved']: record(folder, data)
        os._exit(71)
    record(folder, data)
DocumentTasks._record = staticmethod(interrupted)
'''
    with monkeypatch.context() as patch:
        inject_worker(patch, prelude)
        with pytest.raises(ValueError, match="worker stopped"):
            asyncio.run(Operations(w).call("document_task", {"action": "save", "task_id": task["task_id"],
                                                           "file": str(target) if save_as else None}))
    restored = DocumentTasks(w)
    if save_as:
        assert source.read_text() == "before"
        assert target.exists() == after_replace
    else:
        assert source.read_text() == ("after" if after_replace else "before")
    status = restored.status(task["task_id"])
    assert status["status"] == ("saved" if after_replace else "not_saved")
    assert status["editing_closed"]
    with pytest.raises(ValueError, match="Start a new task"):
        restored.context(task["task_id"], writing=True)
    assert restored.save(task["task_id"])["status"] == ("already_saved" if after_replace else "saved")
    assert target.read_text() == "after"


@pytest.mark.parametrize("operation", ["workspace_info", "document_task", "check_docx"])
def test_whole_call_deadline_covers_non_python_operations(project, monkeypatch, operation):
    w, _, source, task, _ = prepared(project)
    w.call_timeout_seconds = .8
    inject_worker(monkeypatch, '''
import time
import veqtor_workspace.operations as ops
perform = ops.perform
def stalled(*args):
    time.sleep(10)
    return perform(*args)
ops.perform = stalled
''')
    started = time.monotonic()
    result = asyncio.run(Operations(w).call(operation, {"action": "save", "task_id": task["task_id"]}))
    assert result["status"] == "timed_out"
    assert time.monotonic() - started < 3
    assert source.read_text() == "before"
    assert not w.lock.locked()


def test_cancel_during_save_preparation_never_replaces_later(project, monkeypatch):
    w, tasks, source, task, _ = prepared(project)
    prepared_marker = Path(task["cwd"]) / "save-prepared"
    inject_worker(monkeypatch, '''
import time
from veqtor_workspace.tasks import DocumentTasks
record = DocumentTasks._record
def slow_prepare(folder, data):
    record(folder, data)
    if not data['saved']:
        (folder / 'save-prepared').touch()
        time.sleep(1.5)
DocumentTasks._record = staticmethod(slow_prepare)
''')

    async def exercise():
        request = asyncio.create_task(Operations(w).call("document_task", {"action": "save", "task_id": task["task_id"]}))
        await until_exists(prepared_marker)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.sleep(1.6)

    asyncio.run(exercise())
    assert source.read_text() == "before"
    assert tasks.status(task["task_id"])["status"] == "not_saved"
    assert not w.lock.locked()


async def until_exists(path, seconds=4):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(.02)
    assert path.exists(), path


def unpack(result):
    assert not result.is_error, result
    data = result.structured_content or json.loads(result.content[0].text)
    return data.get("result", data)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_client_timeout_kills_children_and_busy_save_is_never_queued(project, mode):
    w, _, source, task, _ = prepared(project)
    ready, late = Path(task["cwd"]) / "work/ready", Path(task["cwd"]) / "work/late-child"
    code = f'''
from pathlib import Path
import subprocess,sys,time
subprocess.Popen([sys.executable, '-c', "import time;from pathlib import Path;time.sleep(2.2);Path('work/late-child').touch()"])
Path('work/ready').touch()
time.sleep(5)
Path({task['candidate']!r}).write_text('late replacement')
'''

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--project", str(w.project), "--python", str(w.python), "--author", w.author])
        async with Client(stdio_client(params), mode=mode) as client:
            request = asyncio.create_task(client.call_tool("run_python",
                {"code": code, "task_id": task["task_id"]}, read_timeout_seconds=1.4))
            await until_exists(ready)
            for tool, args in [("document_task", {"action": "save", "task_id": task["task_id"]}),
                               ("check_docx", {"task_id": task["task_id"]}),
                               ("run_python", {"code": "print('must not be queued')"})]:
                refused = await client.call_tool(tool, args, read_timeout_seconds=.7)
                assert refused.is_error and "not queued" in refused.content[0].text, refused
            with pytest.raises(Exception, match="[Tt]imeout|[Tt]imed out"):
                await request
            await asyncio.sleep(.2)
            probe = unpack(await client.call_tool("run_python", {"code": "print('ready')"}))
            assert probe["stdout"] == "ready\n", probe
            await asyncio.sleep(1.2)
            assert not late.exists()
            assert source.read_text() == "before"
            status = unpack(await client.call_tool("document_task", {"action": "status", "task_id": task["task_id"]}))
            assert status["status"] == "not_saved"
            for invalid in (None, 271):
                result = await client.call_tool("run_python", {"code": "print('bad')", "timeout_seconds": invalid})
                assert result.is_error, result
    asyncio.run(exercise())


def test_server_process_death_also_stops_worker_and_script(project):
    w, _, _, task, _ = prepared(project)
    ready, late = Path(task["cwd"]) / "work/ready", Path(task["cwd"]) / "work/late"
    code = "from pathlib import Path;import time;Path('work/ready').touch();time.sleep(2);Path('work/late').touch()"
    parent_code = f'''
import asyncio
from pathlib import Path
from veqtor_workspace.runner import Workspace
from veqtor_workspace.operations import Operations
w = Workspace(Path({str(w.root)!r}), Path({str(w.python)!r}), 'Ilya', Path({str(w.project)!r}))
asyncio.run(Operations(w).call('run_python', {{'code':{code!r},'task_id':{task['task_id']!r}}}))
'''
    process = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(until_exists(ready))
        process.kill()
        process.wait(timeout=3)
        time.sleep(2.2)
        assert not late.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_cancel_large_reply_frees_connection(project, monkeypatch, stream):
    """The real readers used to stop draining, leaving wait() and the mutex stuck."""
    w, _, _, _, _ = prepared(project)
    w.call_timeout_seconds = .6
    marker = w.root / "writing-large-reply"
    spawn = asyncio.create_subprocess_exec
    children = []
    reading = asyncio.Event()

    async def injected(*args, **kwargs):
        code = ("import os,sys,json;from pathlib import Path;"
                "json.loads(sys.stdin.buffer.readline());data=b'x'*(20*1024*1024);"
                f"Path({str(marker)!r}).touch();os.write({1 if stream == 'stdout' else 2},data)")
        process = await spawn(sys.executable, "-c", code, **kwargs)
        pipe = getattr(process, stream)
        read = pipe.read
        first = True

        async def slow_reader(n=-1):
            nonlocal first
            if first:
                first = False
                head = await read(1)
                reading.set()
                await asyncio.sleep(.2)  # backpressure while a large reply is in flight
                return head + await read(n)
            return await read(n)

        pipe.read = slow_reader
        children.append(process)
        return process

    async def exercise():
        scopes = []

        async def call():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await Operations(w).call("workspace_info", {})

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_subprocess_exec", injected)
            request = asyncio.create_task(call())
            try:
                await asyncio.wait_for(reading.wait(), 3)
                await asyncio.sleep(.01)
                scopes[0].cancel()
                await asyncio.sleep(.9)
                assert request.done() and not w.lock.locked()
                assert children[0].returncode == -9
            finally:
                if not request.done():
                    await asyncio.gather(children[0].stdout.read(), children[0].stderr.read())
                await request
        response = await Operations(w).call("workspace_info", {})
        assert response["prototype"] == "0.0.12"
    asyncio.run(exercise())


def test_inner_timeout_keeps_unbuffered_progress(project):
    w, _, _, _, _ = prepared(project)
    w.call_timeout_seconds = 2

    async def exercise():
        result = await Operations(w).call("run_python", {
            "code": "import time;print('progress before sleep');time.sleep(5)", "timeout_seconds": .3})
        assert result["timed_out"] and result["stdout"] == "progress before sleep\n", result
        assert not w.lock.locked()
    asyncio.run(exercise())


def test_old_running_server_can_start_new_worker_until_reconnect(project):
    w, _, _, _, _ = prepared(project)
    payload = {"root": str(w.root), "python": str(w.python), "author": w.author,
               "project": str(w.project), "call_timeout_seconds": 270,
               "operation": "workspace_info", "arguments": {}}
    # Keep stdin open, matching the old supervisor's parent-liveness pipe.
    process = subprocess.Popen([sys.executable, "-m", "veqtor_workspace.operations"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        process.stdin.write(json.dumps(payload).encode()+b"\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["result"]["access_mode"] == "legacy_folder"
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.stdin.close()
        process.wait(timeout=3)
