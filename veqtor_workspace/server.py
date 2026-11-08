# SPDX-License-Identifier: Apache-2.0
"""The connected model writes code; the server handles access, snapshots and checks."""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from veqtor_workspace.operations import Operations
from veqtor_workspace.runner import DEFAULT_CALL_SECONDS, Workspace


def build_server(workspace: Workspace) -> MCPServer:
    operations = Operations(workspace)
    server = MCPServer("veqtor-workspace", version="0.0.6", instructions=(
        "Experimental local document workspace. Start with workspace_info and follow its included recipe_text. "
        "Write your own Python. In project mode start each new user editing task with document_task(action='start', file=...). "
        "Use its task_id in run_python/check_docx; document_task(action='save', task_id=...) saves back to the same file. "
        "Project files and inputs/ are read-only to Python; edit in work/. "
        "Files persist, Python variables do not. No network or personal environment is available. "
        "Use the configured author for genuine Word revisions/comments. Call check_docx on DOCX results; "
        "use render=True for a bounded preview when needed. Its limits are not legal or native Word acceptance. "
        "Calls have a connection-level deadline and no queue. If saving is interrupted, read document_task(action='status') "
        "before assuming success or failure. Reuse the user's existing authorization."
    ))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def workspace_info() -> dict:
        """Return protected inputs, outputs, author and the full editing recipe; no separate file read needed."""
        return await operations.call("workspace_info", {})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def run_python(code: str, timeout_seconds: float = workspace.call_timeout_seconds,
                         task_id: str | None = None) -> dict:
        """Run model-written Python against the selected copies, with no network. Files persist between calls.

        Pass task_id from document_task(start) to use that task's current directory:
        inputs/ (protected snapshot) and work/ (editable result). The project is readable.
        The connection sets the whole-call ceiling (normally 270 seconds). You may
        choose a shorter positive timeout; you cannot disable or exceed that ceiling.
        Cancellation stops the call's process group. Busy calls fail immediately, without a queue.
        Return concise evidence with print(); full stdout/stderr and the script are retained in .runs/.
        A successful run is not document validation. Inspect outputs and use check_docx for DOCX.
        """
        return await operations.call("run_python", {"code": code, "timeout_seconds": timeout_seconds, "task_id": task_id})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def document_task(action: Literal["start", "save", "status"], file: str | None = None,
                            task_id: str | None = None) -> dict:
        """Start a new editing task or save its result back to the same project file.

        start(file=...): snapshot the latest saved file, return task_id and a writable
        candidate. Start once per NEW user task, not once per Python call. User edits
        saved in Word between tasks become the next baseline automatically.
        save(task_id=...): atomically replace the selected file, only if it still matches
        the snapshot. Review the candidate first. A conflict preserves the newer file;
        start again from it. This cannot see unsaved Word edits or merge simultaneous writes.
        Successful saves are not undone by later errors. Snapshots stay in .veqtor/.
        status(task_id=...): read the outcome without saving. Use after any interrupted
        save: a missing reply does not prove that the document was not replaced.
        A save intent closes editing even if its final state write fails; start a fresh task.
        """
        return await operations.call("document_task", {"action": action, "file": file, "task_id": task_id})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def check_docx(original: str | None = None, candidate: str | None = None, render: bool = False,
                   preview_timeout_seconds: float = min(60, workspace.call_timeout_seconds), preview_id: str | None = None,
                   preview_pages: list[int] | None = None,
                   preview_side: Literal["original", "candidate"] = "candidate",
                   task_id: str | None = None) -> CallToolResult:
        """Check a work/ DOCX candidate against its protected inputs/ original.

        Pass task_id alone for the snapshot and result of the current task. In legacy
        copy mode provide original and candidate paths instead.
        Undoes the new tracked changes and new comments, then compares the whole document with the
        original: untracked text (including formulas and embedded data), formatting, paragraph,
        table and comment changes are reported with their location. Also checks new revision
        authors, dates, ids and structure, preserved existing revisions and comments, and
        unexpected package changes. The summary separates defects, compatibility warnings and
        checks not performed. Not a Word-opening, layout or legal check; deliberate
        accept/reject/delete needs separate review. With render=True, also render both files
        once within a shared preview deadline (default 60 seconds). The entire check,
        including rendering, stays within the connection's call ceiling (normally 270 seconds).
        All pages are rendered; up to three images are returned per reply. For later pages,
        pass preview_id and preview_pages without render=True to reuse the saved preview;
        preview_side can select the original. Reuse is refused if either document changed.
        Rendering is not
        visual approval and does not test Word comments or accept/reject. No automatic retry.
        """
        reply = await operations.call("check_docx", {
            "original": original, "candidate": candidate, "render": render, "task_id": task_id,
            "preview_timeout_seconds": preview_timeout_seconds, "preview_id": preview_id,
            "preview_pages": preview_pages, "preview_side": preview_side})
        result = reply.get("report", reply)
        images = [ImageContent(type="image", mimeType="image/png", data=data) for data in reply.get("images", [])]
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)), *images],
                              structuredContent=result)

    return server


def main():
    parser = argparse.ArgumentParser(description="Local adaptive document workspace (macOS prototype)")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--project", type=Path, help="Document folder; private snapshots are kept in .veqtor/")
    location.add_argument("--workspace", type=Path, help="Legacy inputs/work copy workspace")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--author", required=True)
    parser.add_argument("--call-timeout-seconds", type=float, default=DEFAULT_CALL_SECONDS,
                        help="Whole-call ceiling; set below the MCP client's timeout")
    args = parser.parse_args()
    project = args.project.expanduser().resolve(strict=True) if args.project else None
    root = project / ".veqtor" if project else args.workspace
    build_server(Workspace(root, args.python, args.author, project, args.call_timeout_seconds)).run(transport="stdio")


if __name__ == "__main__":
    main()
