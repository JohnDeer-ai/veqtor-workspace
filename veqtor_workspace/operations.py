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

from veqtor_workspace import __version__
from veqtor_workspace.checks import check_docx
from veqtor_workspace.files import atomic_save
from veqtor_workspace.output import OutputCursors, collect_output, read_output, reply_limit
from veqtor_workspace.preview import read_preview, render_pair, select_pages
from veqtor_workspace.runner import Workspace
from veqtor_workspace.tasks import DocumentTasks


CHECK_CACHE_FORMAT = 13


async def _stop_worker(process):
    with anyio.CancelScope(shield=True):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdin.close()
        # Drain before wait: cancellation may have stopped the original readers
        # with a full pipe. Cleanup has its own bounded shield.
        with anyio.move_on_after(2):
            await asyncio.gather(process.stdout.read(), process.stderr.read())
            await process.wait()


class Operations:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.output_cursors = OutputCursors()

    async def call(self, operation: str, arguments: dict) -> dict:
        w = self.workspace
        if not w.lock.acquire(blocking=False):
            raise ValueError("Workspace busy. This call was refused immediately; it was not queued and will not run later.")
        process = None
        try:
            arguments = dict(arguments)
            if operation == "run_python":
                budget = reply_limit(arguments.pop("reply_bytes", None), w.max_reply_bytes)
                cursor = arguments.pop("output_cursor", None)
                if cursor is not None:
                    if arguments.get("code") is not None or arguments.get("task_id") is not None or arguments.get("read_paths"):
                        raise ValueError("Use output_cursor alone, without code, task_id or read_paths. Continuation never executes Python.")
                    arguments = self.output_cursors.decode(cursor)
                    operation = "read_output"
                arguments["reply_bytes"] = budget
            payload = {"root": str(w.root), "python": str(w.python), "author": w.author,
                       "project": str(w.project) if w.project else None,
                       "call_timeout_seconds": w.call_timeout_seconds,
                       "max_reply_bytes": w.max_reply_bytes,
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
                if reply.get("expected"):
                    raise ValueError(reply["error"])
                raise RuntimeError(reply["error"])
            result = reply["result"]
            if operation == "run_python":
                # Descendants can still append after the main Python script exits.
                # Stop the whole call before issuing cursors for its final output.
                await _stop_worker(process)
                process = None
                root = w.root / ".runs/documents" / arguments["task_id"] if arguments.get("task_id") else w.root
                result.pop("output_hint", None)
                result.update(collect_output(root, result["run_id"], budget))
                result["reply_bytes"] = budget
                for stream, page in result.pop("_output_pages", {}).items():
                    result[f"{stream}_next_cursor"] = self.output_cursors.encode({
                        "run_id": result["run_id"], "task_id": arguments.get("task_id"), "stream": stream, **page})
                    result[f"{stream}_remaining_bytes"] = page["total_bytes"] - page["offset"]
                if any(result.get(f"{stream}_truncated") for stream in ("stdout", "stderr", "artifacts")):
                    result["output_hint"] = (
                        "Output is incomplete. Use stdout_next_cursor, stderr_next_cursor or artifacts_next_cursor "
                        "as run_python(output_cursor=...), "
                        "without code, task_id or read_paths. Then follow next_cursor until null. "
                        "This reads stored output only; never repeat editing code to recover it. Cursors last for this connection.")
            elif operation == "read_output":
                offset = result.pop("_next_offset")
                result["next_cursor"] = self.output_cursors.encode({**arguments, "offset": offset}) if result["remaining_bytes"] else None
                result["reply_bytes"] = budget
            return result
        except TimeoutError:
            return {"status": "timed_out", "timed_out": True,
                    "detail": "The whole call reached the connection deadline and was stopped. "
                              "For an interrupted save, call document_task(action='status') before deciding its outcome."}
        finally:
            try:
                if process is not None:
                    await _stop_worker(process)
            finally:
                w.lock.release()


def perform(w: Workspace, operation: str, args: dict) -> dict:
    tasks = DocumentTasks(w)
    if operation == "workspace_info":
        return w.info()
    if operation == "read_output":
        return read_output(w.root, args)
    if operation == "document_task":
        action, file, task_id = args["action"], args.get("file"), args.get("task_id")
        if action == "start" and file and task_id is None:
            return tasks.start(file, args.get("read_paths"))
        if action in ("save", "status") and task_id:
            if args.get("read_paths"):
                raise ValueError("read_paths belongs to start or run_python, not save/status.")
            if action == "status" and file is not None:
                raise ValueError("status reads the recorded destination; omit file.")
            return tasks.save(task_id, file) if action == "save" else tasks.status(task_id)
        raise ValueError("Use start with file, or save/status with task_id. save optionally accepts a new file path.")
    if operation == "run_python":
        task_id = args.pop("task_id", None)
        context = tasks.context(task_id, writing=True)[0] if task_id else w
        extra_reads = context.resolve_reads(args.pop("read_paths", None) or [])
        context.read_paths = list(dict.fromkeys([*context.read_paths, *extra_reads]))
        return context.run(**args)
    if operation != "check_docx":
        raise ValueError("Unknown operation.")

    original, candidate, task_id = args["original"], args["candidate"], args["task_id"]
    if task_id:
        if original is not None or candidate is not None:
            raise ValueError("Use task_id or original/candidate paths, not both.")
        context, task = tasks.context(task_id)
        source = context.path(task["original"], "inputs")
        target = context.path(task["candidate"], "work")
        comparison = {"mode": "task", "task_id": task_id, "task_file": task["file"]}
    else:
        if original is None or candidate is None:
            raise ValueError("Provide task_id or both original and candidate paths.")
        source = w.comparison_path(original, "inputs")
        target = w.comparison_path(candidate, "work")
        comparison = {"mode": "paths"}
    comparison.update(original=str(source), candidate=str(target))
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
    # Validate the protected preview against the file pair before reading its cached check.
    if preview_id:
        preview = read_preview(w.root / ".runs/previews", preview_id, source, target, require_paths=w.dynamic)
        receipt = Path(preview["directory"]) / "check.json"
        try:
            cached = json.loads(receipt.read_text())
            result = cached["report"]
            reused_check = (cached.get("format") == CHECK_CACHE_FORMAT
                            and cached["version"] == __version__ and cached["author"] == w.author
                            and cached["changes_start"] == changes_start
                            and "comment_facts" in result
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
        if preview["status"] == "rendered" and result["status"] == "diagnostic" and not reused_check:
            if any(result[f"{name}_sha256"] != preview["files"][name]["sha256"]
                   for name in ("original", "candidate")):
                raise ValueError("A selected DOCX changed between comparison and preview. Compare the current files again.")
            with atomic_save(Path(preview["directory"]) / "check.json") as temporary:
                temporary.write_text(json.dumps({"format": CHECK_CACHE_FORMAT, "version": __version__, "author": w.author,
                                                 "changes_start": changes_start, "report": result}))
        if reused_check:
            fields = ("status", "summary", "original_sha256", "candidate_sha256",
                      "checked_at_utc", "native_word", "preview_guidance")
            if changes_start:
                fields += ("changes",)
            fields += tuple(key for key in ("unchecked_story_xml", "unchecked_auxiliary_xml") if key in result)
            facts = result["comment_facts"]
            result = {key: result[key] for key in fields}
            result["comment_facts"] = {**facts, **{side: {k: v for k, v in facts[side].items() if k != "items"}
                                                  for side in ("original", "candidate")}}
            result["check_reused"] = True
        result["preview"] = select_pages(preview, pages, args["preview_side"], query)
        images = [base64.b64encode(Path(p).read_bytes()).decode() for p in result["preview"]["images"]]
    report = {"status": result["status"], "comparison": comparison, "summary": result["summary"]}
    if "changes" in result:
        report["changes"] = result["changes"]
    return {"report": {**report, **result}, "images": images}


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
                      preview_config=Path(payload["preview_config"]) if payload.get("preview_config") else None,
                      max_reply_bytes=payload.get("max_reply_bytes", 24_000))
        reply = {"result": perform(w, payload["operation"], payload["arguments"])}
    except Exception as exc:  # noqa: BLE001 - serialize worker failures across the process boundary
        reply = {"error": f"{type(exc).__name__}: {exc}", "expected": isinstance(exc, (ValueError, OSError))}
    print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
