# SPDX-License-Identifier: Apache-2.0
"""Test the exact extension launch in a clean, disposable macOS environment.

Run with: uv run python packaging/smoke_mcpb.py dist/veqtor-workspace-VERSION-macos-arm64.mcpb
Requires uv and internet for a fresh managed Python/runtime download. Never changes
an installed client or uses real documents. This is a protocol test, not a GUI test.
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client


async def exercise(bundle: Path, root: Path):
    extension = root / "extension with spaces"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extension)
    manifest = json.loads((extension / "manifest.json").read_text())
    config = manifest["server"]["mcp_config"]
    author = "Release Test"
    args = [arg.replace("${__dirname}", str(extension)).replace("${user_config.author}", author)
            for arg in config["args"]]
    # Only the private state destination differs from an ordinary extension launch.
    args += ["--state-dir", str(root / "private state")]
    uv = shutil.which(config["command"])
    assert uv, "Install uv before running the release smoke test"
    home = root / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "UV_CACHE_DIR": str(root / "cache"), "UV_PYTHON_INSTALL_DIR": str(root / "python"),
           "UV_PYTHON_PREFERENCE": "only-managed", "PYTHONPATH": "", "VIRTUAL_ENV": "",
           **config.get("env", {})}
    # Hosted runners have compilers that a normal Mac may not have. Require
    # published wheels for every dependency before building our pure-Python
    # project. A successful source build on CI is not an installer acceptance.
    await asyncio.to_thread(subprocess.run,
        [uv, "sync", "--frozen", "--no-dev", "--no-install-project", "--no-build", "--directory", str(extension)],
        env=env, cwd=extension, check=True, timeout=300)
    params = StdioServerParameters(command=uv, args=args, env=env, cwd=str(extension))
    await exercise_documents(params, root, manifest["version"], author)
    assert any((root / "python").glob("cpython-*")), "Managed Python was not downloaded"
    print(json.dumps({"bundle": bundle.name, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                      "version": manifest["version"], "tools": 4, "fresh_runtime": True, "dependency_wheels_only": True,
                      "tracked_change": True, "comment": True, "save_as": True,
                      "in_place_save": True, "source_preserved": True, "saved_conflict_refused": True}))


async def exercise_documents(params: StdioServerParameters, root: Path, version: str, author: str):
    """Verify either installer through the exact connection it prepared."""
    source = root / "проверка with spaces.docx"
    doc = Document()
    doc.add_paragraph("The Client has 20 business days to review the Deliverables.")
    doc.save(source)
    original = source.read_bytes()
    supporting = root / "supporting notes.txt"
    supporting.write_text("Use 40 business days for the review period.")
    target = root / "reviewed copy.docx"

    async with Client(stdio_client(params)) as client:
        catalog = await client.list_tools()
        listed = catalog.tools if hasattr(catalog, "tools") else catalog
        assert {tool.name for tool in listed} == {"workspace_info", "document_task", "run_python", "check_docx"}

        async def call(name, arguments):
            reply = await client.call_tool(name, arguments)
            assert not reply.is_error, reply
            value = reply.structured_content or json.loads(reply.content[0].text)
            return value.get("result", value)

        info = await call("workspace_info", {})
        assert info["prototype"] == version and info["author"] == author
        task = await call("document_task", {"action": "start", "file": str(source), "read_paths": [str(supporting)]})
        code = f'''
import os
from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
assert Path({str(supporting)!r}).read_text() == "Use 40 business days for the review period."
doc = Document({task['candidate']!r})
p = doc.paragraphs[0]
p.runs[0].text = "The Client has "
for tag, text, ident in (("del", "20", "1"), ("ins", "40", "2")):
    change = OxmlElement("w:" + tag)
    for key, value in (("id", ident), ("author", os.environ["VEQTOR_AUTHOR"]), ("date", os.environ["VEQTOR_UTC_NOW"])):
        change.set(qn("w:" + key), value)
    run = OxmlElement("w:r")
    node = OxmlElement("w:delText" if tag == "del" else "w:t")
    node.text = text
    run.append(node)
    change.append(run)
    p._p.append(change)
p.add_run(" business days to review the Deliverables.")
doc.add_comment(p.runs[-1], text="Updated the review period using the supporting notes.", author=os.environ["VEQTOR_AUTHOR"], initials="RT")
doc.save({task['candidate']!r})
try:
    Path({str(source)!r}).write_text("must not be written")
except PermissionError:
    print("source write blocked")
else:
    raise AssertionError("Source was writable by task code")
'''
        edited = await call("run_python", {"task_id": task["task_id"], "code": code})
        assert edited["exit_code"] == 0 and "source write blocked" in edited["stdout"], edited
        diagnostic = await call("check_docx", {"task_id": task["task_id"]})
        assert not diagnostic["summary"]["errors"], diagnostic
        saved = await call("document_task", {"action": "save", "task_id": task["task_id"], "file": str(target)})
        assert saved["status"] == "saved" and source.read_bytes() == original
        with zipfile.ZipFile(target) as archive:
            xml = etree.fromstring(archive.read("word/document.xml"))
            comments = etree.fromstring(archive.read("word/comments.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        assert xml.xpath("//w:del/w:r/w:delText/text()", namespaces=ns) == ["20"]
        assert xml.xpath("//w:ins/w:r/w:t/text()", namespaces=ns) == ["40"]
        assert comments.xpath("//w:comment/@w:author", namespaces=ns) == [author]

        # Exercise in-place saving as well as refusing a newer saved source.
        plain = root / "in-place.txt"
        plain.write_text("original")
        task = await call("document_task", {"action": "start", "file": str(plain)})
        edited = await call("run_python", {"task_id": task["task_id"], "code": f"from pathlib import Path; Path({task['candidate']!r}).write_text('updated')"})
        assert edited["exit_code"] == 0
        await call("document_task", {"action": "save", "task_id": task["task_id"]})
        assert plain.read_text() == "updated"
        task = await call("document_task", {"action": "start", "file": str(plain)})
        plain.write_text("newer saved version")
        refused = await call("document_task", {"action": "save", "task_id": task["task_id"]})
        assert refused["status"] == "conflict" and plain.read_text() == "newer saved version"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="veqtor-release-") as temporary:
        asyncio.run(exercise(options.bundle.resolve(), Path(temporary).resolve()))
