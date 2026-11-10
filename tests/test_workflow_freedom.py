# SPDX-License-Identifier: Apache-2.0
"""Requested history changes are observations; choosing diagnostics never gates saving."""

import asyncio
import json
import os
import sys

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client
from test_checks import check_docx, comments, docx, para, rev, run


@pytest.mark.parametrize("diagnose", [False, True])
def test_clean_copy_and_comment_removal_save_as_over_mcp(tmp_path, diagnose):
    source = docx(tmp_path / "source.docx",
        [para('<w:commentRangeStart w:id="0"/>', rev("ins", "1", "Other", run("Agreed clause")),
              '<w:commentRangeEnd w:id="0"/>', '<w:r><w:commentReference w:id="0"/></w:r>')],
        comments('<w:comment w:id="0" w:author="Other"><w:p>' + run("Old question") + '</w:p></w:comment>'))
    source_bytes = source.read_bytes()
    clean = docx(tmp_path / "expected.docx", [para(run("Agreed clause"))]).read_bytes()
    output_dir = tmp_path / "another folder"
    output_dir.mkdir()
    target = output_dir / "clean copy.docx"

    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "veqtor_workspace.server", "--state-dir", str(tmp_path / "state"),
                  "--author", "Ilya", "--python", os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)])
        async with Client(stdio_client(params)) as client:
            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                data = result.structured_content or json.loads(result.content[0].text)
                return data.get("result", data)

            task = await call("document_task", {"action": "start", "file": str(source)})
            result = await call("run_python", {"task_id": task["task_id"],
                "code": f"from pathlib import Path; Path({task['candidate']!r}).write_bytes({clean!r})"})
            assert result["exit_code"] == 0
            if diagnose:
                report = await call("check_docx", {"task_id": task["task_id"]})
                assert report["status"] == "diagnostic" and not report["summary"]["errors"]
                assert {"existing_revisions_preserved", "existing_comments_preserved"} <= set(report["summary"]["observations"])
                assert "defects" not in report["summary"]
            saved = await call("document_task", {"action": "save", "task_id": task["task_id"], "file": str(target)})
            assert saved["status"] == "saved" and saved["file"] == str(target)
            assert target.read_bytes() == clean and source.read_bytes() == source_bytes
            status = await call("document_task", {"action": "status", "task_id": task["task_id"]})
            assert status["status"] == "saved" and status["file"] == str(target)

    asyncio.run(exercise())


def test_unreadable_package_is_a_diagnostic_error(tmp_path):
    source = docx(tmp_path / "source.docx", [para(run("Readable"))])
    target = tmp_path / "broken.docx"
    target.write_bytes(b"not a ZIP document")
    result = check_docx(source, target, "Ilya")
    assert result["status"] == "error"
    assert result["summary"]["errors"] == ["package_and_xml"]
    assert result["summary"]["observations"] == []
