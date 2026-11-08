# SPDX-License-Identifier: Apache-2.0
"""The connected model writes code; the server handles access, snapshots and checks."""

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from veqtor_workspace.checks import check_docx as check
from veqtor_workspace.preview import read_preview, render_pair, select_pages
from veqtor_workspace.runner import Workspace
from veqtor_workspace.tasks import DocumentTasks


def build_server(workspace: Workspace) -> MCPServer:
    tasks = DocumentTasks(workspace)
    server = MCPServer("veqtor-workspace", instructions=(
        "Experimental local document workspace. Start with workspace_info and follow its included recipe_text. "
        "Write your own Python. In project mode start each new user editing task with document_task(action='start', file=...). "
        "Use its task_id in run_python/check_docx; document_task(action='save', task_id=...) saves back to the same file. "
        "Project files and inputs/ are read-only to Python; edit in work/. "
        "Files persist, Python variables do not. No network or personal environment is available. "
        "Use the configured author for genuine Word revisions/comments. Call check_docx on DOCX results; "
        "use render=True for a bounded preview when needed. Its limits are not legal or native Word acceptance. "
        "Reuse the user's existing authorization."
    ))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def workspace_info() -> dict:
        """Return protected inputs, outputs, author and the full editing recipe; no separate file read needed."""
        return workspace.info()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    def run_python(code: str, timeout_seconds: float | None = 300, task_id: str | None = None) -> dict:
        """Run model-written Python against the selected copies, with no network. Files persist between calls.

        Pass task_id from document_task(start) to use that task's current directory:
        inputs/ (protected snapshot) and work/ (editable result). The project is readable.
        The default deadline is 300 seconds; any positive value is allowed, or null to
        disable the server deadline. The MCP client may have its own timeout.
        Return concise evidence with print(); full stdout/stderr and the script are retained in .runs/.
        A successful run is not document validation. Inspect outputs and use check_docx for DOCX.
        """
        if task_id is None:
            return workspace.run(code, timeout_seconds)
        with workspace.lock:
            context, _ = tasks.context(task_id, writing=True)
            return context.run(code, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    def document_task(action: Literal["start", "save"], file: str | None = None,
                      task_id: str | None = None) -> dict:
        """Start a new editing task or save its result back to the same project file.

        start(file=...): snapshot the latest saved file, return task_id and a writable
        candidate. Start once per NEW user task, not once per Python call. User edits
        saved in Word between tasks become the next baseline automatically.
        save(task_id=...): atomically replace the selected file, only if it still matches
        the snapshot. Review the candidate first. A conflict preserves the newer file;
        start again from it. This cannot see unsaved Word edits or merge simultaneous writes.
        Successful saves are not undone by later errors. Snapshots stay in .veqtor/.
        """
        with workspace.lock:
            if action == "start" and file and task_id is None:
                return tasks.start(file)
            if action == "save" and task_id and file is None:
                return tasks.save(task_id)
            raise ValueError("Use start with file, or save with task_id.")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    def check_docx(original: str | None = None, candidate: str | None = None, render: bool = False,
                   preview_timeout_seconds: int = 60, preview_id: str | None = None,
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
        once within a shared deadline (default 60 seconds, configurable up to 300).
        All pages are rendered; up to three images are returned per reply. For later pages,
        pass preview_id and preview_pages without render=True to reuse the saved preview;
        preview_side can select the original. Reuse is refused if either document changed.
        Rendering is not
        visual approval and does not test Word comments or accept/reject. No automatic retry.
        """
        with workspace.lock:
            context = workspace
            if task_id:
                if original is not None or candidate is not None:
                    raise ValueError("Use task_id or original/candidate paths, not both.")
                context, task = tasks.context(task_id)
                original, candidate = task["original"], task["candidate"]
            if original is None or candidate is None:
                raise ValueError("Provide task_id or both original and candidate paths.")
            source, target = context.path(original, "inputs"), context.path(candidate, "work")
            result = check(source, target, workspace.author)
            images = []
            if render and preview_id:
                raise ValueError("Choose render=True for new images or preview_id to reuse them, not both.")
            if preview_pages is not None and not (render or preview_id):
                raise ValueError("Provide render=True or a preview_id when requesting pages.")
            if render or preview_id:
                preview_root = workspace.root / ".runs/previews"
                preview = (read_preview(preview_root, preview_id, source, target) if preview_id else
                           render_pair(source, target, preview_root, workspace.root / ".preview.json",
                                       preview_timeout_seconds))
                result["preview"] = select_pages(preview, preview_pages, preview_side)
                images = [ImageContent(type="image", mimeType="image/png",
                                       data=base64.b64encode(Path(p).read_bytes()).decode())
                          for p in result["preview"]["images"]]
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
    args = parser.parse_args()
    project = args.project.expanduser().resolve(strict=True) if args.project else None
    root = project / ".veqtor" if project else args.workspace
    build_server(Workspace(root, args.python, args.author, project)).run(transport="stdio")


if __name__ == "__main__":
    main()
