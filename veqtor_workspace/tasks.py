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

    def source(self, file: str) -> Path:
        project = self.workspace.project
        if project is None:
            raise ValueError("Choose the document folder with --project to edit files in place.")
        path = (project / file).resolve(strict=True)
        if (not path.is_relative_to(project) or path.is_relative_to(self.workspace.root)
                or not path.is_file()):
            raise ValueError("Select a regular document inside the project folder, outside its private state.")
        return path

    def start(self, file: str) -> dict:
        source = self.source(file)
        task_id = uuid.uuid4().hex[:12]
        folder = self.directory / task_id
        child = Workspace(folder, self.workspace.python, self.workspace.author, self.workspace.project)
        original, candidate = f"inputs/{source.name}", f"work/{source.name}"
        try:
            shutil.copyfile(source, folder / original)
            original_hash = digest(folder / original)
            if digest(source) != original_hash:
                raise ValueError("The file changed while taking its snapshot. Start again from the saved version.")
            shutil.copyfile(folder / original, folder / candidate)
            data = {"task_id": task_id, "file": str(source), "original": original,
                    "candidate": candidate, "original_sha256": original_hash, "saved": False}
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
        return json.loads((folder / "task.json").read_text(encoding="utf-8")), folder

    def context(self, task_id: str, writing: bool = False) -> tuple[Workspace, dict]:
        data, folder = self.read(task_id)
        if writing and data["saved"]:
            raise ValueError("This task was saved. Start a new task to snapshot the latest file before further edits.")
        return Workspace(folder, self.workspace.python, self.workspace.author, self.workspace.project), data

    def save(self, task_id: str) -> dict:
        data, folder = self.read(task_id)
        source = self.source(data["file"])
        candidate = (folder / data["candidate"]).resolve(strict=True)
        if not candidate.is_relative_to(folder / "work") or not candidate.is_file():
            raise ValueError("The result must be a regular file inside this task's work directory.")
        candidate_hash, current_hash = digest(candidate), digest(source)
        if data["saved"]:
            return {"status": "already_saved", "file": str(source),
                    "current_file_matches_saved": current_hash == data["saved_sha256"]}
        if current_hash != data["original_sha256"]:
            return {"status": "conflict", "file": str(source), "saved": False,
                    "detail": "The project file changed since this task began. Start a new task from that version."}
        try:
            with atomic_save(source) as temporary:
                shutil.copyfile(candidate, temporary)
                shutil.copymode(source, temporary)
                if digest(temporary) != candidate_hash:
                    raise ValueError("The task result changed while saving; the project file was not replaced.")
                if digest(source) != data["original_sha256"]:
                    raise SaveConflict
        except SaveConflict:
            return {"status": "conflict", "file": str(source), "saved": False,
                    "detail": "The project file changed during preparation; start a new task."}
        data.update(saved=True, saved_sha256=candidate_hash)
        with atomic_save(folder / "task.json") as temporary:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {"status": "saved", "file": str(source), "sha256": candidate_hash,
                "snapshot": str(folder / data["original"]),
                "detail": "Replaced the file. Later script errors do not roll this save back."}
