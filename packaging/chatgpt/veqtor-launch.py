# SPDX-License-Identifier: Apache-2.0
"""Stable local entry point; called with Python isolated mode (-I)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from veqtor_workspace.server import main

if __name__ == "__main__":
    main()
