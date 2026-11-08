# SPDX-License-Identifier: Apache-2.0
"""Disposable document projects shared by round-trip and failure tests."""

import os
import platform
import sys
from pathlib import Path

import pytest

from veqtor_workspace.runner import Workspace
from veqtor_workspace.tasks import DocumentTasks


@pytest.fixture
def project(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("OS sandbox tests require macOS")
    folder = tmp_path / "договоры с пробелами"
    folder.mkdir()
    workspace = Workspace(folder / ".veqtor", Path(os.environ.get("VEQTOR_TEST_PYTHON", sys.executable)), "Ilya", folder)
    return workspace, DocumentTasks(workspace)
