# SPDX-License-Identifier: Apache-2.0
"""Round trips and failure controls for one visible working file; no native Word automation."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from test_checks import ME, check_docx, docx, para, rev, run

from veqtor_workspace.runner import Workspace
from veqtor_workspace.tasks import DocumentTasks, digest


def test_model_manual_model_rounds_use_latest_saved_baseline(project):
    workspace, tasks = project
    first = para(run("Pay within "), rev("del", "1", ME, run("30", "delText")),
                 rev("ins", "2", ME, run("45")), run(" days"))
    manual = para(run("User's saved clause"))
    source = docx(workspace.project / "Contract.docx", [para(run("Pay within 30 days"))])
    initial = digest(source)
    task = tasks.start(str(source))
    candidate = Path(task["cwd"]) / task["candidate"]
    docx(candidate, [first])
    assert not check_docx(Path(task["snapshot"]), candidate, ME)["summary"]["defects"]
    assert tasks.save(task["task_id"])["status"] == "saved"
    assert digest(Path(task["snapshot"])) == initial

    # Simulate a user's intervening on-disk edit, preserving the previous revisions.
    docx(source, [first, manual, para(run("Notice: 10 days"))])
    user_version = digest(source)
    task2 = tasks.start(str(source))
    assert task2["original_sha256"] == user_version
    candidate2 = Path(task2["cwd"]) / task2["candidate"]
    docx(candidate2, [first, manual, para(run("Notice: "), rev("del", "3", ME, run("10", "delText")),
                                         rev("ins", "4", ME, run("20")), run(" days"))])
    assert not check_docx(Path(task2["snapshot"]), candidate2, ME)["summary"]["defects"]
    assert tasks.save(task2["task_id"])["status"] == "saved"
    assert digest(source) == digest(candidate2)
    assert digest(Path(task2["snapshot"])) == user_version
    assert sorted(p.name for p in workspace.project.iterdir() if not p.name.startswith(".")) == ["Contract.docx"]
    with pytest.raises(ValueError, match="Start a new task"):
        tasks.context(task2["task_id"], writing=True)
    assert tasks.save(task2["task_id"])["status"] == "already_saved"
    source.write_text("Another user version")
    assert tasks.save(task2["task_id"])["current_file_matches_saved"] is False
    assert source.read_text() == "Another user version"


@pytest.mark.parametrize("during_copy", [False, True])
def test_conflicting_save_does_not_replace_user_file(project, monkeypatch, during_copy):
    workspace, tasks = project
    source = workspace.project / "contract.txt"
    source.write_text("original")
    task = tasks.start(str(source))
    candidate = Path(task["cwd"]) / task["candidate"]
    candidate.write_text("model result")
    if during_copy:
        copy = shutil.copyfile

        def concurrent_copy(src, dst):
            result = copy(src, dst)
            source.write_text("user's newer version")
            return result

        monkeypatch.setattr(shutil, "copyfile", concurrent_copy)
    else:
        source.write_text("user's newer version")
    assert tasks.save(task["task_id"])["status"] == "conflict"
    assert source.read_text() == "user's newer version"
    assert Path(task["snapshot"]).read_text() == "original"
    assert not list(source.parent.glob(".contract-*.txt"))


def test_copy_failure_preserves_original_and_task_can_retry(project, monkeypatch):
    workspace, tasks = project
    source = workspace.project / "contract.txt"
    source.write_text("original")
    task = tasks.start(str(source))
    (Path(task["cwd"]) / task["candidate"]).write_text("model result")

    def failed_copy(src, dst):
        Path(dst).write_text("partial")
        raise OSError("disk error")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copyfile", failed_copy)
        with pytest.raises(OSError, match="disk error"):
            tasks.save(task["task_id"])
    assert source.read_text() == "original"
    assert not list(source.parent.glob(".contract-*.txt"))
    assert DocumentTasks(workspace).save(task["task_id"])["status"] == "saved"


def test_project_reads_and_snapshot_write_restrictions(project):
    workspace, tasks = project
    source = workspace.project / "contract.txt"
    source.write_text("original")
    (workspace.project / "reference.txt").write_text("supporting evidence")
    task = tasks.start(str(source))
    context, _ = tasks.context(task["task_id"])
    code = f'''
from pathlib import Path
import os
print(Path({str(workspace.project / 'reference.txt')!r}).read_text())
for name, action in [
    ("project", lambda: Path({str(source)!r}).write_text("bad")),
    ("snapshot", lambda: Path({task['original']!r}).write_text("bad")),
    ("metadata", lambda: Path("task.json").write_text("bad")),
    ("hardlink", lambda: os.link({str(source)!r}, "work/alias.txt")),
]:
    try: action(); print(name, "UNEXPECTED_SUCCESS")
    except OSError: print(name, "blocked")
Path({task['candidate']!r}).write_text("good result")
'''
    result = context.run(code)
    assert result["exit_code"] == 0, result
    assert "supporting evidence" in result["stdout"]
    assert "UNEXPECTED_SUCCESS" not in result["stdout"], result
    assert source.read_text() == "original"
    assert Path(task["snapshot"]).read_text() == "original"
    assert tasks.save(task["task_id"])["status"] == "saved"
    assert source.read_text() == "good result"


def test_task_path_boundaries(project, tmp_path):
    workspace, tasks = project
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (workspace.project / "escape.txt").symlink_to(outside)
    for name in (str(outside), "escape.txt", str(workspace.root / "work")):
        with pytest.raises(ValueError):
            tasks.start(name)
    with pytest.raises(ValueError):
        tasks.read("../work")
    with pytest.raises(ValueError):
        Workspace(outside.parent, workspace.python, ME, workspace.project)


def test_project_workflow_over_mcp(project):
    workspace, _ = project
    source = docx(workspace.project / "Contract.docx", [para(run("Pay within 30 days"))])

    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "veqtor_workspace.server", "--project", str(workspace.project),
                  "--python", str(workspace.python), "--author", ME],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with Client(stdio_client(params)) as client:
            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                data = result.structured_content or json.loads(result.content[0].text)
                return data.get("result", data)

            task = await call("document_task", {"action": "start", "file": str(source)})
            code = f'''
from zipfile import ZipFile
from lxml import etree as E
from veqtor_workspace.files import atomic_save
path = {task['candidate']!r}
with ZipFile(path) as archive:
    entries = [(info, archive.read(info)) for info in archive.infolist()]
with atomic_save(path) as draft:
    with ZipFile(draft, "w") as archive:
        for info, data in entries:
            if info.filename == "word/document.xml":
                root = E.fromstring(data)
                w = "{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}"
                p = root.find(".//" + w + "p")
                r = E.SubElement(p, w + "ins", {{w + "id": "1", w + "author": "Ilya", w + "date": "2026-11-08T00:00:00Z"}})
                E.SubElement(E.SubElement(r, w + "r"), w + "t").text = "; confirmed."
                data = E.tostring(root)
            archive.writestr(info, data)
print("edited")
'''
            result = await call("run_python", {"task_id": task["task_id"], "code": code})
            assert result["stdout"] == "edited\n", result
            check = await call("check_docx", {"task_id": task["task_id"]})
            assert not check["summary"]["defects"], check
            saved = await call("document_task", {"action": "save", "task_id": task["task_id"]})
            assert saved["status"] == "saved"
            assert digest(source) == saved["sha256"]
    asyncio.run(exercise())
