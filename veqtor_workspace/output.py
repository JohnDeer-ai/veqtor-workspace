# SPDX-License-Identifier: Apache-2.0
"""Bounded UTF-8 output pages and cursors issued by this server connection."""

import base64
import codecs
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

MAX_REPLY = 12_000


class OutputCursors:
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def encode(self, data: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        return payload + "." + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def decode(self, cursor: str) -> dict:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError
            payload, signature = cursor.split(".")
            expected = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (set(data) != {"run_id", "task_id", "stream", "offset", "total_bytes"}
                    or not re.fullmatch(r"[0-9a-f]{12}", data["run_id"])
                    or (data["task_id"] is not None and not re.fullmatch(r"[0-9a-f]{12}", data["task_id"]))
                    or data["stream"] not in ("stdout", "stderr")
                    or type(data["offset"]) is not int or type(data["total_bytes"]) is not int
                    or not 0 <= data["offset"] <= data["total_bytes"]):
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid output cursor or different server connection. Use a cursor returned by this connection; "
                             "do not repeat editing code to recover output.") from None


def read_stream(path: Path, offset: int = 0, total_bytes: int | None = None) -> dict:
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
            count = min(MAX_REPLY, size - offset)
            raw = stream.read(count)
            if len(raw) != count:
                raise ValueError("Stored output changed while reading; this cursor cannot continue it.")
    except FileNotFoundError:
        raise ValueError("Stored output is no longer available. Read the source or a saved scratch extract again.") from None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(raw, final=offset + len(raw) == size)
    next_offset = offset + len(raw) - len(decoder.getstate()[0])
    return {"text": text, "offset_bytes": offset, "total_bytes": size,
            "remaining_bytes": size - next_offset, "_next_offset": next_offset}


def collect_output(root: Path, run_id: str) -> dict:
    result = {"_output_pages": {}}
    for kind in ("stdout", "stderr"):
        path = root / ".runs" / f"{run_id}.{kind}"
        page = read_stream(path)
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
            **read_stream(path, data["offset"], data["total_bytes"])}
