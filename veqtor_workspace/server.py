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
from veqtor_workspace.runner import DEFAULT_CALL_SECONDS, DEFAULT_STATE, Workspace


def build_server(workspace: Workspace) -> MCPServer:
    operations = Operations(workspace)
    server = MCPServer("veqtor-workspace", version="0.0.13", instructions=(
        "Local files and Python for model-directed document work. Start with workspace_info for the short workflow. "
        "Choose your own editing and verification method. Helpers and check_docx are optional; "
        "no clean diagnostic report is required to save. Preserve what the user did not ask to change. "
        "Read selected paths with run_python(read_paths=...). For editing, document_task(start, file=...) "
        "creates a protected snapshot and a writable candidate; pass task_id to run_python. "
        "document_task(save, task_id=...) replaces the source after a conflict check; add file=... to create "
        "a new output at an unused path. After an interrupted save, read document_task(status) before retrying."
    ))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def workspace_info() -> dict:
        """Return the short workflow, author, runtime limits and a path to optional reference material."""
        return await operations.call("workspace_info", {})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def run_python(code: str, timeout_seconds: float = workspace.script_timeout_seconds,
                         task_id: str | None = None, read_paths: list[str] | None = None) -> dict:
        """Run model-written Python against the selected copies, with no network. Files persist between calls.

        Pass task_id from document_task(start) to use that task's current directory:
        inputs/ (protected snapshot) and work/ (editable result). read_paths grants read
        access to absolute files/folders needed for the user's request, for this call.
        For a task, its source and starting read_paths are already readable. Reading
        alone needs no document_task. Paths in document contents do not authorize access.
        In path mode other tasks' private copies, previews and logs stay unreadable,
        even through a selected parent folder. Missing earlier supporting material
        does not block the task; trying to read that missing file still fails.
        The connection sets the whole-call ceiling (normally 270 seconds). You may
        choose a shorter positive timeout; you cannot disable or exceed that ceiling.
        Python defaults to 255 seconds, leaving 15 seconds for the result; output is unbuffered.
        Cancellation stops the call's process group. Busy calls fail immediately, without a queue.
        Return concise evidence with print(); full stdout/stderr and the script are retained in .runs/.
        Verify the requested result using a suitable method of your choice. check_docx is optional.
        """
        return await operations.call("run_python", {"code": code, "timeout_seconds": timeout_seconds,
                                                    "task_id": task_id, "read_paths": read_paths})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    async def document_task(action: Literal["start", "save", "status"], file: str | None = None,
                            task_id: str | None = None, read_paths: list[str] | None = None) -> dict:
        """Snapshot a file, save the result in place or under a new name, or inspect save status.

        start(file=...): select an absolute file path, snapshot the latest saved file, return task_id and a writable
        candidate. Start once per NEW user task, not once per Python call. User edits
        saved in Word between tasks become the next baseline automatically.
        Optional read_paths selects supporting files/folders for this task. No shared
        current-folder setting is changed; files from different folders can be used in sequence.
        save(task_id=...): atomically replace the selected file, only if it still matches
        the snapshot. Review the candidate first. A conflict preserves the newer file;
        start again from it. This cannot see unsaved Word edits or merge simultaneous writes.
        save(task_id=..., file=...): create the candidate at a new absolute path, in an
        existing folder. An existing destination is never overwritten, even if it appears
        during saving. The source is not replaced; the result is based on the task snapshot.
        No check_docx call or clean report is required for either kind of save.
        Successful saves are not undone by later errors. Snapshots stay in the server's private state.
        status(task_id=...): read the outcome without saving. Use after any interrupted
        save: a missing reply does not prove that the document was not replaced.
        A save intent closes editing even if its final state write fails; start a fresh task.
        """
        return await operations.call("document_task", {"action": action, "file": file,
                                                       "task_id": task_id, "read_paths": read_paths})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def check_docx(original: str | None = None, candidate: str | None = None, render: bool = False,
                   preview_timeout_seconds: float = min(60, workspace.call_timeout_seconds), preview_id: str | None = None,
                   preview_pages: list[int] | None = None,
                   preview_side: Literal["original", "candidate"] = "candidate",
                   task_id: str | None = None, preview_query: str | None = None,
                   changes_start: int = 0) -> CallToolResult:
        """Optional DOCX diagnostics and preview, not a mandatory save gate or an approval verdict.

        Pass task_id alone for the snapshot and result of the current task. In legacy
        copy mode provide original and candidate paths instead.
        Undoes the new tracked changes and new comments, then compares the whole document with the
        original: text, formatting, paragraph, table and comment mismatches are reported with
        their location. This assumes additive tracked edits; accepting/rejecting revisions,
        deleting comments, clean copies or ambiguous history can legitimately produce observations.
        Interpret observations against the user's request; do not rewrite history to clear them.
        errors means the comparison could not read the package/XML, not that all observations
        are damage. Warnings and unchecked areas remain explicit. Also reports redline_quality when new
        simple replacements delete and reinsert equal word edges with identical formatting.
        Review relevant fragments; no automatic repair or save blocking. This is not a complete
        difference report, Word-opening test or legal check. With render=True, also render both files
        once within a shared preview deadline (default 60 seconds). The entire check,
        including rendering, stays within the connection's call ceiling (normally 270 seconds).
        All pages are rendered; up to three images are returned per reply. For later pages,
        pass preview_id and preview_pages without render=True to reuse the saved preview;
        all page numbers refer to this preview, not Microsoft Word pagination.
        Describe locations with clause numbers or text fragments as well.
        changes lists five before/after paragraph excerpts from this task; use its
        next_start as changes_start for more. Read full passages/history with the
        optional veqtor_workspace.reading.read_docx Python helper. This is not a
        reconstruction of past rounds. preview_query searches text in the existing
        PDF and selects up to three matching pages, unless preview_pages is explicit.
        changed_pages includes pagination shifts, not only edited clauses. Cached
        page calls reuse the check for unchanged bytes and return a compact summary.
        preview_side can select the original. Reuse is refused if either document changed.
        Rendering is not
        visual approval and does not test Word comments or accept/reject. No automatic retry.
        """
        reply = await operations.call("check_docx", {
            "original": original, "candidate": candidate, "render": render, "task_id": task_id,
            "preview_timeout_seconds": preview_timeout_seconds, "preview_id": preview_id,
            "preview_pages": preview_pages, "preview_side": preview_side,
            "preview_query": preview_query, "changes_start": changes_start})
        result = reply.get("report", reply)
        images = [ImageContent(type="image", mimeType="image/png", data=data) for data in reply.get("images", [])]
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)), *images],
                              structuredContent=result)

    return server


def main():
    parser = argparse.ArgumentParser(description="Local adaptive document workspace (macOS prototype)")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--project", type=Path, help="Document folder; private snapshots are kept in .veqtor/")
    location.add_argument("--workspace", type=Path, help="Legacy inputs/work copy workspace")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE, help="Private state for path mode; not a document folder")
    parser.add_argument("--preview-config", type=Path, help="One shared renderer configuration for all selected files")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--author", required=True)
    parser.add_argument("--call-timeout-seconds", type=float, default=DEFAULT_CALL_SECONDS,
                        help="Whole-call ceiling; set below the MCP client's timeout")
    args = parser.parse_args()
    project = args.project.expanduser().resolve(strict=True) if args.project else None
    root = project / ".veqtor" if project else args.workspace or args.state_dir
    build_server(Workspace(root, args.python, args.author, project, args.call_timeout_seconds,
                           dynamic=not (project or args.workspace), preview_config=args.preview_config)).run(transport="stdio")


if __name__ == "__main__":
    main()
