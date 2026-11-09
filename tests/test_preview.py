# SPDX-License-Identifier: Apache-2.0
"""Preview failure, deadline/child cleanup, and usable MCP image responses."""

import asyncio
import base64
import json
import os
import platform
import sys
import time
from pathlib import Path

import pytest
from docx import Document
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from veqtor_workspace.preview import render_pair

pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="macOS renderer restrictions")
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII="


@pytest.fixture(params=["separate_folders", "same_folder"])
def preview(tmp_path, request):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    original = (work if request.param == "same_folder" else inputs) / "source.docx"
    candidate = work / "result.docx"
    doc = Document()
    doc.add_paragraph("Unchanged document")
    doc.save(original)
    candidate.write_bytes(original.read_bytes())
    script = tmp_path / "test renderer.py"
    config = tmp_path / ".preview.json"
    config.write_text(json.dumps({"python": sys.executable, "script": str(script)}))
    return original, candidate, work / ".previews", config, script


def test_missing_renderer_is_explicit(preview):
    original, candidate, output, config, _ = preview
    result = render_pair(original, candidate, output, config)
    assert result["status"] == "not_available"
    assert result["visual_review"] == "not_performed"
    assert not output.exists()


def test_render_failure_does_not_claim_visual_success(preview):
    original, candidate, output, config, script = preview
    script.write_text("raise SystemExit(7)")
    result = render_pair(original, candidate, output, config)
    assert result["status"] == "failed"
    assert "exited 7" in result["detail"]
    assert result["images"] == []


def test_timeout_stops_descendants_and_has_no_retry(preview):
    original, candidate, output, config, script = preview
    marker = output.parent / "child-survived"
    script.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',\"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()\"])\n"
        "time.sleep(10)\n"
    )
    started = time.monotonic()
    result = render_pair(original, candidate, output, config, timeout_seconds=0.4)
    assert result["status"] == "timed_out"
    assert time.monotonic() - started < 2
    time.sleep(2.1)
    assert not marker.exists()
    assert len(list(output.iterdir())) == 1


def test_preview_protects_documents_and_mcp_returns_images(preview):
    original, candidate, output, config, script = preview
    before = original.read_bytes()
    script.write_text(
        "from pathlib import Path\nimport sys,base64,socket\n"
        # Real restrictions: both files stay protected even when outputs share their folder.
        f"for target in [{str(original)!r},{str(candidate)!r}]:\n"
        " try: Path(target).write_text('unexpected write')\n"
        " except PermissionError: pass\n"
        " else: raise RuntimeError('document write was not blocked')\n"
        "try: socket.create_connection(('127.0.0.1',9),timeout=0.2)\n"
        "except PermissionError: pass\n"
        "else: raise RuntimeError('network was not blocked')\n"
        "p=Path(sys.argv[sys.argv.index('--output_dir')+1]);p.mkdir(parents=True)\n"
        f"(p/'page-1.png').write_bytes(base64.b64decode({PNG!r}))\n"
    )

    if original.parent == candidate.parent:
        # The direct adapter permits siblings; the MCP path contract uses inputs/work.
        data = render_pair(original, candidate, output, config)
        assert data["status"] == "rendered", data
        assert data["changed_pages"] == []
        assert original.read_bytes() == candidate.read_bytes() == before
        return

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "veqtor_workspace.server", "--workspace", str(config.parent), "--author", "Ilya"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with Client(stdio_client(params)) as client:
            result = await client.call_tool("check_docx", {
                "original": "inputs/source.docx", "candidate": "work/result.docx", "render": True})
            data = json.loads(result.content[0].text)
            assert data["preview"]["status"] == "rendered", data
            assert data["preview"]["changed_pages"] == []
            assert data["preview"]["visual_review"] == "not_performed"
            assert len(result.content) == 2
            assert result.content[1].type == "image"
            assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG")
            blocked = await client.call_tool("check_docx", {
                "original": "inputs/source.docx", "candidate": "../outside.docx", "render": True})
            assert blocked.is_error

    asyncio.run(exercise())
    assert original.read_bytes() == candidate.read_bytes() == before


def test_mcp_later_pages_without_reconversion_and_stale_preview_rejected(tmp_path):
    inputs, work = tmp_path / "inputs", tmp_path / "work"
    inputs.mkdir()
    work.mkdir()
    original, candidate = inputs / "source.docx", work / "result.docx"
    document = Document()
    document.add_paragraph("Source content")
    document.save(original)
    candidate.write_bytes(original.read_bytes())
    renderer = tmp_path / "renderer.py"
    renderer.write_text(
        "from pathlib import Path\nimport sys,base64\n"
        "p=Path(sys.argv[sys.argv.index('--output_dir')+1]);p.mkdir(parents=True)\n"
        f"for i in range(1,8): (p/f'page-{{i}}.png').write_bytes(base64.b64decode({PNG!r}))\n"
    )
    (tmp_path / ".preview.json").write_text(json.dumps({"python": sys.executable, "script": str(renderer)}))

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "veqtor_workspace.server", "--workspace", str(tmp_path), "--author", "Ilya"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with Client(stdio_client(params)) as client:
            paths = {"original": "inputs/source.docx", "candidate": "work/result.docx"}
            initial = await client.call_tool("check_docx", {**paths, "render": True, "preview_timeout_seconds": 180})
            data = json.loads(initial.content[0].text)["preview"]
            assert data["status"] == "rendered", data
            assert data["files"]["candidate"]["pages"] == 7
            assert data["timeout_seconds"] == 180
            renderer.unlink()  # Later pages must not need the converter again.
            more = await client.call_tool("check_docx", {
                **paths, "preview_id": data["preview_id"], "preview_pages": [4, 5, 6]})
            page_data = json.loads(more.content[0].text)["preview"]
            assert page_data["reused"] is True
            assert page_data["page_numbering"] == data["page_numbering"]
            assert "not Microsoft Word" in page_data["page_numbering"]
            assert page_data["shown_pages"] == [4, 5, 6]
            assert sum(c.type == "image" for c in more.content) == 3
            before = await client.call_tool("check_docx", {
                **paths, "preview_id": data["preview_id"], "preview_pages": [7], "preview_side": "original"})
            assert json.loads(before.content[0].text)["preview"]["image_side"] == "original"
            protected = await client.call_tool("run_python", {"code":
                f"from pathlib import Path; Path({page_data['images'][0]!r}).write_bytes(b'changed')"})
            assert "PermissionError" in json.loads(protected.content[0].text)["stderr"]
            invalid = await client.call_tool("check_docx", {**paths, "preview_id": "../escape", "preview_pages": [1]})
            assert invalid.is_error
            document.add_paragraph("Changed after the preview")
            document.save(candidate)
            stale = await client.call_tool("check_docx", {**paths, "preview_id": data["preview_id"], "preview_pages": [1]})
            assert stale.is_error
            assert "changed since this preview" in stale.content[0].text

    asyncio.run(exercise())
