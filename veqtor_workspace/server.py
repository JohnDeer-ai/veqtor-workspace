# SPDX-License-Identifier: Apache-2.0
"""Three tools; the connected model writes the document-processing code."""

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from veqtor_workspace.checks import check_docx as check
from veqtor_workspace.runner import Workspace
from veqtor_workspace.preview import read_preview, render_pair, select_pages


def build_server(workspace: Workspace) -> MCPServer:
    server = MCPServer("veqtor-workspace", instructions=(
        "Experimental local document workspace. Start with workspace_info and follow its included recipe_text. "
        "Write your own Python to inspect and edit selected copies. inputs/ is read-only; save in work/. "
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
    def run_python(code: str, timeout_seconds: int = 30) -> dict:
        """Run model-written Python against the selected copies, with no network. Files persist between calls.

        The current directory contains inputs/ (read-only) and work/ (writable).
        Return concise evidence with print(); full stdout/stderr and the script are retained in .runs/.
        A successful run is not document validation. Inspect outputs and use check_docx for DOCX.
        """
        return workspace.run(code, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    def check_docx(original: str, candidate: str, render: bool = False,
                   preview_timeout_seconds: int = 60, preview_id: str | None = None,
                   preview_pages: list[int] | None = None,
                   preview_side: Literal["original", "candidate"] = "candidate") -> CallToolResult:
        """Check a work/ DOCX candidate against its protected inputs/ original.

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
            source, target = workspace.path(original, "inputs"), workspace.path(candidate, "work")
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
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--author", required=True)
    args = parser.parse_args()
    build_server(Workspace(args.workspace, args.python, args.author)).run(transport="stdio")


if __name__ == "__main__":
    main()
