# SPDX-License-Identifier: Apache-2.0
"""One cancellable process group per MCP call. No queue, jobs or background service."""

import asyncio
import base64
import json
import math
import os
import signal
import sys
import threading
from pathlib import Path

import anyio

from veqtor_workspace.checks import check_docx
from veqtor_workspace.files import atomic_save
from veqtor_workspace.preview import read_preview, render_pair, select_pages
from veqtor_workspace.runner import Workspace
from veqtor_workspace.tasks import DocumentTasks


class Operations:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def call(self, operation: str, arguments: dict) -> dict:
        w = self.workspace
        if not w.lock.acquire(blocking=False):
            raise ValueError("Workspace busy. This call was refused immediately; it was not queued and will not run later.")
        process = None
        try:
            payload = {"root": str(w.root), "python": str(w.python), "author": w.author,
                       "project": str(w.project) if w.project else None,
                       "call_timeout_seconds": w.call_timeout_seconds,
                       "dynamic": w.dynamic, "read_paths": [str(p) for p in w.read_paths],
                       "preview_config": str(w.preview_config),
                       "operation": operation, "arguments": arguments}
            with anyio.fail_after(w.call_timeout_seconds):
                # Assign the child before accepting cancellation, so cleanup always knows its pid.
                with anyio.CancelScope(shield=True):
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-B", "-m", "veqtor_workspace.operations",
                        cwd=w.package, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                await process.stdin.drain()
                # Keep stdin open. Its EOF also kills the worker group if this server crashes.
                out, err = await asyncio.gather(process.stdout.read(), process.stderr.read())
                await process.wait()
            if process.returncode:
                raise ValueError(f"Operation worker stopped ({process.returncode}): {err[:2000].decode(errors='replace')}")
            reply = json.loads(out)
            if "error" in reply:
                raise ValueError(reply["error"])
            return reply["result"]
        except TimeoutError:
            return {"status": "timed_out", "timed_out": True,
                    "detail": "The whole call reached the connection deadline and was stopped. "
                              "For an interrupted save, call document_task(action='status') before deciding its outcome."}
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    if process is not None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.stdin.close()
                        # Cancellation has stopped the original readers. Drain the pipes
                        # before wait(), otherwise a full StreamReader can keep wait stuck
                        # even after SIGKILL. Cleanup has its own bounded shield.
                        with anyio.move_on_after(2):
                            await asyncio.gather(process.stdout.read(), process.stderr.read())
                            await process.wait()
            finally:
                w.lock.release()


def perform(w: Workspace, operation: str, args: dict) -> dict:
    tasks = DocumentTasks(w)
    if operation == "workspace_info":
        return w.info()
    if operation == "document_task":
        action, file, task_id = args["action"], args.get("file"), args.get("task_id")
        if action == "start" and file and task_id is None:
            return tasks.start(file, args.get("read_paths"))
        if action in ("save", "status") and task_id and file is None:
            if args.get("read_paths"):
                raise ValueError("read_paths belongs to start or run_python, not save/status.")
            return tasks.save(task_id) if action == "save" else tasks.status(task_id)
        raise ValueError("Use start with file, or save/status with task_id.")
    if operation == "run_python":
        task_id = args.pop("task_id", None)
        context = tasks.context(task_id, writing=True)[0] if task_id else w
        extra_reads = context.resolve_reads(args.pop("read_paths", None) or [])
        context.read_paths = list(dict.fromkeys([*context.read_paths, *extra_reads]))
        return context.run(**args)
    if operation != "check_docx":
        raise ValueError("Unknown operation.")

    original, candidate, task_id = args["original"], args["candidate"], args["task_id"]
    context = w
    if task_id:
        if original is not None or candidate is not None:
            raise ValueError("Use task_id or original/candidate paths, not both.")
        context, task = tasks.context(task_id)
        original, candidate = task["original"], task["candidate"]
    if original is None or candidate is None:
        raise ValueError("Provide task_id or both original and candidate paths.")
    source, target = context.path(original, "inputs"), context.path(candidate, "work")
    render, preview_id, pages = args["render"], args["preview_id"], args["preview_pages"]
    seconds = args["preview_timeout_seconds"]
    if render and (not math.isfinite(seconds) or not 0 < seconds <= w.call_timeout_seconds):
        raise ValueError("The preview deadline must be positive and within the connection's call limit.")
    if render and preview_id:
        raise ValueError("Choose render=True for new images or preview_id to reuse them, not both.")
    if pages is not None and not (render or preview_id):
        raise ValueError("Provide render=True or a preview_id when requesting pages.")
    changes_start, query = args.get("changes_start", 0), args.get("preview_query")
    if query is not None and not (render or preview_id):
        raise ValueError("Provide render=True or preview_id for preview_query.")
    images, preview = [], None
    reused_check = False
    # Validate the protected preview's hashes BEFORE reading its cached check.
    # Legacy previews and other checker versions are checked once again.
    if preview_id:
        preview = read_preview(w.root / ".runs/previews", preview_id, source, target)
        receipt = Path(preview["directory"]) / "check.json"
        try:
            cached = json.loads(receipt.read_text())
            result = cached["report"]
            reused_check = (cached["version"] == "0.0.11" and cached["author"] == w.author
                            and cached["changes_start"] == changes_start
                            and result["original_sha256"] == preview["files"]["original"]["sha256"]
                            and result["candidate_sha256"] == preview["files"]["candidate"]["sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if not reused_check:
        result = check_docx(source, target, w.author, changes_start)
    if render or preview_id:
        preview_root = w.root / ".runs/previews"
        if preview is None:
            preview = render_pair(source, target, preview_root, w.preview_config, seconds,
                                  managed_process_group=w.managed_process_group)
        if (preview["status"] == "rendered" and not reused_check
                and result.get("original_sha256") == preview["files"]["original"]["sha256"]
                and result.get("candidate_sha256") == preview["files"]["candidate"]["sha256"]):
            with atomic_save(Path(preview["directory"]) / "check.json") as temporary:
                temporary.write_text(json.dumps({"version": "0.0.11", "author": w.author,
                                                 "changes_start": changes_start, "report": result}))
        if reused_check:
            result = {key: result[key] for key in ("status", "summary", "original_sha256", "candidate_sha256")}
            result["check_reused"] = True
        result["preview"] = select_pages(preview, pages, args["preview_side"], query)
        images = [base64.b64encode(Path(p).read_bytes()).decode() for p in result["preview"]["images"]]
    return {"report": result, "images": images}


def main():
    payload = json.loads(sys.stdin.buffer.readline())

    def parent_disconnected():
        while os.read(0, 1):
            pass
        os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=parent_disconnected, daemon=True).start()
    try:
        w = Workspace(Path(payload["root"]), Path(payload["python"]), payload["author"],
                      Path(payload["project"]) if payload["project"] else None,
                      payload["call_timeout_seconds"], managed_process_group=True,
                      dynamic=payload.get("dynamic", False), read_paths=payload.get("read_paths"),
                      preview_config=Path(payload["preview_config"]) if payload.get("preview_config") else None)
        reply = {"result": perform(w, payload["operation"], payload["arguments"])}
    except Exception as exc:  # noqa: BLE001 - serialize worker failures across the process boundary
        reply = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
