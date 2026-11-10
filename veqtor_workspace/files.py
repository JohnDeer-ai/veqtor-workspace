# SPDX-License-Identifier: Apache-2.0
"""Optional safe saving for model-written Python, without another MCP tool."""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def atomic_save(destination: str | Path, *, overwrite: bool = True):
    """Write and validate the yielded temporary path, then replace the destination.

    An exception or interruption before replacement leaves the destination intact.
    This does not validate content or protect writes that bypass this helper. A
    forcibly stopped process can leave a hidden temporary file, not a partial result.
    With overwrite=False, publish only if the destination is still absent.
    """
    target = Path(destination)
    fd, name = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)  # Atomic creation: never replace a file that appeared meanwhile.
    finally:
        temporary.unlink(missing_ok=True)
