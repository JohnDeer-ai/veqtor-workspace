# SPDX-License-Identifier: Apache-2.0
"""Small macOS-only Python runner with OS-enforced workspace access."""

from __future__ import annotations

import json
import math
import os
import platform
import signal
import subprocess
import threading
import uuid
from pathlib import Path

MAX_REPLY = 12_000
DEFAULT_CALL_SECONDS = 270


def _quoted(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


class Workspace:
    def __init__(self, root: Path, python: Path, author: str, project: Path | None = None,
                 call_timeout_seconds: float = DEFAULT_CALL_SECONDS, managed_process_group: bool = False):
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise RuntimeError("This prototype requires macOS sandbox-exec; no unrestricted fallback.")
        self.root = root.expanduser().resolve()
        self.project = project.expanduser().resolve(strict=True) if project else None
        if self.project is not None and not self.project.is_dir():
            raise ValueError("The selected project must be a directory.")
        if self.project and (self.root == self.project or not self.root.is_relative_to(self.project)):
            raise ValueError("Private state must be inside the selected project folder.")
        self.python = python.expanduser().absolute()
        if not math.isfinite(call_timeout_seconds) or call_timeout_seconds <= 0:
            raise ValueError("The connection's call timeout must be positive and finite.")
        self.call_timeout_seconds = call_timeout_seconds
        self.managed_process_group = managed_process_group
        self.author = author.strip()
        if not self.author:
            raise ValueError("A tracked-change author is required.")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("inputs", "work", ".runs", "work/.tmp", "work/.home"):
            directory = self.root / name
            if directory.is_symlink():
                raise ValueError(f"Workspace directory must not be a symlink: {name}")
            directory.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.package = Path(__file__).resolve().parent.parent
        # Query the configured interpreter, never model-supplied code, for its runtime roots.
        info = subprocess.check_output(
            [str(self.python), "-I", "-c", "import json,sys; print(json.dumps([sys.prefix,sys.base_prefix]))"],
            text=True, timeout=10,
        )
        self.runtime_roots = [Path(p).resolve() for p in json.loads(info)]

    def path(self, relative: str, area: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root / area):
            raise ValueError(f"Path must be inside {area}/.")
        return path

    def info(self) -> dict:
        return {
            "prototype": "0.0.6", "author": self.author,
            "project_folder": str(self.project) if self.project else None,
            "cwd": str(self.root), "python": str(self.python),
            "inputs": sorted(str(p.relative_to(self.root)) for p in (self.root / "inputs").rglob("*") if p.is_file()),
            "outputs": self.outputs(),
            "recipe": str(self.package / "veqtor_workspace/RECIPE.md"),
            "recipe_text": (self.package / "veqtor_workspace/RECIPE.md").read_text(encoding="utf-8"),
            "execution": "Fresh Python process per call; files persist. Project and inputs read-only, work writable, no network. "
                         "For in-place editing use document_task(start), run_python/check_docx with task_id, then document_task(save).",
            "limits": {"default_timeout_seconds": self.call_timeout_seconds,
                       "max_timeout_seconds": self.call_timeout_seconds,
                       "scope": "Whole MCP call, including setup, checks and saving; no disabled deadline or queue.",
                       "bytes_per_written_file": None, "reply_bytes_per_stream": MAX_REPLY},
            "preview": {"configured": (self.root / ".preview.json").is_file(),
                        "usage": "check_docx(render=True); reuse preview_id with preview_pages for more pages",
                        "default_timeout_seconds": min(60, self.call_timeout_seconds),
                        "max_timeout_seconds": self.call_timeout_seconds, "max_images_per_reply": 3},
        }

    def outputs(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root)) for p in (self.root / "work").rglob("*")
            if p.is_file() and not any(part.startswith((".", "~$")) for part in p.relative_to(self.root / "work").parts)
            and not p.is_symlink()
        )[:100]

    def profile(self) -> str:
        read = [self.root, self.package, *self.runtime_roots,
                Path("/System"), Path("/usr/lib"), Path("/usr/share"), Path("/Library/Apple")]
        if self.project:
            read.append(self.project)
        paths = " ".join(f"(subpath {_quoted(p)})" for p in read)
        return f'''(version 1)
(deny default)
(allow process-fork process-exec sysctl-read)
(allow signal (target self))
(allow file-read-metadata)
(allow file-read* (literal "/"))
(allow file-read* {paths})
(allow file-read* file-write* (subpath {_quoted(self.root / "work")}))
(allow file-read* file-write* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))
'''

    def run(self, code: str, timeout_seconds: float | None = None) -> dict:
        if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
            raise ValueError("Code must contain 1–100,000 characters.")
        timeout_seconds = self.call_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= self.call_timeout_seconds:
            raise ValueError(f"timeout_seconds must be positive and at most {self.call_timeout_seconds}; no unlimited calls.")
        if not self.lock.acquire(blocking=False):
            raise ValueError("A script is already running in this workspace. Wait for its result.")
        try:
            return self._run(code, timeout_seconds)
        finally:
            self.lock.release()

    def _run(self, code: str, timeout_seconds: float) -> dict:
        run_id = uuid.uuid4().hex[:12]
        stem = self.root / ".runs" / run_id
        script = stem.with_suffix(".py")
        script.write_text(code, encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(self.package),
            "HOME": str(self.root / "work/.home"), "TMPDIR": str(self.root / "work/.tmp"),
            "VEQTOR_AUTHOR": self.author,
        }
        bootstrap = f"import runpy; runpy.run_path({str(script)!r},run_name='__main__')"
        timed_out = False
        with stem.with_suffix(".stdout").open("wb") as out, stem.with_suffix(".stderr").open("wb") as err:
            process = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-p", self.profile(), str(self.python), "-B", "-c", bootstrap],
                cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=not self.managed_process_group,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Also stop surviving children after an otherwise successful script.
                try:
                    if self.managed_process_group:
                        process.kill()  # The MCP supervisor also stops this call's entire process group.
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        result = {"run_id": run_id, "exit_code": process.returncode, "timed_out": timed_out,
                  "artifacts": self.outputs(), "verification": "not_run"}
        for kind in ("stdout", "stderr"):
            path = stem.with_suffix(f".{kind}")
            with path.open("rb") as stream:
                result[kind] = stream.read(MAX_REPLY).decode("utf-8", errors="replace")
            result[f"{kind}_truncated"] = path.stat().st_size > MAX_REPLY
            result[f"{kind}_path"] = str(path.relative_to(self.root))
        return result
