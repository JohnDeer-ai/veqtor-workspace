# SPDX-License-Identifier: Apache-2.0
"""Small macOS-only Python runner with OS-enforced workspace access."""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import threading
import uuid
from pathlib import Path

MAX_REPLY = 12_000


def _quoted(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


class Workspace:
    def __init__(self, root: Path, python: Path, author: str):
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise RuntimeError("This prototype requires macOS sandbox-exec; no unrestricted fallback.")
        self.root = root.expanduser().resolve()
        self.python = python.expanduser().absolute()
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
            "prototype": "0.0.3", "author": self.author,
            "cwd": str(self.root), "python": str(self.python),
            "inputs": sorted(str(p.relative_to(self.root)) for p in (self.root / "inputs").rglob("*") if p.is_file()),
            "outputs": self.outputs(),
            "recipe": str(self.package / "veqtor_workspace/RECIPE.md"),
            "execution": "Fresh Python process per call; files persist. Inputs read-only, work writable, no network.",
            "limits": {"timeout_seconds": 120, "bytes_per_written_file": 8_388_608, "reply_bytes_per_stream": MAX_REPLY},
            "preview": {"configured": (self.root / ".preview.json").is_file(),
                        "usage": "check_docx(render=True); reuse preview_id with preview_pages for more pages",
                        "default_timeout_seconds": 60, "max_timeout_seconds": 300, "max_images_per_reply": 3},
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

    def run(self, code: str, timeout_seconds: int = 30) -> dict:
        if not isinstance(code, str) or not code.strip() or len(code) > 100_000:
            raise ValueError("Code must contain 1–100,000 characters.")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120.")
        if not self.lock.acquire(blocking=False):
            raise ValueError("A script is already running in this workspace. Wait for its result.")
        try:
            return self._run(code, timeout_seconds)
        finally:
            self.lock.release()

    def _run(self, code: str, timeout_seconds: int) -> dict:
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
        # Resource limits are applied inside the isolated interpreter before the task script.
        bootstrap = (
            "import resource,runpy; "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608)); "
            f"runpy.run_path({str(script)!r},run_name='__main__')"
        )
        timed_out = False
        with stem.with_suffix(".stdout").open("wb") as out, stem.with_suffix(".stderr").open("wb") as err:
            process = subprocess.Popen(
                ["/usr/bin/sandbox-exec", "-p", self.profile(), str(self.python), "-B", "-c", bootstrap],
                cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Also stop surviving children after an otherwise successful script.
                try:
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
