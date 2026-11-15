# SPDX-License-Identifier: Apache-2.0
"""Bounded UTF-8 output pages and cursors issued by this server connection."""

import base64
import codecs
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path

MAX_REPLY = 24_000


def reply_limit(requested: int | None, ceiling: int = MAX_REPLY) -> int:
    """The client may request a smaller page, never raise the server ceiling."""
    if type(ceiling) is not int or ceiling < 1024:
        raise ValueError("The server reply ceiling must be an integer of at least 1024 bytes.")
    if requested is not None and (type(requested) is not int or requested <= 0):
        raise ValueError("reply_bytes must be a positive integer or omitted.")
    return min(ceiling, max(1024, requested if requested is not None else ceiling))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class OutputCursors:
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def encode(self, data: dict) -> str:
        flags = 16 | bool(data["task_id"]) | (2 if data["stream"] == "stderr" else 0)
        payload = _b64(struct.pack(">B6s6sQQ", flags, bytes.fromhex(data["run_id"]),
                                  bytes.fromhex(data["task_id"] or "0" * 12), data["offset"], data["total_bytes"]))
        return payload + "." + _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest()[:16])

    def decode(self, cursor: str) -> dict:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError
            payload, signature = cursor.split(".")
            expected = _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest()[:16])
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            flags, run, task, offset, size = struct.unpack(">B6s6sQQ", base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if flags not in range(16, 20) or offset > size:
                raise ValueError
            return {"run_id": run.hex(), "task_id": task.hex() if flags & 1 else None,
                    "stream": "stderr" if flags & 2 else "stdout", "offset": offset, "total_bytes": size}
        except (ValueError, TypeError, KeyError, struct.error):
            raise ValueError("Invalid output cursor or different server connection. Use a cursor returned by this connection; "
                             "do not repeat editing code to recover output.") from None


def read_stream(path: Path, offset: int = 0, total_bytes: int | None = None, reply_bytes: int = MAX_REPLY) -> dict:
    try:
        with path.open("rb") as stream:
            available = os.fstat(stream.fileno()).st_size
            # A cursor addresses the captured prefix, never later appended bytes.
            size = available if total_bytes is None else total_bytes
            if available < size:
                raise ValueError("Stored output was shortened; this cursor cannot continue it.")
            if not 0 <= offset <= size:
                raise ValueError("Output offset is outside the stored result.")
            stream.seek(offset)
            count = min(reply_bytes, size - offset)
            raw = stream.read(count)
            if len(raw) != count:
                raise ValueError("Stored output changed while reading; this cursor cannot continue it.")
    except FileNotFoundError:
        raise ValueError("Stored output is no longer available. Read the source or a saved scratch extract again.") from None
    while True:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=offset + len(raw) == size)
        encoded_size = len(text.encode())
        if encoded_size <= reply_bytes:
            break
        # Invalid UTF-8 expands into replacement characters. Bound the visible text too.
        raw = raw[:max(1, len(raw) * reply_bytes // encoded_size)]
    next_offset = offset + len(raw) - len(decoder.getstate()[0])
    return {"text": text, "offset_bytes": offset, "total_bytes": size,
            "remaining_bytes": size - next_offset, "_next_offset": next_offset}


def collect_output(root: Path, run_id: str, reply_bytes: int = MAX_REPLY) -> dict:
    result = {"_output_pages": {}}
    sizes = {kind: (root / ".runs" / f"{run_id}.{kind}").stat().st_size for kind in ("stdout", "stderr")}
    # Share one budget. Errors stay visible even when stdout is very large.
    error_path = root / ".runs" / f"{run_id}.stderr"
    error = read_stream(error_path, total_bytes=sizes["stderr"],
                        reply_bytes=reply_bytes // 2 if sizes["stdout"] else reply_bytes)
    out = read_stream(root / ".runs" / f"{run_id}.stdout", total_bytes=sizes["stdout"],
                      reply_bytes=reply_bytes - len(error["text"].encode()))
    if error["remaining_bytes"] and not out["remaining_bytes"]:
        error = read_stream(error_path, total_bytes=sizes["stderr"], reply_bytes=reply_bytes - len(out["text"].encode()))
    pages = {"stdout": out, "stderr": error}
    for kind in ("stdout", "stderr"):
        path = root / ".runs" / f"{run_id}.{kind}"
        page = pages[kind]
        result[kind] = page["text"]
        result[f"{kind}_truncated"] = page["remaining_bytes"] > 0
        result[f"{kind}_path"] = str(path.relative_to(root))
        if page["remaining_bytes"]:
            result["_output_pages"][kind] = {"offset": page["_next_offset"], "total_bytes": page["total_bytes"]}
    return result


def read_output(root: Path, data: dict) -> dict:
    folder = root / ".runs/documents" / data["task_id"] if data["task_id"] else root
    path = folder / ".runs" / f"{data['run_id']}.{data['stream']}"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Stored output must remain inside private state.")
    return {"run_id": data["run_id"], "stream": data["stream"],
            **read_stream(path, data["offset"], data["total_bytes"], data.get("reply_bytes", MAX_REPLY))}


def output_text(result: dict) -> str:
    """Human-readable MCP text, with intact raw streams in structuredContent."""
    if "run_id" not in result:
        return json.dumps(result, ensure_ascii=False)
    keys = ("run_id", "exit_code", "timed_out", "scratch_dir", "artifacts", "reply_bytes",
            "stream", "offset_bytes", "total_bytes", "remaining_bytes")
    lines = [json.dumps({k: result[k] for k in keys if k in result}, ensure_ascii=False, separators=(",", ":"))]
    streams = [(result["stream"], "text", "next_cursor", "remaining_bytes")] if "stream" in result else [
        (s, s, f"{s}_next_cursor", f"{s}_remaining_bytes") for s in ("stdout", "stderr")]
    for stream, field, cursor_key, remaining in streams:
        text, cursor = result.get(field, ""), result.get(cursor_key)
        if text or cursor:
            lines.extend([f"--- {stream} ---", text])
        if cursor:
            lines.append(f"[OUTPUT CONTINUES: {result[remaining]} bytes remain in {stream}. "
                         f'Call run_python(output_cursor="{cursor}"). Stored output only; code is not rerun.]')
    return "\n".join(lines)
