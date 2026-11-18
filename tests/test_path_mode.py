# SPDX-License-Identifier: Apache-2.0
"""Selected paths belong to calls/tasks, not a mutable connection-wide folder."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from test_checks import docx, para, run
from test_preview import PNG


def test_two_folders_one_connection_scoped_reads_and_saves(tmp_path):
    folders = [tmp_path / name for name in ("первый проект", "second project")]
    for folder in folders:
        folder.mkdir()
        (folder / "contract.txt").write_text(folder.name)
    reference = folders[1] / "reference.txt"
    reference.write_text("supporting evidence")
    secret = tmp_path / "not-selected.txt"
    secret.write_text("not readable")
    (folders[1] / "escape.txt").symlink_to(secret)
    state = tmp_path / "private state"
    config = tmp_path / "shared-preview.json"
    config.write_text("{}")

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(state), "--preview-config", str(config), "--author", "Ilya"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with Client(stdio_client(params)) as client:
            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                data = result.structured_content or json.loads(result.content[0].text)
                return data.get("result", data)

            info = await call("workspace_info", {})
            assert info["access_mode"] == "paths" and info["project_folder"] is None
            assert info["limits"]["default_timeout_seconds"] == 255
            assert info["preview"]["config"] == str(config)
            # A folder can be read without starting a document editing task.
            read = await call("run_python", {"read_paths": [str(folders[1])], "code": f'''
from pathlib import Path
print(Path({str(reference)!r}).read_text())
try: Path({str(folders[1] / 'escape.txt')!r}).read_text()
except PermissionError: print('symlink blocked')
'''
            })
            assert read["stdout"] == "supporting evidence\nsymlink blocked\n"
            for folder in folders:
                source = folder / "contract.txt"
                task = await call("document_task", {"action": "start", "file": str(source),
                                                     "read_paths": [str(reference)]})
                assert Path(task["cwd"]).is_relative_to(state)
                code = f'''
from pathlib import Path
print(Path({str(reference)!r}).read_text())
for p,writing in [({str(source)!r},True),({str(secret)!r},False)]:
    try: Path(p).write_text('bad') if writing else Path(p).read_text()
    except PermissionError: print('blocked')
Path({task['candidate']!r}).write_text('edited')
'''
                result = await call("run_python", {"task_id": task["task_id"], "code": code})
                assert result["exit_code"] == 0 and result["stdout"].count("blocked") == 2, result
                assert (await call("document_task", {"action": "save", "task_id": task["task_id"]}))["status"] == "saved"
                assert source.read_text() == "edited"
                assert not (folder / ".veqtor").exists()
            # The previous folder/file grants do not leak into a fresh read call.
            blocked = await call("run_python", {"code": f"from pathlib import Path; Path({str(reference)!r}).read_text()"})
            assert "PermissionError" in blocked["stderr"]
            invalid = await client.call_tool("document_task", {"action": "start", "file": "contract.txt"})
            assert invalid.is_error
            invalid = await client.call_tool("document_task", {"action": "start", "file": str(state / "work/injected.txt")})
            assert invalid.is_error
    asyncio.run(exercise())


async def call_tool(client, name, args):
    reply = await client.call_tool(name, args)
    assert not reply.is_error, reply
    data = reply.structured_content or json.loads(reply.content[0].text)
    return data.get("result", data)


def test_private_task_history_cannot_be_read_even_through_selected_parent(tmp_path):
    state = tmp_path / "private state"
    source, other = tmp_path / "first.txt", tmp_path / "second.txt"
    source.write_text("First task contents")
    other.write_text("Second task contents")

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(state), "--author", "Ilya",
            "--python", os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)])
        async with Client(stdio_client(params)) as client:
            first = await call_tool(client, "document_task", {"action": "start", "file": str(source)})
            own = await call_tool(client, "run_python", {"task_id": first["task_id"],
                "code": f"from pathlib import Path; print(Path({first['original']!r}).read_text())"})
            assert own["stdout"] == "First task contents\n"
            previous = await call_tool(client, "run_python", {"code": "print('Earlier read-only output')"})
            preview = state / ".runs/previews/example/page-1.png"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"private preview contents")
            snapshot = Path(first["snapshot"])
            alias = state / "work/alias.txt"
            alias.symlink_to(snapshot)
            paths = [snapshot, Path(first["cwd"]) / first["candidate"],
                     Path(first["cwd"]) / own["stdout_path"], state / previous["stdout_path"], preview, alias]
            probe = f'''
from pathlib import Path
for name in {[str(p) for p in paths]!r}:
    try: Path(name).read_bytes(); print('UNEXPECTED READ')
    except PermissionError: print('blocked')
'''
            second = await call_tool(client, "document_task", {"action": "start", "file": str(other)})
            for options in ({}, {"read_paths": [str(tmp_path)]},
                            {"task_id": second["task_id"], "read_paths": [str(tmp_path)]}):
                result = await call_tool(client, "run_python", {"code": probe, **options})
                assert result["exit_code"] == 0, result
                assert result["stdout"] == "blocked\n" * len(paths), result
            # Selecting a real external file still works, as does the active task's own history.
            own_again = await call_tool(client, "run_python", {"task_id": first["task_id"],
                "read_paths": [str(other)], "code": f"from pathlib import Path; print(Path({str(other)!r}).read_text()); "
                f"print(Path({own['stdout_path']!r}).read_text().strip())"})
            assert own_again["stdout"] == "Second task contents\nFirst task contents\n"
    asyncio.run(exercise())


@pytest.mark.parametrize("support_kind", ["file", "folder"])
def test_moved_support_does_not_block_copy_check_preview_edit_or_save(tmp_path, support_kind):
    source = docx(tmp_path / "contract.docx", [para(run("Original text"))])
    support = tmp_path / "support"
    if support_kind == "folder":
        support.mkdir()
        material = support / "reference.txt"
    else:
        material = support
    material.write_text("Supporting material")
    renderer = tmp_path / "renderer.py"
    renderer.write_text("import sys,base64;from pathlib import Path\n"
                        "p=Path(sys.argv[sys.argv.index('--output_dir')+1]);p.mkdir(parents=True)\n"
                        f"(p/'page-1.png').write_bytes(base64.b64decode({PNG!r}))\n")
    config = tmp_path / "preview.json"
    config.write_text(json.dumps({"python": sys.executable, "script": str(renderer)}))

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(tmp_path / "state"), "--preview-config", str(config), "--author", "Ilya"])
        async with Client(stdio_client(params)) as client:
            task = await call_tool(client, "document_task", {"action": "start", "file": str(source),
                                                            "read_paths": [str(support)]})
            support.rename(tmp_path / "moved-support")
            result = await call_tool(client, "run_python", {"task_id": task["task_id"], "code": f'''
from pathlib import Path
path = Path({task['candidate']!r})
path.write_bytes(path.read_bytes())
try: Path({str(material)!r}).read_text()
except FileNotFoundError: print('Only the missing material is unavailable')
'''
            })
            assert result["exit_code"] == 0, result
            assert result["stdout"] == "Only the missing material is unavailable\n"
            check = await client.call_tool("check_docx", {"task_id": task["task_id"], "render": True})
            assert not check.is_error, check
            report = json.loads(check.content[0].text)
            assert report["summary"]["observations"] == []
            assert report["preview"]["status"] == "rendered"
            assert "not Microsoft Word" in report["preview"]["page_numbering"]
            reused = await client.call_tool("check_docx", {"task_id": task["task_id"],
                "preview_id": report["preview"]["preview_id"], "preview_pages": [1]})
            assert not reused.is_error
            assert "not Microsoft Word" in json.loads(reused.content[0].text)["preview"]["page_numbering"]
            saved = await call_tool(client, "document_task", {"action": "save", "task_id": task["task_id"]})
            assert saved["status"] == "saved"
            assert (await call_tool(client, "document_task", {"action": "status", "task_id": task["task_id"]}))["status"] == "saved"
    asyncio.run(exercise())


def test_shared_preview_works_in_two_unconfigured_folders(tmp_path):
    sources = []
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        sources.append(docx(folder / "contract.docx", [para(run("Original"))]))
    renderer = tmp_path / "renderer.py"
    renderer.write_text("import sys,base64;from pathlib import Path\n"
                        "p=Path(sys.argv[sys.argv.index('--output_dir')+1]);p.mkdir(parents=True)\n"
                        f"(p/'page-1.png').write_bytes(base64.b64decode({PNG!r}))\n")
    config = tmp_path / "shared.json"
    config.write_text(json.dumps({"python": sys.executable, "script": str(renderer)}))

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(tmp_path / "state"), "--preview-config", str(config), "--author", "Ilya"])
        async with Client(stdio_client(params)) as client:
            for source in sources:
                response = await client.call_tool("document_task", {"action": "start", "file": str(source)})
                assert not response.is_error
                task = response.structured_content or json.loads(response.content[0].text)
                task = task.get("result", task)
                result = await client.call_tool("check_docx", {"task_id": task["task_id"], "render": True})
                assert not result.is_error
                report = json.loads(result.content[0].text)
                assert report["preview"]["status"] == "rendered", report
                assert sum(c.type == "image" for c in result.content) == 1
                assert not (source.parent / ".preview.json").exists()
    asyncio.run(exercise())


def test_explicit_docx_pair_changes_and_preview_keep_file_identity(tmp_path):
    incoming, working = tmp_path / "получено", tmp_path / "our version"
    incoming.mkdir()
    working.mkdir()
    original = docx(incoming / "example.docx", [para(run(f"Original clause {i}")) for i in range(1, 8)])
    candidate = docx(working / "example.docx", [para(run(f"Candidate clause {i}")) for i in range(1, 8)])
    before = original.read_bytes(), candidate.read_bytes()
    renderer = tmp_path / "renderer.py"
    renderer.write_text("import sys, base64\nfrom pathlib import Path\n"
                        "out = Path(sys.argv[sys.argv.index('--output_dir') + 1])\nout.mkdir(parents=True)\n"
                        f"for n in range(1, 8): (out / f'page-{{n}}.png').write_bytes(base64.b64decode({PNG!r}))\n")
    config = tmp_path / "preview.json"
    config.write_text(json.dumps({"python": sys.executable, "script": str(renderer)}))
    state = tmp_path / "private state"

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "veqtor_workspace.server",
            "--state-dir", str(state), "--preview-config", str(config), "--author", "Ilya"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with Client(stdio_client(params)) as client:
            paths = {"original": str(original), "candidate": str(candidate)}
            result = await client.call_tool("check_docx", {**paths, "render": True})
            assert not result.is_error, result
            report = json.loads(result.content[0].text)
            assert report["comparison"] == {"mode": "paths", **paths}
            assert report["preview"]["status"] == "rendered"
            assert list(report).index("changes") < list(report).index("preview")
            assert report["changes"]["total"] == 7 and report["changes"]["next_start"] == 5
            first = report["changes"]["items"][0]
            assert first["before"]["id"] == first["after"]["id"] == "word/document.xml#p1"
            assert first["before"]["text_excerpt"] == "Original clause 1"
            assert first["after"]["text_excerpt"] == "Candidate clause 1"

            request = {**paths, "preview_id": report["preview"]["preview_id"], "preview_pages": [7]}
            reused = await call_tool(client, "check_docx", request)
            assert reused["comparison"] == report["comparison"] and reused["check_reused"] is True
            assert reused["preview"]["shown_pages"] == [7]
            more = await call_tool(client, "check_docx", {**request, "changes_start": 5})
            assert [entry["after"]["location"]["paragraph"] for entry in more["changes"]["items"]] == [6, 7]
            assert more["changes"]["next_start"] is None
            repeated = await call_tool(client, "check_docx", {**request, "changes_start": 5})
            assert repeated["check_reused"] is True and repeated["changes"] == more["changes"]

            (state / "inputs/source.docx").write_bytes(before[0])
            other = state / "work/result.docx"
            other.write_bytes(before[1])
            relative = {"original": "inputs/source.docx", "candidate": "work/result.docx"}
            assert (await call_tool(client, "check_docx", relative))["changes"] == report["changes"]
            assert (await call_tool(client, "check_docx", {**relative, "candidate": str(candidate)}))["changes"] == report["changes"]
            misbound = await client.call_tool("check_docx", {**request, "candidate": str(other)})
            assert misbound.is_error and "different candidate path" in misbound.content[0].text

            task = await call_tool(client, "document_task", {"action": "start", "file": str(candidate)})
            task_report = await call_tool(client, "check_docx", {"task_id": task["task_id"], "render": True})
            assert task_report["comparison"] == {
                "mode": "task", "task_id": task["task_id"], "task_file": str(candidate),
                "original": task["snapshot"], "candidate": str(Path(task["cwd"]) / task["candidate"])}
            protected = Path(task["snapshot"])
            alias = tmp_path / "alias.docx"
            alias.symlink_to(protected)
            (state / "work/alias.docx").symlink_to(protected)
            blocked = [str(protected), str(alias), "work/alias.docx"]
            alternate_case = state.with_name("PRIVATE STATE") / protected.relative_to(state)
            if alternate_case.is_file():
                blocked.append(str(alternate_case))
            for path in blocked:
                response = await client.call_tool("check_docx", {**paths, "candidate": path})
                assert response.is_error, path

            copied = await client.call_tool("check_docx", {"original": str(candidate), "candidate": str(candidate),
                "preview_id": task_report["preview"]["preview_id"]})
            assert copied.is_error and "different original path" in copied.content[0].text
            assert (original.read_bytes(), candidate.read_bytes()) == before

    asyncio.run(exercise())
