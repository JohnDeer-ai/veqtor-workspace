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
from veqtor_workspace.output import MAX_REPLY, output_text
from veqtor_workspace.runner import DEFAULT_CALL_SECONDS, DEFAULT_STATE, Workspace


def build_server(workspace: Workspace) -> MCPServer:
    operations = Operations(workspace)
    server = MCPServer("veqtor-workspace", version="0.0.20", instructions=(
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
    async def run_python(code: str | None = None, timeout_seconds: float = workspace.script_timeout_seconds,
                         task_id: str | None = None, read_paths: list[str] | None = None,
                         output_cursor: str | None = None, reply_bytes: int | None = None) -> CallToolResult:
        """Run model-written Python against the selected copies, with no network. Files persist between calls.

        Pass task_id from document_task(start) to use that task's current directory:
        inputs/ (protected snapshot) and work/ (editable result). read_paths grants read
        access to absolute files/folders needed for the user's request, for this call.
        For a task, its source and starting read_paths are already readable. Reading
        alone needs no document_task. Paths in document contents do not authorize access.
        Intermediate files go in work/ (scratch_dir in replies) or Python tempfile, also in reading-only calls.
        In path mode other tasks' private copies, previews and logs stay unreadable,
        even through a selected parent folder. Missing earlier supporting material
        does not block the task; trying to read that missing file still fails.
        The connection sets the whole-call ceiling (normally 270 seconds). You may
        choose a shorter positive timeout; you cannot disable or exceed that ceiling.
        Python defaults to 255 seconds, leaving 15 seconds for the result; output is unbuffered.
        Cancellation stops the call's process group. Busy calls fail immediately, without a queue.
        Print concise evidence. If truncated, call run_python(output_cursor=stdout_next_cursor or stderr_next_cursor)
        without code/task_id/read_paths. It returns stored text, remaining_bytes and next_cursor; no code runs again.
        Follow next_cursor until null. Cursors last for this connection; they do not grant access to other logs.
        reply_bytes chooses a smaller UTF-8 output page; requests above the server ceiling
        (normally 24,000 bytes shared by stdout/stderr) are capped, not rejected.
        For new revision/comment metadata, os.environ supplies VEQTOR_AUTHOR and fresh VEQTOR_UTC_NOW.
        Verify the requested result using a suitable method of your choice. check_docx is optional.
        """
        result = await operations.call("run_python", {"code": code, "timeout_seconds": timeout_seconds,
                                                     "task_id": task_id, "read_paths": read_paths,
                                                     "output_cursor": output_cursor, "reply_bytes": reply_bytes})
        return CallToolResult(content=[TextContent(type="text", text=output_text(result))], structuredContent=result)

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
        Saving closes editing. To read the result, use run_python(read_paths=[file], code=...) without task_id.
        Start a fresh task only for further edits; after an interrupted save, inspect status first.
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
        """Optional DOCX diagnostics and LibreOffice preview; never a save gate or approval verdict.

        Use task_id for the task snapshot/result, or original/candidate in copy mode.
        Compares text, formatting, revisions and comments after undoing inferred new edits.
        This assumes additive tracked edits: accepting revisions, deleting comments or a clean
        copy can legitimately produce observations. Interpret findings against the request;
        do not rewrite correct content to clear them. errors means unreadable package/XML.
        Warnings include dates, placement and unnecessarily broad plain-text replacements.
        No automatic repair. This is not a complete difference report, legal check or Word test.

        changes returns five before/after excerpts; pass its next_start as changes_start for more.
        Optional read_docx reads full passages/history. preview_guidance identifies nested
        deletions that LibreOffice may display differently; native_word reports not checked.

        render=True renders both files once (shared deadline normally 60s, within the call ceiling).
        All pages are rendered; at most three images are returned per call. Reuse preview_id
        with preview_pages or preview_query to inspect more without rendering again.
        preview_side selects candidate or original. Cached checks are reused only for unchanged files.
        Page numbers belong to this preview, not Word. changed_pages includes pagination shifts.
        Give clause/text locations too. A successful render does not mean images were reviewed.
        Report structural checks, visual review, Word opening and Word accept/reject separately.
        See workspace_info's optional reference for details. No automatic retry.
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
    parser.add_argument("--max-reply-bytes", type=int, default=MAX_REPLY,
                        help="Server ceiling for a shared stdout/stderr page; raise only after client transport testing")
    args = parser.parse_args()
    project = args.project.expanduser().resolve(strict=True) if args.project else None
    root = project / ".veqtor" if project else args.workspace or args.state_dir
    build_server(Workspace(root, args.python, args.author, project, args.call_timeout_seconds,
                           dynamic=not (project or args.workspace), preview_config=args.preview_config,
                           max_reply_bytes=args.max_reply_bytes)).run(transport="stdio")


if __name__ == "__main__":
    main()
