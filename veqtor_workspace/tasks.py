# SPDX-License-Identifier: Apache-2.0
"""Snapshot one selected file, edit privately, then replace it or publish a new file."""

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


def current_digest(path: Path) -> str | None:
    try:
        return digest(path)
    except FileNotFoundError:
        return None


def _read_hint(path: Path) -> str:
    return (f"To read the current on-disk file, call run_python(read_paths={json.dumps([str(path)], ensure_ascii=False)}, "
            "code=...), omitting task_id. No new task is needed for reading.")


class SaveConflict(Exception):
    """Abort the atomic replacement when a newer on-disk version is detected."""


class DocumentTasks:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.directory = workspace.root / ".runs/documents"

    def _context(self, folder: Path, read_paths: list[str] | None = None) -> Workspace:
        w = self.workspace
        child = Workspace(folder, w.python, w.author, w.project, w.call_timeout_seconds, w.managed_process_group,
                          w.dynamic, preview_config=w.preview_config, private_root=w.private_root)
        # These paths were resolved and validated at start, in protected task metadata.
        # Missing supporting material should fail only when code actually reads it.
        child.read_paths = [Path(p) for p in read_paths or []]
        return child

    @staticmethod
    def _record(folder: Path, data: dict):
        with atomic_save(folder / "task.json") as temporary:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def source(self, file: str, *, existing: bool = True) -> Path:
        project = self.workspace.project
        if project is None and not self.workspace.dynamic:
            raise ValueError("Choose the document folder with --project to edit files in place.")
        selected = Path(file).expanduser()
        if self.workspace.dynamic and not selected.is_absolute():
            raise ValueError("Select the document by its absolute path.")
        selected = project / selected if project else selected
        # A new destination must not follow an existing final symlink (including a dangling one).
        path = selected.resolve(strict=True) if existing else selected.parent.resolve(strict=True) / selected.name
        if ((project and not path.is_relative_to(project)) or path.is_relative_to(self.workspace.root)
                or (existing and not path.is_file())):
            raise ValueError("Select a file outside private state and within the configured project, if any.")
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
                and current_digest(Path(data.get("save_file", data["file"]))) == data["save_sha256"]):
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
            target = Path(data.get("save_file", data["file"]))
            raise ValueError("Saving has started for this task. Inspect document_task(action='status', task_id=...) "
                             "for its outcome. " + _read_hint(target) + " Start a new task before further edits.")
        return self._context(folder, data.get("read_paths") if writing else None), data

    def status(self, task_id: str) -> dict:
        data, _ = self.read(task_id)
        target = Path(data.get("save_file", data["file"]))
        current = current_digest(target)
        if data["saved"]:
            status = "saved" if current == data["saved_sha256"] else "saved_then_changed"
        else:
            baseline = data["original_sha256"] if target == Path(data["file"]) else None
            status = "not_saved" if current == baseline else "outcome_unknown"
        return {"status": status, "file": str(target), "saved": data["saved"],
                "editing_closed": bool(data["saved"] or data.get("save_sha256")),
                "state_recovery_pending": data.get("state_recovery_pending", False),
                "read_hint": _read_hint(target),
                "detail": "Read-only status. Unknown means the current file differs; do not overwrite it to resolve uncertainty."}

    def save(self, task_id: str, file: str | None = None) -> dict:
        data, folder = self.read(task_id)
        target = self.source(file or data.get("save_file", data["file"]), existing=False)
        if (data["saved"] or data.get("save_sha256")) and target != Path(data.get("save_file", data["file"])):
            raise ValueError("Saving already targeted another file. Inspect status; start a new task for a different destination.")
        new_file = target != Path(data["file"])
        current_hash = current_digest(target)
        if data["saved"]:
            return {"status": "already_saved", "file": str(target),
                    "current_file_matches_saved": current_hash == data["saved_sha256"],
                    "read_hint": _read_hint(target),
                    "state_recovery_pending": data.get("state_recovery_pending", False)}
        candidate = (folder / data["candidate"]).resolve(strict=True)
        if not candidate.is_relative_to(folder / "work") or not candidate.is_file():
            raise ValueError("The result must be a regular file inside this task's work directory.")
        candidate_hash = digest(candidate)
        if data.get("save_sha256") and candidate_hash != data["save_sha256"]:
            raise ValueError("The prepared result changed after saving started. Start a new task from the project file.")
        if new_file and (target.exists() or target.is_symlink()):
            return {"status": "conflict", "file": str(target), "saved": False,
                    "detail": "Destination already exists. Choose an unused name; no file was replaced."}
        if not new_file and current_hash != data["original_sha256"]:
            return {"status": "conflict", "file": str(target), "saved": False,
                    "detail": "The project file differs from the baseline. An interrupted save may already have completed. "
                              "Inspect status and start a new task from the current file."}
        try:
            with atomic_save(target, overwrite=not new_file) as temporary:
                shutil.copyfile(candidate, temporary)
                shutil.copymode(candidate if new_file else target, temporary)
                if digest(temporary) != candidate_hash:
                    raise ValueError("The task result changed while saving; the project file was not replaced.")
                # Record the exact intended result BEFORE the irreversible replacement. An
                # interrupted or disk-full final status write can then be recovered by its hash.
                data["save_sha256"] = candidate_hash
                data["save_file"] = str(target)
                self._record(folder, data)
                if not new_file and current_digest(target) != data["original_sha256"]:
                    raise SaveConflict
        except FileExistsError:
            return {"status": "conflict", "file": str(target), "saved": False,
                    "detail": "Destination appeared during saving. It was not replaced. Inspect status before continuing."}
        except SaveConflict:
            return {"status": "conflict", "file": str(target), "saved": False,
                    "detail": "The project file changed during preparation; start a new task."}
        data.update(saved=True, saved_sha256=candidate_hash)
        pending = False
        try:
            self._record(folder, data)
        except OSError:
            pending = True  # The document IS saved; never turn this into a generic failure.
        return {"status": "saved", "file": str(target), "sha256": candidate_hash,
                "read_hint": _read_hint(target),
                "state_recovery_pending": pending,
                "snapshot": str(folder / data["original"]),
                "detail": ("Created a new file; the source was not replaced." if new_file else "Replaced the file.")
                          + " Later script errors do not roll this save back."}
