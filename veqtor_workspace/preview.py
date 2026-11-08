# SPDX-License-Identifier: Apache-2.0
"""Bounded previews using an administrator-configured, existing DOCX renderer.

No editing engine or Word UI automation. The same function is usable without MCP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

DEFAULT_SECONDS = 60
MAX_SECONDS = 300
MAX_IMAGES = 3


def select_pages(result: dict, pages: list[int] | None = None, side: str = "candidate") -> dict:
    if side not in ("original", "candidate"):
        raise ValueError("Preview side must be original or candidate.")
    if result["status"] != "rendered":
        return result
    count = result["files"][side]["pages"]
    if pages is None:
        pages = [n for n in result["changed_pages"] if n <= count][:MAX_IMAGES] or [1]
    if not 1 <= len(pages) <= MAX_IMAGES or any(type(n) is not int or not 1 <= n <= count for n in pages):
        raise ValueError(f"Request 1–{MAX_IMAGES} page numbers between 1 and {count}.")
    images = [str(Path(result["directory"]) / side / f"page-{n}.png") for n in pages]
    if not all(Path(p).is_file() for p in images):
        raise ValueError("Preview images are missing; render the current documents again.")
    return {**result, "images": images, "image_side": side, "shown_pages": pages,
            "unshown_changed_pages": [n for n in result["changed_pages"] if n not in pages]}


def read_preview(root: Path, preview_id: str, original: Path, candidate: Path) -> dict:
    """Reuse a protected preview only while it still matches both documents."""
    if not re.fullmatch(r"[0-9a-f]{12}", preview_id):
        raise ValueError("Invalid preview_id.")
    receipt = (root / preview_id / "preview.json").resolve()
    if not receipt.is_relative_to(root.resolve()):
        raise ValueError("Preview must be inside the protected preview directory.")
    result = json.loads(receipt.read_text())
    if result["status"] != "rendered" or Path(result["directory"]).resolve() != receipt.parent:
        raise ValueError("Preview is not a completed render.")
    for name, path in (("original", original), ("candidate", candidate)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["files"][name]["sha256"]:
            raise ValueError(f"The {name} changed since this preview; render the current documents again.")
    return {**result, "reused": True}


def render_pair(original: Path, candidate: Path, output: Path, config: Path,
                timeout_seconds: float = DEFAULT_SECONDS) -> dict:
    """Render both files once, sharing one deadline; return evidence, not visual approval."""
    if not 0 < timeout_seconds <= MAX_SECONDS:
        raise ValueError(f"Preview timeout must be between 0 and {MAX_SECONDS} seconds.")
    result = {"status": "not_available", "timeout_seconds": timeout_seconds,
              "visual_review": "not_performed", "images": [],
              "limits": "LibreOffice preview, not native Word. Review the images; comments and accept/reject are not tested."}
    try:
        settings = json.loads(config.read_text())
        python, script = (Path(settings[k]).expanduser().resolve(strict=True) for k in ("python", "script"))
        if not python.is_file() or not script.is_file():
            raise ValueError("Preview configuration must name a Python executable and renderer script.")
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return {**result, "detail": f"Preview renderer is not configured: {exc}"}

    started = time.monotonic()
    directory = output / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONUTF8": "1",
           "HOME": str(directory), "TMPDIR": "/private/tmp", "PYTHONDONTWRITEBYTECODE": "1"}
    result.update(directory=str(directory), renderer=str(script), files={})
    # This fixed converter is separate from model-written Python. It receives no
    # credentials or network access, and cannot write either input document.
    # Protect the files, not their parent: direct workflows often save previews
    # beside the source and candidate in the same folder.
    protected = ' '.join(f'(literal {json.dumps(str(p.resolve()))})' for p in (original, candidate))
    profile = f'(version 1)(allow default)(deny network*)(deny file-write* {protected})'
    try:
        for name, source in (("original", original), ("candidate", candidate)):
            target = directory / name
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            command = ["/usr/bin/sandbox-exec", "-p", profile, str(python), str(script),
                       str(source.resolve()), "--output_dir", str(target), "--emit_pdf"]
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            with (directory / f"{name}.log").open("wb") as log:
                process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
                try:
                    process.wait(timeout=remaining)
                finally:
                    # Includes descendants, even when the renderer exits before them.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            if process.returncode:
                raise RuntimeError(f"{name} renderer exited {process.returncode}; see {name}.log")
            pages = sorted(target.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
            if not pages or [p.name for p in pages] != [f"page-{i}.png" for i in range(1, len(pages) + 1)]:
                raise RuntimeError(f"{name} renderer did not produce a complete numbered page set.")
            if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                raise RuntimeError(f"{name} changed during rendering; preview does not verify the current file.")
            result["files"][name] = {"sha256": source_hash, "pages": len(pages), "directory": str(target)}
        before, after = (result["files"][key]["pages"] for key in ("original", "candidate"))
        changed = []
        for number in range(1, max(before, after) + 1):
            a, b = (directory / key / f"page-{number}.png" for key in ("original", "candidate"))
            if not a.exists() or not b.exists() or a.read_bytes() != b.read_bytes():
                changed.append(number)
        result.update(status="rendered", changed_pages=changed, preview_id=directory.name,
                      comparison="Exact PNG bytes; differences may include renderer metadata, not just content.")
        result = select_pages(result)
    except subprocess.TimeoutExpired:
        result.update(status="timed_out", detail="Shared preview deadline reached; converter processes stopped. No retry.")
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(status="failed", detail=str(exc))
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if result["status"] == "rendered":
        (directory / "preview.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("original", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_SECONDS)
    args = parser.parse_args()
    print(json.dumps(render_pair(args.original, args.candidate, args.output, args.config, args.timeout), indent=2))


if __name__ == "__main__":
    main()
