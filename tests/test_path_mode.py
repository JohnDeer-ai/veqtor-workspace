# SPDX-License-Identifier: Apache-2.0
"""Selected paths belong to calls/tasks, not a mutable connection-wide folder."""
import asyncio
import json
import os
import sys
from pathlib import Path

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
