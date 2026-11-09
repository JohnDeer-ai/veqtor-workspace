# SPDX-License-Identifier: Apache-2.0
"""Snapshot one selected file, edit privately, then save it back to the same path."""

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

from veqtor_workspace.files import atomic_save
from veqtor_workspace.runner import Workspace


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class SaveConflict(Exception):
    """Abort the atomic replacement when a newer on-disk version is detected."""


class DocumentTasks:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.directory = workspace.root / ".runs/documents"

    def _context(self, folder: Path, read_paths: list[str] | None = None) -> Workspace:
        w = self.workspace
        return Workspace(folder, w.python, w.author, w.project, w.call_timeout_seconds, w.managed_process_group,
                         w.dynamic, read_paths, w.preview_config)

    @staticmethod
    def _record(folder: Path, data: dict):
        with atomic_save(folder / "task.json") as temporary:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def source(self, file: str) -> Path:
        project = self.workspace.project
        if project is None and not self.workspace.dynamic:
            raise ValueError("Choose the document folder with --project to edit files in place.")
        selected = Path(file).expanduser()
        if self.workspace.dynamic and not selected.is_absolute():
            raise ValueError("Select the document by its absolute path.")
        path = (project / selected if project else selected).resolve(strict=True)
        if ((project and not path.is_relative_to(project)) or path.is_relative_to(self.workspace.root)
                or not path.is_file()):
            raise ValueError("Select a regular document inside the project folder, outside its private state.")
        return path

    def start(self, file: str, read_paths: list[str] | None = None) -> dict:
        source = self.source(file)
        reads = [str(p) for p in self.workspace.resolve_reads([str(source), *(read_paths or [])])]
        task_id = uuid.uuid4().hex[:12]
        folder = self.directory / task_id
        child = self._context(folder, reads)
        original, candidate = f"inputs/{source.name}", f"work/{source.name}"
        try:
            shutil.copyfile(source, folder / original)
            original_hash = digest(folder / original)
            if digest(source) != original_hash:
                raise ValueError("The file changed while taking its snapshot. Start again from the saved version.")
            shutil.copyfile(folder / original, folder / candidate)
            data = {"task_id": task_id, "file": str(source), "original": original,
                    "candidate": candidate, "original_sha256": original_hash, "saved": False, "read_paths": reads}
            (folder / "task.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except BaseException:
            shutil.rmtree(folder)
            raise
        return {**data, "cwd": str(child.root), "snapshot": str(folder / original)}

    def read(self, task_id: str) -> tuple[dict, Path]:
        if not re.fullmatch(r"[0-9a-f]{12}", task_id):
            raise ValueError("Invalid task_id.")
        folder = (self.directory / task_id).resolve(strict=True)
        if not folder.is_relative_to(self.directory.resolve()) or not folder.is_relative_to(self.workspace.root):
            raise ValueError("Task must be inside the protected task directory.")
        data = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        if (not data["saved"] and data.get("save_sha256")
                and digest(self.source(data["file"])) == data["save_sha256"]):
            data.update(saved=True, saved_sha256=data["save_sha256"])
            try:
                self._record(folder, data)
            except OSError:
                # Even if state storage is still full, the result is known and editing stays closed.
                data["state_recovery_pending"] = True
        return data, folder

    def context(self, task_id: str, writing: bool = False) -> tuple[Workspace, dict]:
        data, folder = self.read(task_id)
        if writing and (data["saved"] or data.get("save_sha256")):
            raise ValueError("Saving has started for this task. Inspect its status. Start a new task before further edits.")
        return self._context(folder, data.get("read_paths")), data

    def status(self, task_id: str) -> dict:
        data, _ = self.read(task_id)
        current = digest(self.source(data["file"]))
        if data["saved"]:
            status = "saved" if current == data["saved_sha256"] else "saved_then_changed"
        else:
            status = "not_saved" if current == data["original_sha256"] else "outcome_unknown"
        return {"status": status, "file": data["file"], "saved": data["saved"],
                "editing_closed": bool(data["saved"] or data.get("save_sha256")),
                "state_recovery_pending": data.get("state_recovery_pending", False),
                "detail": "Read-only status. Unknown means the current file differs; do not overwrite it to resolve uncertainty."}

    def save(self, task_id: str) -> dict:
        data, folder = self.read(task_id)
        source = self.source(data["file"])
        current_hash = digest(source)
        if data["saved"]:
            return {"status": "already_saved", "file": str(source),
                    "current_file_matches_saved": current_hash == data["saved_sha256"],
                    "state_recovery_pending": data.get("state_recovery_pending", False)}
        candidate = (folder / data["candidate"]).resolve(strict=True)
        if not candidate.is_relative_to(folder / "work") or not candidate.is_file():
            raise ValueError("The result must be a regular file inside this task's work directory.")
        candidate_hash = digest(candidate)
        if data.get("save_sha256") and candidate_hash != data["save_sha256"]:
            raise ValueError("The prepared result changed after saving started. Start a new task from the project file.")
        if current_hash != data["original_sha256"]:
            return {"status": "conflict", "file": str(source), "saved": False,
                    "detail": "The project file differs from the baseline. An interrupted save may already have completed. "
                              "Inspect status and start a new task from the current file."}
        try:
            with atomic_save(source) as temporary:
                shutil.copyfile(candidate, temporary)
                shutil.copymode(source, temporary)
                if digest(temporary) != candidate_hash:
                    raise ValueError("The task result changed while saving; the project file was not replaced.")
                # Record the exact intended result BEFORE the irreversible replacement. An
                # interrupted or disk-full final status write can then be recovered by its hash.
                data["save_sha256"] = candidate_hash
                self._record(folder, data)
                if digest(source) != data["original_sha256"]:
                    raise SaveConflict
        except SaveConflict:
            return {"status": "conflict", "file": str(source), "saved": False,
                    "detail": "The project file changed during preparation; start a new task."}
        data.update(saved=True, saved_sha256=candidate_hash)
        pending = False
        try:
            self._record(folder, data)
        except OSError:
            pending = True  # The document IS saved; never turn this into a generic failure.
        return {"status": "saved", "file": str(source), "sha256": candidate_hash,
                "state_recovery_pending": pending,
                "snapshot": str(folder / data["original"]),
                "detail": "Replaced the file. Later script errors do not roll this save back."}
